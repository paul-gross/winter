//! What a git operation answers on failure, and the commit id a git call returns.

use crate::termination::{InternalError, RepoError};

/// A commit or object id as git prints it. A lock commit is kept as read, unvalidated.
pub type Sha = String;

/// What a git operation answers on failure: the call's repo error, or a git that could not be started.
#[derive(Debug)]
pub enum GitFailure {
    Repo(Box<RepoError>),
    Internal(Box<InternalError>),
}
