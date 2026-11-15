//! How an invocation ends, and the errors an ending carries.

use std::backtrace::Backtrace;
use std::error::Error;
use std::fmt;
use std::path::PathBuf;

use crate::config_edit::CatalogError;

/// One value per way an invocation ends.
///
/// Commands return these and write only their result. The process boundary alone writes the final diagnostic of
/// a termination and maps its exit code.
#[derive(Debug)]
pub enum Termination {
    Success,
    /// The command's own result already carries the failure; nothing more is written.
    Reported,
    Usage(Box<UsageError>),
    /// No command was given at the root or in a group; the diagnostic is that node's help.
    BareGroup {
        help: String,
    },
    Command(Box<CommandError>),
    Config(Box<ConfigError>),
    /// The reader closed stdout before the result was written; nothing more is written.
    BrokenPipe,
    Internal(Box<InternalError>),
}

impl Termination {
    /// The process exit code of this ending.
    pub fn exit_code(&self) -> u8 {
        match self {
            Self::Success => 0,
            Self::Reported
            | Self::Command(_)
            | Self::Config(_)
            | Self::BrokenPipe
            | Self::Internal(_) => 1,
            Self::Usage(_) | Self::BareGroup { .. } => 2,
        }
    }

    /// The text the process boundary writes to its error stream, or `None` when nothing more is written.
    ///
    /// A bare group's text is its help. Every other text opens with a line that starts with the error marker
    /// and carries the message; a usage error adds its usage line and the command that prints its help.
    pub fn diagnostic(&self) -> Option<String> {
        match self {
            Self::Success | Self::Reported | Self::BrokenPipe => None,
            Self::Usage(error) => Some(error.to_string()),
            Self::BareGroup { help } => Some(help.clone()),
            Self::Command(error) => Some(format!("{ERROR_MARKER}{error}")),
            Self::Config(error) => Some(format!("{ERROR_MARKER}{error}")),
            Self::Internal(error) => Some(format!("{ERROR_MARKER}{error}")),
        }
    }

    /// How an invocation ends on a write or a flush of stdout that failed with `error`.
    ///
    /// The one classifier of such a failure. A reader that closed the handle is no failure of the program: that
    /// ending has no diagnostic. Every other error is an internal failure that carries it.
    pub fn from_write_error(error: std::io::Error) -> Self {
        match error.kind() {
            std::io::ErrorKind::BrokenPipe => Self::BrokenPipe,
            _ => Self::Internal(Box::new(InternalError::unexpected(error))),
        }
    }
}

/// What opens the first line of every error diagnostic.
const ERROR_MARKER: &str = "error: ";

/// What a usage diagnostic shows of the command at fault.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Usage {
    /// The usage line of the node, as its `--help` prints it.
    pub line: String,
    /// The command line that prints the node's help: the program, the node's path, and `--help`.
    pub help_command: String,
}

/// A fault in how the command line was written.
///
/// Its text is the whole usage diagnostic, one part per line: the error line with the fault, the usage line of
/// the node at fault, and where to ask for that node's help.
#[derive(Debug, thiserror::Error)]
pub enum UsageError {
    /// Raised by the argument parser. `command_path` is the path parsed before the fault, and `usage` is that
    /// of the node at that path.
    #[error("{}{fault}\n{}\nFor help, run '{}'.", ERROR_MARKER, .usage.line, .usage.help_command)]
    Parse {
        command_path: Vec<String>,
        usage: Usage,
        fault: ParseFault,
    },
}

/// What the argument parser refused.
#[derive(Debug, thiserror::Error)]
pub enum ParseFault {
    #[error("unknown command '{name}'")]
    UnknownCommand { name: String },
    #[error("unknown option '{name}'")]
    UnknownOption { name: String },
    #[error("missing argument {metavar}")]
    MissingArgument { metavar: String },
    #[error("option '{option}' requires a value")]
    MissingOptionValue { option: String },
    #[error("invalid value '{value}' for {param}: expected {expected}")]
    InvalidValue {
        param: String,
        value: String,
        expected: String,
    },
    #[error("unexpected argument: {}", quoted(.values))]
    UnexpectedArgument { values: Vec<String> },
    /// A flag or a single-value option given more than once.
    #[error("option '{option}' is given more than once")]
    RepeatedOption { option: String },
    /// An argument of the command line that is not UTF-8. `argument` is its text with each invalid sequence
    /// replaced, and is rendered in quotes with its control characters escaped.
    #[error("argument is not valid UTF-8: {argument:?}")]
    NotUtf8 { argument: String },
}

