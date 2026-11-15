//! Steps: the child processes a driver command runs, each in its own process group and bounded by a deadline.

use std::ffi::OsString;
use std::io::Write;
use std::os::unix::process::CommandExt;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

/// One child process of a driver command.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Step {
    /// The name the summary line spells the step with.
    pub name: &'static str,
    pub program: PathBuf,
    pub args: Vec<OsString>,
}

/// How long one step may run before its process group is killed and the step is failed.
pub const STEP_DEADLINE: Duration = Duration::from_secs(15 * 60);

/// How often a running step is checked for its exit, its deadline, and an interrupt.
const POLL_INTERVAL: Duration = Duration::from_millis(20);

/// Set by the signal handler once SIGINT or SIGTERM has arrived.
static INTERRUPTED: AtomicBool = AtomicBool::new(false);

extern "C" fn record_interrupt(_signal: libc::c_int) {
    INTERRUPTED.store(true, Ordering::SeqCst);
}

/// Makes SIGINT and SIGTERM end the running step instead of the driver: a step runs in its own process group,
/// which a signal sent to the driver's group does not reach, so the driver has to kill it itself.
pub(crate) fn watch_interrupts() {
    let handler = record_interrupt as extern "C" fn(libc::c_int) as libc::sighandler_t;
    for signal in [libc::SIGINT, libc::SIGTERM] {
        // SAFETY: `record_interrupt` is an `extern "C" fn(c_int)`, the type a signal handler has, and it is
        // async-signal-safe: its whole body is one atomic store.
        unsafe { libc::signal(signal, handler) };
    }
}

/// How one step ended.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum Outcome {
    Passed,
    /// It exited unsuccessfully, outran the deadline, or could not start.
    Failed,
    /// SIGINT or SIGTERM arrived while it ran; its process group was killed.
    Interrupted,
}

/// Runs `step` to its end, to `deadline`, or to an interrupt, whichever comes first. The child inherits the
/// driver's environment and its stdout and stderr; it reads no input.
pub(crate) fn run_step(step: &Step, deadline: Duration, err: &mut dyn Write) -> Outcome {
    let spawned = Command::new(&step.program)
        .args(&step.args)
        .stdin(Stdio::null())
        .process_group(0)
        .spawn();
    let mut child = match spawned {
        Ok(child) => child,
        Err(error) => {
            let _ = writeln!(
                err,
                "{}: cannot start {}: {error}",
                step.name,
                step.program.display()
            );
            return Outcome::Failed;
        }
    };

    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => return Outcome::Passed,
            Ok(Some(_)) => return Outcome::Failed,
            Ok(None) => {}
            Err(error) => {
                let _ = writeln!(err, "{}: cannot wait for the step: {error}", step.name);
                kill_group(&mut child);
                return Outcome::Failed;
            }
        }
        if INTERRUPTED.load(Ordering::SeqCst) {
            kill_group(&mut child);
            return Outcome::Interrupted;
        }
        if started.elapsed() >= deadline {
            kill_group(&mut child);
            let _ = writeln!(
                err,
                "{}: killed after running for {} seconds",
                step.name,
                deadline.as_secs()
            );
            return Outcome::Failed;
        }
        std::thread::sleep(POLL_INTERVAL);
    }
}

/// Kills every process of the group `child` leads, then reaps `child`.
fn kill_group(child: &mut Child) {
    let group = child.id() as libc::pid_t;
    // SAFETY: `kill` takes two integers and touches no memory. The child was started as the leader of a new
    // process group and has not been reaped, so its id still names that group and no other.
    unsafe { libc::kill(-group, libc::SIGKILL) };
    let _ = child.wait();
}

