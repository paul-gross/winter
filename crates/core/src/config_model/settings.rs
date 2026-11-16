//! The top-level keys and the small tables no other child of the model owns: the git identity, the key
//! bindings, the dashboard layout, the capabilities, the file size limits, the artifact space, the removed
//! keys, and the raw `provision` and `service`.
//!
//! A key that falls back is read as its default, with no message, when its value is of a type it does not
//! take. `keybindings`, `tui`, `capabilities`, `core_checks`, and `space`, and the tables inside them, are
//! read as absent when they are not tables.

use toml::{Table, Value};

use super::{
    AdoptExtensions, DashboardLayout, FileSizeLimits, GitIdentity, Keybindings, RawArray, RawTable,
    Space, config_value, integer_or, is_empty_form, string, string_list, strings, wrong_type,
};
use crate::termination::{ChoiceKey, ConfigError, MergedTable, RemovedKey, TypedKey};

const MAIN_BRANCH: &str = "main_branch";
const DEFAULT_MAIN_BRANCH: &str = "main";

/// The table `table` holds under `key`, and none when the key is absent or its value is no table.
fn table_at<'t>(table: &'t Table, key: &str) -> Option<&'t Table> {
    table.get(key).and_then(Value::as_table)
}

/// The string `table` holds under `key` when it is not empty, and none when the key is absent, its value is
/// of another type, or the string is empty.
fn non_empty_string<'t>(table: &'t Table, key: &str) -> Option<&'t str> {
    table
        .get(key)
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
}

/// The git identity of `[git.user]`: its `name` and `email`, and none unless both are there and neither is
/// an empty form.
///
/// A `git` or a `git.user` that is an empty form is read as absent: there is no identity. One that is
/// neither a table nor an empty form, and a name or an email that is not a string where neither of the two
/// is an empty form, are each [`ConfigError::ValueType`]; the name is checked before the email.
pub(super) fn git_identity(merged: &Table) -> Result<Option<GitIdentity>, ConfigError> {
    let git = match merged.get("git") {
        Some(Value::Table(git)) => git,
        Some(other) if !is_empty_form(other) => {
            return Err(wrong_type(other, TypedKey::Table(MergedTable::Git), None));
        }
        _ => return Ok(None),
    };
    let user = match git.get("user") {
        Some(Value::Table(user)) => user,
        Some(other) if !is_empty_form(other) => {
            return Err(wrong_type(other, TypedKey::GitUser, None));
        }
        _ => return Ok(None),
    };
    let (Some(name), Some(email)) = (user.get("name"), user.get("email")) else {
        return Ok(None);
    };
    if is_empty_form(name) || is_empty_form(email) {
        return Ok(None);
    }
    Ok(Some(GitIdentity {
        name: string(name, TypedKey::GitUserName, None)?.to_owned(),
        email: string(email, TypedKey::GitUserEmail, None)?.to_owned(),
    }))
}

/// The key bindings of `[keybindings]`.
///
/// `leader` is any string, the empty one included, and a backslash otherwise; `timeoutlen` is an integer,
/// and 1000 otherwise; `bindings` are the entries of that table in document order, each value as written
/// whatever its type, and none when it is no table.
pub(super) fn keybindings(merged: &Table) -> Keybindings {
    let table = table_at(merged, "keybindings");
    let leader = table
        .and_then(|table| table.get("leader"))
        .and_then(Value::as_str)
        .unwrap_or("\\");
    let bindings = table
        .and_then(|table| table_at(table, "bindings"))
        .map(|bindings| {
            bindings
                .iter()
                .map(|(action, key)| (action.clone(), config_value(key)))
                .collect()
        })
        .unwrap_or_default();
    Keybindings {
        leader: leader.to_owned(),
        timeoutlen: table.map_or(1000, |table| integer_or(table, "timeoutlen", 1000)),
        bindings,
    }
}

/// `tui.dashboard.layout`: `auto`, `repos-as-columns`, `repos-as-rows`, or `list`, and `auto` when the key
/// is absent or `tui` or `tui.dashboard` is no table. Any other value is [`ConfigError::InvalidChoice`],
/// which carries it.
pub(super) fn dashboard_layout(merged: &Table) -> Result<DashboardLayout, ConfigError> {
    let Some(value) = table_at(merged, "tui")
        .and_then(|tui| table_at(tui, "dashboard"))
        .and_then(|dashboard| dashboard.get("layout"))
    else {
        return Ok(DashboardLayout::Auto);
    };
    match value.as_str() {
        Some("auto") => Ok(DashboardLayout::Auto),
        Some("repos-as-columns") => Ok(DashboardLayout::ReposAsColumns),
        Some("repos-as-rows") => Ok(DashboardLayout::ReposAsRows),
        Some("list") => Ok(DashboardLayout::List),
        _ => Err(ConfigError::InvalidChoice {
            key: ChoiceKey::DashboardLayout,
            value: config_value(value),
        }),
    }
}

