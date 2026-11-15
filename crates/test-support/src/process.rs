//! The bounded process runner: one child, in its own process group, under a pinned environment and a deadline.

use std::ffi::OsString;
use std::fmt;
use std::io::{self, Read, Write};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::PathBuf;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, Instant};

use crate::env::PinnedEnv;

/// How many bytes of one stream are captured; a child that writes more is failed.
const OUTPUT_LIMIT: usize = 8 * 1024 * 1024;

/// How long the streams may stay open once the process group has been killed and its leader reaped. Only a
/// descendant that left the group can hold them open.
const CLEANUP_GRACE: Duration = Duration::from_secs(2);

/// How many bytes of a stream the debug form of a result shows.
const DEBUG_LIMIT: usize = 4 * 1024;

/// One run of an executable.
#[derive(Clone, Debug)]
pub struct Invocation {
    /// Absolute: nothing is looked up on `PATH`.
    pub executable: PathBuf,
    pub args: Vec<OsString>,
    /// Absolute.
    pub cwd: PathBuf,
    /// The whole environment of the child.
    pub env: PinnedEnv,
    /// Written to the child's input, which is then closed. Empty means end of input at once.
    pub stdin: Vec<u8>,
    /// How long the child may run before its process group is killed.
    pub timeout: Duration,
}

/// What a child that ran to its end wrote and how it ended.
#[derive(Clone, PartialEq, Eq)]
pub struct Observation {
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub exit: Exit,
}

/// How a child ended. A non-zero code is an observation, not a failure.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Exit {
    Code(i32),
    Signal(i32),
}

/// An invocation that gave no observation, with what the child had written until then.
#[derive(Clone, PartialEq, Eq)]
pub struct InvocationFailure {
    pub kind: FailureKind,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

/// Why an invocation gave no observation.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FailureKind {
    /// The executable or the working directory is not an absolute path. Nothing was started.
    InvalidInvocation,
    /// The executable could not be started. Holds the cause: the executable, the working directory, and the
    /// error of the operating system.
    Spawn(String),
    /// The child outran its deadline and its process group was killed.
    Timeout,
    /// The child wrote more to one stream than is captured, and its process group was killed.
    OutputLimit,
    /// The child could not be reaped, or a descendant outside its process group kept a stream open.
    Cleanup,
}

impl fmt::Debug for Observation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Observation")
            .field("stdout", &Text(&self.stdout))
            .field("stderr", &Text(&self.stderr))
            .field("exit", &self.exit)
            .finish()
    }
}

impl fmt::Debug for InvocationFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("InvocationFailure")
            .field("kind", &self.kind)
            .field("stdout", &Text(&self.stdout))
            .field("stderr", &Text(&self.stderr))
            .finish()
    }
}

impl fmt::Display for InvocationFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match &self.kind {
            FailureKind::InvalidInvocation => {
                "the executable or the working directory is not an absolute path"
            }
            FailureKind::Spawn(cause) => {
                return write!(f, "the executable could not be started: {cause}");
            }
            FailureKind::Timeout => "the child outran its deadline and was killed",
            FailureKind::OutputLimit => "the child wrote more than is captured and was killed",
            FailureKind::Cleanup => "the child could not be reaped or left a stream open",
        })
    }
}

impl std::error::Error for InvocationFailure {}

impl InvocationFailure {
    fn before_output(kind: FailureKind) -> Self {
        Self {
            kind,
            stdout: Vec::new(),
            stderr: Vec::new(),
        }
    }
}

/// The bytes of a stream as text, cut short when long, for the debug form of a result.
struct Text<'a>(&'a [u8]);

impl fmt::Debug for Text<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let shown = &self.0[..self.0.len().min(DEBUG_LIMIT)];
        fmt::Debug::fmt(&String::from_utf8_lossy(shown), f)?;
        if shown.len() < self.0.len() {
            write!(f, " and {} more bytes", self.0.len() - shown.len())?;
        }
        Ok(())
    }
}