/// Runs all of `steps` in order, each bounded by `deadline`, a later one whether or not an earlier one failed,
/// and writes the summary line of the gate `name` to `err`. Returns 0 when every step passed and 1 otherwise.
///
/// An interrupt fails the running step and ends the run: the steps after it do not start.
pub fn run_steps(name: &str, steps: &[Step], deadline: Duration, err: &mut dyn Write) -> u8 {
    let mut failed = Vec::new();
    for step in steps {
        match run_step(step, deadline, err) {
            Outcome::Passed => {}
            Outcome::Failed => failed.push(step.name),
            Outcome::Interrupted => {
                failed.push(step.name);
                break;
            }
        }
    }

    if failed.is_empty() {
        let _ = writeln!(err, "gate {name}: ok");
        0
    } else {
        let _ = writeln!(err, "gate {name}: failed: {}", failed.join(", "));
        1
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scratch::Scratch;

    /// A step that runs `script` with `/bin/sh`; `argument` is the script's `$1`.
    fn shell(name: &'static str, script: &str, argument: &std::path::Path) -> Step {
        Step {
            name,
            program: "/bin/sh".into(),
            args: vec![
                "-c".into(),
                script.into(),
                "sh".into(),
                argument.as_os_str().to_owned(),
            ],
        }
    }

    fn group_is_alive(group: libc::pid_t) -> bool {
        // SAFETY: `kill` takes two integers and touches no memory; signal 0 delivers nothing.
        unsafe { libc::kill(-group, 0) == 0 }
    }

    #[test]
    fn all_steps_passing_is_ok() {
        let scratch = Scratch::new("steps-ok");
        let steps = [
            shell("first", "exit 0", scratch.path()),
            shell("second", "exit 0", scratch.path()),
        ];
        let mut err = Vec::new();

        let code = run_steps("full", &steps, STEP_DEADLINE, &mut err);

        assert_eq!(code, 0);
        assert_eq!(String::from_utf8(err).unwrap(), "gate full: ok\n");
    }

    #[test]
    fn a_failed_step_is_named_and_the_steps_after_it_still_run() {
        let scratch = Scratch::new("steps-failed");
        let marker = scratch.path().join("third-ran");
        let steps = [
            shell("first", "exit 0", &marker),
            shell("second", "exit 3", &marker),
            shell("third", ": > \"$1\"", &marker),
        ];
        let mut err = Vec::new();

        let code = run_steps("slice", &steps, STEP_DEADLINE, &mut err);

        assert_eq!(code, 1);
        assert_eq!(
            String::from_utf8(err).unwrap(),
            "gate slice: failed: second\n"
        );
        assert!(marker.exists());
    }

    #[test]
    fn every_failed_step_is_named_in_order() {
        let scratch = Scratch::new("steps-two-failed");
        let steps = [
            shell("first", "exit 1", scratch.path()),
            shell("second", "exit 0", scratch.path()),
            shell("third", "exit 1", scratch.path()),
        ];
        let mut err = Vec::new();

        let code = run_steps("slice", &steps, STEP_DEADLINE, &mut err);

        assert_eq!(code, 1);
        assert_eq!(
            String::from_utf8(err).unwrap(),
            "gate slice: failed: first, third\n"
        );
    }

    #[test]
    fn a_step_that_cannot_start_is_failed() {
        let scratch = Scratch::new("steps-no-program");
        let steps = [Step {
            name: "absent",
            program: scratch.path().join("no-such-program"),
            args: Vec::new(),
        }];
        let mut err = Vec::new();

        let code = run_steps("slice", &steps, STEP_DEADLINE, &mut err);

        assert_eq!(code, 1);
        let err = String::from_utf8(err).unwrap();
        assert_eq!(err.lines().last(), Some("gate slice: failed: absent"));
    }

    #[test]
    fn a_step_past_its_deadline_is_failed_and_its_process_group_is_killed() {
        let scratch = Scratch::new("steps-deadline");
        let group_file = scratch.path().join("group");
        // The shell leads the step's process group, so its own id is the group's; the sleep it starts in the
        // background is a second member that outlives a kill of the shell alone.
        let steps = [shell(
            "slow",
            "echo $$ > \"$1\"; sleep 60 & wait",
            &group_file,
        )];
        let mut err = Vec::new();

        let started = Instant::now();
        let code = run_steps("slice", &steps, Duration::from_secs(1), &mut err);

        assert_eq!(code, 1);
        assert!(started.elapsed() < Duration::from_secs(30));
        let err = String::from_utf8(err).unwrap();
        assert_eq!(err.lines().last(), Some("gate slice: failed: slow"));

        let group: libc::pid_t = std::fs::read_to_string(&group_file)
            .unwrap()
            .trim()
            .parse()
            .unwrap();
        // The shell is reaped by the driver; the killed sleep is reaped by whichever process adopted it, which
        // can take a moment, and until then it still counts as a member.
        let waited = Instant::now();
        while group_is_alive(group) && waited.elapsed() < Duration::from_secs(10) {
            std::thread::sleep(POLL_INTERVAL);
        }
        assert!(!group_is_alive(group));
    }
}
