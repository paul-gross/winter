//! The creation and the reconciliation of a workspace: the module's error. The flows are its submodules.

use std::path::PathBuf;

pub(crate) mod checkout;
pub(crate) mod create;

/// A rule of workspace creation or pruning that the invocation violates.
#[derive(Debug, thiserror::Error)]
pub enum BootstrapError {
    #[error("{} already exists; run `winter-workspace ws init` to reconcile it", .path.display())]
    AlreadyInitialized { path: PathBuf },
    #[error("`ws prune --json` needs `--force` or `--dry-run`")]
    JsonNeedsForce,
}
