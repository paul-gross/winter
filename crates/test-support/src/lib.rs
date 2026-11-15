//! Test support for the `winter-workspace` packages: isolated trees, a process runner, and fixtures.

mod env;
mod process;
mod tree;

pub use env::PinnedEnv;
pub use process::{Exit, FailureKind, Invocation, InvocationFailure, Observation, invoke};
pub use tree::TempTree;
