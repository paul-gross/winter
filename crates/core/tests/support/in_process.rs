//! The scaffold that runs the library in process: the one place a component test builds an `Invocation`.
//!
//! A component test file declares it with `#[path = "support/in_process.rs"] mod in_process;`. Cargo builds no
//! test target from this file, because it lies in a subdirectory of `tests/` that holds no `main.rs`.

#![allow(dead_code, reason = "each test target uses part of the scaffold")]

use std::ffi::OsString;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use winter_workspace_core::{Identity, Interrupts, Invocation, ProcessEnv, Termination, run};

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

    /// Runs the invocation with `stdout` as its stdout and an in-memory stderr.
    ///
    /// The scaffold cannot read back what a handle of the test's own was given, so `stdout` of the returned
    /// value is empty.
    pub fn run_with_stdout(self, stdout: &mut dyn Write) -> Ended {
        let mut stdin: &[u8] = &self.stdin;
        let mut stderr = Vec::new();
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
        });
        Ended {
            termination,
            stdout: String::new(),
            stderr: String::from_utf8(stderr).expect("UTF-8 on stderr"),
        }
    }
}

/// What an invocation ended with: how the library says it ends, and what it wrote.
pub struct Ended {
    pub termination: Termination,
    pub stdout: String,
    pub stderr: String,
}