/// Each value in quotes with its control characters escaped, so that an empty value shows and a value that
/// holds a line break stays on the error line.
fn quoted(values: &[String]) -> String {
    values
        .iter()
        .map(|value| format!("{value:?}"))
        .collect::<Vec<_>>()
        .join(" ")
}

/// A rule of a command that the invocation violates. Each variant wraps the error of the module that owns the
/// rule.
#[derive(Debug, thiserror::Error)]
pub enum CommandError {
    #[error(transparent)]
    Catalog(#[from] CatalogError),
}

/// A failure to locate, read, or accept the workspace configuration.
#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("no workspace found at or above {}", .start.display())]
    RootNotFound { start: PathBuf },
    #[error("cannot load {}: {cause}", .path.display())]
    FileRead { path: PathBuf, cause: FileCause },
    #[error(
        "envs_per_workspace is {envs_per_workspace}, too small for the {aliases} entries of env_aliases"
    )]
    EnvsPerWorkspaceTooSmall {
        envs_per_workspace: i64,
        aliases: usize,
    },
}

/// Why a TOML file could not be loaded.
#[derive(Debug, thiserror::Error)]
pub enum FileCause {
    /// The file exists but cannot be read.
    #[error(transparent)]
    Io(#[from] std::io::Error),
    /// The file was read but is not valid TOML.
    #[error(transparent)]
    Toml(#[from] TomlParseError),
}

/// The TOML parser's own error, with its position.
#[derive(Debug, thiserror::Error)]
#[error(transparent)]
pub struct TomlParseError(#[from] toml::de::Error);

/// A failure with no other class, and any panic.
///
/// The backtrace is a public field of each variant: the process boundary reads it from there.
#[derive(Debug)]
pub enum InternalError {
    Unexpected {
        source: Box<dyn Error + Send + Sync>,
        backtrace: Backtrace,
    },
    Panic {
        message: String,
        backtrace: Backtrace,
    },
}

impl InternalError {
    /// The one constructor of an unexpected failure: it captures the backtrace.
    pub(crate) fn unexpected(source: impl Into<Box<dyn Error + Send + Sync>>) -> Self {
        Self::Unexpected {
            source: source.into(),
            backtrace: Backtrace::capture(),
        }
    }
}

impl fmt::Display for InternalError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unexpected { source, .. } => source.fmt(f),
            Self::Panic { message, .. } => f.write_str(message),
        }
    }
}

