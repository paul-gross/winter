//! The entry of the library: what the binary hands over, and `run`.

use std::backtrace::Backtrace;
use std::collections::HashMap;
use std::ffi::{OsStr, OsString};
use std::io::{BufRead, Write};
use std::path::PathBuf;

use crate::context::Context;
use crate::termination::{InternalError, ParseFault, Termination, UsageError};

/// The name and version the binary reports.
pub struct Identity {
    /// The program name; never derived from the first process argument.
    pub program: &'static str,
    /// The version of the binary package.
    pub version: &'static str,
}

/// A snapshot of the process environment, taken once by the binary.
pub struct ProcessEnv(HashMap<OsString, OsString>);

impl ProcessEnv {
    /// Builds the snapshot from name and value pairs; a later pair replaces an earlier one of the same name.
    pub fn from_vars(vars: impl IntoIterator<Item = (OsString, OsString)>) -> Self {
        Self(vars.into_iter().collect())
    }

    /// The value of `key`, or `None` when the snapshot does not hold it.
    pub fn get(&self, key: &str) -> Option<&OsStr> {
        self.0.get(OsStr::new(key)).map(OsString::as_os_str)
    }
}

/// Everything one invocation is given by the process that hosts it.
pub struct Invocation<'a> {
    /// The process arguments without the program name.
    pub args: Vec<OsString>,
    pub cwd: PathBuf,
    pub env: ProcessEnv,
    pub identity: Identity,
    pub stdin: &'a mut dyn BufRead,
    pub stdout: &'a mut dyn Write,
    pub stderr: &'a mut dyn Write,
    pub stdout_is_terminal: bool,
    pub stderr_is_terminal: bool,
}

/// Parses, dispatches, and returns how the invocation ends.
///
/// Writes results to the handles of `invocation`. Never writes the final diagnostic and never exits: the caller
/// renders the returned [`Termination`] and maps its exit code.
pub fn run(invocation: Invocation<'_>) -> Termination {
    let Invocation {
        args,
        cwd,
        env,
        identity,
        stdin,
        stdout,
        stderr,
        stdout_is_terminal,
        stderr_is_terminal,
    } = invocation;
    let cx = Context {
        cwd: &cwd,
        env: &env,
        identity: &identity,
        stdin,
        out: stdout,
        err: stderr,
        out_is_terminal: stdout_is_terminal,
        err_is_terminal: stderr_is_terminal,
    };

    if let [only] = args.as_slice()
        && only == "--version"
    {
        return match writeln!(cx.out, "{} {}", cx.identity.program, cx.identity.version) {
            Ok(()) => Termination::Success,
            Err(error) => Termination::Internal(InternalError::Unexpected {
                source: Box::new(error),
                backtrace: Backtrace::capture(),
            }),
        };
    }

    Termination::Usage(UsageError::Parse {
        command_path: Vec::new(),
        fault: ParseFault::UnexpectedArgument {
            values: args
                .iter()
                .map(|arg| arg.to_string_lossy().into_owned())
                .collect(),
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn run_with(args: &[&str]) -> (Termination, Vec<u8>, Vec<u8>) {
        let mut stdin: &[u8] = &[];
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();
        let termination = run(Invocation {
            args: args.iter().map(OsString::from).collect(),
            cwd: PathBuf::from("/"),
            env: ProcessEnv::from_vars([]),
            identity: Identity {
                program: "winter-workspace",
                version: "0.1.0",
            },
            stdin: &mut stdin,
            stdout: &mut stdout,
            stderr: &mut stderr,
            stdout_is_terminal: false,
            stderr_is_terminal: false,
        });
        (termination, stdout, stderr)
    }

    #[test]
    fn version_is_written_to_stdout() {
        let (termination, stdout, stderr) = run_with(&["--version"]);

        assert!(matches!(termination, Termination::Success));
        assert_eq!(stdout, b"winter-workspace 0.1.0\n");
        assert!(stderr.is_empty());
    }

    #[test]
    fn unrecognised_argument_is_a_usage_error_and_writes_nothing() {
        let (termination, stdout, stderr) = run_with(&["--nope"]);

        let Termination::Usage(UsageError::Parse {
            command_path,
            fault,
        }) = termination
        else {
            panic!("expected a usage termination, got {termination:?}");
        };
        assert!(command_path.is_empty());
        assert!(matches!(fault, ParseFault::UnexpectedArgument { values } if values == ["--nope"]));
        assert!(stdout.is_empty());
        assert!(stderr.is_empty());
    }

    #[test]
    fn no_argument_is_a_usage_error() {
        let (termination, stdout, stderr) = run_with(&[]);

        assert!(matches!(termination, Termination::Usage(_)));
        assert!(stdout.is_empty());
        assert!(stderr.is_empty());
    }

    #[test]
    fn process_env_returns_the_value_of_a_present_key_only() {
        let env = ProcessEnv::from_vars([
            (OsString::from("HOME"), OsString::from("/home/someone")),
            (OsString::from("EDITOR"), OsString::from("vi")),
        ]);

        assert_eq!(env.get("EDITOR"), Some(OsStr::new("vi")));
        assert_eq!(env.get("SHELL"), None);
    }
}
