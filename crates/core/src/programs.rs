//! The program port: how the library starts a program and waits for it, and its one adapter over the operating
//! system.
//!
//! The only source of the library that starts a process. The environment of a child is built from the
//! environment snapshot of the invocation and never inherited from the process.

use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};

use crate::process::ProcessEnv;

/// A program the library starts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Program {
    Git,
}

impl Program {
    /// The name the program is looked up by.
    pub fn name(self) -> &'static str {
        match self {
            Self::Git => "git",
        }
    }
}

/// One start of a program: its arguments, the subcommand first, and its working directory.
#[derive(Debug, Clone, Copy)]
pub struct Call<'a> {
    pub program: Program,
    pub args: &'a [&'a OsStr],
    pub cwd: &'a Path,
}

/// What a program that ran to its end wrote, and how it ended.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Finished {
    pub exit: Exit,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

/// How a program ended: with an exit code, or by a signal of that number.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Exit {
    Code(i32),
    Signal(i32),
}

impl Exit {
    /// The exit as one number: the code, or minus the number of the signal.
    pub fn number(self) -> i32 {
        match self {
            Self::Code(code) => code,
            Self::Signal(signal) => -signal,
        }
    }
}

/// Starts programs.
pub trait RunPrograms {
    /// Starts the program of `call` and waits for it, however long it runs. Its stdin reads empty, and its
    /// stdout and stderr are captured whole. An error only when the program cannot be started.
    fn run(&self, call: &Call<'_>) -> io::Result<Finished>;
}

/// The variable of the snapshot that no child is given.
const LAUNCH_TIME: &str = "WINTER_LAUNCH_TIME";

/// The variable that names the command git reaches a remote over SSH with, and its value when the snapshot
/// does not hold the variable.
const SSH_COMMAND: &str = "GIT_SSH_COMMAND";
const SSH_COMMAND_DEFAULT: &str =
    "ssh -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3";

/// The variables that decide the language of what git writes, each set to `C` so that its output reads the
/// same on every host.
const MESSAGE_LOCALE: [&str; 2] = ["LANGUAGE", "LC_ALL"];

/// The directories searched when the snapshot holds no `PATH`.
const DEFAULT_PATH: &str = "/bin:/usr/bin";

/// The programs of the operating system.
pub struct OsPrograms {
    /// The whole environment of a child.
    environment: BTreeMap<OsString, OsString>,
    /// `PATH` as the snapshot holds it.
    path: Option<OsString>,
}

impl OsPrograms {
    /// The programs as an invocation with the environment `snapshot` starts them.
    pub fn new(snapshot: &ProcessEnv) -> Self {
        let environment = child_environment(snapshot.pairs());
        let path = environment.get(OsStr::new("PATH")).cloned();
        Self { environment, path }
    }
}

/// The environment of a git child, from the names and values of the snapshot: each as given, without
/// [`LAUNCH_TIME`], with [`SSH_COMMAND`] added when the snapshot holds no such name, an empty value being
/// one, and with each of [`MESSAGE_LOCALE`] set to `C`.
fn child_environment<'s>(
    snapshot: impl Iterator<Item = (&'s OsStr, &'s OsStr)>,
) -> BTreeMap<OsString, OsString> {
    let mut environment: BTreeMap<OsString, OsString> = snapshot
        .filter(|(name, _)| *name != OsStr::new(LAUNCH_TIME))
        .map(|(name, value)| (name.to_owned(), value.to_owned()))
        .collect();
    environment
        .entry(SSH_COMMAND.into())
        .or_insert_with(|| SSH_COMMAND_DEFAULT.into());
    for name in MESSAGE_LOCALE {
        environment.insert(name.into(), "C".into());
    }
    environment
}

impl RunPrograms for OsPrograms {
    fn run(&self, call: &Call<'_>) -> io::Result<Finished> {
        let name = call.program.name();
        let program = find_program(name, self.path.as_deref(), call.cwd, &is_executable)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::NotFound,
                    format!("no executable file named {name} in a directory of PATH"),
                )
            })?;
        let output = std::process::Command::new(program)
            .args(call.args)
            .current_dir(call.cwd)
            .env_clear()
            .envs(&self.environment)
            .stdin(std::process::Stdio::null())
            .output()?;
        let exit = match (output.status.code(), output.status.signal()) {
            (Some(code), _) => Exit::Code(code),
            (None, Some(signal)) => Exit::Signal(signal),
            (None, None) => {
                return Err(io::Error::other(format!(
                    "{name} ended with neither an exit code nor a signal"
                )));
            }
        };
        Ok(Finished {
            exit,
            stdout: output.stdout,
            stderr: output.stderr,
        })
    }
}

/// Whether `path` is a regular file that someone may execute, symbolic links followed.
fn is_executable(path: &Path) -> bool {
    path.metadata()
        .is_ok_and(|metadata| metadata.is_file() && metadata.permissions().mode() & 0o111 != 0)
}

