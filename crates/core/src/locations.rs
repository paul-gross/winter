//! The checkout locations of a workspace: the worktrees of each environment present, the standalone
//! repositories, and the workspace itself.

use std::path::{Path, PathBuf};

use crate::config_load;
use crate::environments;
use crate::files::ReadFiles;
use crate::state_registry;
use crate::termination::{ConfigError, InternalError};
use crate::workspace_files::location;

/// What a location is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum LocationKind {
    Worktree,
    Standalone,
    Workspace,
}

/// One checkout location. Its path is absolute, and exists as a directory.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct Location {
    pub kind: LocationKind,
    /// The environment of a worktree.
    pub env: Option<String>,
    /// The resolved project name of a worktree.
    pub repo: Option<String>,
    /// The resolved name of a standalone, or the base name of the root for the workspace.
    pub name: Option<String>,
    pub label: String,
    pub path: PathBuf,
}

/// Why the locations were not listed.
#[derive(Debug, thiserror::Error)]
pub(crate) enum ListError {
    #[error(transparent)]
    Config(#[from] ConfigError),
    #[error(transparent)]
    Internal(#[from] InternalError),
}

/// The locations of the workspace of `start`: for each environment present, in order, a worktree for each
/// project entry in merged order (pinned entries and entries of one name included); then a location for each
/// standalone entry, at its `path` or else its name; then the workspace root, always.
///
/// A location other than the workspace is listed only when it is a directory, symbolic links followed. The
/// state file is read after the load. Paths are joined and never resolved. Nothing is written.
pub(crate) fn list(files: &dyn ReadFiles, start: &Path) -> Result<Vec<Location>, ListError> {
    let workspace = config_load::load(files, start)?;
    let root = workspace.root;
    let config = workspace.config;
    let registry = state_registry::read_in_order(files, &root)?;
    let environments = environments::present(files, &root, &config, &registry)?;

    let mut listed = Vec::new();
    for env in &environments {
        for project in &config.project_repositories {
            let name = &project.common.name;
            let path = location(&root, &Path::new(env).join(name));
            if files.is_dir(&path) {
                listed.push(Location {
                    kind: LocationKind::Worktree,
                    env: Some(env.clone()),
                    repo: Some(name.clone()),
                    name: None,
                    label: format!("{env}/{name}"),
                    path,
                });
            }
        }
    }
    for standalone in &config.standalone_repositories {
        let name = &standalone.common.name;
        let relative = if standalone.path.is_empty() {
            name
        } else {
            &standalone.path
        };
        let path = location(&root, Path::new(relative));
        if files.is_dir(&path) {
            listed.push(Location {
                kind: LocationKind::Standalone,
                env: None,
                repo: None,
                name: Some(name.clone()),
                label: name.clone(),
                path,
            });
        }
    }
    listed.push(Location {
        kind: LocationKind::Workspace,
        env: None,
        repo: None,
        name: root
            .file_name()
            .map(|base| base.to_string_lossy().into_owned()),
        label: "<workspace>".to_owned(),
        path: root,
    });
    Ok(listed)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::files::MemoryFiles;

    const CONFIG: &str = "/ws/.winter/config.toml";

    fn labels(files: &MemoryFiles) -> Vec<String> {
        list(files, Path::new("/ws"))
            .expect("locations")
            .into_iter()
            .map(|location| location.label)
            .collect()
    }

    #[test]
    fn a_workspace_with_nothing_lists_only_itself() {
        let files = MemoryFiles::default().with(CONFIG, b"");

        assert_eq!(labels(&files), ["<workspace>"]);
    }

    #[test]
    fn worktrees_come_first_then_standalones_then_the_workspace() {
        let files = MemoryFiles::default()
            .with(
                CONFIG,
                b"[[project_repository]]\nname = \"api\"\n\n[[project_repository]]\nname = \"web\"\n\n\
                  [[standalone_repository]]\nname = \"ctx\"\npath = \".winter/ext/context\"\n\n\
                  [[standalone_repository]]\nname = \"gone\"\n",
            )
            .dir("/ws/alpha/api")
            .dir("/ws/.winter/ext/context");

        assert_eq!(labels(&files), ["alpha/api", "ctx", "<workspace>"]);
    }

    #[test]
    fn an_alias_whose_project_entry_is_a_link_to_a_directory_is_listed() {
        let files = MemoryFiles::default()
            .with(CONFIG, b"[[project_repository]]\nname = \"api\"\n")
            .dir("/elsewhere")
            .link("/ws/alpha/api", "/elsewhere");

        assert_eq!(labels(&files), ["alpha/api", "<workspace>"]);
    }
}
