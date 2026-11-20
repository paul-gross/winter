//! The load of a workspace: its root found, its two configuration files read, checked, and merged, and the
//! typed configuration built. Nothing is written.

use std::path::{Path, PathBuf};

use toml::Table;

use crate::config_file;
use crate::config_merge;
use crate::config_model::{self, REPOSITORY_ARRAYS, Workspace, value_type};
use crate::files::ReadFiles;
use crate::termination::{ConfigError, ConfigFileName};
use crate::workspace_files::{CONFIG_FILE, LOCAL_CONFIG_FILE};
use crate::workspace_root;

const AGENT_MODEL_OVERRIDES: &str = "agent_model_overrides";

/// The workspace of `start`: the root at or above it, and the configuration of that root.
///
/// Each configuration file is read once, the shared one first. Before the two are merged, each is checked on
/// its own, and a refusal names the file: an `agent_model_overrides` that is not a table, in the shared file
/// and then in the local one, and then a `project_repository` or a `standalone_repository` that is not an
/// array, in the same order of files and the project key first in each.
pub(crate) fn load(files: &dyn ReadFiles, start: &Path) -> Result<Workspace, ConfigError> {
    let root = workspace_root::find(files, start)?;
    let shared = config_file::read(files, &root.join(CONFIG_FILE))?;
    let local = config_file::read(files, &root.join(LOCAL_CONFIG_FILE))?;
    build_workspace(root, shared, local)
}

/// The workspace of the root `root` as `load` builds it, with `bytes` as the content of the file `edited` in
/// place of its read; the other file is read through `files`.
///
/// Every check of `load` that follows the two reads runs in the same order, through the same code, so a
/// configuration is accepted here exactly when `load` accepts it with those bytes in the file. Bytes that are
/// not UTF-8 or not valid TOML are [`ConfigError::FileRead`] naming `edited` with the `Toml` cause.
pub(crate) fn reload(
    files: &dyn ReadFiles,
    root: &Path,
    edited: ConfigFileName,
    bytes: &[u8],
) -> Result<Workspace, ConfigError> {
    let path_of = |file| match file {
        ConfigFileName::Shared => root.join(CONFIG_FILE),
        ConfigFileName::Local => root.join(LOCAL_CONFIG_FILE),
    };
    let table_of = |file| {
        if file == edited {
            config_file::parse(&path_of(file), bytes)
        } else {
            config_file::read(files, &path_of(file))
        }
    };
    let shared = table_of(ConfigFileName::Shared)?;
    let local = table_of(ConfigFileName::Local)?;
    build_workspace(root.to_path_buf(), shared, local)
}

fn build_workspace(root: PathBuf, shared: Table, local: Table) -> Result<Workspace, ConfigError> {
    let layers = [
        (ConfigFileName::Shared, &shared),
        (ConfigFileName::Local, &local),
    ];
    for (file, table) in layers {
        agent_overrides_are_a_table(file, table)?;
    }
    for (file, table) in layers {
        repositories_are_arrays(file, table)?;
    }

    let merged = config_merge::merge(shared, local)?;
    let config = config_model::build(&merged)?;
    Ok(Workspace { root, config })
}

fn agent_overrides_are_a_table(file: ConfigFileName, table: &Table) -> Result<(), ConfigError> {
    match table.get(AGENT_MODEL_OVERRIDES) {
        Some(value) if !value.is_table() => Err(ConfigError::AgentOverridesNotTable {
            file,
            found: value_type(value),
        }),
        _ => Ok(()),
    }
}

