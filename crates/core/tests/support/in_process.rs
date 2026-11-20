//! The scaffold that runs the library in process: the one place a component test builds an `Invocation`.
//!
//! A component test file declares it with `#[path = "support/in_process.rs"] mod in_process;`. Cargo builds no
//! test target from this file, because it lies in a subdirectory of `tests/` that holds no `main.rs`.

#![allow(dead_code, reason = "each test target uses part of the scaffold")]

use std::cell::RefCell;
use std::ffi::OsString;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use serde_json::Value;
use winter_workspace_core::{
    CommandTracer, Identity, Interrupts, Invocation, ProcessEnv, Termination, TraceLog, run,
};
use winter_workspace_test_support::PinnedEnv;

/// One invocation of the library, over handles the scaffold owns.
pub struct InProcess {
    args: Vec<OsString>,
    cwd: io::Result<PathBuf>,
    env: Vec<(OsString, OsString)>,
    stdin: Vec<u8>,
    stdout_is_terminal: bool,
    stderr_is_terminal: bool,
    interrupts: Interrupts,
}

impl InProcess {
    /// An invocation of `args` in the directory `/`, with an empty environment, an empty stdin, neither
    /// output stream a terminal, and no interrupt pending.
    pub fn new(args: &[&str]) -> Self {
        Self {
            args: args.iter().map(OsString::from).collect(),
            cwd: Ok(PathBuf::from("/")),
            env: Vec::new(),
            stdin: Vec::new(),
            stdout_is_terminal: false,
            stderr_is_terminal: false,
            interrupts: Interrupts::new(),
        }
    }

    /// The same invocation for arguments that need not be text.
    pub fn os_args(args: Vec<OsString>) -> Self {
        Self {
            args,
            ..Self::new(&[])
        }
    }

    /// Sets the working directory.
    pub fn cwd(mut self, cwd: &Path) -> Self {
        self.cwd = Ok(cwd.to_path_buf());
        self
    }

    /// Makes the working directory one the process could not read: the invocation carries `error` in its
    /// place.
    pub fn cwd_error(mut self, error: io::Error) -> Self {
        self.cwd = Err(error);
        self
    }

    /// Adds one variable to the environment; a later value replaces an earlier one of the same key.
    pub fn env(mut self, key: &str, value: &str) -> Self {
        self.env.push((OsString::from(key), OsString::from(value)));
        self
    }

    /// Makes the environment the whole of `pinned`, the pinned environment of a tree, in place of what was
    /// set before. An invocation that starts a program takes this: the program is then found on the pinned
    /// `PATH` and gets nothing but the pinned variables and those added after this call.
    pub fn pinned(mut self, pinned: &PinnedEnv) -> Self {
        self.env = pinned
            .variables()
            .map(|(name, value)| (name.to_owned(), value.to_owned()))
            .collect();
        self
    }

    /// Sets the bytes stdin holds.
    pub fn stdin(mut self, bytes: &[u8]) -> Self {
        self.stdin = bytes.to_vec();
        self
    }

    /// Makes an interrupt pending: the invocation runs as one whose command deferred the interrupt and whose
    /// handler then noted one.
    pub fn interrupt_pending(self) -> Self {
        self.interrupts.defer();
        self.interrupts.note();
        self
    }

    /// Runs the invocation with an in-memory stdout and stderr, and returns both as text.
    pub fn run(self) -> Ended {
        let mut stdout = Vec::new();
        let ended = self.run_with_stdout(&mut stdout);
        Ended {
            stdout: String::from_utf8(stdout).expect("UTF-8 on stdout"),
            ..ended
        }
    }

    /// Runs the invocation with `stdout` as its stdout, an in-memory stderr, and a tracer that records what
    /// the library announces to it.
    ///
    /// The scaffold cannot read back what a handle of the test's own was given, so `stdout` of the returned
    /// value is empty.
    pub fn run_with_stdout(self, stdout: &mut dyn Write) -> Ended {
        let mut stdin: &[u8] = &self.stdin;
        let mut stderr = Vec::new();
        let tracer = Recording::default();
        let termination = run(Invocation {
            args: self.args,
            cwd: self.cwd,
            env: ProcessEnv::from_vars(self.env),
            identity: Identity {
                program: "winter-workspace",
                version: "0.1.0",
            },
            stdin: &mut stdin,
            stdout,
            stderr: &mut stderr,
            stdout_is_terminal: self.stdout_is_terminal,
            stderr_is_terminal: self.stderr_is_terminal,
            interrupts: &self.interrupts,
            tracer: &tracer,
        });
        Ended {
            termination,
            stdout: String::new(),
            stderr: String::from_utf8(stderr).expect("UTF-8 on stderr"),
            announced: tracer.announced.into_inner(),
        }
    }
}

/// What an invocation ended with: how the library says it ends, and what it wrote.
pub struct Ended {
    pub termination: Termination,
    pub stdout: String,
    pub stderr: String,
    /// The command path the library announced to the tracer of the invocation, or `None` when it announced
    /// none.
    pub announced: Option<Vec<String>>,
}

/// The assertions more than one component test file makes on an ending. Each panics with what it found, at
/// the line of the test that called it, and `subject` names the case in that message.
impl Ended {
    /// A success that wrote one JSON document and nothing else: asserts `Termination::Success` and an empty
    /// stderr, and answers stdout parsed as JSON.
    #[track_caller]
    pub fn document(&self, subject: &str) -> Value {
        assert!(
            matches!(self.termination, Termination::Success),
            "{subject}: {:?}",
            self.termination
        );
        assert_eq!(self.stderr, "", "{subject}: stderr");
        match serde_json::from_str(&self.stdout) {
            Ok(document) => document,
            Err(error) => panic!("{subject}: {error}: {:?}", self.stdout),
        }
    }

    /// An ending that wrote nothing: asserts an empty stdout, an empty stderr, and the exit code `exit_code`,
    /// and answers the termination, for the test to match its variant.
    #[track_caller]
    pub fn wrote_nothing(self, exit_code: u8, subject: &str) -> Termination {
        assert_eq!(self.stdout, "", "{subject}: stdout");
        assert_eq!(self.stderr, "", "{subject}: stderr");
        assert_eq!(
            self.termination.exit_code(),
            exit_code,
            "{subject}: exit code"
        );
        self.termination
    }
}

/// The tracer of the scaffold: it keeps the command path it is told, and fails when it is told a second one.
#[derive(Default)]
struct Recording {
    announced: RefCell<Option<Vec<String>>>,
}

impl CommandTracer for Recording {
    fn command_started(&self, command_path: &[String], _: TraceLog) {
        let earlier = self.announced.replace(Some(command_path.to_vec()));
        assert_eq!(earlier, None, "a command was announced twice");
    }
}
