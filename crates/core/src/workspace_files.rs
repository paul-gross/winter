//! The names of the files and the directories of a workspace, each relative to a workspace root.

use std::path::{Path, PathBuf};

/// The shared configuration file. A directory is a workspace root when it holds this file.
pub(crate) const CONFIG_FILE: &str = ".winter/config.toml";

/// The machine-local configuration file, layered over the shared one.
pub(crate) const LOCAL_CONFIG_FILE: &str = ".winter/config.local.toml";

/// The machine-local state file.
pub(crate) const STATE_FILE: &str = ".winter/state.toml";

/// The directory that holds the main checkout of each project repository, one directory per project name.
pub(crate) const PROJECTS_DIR: &str = "projects";

/// `relative` under `root`, as a join of the two and nothing more: a repeated `/`, a `.` part, and a trailing
/// `/` are dropped, a `..` part is kept, and an absolute `relative` replaces the root.
pub(crate) fn location(root: &Path, relative: &Path) -> PathBuf {
    root.join(relative).components().collect()
}