/// The slots of `[capabilities]` in document order, each with its providers.
///
/// A slot takes one string or an array of them: items that are not strings or are empty are dropped, and a
/// provider named again is kept once, at its first place. A slot left with no provider is dropped. A slot
/// name is not checked.
pub(super) fn capabilities(merged: &Table) -> Vec<(String, Vec<String>)> {
    let Some(table) = table_at(merged, "capabilities") else {
        return Vec::new();
    };
    table
        .iter()
        .filter_map(|(slot, value)| {
            let mut providers: Vec<String> = Vec::new();
            for provider in string_list(value) {
                if !providers.contains(&provider) {
                    providers.push(provider);
                }
            }
            (!providers.is_empty()).then(|| (slot.clone(), providers))
        })
        .collect()
}

/// Refuses a key that was removed, whatever its value: `service_orchestrator`, then `session_prefix`, each
/// as [`ConfigError::RemovedKey`].
pub(super) fn removed_keys(merged: &Table) -> Result<(), ConfigError> {
    if merged.contains_key("service_orchestrator") {
        return Err(ConfigError::RemovedKey(RemovedKey::ServiceOrchestrator));
    }
    if merged.contains_key("session_prefix") {
        return Err(ConfigError::RemovedKey(RemovedKey::SessionPrefix));
    }
    Ok(())
}

/// `adopt_extensions`: `none`, `winter`, or `all`, and `winter` when the key is absent. Any other value is
/// [`ConfigError::InvalidChoice`], which carries it.
pub(super) fn adopt_extensions(merged: &Table) -> Result<AdoptExtensions, ConfigError> {
    let Some(value) = merged.get("adopt_extensions") else {
        return Ok(AdoptExtensions::Winter);
    };
    match value.as_str() {
        Some("none") => Ok(AdoptExtensions::None),
        Some("winter") => Ok(AdoptExtensions::Winter),
        Some("all") => Ok(AdoptExtensions::All),
        _ => Err(ConfigError::InvalidChoice {
            key: ChoiceKey::AdoptExtensions,
            value: config_value(value),
        }),
    }
}

/// `prefix`: a string that is not empty, and `ws` otherwise.
pub(super) fn prefix(merged: &Table) -> String {
    non_empty_string(merged, "prefix")
        .unwrap_or("ws")
        .to_owned()
}

/// The limits of `[core_checks.file_size]`: `injected_bytes` is an integer, and 6000 otherwise;
/// `reference_bytes` is an integer, and 12000 otherwise.
pub(super) fn file_size_limits(merged: &Table) -> FileSizeLimits {
    let table = table_at(merged, "core_checks").and_then(|checks| table_at(checks, "file_size"));
    FileSizeLimits {
        injected_bytes: table.map_or(6000, |table| integer_or(table, "injected_bytes", 6000)),
        reference_bytes: table.map_or(12000, |table| integer_or(table, "reference_bytes", 12000)),
    }
}

/// The artifact space of `[space]`: `root` is a string that is not empty, and `.winter` otherwise; `kinds`
/// are the entries of that table in document order whose value is a string that is not empty, and none when
/// it is no table.
pub(super) fn space(merged: &Table) -> Space {
    let table = table_at(merged, "space");
    let kinds = table
        .and_then(|table| table_at(table, "kinds"))
        .map(|kinds| {
            kinds
                .iter()
                .filter_map(|(kind, directory)| {
                    Some((kind.clone(), directory.as_str()?.to_owned()))
                })
                .filter(|(_, directory)| !directory.is_empty())
                .collect()
        })
        .unwrap_or_default();
    Space {
        root: table
            .and_then(|table| non_empty_string(table, "root"))
            .unwrap_or(".winter")
            .to_owned(),
        kinds,
    }
}

/// The top-level `git_excludes`: an array of strings, and empty when the key is absent. Any other value is
/// [`ConfigError::ValueType`].
pub(super) fn git_excludes(merged: &Table) -> Result<Vec<String>, ConfigError> {
    match merged.get("git_excludes") {
        Some(value) => strings(value, TypedKey::GitExcludes, None),
        None => Ok(Vec::new()),
    }
}

/// The top-level `main_branch`: a string, and `main` when the key is absent or holds an empty form. Any other
/// value is [`ConfigError::ValueType`].
pub(super) fn main_branch(merged: &Table) -> Result<String, ConfigError> {
    match merged.get(MAIN_BRANCH) {
        Some(value) if !is_empty_form(value) => {
            string(value, TypedKey::MainBranch, None).map(str::to_owned)
        }
        _ => Ok(DEFAULT_MAIN_BRANCH.to_owned()),
    }
}

