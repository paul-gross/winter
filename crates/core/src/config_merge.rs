//! The merge of the shared and the local configuration table into one. It reads no file.

use toml::{Table, Value};

use crate::config_model::{REPOSITORY_ARRAYS, is_empty_form, value_type};
use crate::termination::{ConfigError, MergedTable, TypedKey};

/// The top-level tables the local file merges into the shared one, in the order they are merged.
const MERGED_TABLES: [MergedTable; 7] = [
    MergedTable::Git,
    MergedTable::Keybindings,
    MergedTable::Tui,
    MergedTable::Capabilities,
    MergedTable::Env,
    MergedTable::ModelTiers,
    MergedTable::AgentModelOverrides,
];

/// The shared table with the local one laid over it.
///
/// - A table of [`MERGED_TABLES`] that the local file holds is merged one level deep: a direct child of the
///   local table replaces the shared child of the same name, and the other shared children stay. A local value
///   that is not a table adds nothing. The shared value is the base: one that is absent or an empty form is an
///   empty table, and any other value that is not a table is [`ConfigError::ValueType`], an array of pairs
///   included. A table the local file does not hold stays as the shared file has it, whatever its type.
/// - The two repository arrays are appended, the shared entries first.
/// - The local value of every other key replaces the shared one whole, whatever the type of either.
///
/// A key the shared file holds keeps its place there, whichever rule it falls under. The keys only the local
/// file holds follow: first its tables of [`MERGED_TABLES`], in the order of that list, then its other keys,
/// in the order of the local file. Inside a merged table the shared children keep their places, and the
/// children only the local table holds follow in its order.
pub(crate) fn merge(mut shared: Table, mut local: Table) -> Result<Table, ConfigError> {
    for table in MERGED_TABLES {
        let key = table.to_string();
        let Some(overlay) = local.remove(&key) else {
            continue;
        };
        let base = shared
            .entry(key)
            .or_insert_with(|| Value::Table(Table::new()));
        if !base.is_table() {
            if !is_empty_form(base) {
                return Err(ConfigError::ValueType {
                    key: TypedKey::Table(table),
                    owner: None,
                    found: value_type(base),
                });
            }
            *base = Value::Table(Table::new());
        }
        if let (Value::Table(base), Value::Table(overlay)) = (base, overlay) {
            base.extend(overlay);
        }
    }
    for (_, key) in REPOSITORY_ARRAYS {
        if let (Some(Value::Array(entries)), Some(Value::Array(_))) =
            (shared.get_mut(key), local.get(key))
            && let Some(Value::Array(added)) = local.remove(key)
        {
            entries.extend(added);
        }
    }
    shared.extend(local);
    Ok(shared)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config_model::table;
    use crate::termination::ValueType;

    fn merged(shared: &str, local: &str) -> Table {
        merge(table(shared), table(local)).expect("a merged table")
    }

    fn refused(shared: &str, local: &str) -> ConfigError {
        merge(table(shared), table(local)).expect_err("a refusal")
    }

    #[test]
    fn an_empty_local_file_leaves_the_shared_document_unchanged() {
        let shared = "base_port = 5000\ngit_excludes = [\"a\"]\n\n[git.user]\nname = \"n\"\n\n\
                      [[project_repository]]\nname = \"one\"\n";

        assert_eq!(merged(shared, ""), table(shared));
    }

    #[test]
    fn the_local_repository_entries_follow_the_shared_ones() {
        let document = merged(
            "[[project_repository]]\nname = \"one\"\n\n[[standalone_repository]]\nname = \"tools\"\n",
            "[[project_repository]]\nname = \"two\"\n\n[[standalone_repository]]\nname = \"more\"\n",
        );

        assert_eq!(
            document,
            table(
                "[[project_repository]]\nname = \"one\"\n\n[[project_repository]]\nname = \"two\"\n\n\
                 [[standalone_repository]]\nname = \"tools\"\n\n[[standalone_repository]]\nname = \"more\"\n"
            )
        );
    }

    #[test]
    fn a_repository_array_only_one_file_holds_is_kept() {
        let document = merged(
            "[[project_repository]]\nname = \"one\"\n",
            "[[standalone_repository]]\nname = \"more\"\n",
        );

        assert_eq!(
            document,
            table(
                "[[project_repository]]\nname = \"one\"\n\n[[standalone_repository]]\nname = \"more\"\n"
            )
        );
    }

    #[test]
    fn a_local_child_replaces_the_shared_child_of_the_same_name_and_the_others_survive() {
        for key in MERGED_TABLES {
            let document = merged(
                &format!("[{key}]\nkept = 1\nreplaced = \"shared\"\n"),
                &format!("[{key}]\nreplaced = \"local\"\nadded = true\n"),
            );

            assert_eq!(
                document,
                table(&format!(
                    "[{key}]\nkept = 1\nreplaced = \"local\"\nadded = true\n"
                )),
                "{key}"
            );
        }
    }

    #[test]
    fn a_local_value_that_is_not_a_table_leaves_the_shared_table_untouched() {
        for key in MERGED_TABLES {
            let shared = format!("[{key}]\nkept = 1\n");

            assert_eq!(
                merged(&shared, &format!("{key} = \"x\"\n")),
                table(&shared),
                "{key}"
            );
        }
    }

    #[test]
    fn the_local_value_of_any_other_key_replaces_the_shared_one_whatever_its_type() {
        let document = merged(
            "git_excludes = [\"a\", \"b\"]\ncore_checks = 3\nprovision = \"x\"\nmain_branch = \"trunk\"\n\
             [lint]\nscripts = [\"a\"]\n\n[[service]]\nname = \"one\"\n",
            "git_excludes = \"c\"\nlint = \"check.sh\"\nservice = false\n\n[core_checks]\nfile_size = 1\n\n\
             [provision]\nall = []\n",
        );

        assert_eq!(
            document,
            table(
                "git_excludes = \"c\"\nlint = \"check.sh\"\nservice = false\nmain_branch = \"trunk\"\n\n\
                 [core_checks]\nfile_size = 1\n\n[provision]\nall = []\n"
            )
        );
    }

    #[test]
    fn a_local_git_user_drops_the_shared_email() {
        let document = merged(
            "[git.user]\nname = \"shared\"\nemail = \"shared@example.invalid\"\n",
            "[git]\nuser = { name = \"local\" }\n",
        );

        assert_eq!(document, table("[git.user]\nname = \"local\"\n"));
    }

    #[test]
    fn local_bindings_replace_the_shared_bindings_and_the_other_keys_survive() {
        let document = merged(
            "[keybindings]\nleader = \",\"\ntimeoutlen = 300\n\n[keybindings.bindings]\nquit = \"q\"\n\
             help = \"h\"\n",
            "[keybindings.bindings]\nopen = \"o\"\n",
        );

        assert_eq!(
            document,
            table(
                "[keybindings]\nleader = \",\"\ntimeoutlen = 300\n\n[keybindings.bindings]\nopen = \"o\"\n"
            )
        );
    }

    #[test]
    fn local_feature_vars_replace_the_shared_ones_and_the_other_bands_survive() {
        let document = merged(
            "[env.workspace.vars]\nA = \"1\"\n\n[env.feature.vars]\nB = \"2\"\nC = \"3\"\n\n\
             [env.alpha.vars]\nD = \"4\"\n",
            "[env.feature.vars]\nE = \"5\"\n",
        );

        assert_eq!(
            document,
            table(
                "[env.workspace.vars]\nA = \"1\"\n\n[env.feature.vars]\nE = \"5\"\n\n\
                 [env.alpha.vars]\nD = \"4\"\n"
            )
        );
    }

    #[test]
    fn a_local_capability_slot_replaces_the_shared_list_and_the_other_slots_survive() {
        let document = merged(
            "[capabilities]\nservice = [\"tmux\", \"docker\"]\nother = \"x\"\n",
            "[capabilities]\nservice = \"docker\"\n",
        );

        assert_eq!(
            document,
            table("[capabilities]\nservice = \"docker\"\nother = \"x\"\n")
        );
    }

    #[test]
    fn a_local_space_drops_the_shared_kinds() {
        let document = merged(
            "[space]\nroot = \"shared\"\n\n[space.kinds]\nnotes = \"n\"\n",
            "[space]\nroot = \"local\"\n",
        );

        assert_eq!(document, table("[space]\nroot = \"local\"\n"));
    }

    #[test]
    fn a_local_lint_and_local_git_excludes_replace_the_shared_values() {
        let document = merged(
            "git_excludes = [\"a\"]\n\n[lint]\nscripts = [\"a.sh\"]\n\n[lint.ignore]\npaths = [\"x\"]\n",
            "git_excludes = [\"b\"]\nlint = [\"b.sh\"]\n",
        );

        assert_eq!(
            document,
            table("git_excludes = [\"b\"]\nlint = [\"b.sh\"]\n")
        );
    }

    #[test]
    fn an_absent_or_empty_shared_value_of_a_merged_table_is_an_empty_table() {
        for key in MERGED_TABLES {
            for shared in ["", "false", "0", "0.0", "\"\"", "[]", "{}"] {
                let shared = if shared.is_empty() {
                    String::new()
                } else {
                    format!("{key} = {shared}\n")
                };

                assert_eq!(
                    merged(&shared, &format!("[{key}]\nadded = 1\n")),
                    table(&format!("[{key}]\nadded = 1\n")),
                    "{key}: {shared}"
                );
                assert_eq!(
                    merged(&shared, &format!("{key} = 5\n")),
                    table(&format!("{key} = {{}}\n")),
                    "{key}: {shared}"
                );
            }
        }
    }

    #[test]
    fn any_other_shared_value_of_a_merged_table_the_local_file_holds_is_a_value_type_error() {
        let values = [
            ("true", ValueType::Boolean),
            ("7", ValueType::Integer),
            ("1.5", ValueType::Float),
            ("\"x\"", ValueType::String),
            ("[1]", ValueType::Array),
            ("1979-05-27", ValueType::Datetime),
        ];
        for table in MERGED_TABLES {
            for (value, found) in values {
                for local in [format!("[{table}]\n"), format!("{table} = 5\n")] {
                    let error = refused(&format!("{table} = {value}\n"), &local);

                    assert!(
                        matches!(
                            &error,
                            ConfigError::ValueType { key: TypedKey::Table(key), owner: None, found: seen }
                                if *key == table && *seen == found
                        ),
                        "{table} = {value}: {error:?}"
                    );
                }
            }
        }
    }

    #[test]
    fn a_shared_git_string_under_a_local_git_table_is_a_value_type_error_of_the_git_table() {
        let error = refused("git = \"x\"\n", "[git]\n");

        assert!(
            matches!(
                error,
                ConfigError::ValueType {
                    key: TypedKey::Table(MergedTable::Git),
                    owner: None,
                    found: ValueType::String,
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_shared_array_of_pairs_under_a_local_table_is_a_value_type_error_with_the_type_array() {
        let error = refused("keybindings = [[\"leader\", \"x\"]]\n", "[keybindings]\n");

        assert!(
            matches!(
                error,
                ConfigError::ValueType {
                    key: TypedKey::Table(MergedTable::Keybindings),
                    owner: None,
                    found: ValueType::Array,
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_shared_value_of_a_merged_table_the_local_file_does_not_hold_passes_unchanged() {
        let shared = "keybindings = [[\"leader\", \"x\"]]\ngit = \"x\"\ntui = 7\n";

        assert_eq!(merged(shared, "base_port = 1\n"), {
            let mut expected = table(shared);
            expected.insert("base_port".to_owned(), Value::Integer(1));
            expected
        });
    }

    #[test]
    fn the_shared_keys_keep_their_places_and_the_keys_of_the_local_file_alone_follow() {
        let document = merged(
            "zeta = 1\n\n[tui]\nkept = 1\n\n[capabilities]\nservice = \"a\"\nother = \"b\"\n\n\
             [[project_repository]]\nname = \"one\"\n\n[alpha]\n",
            "omega = 1\nzeta = 2\nbase_port = 3\n\n[env]\n\n[capabilities]\nnew = \"c\"\nother = \"d\"\n\
             early = \"e\"\n\n[git]\n\n[[project_repository]]\nname = \"two\"\n",
        );

        let keys = |table: &Table| table.keys().cloned().collect::<Vec<_>>();
        assert_eq!(
            keys(&document),
            [
                "zeta",
                "tui",
                "capabilities",
                "project_repository",
                "alpha",
                "git",
                "env",
                "omega",
                "base_port"
            ]
        );
        assert_eq!(
            keys(document["capabilities"].as_table().expect("a table")),
            ["service", "other", "new", "early"]
        );
    }

    #[test]
    fn of_several_refused_tables_the_first_in_the_order_of_the_merge_is_reported() {
        let error = refused("tui = 7\ngit = \"x\"\n", "[tui]\n\n[git]\n");

        assert!(
            matches!(
                error,
                ConfigError::ValueType {
                    key: TypedKey::Table(MergedTable::Git),
                    ..
                }
            ),
            "{error:?}"
        );
    }
}
