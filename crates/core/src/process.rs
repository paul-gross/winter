//! The entry of the library: what the binary hands over, and `run`, which acts on what the parser answers.

use std::collections::HashMap;
use std::ffi::{OsStr, OsString};
use std::io::{BufRead, Write};
use std::path::PathBuf;

use crate::context::Context;
use crate::diagnostics::{self, Record};
use crate::files::OsFiles;
use crate::logging::Level;
use crate::output;
use crate::parse::{self, Outcome, Parsed, Stage};
use crate::registry::{Entry, REGISTRY};
use crate::termination::{InternalError, Termination};

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
/// Writes results, and the help or version line a flag asks for, to the handles of `invocation`. Never writes
/// the final diagnostic and never exits: the caller renders the returned [`Termination`] and maps its exit
/// code.
pub fn run(invocation: Invocation<'_>) -> Termination {
    run_with(REGISTRY, invocation)
}

/// A command was selected though no command name was resolved.
#[derive(Debug, thiserror::Error)]
#[error("the parser selected a command under an empty command path")]
struct DispatchAtRoot;

/// [`run`] over the commands of `entries`: asks the parser, then acts on its answer.
///
/// The one place that writes the help and the version line, builds the `Context`, and calls a command. Once a
/// command name was resolved it writes one debug record that names the command path to the stderr handle,
/// whatever the outcome.
pub(crate) fn run_with(entries: &[Entry], invocation: Invocation<'_>) -> Termination {
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

    let Parsed { stage, outcome } = parse::parse(entries, &identity, &args);
    // Logging and the root option values are resolved only once a command name was: an invocation that ends
    // at the root resolves neither and writes no record.
    let command = match stage {
        Stage::Root => None,
        Stage::Command { invoked, root } => {
            let (options, log) = parse::root_values(&root, &env);
            diagnostics::record(
                stderr,
                log,
                &Record {
                    level: Level::Debug,
                    origin: "parse",
                    message: &format!("command path: {}", invoked.path.join(" ")),
                    only_when_switched: false,
                },
            );
            Some((invoked, options, log))
        }
    };
    match outcome {
        Outcome::Help(text) => output::text(stdout, &text),
        Outcome::Version(line) => output::line(stdout, &line),
        Outcome::Ended(ending) => ending,
        Outcome::Dispatch { run, matches } => {
            let Some((invoked, options, log)) = command else {
                return Termination::Internal(Box::new(InternalError::unexpected(DispatchAtRoot)));
            };
            let mut cx = Context {
                cwd: &cwd,
                env: &env,
                identity: &identity,
                stdin,
                out: stdout,
                err: stderr,
                out_is_terminal: stdout_is_terminal,
                err_is_terminal: stderr_is_terminal,
                files: &OsFiles,
                file_writer: &OsFiles,
                invoked: &invoked,
                root: &options,
                log,
            };
            run(&matches, &mut cx)
        }
    }
}

/// Runs `args` over the commands of `entries` with in-memory handles, and returns the ending with what was
/// written to stdout and to stderr.
///
/// The working directory is `/`, the environment and stdin are empty, and neither stream is a terminal.
#[cfg(test)]
pub(crate) fn run_in_memory(entries: &[Entry], args: &[&str]) -> (Termination, String, String) {
    let mut stdin: &[u8] = &[];
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    let termination = run_with(
        entries,
        Invocation {
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
        },
    );
    (
        termination,
        String::from_utf8(stdout).expect("UTF-8 on stdout"),
        String::from_utf8(stderr).expect("UTF-8 on stderr"),
    )
}

#[cfg(test)]
mod tests {
    use clap::ArgMatches;

    use super::*;
    use crate::registry::Registration;

    fn show_args(command: clap::Command) -> clap::Command {
        command.about("Show the node being run")
    }

    fn show_run(_: &ArgMatches, cx: &mut Context<'_>) -> Termination {
        output::line(
            cx.out,
            &format!(
                "{} | {} | {}",
                cx.invoked.path.join(" "),
                cx.invoked.usage.line,
                cx.invoked.usage.help_command
            ),
        )
    }

    static SHOW: Registration = Registration::Command {
        args: show_args,
        run: show_run,
        hidden: false,
    };
    static DISPLAY: Registration = Registration::Alias { of: "show" };

