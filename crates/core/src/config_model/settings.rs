//! The top-level keys and the small tables no other child of the model owns.
//!
//! Three keys are read: `adopt_extensions`, `git_excludes`, and `main_branch`. Every other function here
//! answers the default of its key, whatever the table holds.

use toml::{Table, Value};

use super::{
    AdoptExtensions, DashboardLayout, FileSizeLimits, GitIdentity, Keybindings, RawArray, RawTable,
    Space, config_value, is_empty_form, string, strings,
};
use crate::termination::{ChoiceKey, ConfigError, TypedKey};

const MAIN_BRANCH: &str = "main_branch";
const DEFAULT_MAIN_BRANCH: &str = "main";

/// The git identity. Not read yet: none.
pub(super) fn git_identity(_merged: &Table) -> Result<Option<GitIdentity>, ConfigError> {
    Ok(None)
}

/// The key bindings. Not read yet: the defaults.
pub(super) fn keybindings(_merged: &Table) -> Keybindings {
    Keybindings {
        leader: "\\".to_owned(),
        timeoutlen: 1000,
        bindings: Vec::new(),
    }
}

/// The dashboard layout. Not read yet: the default.
pub(super) fn dashboard_layout(_merged: &Table) -> Result<DashboardLayout, ConfigError> {
    Ok(DashboardLayout::Auto)
}

/// The capability slots. Not read yet: none.
pub(super) fn capabilities(_merged: &Table) -> Vec<(String, Vec<String>)> {
    Vec::new()
}

/// Refuses a key that was removed. Not checked yet: nothing is refused.
pub(super) fn removed_keys(_merged: &Table) -> Result<(), ConfigError> {
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

/// The workspace prefix. Not read yet: the default.
pub(super) fn prefix(_merged: &Table) -> String {
    "ws".to_owned()
}

/// The limits of the file size check. Not read yet: the defaults.
pub(super) fn file_size_limits(_merged: &Table) -> FileSizeLimits {
    FileSizeLimits {
        injected_bytes: 6000,
        reference_bytes: 12000,
    }
}

/// The artifact space. Not read yet: the defaults.
pub(super) fn space(_merged: &Table) -> Space {
    Space {
        root: ".winter".to_owned(),
        kinds: Vec::new(),
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

/// The service prefix. Not read yet: the default.
pub(super) fn service_prefix(_merged: &Table) -> String {
    "winter".to_owned()
}

/// The skills directory. Not read yet: the default.
pub(super) fn skills_dir(_merged: &Table) -> String {
    "skills".to_owned()
}

/// The doctor script. Not read yet: none.
pub(super) fn doctor(_merged: &Table) -> Option<String> {
    None
}

/// The lint scripts. Not read yet: none.
pub(super) fn lint_scripts(_merged: &Table) -> Vec<String> {
    Vec::new()
}

/// `[provision]` as written. Not read yet: an empty table.
pub(super) fn provision(_merged: &Table) -> RawTable {
    RawTable(Table::new())
}

/// `[[service]]` as written. Not read yet: no entry.
pub(super) fn service(_merged: &Table) -> RawArray {
    RawArray(Vec::new())
}

#[cfg(test)]
mod tests {
    use super::super::table;
    use super::*;
    use crate::termination::{ConfigValue, ValueType};

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
