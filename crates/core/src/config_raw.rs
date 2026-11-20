//! The two configuration keys the env index depends on, read from the shared and the local configuration
//! file of a workspace: layered, coerced, defaulted, and checked against each other.
//!
//! No other key or table is read, and nothing else about either file is validated.

use std::path::Path;

use toml::{Table, Value};

use crate::files::ReadFiles;
use crate::termination::{ConfigError, FileCause, TomlParseError};
use crate::workspace_files::{CONFIG_FILE, LOCAL_CONFIG_FILE};

const ENV_ALIASES: &str = "env_aliases";
const ENVS_PER_WORKSPACE: &str = "envs_per_workspace";

const DEFAULT_ENV_ALIASES: [&str; 10] = [
    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa",
];
const DEFAULT_ENVS_PER_WORKSPACE: i64 = 48;

/// What the configuration of a workspace says about env indexes.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct RawConfig {
    pub env_aliases: Vec<String>,
    /// At least `env_aliases.len() + 2`.
    pub envs_per_workspace: i64,
}

/// Loads the two keys of the workspace at `root`.
///
/// Each key is taken from the local file when that file holds it, whatever the type of the value there, else
/// from the shared file, else it is its default. A missing file holds no key. A file that exists and cannot
/// be read, or is not valid TOML, is [`ConfigError::FileRead`]; the shared file is loaded first.
pub(crate) fn load(files: &dyn ReadFiles, root: &Path) -> Result<RawConfig, ConfigError> {
    let shared = document(files, &root.join(CONFIG_FILE))?;
    let local = document(files, &root.join(LOCAL_CONFIG_FILE))?;
    let layered = |key: &str| local.get(key).or_else(|| shared.get(key));

    let env_aliases = match layered(ENV_ALIASES) {
        Some(value) => aliases(value),
        None => DEFAULT_ENV_ALIASES.map(str::to_owned).to_vec(),
    };
    let envs_per_workspace = match layered(ENVS_PER_WORKSPACE) {
        Some(Value::Integer(count)) => *count,
        _ => DEFAULT_ENVS_PER_WORKSPACE,
    };

    let smallest = i64::try_from(env_aliases.len())
        .unwrap_or(i64::MAX)
        .saturating_add(2);
    if envs_per_workspace < smallest {
        return Err(ConfigError::EnvsPerWorkspaceTooSmall {
            envs_per_workspace,
            aliases: env_aliases.len(),
        });
    }
    Ok(RawConfig {
        env_aliases,
        envs_per_workspace,
    })
}

/// The document of the configuration file at `path`; an empty one when no such file exists.
fn document(files: &dyn ReadFiles, path: &Path) -> Result<Table, ConfigError> {
    let file_read = |cause: FileCause| ConfigError::FileRead {
        path: path.to_path_buf(),
        cause,
    };
    match files.read(path) {
        Ok(Some(bytes)) => toml::from_slice(&bytes)
            .map_err(|error| file_read(FileCause::Toml(TomlParseError::from(error)))),
        Ok(None) => Ok(Table::new()),
        Err(error) => Err(file_read(FileCause::Io(error))),
    }
}

/// The alias list a value of `env_aliases` stands for: a string is a list of that one string, an array is
/// its items, and any other value is no alias. An item that is not a string, or is empty, is dropped.
fn aliases(value: &Value) -> Vec<String> {
    let items = match value {
        Value::String(_) => std::slice::from_ref(value),
        Value::Array(items) => items.as_slice(),
        _ => &[],
    };
    items
        .iter()
        .filter_map(Value::as_str)
        .filter(|alias| !alias.is_empty())
        .map(str::to_owned)
        .collect()
}

#[cfg(test)]
mod tests {
    use std::io::ErrorKind;

    use super::*;
    use crate::files::MemoryFiles;

    const SHARED: &str = "/ws/.winter/config.toml";
    const LOCAL: &str = "/ws/.winter/config.local.toml";

    fn load_from(files: &MemoryFiles) -> Result<RawConfig, ConfigError> {
        load(files, Path::new("/ws"))
    }

    fn shared(text: &str) -> RawConfig {
        load_from(&MemoryFiles::default().with(SHARED, text.as_bytes())).expect("a configuration")
    }

    fn layered(shared: &str, local: &str) -> RawConfig {
        let files = MemoryFiles::default()
            .with(SHARED, shared.as_bytes())
            .with(LOCAL, local.as_bytes());
        load_from(&files).expect("a configuration")
    }

    fn strings(items: &[&str]) -> Vec<String> {
        items.iter().map(|item| (*item).to_owned()).collect()
    }

    #[test]
    fn absent_keys_take_their_defaults() {
        let config = shared("# nothing declared\n");

        assert_eq!(
            config.env_aliases,
            strings(&[
                "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota",
                "kappa"
            ])
        );
        assert_eq!(config.envs_per_workspace, 48);
    }

    #[test]
    fn missing_files_hold_no_key() {
        let config = load_from(&MemoryFiles::default()).expect("a configuration");

        assert_eq!(config.env_aliases.len(), 10);
        assert_eq!(config.envs_per_workspace, 48);
    }

    #[test]
    fn declared_keys_are_read() {
        let config = shared("env_aliases = [\"one\", \"two\"]\nenvs_per_workspace = 12\n");

        assert_eq!(config.env_aliases, strings(&["one", "two"]));
        assert_eq!(config.envs_per_workspace, 12);
    }

    #[test]
    fn a_bare_string_is_a_list_of_one_alias() {
        assert_eq!(
            shared("env_aliases = \"solo\"\n").env_aliases,
            strings(&["solo"])
        );
    }

