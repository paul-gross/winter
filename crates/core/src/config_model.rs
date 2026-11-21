//! The configuration of a workspace as typed values, and its build from the merged table of the two
//! configuration files. What the children share to read one value is here.

mod agents;
mod env_bands;
mod ports;
mod repositories;
mod settings;

use std::path::PathBuf;

use toml::{Table, Value};

use crate::termination::{ConfigError, ConfigValue, RepoKind, RepoRef, TypedKey, ValueType};

/// The two kinds of repository, each with the top-level key of its array of entries, in the order they are
/// read.
pub(crate) const REPOSITORY_ARRAYS: [(RepoKind, &str); 2] = [
    (RepoKind::Project, "project_repository"),
    (RepoKind::Standalone, "standalone_repository"),
];

/// A workspace an invocation works in: its root and its validated configuration.
#[derive(Debug, Clone, PartialEq)]
pub struct Workspace {
    pub root: PathBuf,
    pub config: WorkspaceConfig,
}

/// The configuration of a workspace: the shared file with the local file laid over it, validated.
#[derive(Debug, Clone, PartialEq)]
pub struct WorkspaceConfig {
    pub service_prefix: String,
    pub main_branch: String,
    pub git_excludes: Vec<String>,
    pub ports: Ports,
    pub adopt_extensions: AdoptExtensions,
    pub prefix: String,
    pub skills_dir: String,
    pub doctor: Option<String>,
    pub lint_scripts: Vec<String>,
    pub git_identity: Option<GitIdentity>,
    /// Each slot with its providers.
    pub capabilities: Vec<(String, Vec<String>)>,
    pub keybindings: Keybindings,
    pub dashboard_layout: DashboardLayout,
    pub file_size_limits: FileSizeLimits,
    pub env: EnvBands,
    pub space: Space,
    pub model_tiers: Vec<ModelTier>,
    /// Each agent with its override.
    pub agent_model_overrides: Vec<(String, AgentOverride)>,
    /// The entries of the shared file, then those of the local file.
    pub project_repositories: Vec<ProjectRepository>,
    /// The entries of the shared file, then those of the local file.
    pub standalone_repositories: Vec<StandaloneRepository>,
    /// `[provision]` as written; not validated when the configuration is loaded.
    pub provision: RawTable,
    /// `[[service]]` as written; not validated when the configuration is loaded.
    pub service: RawArray,
}

/// The port layout and the env index layout.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Ports {
    pub base_port: i64,
    pub ports_per_env: i64,
    pub env_aliases: Vec<String>,
    /// At least `env_aliases.len() + 2`.
    pub envs_per_workspace: i64,
}

/// Which extensions a workspace adopts.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AdoptExtensions {
    None,
    Winter,
    All,
}

/// How the dashboard lays out its repositories.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DashboardLayout {
    Auto,
    ReposAsColumns,
    ReposAsRows,
    List,
}

/// The git identity of the workspace.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GitIdentity {
    pub name: String,
    pub email: String,
}

/// The key bindings of the dashboard. A binding keeps its value as written.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Keybindings {
    pub leader: String,
    pub timeoutlen: i64,
    pub bindings: Vec<(String, ConfigValue)>,
}

/// The sizes, in bytes, the file size check allows.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileSizeLimits {
    pub injected_bytes: i64,
    pub reference_bytes: i64,
}

/// Where artifacts are kept: the root and the directory of each kind, as written.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Space {
    pub root: String,
    pub kinds: Vec<(String, String)>,
}

/// The environment variables of `[env]`, by band.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnvBands {
    pub workspace: Vec<EnvVar>,
    pub feature: Vec<EnvVar>,
    /// Each band of one env, with its name.
    pub named: Vec<(String, Vec<EnvVar>)>,
}

/// One environment variable of a band.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnvVar {
    pub key: String,
    pub value: EnvValue,
}