impl Error for InternalError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Unexpected { source, .. } => Some(source.as_ref()),
            Self::Panic { .. } => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn usage_of_index() -> Usage {
        Usage {
            line: "Usage: winter-workspace ws index [OPTIONS] <NAME>".to_owned(),
            help_command: "winter-workspace ws index --help".to_owned(),
        }
    }

    fn usage_with(fault: ParseFault) -> UsageError {
        UsageError::Parse {
            command_path: vec!["ws".to_owned(), "index".to_owned()],
            usage: usage_of_index(),
            fault,
        }
    }

    fn usage() -> UsageError {
        usage_with(ParseFault::UnknownOption {
            name: "--nope".to_owned(),
        })
    }

    /// One fault of each variant, with a text its error line has to carry.
    fn faults() -> Vec<(ParseFault, &'static str)> {
        vec![
            (
                ParseFault::UnknownCommand {
                    name: "nope".to_owned(),
                },
                "nope",
            ),
            (
                ParseFault::UnknownOption {
                    name: "--nope".to_owned(),
                },
                "--nope",
            ),
            (
                ParseFault::MissingArgument {
                    metavar: "<NAME>".to_owned(),
                },
                "<NAME>",
            ),
            (
                ParseFault::MissingOptionValue {
                    option: "--tail".to_owned(),
                },
                "--tail",
            ),
            (
                ParseFault::InvalidValue {
                    param: "--tail".to_owned(),
                    value: "many".to_owned(),
                    expected: "a number".to_owned(),
                },
                "many",
            ),
            (
                ParseFault::UnexpectedArgument {
                    values: vec!["extra".to_owned()],
                },
                "\"extra\"",
            ),
            (
                ParseFault::RepeatedOption {
                    option: "--json".to_owned(),
                },
                "--json",
            ),
            (
                ParseFault::NotUtf8 {
                    argument: "na\u{fffd}me".to_owned(),
                },
                "\"na\u{fffd}me\"",
            ),
        ]
    }

    fn root_not_found() -> ConfigError {
        ConfigError::RootNotFound {
            start: PathBuf::from("/somewhere/below"),
        }
    }

    fn panic() -> InternalError {
        InternalError::Panic {
            message: "index out of range".to_owned(),
            backtrace: Backtrace::disabled(),
        }
    }

    #[test]
    fn a_termination_is_at_most_four_words_wide() {
        assert!(
            std::mem::size_of::<Termination>() <= 4 * std::mem::size_of::<usize>(),
            "{} bytes",
            std::mem::size_of::<Termination>()
        );
    }

    #[test]
    fn exit_codes_follow_the_class_of_the_ending() {
        assert_eq!(Termination::Success.exit_code(), 0);
        assert_eq!(Termination::Reported.exit_code(), 1);
        assert_eq!(Termination::Usage(Box::new(usage())).exit_code(), 2);
        assert_eq!(
            Termination::BareGroup {
                help: "help".to_owned()
            }
            .exit_code(),
            2
        );
        assert_eq!(
            Termination::Config(Box::new(root_not_found())).exit_code(),
            1
        );
        assert_eq!(Termination::Internal(Box::new(panic())).exit_code(), 1);
    }

    #[test]
    fn success_and_reported_have_no_diagnostic() {
        assert_eq!(Termination::Success.diagnostic(), None);
        assert_eq!(Termination::Reported.diagnostic(), None);
    }

    #[test]
    fn a_broken_pipe_exits_1_and_has_no_diagnostic() {
        assert_eq!(Termination::BrokenPipe.exit_code(), 1);
        assert_eq!(Termination::BrokenPipe.diagnostic(), None);
    }

    #[test]
    fn a_failed_write_to_a_handle_the_reader_closed_is_a_broken_pipe() {
        let termination =
            Termination::from_write_error(std::io::Error::from(std::io::ErrorKind::BrokenPipe));

        assert!(
            matches!(termination, Termination::BrokenPipe),
            "{termination:?}"
        );
    }

    #[test]
    fn a_failed_write_of_any_other_kind_is_an_internal_failure_that_carries_the_error() {
        for kind in [
            std::io::ErrorKind::StorageFull,
            std::io::ErrorKind::PermissionDenied,
            std::io::ErrorKind::Other,
        ] {
            let termination =
                Termination::from_write_error(std::io::Error::new(kind, "the device is full"));

            let Termination::Internal(error) = &termination else {
                panic!("{kind:?}: expected an internal failure, got {termination:?}");
            };
            assert!(
                matches!(**error, InternalError::Unexpected { .. }),
                "{kind:?}: {error:?}"
            );
            let source = error.source().expect("a source");
            let source = source
                .downcast_ref::<std::io::Error>()
                .expect("the write error");
            assert_eq!(source.kind(), kind);
            assert_eq!(termination.exit_code(), 1, "{kind:?}");
            assert_eq!(
                termination.diagnostic().as_deref(),
                Some("error: the device is full"),
                "{kind:?}"
            );
        }
    }

    #[test]
    fn bare_group_diagnostic_is_its_help() {
        let help = "Usage: winter-workspace <COMMAND>\n".to_owned();

        assert_eq!(
            Termination::BareGroup { help: help.clone() }.diagnostic(),
            Some(help)
        );
    }

    #[test]
    fn root_not_found_diagnostic_names_the_start_path() {
        let diagnostic = Termination::Config(Box::new(root_not_found()))
            .diagnostic()
            .expect("a diagnostic");

        assert!(diagnostic.contains("/somewhere/below"), "{diagnostic}");
    }

    #[test]
    fn a_usage_diagnostic_is_the_error_line_the_usage_line_and_the_help_command_in_that_order() {
        for (fault, carried) in faults() {
            let name = format!("{fault:?}");
            let usage = usage_of_index();

            let diagnostic = Termination::Usage(Box::new(usage_with(fault)))
                .diagnostic()
                .expect("a diagnostic");

            let lines: Vec<&str> = diagnostic.lines().collect();
            let [error_line, usage_line, help_line] = lines.as_slice() else {
                panic!("{name}: expected three lines, got {diagnostic:?}");
            };
            assert!(error_line.starts_with(ERROR_MARKER), "{name}: {diagnostic}");
            assert!(error_line.contains(carried), "{name}: {diagnostic}");
            assert_eq!(*usage_line, usage.line, "{name}");
            assert!(
                help_line.contains("winter-workspace ws index --help"),
                "{name}: {diagnostic}"
            );
        }
    }

    #[test]
    fn unexpected_arguments_are_each_quoted_and_stay_on_the_error_line() {
        let diagnostic = Termination::Usage(Box::new(usage_with(ParseFault::UnexpectedArgument {
            values: vec!["two".to_owned(), String::new(), "a\nb".to_owned()],
        })))
        .diagnostic()
        .expect("a diagnostic");

        let lines: Vec<&str> = diagnostic.lines().collect();
        assert_eq!(lines.len(), 3, "{diagnostic:?}");
        assert!(lines[0].ends_with(r#""two" "" "a\nb""#), "{diagnostic}");
    }

    #[test]
    fn an_argument_that_is_not_utf_8_is_quoted_and_stays_on_the_error_line() {
        let diagnostic = Termination::Usage(Box::new(usage_with(ParseFault::NotUtf8 {
            argument: "a\nb\u{fffd}".to_owned(),
        })))
        .diagnostic()
        .expect("a diagnostic");

        let lines: Vec<&str> = diagnostic.lines().collect();
        assert_eq!(lines.len(), 3, "{diagnostic:?}");
        assert!(lines[0].ends_with("\"a\\nb\u{fffd}\""), "{diagnostic}");
    }

    #[test]
    fn every_other_error_ending_starts_with_the_error_marker() {
        let command =
            Termination::Command(Box::new(CommandError::Catalog(CatalogError::NotFound {
                kind: crate::RepositoryKind::Project,
                name: "docs".to_owned(),
            })));
        let config = Termination::Config(Box::new(root_not_found()));
        let unexpected = Termination::Internal(Box::new(InternalError::unexpected(
            std::io::Error::other("disk on fire"),
        )));
        let panicked = Termination::Internal(Box::new(panic()));

        for (termination, carried) in [
            (command, "docs"),
            (config, "/somewhere/below"),
            (unexpected, "disk on fire"),
            (panicked, "index out of range"),
        ] {
            let diagnostic = termination.diagnostic().expect("a diagnostic");

            assert!(diagnostic.starts_with(ERROR_MARKER), "{diagnostic}");
            assert!(diagnostic.contains(carried), "{diagnostic}");
            assert_eq!(diagnostic.lines().count(), 1, "{diagnostic}");
        }
    }

    #[test]
    fn the_constructor_of_an_unexpected_failure_keeps_its_source() {
        let error = InternalError::unexpected(std::io::Error::other("disk on fire"));

        assert!(matches!(error, InternalError::Unexpected { .. }));
        assert_eq!(error.to_string(), "disk on fire");
        assert!(error.source().is_some());
    }

    #[test]
    fn file_read_names_the_path_and_the_cause() {
        let cause = FileCause::from(std::io::Error::other("permission denied"));
        let error = ConfigError::FileRead {
            path: PathBuf::from("/ws/.winter/config.toml"),
            cause,
        };

        let text = error.to_string();
        assert!(text.contains("/ws/.winter/config.toml"), "{text}");
        assert!(text.contains("permission denied"), "{text}");
    }

    #[test]
    fn unexpected_displays_its_source_and_exposes_it() {
        let error = InternalError::Unexpected {
            source: Box::new(std::io::Error::other("disk on fire")),
            backtrace: Backtrace::disabled(),
        };

        assert_eq!(error.to_string(), "disk on fire");
        assert!(error.source().is_some());
    }

    #[test]
    fn panic_displays_its_message_and_has_no_source() {
        let error = panic();

        assert_eq!(error.to_string(), "index out of range");
        assert!(error.source().is_none());
    }
}