    #[test]
    fn a_bare_empty_string_is_no_alias() {
        assert_eq!(shared("env_aliases = \"\"\n").env_aliases, strings(&[]));
    }

    #[test]
    fn items_that_are_not_strings_or_are_empty_are_dropped() {
        let config = shared("env_aliases = [\"one\", 2, \"\", true, [\"deep\"], \"two\"]\n");

        assert_eq!(config.env_aliases, strings(&["one", "two"]));
    }

    #[test]
    fn a_value_of_another_type_is_no_alias() {
        for value in ["7", "true", "1.5", "{ a = \"one\" }"] {
            let config = shared(&format!("env_aliases = {value}\n"));

            assert_eq!(config.env_aliases, strings(&[]), "{value}");
        }
    }

    #[test]
    fn an_envs_per_workspace_of_the_wrong_type_falls_back_to_the_default() {
        for value in ["true", "\"12\"", "12.0", "[12]"] {
            let config = shared(&format!("envs_per_workspace = {value}\n"));

            assert_eq!(config.envs_per_workspace, 48, "{value}");
        }
    }

    #[test]
    fn a_local_key_replaces_the_shared_one_whole() {
        let config = layered(
            "env_aliases = [\"one\", \"two\"]\nenvs_per_workspace = 12\n",
            "env_aliases = [\"three\"]\n",
        );

        assert_eq!(config.env_aliases, strings(&["three"]));
        assert_eq!(config.envs_per_workspace, 12);
    }

    #[test]
    fn a_local_value_replaces_the_shared_one_whatever_its_type() {
        let config = layered(
            "env_aliases = [\"one\", \"two\"]\nenvs_per_workspace = 12\n",
            "env_aliases = 5\nenvs_per_workspace = \"many\"\n",
        );

        assert_eq!(config.env_aliases, strings(&[]));
        assert_eq!(config.envs_per_workspace, 48);
    }

    #[test]
    fn an_empty_local_file_leaves_the_shared_keys() {
        let config = layered("env_aliases = [\"one\"]\nenvs_per_workspace = 9\n", "");

        assert_eq!(config.env_aliases, strings(&["one"]));
        assert_eq!(config.envs_per_workspace, 9);
    }

    #[test]
    fn the_smallest_envs_per_workspace_the_aliases_allow_is_accepted() {
        let config = shared("env_aliases = [\"one\", \"two\"]\nenvs_per_workspace = 4\n");

        assert_eq!(config.envs_per_workspace, 4);
    }

    #[test]
    fn an_envs_per_workspace_above_the_range_of_32_bits_is_kept() {
        assert_eq!(
            shared("envs_per_workspace = 5000000000\n").envs_per_workspace,
            5_000_000_000
        );
    }

    #[test]
    fn an_envs_per_workspace_below_the_alias_count_plus_two_is_refused() {
        let files = MemoryFiles::default().with(
            SHARED,
            b"env_aliases = [\"one\", \"two\"]\nenvs_per_workspace = 3\n",
        );

        let error = load_from(&files).expect_err("a refusal");

        assert!(
            matches!(
                error,
                ConfigError::EnvsPerWorkspaceTooSmall {
                    envs_per_workspace: 3,
                    aliases: 2
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_negative_envs_per_workspace_is_refused_with_its_value() {
        let files =
            MemoryFiles::default().with(SHARED, b"env_aliases = []\nenvs_per_workspace = -1\n");

        let error = load_from(&files).expect_err("a refusal");

        assert!(
            matches!(
                error,
                ConfigError::EnvsPerWorkspaceTooSmall {
                    envs_per_workspace: -1,
                    aliases: 0
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn the_default_envs_per_workspace_is_checked_against_the_aliases_too() {
        let aliases: Vec<String> = (0..47).map(|n| format!("\"env{n}\"")).collect();
        let text = format!("env_aliases = [{}]\n", aliases.join(", "));
        let files = MemoryFiles::default().with(SHARED, text.as_bytes());

        let error = load_from(&files).expect_err("a refusal");

        assert!(
            matches!(
                error,
                ConfigError::EnvsPerWorkspaceTooSmall {
                    envs_per_workspace: 48,
                    aliases: 47
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn the_cross_key_rule_is_checked_on_the_layered_values() {
        let files = MemoryFiles::default()
            .with(SHARED, b"env_aliases = [\"one\"]\nenvs_per_workspace = 3\n")
            .with(LOCAL, b"env_aliases = [\"one\", \"two\", \"three\"]\n");

        let error = load_from(&files).expect_err("a refusal");

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

            let error = load_from(&files).expect_err("a refusal");

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
    fn a_file_that_is_not_utf_8_is_not_valid_toml() {
        let files = MemoryFiles::default().with(SHARED, &[0xff, 0xfe, b'\n']);

        let error = load_from(&files).expect_err("a refusal");

        assert!(
            matches!(
                &error,
                ConfigError::FileRead { path, cause: FileCause::Toml(_) } if path == Path::new(SHARED)
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_file_that_cannot_be_read_is_a_file_read_error_with_the_io_cause() {
        let files = MemoryFiles::default()
            .with(SHARED, b"")
            .unreadable(LOCAL, ErrorKind::PermissionDenied);

        let error = load_from(&files).expect_err("a refusal");

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
        let files = MemoryFiles::default()
            .with(SHARED, b"= broken\n")
            .with(LOCAL, b"= broken\n");

        let error = load_from(&files).expect_err("a refusal");

        assert!(
            matches!(&error, ConfigError::FileRead { path, .. } if path == Path::new(SHARED)),
            "{error:?}"
        );
    }
}