/// Runs `invocation` to its end or to its deadline, whichever comes first.
///
/// The child's environment is exactly `invocation.env`, its umask is 022, and it leads a process group of its
/// own. However the run ends, every process still in that group is killed and the child is reaped before this
/// returns.
pub fn invoke(invocation: Invocation) -> Result<Observation, InvocationFailure> {
    if !invocation.executable.is_absolute() || !invocation.cwd.is_absolute() {
        return Err(InvocationFailure::before_output(
            FailureKind::InvalidInvocation,
        ));
    }

    let mut command = Command::new(&invocation.executable);
    command
        .args(&invocation.args)
        .current_dir(&invocation.cwd)
        .env_clear()
        .envs(invocation.env.variables())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    // SAFETY: the closure runs in the forked child before it executes the program, where only
    // async-signal-safe calls are allowed. `umask` is one, takes an integer, and touches no memory.
    unsafe {
        command.pre_exec(|| {
            libc::umask(0o022);
            Ok(())
        });
    }
    let child = command.spawn().map_err(|error| {
        InvocationFailure::before_output(FailureKind::Spawn(format!(
            "{} in {}: {error}",
            invocation.executable.display(),
            invocation.cwd.display()
        )))
    })?;
    let mut group = Group {
        child,
        reaped: false,
    };

    let (events, inbox) = mpsc::channel();
    let stdout = Arc::new(Mutex::new(Capture::default()));
    let stderr = Arc::new(Mutex::new(Capture::default()));
    let mut open_streams = 0;
    if let Some(stream) = group.child.stdout.take() {
        open_streams += 1;
        let (capture, events) = (Arc::clone(&stdout), events.clone());
        std::thread::spawn(move || drain(stream, &capture, &events));
    }
    if let Some(stream) = group.child.stderr.take() {
        open_streams += 1;
        let (capture, events) = (Arc::clone(&stderr), events.clone());
        std::thread::spawn(move || drain(stream, &capture, &events));
    }
    match group.child.stdin.take() {
        Some(mut stream) if !invocation.stdin.is_empty() => {
            open_streams += 1;
            let (input, events) = (invocation.stdin, events.clone());
            std::thread::spawn(move || {
                // A child that ends without reading its input is no failure of the invocation.
                let _ = stream.write_all(&input);
                drop(stream);
                let _ = events.send(Event::Closed);
            });
        }
        _ => {}
    }
    let leader = group.leader();
    let exit_watch = std::thread::spawn(move || {
        wait_for_exit(leader);
        let _ = events.send(Event::Exited);
    });

    let deadline = Instant::now() + invocation.timeout;
    let mut closed_streams = 0;
    let timed_out = loop {
        match inbox.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
            Ok(Event::Closed) => closed_streams += 1,
            Ok(Event::Exited | Event::Overflowed) => break false,
            Err(_) => break true,
        }
    };

    // The leader is running or not yet reaped, so the group is still this child's own. Once it is killed the
    // leader has ended, the exit watch returns, and only then is the leader reaped and its id free for reuse.
    group.kill();
    let _ = exit_watch.join();
    let status = group.reap();

    let grace = Instant::now() + CLEANUP_GRACE;
    while closed_streams < open_streams {
        match inbox.recv_timeout(grace.saturating_duration_since(Instant::now())) {
            Ok(Event::Closed) => closed_streams += 1,
            Ok(Event::Exited | Event::Overflowed) => {}
            Err(_) => break,
        }
    }

    let (stdout, stdout_overflowed) = captured(&stdout);
    let (stderr, stderr_overflowed) = captured(&stderr);
    let kind = if timed_out {
        FailureKind::Timeout
    } else if stdout_overflowed || stderr_overflowed {
        FailureKind::OutputLimit
    } else if closed_streams < open_streams {
        FailureKind::Cleanup
    } else {
        match status.ok().and_then(exit_of) {
            Some(exit) => {
                return Ok(Observation {
                    stdout,
                    stderr,
                    exit,
                });
            }
            None => FailureKind::Cleanup,
        }
    };
    Err(InvocationFailure {
        kind,
        stdout,
        stderr,
    })
}