/// The top-level `main_branch` when it is a non-empty string, and `main` otherwise. It refuses nothing, and
/// answers what [`main_branch`] answers whenever that one accepts the value.
pub(super) fn main_branch_or_default(merged: &Table) -> &str {
    match merged.get(MAIN_BRANCH) {
        Some(Value::String(branch)) if !branch.is_empty() => branch,
        _ => DEFAULT_MAIN_BRANCH,
    }
}

/// `service_prefix`: a string that is not empty, and `winter` otherwise.
pub(super) fn service_prefix(merged: &Table) -> String {
    non_empty_string(merged, "service_prefix")
        .unwrap_or("winter")
        .to_owned()
}

/// `skills_dir`: a string that is not empty, and `skills` otherwise.
pub(super) fn skills_dir(merged: &Table) -> String {
    non_empty_string(merged, "skills_dir")
        .unwrap_or("skills")
        .to_owned()
}

/// `doctor`: any string, the empty one included, and none when the key is absent or of another type.
pub(super) fn doctor(merged: &Table) -> Option<String> {
    merged
        .get("doctor")
        .and_then(Value::as_str)
        .map(str::to_owned)
}

/// The lint scripts: one string or an array of them, under `lint` itself or, when `lint` is a table, under
/// its `scripts`. Items that are not strings or are empty are dropped, and a value of another type is no
/// script.
pub(super) fn lint_scripts(merged: &Table) -> Vec<String> {
    let scripts = match merged.get("lint") {
        Some(Value::Table(lint)) => lint.get("scripts"),
        other => other,
    };
    scripts.map(string_list).unwrap_or_default()
}

/// `[provision]` as written, and an empty table when the key is absent or its value is no table.
pub(super) fn provision(merged: &Table) -> RawTable {
    RawTable(table_at(merged, "provision").cloned().unwrap_or_default())
}