/// The value of an environment variable: a literal, or a command that yields it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EnvValue {
    Literal(String),
    Command(EnvCommand),
}

/// A command entry of a band.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EnvCommand {
    pub command: String,
    pub format: EnvFormat,
    pub shell: bool,
    pub exports: Option<Vec<String>>,
}

/// How the output of a command entry is read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EnvFormat {
    Raw,
    Dotenv,
    Json,
}

/// One model tier: its label and the model id of each vendor that has one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModelTier {
    pub label: String,
    pub claude: Option<String>,
    pub codex: Option<String>,
    pub opencode: Option<String>,
}

/// The model override of one agent: a tier by its label, or a model for each vendor named.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AgentOverride {
    Tier(String),
    Vendors(Vec<(Vendor, VendorModel)>),
}

/// A vendor of agent models.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Vendor {
    Claude,
    Codex,
    Opencode,
}

/// The model and the effort an override gives one vendor.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VendorModel {
    pub model: Option<String>,
    pub effort: Option<String>,
}

/// What a project and a standalone repository entry have in common.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RepositoryCommon {
    pub name: String,
    pub url: Option<String>,
    pub main_branch: String,
    pub git_excludes: Vec<String>,
    pub cmd: Vec<String>,
    pub extension: bool,
    pub load: Option<LoadMode>,
    pub entry: Option<Vec<String>>,
}

/// When the extension of a repository is loaded.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LoadMode {
    Eager,
    Lazy,
    None,
}

/// A `[[project_repository]]` entry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProjectRepository {
    pub common: RepositoryCommon,
    pub pinned: bool,
    pub nested: Option<Nested>,
}

/// What a project repository that is a nested workspace declares.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Nested {
    pub envs: Option<i64>,
    pub inherit_local: Option<Vec<String>>,
}

/// A `[[standalone_repository]]` entry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StandaloneRepository {
    pub common: RepositoryCommon,
    pub reference: Option<String>,
    pub path: String,
    pub prefix: Option<String>,
    pub config_dir: String,
}

/// A TOML table kept as it was parsed.
#[derive(Debug, Clone, PartialEq)]
pub struct RawTable(pub(crate) Table);

/// The values of a TOML array, kept as they were parsed.
#[derive(Debug, Clone, PartialEq)]
pub struct RawArray(pub(crate) Vec<Value>);

/// The typed configuration of the merged table.
///
/// The parts are built in a fixed order, and the first refusal met ends the build, so one configuration always
/// gives the same error: the repository entries; the git identity, the key bindings, and the dashboard layout;
/// the capabilities; the removed keys; `adopt_extensions`; `envs_per_workspace` against `env_aliases`; the
/// workspace prefix against each standalone prefix; the file size limits, the env bands, the space, the model
/// tiers, and the agent overrides; and last the types of `git_excludes` and of `main_branch`.
pub(crate) fn build(merged: &Table) -> Result<WorkspaceConfig, ConfigError> {
    // A repository entry without a branch of its own takes the one of the workspace, so that is read first; a
    // value of a type it does not accept is still refused last.
    let (project_repositories, standalone_repositories) =
        repositories::repositories(merged, settings::main_branch_or_default(merged))?;

    let git_identity = settings::git_identity(merged)?;
    let keybindings = settings::keybindings(merged);
    let dashboard_layout = settings::dashboard_layout(merged)?;

    let capabilities = settings::capabilities(merged);

    settings::removed_keys(merged)?;

    let adopt_extensions = settings::adopt_extensions(merged)?;

    let ports = ports::ports(merged)?;

    let prefix = settings::prefix(merged);
    repositories::prefix_collision(&prefix, &standalone_repositories)?;

    let file_size_limits = settings::file_size_limits(merged);
    let env = env_bands::env_bands(merged)?;
    let space = settings::space(merged);
    let model_tiers = agents::model_tiers(merged)?;
    let agent_model_overrides = agents::agent_model_overrides(merged, &model_tiers)?;

    let git_excludes = settings::git_excludes(merged)?;
    let main_branch = settings::main_branch(merged)?;

    Ok(WorkspaceConfig {
        service_prefix: settings::service_prefix(merged),
        main_branch,
        git_excludes,
        ports,
        adopt_extensions,
        prefix,
        skills_dir: settings::skills_dir(merged),
        doctor: settings::doctor(merged),
        lint_scripts: settings::lint_scripts(merged),
        git_identity,
        capabilities,
        keybindings,
        dashboard_layout,
        file_size_limits,
        env,
        space,
        model_tiers,
        agent_model_overrides,
        project_repositories,
        standalone_repositories,
        provision: settings::provision(merged),
        service: settings::service(merged),
    })
}