    fn given_args(command: clap::Command) -> clap::Command {
        command.about("Show the root option values and the logging")
    }

    fn given_run(_: &ArgMatches, cx: &mut Context<'_>) -> Termination {
        output::line(cx.out, &format!("{:?} | {:?}", cx.root, cx.log))
    }

    static GIVEN: Registration = Registration::Command {
        args: given_args,
        run: given_run,
        hidden: false,
    };

    static ENTRIES: &[Entry] = &[
        Entry {
            path: &["ws", "display"],
            file: "crates/core/src/commands/ws/display.rs",
            registration: &DISPLAY,
        },
        Entry {
            path: &["ws", "given"],
            file: "crates/core/src/commands/ws/given.rs",
            registration: &GIVEN,
        },
        Entry {
            path: &["ws", "show"],
            file: "crates/core/src/commands/ws/show.rs",
            registration: &SHOW,
        },
    ];

    #[test]
    fn process_env_returns_the_value_of_a_present_key_only() {
        let env = ProcessEnv::from_vars([
            (OsString::from("HOME"), OsString::from("/home/someone")),
            (OsString::from("EDITOR"), OsString::from("vi")),
        ]);

        assert_eq!(env.get("EDITOR"), Some(OsStr::new("vi")));
        assert_eq!(env.get("SHELL"), None);
    }

    #[test]
    fn a_command_is_called_with_the_node_it_was_typed_as() {
        let (termination, stdout, stderr) = run_in_memory(ENTRIES, &["ws", "show"]);

        assert!(matches!(termination, Termination::Success));
        assert_eq!(
            stdout,
            "ws show | Usage: winter-workspace ws show | winter-workspace ws show --help\n"
        );
        assert_eq!(stderr, "");
    }

    #[test]
    fn an_alias_is_called_with_the_alias_name_in_its_path() {
        let (termination, stdout, _) = run_in_memory(ENTRIES, &["ws", "display"]);

        assert!(matches!(termination, Termination::Success));
        assert!(stdout.starts_with("ws display | "), "{stdout}");
        assert!(
            stdout.ends_with("| winter-workspace ws display --help\n"),
            "{stdout}"
        );
    }

    #[test]
    fn a_command_is_called_with_the_root_option_values_and_the_logging() {
        let (termination, stdout, stderr) = run_in_memory(
            ENTRIES,
            &[
                "-v",
                "--service-orchestrator",
                "X",
                "--source-override",
                "T",
                "ws",
                "given",
            ],
        );

        assert!(matches!(termination, Termination::Success));
        assert_eq!(
            stdout,
            "RootOptions { service_orchestrator: Some(\"X\"), source_override: Some(\"T\") } \
             | Log { threshold: Debug, switched: true }\n"
        );
        assert_eq!(stderr, "DEBUG parse: command path: ws given\n");
    }

    #[test]
    fn a_command_typed_without_root_options_is_called_with_none_and_the_default_logging() {
        let (termination, stdout, stderr) = run_in_memory(ENTRIES, &["ws", "given"]);

        assert!(matches!(termination, Termination::Success));
        assert_eq!(
            stdout,
            "RootOptions { service_orchestrator: None, source_override: None } \
             | Log { threshold: Warning, switched: false }\n"
        );
        assert_eq!(stderr, "");
    }

    #[test]
    fn help_and_the_version_line_are_written_to_stdout_and_succeed() {
        let (termination, stdout, stderr) = run_in_memory(ENTRIES, &["ws", "show", "--help"]);
        assert!(matches!(termination, Termination::Success));
        assert!(stdout.contains("Show the node being run"), "{stdout}");
        assert_eq!(stderr, "");

        let (termination, stdout, stderr) = run_in_memory(ENTRIES, &["--version"]);
        assert!(matches!(termination, Termination::Success));
        assert_eq!(stdout, "winter-workspace 0.1.0\n");
        assert_eq!(stderr, "");
    }

    #[test]
    fn an_ending_of_the_parser_is_returned_and_nothing_is_written() {
        for args in [&["ws", "nope"][..], &["ws"], &[]] {
            let (termination, stdout, stderr) = run_in_memory(ENTRIES, args);

            assert_eq!(termination.exit_code(), 2, "{args:?}");
            assert_eq!(stdout, "", "{args:?}");
            assert_eq!(stderr, "", "{args:?}");
        }
    }
}
