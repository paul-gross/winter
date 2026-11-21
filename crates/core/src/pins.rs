//! Pinning standalone repositories to recorded inputs: the ports every pin entry takes and the module's error.

use crate::files::{ReadFiles, WriteFiles};
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
