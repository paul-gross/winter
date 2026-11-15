//! Test support for the `winter-workspace` packages: isolated trees, a process runner, and fixtures.

mod env;
mod git;
mod process;
mod tree;
mod workspace;

pub use env::PinnedEnv;
pub use git::{CommitSpec, Git, GitError};
pub use process::{Exit, FailureKind, Invocation, InvocationFailure, Observation, invoke};
pub use tree::TempTree;
pub use workspace::{Workspace, WorkspaceBuilder};
