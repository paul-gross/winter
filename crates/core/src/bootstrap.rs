//! The creation and the reconciliation of a workspace: the module's error. The flows are its submodules.

use std::path::PathBuf;

pub(crate) mod checkout;
pub(crate) mod create;

/// What reconciliation reports as it happens.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InitEvent {
    /// The target `projects/` or `standalone/` begins.
    TargetStarted {
        target: String,
    },
    TargetCompleted {
        target: String,
        success: bool,
    },
    /// `location` is the path the action concerns, and `detail` is as [`Action`] states.
    RepoAction {
        repo: String,
        location: String,
        action: Action,
        detail: String,
    },
    /// `error` is the rendered repo error, or the text of the I/O error.
    RepoError {
        repo: String,
        error: String,
    },
    CmdStarted {
        repo: String,
        command: String,
    },
    /// One line of a command's stdout or stderr, without its line end.
    CmdOutputLine {
        repo: String,
        line: String,
    },
    /// `returncode` is the exit code, or minus the number of the signal.
    CmdCompleted {
        repo: String,
        command: String,
        returncode: i32,
    },
}

/// What was done to a repository or to the workspace. The `detail` of the event is empty unless stated.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Cloned,
    Exists,
    /// `detail`: the appended entries joined by `, `.
    ExcludesUpdated,
    /// `detail`: the kind of the pin and the first 8 characters of its commit.
    Pinned,
    /// `detail`: what the workspace's exclude block now holds.
    WorkspaceExcludesUpdated,
    AgentsMdUpdated,
}

/// A rule of workspace creation or pruning that the invocation violates.
#[derive(Debug, thiserror::Error)]
pub enum BootstrapError {
    #[error("{} already exists; run `winter-workspace ws init` to reconcile it", .path.display())]
    AlreadyInitialized { path: PathBuf },
    #[error("`ws prune --json` needs `--force` or `--dry-run`")]
    JsonNeedsForce,
}
