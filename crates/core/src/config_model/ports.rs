//! The port layout and the env index layout: `base_port`, `ports_per_env`, `env_aliases`, and
//! `envs_per_workspace`, coerced, defaulted, and checked against each other.

use toml::Table;

use super::{Ports, integer_or, string_list};
use crate::termination::ConfigError;

const DEFAULT_BASE_PORT: i64 = 4000;
const DEFAULT_PORTS_PER_ENV: i64 = 20;
const DEFAULT_ENV_ALIASES: [&str; 10] = [
    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa",
];
const DEFAULT_ENVS_PER_WORKSPACE: i64 = 48;

/// The four keys of `merged`.
///
/// A number key that is absent or not an integer is its default, and an integer is kept whatever its sign or
/// size. An `envs_per_workspace` below the number of aliases plus two is
/// [`ConfigError::EnvsPerWorkspaceTooSmall`], whether it was declared or is the default.
pub(super) fn ports(merged: &Table) -> Result<Ports, ConfigError> {
    let env_aliases = match merged.get("env_aliases") {
        Some(value) => string_list(value),
        None => DEFAULT_ENV_ALIASES.map(str::to_owned).to_vec(),
    };
    let envs_per_workspace = integer_or(merged, "envs_per_workspace", DEFAULT_ENVS_PER_WORKSPACE);

    let smallest = i64::try_from(env_aliases.len())
        .unwrap_or(i64::MAX)
        .saturating_add(2);
    if envs_per_workspace < smallest {
        return Err(ConfigError::EnvsPerWorkspaceTooSmall {
            envs_per_workspace,
            aliases: env_aliases.len(),
        });
    }
    Ok(Ports {
        base_port: integer_or(merged, "base_port", DEFAULT_BASE_PORT),
        ports_per_env: integer_or(merged, "ports_per_env", DEFAULT_PORTS_PER_ENV),
        env_aliases,
        envs_per_workspace,
    })
}

#[cfg(test)]
mod tests {
    use super::super::table;
    use super::*;

    fn ports_of(text: &str) -> Ports {
        ports(&table(text)).expect("the ports")
    }

    fn strings(items: &[&str]) -> Vec<String> {
        items.iter().map(|item| (*item).to_owned()).collect()
    }

    #[test]
    fn absent_keys_take_their_defaults() {
        assert_eq!(
            ports_of("# nothing declared\n"),
            Ports {
                base_port: 4000,
                ports_per_env: 20,
                env_aliases: strings(&[
                    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota",
                    "kappa"
                ]),
                envs_per_workspace: 48,
            }
        );
    }

    #[test]
    fn declared_keys_are_read() {
        assert_eq!(
            ports_of(
                "base_port = 5000\nports_per_env = 7\nenv_aliases = [\"one\", \"two\"]\n\
                 envs_per_workspace = 12\n"
            ),
            Ports {
                base_port: 5000,
                ports_per_env: 7,
                env_aliases: strings(&["one", "two"]),
                envs_per_workspace: 12,
            }
        );
    }

    #[test]
    fn a_base_port_or_a_ports_per_env_of_zero_or_below_is_kept() {
        let ports = ports_of("base_port = 0\nports_per_env = -3\n");

        assert_eq!(ports.base_port, 0);
        assert_eq!(ports.ports_per_env, -3);
    }

    #[test]
    fn a_base_port_or_a_ports_per_env_of_the_wrong_type_falls_back_to_the_default() {
        for value in ["true", "\"12\"", "12.0", "[12]"] {
            let ports = ports_of(&format!("base_port = {value}\nports_per_env = {value}\n"));

            assert_eq!(ports.base_port, 4000, "{value}");
            assert_eq!(ports.ports_per_env, 20, "{value}");
        }
    }

    #[test]
    fn a_bare_string_is_a_list_of_one_alias() {
        assert_eq!(
            ports_of("env_aliases = \"solo\"\n").env_aliases,
            strings(&["solo"])
        );
    }

    #[test]
    fn a_bare_empty_string_is_no_alias() {
        assert_eq!(ports_of("env_aliases = \"\"\n").env_aliases, strings(&[]));
    }

    #[test]
    fn items_that_are_not_strings_or_are_empty_are_dropped() {
        let ports = ports_of("env_aliases = [\"one\", 2, \"\", true, [\"deep\"], \"two\"]\n");

        assert_eq!(ports.env_aliases, strings(&["one", "two"]));
    }

    #[test]
    fn an_alias_that_appears_twice_is_kept_twice() {
        assert_eq!(
            ports_of("env_aliases = [\"one\", \"two\", \"one\"]\n").env_aliases,
            strings(&["one", "two", "one"])
        );
    }

    #[test]
    fn a_value_of_another_type_is_no_alias() {
        for value in ["7", "true", "1.5", "{ a = \"one\" }"] {
            let ports = ports_of(&format!("env_aliases = {value}\n"));

            assert_eq!(ports.env_aliases, strings(&[]), "{value}");
        }
    }

    #[test]
    fn an_envs_per_workspace_of_the_wrong_type_falls_back_to_the_default() {
        for value in ["true", "\"12\"", "12.0", "[12]"] {
            let ports = ports_of(&format!("envs_per_workspace = {value}\n"));

            assert_eq!(ports.envs_per_workspace, 48, "{value}");
        }
    }

    #[test]
    fn the_smallest_envs_per_workspace_the_aliases_allow_is_accepted() {
        let ports = ports_of("env_aliases = [\"one\", \"two\"]\nenvs_per_workspace = 4\n");

        assert_eq!(ports.envs_per_workspace, 4);
    }

    #[test]
    fn an_envs_per_workspace_above_the_range_of_32_bits_is_kept() {
        assert_eq!(
            ports_of("envs_per_workspace = 5000000000\n").envs_per_workspace,
            5_000_000_000
        );
    }

    #[test]
    fn an_envs_per_workspace_below_the_alias_count_plus_two_is_refused() {
        let error = ports(&table(
            "env_aliases = [\"one\", \"two\"]\nenvs_per_workspace = 3\n",
        ))
        .expect_err("a refusal");

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
        let error =
            ports(&table("env_aliases = []\nenvs_per_workspace = -1\n")).expect_err("a refusal");

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

        let error = ports(&table(&text)).expect_err("a refusal");

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
}
