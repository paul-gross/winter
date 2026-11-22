//! Pinning standalone repositories to recorded inputs: the ports every pin entry takes and the module's error.

use crate::files::{ReadFiles, WriteFiles};
use crate::git::GitFailure;
use crate::programs::RunPrograms;
use crate::termination::{ConfigError, InternalError, RepoError};
use crate::trace::CommandTracer;

pub(crate) mod advance;
pub(crate) mod freeze;
pub(crate) mod materialize;
pub(crate) mod outcome;
pub(crate) mod repin;
pub(crate) mod resolve;
pub(crate) mod snapshot;

/// The ports every pin entry takes, as one value.
pub struct Ports<'p> {
    pub files: &'p dyn ReadFiles,
    pub writer: &'p dyn WriteFiles,
    pub programs: &'p dyn RunPrograms,
    pub tracer: &'p dyn CommandTracer,
}

/// What a pin flow answers on failure. The command maps `Repo` to a repo ending, `Config` to a configuration
/// ending, and `Internal` to an internal ending.
#[derive(Debug, thiserror::Error)]
pub enum PinError {
    #[error(transparent)]
    Repo(Box<RepoError>),
    /// The lock's error, or an error of the writer of a standalone's ref.
    #[error(transparent)]
    Config(Box<ConfigError>),
    /// A git that could not be started.
    #[error(transparent)]
    Internal(Box<InternalError>),
}

/// Whether `name` is selected by `patterns`: a glob pattern matches it by `fnmatchcase`, any other pattern is a
/// literal name. No pattern selects every name.
pub(crate) fn selected(name: &str, patterns: &[String]) -> bool {
    patterns.is_empty()
        || patterns.iter().any(|pattern| {
            if crate::pattern_match::is_glob(pattern) {
                crate::pattern_match::fnmatchcase(name, pattern)
            } else {
                name == pattern
            }
        })
}

impl From<GitFailure> for PinError {
    fn from(failure: GitFailure) -> Self {
        match failure {
            GitFailure::Repo(error) => Self::Repo(error),
            GitFailure::Internal(error) => Self::Internal(error),
        }
    }
}

impl From<ConfigError> for PinError {
    fn from(error: ConfigError) -> Self {
        Self::Config(Box::new(error))
    }
}

#[cfg(test)]
mod tests {
    use std::io;
    use std::path::Path;

    use super::*;
    use crate::files::OsFiles;
    use crate::programs::{Call, Finished};
    use crate::trace::NoTrace;

    struct CannotStart;

    impl RunPrograms for CannotStart {
        fn run(&self, _: &Call<'_>) -> io::Result<Finished> {
            Err(io::Error::other("no git"))
        }
    }

    #[test]
    fn a_git_that_cannot_start_is_an_internal_pin_error() {
        let ports = Ports {
            files: &OsFiles,
            writer: &OsFiles,
            programs: &CannotStart,
            tracer: &NoTrace,
        };

        let answer = resolve::resolve(&ports, "api", Path::new("/nowhere"), "main");

        assert!(matches!(
            answer.map_err(PinError::from),
            Err(PinError::Internal(_))
        ));
    }
}