/// The type of `value`.
pub(crate) fn value_type(value: &Value) -> ValueType {
    match value {
        Value::String(_) => ValueType::String,
        Value::Integer(_) => ValueType::Integer,
        Value::Float(_) => ValueType::Float,
        Value::Boolean(_) => ValueType::Boolean,
        Value::Datetime(_) => ValueType::Datetime,
        Value::Array(_) => ValueType::Array,
        Value::Table(_) => ValueType::Table,
    }
}

/// `value` as an error carries it: its TOML text and its type.
pub(crate) fn config_value(value: &Value) -> ConfigValue {
    ConfigValue {
        text: value.to_string(),
        found: value_type(value),
    }
}

/// Whether `value` is an empty form: `false`, a zero of either number type, an empty string, an empty array,
/// or an empty table.
pub(crate) fn is_empty_form(value: &Value) -> bool {
    match value {
        Value::String(text) => text.is_empty(),
        Value::Integer(number) => *number == 0,
        Value::Float(number) => *number == 0.0,
        Value::Boolean(flag) => !flag,
        Value::Datetime(_) => false,
        Value::Array(items) => items.is_empty(),
        Value::Table(table) => table.is_empty(),
    }
}

/// The string `value` is. A value of another type is [`ConfigError::ValueType`] for `key`, of the repository
/// `owner` when the key is a field of one.
fn string<'v>(
    value: &'v Value,
    key: TypedKey,
    owner: Option<&RepoRef>,
) -> Result<&'v str, ConfigError> {
    value.as_str().ok_or_else(|| wrong_type(value, key, owner))
}

/// The strings of the array `value` is. A value that is not an array is [`ConfigError::ValueType`] for `key`
/// with the type of the value, and an element that is not a string is one with the type of the first such
/// element; `owner` is the repository that holds the key, when the key is a field of one.
fn strings(
    value: &Value,
    key: TypedKey,
    owner: Option<&RepoRef>,
) -> Result<Vec<String>, ConfigError> {
    let Value::Array(items) = value else {
        return Err(wrong_type(value, key, owner));
    };
    items
        .iter()
        .map(|item| string(item, key, owner).map(str::to_owned))
        .collect()
}

/// The integer `table` holds under `key`, or `default` when it holds none there: the key is absent, or its
/// value is of another type, a boolean included.
fn integer_or(table: &Table, key: &str, default: i64) -> i64 {
    table
        .get(key)
        .and_then(Value::as_integer)
        .unwrap_or(default)
}

/// The list of strings `value` stands for, where a key takes one string or an array of them: a string is a
/// list of that one string, an array is its items, and any other value is an empty list. An item that is not
/// a string, or is empty, is dropped. Nothing is refused.
fn string_list(value: &Value) -> Vec<String> {
    let items = match value {
        Value::String(_) => std::slice::from_ref(value),
        Value::Array(items) => items.as_slice(),
        _ => &[],
    };
    items
        .iter()
        .filter_map(Value::as_str)
        .filter(|item| !item.is_empty())
        .map(str::to_owned)
        .collect()
}