/// `[[service]]` as written, and no entry when the key is absent or its value is no array.
pub(super) fn service(merged: &Table) -> RawArray {
    RawArray(
        merged
            .get("service")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default(),
    )
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::super::{build, table};
    use super::*;
    use crate::config_load::load;
    use crate::files::MemoryFiles;
    use crate::termination::{ConfigValue, ValueType};

    /// A value of each TOML type but the table, none of them an empty form.
    const NOT_A_TABLE: [&str; 6] = ["\"text\"", "7", "1.5", "true", "[\"a\"]", "1979-05-27"];

    fn owned(items: &[&str]) -> Vec<String> {
        items.iter().map(|item| (*item).to_owned()).collect()
    }

    fn value(text: &str, found: ValueType) -> ConfigValue {
        ConfigValue {
            text: text.to_owned(),
            found,
        }
    }

    #[test]
    fn a_git_identity_is_the_name_and_the_email_of_git_user() {
        let identity = git_identity(&table(
            "[git.user]\nname = \"Ada\"\nemail = \"ada@example.invalid\"\n",
        ))
        .expect("an identity");

        assert_eq!(
            identity,
            Some(GitIdentity {
                name: "Ada".to_owned(),
                email: "ada@example.invalid".to_owned(),
            })
        );
    }

    #[test]
    fn a_git_or_a_git_user_that_is_an_empty_form_is_read_as_absent() {
        for form in ["false", "0", "0.0", "\"\"", "[]", "{}"] {
            for text in [format!("git = {form}\n"), format!("[git]\nuser = {form}\n")] {
                assert_eq!(
                    git_identity(&table(&text)).expect("no refusal"),
                    None,
                    "{text}"
                );
                assert!(build(&table(&text)).is_ok(), "{text}");
            }
        }
    }

    #[test]
    fn there_is_no_git_identity_unless_both_values_are_there_and_neither_is_an_empty_form() {
        for text in [
            "",
            "[git]\n",
            "[git]\nother = 1\n",
            "[git.user]\n",
            "[git.user]\nname = \"Ada\"\n",
            "[git.user]\nemail = \"ada@example.invalid\"\n",
            "[git.user]\nname = \"\"\nemail = \"ada@example.invalid\"\n",
            "[git.user]\nname = \"Ada\"\nemail = \"\"\n",
            "[git.user]\nname = 0\nemail = \"ada@example.invalid\"\n",
            "[git.user]\nname = \"Ada\"\nemail = false\n",
            "[git.user]\nname = 7\nemail = []\n",
            "[git.user]\nname = {}\nemail = 7\n",
            "[git.user]\nname = 7\n",
        ] {
            assert_eq!(
                git_identity(&table(text)).expect("no refusal"),
                None,
                "{text}"
            );
        }
    }

    #[test]
    fn a_git_that_is_not_a_table_is_a_value_type_error() {
        for (value, found) in [
            ("\"text\"", ValueType::String),
            ("7", ValueType::Integer),
            ("1.5", ValueType::Float),
            ("true", ValueType::Boolean),
            ("[\"a\"]", ValueType::Array),
            ("1979-05-27", ValueType::Datetime),
        ] {
            let error = git_identity(&table(&format!("git = {value}\n"))).expect_err("a refusal");

            assert!(
                matches!(
                    &error,
                    ConfigError::ValueType {
                        key: TypedKey::Table(MergedTable::Git),
                        owner: None,
                        found: seen,
                    } if *seen == found
                ),
                "{value}: {error:?}"
            );
        }
    }

    #[test]
    fn a_git_user_that_is_not_a_table_is_a_value_type_error() {
        for (value, found) in [
            ("\"someone\"", ValueType::String),
            ("7", ValueType::Integer),
            ("1.5", ValueType::Float),
            ("true", ValueType::Boolean),
            ("[\"a\"]", ValueType::Array),
            ("1979-05-27", ValueType::Datetime),
        ] {
            let error =
                git_identity(&table(&format!("[git]\nuser = {value}\n"))).expect_err("a refusal");

            assert!(
                matches!(
                    &error,
                    ConfigError::ValueType { key: TypedKey::GitUser, owner: None, found: seen }
                        if *seen == found
                ),
                "{value}: {error:?}"
            );
        }
    }

    #[test]
    fn a_name_or_an_email_that_is_no_string_is_refused_when_neither_is_an_empty_form() {
        for (name, email, key, found) in [
            (
                "7",
                "\"ada@example.invalid\"",
                TypedKey::GitUserName,
                ValueType::Integer,
            ),
            (
                "true",
                "\"ada@example.invalid\"",
                TypedKey::GitUserName,
                ValueType::Boolean,
            ),
            (
                "[\"Ada\"]",
                "\"ada@example.invalid\"",
                TypedKey::GitUserName,
                ValueType::Array,
            ),
            ("\"Ada\"", "1.5", TypedKey::GitUserEmail, ValueType::Float),
            (
                "\"Ada\"",
                "{ at = \"x\" }",
                TypedKey::GitUserEmail,
                ValueType::Table,
            ),
            ("7", "true", TypedKey::GitUserName, ValueType::Integer),
        ] {
            let text = format!("[git.user]\nname = {name}\nemail = {email}\n");

            let error = git_identity(&table(&text)).expect_err("a refusal");

            assert!(
                matches!(
                    &error,
                    ConfigError::ValueType { key: seen_key, owner: None, found: seen }
                        if *seen_key == key && *seen == found
                ),
                "{text}: {error:?}"
            );
        }
    }

    #[test]
    fn absent_keybindings_are_a_backslash_leader_a_second_and_no_binding() {
        assert_eq!(
            keybindings(&table("")),
            Keybindings {
                leader: "\\".to_owned(),
                timeoutlen: 1000,
                bindings: Vec::new(),
            }
        );
    }

    #[test]
    fn declared_keybindings_are_read_with_each_binding_as_written_in_document_order() {
        let read = keybindings(&table(
            "[keybindings]\nleader = \",\"\ntimeoutlen = 250\n\n[keybindings.bindings]\n\
             \"workspace.sync\" = \"<leader>s\"\n\"app.quit\" = \"q\"\ncount = 3\n",
        ));

        assert_eq!(
            read,
            Keybindings {
                leader: ",".to_owned(),
                timeoutlen: 250,
                bindings: vec![
                    (
                        "workspace.sync".to_owned(),
                        value("\"<leader>s\"", ValueType::String)
                    ),
                    ("app.quit".to_owned(), value("\"q\"", ValueType::String)),
                    ("count".to_owned(), value("3", ValueType::Integer)),
                ],
            }
        );
    }

    #[test]
    fn an_empty_leader_is_kept_and_a_timeoutlen_of_zero_or_below_is_kept() {
        let read = keybindings(&table("[keybindings]\nleader = \"\"\ntimeoutlen = -1\n"));

        assert_eq!(read.leader, "");
        assert_eq!(read.timeoutlen, -1);
    }

    #[test]
    fn a_leader_or_a_timeoutlen_of_the_wrong_type_falls_back_to_its_default() {
        for leader in ["7", "true", "1.5", "[\"x\"]", "{ key = \"x\" }"] {
            let read = keybindings(&table(&format!("[keybindings]\nleader = {leader}\n")));

            assert_eq!(read.leader, "\\", "{leader}");
        }
        for timeoutlen in ["true", "\"250\"", "250.0", "[250]"] {
            let read = keybindings(&table(&format!(
                "[keybindings]\ntimeoutlen = {timeoutlen}\n"
            )));

            assert_eq!(read.timeoutlen, 1000, "{timeoutlen}");
        }
    }

    #[test]
    fn the_dashboard_layout_reads_its_four_choices_and_defaults_to_auto() {
        let read = |text: &str| dashboard_layout(&table(text)).expect("a layout");

        assert_eq!(read(""), DashboardLayout::Auto);
        assert_eq!(read("[tui]\n"), DashboardLayout::Auto);
        assert_eq!(read("[tui.dashboard]\n"), DashboardLayout::Auto);
        for (choice, layout) in [
            ("auto", DashboardLayout::Auto),
            ("repos-as-columns", DashboardLayout::ReposAsColumns),
            ("repos-as-rows", DashboardLayout::ReposAsRows),
            ("list", DashboardLayout::List),
        ] {
            assert_eq!(
                read(&format!("[tui.dashboard]\nlayout = \"{choice}\"\n")),
                layout,
                "{choice}"
            );
        }
    }

    #[test]
    fn a_dashboard_layout_that_is_no_choice_is_refused_carrying_the_value_as_toml_text() {
        for (text, found) in [
            ("\"grid\"", ValueType::String),
            ("\"\"", ValueType::String),
            ("\"List\"", ValueType::String),
            ("\"repos_as_rows\"", ValueType::String),
            ("7", ValueType::Integer),
            ("false", ValueType::Boolean),
            ("[\"list\"]", ValueType::Array),
        ] {
            let error = dashboard_layout(&table(&format!("[tui.dashboard]\nlayout = {text}\n")))
                .expect_err("a refusal");

            assert!(
                matches!(
                    &error,
                    ConfigError::InvalidChoice { key: ChoiceKey::DashboardLayout, value: carried }
                        if *carried == value(text, found)
                ),
                "{text}: {error:?}"
            );
        }
    }

    #[test]
    fn a_capability_slot_takes_one_string_or_an_array_and_the_slots_keep_document_order() {
        let read = capabilities(&table(
            "[capabilities]\nservice = [\"tmux\", \"docker\"]\nagent = \"claude\"\n",
        ));

        assert_eq!(
            read,
            [
                ("service".to_owned(), owned(&["tmux", "docker"])),
                ("agent".to_owned(), owned(&["claude"])),
            ]
        );
    }

    #[test]
    fn a_provider_named_again_is_kept_once_and_items_that_are_no_provider_are_dropped() {
        let read = capabilities(&table(
            "[capabilities]\nservice = [\"tmux\", 7, \"\", \"docker\", \"tmux\", [\"deep\"]]\n",
        ));

        assert_eq!(read, [("service".to_owned(), owned(&["tmux", "docker"]))]);
    }

    #[test]
    fn a_capability_slot_left_with_no_provider_is_dropped() {
        for value in [
            "\"\"",
            "[]",
            "[\"\", 7]",
            "7",
            "true",
            "1.5",
            "{ name = \"tmux\" }",
        ] {
            let read = capabilities(&table(&format!(
                "[capabilities]\nservice = {value}\nagent = \"claude\"\n"
            )));

            assert_eq!(read, [("agent".to_owned(), owned(&["claude"]))], "{value}");
        }
        assert_eq!(capabilities(&table("")), []);
        assert_eq!(capabilities(&table("[capabilities]\n")), []);
    }

    #[test]
    fn a_removed_key_is_refused_whatever_its_value_and_nothing_else_is() {
        assert!(removed_keys(&table("service_prefix = \"winter\"\n")).is_ok());
        for value in ["\"tmux\"", "\"\"", "false", "[]", "{}"] {
            let orchestrator = removed_keys(&table(&format!("service_orchestrator = {value}\n")))
                .expect_err("a refusal");
            let prefix = removed_keys(&table(&format!("session_prefix = {value}\n")))
                .expect_err("a refusal");

            assert!(
                matches!(
                    orchestrator,
                    ConfigError::RemovedKey(RemovedKey::ServiceOrchestrator)
                ),
                "{value}: {orchestrator:?}"
            );
            assert!(
                matches!(prefix, ConfigError::RemovedKey(RemovedKey::SessionPrefix)),
                "{value}: {prefix:?}"
            );
        }
    }

    #[test]
    fn service_orchestrator_is_refused_before_session_prefix() {
        let error = removed_keys(&table(
            "session_prefix = \"old\"\nservice_orchestrator = \"tmux\"\n",
        ))
        .expect_err("a refusal");

        assert!(
            matches!(
                error,
                ConfigError::RemovedKey(RemovedKey::ServiceOrchestrator)
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_removed_key_in_the_local_file_alone_is_refused_and_the_message_names_its_replacement() {
        for (key, removed, replacement) in [
            (
                "service_orchestrator",
                RemovedKey::ServiceOrchestrator,
                "[capabilities] service",
            ),
            (
                "session_prefix",
                RemovedKey::SessionPrefix,
                "service_prefix",
            ),
        ] {
            let files = MemoryFiles::default()
                .with("/ws/.winter/config.toml", b"base_port = 5000\n")
                .with(
                    "/ws/.winter/config.local.toml",
                    format!("{key} = \"old\"\n").as_bytes(),
                );

            let error = load(&files, Path::new("/ws")).expect_err("a refusal");

            assert!(
                matches!(&error, ConfigError::RemovedKey(seen) if *seen == removed),
                "{key}: {error:?}"
            );
            let message = error.to_string();
            assert!(message.contains(key), "{message}");
            assert!(message.contains(replacement), "{message}");
        }
    }

    #[test]
    fn the_three_names_are_strings_that_are_not_empty_and_their_defaults_otherwise() {
        type Read = fn(&Table) -> String;
        let names = [
            ("service_prefix", service_prefix as Read, "winter"),
            ("prefix", prefix as Read, "ws"),
            ("skills_dir", skills_dir as Read, "skills"),
        ];

        for (key, read, default) in names {
            assert_eq!(read(&table("")), default, "{key}");
            assert_eq!(
                read(&table(&format!("{key} = \"mine\"\n"))),
                "mine",
                "{key}"
            );
            for value in [
                "\"\"",
                "7",
                "true",
                "1.5",
                "[\"mine\"]",
                "{ name = \"mine\" }",
            ] {
                assert_eq!(
                    read(&table(&format!("{key} = {value}\n"))),
                    default,
                    "{key} = {value}"
                );
            }
        }
    }

    #[test]
    fn doctor_is_any_string_and_unset_when_absent_or_of_another_type() {
        let read = |text: &str| doctor(&table(text));

        assert_eq!(read(""), None);
        assert_eq!(
            read("doctor = \"scripts/doctor.sh\"\n").as_deref(),
            Some("scripts/doctor.sh")
        );
        assert_eq!(read("doctor = \"\"\n").as_deref(), Some(""));
        for value in [
            "7",
            "true",
            "1.5",
            "[\"doctor.sh\"]",
            "{ path = \"doctor.sh\" }",
        ] {
            assert_eq!(read(&format!("doctor = {value}\n")), None, "{value}");
        }
    }

    #[test]
    fn lint_is_one_script_an_array_of_scripts_or_a_table_that_holds_them_under_scripts() {
        let read = |text: &str| lint_scripts(&table(text));

        assert_eq!(read(""), owned(&[]));
        assert_eq!(read("lint = \"lint.sh\"\n"), owned(&["lint.sh"]));
        assert_eq!(
            read("lint = [\"one.sh\", \"two.sh\", \"one.sh\"]\n"),
            owned(&["one.sh", "two.sh", "one.sh"])
        );
        assert_eq!(read("[lint]\nscripts = \"lint.sh\"\n"), owned(&["lint.sh"]));
        assert_eq!(
            read("[lint]\nscripts = [\"one.sh\", \"two.sh\"]\n\n[lint.ignore]\npaths = [\"a\"]\n"),
            owned(&["one.sh", "two.sh"])
        );
    }

    #[test]
    fn lint_drops_what_is_no_script_and_reads_a_value_of_another_type_as_none() {
        let read = |text: &str| lint_scripts(&table(text));

        assert_eq!(
            read("lint = [\"one.sh\", 7, \"\", [\"deep.sh\"], \"two.sh\"]\n"),
            owned(&["one.sh", "two.sh"])
        );
        assert_eq!(read("lint = \"\"\n"), owned(&[]));
        assert_eq!(read("[lint]\n"), owned(&[]));
        assert_eq!(read("[lint.ignore]\npaths = [\"a\"]\n"), owned(&[]));
        for value in ["7", "true", "1.5"] {
            assert_eq!(read(&format!("lint = {value}\n")), owned(&[]), "{value}");
            assert_eq!(
                read(&format!("[lint]\nscripts = {value}\n")),
                owned(&[]),
                "scripts = {value}"
            );
        }
        assert_eq!(
            read("[lint]\nscripts = { path = \"lint.sh\" }\n"),
            owned(&[])
        );
    }

    #[test]
    fn the_file_size_limits_are_integers_and_their_defaults_otherwise() {
        let read = |text: &str| file_size_limits(&table(text));
        let defaults = FileSizeLimits {
            injected_bytes: 6000,
            reference_bytes: 12000,
        };

        assert_eq!(read(""), defaults);
        assert_eq!(read("[core_checks]\n"), defaults);
        assert_eq!(read("[core_checks.file_size]\n"), defaults);
        assert_eq!(
            read("[core_checks.file_size]\ninjected_bytes = 100\nreference_bytes = 0\n"),
            FileSizeLimits {
                injected_bytes: 100,
                reference_bytes: 0,
            }
        );
        assert_eq!(
            read("[core_checks.file_size]\nreference_bytes = -5\n"),
            FileSizeLimits {
                injected_bytes: 6000,
                reference_bytes: -5,
            }
        );
        for value in ["true", "\"100\"", "100.0", "[100]"] {
            assert_eq!(
                read(&format!(
                    "[core_checks.file_size]\ninjected_bytes = {value}\nreference_bytes = {value}\n"
                )),
                defaults,
                "{value}"
            );
        }
    }

    #[test]
    fn the_space_is_its_root_and_its_kinds_as_written_in_document_order() {
        let read = space(&table(
            "[space]\nroot = \"~/artifacts\"\n\n[space.kinds]\nnotes = \"notes\"\nlogs = \"/var/log/ws\"\n",
        ));

        assert_eq!(
            read,
            Space {
                root: "~/artifacts".to_owned(),
                kinds: vec![
                    ("notes".to_owned(), "notes".to_owned()),
                    ("logs".to_owned(), "/var/log/ws".to_owned()),
                ],
            }
        );
    }

    #[test]
    fn a_space_root_that_is_no_string_or_is_empty_falls_back_to_the_winter_directory() {
        assert_eq!(space(&table("")).root, ".winter");
        assert_eq!(space(&table("[space]\n")).root, ".winter");
        for value in ["\"\"", "7", "true", "1.5", "[\"a\"]", "{ path = \"a\" }"] {
            assert_eq!(
                space(&table(&format!("[space]\nroot = {value}\n"))).root,
                ".winter",
                "{value}"
            );
        }
    }

    #[test]
    fn a_space_kind_that_is_no_string_or_is_empty_is_dropped() {
        let read = space(&table(
            "[space.kinds]\nnotes = 3\nlogs = \"logs\"\nplans = \"\"\ndrafts = [\"d\"]\nruns = true\n",
        ));

        assert_eq!(read.kinds, [("logs".to_owned(), "logs".to_owned())]);
        assert_eq!(space(&table("")).kinds, []);
    }

    #[test]
    fn provision_is_kept_as_written_and_is_empty_when_absent_or_no_table() {
        let declared = table(
            "[provision]\nodd = \"kept\"\n\n[[provision.setup]]\nrun = \"make\"\nunknown = 7\n",
        );

        assert_eq!(
            provision(&declared),
            RawTable(declared["provision"].as_table().expect("a table").clone())
        );
        assert_eq!(provision(&table("")), RawTable(Table::new()));
        for value in NOT_A_TABLE {
            assert_eq!(
                provision(&table(&format!("provision = {value}\n"))),
                RawTable(Table::new()),
                "{value}"
            );
        }
    }

    #[test]
    fn service_is_kept_as_written_and_is_empty_when_absent_or_no_array() {
        let declared = table("[[service]]\nname = \"web\"\nunknown = 7\n\n[[service]]\n");
        let mixed = table("service = [\"web\", 7]\n");

        assert_eq!(
            service(&declared),
            RawArray(declared["service"].as_array().expect("an array").clone())
        );
        assert_eq!(service(&declared).0.len(), 2);
        assert_eq!(
            service(&mixed),
            RawArray(mixed["service"].as_array().expect("an array").clone())
        );
        assert_eq!(service(&table("")), RawArray(Vec::new()));
        for value in [
            "\"web\"",
            "7",
            "1.5",
            "true",
            "{ name = \"web\" }",
            "1979-05-27",
        ] {
            assert_eq!(
                service(&table(&format!("service = {value}\n"))),
                RawArray(Vec::new()),
                "{value}"
            );
        }
    }

    #[test]
    fn a_table_or_a_sub_table_that_is_a_string_loads_as_the_defaults_of_its_keys() {
        let defaults = build(&Table::new()).expect("the defaults");

        for text in [
            "keybindings = \"none\"\n",
            "tui = \"none\"\n",
            "capabilities = \"none\"\n",
            "core_checks = \"none\"\n",
            "space = \"none\"\n",
            "[tui]\ndashboard = \"none\"\n",
            "[keybindings]\nbindings = \"none\"\n",
            "[core_checks]\nfile_size = \"none\"\n",
            "[space]\nkinds = \"none\"\n",
        ] {
            let files = MemoryFiles::default().with("/ws/.winter/config.toml", text.as_bytes());

            let workspace = load(&files, Path::new("/ws")).expect("a workspace");

            assert_eq!(workspace.config, defaults, "{text}");
        }
    }

    #[test]
    fn a_table_that_is_of_another_type_than_a_string_reads_as_absent_too() {
        let defaults = build(&Table::new()).expect("the defaults");

        for key in ["keybindings", "tui", "capabilities", "core_checks", "space"] {
            for value in NOT_A_TABLE {
                let config = build(&table(&format!("{key} = {value}\n"))).expect("a configuration");

                assert_eq!(config, defaults, "{key} = {value}");
            }
        }
    }

    #[test]
    fn an_invalid_layout_is_reported_before_a_removed_key_and_that_before_adopt_extensions() {
        let layout = build(&table(
            "session_prefix = \"old\"\n\n[tui.dashboard]\nlayout = \"grid\"\n",
        ))
        .expect_err("a refusal");
        let removed = build(&table(
            "session_prefix = \"old\"\nadopt_extensions = \"some\"\n",
        ))
        .expect_err("a refusal");

        assert!(
            matches!(
                layout,
                ConfigError::InvalidChoice {
                    key: ChoiceKey::DashboardLayout,
                    ..
                }
            ),
            "{layout:?}"
        );
        assert!(
            matches!(removed, ConfigError::RemovedKey(RemovedKey::SessionPrefix)),
            "{removed:?}"
        );
    }

    #[test]
    fn a_git_that_is_no_table_is_reported_before_an_invalid_layout() {
        let error = build(&table(
            "git = \"x\"\n\n[tui.dashboard]\nlayout = \"grid\"\n",
        ))
        .expect_err("a refusal");

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

    #[test]
    fn adopt_extensions_reads_its_three_choices_and_defaults_to_winter() {
        let read = |text: &str| adopt_extensions(&table(text)).expect("a choice");

        assert_eq!(read(""), AdoptExtensions::Winter);
        assert_eq!(read("adopt_extensions = \"none\"\n"), AdoptExtensions::None);
        assert_eq!(
            read("adopt_extensions = \"winter\"\n"),
            AdoptExtensions::Winter
        );
        assert_eq!(read("adopt_extensions = \"all\"\n"), AdoptExtensions::All);
    }

    #[test]
    fn an_adopt_extensions_that_is_no_choice_is_refused_carrying_the_value_as_toml_text() {
        for (value, found) in [
            ("\"some\"", ValueType::String),
            ("\"\"", ValueType::String),
            ("\"All\"", ValueType::String),
            ("true", ValueType::Boolean),
            ("[\"all\"]", ValueType::Array),
        ] {
            let error = adopt_extensions(&table(&format!("adopt_extensions = {value}\n")))
                .expect_err("a refusal");

            assert!(
                matches!(
                    &error,
                    ConfigError::InvalidChoice { key: ChoiceKey::AdoptExtensions, value: carried }
                        if *carried == ConfigValue { text: value.to_owned(), found }
                ),
                "{value}: {error:?}"
            );
        }
    }

    #[test]
    fn git_excludes_is_an_array_of_strings_and_empty_when_absent() {
        let read = |text: &str| git_excludes(&table(text)).expect("the excludes");

        assert_eq!(read(""), [] as [&str; 0]);
        assert_eq!(read("git_excludes = []\n"), [] as [&str; 0]);
        assert_eq!(
            read("git_excludes = [\"target/\", \"*.log\"]\n"),
            ["target/", "*.log"]
        );
    }

    #[test]
    fn a_git_excludes_that_is_not_an_array_of_strings_is_a_value_type_error() {
        for (value, found) in [
            ("\"abc\"", ValueType::String),
            ("\"\"", ValueType::String),
            ("false", ValueType::Boolean),
            ("0", ValueType::Integer),
            ("{}", ValueType::Table),
            ("[\"a\", 7]", ValueType::Integer),
        ] {
            let error =
                git_excludes(&table(&format!("git_excludes = {value}\n"))).expect_err("a refusal");

            assert!(
                matches!(
                    &error,
                    ConfigError::ValueType { key: TypedKey::GitExcludes, owner: None, found: seen }
                        if *seen == found
                ),
                "{value}: {error:?}"
            );
        }
    }

    #[test]
    fn main_branch_is_a_string_and_main_when_absent_or_an_empty_form() {
        let read = |text: &str| main_branch(&table(text)).expect("a branch");

        assert_eq!(read("main_branch = \"trunk\"\n"), "trunk");
        assert_eq!(read(""), "main");
        for value in ["\"\"", "0", "false", "0.0", "[]", "{}"] {
            assert_eq!(read(&format!("main_branch = {value}\n")), "main", "{value}");
        }
    }

    #[test]
    fn a_main_branch_of_another_type_that_is_no_empty_form_is_a_value_type_error() {
        for (value, found) in [
            ("7", ValueType::Integer),
            ("true", ValueType::Boolean),
            ("1.5", ValueType::Float),
            ("[\"main\"]", ValueType::Array),
            ("{ name = \"main\" }", ValueType::Table),
        ] {
            let error =
                main_branch(&table(&format!("main_branch = {value}\n"))).expect_err("a refusal");

            assert!(
                matches!(
                    &error,
                    ConfigError::ValueType { key: TypedKey::MainBranch, owner: None, found: seen }
                        if *seen == found
                ),
                "{value}: {error:?}"
            );
        }
    }

    #[test]
    fn the_branch_read_without_a_refusal_is_the_string_or_main() {
        let read = |text: &str| main_branch_or_default(&table(text)).to_owned();

        assert_eq!(read("main_branch = \"trunk\"\n"), "trunk");
        for text in [
            "",
            "main_branch = \"\"\n",
            "main_branch = 0\n",
            "main_branch = 7\n",
        ] {
            assert_eq!(read(text), "main", "{text}");
        }
    }
}
