//! How an invocation ends, and the errors an ending carries.

use std::backtrace::Backtrace;
use std::error::Error;
use std::fmt;
use std::path::PathBuf;

/// One value per way an invocation ends.
///
/// Commands return these and write only their result. The process boundary alone writes the final diagnostic of
/// a termination and maps its exit code.
#[derive(Debug)]
pub enum Termination {
    Success,
    /// The command's own result already carries the failure; nothing more is written.
    Reported,
    Usage(UsageError),
    /// No command was given at the root or in a group; the diagnostic is that node's help.
    BareGroup {
        help: String,
    },
    Command(CommandError),
    Config(ConfigError),
    Internal(InternalError),
}

impl Termination {
    /// The process exit code of this ending.
    pub fn exit_code(&self) -> u8 {
        match self {
            Self::Success => 0,
            Self::Reported | Self::Command(_) | Self::Config(_) | Self::Internal(_) => 1,
            Self::Usage(_) | Self::BareGroup { .. } => 2,
        }
    }

    /// The text the process boundary writes to its error stream, or `None` when nothing more is written.
    pub fn diagnostic(&self) -> Option<String> {
        match self {
            Self::Success | Self::Reported => None,
            Self::Usage(error) => Some(error.to_string()),
            Self::BareGroup { help } => Some(help.clone()),
            Self::Command(error) => Some(error.to_string()),
            Self::Config(error) => Some(error.to_string()),
            Self::Internal(error) => Some(error.to_string()),
        }
    }
}

/// A fault in how the command line was written.
#[derive(Debug, thiserror::Error)]
pub enum UsageError {
    /// Raised by the argument parser. `command_path` is the path parsed before the fault.
    #[error("{fault}")]
    Parse {
        command_path: Vec<String>,
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
    #[error("unexpected argument: {}", .values.join(" "))]
    UnexpectedArgument { values: Vec<String> },
}

/// A rule of a command that the invocation violates. Each variant wraps the error of the module that owns the
/// rule.
#[derive(Debug, thiserror::Error)]
pub enum CommandError {}

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

    fn usage() -> UsageError {
        UsageError::Parse {
            command_path: Vec::new(),
            fault: ParseFault::UnknownOption {
                name: "--nope".to_owned(),
            },
        }
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
    fn exit_codes_follow_the_class_of_the_ending() {
        assert_eq!(Termination::Success.exit_code(), 0);
        assert_eq!(Termination::Reported.exit_code(), 1);
        assert_eq!(Termination::Usage(usage()).exit_code(), 2);
        assert_eq!(
            Termination::BareGroup {
                help: "help".to_owned()
            }
            .exit_code(),
            2
        );
        assert_eq!(Termination::Config(root_not_found()).exit_code(), 1);
        assert_eq!(Termination::Internal(panic()).exit_code(), 1);
    }

    #[test]
    fn success_and_reported_have_no_diagnostic() {
        assert_eq!(Termination::Success.diagnostic(), None);
        assert_eq!(Termination::Reported.diagnostic(), None);
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
        let diagnostic = Termination::Config(root_not_found())
            .diagnostic()
            .expect("a diagnostic");

        assert!(diagnostic.contains("/somewhere/below"), "{diagnostic}");
    }

    #[test]
    fn usage_diagnostic_names_the_fault() {
        let diagnostic = Termination::Usage(usage())
            .diagnostic()
            .expect("a diagnostic");

        assert!(diagnostic.contains("--nope"), "{diagnostic}");
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