/// Where the program `name` is on the `PATH` value `path`: the file of that name in the first entry for
/// which `executable` holds, or `None` when it holds in none.
///
/// The entries are separated by `:` and tried in order. An entry is read from `cwd`, the working directory
/// of the call, so an empty entry is that directory. Without a `PATH` the entries are those of
/// [`DEFAULT_PATH`].
pub(crate) fn find_program(
    name: &str,
    path: Option<&OsStr>,
    cwd: &Path,
    executable: &dyn Fn(&Path) -> bool,
) -> Option<PathBuf> {
    path.unwrap_or(OsStr::new(DEFAULT_PATH))
        .as_bytes()
        .split(|byte| *byte == b':')
        .map(|entry| cwd.join(OsStr::from_bytes(entry)).join(name))
        .find(|candidate| executable(candidate))
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use super::*;

    fn found(path: Option<&str>, executables: &[&str]) -> (Option<PathBuf>, Vec<PathBuf>) {
        let asked = RefCell::new(Vec::new());
        let found = find_program(
            "git",
            path.map(OsStr::new),
            Path::new("/work"),
            &|candidate| {
                asked.borrow_mut().push(candidate.to_path_buf());
                executables.iter().any(|held| Path::new(held) == candidate)
            },
        );
        (found, asked.into_inner())
    }

    fn environment(snapshot: &[(&str, &str)]) -> BTreeMap<OsString, OsString> {
        child_environment(
            snapshot
                .iter()
                .map(|(name, value)| (OsStr::new(*name), OsStr::new(*value))),
        )
    }

    #[test]
    fn the_first_entry_that_holds_an_executable_wins() {
        let (program, asked) = found(Some("/a:/b:/c"), &["/b/git", "/c/git"]);

        assert_eq!(program, Some(PathBuf::from("/b/git")));
        assert_eq!(asked, [Path::new("/a/git"), Path::new("/b/git")]);
    }

    #[test]
    fn an_entry_whose_file_is_not_executable_is_skipped() {
        // The predicate is what tells a file that may be executed from one that may not.
        let (program, asked) = found(Some("/plain:/real"), &["/real/git"]);

        assert_eq!(program, Some(PathBuf::from("/real/git")));
        assert_eq!(asked.len(), 2);
    }

    #[test]
    fn an_empty_entry_is_the_working_directory() {
        for path in [":/a", "/a:", "/a::/b", ""] {
            let (program, _) = found(Some(path), &["/work/git"]);

            assert_eq!(program, Some(PathBuf::from("/work/git")), "{path:?}");
        }
    }

    #[test]
    fn a_relative_entry_is_read_from_the_working_directory() {
        let (program, _) = found(Some("tools"), &["/work/tools/git"]);

        assert_eq!(program, Some(PathBuf::from("/work/tools/git")));
    }

    #[test]
    fn an_unset_path_searches_bin_and_then_usr_bin() {
        let (program, asked) = found(None, &[]);

        assert_eq!(program, None);
        assert_eq!(asked, [Path::new("/bin/git"), Path::new("/usr/bin/git")]);

        let (program, _) = found(None, &["/usr/bin/git", "/bin/git"]);
        assert_eq!(program, Some(PathBuf::from("/bin/git")));
    }

    #[test]
    fn no_entry_with_an_executable_finds_nothing() {
        assert_eq!(found(Some("/a:/b"), &["/c/git"]).0, None);
    }

    #[test]
    fn a_child_gets_the_snapshot_without_the_launch_time() {
        let environment = environment(&[
            ("HOME", "/home/someone"),
            ("WINTER_LAUNCH_TIME", "1700000000000000000"),
            (
                "TRACEPARENT",
                "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
            ),
            ("TRACESTATE", "vendor=value"),
            ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1"),
        ]);

        let names: Vec<&OsStr> = environment.keys().map(OsString::as_os_str).collect();
        assert_eq!(
            names,
            [
                "GIT_SSH_COMMAND",
                "HOME",
                "LANGUAGE",
                "LC_ALL",
                "OTEL_EXPORTER_OTLP_ENDPOINT",
                "TRACEPARENT",
                "TRACESTATE"
            ]
        );
        assert_eq!(environment[OsStr::new("HOME")], "/home/someone");
        assert_eq!(
            environment[OsStr::new("TRACEPARENT")],
            "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        );
        assert_eq!(environment[OsStr::new("TRACESTATE")], "vendor=value");
    }

    #[test]
    fn the_ssh_command_is_the_default_only_when_the_snapshot_has_none() {
        assert_eq!(
            environment(&[])[OsStr::new(SSH_COMMAND)],
            "ssh -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3"
        );
        assert_eq!(
            environment(&[(SSH_COMMAND, "ssh -v")])[OsStr::new(SSH_COMMAND)],
            "ssh -v"
        );
        assert_eq!(
            environment(&[(SSH_COMMAND, "")])[OsStr::new(SSH_COMMAND)],
            ""
        );
    }

    #[test]
    fn the_message_locale_is_c_whatever_the_snapshot_holds() {
        let environment = environment(&[("LANGUAGE", "de"), ("LC_ALL", "de_DE.UTF-8")]);

        assert_eq!(environment[OsStr::new("LANGUAGE")], "C");
        assert_eq!(environment[OsStr::new("LC_ALL")], "C");
    }

    #[test]
    fn a_name_and_a_value_that_are_not_utf_8_reach_the_child_as_given() {
        let name = OsStr::from_bytes(b"NAME\xff");
        let value = OsStr::from_bytes(b"value\xfe");

        let environment = child_environment([(name, value)].into_iter());

        assert_eq!(environment.get(name).map(OsString::as_os_str), Some(value));
    }

    #[test]
    fn an_exit_is_its_code_or_minus_its_signal() {
        assert_eq!(Exit::Code(128).number(), 128);
        assert_eq!(Exit::Signal(9).number(), -9);
    }
}