/// What the threads that serve one child tell the thread that runs it.
enum Event {
    /// The child ended; it has not been reaped.
    Exited,
    /// A stream held more than is captured.
    Overflowed,
    /// A stream of the child was read to its end, or written and closed.
    Closed,
}

/// The process group a child leads. Dropping it before the child is reaped kills the group and reaps the child,
/// so no way out of [`invoke`] leaves a process of the group behind.
///
/// A descendant that started a session or a group of its own is outside the group and is not killed. While it
/// holds a stream of the child open it is reported as [`FailureKind::Cleanup`]; once it has closed them nothing
/// here sees it, and it keeps running.
struct Group {
    child: Child,
    reaped: bool,
}

impl Group {
    fn leader(&self) -> libc::pid_t {
        self.child.id() as libc::pid_t
    }

    /// Kills every process of the group. Does nothing once the leader is reaped: its id may name another group.
    fn kill(&self) {
        if !self.reaped {
            // SAFETY: `kill` takes two integers and touches no memory. The child was started as the leader of
            // a new process group and has not been reaped, so its id still names that group and no other.
            unsafe { libc::kill(-self.leader(), libc::SIGKILL) };
        }
    }

    fn reap(&mut self) -> io::Result<ExitStatus> {
        self.reaped = true;
        self.child.wait()
    }
}

impl Drop for Group {
    fn drop(&mut self) {
        if !self.reaped {
            self.kill();
            let _ = self.reap();
        }
    }
}

/// Blocks until the child `leader` has ended, and leaves it unreaped: its id keeps naming it and its group.
fn wait_for_exit(leader: libc::pid_t) {
    loop {
        // SAFETY: `siginfo_t` is a plain C structure for which all zeroes is a valid value.
        let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
        // SAFETY: `info` is a valid, exclusively borrowed `siginfo_t`, the only memory `waitid` writes to.
        let result = unsafe {
            libc::waitid(
                libc::P_PID,
                leader as libc::id_t,
                &mut info,
                libc::WEXITED | libc::WNOWAIT,
            )
        };
        if result == 0 || io::Error::last_os_error().kind() != io::ErrorKind::Interrupted {
            return;
        }
    }
}

fn exit_of(status: ExitStatus) -> Option<Exit> {
    status
        .code()
        .map(Exit::Code)
        .or_else(|| status.signal().map(Exit::Signal))
}

/// What has been read of one stream.
#[derive(Default)]
struct Capture {
    bytes: Vec<u8>,
    /// The stream held more than [`OUTPUT_LIMIT`] bytes; `bytes` is its first [`OUTPUT_LIMIT`].
    overflowed: bool,
}

/// Reads `stream` into `capture` until its end or until it has given more than [`OUTPUT_LIMIT`] bytes.
fn drain(mut stream: impl Read, capture: &Mutex<Capture>, events: &Sender<Event>) {
    let mut chunk = vec![0; 64 * 1024];
    loop {
        let read = match stream.read(&mut chunk) {
            Ok(0) => break,
            Ok(read) => read,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        };
        let mut capture = capture.lock().unwrap_or_else(PoisonError::into_inner);
        let room = OUTPUT_LIMIT - capture.bytes.len();
        if read > room {
            capture.bytes.extend_from_slice(&chunk[..room]);
            capture.overflowed = true;
            let _ = events.send(Event::Overflowed);
            break;
        }
        capture.bytes.extend_from_slice(&chunk[..read]);
    }
    let _ = events.send(Event::Closed);
}

/// Takes what has been read of a stream, and whether the stream overflowed.
fn captured(capture: &Mutex<Capture>) -> (Vec<u8>, bool) {
    let mut capture = capture.lock().unwrap_or_else(PoisonError::into_inner);
    (std::mem::take(&mut capture.bytes), capture.overflowed)
}
