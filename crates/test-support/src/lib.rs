//! Test support for the `winter-workspace` packages: isolated trees, a process runner, fixtures, and a
//! loopback trace collector.

mod env;
mod git;
mod otlp;
mod process;
pub mod scenario;
mod tree;
mod workspace;

pub use env::PinnedEnv;
pub use git::{CommitSpec, Git, GitError};
pub use otlp::{Answer, Collector, ReceivedExport, ReceivedRequest, ReceivedSpan, decode_export};
pub use process::{
    Exit, FailureKind, Invocation, InvocationFailure, Observation, StdoutFault, invoke,
    invoke_with_stdout,
};
pub use tree::{Found, TempTree, snapshot};
pub use workspace::{Workspace, WorkspaceBuilder};