/// The refusal of `value` for `key`, which carries the type of the value.
fn wrong_type(value: &Value, key: TypedKey, owner: Option<&RepoRef>) -> ConfigError {
    ConfigError::ValueType {
        key,
        owner: owner.cloned(),
        found: value_type(value),
    }
}

/// The table `text` parses to.
#[cfg(test)]
pub(crate) fn table(text: &str) -> Table {
    text.parse().expect("a TOML document")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::termination::ChoiceKey;

    /// The value of the key `v` in `text`.
    fn value(text: &str) -> Value {
        table(text).remove("v").expect("the key v")
    }

    #[test]
    fn the_text_of_a_value_is_its_toml_text_for_each_type() {
        let cases = [
            ("v = \"some\"", "\"some\"", ValueType::String),
            ("v = 'a \"b\"'", "'a \"b\"'", ValueType::String),
            ("v = 7", "7", ValueType::Integer),
            ("v = 0x10", "16", ValueType::Integer),
            ("v = -3", "-3", ValueType::Integer),
            ("v = 1.5", "1.5", ValueType::Float),
            ("v = 2.0", "2.0", ValueType::Float),
            ("v = true", "true", ValueType::Boolean),
            (
                "v = 1979-05-27T07:32:00Z",
                "1979-05-27T07:32:00Z",
                ValueType::Datetime,
            ),
            ("v = [1, \"a\"]", "[1, \"a\"]", ValueType::Array),
            (
                "v = { a = 1, b = \"x\" }",
                "{ a = 1, b = \"x\" }",
                ValueType::Table,
            ),
        ];

        for (text, expected, found) in cases {
            assert_eq!(
                config_value(&value(text)),
                ConfigValue {
                    text: expected.to_owned(),
                    found
                },
                "{text}"
            );
        }
    }

    #[test]
    fn the_empty_forms_are_false_zero_and_the_empty_string_array_and_table() {
        for text in ["false", "0", "0.0", "-0.0", "\"\"", "[]", "{}"] {
            assert!(is_empty_form(&value(&format!("v = {text}"))), "{text}");
        }
        for text in [
            "true",
            "1",
            "-1",
            "0.5",
            "\" \"",
            "[0]",
            "{ a = 0 }",
            "1979-05-27",
        ] {
            assert!(!is_empty_form(&value(&format!("v = {text}"))), "{text}");
        }
    }

    #[test]
    fn a_string_is_read_and_a_value_of_another_type_is_refused_with_its_key_and_owner() {
        let owner = RepoRef {
            kind: RepoKind::Standalone,
            label: "tools".to_owned(),
        };

        assert_eq!(
            string(&value("v = \"\""), TypedKey::Ref, None).expect("a string"),
            ""
        );
        let error = string(&value("v = 0"), TypedKey::Ref, Some(&owner)).expect_err("a refusal");
        assert!(
            matches!(
                &error,
                ConfigError::ValueType { key: TypedKey::Ref, owner: Some(held), found: ValueType::Integer }
                    if *held == owner
            ),
            "{error:?}"
        );
    }

    #[test]
    fn an_array_of_strings_is_read_and_anything_else_is_refused_with_the_type_at_fault() {
        let read = |text: &str| strings(&value(text), TypedKey::Cmd, None);

        assert_eq!(read("v = []").expect("strings"), [] as [&str; 0]);
        assert_eq!(read("v = [\"a\", \"\"]").expect("strings"), ["a", ""]);
        for (text, found) in [
            ("v = \"a\"", ValueType::String),
            ("v = {}", ValueType::Table),
            ("v = [\"a\", 1.5, true]", ValueType::Float),
            ("v = [[\"a\"]]", ValueType::Array),
        ] {
            let error = read(text).expect_err("a refusal");
            assert!(
                matches!(
                    &error,
                    ConfigError::ValueType { key: TypedKey::Cmd, owner: None, found: seen } if *seen == found
                ),
                "{text}: {error:?}"
            );
        }
    }

    #[test]
    fn an_integer_is_read_and_an_absent_key_or_a_value_of_another_type_is_the_default() {
        let read = |text: &str| integer_or(&table(text), "v", 48);

        assert_eq!(read("v = 12"), 12);
        assert_eq!(read("v = 0"), 0);
        assert_eq!(read("v = -3"), -3);
        assert_eq!(read("v = 5000000000"), 5_000_000_000);
        for text in ["", "v = true", "v = \"12\"", "v = 12.0", "v = [12]"] {
            assert_eq!(read(text), 48, "{text}");
        }
    }

    #[test]
    fn a_string_or_an_array_is_a_list_of_its_non_empty_strings_and_any_other_value_is_empty() {
        let read = |text: &str| string_list(&value(text));

        assert_eq!(read("v = \"solo\""), ["solo"]);
        assert_eq!(read("v = \"\""), [] as [&str; 0]);
        assert_eq!(
            read("v = [\"one\", \"two\", \"one\"]"),
            ["one", "two", "one"]
        );
        assert_eq!(
            read("v = [\"one\", 2, \"\", true, [\"deep\"], \"two\"]"),
            ["one", "two"]
        );
        for text in ["v = 7", "v = true", "v = 1.5", "v = { a = \"one\" }"] {
            assert_eq!(read(text), [] as [&str; 0], "{text}");
        }
    }

    #[test]
    fn a_table_that_declares_nothing_builds_the_defaults() {
        let config = build(&Table::new()).expect("a configuration");

        assert_eq!(config.service_prefix, "winter");
        assert_eq!(config.main_branch, "main");
        assert_eq!(config.git_excludes, [] as [&str; 0]);
        assert_eq!(config.ports.base_port, 4000);
        assert_eq!(config.ports.ports_per_env, 20);
        assert_eq!(config.ports.env_aliases.len(), 10);
        assert_eq!(config.ports.envs_per_workspace, 48);
        assert_eq!(config.adopt_extensions, AdoptExtensions::Winter);
        assert_eq!(config.prefix, "ws");
        assert_eq!(config.skills_dir, "skills");
        assert_eq!(config.doctor, None);
        assert_eq!(config.git_identity, None);
        assert_eq!(config.keybindings.leader, "\\");
        assert_eq!(config.keybindings.timeoutlen, 1000);
        assert_eq!(config.dashboard_layout, DashboardLayout::Auto);
        assert_eq!(config.file_size_limits.injected_bytes, 6000);
        assert_eq!(config.file_size_limits.reference_bytes, 12000);
        assert_eq!(config.space.root, ".winter");
        assert!(config.project_repositories.is_empty());
        assert!(config.standalone_repositories.is_empty());
    }

    #[test]
    fn an_invalid_adopt_extensions_is_reported_before_a_slot_count_that_is_too_small() {
        let error = build(&table(
            "adopt_extensions = \"some\"\nenvs_per_workspace = 3\n",
        ))
        .expect_err("a refusal");

        assert!(
            matches!(
                error,
                ConfigError::InvalidChoice {
                    key: ChoiceKey::AdoptExtensions,
                    ..
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_slot_count_that_is_too_small_is_reported_before_the_type_of_git_excludes() {
        let error =
            build(&table("envs_per_workspace = 3\ngit_excludes = \"a\"\n")).expect_err("a refusal");

        assert!(
            matches!(error, ConfigError::EnvsPerWorkspaceTooSmall { .. }),
            "{error:?}"
        );
    }

    #[test]
    fn the_type_of_git_excludes_is_reported_before_the_type_of_main_branch() {
        let error =
            build(&table("main_branch = 7\ngit_excludes = \"a\"\n")).expect_err("a refusal");

        assert!(
            matches!(
                error,
                ConfigError::ValueType {
                    key: TypedKey::GitExcludes,
                    ..
                }
            ),
            "{error:?}"
        );
    }
}
