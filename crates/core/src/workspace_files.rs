//! The names of the files and the directories of a workspace, each relative to a workspace root.

/// The shared configuration file. A directory is a workspace root when it holds this file.
pub(crate) const CONFIG_FILE: &str = ".winter/config.toml";

/// The machine-local configuration file, layered over the shared one.
pub(crate) const LOCAL_CONFIG_FILE: &str = ".winter/config.local.toml";

/// The machine-local state file.
pub(crate) const STATE_FILE: &str = ".winter/state.toml";

/// The directory that holds the main checkout of each project repository, one directory per project name.
pub(crate) const PROJECTS_DIR: &str = "projects";