fn repositories_are_arrays(file: ConfigFileName, table: &Table) -> Result<(), ConfigError> {
    for (kind, key) in REPOSITORY_ARRAYS {
        if let Some(value) = table.get(key)
            && !value.is_array()
        {
            return Err(ConfigError::RepositoriesNotArray {
                kind,
                file,
                found: value_type(value),
            });
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::io::ErrorKind;

    use super::*;
    use crate::files::MemoryFiles;
    use crate::termination::{FileCause, RepoKind, ValueType};

    const SHARED: &str = "/ws/.winter/config.toml";
    const LOCAL: &str = "/ws/.winter/config.local.toml";

    fn files(shared: &str, local: &str) -> MemoryFiles {
        MemoryFiles::default()
            .with(SHARED, shared.as_bytes())
            .with(LOCAL, local.as_bytes())
    }

    fn loaded(shared: &str, local: &str) -> Workspace {
        load(&files(shared, local), Path::new("/ws/alpha/project")).expect("a workspace")
    }

    fn refused(shared: &str, local: &str) -> ConfigError {
        load(&files(shared, local), Path::new("/ws")).expect_err("a refusal")
    }

    #[test]
    fn the_workspace_holds_the_root_found_above_the_start() {
        assert_eq!(loaded("", "").root, Path::new("/ws"));
    }

    #[test]
    fn no_workspace_above_the_start_is_the_missing_root() {
        let error = load(&files("", ""), Path::new("/elsewhere")).expect_err("no root");

        assert!(
            matches!(&error, ConfigError::RootNotFound { start } if start == Path::new("/elsewhere")),
            "{error:?}"
        );
    }

    #[test]
    fn a_missing_local_file_holds_no_key() {
        let files = MemoryFiles::default().with(SHARED, b"envs_per_workspace = 12\n");

        let workspace = load(&files, Path::new("/ws")).expect("a workspace");

        assert_eq!(workspace.config.ports.envs_per_workspace, 12);
        assert_eq!(workspace.config.ports.env_aliases.len(), 10);
    }

    #[test]
    fn a_directory_at_the_local_path_gives_the_configuration_of_the_shared_file_alone() {
        let files = MemoryFiles::default()
            .with(SHARED, b"envs_per_workspace = 12\n")
            .unreadable(LOCAL, ErrorKind::IsADirectory);

        let workspace = load(&files, Path::new("/ws")).expect("a workspace");

        assert_eq!(workspace.config.ports.envs_per_workspace, 12);
    }

    #[test]
    fn a_local_key_replaces_the_shared_one_whole() {
        let ports = loaded(
            "env_aliases = [\"one\", \"two\"]\nenvs_per_workspace = 12\n",
            "env_aliases = [\"three\"]\n",
        )
        .config
        .ports;

        assert_eq!(ports.env_aliases, ["three"]);
        assert_eq!(ports.envs_per_workspace, 12);
    }

    #[test]
    fn a_local_value_replaces_the_shared_one_whatever_its_type() {
        let ports = loaded(
            "env_aliases = [\"one\", \"two\"]\nenvs_per_workspace = 12\n",
            "env_aliases = 5\nenvs_per_workspace = \"many\"\n",
        )
        .config
        .ports;

        assert_eq!(ports.env_aliases, [] as [&str; 0]);
        assert_eq!(ports.envs_per_workspace, 48);
    }

    #[test]
    fn an_empty_local_file_leaves_the_shared_keys() {
        let ports = loaded("env_aliases = [\"one\"]\nenvs_per_workspace = 9\n", "")
            .config
            .ports;

        assert_eq!(ports.env_aliases, ["one"]);
        assert_eq!(ports.envs_per_workspace, 9);
    }

    #[test]
    fn the_cross_key_rule_is_checked_on_the_layered_values() {
        let error = refused(
            "env_aliases = [\"one\"]\nenvs_per_workspace = 3\n",
            "env_aliases = [\"one\", \"two\", \"three\"]\n",
        );

        assert!(
            matches!(
                error,
                ConfigError::EnvsPerWorkspaceTooSmall {
                    envs_per_workspace: 3,
                    aliases: 3
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_file_that_is_not_valid_toml_is_a_file_read_error_naming_it() {
        for (file, other) in [(SHARED, LOCAL), (LOCAL, SHARED)] {
            let files = MemoryFiles::default()
                .with(file, b"env_aliases = [\n")
                .with(other, b"");

            let error = load(&files, Path::new("/ws")).expect_err("a refusal");

            assert!(
                matches!(
                    &error,
                    ConfigError::FileRead { path, cause: FileCause::Toml(_) } if path == Path::new(file)
                ),
                "{error:?}"
            );
        }
    }

    #[test]
    fn a_regular_local_file_whose_read_fails_is_a_file_read_error_with_the_io_cause() {
        let files = MemoryFiles::default()
            .with(SHARED, b"")
            .failing(LOCAL, ErrorKind::PermissionDenied);

        let error = load(&files, Path::new("/ws")).expect_err("a refusal");

        assert!(
            matches!(
                &error,
                ConfigError::FileRead { path, cause: FileCause::Io(cause) }
                    if path == Path::new(LOCAL) && cause.kind() == ErrorKind::PermissionDenied
            ),
            "{error:?}"
        );
    }

    #[test]
    fn the_shared_file_is_loaded_before_the_local_one() {
        let error = refused("= broken\n", "= broken\n");

        assert!(
            matches!(&error, ConfigError::FileRead { path, .. } if path == Path::new(SHARED)),
            "{error:?}"
        );
    }

    #[test]
    fn agent_overrides_that_are_no_table_in_the_local_file_alone_name_the_local_file() {
        let error = refused(
            "[agent_model_overrides]\n",
            "agent_model_overrides = \"opus\"\n",
        );

        assert!(
            matches!(
                error,
                ConfigError::AgentOverridesNotTable {
                    file: ConfigFileName::Local,
                    found: ValueType::String,
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn agent_overrides_that_are_no_table_in_both_files_name_the_shared_file() {
        let error = refused(
            "agent_model_overrides = 7\n",
            "agent_model_overrides = \"opus\"\n",
        );

        assert!(
            matches!(
                error,
                ConfigError::AgentOverridesNotTable {
                    file: ConfigFileName::Shared,
                    found: ValueType::Integer,
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn empty_agent_overrides_that_are_no_table_are_refused_too() {
        let error = refused("agent_model_overrides = []\n", "");

        assert!(
            matches!(
                error,
                ConfigError::AgentOverridesNotTable {
                    file: ConfigFileName::Shared,
                    found: ValueType::Array,
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_repository_key_that_is_a_string_in_the_shared_file_carries_key_file_and_type() {
        let error = refused("project_repository = \"abc\"\n", "");

        assert!(
            matches!(
                error,
                ConfigError::RepositoriesNotArray {
                    kind: RepoKind::Project,
                    file: ConfigFileName::Shared,
                    found: ValueType::String,
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_repository_key_that_is_false_in_the_local_file_carries_key_file_and_type() {
        let error = refused(
            "[[standalone_repository]]\nname = \"tools\"\n",
            "standalone_repository = false\n",
        );

        assert!(
            matches!(
                error,
                ConfigError::RepositoriesNotArray {
                    kind: RepoKind::Standalone,
                    file: ConfigFileName::Local,
                    found: ValueType::Boolean,
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn repository_keys_that_are_no_array_in_both_files_name_the_shared_file() {
        let error = refused(
            "standalone_repository = {}\n",
            "project_repository = 0\nstandalone_repository = 0\n",
        );

        assert!(
            matches!(
                error,
                ConfigError::RepositoriesNotArray {
                    kind: RepoKind::Standalone,
                    file: ConfigFileName::Shared,
                    found: ValueType::Table,
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn in_one_file_the_project_key_is_checked_before_the_standalone_key() {
        let error = refused("standalone_repository = 1\nproject_repository = 2.5\n", "");

        assert!(
            matches!(
                error,
                ConfigError::RepositoriesNotArray {
                    kind: RepoKind::Project,
                    file: ConfigFileName::Shared,
                    found: ValueType::Float,
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn agent_overrides_of_the_local_file_are_checked_before_a_repository_key_of_the_shared_file() {
        let error = refused(
            "project_repository = \"abc\"\n",
            "agent_model_overrides = true\n",
        );

        assert!(
            matches!(
                error,
                ConfigError::AgentOverridesNotTable {
                    file: ConfigFileName::Local,
                    found: ValueType::Boolean,
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn the_checks_of_each_file_come_before_the_merge() {
        let error = refused("git = \"x\"\n", "project_repository = 1\n\n[git]\n");

        assert!(
            matches!(error, ConfigError::RepositoriesNotArray { .. }),
            "{error:?}"
        );
    }

    #[test]
    fn a_refusal_of_the_merge_and_a_refusal_of_the_build_end_the_load() {
        let of_the_merge = refused("git = \"x\"\n", "[git]\n");
        let of_the_build = refused("", "adopt_extensions = \"some\"\n");

        assert!(
            matches!(of_the_merge, ConfigError::ValueType { .. }),
            "{of_the_merge:?}"
        );
        assert!(
            matches!(of_the_build, ConfigError::InvalidChoice { .. }),
            "{of_the_build:?}"
        );
    }

    fn reloaded(
        files: &MemoryFiles,
        edited: ConfigFileName,
        bytes: &[u8],
    ) -> Result<Workspace, ConfigError> {
        reload(files, Path::new("/ws"), edited, bytes)
    }

    #[test]
    fn a_reload_of_the_bytes_the_file_holds_is_the_workspace_the_load_gives() {
        let shared = "envs_per_workspace = 12\n[[project_repository]]\nname = \"api\"\n";
        let local = "env_aliases = [\"one\"]\n";
        let files = files(shared, local);
        let loaded = load(&files, Path::new("/ws")).expect("a workspace");

        let from_shared = reloaded(&files, ConfigFileName::Shared, shared.as_bytes());
        let from_local = reloaded(&files, ConfigFileName::Local, local.as_bytes());

        assert_eq!(
            format!("{:?}", from_shared.expect("shared")),
            format!("{loaded:?}")
        );
        assert_eq!(
            format!("{:?}", from_local.expect("local")),
            format!("{loaded:?}")
        );
    }

    #[test]
    fn reloaded_bytes_that_are_not_utf_8_are_a_toml_file_read_error_naming_the_edited_file() {
        for (edited, path) in [
            (ConfigFileName::Shared, SHARED),
            (ConfigFileName::Local, LOCAL),
        ] {
            let error = reloaded(&files("", ""), edited, b"a = \"\xff\"\n").expect_err("a refusal");

            assert!(
                matches!(
                    &error,
                    ConfigError::FileRead { path: named, cause: FileCause::Toml(_) } if named == Path::new(path)
                ),
                "{error:?}"
            );
        }
    }

    #[test]
    fn reloaded_bytes_stand_in_for_the_edited_file_and_the_other_file_is_read() {
        let files = files("envs_per_workspace = 12\n", "envs_per_workspace = 15\n");

        let in_place_of_shared =
            reloaded(&files, ConfigFileName::Shared, b"envs_per_workspace = 30\n");
        let in_place_of_local = reloaded(&files, ConfigFileName::Local, b"");

        // The local file wins the merge, so the edited shared bytes show only without a local value.
        assert_eq!(
            in_place_of_shared
                .expect("workspace")
                .config
                .ports
                .envs_per_workspace,
            15
        );
        assert_eq!(
            in_place_of_local
                .expect("workspace")
                .config
                .ports
                .envs_per_workspace,
            12
        );
    }

    #[test]
    fn a_local_file_with_an_invalid_key_is_that_keys_error_while_the_edited_shared_bytes_are_valid()
    {
        let files = files("", "project_repository = 3\n");

        let error = reloaded(&files, ConfigFileName::Shared, b"envs_per_workspace = 12\n")
            .expect_err("a refusal");

        assert!(
            matches!(
                error,
                ConfigError::RepositoriesNotArray {
                    kind: RepoKind::Project,
                    file: ConfigFileName::Local,
                    found: ValueType::Integer,
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn the_shared_file_is_read_through_the_files_when_the_local_one_is_edited() {
        let files = files("= broken\n", "");

        let error = reloaded(&files, ConfigFileName::Local, b"").expect_err("a refusal");

        assert!(
            matches!(&error, ConfigError::FileRead { path, .. } if path == Path::new(SHARED)),
            "{error:?}"
        );
    }
}
