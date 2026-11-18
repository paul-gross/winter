//! The workspace configuration: the shared and the machine-local file read, layered into one document, and
//! the keys the env index depends on taken from it.
//!
//! The load reads both files, refuses what the layering cannot read, merges the local document over the
//! shared one, and then applies the refusals of the merged keys in a fixed order. Only
//! `env_aliases` and `envs_per_workspace` are kept; every other key is checked and dropped, and nothing is
//! written. The document keeps the order of the files, so the first fault met is the first one written.

use std::collections::{BTreeSet, HashMap};
use std::path::{Component, Path};

use toml::{Table, Value};

use crate::config_edit::name_from_url;
use crate::files::ReadFiles;
use crate::termination::{
    AgentOverrideFault, ChoiceKey, ConfigError, ConfigValue, EntryIdent, EnvVarFault, FileCause,
    ModelTierFault, NestedExcludedKey, NestedOnlyKey, PathField, RemovedKey, RepoKind, RepoRef,
    RepositoryFault, TomlParseError, TypedKey, ValueType,
};
use crate::workspace_files::{CONFIG_FILE, LOCAL_CONFIG_FILE};

const ENV_ALIASES: &str = "env_aliases";
const ENVS_PER_WORKSPACE: &str = "envs_per_workspace";
const AGENT_MODEL_OVERRIDES: &str = "agent_model_overrides";
const MODEL_TIERS: &str = "model_tiers";
const ADOPT_EXTENSIONS: &str = "adopt_extensions";
const MAIN_BRANCH: &str = "main_branch";
const GIT_EXCLUDES: &str = "git_excludes";
const PREFIX: &str = "prefix";
const ENV: &str = "env";
const ENV_VARS: &str = "vars";

/// The two env bands with a name of their own; every other `[env]` sub-table is a band of the env it names.
const RESERVED_BANDS: [&str; 2] = ["workspace", "feature"];
/// The fields of an env command entry, which is closed.
const COMMAND_ENTRY_FIELDS: [&str; 4] = ["command", "format", "shell", "exports"];
const COMMAND_FORMATS: [&str; 3] = ["raw", "dotenv", "json"];
/// The prefix of the variables winter manages, which no command entry may be declared under.
const MANAGED_PREFIX: &str = "WINTER_";

/// The vendors a model id is given for.
const VENDORS: [&str; 3] = ["claude", "codex", "opencode"];
/// The labels of the built-in model tiers, sorted. Their cells are overlaid by `[model_tiers]` and read only
/// when an agent's model is resolved, so none is kept.
const BUILTIN_TIERS: [&str; 4] = ["fable", "haiku", "opus", "sonnet"];
/// The keys of a vendor profile in `agent_model_overrides`, which is closed.
const PROFILE_KEYS: [&str; 2] = ["model", "effort"];

const DEFAULT_PREFIX: &str = "ws";
const LOAD_CHOICES: [&str; 3] = ["eager", "lazy", "none"];

const ADOPT_CHOICES: [&str; 3] = ["none", "winter", "all"];
const LAYOUT_CHOICES: [&str; 4] = ["auto", "repos-as-columns", "repos-as-rows", "list"];

const DEFAULT_ENV_ALIASES: [&str; 10] = [
    "alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa",
];
const DEFAULT_ENVS_PER_WORKSPACE: i64 = 48;

/// What the configuration of a workspace says about env indexes.
#[derive(Debug, PartialEq, Eq)]
pub(crate) struct Config {
    pub env_aliases: Vec<String>,
    /// At least `env_aliases.len() + 2`.
    pub envs_per_workspace: i64,
}

/// Loads the configuration of the workspace at `root`.
///
/// The shared file is read first, then the local one; a missing file is an empty document, and a file that
/// exists and cannot be read, or is not valid TOML, is [`ConfigError::FileRead`]. An `agent_model_overrides`
/// that is not a table in either file is refused, and then the two documents are merged. The merged
/// document is then checked, in this order: the repository arrays and their entries, `[git]`,
/// `git.user`, and the identity, then `tui.dashboard.layout`, the removed keys, `adopt_extensions`,
/// `envs_per_workspace` against `env_aliases`, the workspace `prefix` against the standalone prefixes,
/// the env bands, `[model_tiers]`, `[agent_model_overrides]`, `main_branch`, and `git_excludes`. The first
/// refusal ends the load.
pub(crate) fn load(files: &dyn ReadFiles, root: &Path) -> Result<Config, ConfigError> {
    let shared_path = root.join(CONFIG_FILE);
    let local_path = root.join(LOCAL_CONFIG_FILE);
    let shared = document(files, &shared_path)?;
    let local = document(files, &local_path)?;
    overrides_are_a_table(&shared, &shared_path)?;
    overrides_are_a_table(&local, &local_path)?;
    let merged = merge(shared, local, &shared_path)?;
    let standalone_prefixes = repository_entries_are_valid(&merged)?;
    git_identity_is_readable(&merged)?;
    dashboard_layout_is_valid(&merged)?;
    removed_keys_are_absent(&merged)?;
    adopt_extensions_is_valid(&merged)?;

    let env_aliases = match merged.get(ENV_ALIASES) {
        Some(value) => aliases(value),
        None => DEFAULT_ENV_ALIASES.map(str::to_owned).to_vec(),
    };
    let envs_per_workspace = match merged.get(ENVS_PER_WORKSPACE) {
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
    prefix_differs_from_the_standalone_prefixes(&merged, &standalone_prefixes)?;
    env_bands_are_valid(&merged)?;
    // `space` holds no refusal: a value it cannot read falls back.
    let tiers = model_tiers_are_valid(&merged)?;
    agent_overrides_are_valid(&merged, &tiers)?;
    main_branch_is_a_string(&merged)?;
    git_excludes_is_a_list_of_strings(&merged)?;
    Ok(Config {
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

/// Refuses an `agent_model_overrides` of `document`, the content of the file at `path`, that is not a table.
fn overrides_are_a_table(document: &Table, path: &Path) -> Result<(), ConfigError> {
    match document.get(AGENT_MODEL_OVERRIDES) {
        None | Some(Value::Table(_)) => Ok(()),
        Some(other) => Err(ConfigError::AgentOverridesNotTable {
            path: path.to_path_buf(),
            found: ValueType::of(other),
        }),
    }
}

/// Refuses a `[git]`, a `git.user`, or an identity the reference tool cannot read: a `[git]` or `git.user`
/// that is not a table, unless it is one of the empty forms, and a `git.user` whose `name` and `email` are
/// both non-empty with either not a string. With either empty there is no identity and nothing to read.
fn git_identity_is_readable(document: &Table) -> Result<(), ConfigError> {
    let Some(git) = table_or_empty(document.get("git"), TypedKey::Git)? else {
        return Ok(());
    };
    let Some(user) = table_or_empty(git.get("user"), TypedKey::GitUser)? else {
        return Ok(());
    };
    match (user.get("name"), user.get("email")) {
        (Some(name), Some(email)) if !reads_empty(name) && !reads_empty(email) => {
            for (key, value) in [
                (TypedKey::GitUserName, name),
                (TypedKey::GitUserEmail, email),
            ] {
                if !value.is_str() {
                    return Err(value_type(key, value));
                }
            }
            Ok(())
        }
        _ => Ok(()),
    }
}

/// The table `value` is, `None` when it is absent or one of the empty forms, and the refusal of `key`
/// for any other value.
fn table_or_empty(value: Option<&Value>, key: TypedKey) -> Result<Option<&Table>, ConfigError> {
    match value {
        None => Ok(None),
        Some(value) if reads_empty(value) => Ok(None),
        Some(Value::Table(table)) => Ok(Some(table)),
        Some(other) => Err(value_type(key, other)),
    }
}

fn value_type(key: TypedKey, value: &Value) -> ConfigError {
    ConfigError::ValueType {
        key,
        owner: None,
        path: None,
        found: ValueType::of(value),
    }
}

/// Refuses a `tui.dashboard.layout` that is not one of the layouts. A `tui` or `tui.dashboard` that is not a
/// table holds no layout.
fn dashboard_layout_is_valid(document: &Table) -> Result<(), ConfigError> {
    let layout = document
        .get("tui")
        .and_then(Value::as_table)
        .and_then(|tui| tui.get("dashboard"))
        .and_then(Value::as_table)
        .and_then(|dashboard| dashboard.get("layout"));
    choice(ChoiceKey::DashboardLayout, layout, &LAYOUT_CHOICES)
}

/// Refuses an `adopt_extensions` that is not `none`, `winter`, or `all`.
fn adopt_extensions_is_valid(document: &Table) -> Result<(), ConfigError> {
    choice(
        ChoiceKey::AdoptExtensions,
        document.get(ADOPT_EXTENSIONS),
        &ADOPT_CHOICES,
    )
}

/// Refuses a present `value` that is not a string among `choices`.
fn choice(key: ChoiceKey, value: Option<&Value>, choices: &[&str]) -> Result<(), ConfigError> {
    match value {
        None => Ok(()),
        Some(Value::String(text)) if choices.contains(&text.as_str()) => Ok(()),
        Some(other) => Err(ConfigError::InvalidChoice {
            key,
            value: ConfigValue::new(other),
        }),
    }
}

/// Refuses a `service_orchestrator`, then a `session_prefix`, in the merged document.
fn removed_keys_are_absent(document: &Table) -> Result<(), ConfigError> {
    if document.contains_key("service_orchestrator") {
        return Err(ConfigError::RemovedKey(RemovedKey::ServiceOrchestrator));
    }
    if document.contains_key("session_prefix") {
        return Err(ConfigError::RemovedKey(RemovedKey::SessionPrefix));
    }
    Ok(())
}

/// Refuses an `[env.vars]`, then the first value of an env band that breaks its rules: the `workspace` band,
/// the `feature` band, and then every other band in the order written. A value is checked and dropped, and a
/// command entry is never run. An `env` that is not a table, a band that is not a table, and a band without
/// a `vars` table hold no value.
fn env_bands_are_valid(document: &Table) -> Result<(), ConfigError> {
    let Some(env) = document.get(ENV).and_then(Value::as_table) else {
        return Ok(());
    };
    if let Some(vars) = env.get(ENV_VARS) {
        let keys = vars
            .as_table()
            .map(|vars| vars.keys().cloned().collect())
            .unwrap_or_default();
        return Err(ConfigError::RemovedKey(RemovedKey::EnvVars { keys }));
    }
    let named = env
        .keys()
        .map(String::as_str)
        .filter(|name| !RESERVED_BANDS.contains(name));
    for band in RESERVED_BANDS.into_iter().chain(named) {
        band_is_valid(env, band)?;
    }
    Ok(())
}

/// Refuses the first value of the `vars` of the band `band` of `env` that breaks its rules.
fn band_is_valid(env: &Table, band: &str) -> Result<(), ConfigError> {
    let vars = env
        .get(band)
        .and_then(Value::as_table)
        .and_then(|band| band.get(ENV_VARS))
        .and_then(Value::as_table);
    for (key, value) in vars.into_iter().flatten() {
        env_value_is_valid(key, value).map_err(|fault| ConfigError::EnvVar {
            band: band.to_owned(),
            key: key.clone(),
            fault,
        })?;
    }
    Ok(())
}

/// Refuses an env band value that is no string, integer, float, or valid command entry.
fn env_value_is_valid(key: &str, value: &Value) -> Result<(), EnvVarFault> {
    match value {
        Value::Table(entry) => command_entry_is_valid(key, entry),
        Value::String(_) | Value::Integer(_) | Value::Float(_) => Ok(()),
        other => Err(EnvVarFault::UnsupportedType {
            found: ValueType::of(other),
        }),
    }
}

/// Refuses a command entry declared under a managed name, then one with an unknown field, a missing or
/// empty `command`, a `format` out of its set, a `shell` that is no boolean, or an `exports` that is no
/// array of strings.
fn command_entry_is_valid(key: &str, entry: &Table) -> Result<(), EnvVarFault> {
    if key.starts_with(MANAGED_PREFIX) {
        return Err(EnvVarFault::ManagedName);
    }
    let mut unknown: Vec<String> = entry
        .keys()
        .filter(|field| !COMMAND_ENTRY_FIELDS.contains(&field.as_str()))
        .cloned()
        .collect();
    unknown.sort();
    if !unknown.is_empty() {
        return Err(EnvVarFault::UnknownFields { fields: unknown });
    }
    match entry.get("command") {
        Some(Value::String(command)) if !command.is_empty() => {}
        _ => return Err(EnvVarFault::MissingCommand),
    }
    match entry.get("format") {
        None => {}
        Some(Value::String(format)) if COMMAND_FORMATS.contains(&format.as_str()) => {}
        Some(other) => {
            return Err(EnvVarFault::InvalidFormat {
                value: ConfigValue::new(other),
            });
        }
    }
    match entry.get("shell") {
        None | Some(Value::Boolean(_)) => {}
        Some(other) => {
            return Err(EnvVarFault::ShellNotBoolean {
                found: ValueType::of(other),
            });
        }
    }
    match entry.get("exports").map(not_a_list_of_strings) {
        Some(Some(_)) => Err(EnvVarFault::ExportsNotStringList),
        _ => Ok(()),
    }
}

/// Refuses the first `[model_tiers]` label, in the order written, whose value is no per-vendor table, or whose
/// table names a vendor that is unknown, a model id that is no non-empty string, or no vendor at all. A
/// `model_tiers` that is not a table holds no label.
///
/// Answers the sorted labels of the effective tier table: the built-in ones and those the table declares.
fn model_tiers_are_valid(document: &Table) -> Result<BTreeSet<String>, ConfigError> {
    let mut labels: BTreeSet<String> = BUILTIN_TIERS.map(str::to_owned).into();
    let Some(tiers) = document.get(MODEL_TIERS).and_then(Value::as_table) else {
        return Ok(labels);
    };
    for (label, vendors) in tiers {
        tier_is_valid(vendors).map_err(|fault| ConfigError::ModelTier {
            label: label.clone(),
            fault,
        })?;
        labels.insert(label.clone());
    }
    Ok(labels)
}

/// Refuses a tier value that is no table, a vendor that is unknown, a model id that is no non-empty string,
/// and a table without a vendor.
fn tier_is_valid(vendors: &Value) -> Result<(), ModelTierFault> {
    let Value::Table(vendors) = vendors else {
        return Err(ModelTierFault::NotVendorTable {
            found: ValueType::of(vendors),
        });
    };
    for (vendor, model) in vendors {
        if !VENDORS.contains(&vendor.as_str()) {
            return Err(ModelTierFault::UnknownVendor {
                vendor: vendor.clone(),
            });
        }
        match model {
            Value::String(id) if !id.is_empty() => {}
            other => {
                return Err(ModelTierFault::ModelIdNotString {
                    vendor: vendor.clone(),
                    found: ValueType::of(other),
                });
            }
        }
    }
    if vendors.is_empty() {
        return Err(ModelTierFault::NoVendor);
    }
    Ok(())
}

/// Refuses the first `[agent_model_overrides]` agent, in the order written, whose value is not a label of
/// `tiers` or a valid per-vendor table.
fn agent_overrides_are_valid(
    document: &Table,
    tiers: &BTreeSet<String>,
) -> Result<(), ConfigError> {
    let Some(overrides) = document
        .get(AGENT_MODEL_OVERRIDES)
        .and_then(Value::as_table)
    else {
        return Ok(());
    };
    for (agent, value) in overrides {
        override_is_valid(value, tiers).map_err(|fault| ConfigError::AgentOverride {
            agent: agent.clone(),
            fault,
        })?;
    }
    Ok(())
}

/// Refuses a blank string, a string that is no label of `tiers`, a value that is neither a string nor a
/// table, and a table that is empty or whose vendor breaks its rules.
fn override_is_valid(value: &Value, tiers: &BTreeSet<String>) -> Result<(), AgentOverrideFault> {
    match value {
        Value::String(label) if is_blank(label) => Err(AgentOverrideFault::EmptyValue),
        Value::String(label) if !tiers.contains(label) => Err(AgentOverrideFault::UnknownTier {
            label: label.clone(),
            known: tiers.iter().cloned().collect(),
        }),
        Value::String(_) => Ok(()),
        Value::Table(vendors) => {
            for (vendor, model) in vendors {
                vendor_override_is_valid(vendor, model)?;
            }
            if vendors.is_empty() {
                return Err(AgentOverrideFault::NoVendor);
            }
            Ok(())
        }
        other => Err(AgentOverrideFault::WrongType {
            found: ValueType::of(other),
        }),
    }
}

/// Refuses an unknown `vendor`, and a `value` that is neither a model id nor a valid profile.
fn vendor_override_is_valid(vendor: &str, value: &Value) -> Result<(), AgentOverrideFault> {
    let named = || vendor.to_owned();
    if !VENDORS.contains(&vendor) {
        return Err(AgentOverrideFault::UnknownVendor { vendor: named() });
    }
    let profile = match value {
        Value::String(id) if is_blank(id) => {
            return Err(AgentOverrideFault::VendorModelId { vendor: named() });
        }
        Value::String(_) => return Ok(()),
        Value::Table(profile) => profile,
        other => {
            return Err(AgentOverrideFault::VendorValueType {
                vendor: named(),
                found: ValueType::of(other),
            });
        }
    };
    let mut unknown: Vec<String> = profile
        .keys()
        .filter(|key| !PROFILE_KEYS.contains(&key.as_str()))
        .cloned()
        .collect();
    unknown.sort();
    if !unknown.is_empty() {
        return Err(AgentOverrideFault::UnknownProfileKeys {
            vendor: named(),
            keys: unknown,
        });
    }
    let set = |key: &str| profile.get(key).is_some();
    let readable = |key: &str| match profile.get(key) {
        Some(Value::String(text)) => !is_blank(text),
        _ => false,
    };
    if set("model") && !readable("model") {
        return Err(AgentOverrideFault::ProfileModel { vendor: named() });
    }
    if set("effort") && !readable("effort") {
        return Err(AgentOverrideFault::ProfileEffort { vendor: named() });
    }
    if !set("model") && !set("effort") {
        return Err(AgentOverrideFault::ProfileEmpty { vendor: named() });
    }
    Ok(())
}

/// Whether `text` is empty or only whitespace, as the reference reads it: Python's whitespace also holds the
/// four separator controls `U+001C` to `U+001F`.
fn is_blank(text: &str) -> bool {
    text.chars()
        .all(|c| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// Refuses a top-level `main_branch` that is neither a string nor one of the empty forms, which read as the
/// default.
fn main_branch_is_a_string(document: &Table) -> Result<(), ConfigError> {
    match document.get(MAIN_BRANCH) {
        None | Some(Value::String(_)) => Ok(()),
        Some(value) if reads_empty(value) => Ok(()),
        Some(other) => Err(value_type(TypedKey::MainBranch, other)),
    }
}

/// Refuses a top-level `git_excludes` that is not an array of strings. The refusal carries the type of the
/// first element that is no string, or the type of the value when it is no array.
fn git_excludes_is_a_list_of_strings(document: &Table) -> Result<(), ConfigError> {
    match document.get(GIT_EXCLUDES).and_then(not_a_list_of_strings) {
        Some(other) => Err(value_type(TypedKey::GitExcludes, other)),
        None => Ok(()),
    }
}

/// The part of `value` that keeps it from being an array of strings: the first element that is no string, or
/// the value itself when it is no array.
fn not_a_list_of_strings(value: &Value) -> Option<&Value> {
    match value {
        Value::Array(items) => items.iter().find(|item| !item.is_str()),
        other => Some(other),
    }
}

/// How a field of a repository entry is held.
#[derive(Clone, Copy)]
enum Shape {
    String,
    Strings,
}

/// The fields of a repository entry whose value has a type to check, in the order they are checked, with the
/// kind of entry that has the field: `None` for both.
const TYPED_FIELDS: [(&str, TypedKey, Shape, Option<RepoKind>); 7] = [
    ("main_branch", TypedKey::MainBranch, Shape::String, None),
    ("git_excludes", TypedKey::GitExcludes, Shape::Strings, None),
    ("cmd", TypedKey::Cmd, Shape::Strings, None),
    (
        "ref",
        TypedKey::Ref,
        Shape::String,
        Some(RepoKind::Standalone),
    ),
    (
        "path",
        TypedKey::Path,
        Shape::String,
        Some(RepoKind::Standalone),
    ),
    (
        "prefix",
        TypedKey::Prefix,
        Shape::String,
        Some(RepoKind::Standalone),
    ),
    (
        "config_dir",
        TypedKey::ConfigDir,
        Shape::String,
        Some(RepoKind::Standalone),
    ),
];

/// The array key that holds the entries of `kind`.
fn array_key(kind: RepoKind) -> (&'static str, TypedKey) {
    match kind {
        RepoKind::Project => ("project_repository", TypedKey::ProjectRepository),
        RepoKind::Standalone => ("standalone_repository", TypedKey::StandaloneRepository),
    }
}

/// The `prefix` of a standalone repository, with the label of the entry that declares it.
struct StandalonePrefix {
    label: String,
    prefix: String,
}

/// Refuses the project entries, then the standalone entries, each in declaration order, that break a rule of
/// their fields, and a standalone entry that resolves to the name of an earlier one. Answers the prefixes
/// the standalone entries declare, for the comparison with the workspace `prefix`.
fn repository_entries_are_valid(document: &Table) -> Result<Vec<StandalonePrefix>, ConfigError> {
    let mut prefixes = Vec::new();
    let mut standalone_names: HashMap<String, EntryIdent> = HashMap::new();
    for kind in [RepoKind::Project, RepoKind::Standalone] {
        let (key, typed) = array_key(kind);
        for entry in entries_of_array(document.get(key), typed)? {
            let Some(entry) = entry.as_table() else {
                continue;
            };
            let Some(identity) = identity(kind, entry)? else {
                continue;
            };
            if kind == RepoKind::Standalone {
                let earlier =
                    standalone_names.insert(identity.name.clone(), identity.ident.clone());
                if let Some(first) = earlier {
                    return Err(ConfigError::DuplicateStandaloneName {
                        name: identity.name,
                        first,
                        second: identity.ident,
                    });
                }
            }
            entry_is_valid(&identity.owner, entry)?;
            if let Some(Value::String(prefix)) = entry.get(PREFIX) {
                prefixes.push(StandalonePrefix {
                    label: identity.owner.label,
                    prefix: prefix.clone(),
                });
            }
        }
    }
    Ok(prefixes)
}

/// The entries `value`, the value of a repository array, stands for: its items for an array, and none for the
/// values the reference tool iterates without finding a table or reads as empty. A value it fails on is
/// refused with its type.
fn entries_of_array(value: Option<&Value>, key: TypedKey) -> Result<&[Value], ConfigError> {
    match value {
        Some(Value::Array(items)) => Ok(items),
        None | Some(Value::String(_) | Value::Table(_)) => Ok(&[]),
        Some(other) if reads_empty(other) => Ok(&[]),
        Some(other) => Err(value_type(key, other)),
    }
}

/// Who a repository entry is.
struct Identity {
    owner: RepoRef,
    /// The resolved name.
    name: String,
    ident: EntryIdent,
}

/// The identity of `entry`, or `None` when its `name` and `url` are both empty and the load skips it. A `name`
/// or `url` that is no string is refused. The resolved name is the `name` when it is not empty, else the name
/// derived from the `url`.
fn identity(kind: RepoKind, entry: &Table) -> Result<Option<Identity>, ConfigError> {
    let name = entry.get("name");
    let url = entry.get("url");
    let label = match (not_empty(name), not_empty(url)) {
        (None, None) => return Ok(None),
        (Some(value), _) | (None, Some(value)) => match value {
            Value::String(text) => text.clone(),
            other => ConfigValue::new(other).to_string(),
        },
    };
    let owner = RepoRef { kind, label };
    for (value, key) in [(name, TypedKey::Name), (url, TypedKey::Url)] {
        if let Some(value) = value.filter(|value| !value.is_str()) {
            return Err(repository_value_type(&owner, key, value));
        }
    }
    let (resolved, ident) = match (name.and_then(Value::as_str), url.and_then(Value::as_str)) {
        (Some(name), _) if !name.is_empty() => (name.to_owned(), EntryIdent::Name(name.to_owned())),
        (_, Some(url)) => (
            name_from_url(url).to_owned(),
            EntryIdent::Url(url.to_owned()),
        ),
        (_, None) => return Ok(None),
    };
    Ok(Some(Identity {
        owner,
        name: resolved,
        ident,
    }))
}

/// `value` when it is present and the reference tool reads it as true.
fn not_empty(value: Option<&Value>) -> Option<&Value> {
    value.filter(|value| !reads_empty(value))
}

fn repository_value_type(owner: &RepoRef, key: TypedKey, value: &Value) -> ConfigError {
    ConfigError::ValueType {
        key,
        owner: Some(owner.clone()),
        path: None,
        found: ValueType::of(value),
    }
}

fn repository_fault(owner: &RepoRef, fault: RepositoryFault) -> ConfigError {
    ConfigError::Repository {
        kind: owner.kind,
        label: owner.label.clone(),
        fault,
    }
}

/// Refuses an entry whose fields break a rule: first the types of the fields that have one, then the
/// booleans `extension` and `nested`, `load` and `entry`, the `path` and `config_dir` of a standalone, and
/// the rules of the nested workspace.
fn entry_is_valid(owner: &RepoRef, entry: &Table) -> Result<(), ConfigError> {
    for (field, key, shape, only) in TYPED_FIELDS {
        if only.is_some_and(|only| only != owner.kind) {
            continue;
        }
        let Some(value) = entry.get(field) else {
            continue;
        };
        let wrong = match shape {
            Shape::String => (!value.is_str()).then_some(value),
            Shape::Strings => not_a_list_of_strings(value),
        };
        if let Some(wrong) = wrong {
            return Err(repository_value_type(owner, key, wrong));
        }
    }
    let fault = |fault| repository_fault(owner, fault);
    if let Some(value) = entry.get("extension").filter(|value| !value.is_bool()) {
        return Err(fault(RepositoryFault::Extension {
            value: ConfigValue::new(value),
        }));
    }
    if let Some(value) = entry.get("nested").filter(|value| !value.is_bool()) {
        return Err(fault(RepositoryFault::Nested {
            value: ConfigValue::new(value),
        }));
    }
    if let Some(value) = entry.get("load") {
        match value {
            Value::String(text) if LOAD_CHOICES.contains(&text.as_str()) => {}
            other => {
                return Err(fault(RepositoryFault::Load {
                    value: ConfigValue::new(other),
                }));
            }
        }
    }
    if let Some(value) = entry.get("entry") {
        entry_paths_are_valid(owner, value)?;
    }
    for (field, path_field) in [
        ("path", PathField::Path),
        ("config_dir", PathField::ConfigDir),
    ] {
        if owner.kind == RepoKind::Standalone
            && let Some(Value::String(path)) = entry.get(field)
            && !is_inside(path)
        {
            return Err(fault(RepositoryFault::Path {
                field: path_field,
                value: path.clone(),
            }));
        }
    }
    nested_rules_hold(owner, entry)
}

/// Refuses an `entry` that is not a non-empty array of non-empty strings, each a path inside the repository.
fn entry_paths_are_valid(owner: &RepoRef, value: &Value) -> Result<(), ConfigError> {
    let Value::Array(items) = value else {
        return Err(repository_fault(
            owner,
            RepositoryFault::EntryList {
                value: ConfigValue::new(value),
            },
        ));
    };
    if items.is_empty() {
        return Err(repository_fault(
            owner,
            RepositoryFault::EntryList {
                value: ConfigValue::new(value),
            },
        ));
    }
    for item in items {
        match item {
            Value::String(path) if !path.is_empty() => {
                if !is_inside(path) {
                    return Err(repository_fault(
                        owner,
                        RepositoryFault::EntryPathEscapes {
                            value: path.clone(),
                        },
                    ));
                }
            }
            other => {
                return Err(repository_fault(
                    owner,
                    RepositoryFault::EntryPathEmpty {
                        value: ConfigValue::new(other),
                    },
                ));
            }
        }
    }
    Ok(())
}

/// Whether `path` is relative and has no `..` segment.
fn is_inside(path: &str) -> bool {
    let path = Path::new(path);
    !path.is_absolute()
        && !path
            .components()
            .any(|component| component == Component::ParentDir)
}

/// Refuses an entry that breaks the rules of a nested workspace: `envs` and `inherit_local` without
/// `nested = true`; with it, `load` and `entry`, `extension = true`, a standalone entry, an `envs` that is not
/// an integer of at least 1, and an `inherit_local` that is not a list of names without a `.`.
fn nested_rules_hold(owner: &RepoRef, entry: &Table) -> Result<(), ConfigError> {
    let fault = |fault| repository_fault(owner, fault);
    if entry.get("nested") != Some(&Value::Boolean(true)) {
        for (field, key) in [
            ("envs", NestedOnlyKey::Envs),
            ("inherit_local", NestedOnlyKey::InheritLocal),
        ] {
            if entry.contains_key(field) {
                return Err(fault(RepositoryFault::NestedOnly { key }));
            }
        }
        return Ok(());
    }
    for (field, key) in [
        ("load", NestedExcludedKey::Load),
        ("entry", NestedExcludedKey::Entry),
    ] {
        if entry.contains_key(field) {
            return Err(fault(RepositoryFault::NestedWith { key }));
        }
    }
    if entry.get("extension") == Some(&Value::Boolean(true)) {
        return Err(fault(RepositoryFault::NestedWithExtensionTrue));
    }
    if owner.kind == RepoKind::Standalone {
        return Err(fault(RepositoryFault::StandaloneNested));
    }
    match entry.get("envs") {
        None => {}
        Some(Value::Integer(count)) if *count >= 1 => {}
        Some(other) => {
            return Err(fault(RepositoryFault::Envs {
                value: ConfigValue::new(other),
            }));
        }
    }
    if let Some(value) = entry.get("inherit_local") {
        let names = match value {
            Value::Array(names) => Some(names),
            _ => None,
        };
        let valid = names.is_some_and(|names| {
            names.iter().all(|name| {
                name.as_str()
                    .is_some_and(|name| !name.is_empty() && !name.contains('.'))
            })
        });
        if !valid {
            return Err(fault(RepositoryFault::InheritLocal {
                value: ConfigValue::new(value),
            }));
        }
    }
    Ok(())
}

/// Refuses a workspace `prefix` that a standalone repository also declares. A `prefix` that is no string, or
/// is empty, is the default.
fn prefix_differs_from_the_standalone_prefixes(
    document: &Table,
    standalones: &[StandalonePrefix],
) -> Result<(), ConfigError> {
    let prefix = match document.get(PREFIX) {
        Some(Value::String(prefix)) if !prefix.is_empty() => prefix.as_str(),
        _ => DEFAULT_PREFIX,
    };
    match standalones
        .iter()
        .find(|standalone| standalone.prefix == prefix)
    {
        Some(standalone) => Err(ConfigError::PrefixCollision {
            prefix: prefix.to_owned(),
            standalone: standalone.label.clone(),
        }),
        None => Ok(()),
    }
}

/// How the layering treats a top-level key.
enum Rule {
    /// The local array is appended to the shared one; a local value that is no array replaces it.
    Append(TypedKey),
    /// The local direct children replace the shared ones of the same name, and the others survive; a local
    /// value that is no table leaves the shared table as it is.
    Table(TypedKey),
    /// As [`Rule::Table`], for a key whose shared value is a table or absent, because the load has refused
    /// every other.
    OverridesTable,
    /// The local value replaces the shared one whole.
    Replace,
}

fn rule(key: &str) -> Rule {
    match key {
        "project_repository" => Rule::Append(TypedKey::ProjectRepository),
        "standalone_repository" => Rule::Append(TypedKey::StandaloneRepository),
        "git" => Rule::Table(TypedKey::Git),
        "keybindings" => Rule::Table(TypedKey::Keybindings),
        "tui" => Rule::Table(TypedKey::Tui),
        "capabilities" => Rule::Table(TypedKey::Capabilities),
        "env" => Rule::Table(TypedKey::Env),
        "model_tiers" => Rule::Table(TypedKey::ModelTiers),
        AGENT_MODEL_OVERRIDES => Rule::OverridesTable,
        _ => Rule::Replace,
    }
}

/// The `shared` document with the `local` one layered over it, by the rule of each key `local` holds, taken
/// in the order of the local file.
///
/// An empty local document leaves the shared one as it is. For a key of the array or table rules, whatever
/// the local value is, the shared value is read as the rule's kind first: one that cannot be read as it is
/// refused, naming the key and `shared_path`, the file it came from. The first key that fails is the one
/// reported.
fn merge(shared: Table, local: Table, shared_path: &Path) -> Result<Table, ConfigError> {
    if local.is_empty() {
        return Ok(shared);
    }
    let refused = |key: TypedKey, found: ValueType| ConfigError::ValueType {
        key,
        owner: None,
        path: Some(shared_path.to_path_buf()),
        found,
    };
    let mut merged = shared;
    for (name, value) in local {
        let layered = match rule(&name) {
            Rule::Replace => value,
            Rule::Append(key) => {
                let mut entries =
                    entries_of(merged.get(&name)).map_err(|found| refused(key, found))?;
                match value {
                    Value::Array(local_entries) => {
                        entries.extend(local_entries);
                        Value::Array(entries)
                    }
                    replacement => replacement,
                }
            }
            Rule::Table(key) => {
                let table = table_of(merged.get(&name)).map_err(|found| refused(key, found))?;
                Value::Table(with_children(table, value))
            }
            Rule::OverridesTable => {
                let table = match merged.get(&name) {
                    Some(Value::Table(table)) => table.clone(),
                    _ => Table::new(),
                };
                Value::Table(with_children(table, value))
            }
        };
        merged.insert(name, layered);
    }
    Ok(merged)
}

/// `table` with the children of `local` set over it, when `local` is a table; else `table` as it is.
fn with_children(mut table: Table, local: Value) -> Table {
    if let Value::Table(children) = local {
        for (name, child) in children {
            table.insert(name, child);
        }
    }
    table
}

/// Whether the reference tool reads `value` as false: `false`, zero, an empty string, array, or table.
fn reads_empty(value: &Value) -> bool {
    match value {
        Value::Boolean(flag) => !flag,
        Value::Integer(number) => *number == 0,
        Value::Float(number) => *number == 0.0,
        Value::String(text) => text.is_empty(),
        Value::Array(items) => items.is_empty(),
        Value::Table(table) => table.is_empty(),
        Value::Datetime(_) => false,
    }
}

/// The shared value of an array-append key read as the list to append to: the items of an array, the
/// characters of a string and the keys of a table as strings, and nothing for an absent or empty value. Any
/// other value is refused with its type.
fn entries_of(shared: Option<&Value>) -> Result<Vec<Value>, ValueType> {
    let strings = |parts: &mut dyn Iterator<Item = String>| parts.map(Value::String).collect();
    match shared {
        None => Ok(Vec::new()),
        Some(value) if reads_empty(value) => Ok(Vec::new()),
        Some(Value::Array(items)) => Ok(items.clone()),
        Some(Value::String(text)) => Ok(strings(&mut text.chars().map(String::from))),
        Some(Value::Table(table)) => Ok(strings(&mut table.keys().cloned())),
        Some(other) => Err(ValueType::of(other)),
    }
}

/// The shared value of a table-merge key read as the table to merge into: a table as it is, nothing for an
/// absent or empty value, and the table of the pairs an array holds, when every item is one. Any other value
/// is refused with its type.
fn table_of(shared: Option<&Value>) -> Result<Table, ValueType> {
    match shared {
        None => Ok(Table::new()),
        Some(value) if reads_empty(value) => Ok(Table::new()),
        Some(Value::Table(table)) => Ok(table.clone()),
        Some(Value::Array(items)) => items
            .iter()
            .map(pair)
            .collect::<Option<Table>>()
            .ok_or(ValueType::Array),
        Some(other) => Err(ValueType::of(other)),
    }
}

/// The key and the value an array item stands for as a pair: a two-character string is its characters, a
/// two-key table is its keys, and a two-item array is its items, when the first is not an array or a table.
/// A later pair of the same key replaces the value of an earlier one and keeps its place.
fn pair(item: &Value) -> Option<(String, Value)> {
    match item {
        Value::String(text) => {
            let mut chars = text.chars();
            match (chars.next(), chars.next(), chars.next()) {
                (Some(key), Some(value), None) => {
                    Some((key.to_string(), Value::String(value.to_string())))
                }
                _ => None,
            }
        }
        Value::Table(table) if table.len() == 2 => {
            let mut keys = table.keys();
            Some((keys.next()?.clone(), Value::String(keys.next()?.clone())))
        }
        Value::Array(items) => match items.as_slice() {
            [Value::Array(_) | Value::Table(_), _] => None,
            [key, value] => Some((key_text(key), value.clone())),
            _ => None,
        },
        _ => None,
    }
}

/// The name a scalar array item gives the table key it stands for.
fn key_text(key: &Value) -> String {
    match key {
        Value::String(text) => text.clone(),
        Value::Boolean(true) => "True".to_owned(),
        Value::Boolean(false) => "False".to_owned(),
        Value::Float(number) => format!("{number:?}"),
        other => other.to_string(),
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

    fn load_from(files: &MemoryFiles) -> Result<Config, ConfigError> {
        load(files, Path::new("/ws"))
    }

    fn shared(text: &str) -> Config {
        load_from(&MemoryFiles::default().with(SHARED, text.as_bytes())).expect("a configuration")
    }

    fn layered(shared: &str, local: &str) -> Config {
        let files = MemoryFiles::default()
            .with(SHARED, shared.as_bytes())
            .with(LOCAL, local.as_bytes());
        load_from(&files).expect("a configuration")
    }

    fn strings(items: &[&str]) -> Vec<String> {
        items.iter().map(|item| (*item).to_owned()).collect()
    }

    fn table(text: &str) -> Table {
        toml::from_str(text).expect("a TOML document")
    }

    fn merged(shared: &str, local: &str) -> Table {
        merge(table(shared), table(local), Path::new(SHARED)).expect("a merged document")
    }

    /// The key and the type a merge of `shared` and `local` refuses, with the file it names.
    fn refusal(shared: &str, local: &str) -> (TypedKey, ValueType) {
        let error = merge(table(shared), table(local), Path::new(SHARED)).expect_err("a refusal");
        match error {
            ConfigError::ValueType {
                key,
                owner: None,
                path: Some(path),
                found,
            } if path == Path::new(SHARED) => (key, found),
            other => panic!("expected a value type refusal naming the shared file: {other:?}"),
        }
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

    #[test]
    fn a_local_repository_entry_appends_after_the_shared_ones() {
        let result = merged(
            "[[project_repository]]\nname = \"api\"\n[[project_repository]]\nname = \"web\"\n\
             [[standalone_repository]]\nname = \"tools\"\n",
            "[[project_repository]]\nname = \"mine\"\n[[standalone_repository]]\nname = \"extra\"\n",
        );

        assert_eq!(
            result,
            table(
                "[[project_repository]]\nname = \"api\"\n[[project_repository]]\nname = \"web\"\n\
                 [[project_repository]]\nname = \"mine\"\n\
                 [[standalone_repository]]\nname = \"tools\"\n[[standalone_repository]]\nname = \"extra\"\n"
            )
        );
    }

    #[test]
    fn a_local_repository_value_that_is_not_an_array_replaces_the_shared_array() {
        let result = merged(
            "[[project_repository]]\nname = \"api\"\n",
            "project_repository = \"none\"\n",
        );

        assert_eq!(result, table("project_repository = \"none\"\n"));
    }

    #[test]
    fn a_local_git_user_table_drops_the_shared_user_email() {
        let result = merged(
            "[git]\nuser = { name = \"A\", email = \"a@x\" }\nother = 1\n",
            "[git]\nuser = { name = \"B\" }\n",
        );

        assert_eq!(result, table("[git]\nuser = { name = \"B\" }\nother = 1\n"));
    }

    #[test]
    fn local_bindings_replace_the_shared_bindings_and_keep_leader_and_timeoutlen() {
        let result = merged(
            "[keybindings]\nleader = \"x\"\ntimeoutlen = 5\n[keybindings.bindings]\nopen = \"o\"\nquit = \"q\"\n",
            "[keybindings.bindings]\nopen = \"p\"\n",
        );

        assert_eq!(
            result,
            table(
                "[keybindings]\nleader = \"x\"\ntimeoutlen = 5\n[keybindings.bindings]\nopen = \"p\"\n"
            )
        );
    }

    #[test]
    fn local_feature_vars_replace_every_shared_feature_var_and_keep_the_workspace_vars() {
        let result = merged(
            "[env.workspace.vars]\nA = \"1\"\n[env.feature.vars]\nB = \"2\"\nC = \"3\"\n[env.named.vars]\nD = \"4\"\n",
            "[env.feature.vars]\nE = \"5\"\n",
        );

        assert_eq!(
            result,
            table(
                "[env.workspace.vars]\nA = \"1\"\n[env.feature.vars]\nE = \"5\"\n[env.named.vars]\nD = \"4\"\n"
            )
        );
    }

    #[test]
    fn a_local_capability_slot_replaces_only_that_slot() {
        let result = merged(
            "[capabilities]\nservice = [\"tmux\"]\nother = \"x\"\n",
            "[capabilities]\nservice = \"docker\"\n",
        );

        assert_eq!(
            result,
            table("[capabilities]\nservice = \"docker\"\nother = \"x\"\n")
        );
    }

    #[test]
    fn a_local_space_table_drops_the_shared_space_kinds() {
        let result = merged(
            "[space]\nroot = \"r\"\n[space.kinds]\nwork = \"w\"\n",
            "[space]\nroot = \"s\"\n",
        );

        assert_eq!(result, table("[space]\nroot = \"s\"\n"));
    }

    #[test]
    fn local_lint_and_git_excludes_replace_the_shared_values() {
        let result = merged(
            "lint = [\"a.sh\"]\ngit_excludes = [\"x\", \"y\"]\n",
            "lint = { scripts = \"b.sh\" }\ngit_excludes = 3\n",
        );

        assert_eq!(
            result,
            table("lint = { scripts = \"b.sh\" }\ngit_excludes = 3\n")
        );
    }

    #[test]
    fn a_local_value_that_is_not_a_table_leaves_the_shared_table_untouched() {
        let shared =
            "[git]\nuser = { name = \"A\" }\n[tui]\nx = 1\n[env.workspace.vars]\nA = \"1\"\n";

        let result = merged(shared, "git = \"x\"\ntui = 5\nenv = [1]\n");

        assert_eq!(result, table(shared));
    }

    #[test]
    fn an_empty_local_document_returns_the_shared_document_unchanged() {
        // Not even a shared value the merge would refuse is read.
        let shared = "git = \"x\"\nproject_repository = true\nlint = [\"a\"]\n";

        let result = merge(table(shared), Table::new(), Path::new(SHARED)).expect("a document");

        assert_eq!(result, table(shared));
        assert_eq!(
            result.keys().collect::<Vec<_>>(),
            ["git", "project_repository", "lint"]
        );
    }

    #[test]
    fn the_merged_document_keeps_the_order_of_the_shared_keys_then_the_new_local_ones() {
        let result = merged(
            "a = 1\nb = 2\n[git]\nx = 1\n",
            "c = 3\nb = 4\n[git]\ny = 2\n",
        );

        assert_eq!(result.keys().collect::<Vec<_>>(), ["a", "b", "git", "c"]);
        assert_eq!(result["b"], Value::Integer(4));
    }

    #[test]
    fn a_shared_git_string_with_a_local_git_table_is_refused_naming_the_key() {
        assert_eq!(
            refusal("git = \"x\"\n", "[git]\nuser = { name = \"B\" }\n"),
            (TypedKey::Git, ValueType::String)
        );
    }

    #[test]
    fn a_shared_repository_boolean_with_a_local_repository_array_is_refused() {
        assert_eq!(
            refusal(
                "project_repository = true\n",
                "[[project_repository]]\nname = \"a\"\n"
            ),
            (TypedKey::ProjectRepository, ValueType::Boolean)
        );
        assert_eq!(
            refusal(
                "standalone_repository = 2\n",
                "standalone_repository = []\n"
            ),
            (TypedKey::StandaloneRepository, ValueType::Integer)
        );
        assert_eq!(
            refusal(
                "project_repository = 1979-05-27\n",
                "project_repository = 5\n"
            ),
            (TypedKey::ProjectRepository, ValueType::Datetime)
        );
    }

    #[test]
    fn a_shared_table_key_that_is_a_scalar_is_refused_whatever_the_local_value_is() {
        assert_eq!(
            refusal("tui = 1\n", "tui = \"y\"\n"),
            (TypedKey::Tui, ValueType::Integer)
        );
        for key in ["git", "keybindings", "capabilities", "env", "model_tiers"] {
            let (refused, found) = refusal(&format!("{key} = 1.5\n"), &format!("{key} = 2\n"));

            assert_eq!(refused.to_string(), key);
            assert_eq!(found, ValueType::Float);
        }
    }

    #[test]
    fn the_first_local_key_in_file_order_whose_shared_value_is_refused_is_the_one_reported() {
        let shared = "git = \"x\"\ntui = 1\n";

        assert_eq!(
            refusal(shared, "tui = \"y\"\ngit = \"z\"\n"),
            (TypedKey::Tui, ValueType::Integer)
        );
        assert_eq!(
            refusal(shared, "git = \"z\"\ntui = \"y\"\n"),
            (TypedKey::Git, ValueType::String)
        );
    }

    #[test]
    fn the_empty_forms_of_a_shared_value_read_as_empty() {
        for shared in ["false", "0", "0.0", "\"\"", "[]", "{}"] {
            let result = merged(
                &format!("git = {shared}\nproject_repository = {shared}\n"),
                "[git]\nx = 1\n[[project_repository]]\nname = \"a\"\n",
            );

            assert_eq!(
                result,
                table("[git]\nx = 1\n[[project_repository]]\nname = \"a\"\n"),
                "{shared}"
            );
        }
    }

    #[test]
    fn a_shared_string_or_table_of_repositories_reads_as_its_characters_or_keys() {
        let result = merged(
            "project_repository = \"ab\"\nstandalone_repository = { x = 1, y = 2 }\n",
            "project_repository = []\nstandalone_repository = [{ name = \"n\" }]\n",
        );

        assert_eq!(
            result,
            table(
                "project_repository = [\"a\", \"b\"]\nstandalone_repository = [\"x\", \"y\", { name = \"n\" }]\n"
            )
        );
    }

    #[test]
    fn a_shared_array_of_pairs_merges_as_the_table_of_those_pairs() {
        let result = merged(
            "keybindings = [\"ab\"]\n",
            "[keybindings]\nleader = \"x\"\n",
        );

        assert_eq!(result, table("[keybindings]\na = \"b\"\nleader = \"x\"\n"));
    }

    #[test]
    fn a_pair_is_a_two_character_string_a_two_key_table_or_a_two_item_array() {
        let result = merged(
            "git = [\"ab\", { k = 1, v = 2 }, [\"c\", 3], [4, [5]], [true, \"t\"], \"ab\"]\n",
            "[git]\nz = 0\n",
        );

        assert_eq!(
            result,
            table("[git]\na = \"b\"\nk = \"v\"\nc = 3\n4 = [5]\nTrue = \"t\"\nz = 0\n")
        );
    }

    #[test]
    fn a_pair_key_that_is_no_string_folds_into_the_string_key_of_its_text_and_the_later_value_is_kept()
     {
        let result = merged(
            "model_tiers = [[1, \"bad\"], [\"1\", { claude = \"c\" }]]\n",
            "[model_tiers]\nx = { claude = \"c\" }\n",
        );

        assert_eq!(
            result,
            table("[model_tiers]\n1 = { claude = \"c\" }\nx = { claude = \"c\" }\n")
        );
    }

    #[test]
    fn a_shared_array_with_an_item_that_is_no_pair_is_refused_as_an_array() {
        for shared in [
            "[\"abc\"]",
            "[\"ab\", 1]",
            "[{ a = 1 }]",
            "[{ a = 1, b = 2, c = 3 }]",
            "[[\"a\"]]",
            "[[[1], 2]]",
            "[[\"a\", 1, 2]]",
        ] {
            assert_eq!(
                refusal(&format!("git = {shared}\n"), "[git]\nx = 1\n"),
                (TypedKey::Git, ValueType::Array),
                "{shared}"
            );
        }
    }

    #[test]
    fn a_non_empty_shared_string_for_a_table_key_is_refused() {
        assert_eq!(
            refusal("env = \"ab\"\n", "env = {}\n"),
            (TypedKey::Env, ValueType::String)
        );
    }

    #[test]
    fn agent_model_overrides_merge_by_agent_whatever_the_other_file_holds() {
        let result = merged(
            "[agent_model_overrides]\na = \"opus\"\nb = \"haiku\"\n",
            "[agent_model_overrides]\nb = \"sonnet\"\n",
        );
        let alone = merged("", "[agent_model_overrides]\nb = \"sonnet\"\n");

        assert_eq!(
            result,
            table("[agent_model_overrides]\na = \"opus\"\nb = \"sonnet\"\n")
        );
        assert_eq!(alone, table("[agent_model_overrides]\nb = \"sonnet\"\n"));
    }

    #[test]
    fn a_missing_file_reads_as_an_empty_document() {
        let files = MemoryFiles::default().with(LOCAL, b"x = 1\n");

        assert_eq!(
            document(&files, Path::new(SHARED)).expect("a document"),
            Table::new()
        );
        assert_eq!(
            document(&files, Path::new("/ws/.winter/missing.toml")).expect("a document"),
            Table::new()
        );
        assert_eq!(
            document(&files, Path::new(LOCAL)).expect("a document"),
            table("x = 1\n")
        );
    }

    #[test]
    fn a_missing_shared_file_and_a_missing_local_file_each_load() {
        let only_local = MemoryFiles::default().with(LOCAL, b"envs_per_workspace = 12\n");
        let only_shared = MemoryFiles::default().with(SHARED, b"envs_per_workspace = 13\n");

        assert_eq!(
            load_from(&only_local)
                .expect("a configuration")
                .envs_per_workspace,
            12
        );
        assert_eq!(
            load_from(&only_shared)
                .expect("a configuration")
                .envs_per_workspace,
            13
        );
    }

    #[test]
    fn an_agent_model_overrides_that_is_not_a_table_is_refused_naming_its_file() {
        for (file, found, text) in [
            (LOCAL, ValueType::Integer, "agent_model_overrides = 1\n"),
            (SHARED, ValueType::String, "agent_model_overrides = \"x\"\n"),
            (
                SHARED,
                ValueType::Boolean,
                "agent_model_overrides = false\n",
            ),
            (LOCAL, ValueType::Array, "agent_model_overrides = []\n"),
        ] {
            let other = if file == LOCAL { SHARED } else { LOCAL };
            let files = MemoryFiles::default()
                .with(file, text.as_bytes())
                .with(other, b"");

            let error = load_from(&files).expect_err("a refusal");

            assert!(
                matches!(
                    &error,
                    ConfigError::AgentOverridesNotTable { path, found: refused }
                        if path == Path::new(file) && *refused == found
                ),
                "{text}: {error:?}"
            );
        }
    }

    #[test]
    fn an_agent_model_overrides_table_is_accepted_in_either_file() {
        let files = MemoryFiles::default()
            .with(SHARED, b"[agent_model_overrides]\na = \"opus\"\n")
            .with(LOCAL, b"[agent_model_overrides]\nb = \"haiku\"\n");

        assert!(load_from(&files).is_ok());
    }

    #[test]
    fn the_load_refuses_a_shared_value_the_merge_cannot_read_naming_the_shared_file() {
        let files = MemoryFiles::default()
            .with(SHARED, b"git = \"x\"\n")
            .with(LOCAL, b"[git]\nuser = { name = \"B\" }\n");

        let error = load_from(&files).expect_err("a refusal");

        assert!(
            matches!(
                &error,
                ConfigError::ValueType { key: TypedKey::Git, owner: None, path: Some(path), found: ValueType::String }
                    if path == Path::new(SHARED)
            ),
            "{error:?}"
        );
        let text = error.to_string();
        assert!(text.contains("git") && text.contains(SHARED), "{text}");
    }

    #[test]
    fn a_file_that_is_not_valid_toml_is_refused_before_the_overrides_and_the_merge() {
        let files = MemoryFiles::default()
            .with(SHARED, b"agent_model_overrides = 1\ngit = \"x\"\n")
            .with(LOCAL, b"= broken\n");

        let error = load_from(&files).expect_err("a refusal");

        assert!(
            matches!(&error, ConfigError::FileRead { path, .. } if path == Path::new(LOCAL)),
            "{error:?}"
        );
    }

    /// The refusal a load of `shared` alone ends with.
    fn refused(shared: &str) -> ConfigError {
        load_from(&MemoryFiles::default().with(SHARED, shared.as_bytes())).expect_err("a refusal")
    }

    /// The refusal a load of `shared` with `local` over it ends with.
    fn refused_layered(shared: &str, local: &str) -> ConfigError {
        let files = MemoryFiles::default()
            .with(SHARED, shared.as_bytes())
            .with(LOCAL, local.as_bytes());
        load_from(&files).expect_err("a refusal")
    }

    /// The key and the type of the value type refusal a load of `shared` ends with.
    fn value_type_refusal(shared: &str) -> (TypedKey, ValueType) {
        match refused(shared) {
            ConfigError::ValueType {
                key,
                owner: None,
                path: None,
                found,
            } => (key, found),
            other => panic!("expected a value type refusal: {other:?}"),
        }
    }

    fn assert_removed(error: &ConfigError, removed: RemovedKey, replacement: &str) {
        let ConfigError::RemovedKey(found) = error else {
            panic!("expected a removed key refusal: {error:?}");
        };
        assert_eq!(
            std::mem::discriminant(found),
            std::mem::discriminant(&removed),
            "{error:?}"
        );
        let text = error.to_string();
        assert!(text.contains(replacement), "{text}");
    }

    #[test]
    fn a_service_orchestrator_in_the_shared_file_is_a_removed_key_naming_its_replacement() {
        let error = refused("service_orchestrator = \"tmux\"\n");

        assert_removed(
            &error,
            RemovedKey::ServiceOrchestrator,
            "[capabilities] service",
        );
        assert!(error.to_string().contains("service_orchestrator"));
    }

    #[test]
    fn a_service_orchestrator_in_the_local_file_only_is_a_removed_key() {
        let error = refused_layered("# nothing\n", "service_orchestrator = \"tmux\"\n");

        assert_removed(
            &error,
            RemovedKey::ServiceOrchestrator,
            "[capabilities] service",
        );
    }

    #[test]
    fn a_session_prefix_in_the_shared_file_is_a_removed_key_naming_its_replacement() {
        let error = refused("session_prefix = \"x\"\n");

        assert_removed(&error, RemovedKey::SessionPrefix, "service_prefix");
        assert!(error.to_string().contains("session_prefix"));
    }

    #[test]
    fn a_session_prefix_in_the_local_file_only_is_a_removed_key() {
        let error = refused_layered("# nothing\n", "session_prefix = \"x\"\n");

        assert_removed(&error, RemovedKey::SessionPrefix, "service_prefix");
    }

    #[test]
    fn a_service_orchestrator_is_refused_before_a_session_prefix() {
        let error = refused("session_prefix = \"x\"\nservice_orchestrator = \"tmux\"\n");

        assert_removed(
            &error,
            RemovedKey::ServiceOrchestrator,
            "[capabilities] service",
        );
    }

    #[test]
    fn an_adopt_extensions_that_is_not_a_choice_is_refused_naming_the_key_and_the_value() {
        let error = refused("adopt_extensions = \"some\"\n");

        assert!(
            matches!(
                &error,
                ConfigError::InvalidChoice { key: ChoiceKey::AdoptExtensions, value }
                    if *value == ConfigValue::new(&Value::String("some".to_owned()))
            ),
            "{error:?}"
        );
        let text = error.to_string();
        assert!(
            text.contains("adopt_extensions") && text.contains("some"),
            "{text}"
        );
        assert_eq!(text.lines().count(), 1);
    }

    #[test]
    fn the_three_adopt_extensions_choices_load() {
        for choice in ["none", "winter", "all"] {
            shared(&format!("adopt_extensions = \"{choice}\"\n"));
        }
    }

    #[test]
    fn an_adopt_extensions_that_is_no_string_is_refused() {
        for value in ["3", "true", "[\"none\"]", "{ a = 1 }", "\"\""] {
            let error = refused(&format!("adopt_extensions = {value}\n"));

            assert!(
                matches!(
                    &error,
                    ConfigError::InvalidChoice {
                        key: ChoiceKey::AdoptExtensions,
                        ..
                    }
                ),
                "{value}: {error:?}"
            );
            assert_eq!(error.to_string().lines().count(), 1, "{value}");
        }
    }

    #[test]
    fn a_dashboard_layout_that_is_not_a_choice_is_refused_naming_the_key_and_the_value() {
        let error = refused("[tui.dashboard]\nlayout = \"grid\"\n");

        assert!(
            matches!(
                &error,
                ConfigError::InvalidChoice { key: ChoiceKey::DashboardLayout, value }
                    if *value == ConfigValue::new(&Value::String("grid".to_owned()))
            ),
            "{error:?}"
        );
        let text = error.to_string();
        assert!(
            text.contains("tui.dashboard.layout") && text.contains("grid"),
            "{text}"
        );
    }

    #[test]
    fn the_four_dashboard_layouts_load() {
        for layout in ["auto", "repos-as-columns", "repos-as-rows", "list"] {
            shared(&format!("[tui.dashboard]\nlayout = \"{layout}\"\n"));
        }
    }

    #[test]
    fn a_dashboard_layout_that_is_no_string_is_refused() {
        let error = refused("[tui.dashboard]\nlayout = 3\n");

        assert!(
            matches!(
                &error,
                ConfigError::InvalidChoice {
                    key: ChoiceKey::DashboardLayout,
                    ..
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_tui_or_dashboard_that_is_no_table_holds_no_layout() {
        shared("tui = \"x\"\n");
        shared("[tui]\ndashboard = \"x\"\n");
        shared("[tui.dashboard]\ncolumns = 3\n");
    }

    #[test]
    fn a_main_branch_that_is_neither_a_string_nor_empty_is_a_value_type_refusal() {
        assert_eq!(
            value_type_refusal("main_branch = 3\n"),
            (TypedKey::MainBranch, ValueType::Integer)
        );
        assert_eq!(
            value_type_refusal("main_branch = true\n"),
            (TypedKey::MainBranch, ValueType::Boolean)
        );
        assert_eq!(
            value_type_refusal("main_branch = [\"main\"]\n"),
            (TypedKey::MainBranch, ValueType::Array)
        );
    }

    #[test]
    fn a_main_branch_in_an_empty_form_or_a_string_loads() {
        for value in ["\"\"", "false", "0", "0.0", "[]", "{}", "\"trunk\""] {
            shared(&format!("main_branch = {value}\n"));
        }
    }

    #[test]
    fn a_git_excludes_that_is_not_an_array_of_strings_is_a_value_type_refusal() {
        assert_eq!(
            value_type_refusal("git_excludes = \"abc\"\n"),
            (TypedKey::GitExcludes, ValueType::String)
        );
        assert_eq!(
            value_type_refusal("git_excludes = {}\n"),
            (TypedKey::GitExcludes, ValueType::Table)
        );
        assert_eq!(
            value_type_refusal("git_excludes = [\"a\", 1]\n"),
            (TypedKey::GitExcludes, ValueType::Integer)
        );
        assert_eq!(
            value_type_refusal("git_excludes = false\n"),
            (TypedKey::GitExcludes, ValueType::Boolean)
        );
    }

    #[test]
    fn an_absent_or_empty_git_excludes_and_an_array_of_strings_load() {
        shared("# nothing\n");
        shared("git_excludes = []\n");
        shared("git_excludes = [\"a\", \"b\"]\n");
    }

    #[test]
    fn a_git_that_is_not_a_table_is_a_value_type_refusal() {
        assert_eq!(
            value_type_refusal("git = \"x\"\n"),
            (TypedKey::Git, ValueType::String)
        );
        assert_eq!(
            value_type_refusal("git = [\"x\"]\n"),
            (TypedKey::Git, ValueType::Array)
        );
    }

    #[test]
    fn a_git_user_that_is_not_a_table_is_a_value_type_refusal() {
        assert_eq!(
            value_type_refusal("[git]\nuser = 1\n"),
            (TypedKey::GitUser, ValueType::Integer)
        );
    }

    #[test]
    fn a_git_user_with_both_fields_non_empty_and_one_not_a_string_is_a_value_type_refusal() {
        assert_eq!(
            value_type_refusal("[git]\nuser = { name = 1, email = \"e\" }\n"),
            (TypedKey::GitUserName, ValueType::Integer)
        );
        assert_eq!(
            value_type_refusal("[git]\nuser = { name = \"n\", email = true }\n"),
            (TypedKey::GitUserEmail, ValueType::Boolean)
        );
    }

    #[test]
    fn an_empty_git_or_git_user_form_loads_with_no_identity() {
        shared("git = false\n");
        shared("git = 0\n");
        shared("git = \"\"\n");
        shared("[git]\nuser = \"\"\n");
        shared("[git]\nuser = false\n");
        shared("[git]\nuser = { name = 1, email = \"\" }\n");
        shared("[git]\nuser = { name = \"\", email = 1 }\n");
        shared("[git]\nuser = { name = 1 }\n");
        shared("[git]\nuser = { name = \"n\", email = \"e\" }\n");
    }

    #[test]
    fn a_refusal_names_the_key_and_the_type() {
        let text = refused("main_branch = 3\n").to_string();
        assert!(
            text.contains("main_branch") && text.contains("integer"),
            "{text}"
        );

        let text = refused("git_excludes = \"abc\"\n").to_string();
        assert!(
            text.contains("git_excludes") && text.contains("string"),
            "{text}"
        );

        let text = refused("[git]\nuser = 1\n").to_string();
        assert!(
            text.contains("git.user") && text.contains("integer"),
            "{text}"
        );
    }

    #[test]
    fn a_git_that_is_not_a_table_is_refused_before_an_invalid_layout() {
        let error = refused("git = \"x\"\n[tui.dashboard]\nlayout = \"grid\"\n");

        assert!(
            matches!(
                &error,
                ConfigError::ValueType {
                    key: TypedKey::Git,
                    ..
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_git_user_is_refused_before_an_invalid_layout() {
        let error = refused("[git]\nuser = 1\n[tui.dashboard]\nlayout = \"grid\"\n");

        assert!(
            matches!(
                &error,
                ConfigError::ValueType {
                    key: TypedKey::GitUser,
                    ..
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_layout_is_refused_before_a_removed_key() {
        let error = refused("session_prefix = \"x\"\n[tui.dashboard]\nlayout = \"grid\"\n");

        assert!(
            matches!(
                &error,
                ConfigError::InvalidChoice {
                    key: ChoiceKey::DashboardLayout,
                    ..
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn a_removed_key_is_refused_before_an_invalid_adopt_extensions() {
        let error = refused("adopt_extensions = \"some\"\nsession_prefix = \"x\"\n");

        assert_removed(&error, RemovedKey::SessionPrefix, "service_prefix");
    }

    #[test]
    fn an_invalid_adopt_extensions_is_refused_before_a_main_branch() {
        let error = refused("main_branch = 3\nadopt_extensions = \"some\"\n");

        assert!(
            matches!(
                &error,
                ConfigError::InvalidChoice {
                    key: ChoiceKey::AdoptExtensions,
                    ..
                }
            ),
            "{error:?}"
        );
    }

    #[test]
    fn an_envs_per_workspace_out_of_range_is_refused_before_a_main_branch() {
        let error = refused("main_branch = 3\nenvs_per_workspace = 1\n");

        assert!(
            matches!(&error, ConfigError::EnvsPerWorkspaceTooSmall { .. }),
            "{error:?}"
        );
    }

    #[test]
    fn a_main_branch_is_refused_before_a_git_excludes_whichever_is_written_first() {
        for text in [
            "main_branch = 3\ngit_excludes = \"abc\"\n",
            "git_excludes = \"abc\"\nmain_branch = 3\n",
        ] {
            assert_eq!(
                value_type_refusal(text),
                (TypedKey::MainBranch, ValueType::Integer),
                "{text}"
            );
        }
    }

    #[test]
    fn unknown_keys_at_the_top_and_in_an_open_table_are_ignored() {
        let config = shared(
            "unheard_of = 1\n[git]\nunheard_of = [1]\nuser = { name = \"n\", email = \"e\", extra = 1 }\n\
             [tui.dashboard]\nlayout = \"list\"\nunheard_of = 1\n",
        );

        assert_eq!(config.envs_per_workspace, 48);
    }

    #[test]
    fn a_refusal_is_one_line() {
        for text in [
            "main_branch = 3\n",
            "git_excludes = [\"a\", 1]\n",
            "adopt_extensions = { a = 1, b = [1, 2] }\n",
            "[tui.dashboard]\nlayout = \"a\\nb\"\n",
            "service_orchestrator = \"tmux\"\n",
        ] {
            let message = refused(text).to_string();

            assert_eq!(message.lines().count(), 1, "{message}");
        }
    }

    /// The kind, the label, and the fault of the `Repository` refusal a load of `shared` ends with.
    fn repository_refusal(shared: &str) -> (RepoKind, String, RepositoryFault) {
        match refused(shared) {
            ConfigError::Repository { kind, label, fault } => (kind, label, fault),
            other => panic!("expected a repository refusal: {other:?}"),
        }
    }

    fn project(body: &str) -> String {
        format!("[[project_repository]]\nname = \"web\"\n{body}\n")
    }

    fn standalone(body: &str) -> String {
        format!("[[standalone_repository]]\nname = \"ctx\"\n{body}\n")
    }

    #[test]
    fn an_absolute_path_is_refused_naming_the_standalone() {
        let (kind, label, fault) = repository_refusal(&standalone("path = \"/abs\""));

        assert_eq!((kind, label.as_str()), (RepoKind::Standalone, "ctx"));
        assert_eq!(
            fault,
            RepositoryFault::Path {
                field: PathField::Path,
                value: "/abs".to_owned()
            }
        );
    }

    #[test]
    fn a_config_dir_with_a_parent_segment_is_refused() {
        let (kind, label, fault) = repository_refusal(&standalone("config_dir = \"a/../b\""));

        assert_eq!((kind, label.as_str()), (RepoKind::Standalone, "ctx"));
        assert_eq!(
            fault,
            RepositoryFault::Path {
                field: PathField::ConfigDir,
                value: "a/../b".to_owned()
            }
        );
    }

    #[test]
    fn a_path_and_a_config_dir_on_a_project_are_not_read() {
        shared(&project("path = \"/abs\"\nconfig_dir = \"../x\""));
    }

    #[test]
    fn an_empty_path_and_an_entry_of_the_repository_root_are_accepted() {
        shared(&standalone("path = \"\"\nentry = [\".\"]"));
    }

    #[test]
    fn the_prefix_of_a_skipped_standalone_does_not_collide() {
        shared("[[standalone_repository]]\nname = \"\"\nprefix = \"ws\"\n");
    }

    #[test]
    fn a_path_that_only_looks_like_a_parent_segment_is_accepted() {
        shared(&standalone("path = \"..x/y..\"\nconfig_dir = \"a/./b\""));
    }

    #[test]
    fn nested_true_on_a_standalone_is_refused() {
        let (kind, label, fault) = repository_refusal(&standalone("nested = true"));

        assert_eq!((kind, label.as_str()), (RepoKind::Standalone, "ctx"));
        assert_eq!(fault, RepositoryFault::StandaloneNested);
    }

    #[test]
    fn envs_and_inherit_local_without_nested_are_refused() {
        for (body, key) in [
            ("envs = 2", NestedOnlyKey::Envs),
            ("nested = false\nenvs = 2", NestedOnlyKey::Envs),
            ("inherit_local = [\"git\"]", NestedOnlyKey::InheritLocal),
        ] {
            let (kind, label, fault) = repository_refusal(&project(body));

            assert_eq!((kind, label.as_str()), (RepoKind::Project, "web"), "{body}");
            assert_eq!(fault, RepositoryFault::NestedOnly { key }, "{body}");
        }
        let (kind, _, fault) = repository_refusal(&standalone("envs = 2"));
        assert_eq!(kind, RepoKind::Standalone);
        assert_eq!(
            fault,
            RepositoryFault::NestedOnly {
                key: NestedOnlyKey::Envs
            }
        );
    }

    #[test]
    fn load_and_entry_on_a_nested_entry_are_refused() {
        for (body, key) in [
            ("nested = true\nload = \"eager\"", NestedExcludedKey::Load),
            (
                "nested = true\nentry = [\"a.py\"]",
                NestedExcludedKey::Entry,
            ),
        ] {
            let (kind, label, fault) = repository_refusal(&project(body));

            assert_eq!((kind, label.as_str()), (RepoKind::Project, "web"), "{body}");
            assert_eq!(fault, RepositoryFault::NestedWith { key }, "{body}");
        }
    }

    #[test]
    fn extension_true_on_a_nested_entry_is_refused_and_false_is_accepted() {
        let (kind, label, fault) = repository_refusal(&project("nested = true\nextension = true"));

        assert_eq!((kind, label.as_str()), (RepoKind::Project, "web"));
        assert_eq!(fault, RepositoryFault::NestedWithExtensionTrue);
        shared(&project(
            "nested = true\nextension = false\nenvs = 3\ninherit_local = [\"git\"]",
        ));
    }

    #[test]
    fn an_extension_and_a_nested_that_are_no_booleans_are_refused_with_their_value() {
        let (kind, label, fault) = repository_refusal(&project("extension = \"yes\""));
        assert_eq!((kind, label.as_str()), (RepoKind::Project, "web"));
        assert_eq!(
            fault,
            RepositoryFault::Extension {
                value: ConfigValue::new(&Value::String("yes".to_owned()))
            }
        );

        let (kind, label, fault) = repository_refusal(&standalone("nested = 1"));
        assert_eq!((kind, label.as_str()), (RepoKind::Standalone, "ctx"));
        assert_eq!(
            fault,
            RepositoryFault::Nested {
                value: ConfigValue::new(&Value::Integer(1))
            }
        );
    }

    #[test]
    fn a_load_other_than_eager_lazy_or_none_is_refused() {
        for load in ["eager", "lazy", "none"] {
            shared(&project(&format!("load = \"{load}\"")));
        }
        for body in ["load = \"soon\"", "load = 1", "load = true"] {
            let (_, _, fault) = repository_refusal(&project(body));

            assert!(
                matches!(fault, RepositoryFault::Load { .. }),
                "{body}: {fault:?}"
            );
        }
        let (kind, label, fault) = repository_refusal(&project("load = \"soon\""));
        assert_eq!((kind, label.as_str()), (RepoKind::Project, "web"));
        assert_eq!(
            fault,
            RepositoryFault::Load {
                value: ConfigValue::new(&Value::String("soon".to_owned()))
            }
        );
    }

    #[test]
    fn an_entry_that_is_no_list_or_an_empty_list_is_refused() {
        for body in ["entry = []", "entry = \"a.py\"", "entry = { a = 1 }"] {
            let (kind, label, fault) = repository_refusal(&standalone(body));

            assert_eq!(
                (kind, label.as_str()),
                (RepoKind::Standalone, "ctx"),
                "{body}"
            );
            assert!(
                matches!(fault, RepositoryFault::EntryList { .. }),
                "{body}: {fault:?}"
            );
        }
    }

    #[test]
    fn an_entry_path_that_is_empty_or_no_string_is_refused() {
        for body in ["entry = [\"\"]", "entry = [\"a\", 1]"] {
            let (kind, label, fault) = repository_refusal(&project(body));

            assert_eq!((kind, label.as_str()), (RepoKind::Project, "web"), "{body}");
            assert!(
                matches!(fault, RepositoryFault::EntryPathEmpty { .. }),
                "{body}: {fault:?}"
            );
        }
    }

    #[test]
    fn an_entry_path_that_is_absolute_or_has_a_parent_segment_is_refused() {
        for path in ["../x", "a/../x", "/x"] {
            let (kind, label, fault) =
                repository_refusal(&project(&format!("entry = [\"ok\", \"{path}\"]")));

            assert_eq!((kind, label.as_str()), (RepoKind::Project, "web"), "{path}");
            assert_eq!(
                fault,
                RepositoryFault::EntryPathEscapes {
                    value: path.to_owned()
                },
                "{path}"
            );
        }
        shared(&project("entry = [\"a/b.py\", \"c\"]"));
    }

    #[test]
    fn an_envs_other_than_an_integer_of_at_least_one_is_refused_on_a_nested_entry() {
        for body in [
            "envs = 0",
            "envs = -1",
            "envs = true",
            "envs = \"2\"",
            "envs = 1.5",
        ] {
            let (kind, label, fault) =
                repository_refusal(&project(&format!("nested = true\n{body}")));

            assert_eq!((kind, label.as_str()), (RepoKind::Project, "web"), "{body}");
            assert!(
                matches!(fault, RepositoryFault::Envs { .. }),
                "{body}: {fault:?}"
            );
        }
        shared(&project("nested = true\nenvs = 1"));
    }

    #[test]
    fn an_inherit_local_that_is_not_a_list_of_plain_names_is_refused_on_a_nested_entry() {
        for body in [
            "inherit_local = [\"a.b\"]",
            "inherit_local = [\"\"]",
            "inherit_local = [1]",
            "inherit_local = \"git\"",
        ] {
            let (kind, label, fault) =
                repository_refusal(&project(&format!("nested = true\n{body}")));

            assert_eq!((kind, label.as_str()), (RepoKind::Project, "web"), "{body}");
            assert!(
                matches!(fault, RepositoryFault::InheritLocal { .. }),
                "{body}: {fault:?}"
            );
        }
        shared(&project("nested = true\ninherit_local = []"));
    }

    /// The refusal of the key, the owner, and the type found, of a load of `shared`.
    fn repository_value_type_refusal(shared: &str) -> (TypedKey, RepoKind, String, ValueType) {
        match refused(shared) {
            ConfigError::ValueType {
                key,
                owner: Some(RepoRef { kind, label }),
                path: None,
                found,
            } => (key, kind, label, found),
            other => panic!("expected a value type refusal naming a repository: {other:?}"),
        }
    }

    #[test]
    fn a_cmd_that_is_a_string_on_a_project_is_refused_naming_the_repository() {
        assert_eq!(
            repository_value_type_refusal(&project("cmd = \"make\"")),
            (
                TypedKey::Cmd,
                RepoKind::Project,
                "web".to_owned(),
                ValueType::String
            )
        );
    }

    #[test]
    fn a_ref_that_is_an_integer_on_a_standalone_is_refused_naming_the_repository() {
        assert_eq!(
            repository_value_type_refusal(&standalone("ref = 1")),
            (
                TypedKey::Ref,
                RepoKind::Standalone,
                "ctx".to_owned(),
                ValueType::Integer
            )
        );
    }

    #[test]
    fn an_empty_cmd_and_an_empty_git_excludes_load() {
        shared(&project("cmd = []\ngit_excludes = []"));
        shared(&standalone("cmd = []"));
    }

    #[test]
    fn every_typed_field_of_an_entry_is_refused_for_a_wrong_type() {
        let cases = [
            (
                project("main_branch = 1"),
                TypedKey::MainBranch,
                ValueType::Integer,
            ),
            (
                project("git_excludes = \"a\""),
                TypedKey::GitExcludes,
                ValueType::String,
            ),
            (
                project("cmd = [\"a\", 1]"),
                TypedKey::Cmd,
                ValueType::Integer,
            ),
            (project("cmd = {}"), TypedKey::Cmd, ValueType::Table),
            (standalone("path = 1"), TypedKey::Path, ValueType::Integer),
            (
                standalone("prefix = true"),
                TypedKey::Prefix,
                ValueType::Boolean,
            ),
            (
                standalone("config_dir = [\"a\"]"),
                TypedKey::ConfigDir,
                ValueType::Array,
            ),
            (
                "[[project_repository]]\nname = 7\n".to_owned(),
                TypedKey::Name,
                ValueType::Integer,
            ),
            (
                "[[project_repository]]\nname = \"web\"\nurl = 7\n".to_owned(),
                TypedKey::Url,
                ValueType::Integer,
            ),
            (
                "[[standalone_repository]]\nname = true\n".to_owned(),
                TypedKey::Name,
                ValueType::Boolean,
            ),
        ];
        for (text, key, found) in cases {
            let (refused_key, _, _, refused_found) = repository_value_type_refusal(&text);

            assert_eq!((refused_key, refused_found), (key, found), "{text}");
        }
    }

    #[test]
    fn a_main_branch_that_is_an_empty_form_is_still_refused_on_an_entry() {
        assert_eq!(
            repository_value_type_refusal(&project("main_branch = false")).0,
            TypedKey::MainBranch
        );
    }

    #[test]
    fn a_repository_array_that_is_true_or_a_non_zero_number_is_refused() {
        for (text, key, found) in [
            (
                "project_repository = true\n",
                TypedKey::ProjectRepository,
                ValueType::Boolean,
            ),
            (
                "standalone_repository = 1\n",
                TypedKey::StandaloneRepository,
                ValueType::Integer,
            ),
            (
                "project_repository = 1.5\n",
                TypedKey::ProjectRepository,
                ValueType::Float,
            ),
            (
                "standalone_repository = 1979-05-27\n",
                TypedKey::StandaloneRepository,
                ValueType::Datetime,
            ),
        ] {
            assert_eq!(value_type_refusal(text), (key, found), "{text}");
        }
    }

    #[test]
    fn the_other_values_of_a_repository_array_read_as_no_entries() {
        for value in ["false", "0", "0.0", "\"\"", "\"abc\"", "{ a = 1 }"] {
            shared(&format!(
                "project_repository = {value}\nstandalone_repository = {value}\n"
            ));
        }
    }

    #[test]
    fn two_standalones_resolving_to_one_name_are_refused_by_name_and_by_url() {
        let text = "[[standalone_repository]]\nurl = \"https://host/org/ctx.git\"\n\
                    [[standalone_repository]]\nname = \"ctx\"\n";
        match refused(text) {
            ConfigError::DuplicateStandaloneName {
                name,
                first,
                second,
            } => {
                assert_eq!(name, "ctx");
                assert_eq!(
                    first,
                    EntryIdent::Url("https://host/org/ctx.git".to_owned())
                );
                assert_eq!(second, EntryIdent::Name("ctx".to_owned()));
            }
            other => panic!("expected a duplicate: {other:?}"),
        }
    }

    #[test]
    fn standalone_urls_without_a_slash_resolve_by_their_last_colon() {
        let text = "[[standalone_repository]]\nurl = \"host:ctx.git\"\n\
                    [[standalone_repository]]\nname = \"ctx\"\n";

        assert!(matches!(
            refused(text),
            ConfigError::DuplicateStandaloneName { ref name, .. } if name == "ctx"
        ));
    }

    #[test]
    fn an_empty_name_resolves_from_the_url() {
        let text = "[[standalone_repository]]\nname = \"\"\nurl = \"git@host:org/ctx/\"\n\
                    [[standalone_repository]]\nurl = \"https://host/ctx.git\"\n";

        assert!(matches!(
            refused(text),
            ConfigError::DuplicateStandaloneName { ref name, .. } if name == "ctx"
        ));
    }

    #[test]
    fn standalones_in_the_two_files_that_resolve_to_one_name_are_refused() {
        let error = refused_layered(
            "[[standalone_repository]]\nname = \"ctx\"\n",
            "[[standalone_repository]]\nurl = \"h:ctx\"\n",
        );

        assert!(
            matches!(error, ConfigError::DuplicateStandaloneName { .. }),
            "{error:?}"
        );
    }

    #[test]
    fn two_projects_of_one_name_load_and_a_project_does_not_collide_with_a_standalone() {
        shared("[[project_repository]]\nname = \"web\"\n[[project_repository]]\nname = \"web\"\n");
        shared(
            "[[project_repository]]\nname = \"ctx\"\n[[standalone_repository]]\nname = \"ctx\"\n",
        );
    }

    #[test]
    fn a_standalone_prefix_equal_to_the_default_workspace_prefix_is_a_collision() {
        match refused(&standalone("prefix = \"ws\"")) {
            ConfigError::PrefixCollision { prefix, standalone } => {
                assert_eq!((prefix.as_str(), standalone.as_str()), ("ws", "ctx"));
            }
            other => panic!("expected a prefix collision: {other:?}"),
        }
    }

    #[test]
    fn a_standalone_prefix_equal_to_the_declared_workspace_prefix_is_a_collision() {
        let text = format!("prefix = \"skills\"\n{}", standalone("prefix = \"skills\""));

        assert!(matches!(
            refused(&text),
            ConfigError::PrefixCollision { .. }
        ));
        shared(&format!(
            "prefix = \"mine\"\n{}",
            standalone("prefix = \"ws\"")
        ));
    }

    #[test]
    fn a_workspace_prefix_that_is_no_string_or_is_empty_is_the_default() {
        for value in ["1", "\"\"", "true"] {
            let text = format!("prefix = {value}\n{}", standalone("prefix = \"ws\""));

            assert!(
                matches!(refused(&text), ConfigError::PrefixCollision { .. }),
                "{value}"
            );
        }
    }

    #[test]
    fn the_prefix_collision_is_reported_at_step_7_after_the_slot_count_and_before_main_branch() {
        let slots = "env_aliases = [\"a\"]\nenvs_per_workspace = 1\n";
        let text = format!("{slots}{}", standalone("prefix = \"ws\""));
        assert!(matches!(
            refused(&text),
            ConfigError::EnvsPerWorkspaceTooSmall { .. }
        ));

        let text = format!("main_branch = 3\n{}", standalone("prefix = \"ws\""));
        assert!(matches!(
            refused(&text),
            ConfigError::PrefixCollision { .. }
        ));
    }

    #[test]
    fn elements_that_are_no_table_and_entries_without_name_and_url_are_skipped() {
        let text = "project_repository = [\"web\", 1, [\"x\"], {}, { name = \"\" }, { url = \"\" }, \
                    { name = 0 }, { name = false, url = [] }, { main_branch = 3 }]\n\
                    standalone_repository = [\"ctx\", { name = \"\", path = \"/abs\" }]\n";

        shared(text);
    }

    #[test]
    fn an_entry_with_a_name_that_reads_empty_but_is_no_string_and_a_url_is_refused() {
        assert_eq!(
            repository_value_type_refusal("[[project_repository]]\nname = 0\nurl = \"u\"\n").0,
            TypedKey::Name
        );
    }

    #[test]
    fn an_entry_is_labelled_by_its_url_when_it_has_no_name() {
        let (kind, label, _) = repository_refusal(
            "[[standalone_repository]]\nurl = \"https://h/ctx.git\"\nnested = true\n",
        );

        assert_eq!(
            (kind, label.as_str()),
            (RepoKind::Standalone, "https://h/ctx.git")
        );
    }

    #[test]
    fn a_faulty_project_entry_is_reported_before_a_faulty_standalone_entry_whichever_is_written_first()
     {
        let project = project("extension = 1");
        let standalone = standalone("path = \"/abs\"");
        for text in [
            format!("{project}{standalone}"),
            format!("{standalone}{project}"),
        ] {
            let (kind, label, _) = repository_refusal(&text);

            assert_eq!((kind, label.as_str()), (RepoKind::Project, "web"), "{text}");
        }
    }

    #[test]
    fn the_first_faulty_entry_of_an_array_is_reported() {
        let text = format!(
            "{}{}",
            project("extension = 1"),
            "[[project_repository]]\nname = \"api\"\nload = \"x\"\n"
        );

        let (_, label, fault) = repository_refusal(&text);

        assert_eq!(label, "web");
        assert!(matches!(fault, RepositoryFault::Extension { .. }));
    }

    #[test]
    fn the_repository_entries_are_refused_before_the_git_identity() {
        let text = format!("git = 1\n{}", project("extension = 1"));

        assert!(matches!(refused(&text), ConfigError::Repository { .. }));
    }

    #[test]
    fn a_refusal_of_an_entry_names_the_repository_in_one_line() {
        for text in [
            standalone("path = \"/abs\""),
            standalone("ref = 1"),
            project("entry = [\"a\\nb\", \"..\"]"),
            "[[project_repository]]\nname = 7\n".to_owned(),
            "[[standalone_repository]]\nname = \"c\\nx\"\nref = 1\n".to_owned(),
        ] {
            let message = refused(&text).to_string();

            assert_eq!(message.lines().count(), 1, "{message}");
            assert!(
                message.contains("ctx")
                    || message.contains("web")
                    || message.contains('7')
                    || message.contains("c\\nx"),
                "{message}"
            );
        }
    }

    /// The band, the key, and the fault of the env refusal a load of `vars` under `[env.<band>.vars]` ends with.
    fn env_refusal(band: &str, vars: &str) -> (String, String, EnvVarFault) {
        env_refusal_of(&format!("[env.{band}.vars]\n{vars}\n"))
    }

    /// The band, the key, and the fault of the env refusal a load of `text` ends with.
    fn env_refusal_of(text: &str) -> (String, String, EnvVarFault) {
        match refused(text) {
            ConfigError::EnvVar { band, key, fault } => (band, key, fault),
            other => panic!("expected an env band refusal: {other:?}"),
        }
    }

    fn fault_of(band: &str, key: &str, fault: EnvVarFault) -> (String, String, EnvVarFault) {
        (band.to_owned(), key.to_owned(), fault)
    }

    #[test]
    fn a_boolean_or_an_array_or_a_datetime_is_an_unsupported_type_naming_the_band_and_the_key() {
        for (vars, found) in [
            ("X = true", ValueType::Boolean),
            ("X = [1]", ValueType::Array),
            ("X = 1979-05-27", ValueType::Datetime),
        ] {
            assert_eq!(
                env_refusal("workspace", vars),
                fault_of("workspace", "X", EnvVarFault::UnsupportedType { found })
            );
        }
    }

    #[test]
    fn a_command_entry_with_a_field_beyond_the_closed_set_is_an_unknown_fields_fault() {
        assert_eq!(
            env_refusal("feature", "X = { command = \"c\", extra = 1, more = 2 }"),
            fault_of(
                "feature",
                "X",
                EnvVarFault::UnknownFields {
                    fields: strings(&["extra", "more"])
                }
            )
        );
    }

    #[test]
    fn the_unknown_fields_of_a_command_entry_are_listed_sorted() {
        let (_, _, fault) = env_refusal("alpha", "X = { zz = 1, command = \"c\", aa = 2, mm = 3 }");

        assert_eq!(
            fault,
            EnvVarFault::UnknownFields {
                fields: strings(&["aa", "mm", "zz"])
            }
        );
    }

    #[test]
    fn a_command_entry_without_a_command_or_with_an_empty_or_non_string_one_is_missing_its_command()
    {
        for vars in [
            "X = { format = \"raw\" }",
            "X = { command = \"\" }",
            "X = { command = 1 }",
            "X = {}",
        ] {
            assert_eq!(
                env_refusal("alpha", vars),
                fault_of("alpha", "X", EnvVarFault::MissingCommand),
                "{vars}"
            );
        }
    }

    #[test]
    fn a_format_outside_raw_dotenv_and_json_is_an_invalid_format_carrying_the_value() {
        for (vars, value) in [
            ("X = { command = \"c\", format = \"yaml\" }", "\"yaml\""),
            ("X = { command = \"c\", format = 1 }", "1"),
        ] {
            let (band, key, fault) = env_refusal("alpha", vars);

            assert_eq!((band.as_str(), key.as_str()), ("alpha", "X"));
            assert!(
                matches!(&fault, EnvVarFault::InvalidFormat { value: found } if found.to_string() == value),
                "{fault:?}"
            );
        }
    }

    #[test]
    fn a_shell_that_is_no_boolean_is_a_shell_not_boolean_fault() {
        assert_eq!(
            env_refusal("workspace", "X = { command = \"c\", shell = \"yes\" }"),
            fault_of(
                "workspace",
                "X",
                EnvVarFault::ShellNotBoolean {
                    found: ValueType::String
                }
            )
        );
    }

    #[test]
    fn an_exports_that_is_no_array_of_strings_is_an_exports_not_string_list_fault() {
        for vars in [
            "X = { command = \"c\", exports = \"A\" }",
            "X = { command = \"c\", exports = [\"A\", 1] }",
        ] {
            assert_eq!(
                env_refusal("feature", vars),
                fault_of("feature", "X", EnvVarFault::ExportsNotStringList),
                "{vars}"
            );
        }
    }

    #[test]
    fn a_command_entry_under_a_winter_key_is_a_managed_name_fault() {
        assert_eq!(
            env_refusal("alpha", "WINTER_X = { command = \"c\" }"),
            fault_of("alpha", "WINTER_X", EnvVarFault::ManagedName)
        );
    }

    #[test]
    fn a_managed_name_is_met_before_the_other_faults_of_the_entry() {
        let (_, _, fault) = env_refusal("alpha", "WINTER_X = { extra = 1 }");

        assert_eq!(fault, EnvVarFault::ManagedName);
    }

    #[test]
    fn strings_numbers_and_command_entries_load_under_every_kind_of_band() {
        for band in ["workspace", "feature", "alpha"] {
            for vars in [
                "X = \"a\"",
                "X = 3",
                "X = 1.5",
                "X = { command = \"c\" }",
                "X = { command = \"c\", format = \"json\", shell = true, exports = [\"A\"] }",
                "X = { command = \"c\", format = \"dotenv\", exports = [] }",
            ] {
                shared(&format!("[env.{band}.vars]\n{vars}\n"));
            }
        }
    }

    #[test]
    fn a_plain_string_under_a_winter_key_loads() {
        shared("[env.feature.vars]\nWINTER_X = \"a\"\n");
    }

    #[test]
    fn an_env_that_holds_nothing_to_read_loads() {
        for text in [
            "env = 1\n",
            "[env]\nalpha = 1\nworkspace = \"x\"\n",
            "[env.alpha]\nvars = 1\n",
            "[env.alpha]\nother = true\n",
            "[env.feature]\n",
        ] {
            shared(text);
        }
    }

    #[test]
    fn the_removed_env_vars_table_lists_its_keys_and_names_both_replacements() {
        let error = refused("[env.vars]\nA = \"1\"\nB = \"2\"\n");

        assert!(
            matches!(
                &error,
                ConfigError::RemovedKey(RemovedKey::EnvVars { keys }) if *keys == strings(&["A", "B"])
            ),
            "{error:?}"
        );
        let text = error.to_string();
        for needed in [
            "[env.vars]",
            "\"A\"",
            "\"B\"",
            "[env.feature.vars]",
            "[env.workspace.vars]",
        ] {
            assert!(text.contains(needed), "{text}");
        }
        assert_eq!(text.lines().count(), 1, "{text}");
    }

    #[test]
    fn an_env_vars_that_is_no_table_is_the_removed_table_with_no_keys() {
        let error = refused("[env]\nvars = 1\n");

        assert!(
            matches!(
                &error,
                ConfigError::RemovedKey(RemovedKey::EnvVars { keys }) if keys.is_empty()
            ),
            "{error:?}"
        );
        assert!(!error.to_string().contains("found keys"), "{error}");
    }

    #[test]
    fn an_env_vars_in_the_local_file_only_is_the_removed_table() {
        let error = refused_layered("# nothing\n", "[env.vars]\nA = \"1\"\n");

        assert!(
            matches!(&error, ConfigError::RemovedKey(RemovedKey::EnvVars { .. })),
            "{error:?}"
        );
    }

    #[test]
    fn the_removed_table_is_refused_before_a_faulty_band() {
        let error = refused("[env.workspace.vars]\nX = true\n[env.vars]\nA = \"1\"\n");

        assert!(
            matches!(&error, ConfigError::RemovedKey(RemovedKey::EnvVars { .. })),
            "{error:?}"
        );
    }

    #[test]
    fn the_removed_table_lists_its_keys_in_the_order_written() {
        let error = refused("[env.vars]\nB = \"2\"\nA = \"1\"\n");

        assert!(
            matches!(
                &error,
                ConfigError::RemovedKey(RemovedKey::EnvVars { keys }) if *keys == strings(&["B", "A"])
            ),
            "{error:?}"
        );
        let text = error.to_string();
        assert!(text.find("\"B\"") < text.find("\"A\""), "{text}");
    }

    #[test]
    fn two_faulty_named_bands_report_the_one_written_first() {
        let (band, _, _) =
            env_refusal_of("[env.zeta.vars]\nX = true\n[env.alpha.vars]\nX = true\n");

        assert_eq!(band, "zeta");
    }

    #[test]
    fn two_faulty_keys_of_one_band_report_the_one_written_first() {
        let (_, key, _) = env_refusal("alpha", "Z = true\nA = true");

        assert_eq!(key, "Z");
    }

    #[test]
    fn the_workspace_band_is_checked_before_the_feature_band_and_both_before_a_named_band() {
        let (band, _, _) = env_refusal_of(
            "[env.alpha.vars]\nX = true\n[env.feature.vars]\nX = true\n\
             [env.workspace.vars]\nX = true\n",
        );
        assert_eq!(band, "workspace");

        let (band, _, _) =
            env_refusal_of("[env.alpha.vars]\nX = true\n[env.feature.vars]\nX = true\n");
        assert_eq!(band, "feature");
    }

    #[test]
    fn a_faulty_band_is_refused_before_an_invalid_model_tier() {
        let (band, key, _) = env_refusal_of("[model_tiers.fast]\n\n[env.alpha.vars]\nX = true\n");

        assert_eq!((band.as_str(), key.as_str()), ("alpha", "X"));
    }

    #[test]
    fn a_faulty_band_is_refused_after_the_prefix_collision_and_before_a_main_branch() {
        let band = "[env.alpha.vars]\nX = true\n";
        let collision = format!(
            "prefix = \"ctx\"\n[[standalone_repository]]\nname = \"ctx\"\nprefix = \"ctx\"\n{band}"
        );
        assert!(matches!(
            refused(&collision),
            ConfigError::PrefixCollision { .. }
        ));

        let after = format!("main_branch = 1\ngit_excludes = 1\n{band}");
        assert!(matches!(refused(&after), ConfigError::EnvVar { .. }));
    }

    #[test]
    fn a_refusal_of_a_band_value_names_the_band_and_the_key_in_one_line() {
        let message = refused("[env.\"a\\nb\".vars]\n\"k\\nx\" = true\n").to_string();

        assert_eq!(message.lines().count(), 1, "{message}");
        assert!(
            message.contains("a\\nb") && message.contains("k\\nx"),
            "{message}"
        );
        assert!(message.contains("boolean"), "{message}");
    }

    /// The label and the fault of the model tier refusal a load of `text` ends with.
    fn tier_refusal(text: &str) -> (String, ModelTierFault) {
        match refused(text) {
            ConfigError::ModelTier { label, fault } => (label, fault),
            other => panic!("expected a model tier refusal: {other:?}"),
        }
    }

    /// The agent and the fault of the override refusal a load of `text` ends with.
    fn override_refusal(text: &str) -> (String, AgentOverrideFault) {
        match refused(text) {
            ConfigError::AgentOverride { agent, fault } => (agent, fault),
            other => panic!("expected an agent override refusal: {other:?}"),
        }
    }

    /// The fault of the override of the agent `a` that a load of `[agent_model_overrides]` holding `value` ends
    /// with.
    fn fault_of_override(value: &str) -> AgentOverrideFault {
        let (agent, fault) = override_refusal(&format!("[agent_model_overrides]\na = {value}\n"));
        assert_eq!(agent, "a");
        fault
    }

    fn vendor(name: &str) -> String {
        name.to_owned()
    }

    #[test]
    fn a_model_tier_that_is_no_table_is_not_a_vendor_table() {
        assert_eq!(
            tier_refusal("[model_tiers]\nfast = \"x\"\n"),
            (
                vendor("fast"),
                ModelTierFault::NotVendorTable {
                    found: ValueType::String
                }
            )
        );
    }

    #[test]
    fn a_model_tier_vendor_beyond_the_three_is_an_unknown_vendor() {
        assert_eq!(
            tier_refusal("[model_tiers]\nfast = { gemini = \"g\" }\n"),
            (
                vendor("fast"),
                ModelTierFault::UnknownVendor {
                    vendor: vendor("gemini")
                }
            )
        );
    }

    #[test]
    fn a_model_id_that_is_no_non_empty_string_is_refused_with_its_type() {
        for (cell, found) in [("1", ValueType::Integer), ("\"\"", ValueType::String)] {
            assert_eq!(
                tier_refusal(&format!("[model_tiers]\nfast = {{ claude = {cell} }}\n")),
                (
                    vendor("fast"),
                    ModelTierFault::ModelIdNotString {
                        vendor: vendor("claude"),
                        found
                    }
                ),
                "{cell}"
            );
        }
    }

    #[test]
    fn a_model_tier_without_a_vendor_is_refused() {
        assert_eq!(
            tier_refusal("[model_tiers]\nfast = {}\n"),
            (vendor("fast"), ModelTierFault::NoVendor)
        );
    }

    #[test]
    fn the_same_model_tier_fault_alone_is_refused() {
        assert_eq!(
            tier_refusal("[model_tiers.fast]\n"),
            (vendor("fast"), ModelTierFault::NoVendor)
        );
    }

    #[test]
    fn the_tier_that_is_met_first_in_a_label_decides_its_fault() {
        assert_eq!(
            tier_refusal("[model_tiers]\nfast = { gemini = 1, claude = 1 }\n").1,
            ModelTierFault::UnknownVendor {
                vendor: vendor("gemini")
            }
        );
        assert_eq!(
            tier_refusal("[model_tiers]\nfast = { claude = 1, gemini = \"g\" }\n").1,
            ModelTierFault::ModelIdNotString {
                vendor: vendor("claude"),
                found: ValueType::Integer
            }
        );
    }

    #[test]
    fn two_faulty_labels_report_the_one_written_first() {
        let (label, _) = tier_refusal("[model_tiers]\nzz = 1\naa = 1\n");

        assert_eq!(label, "zz");
    }

    #[test]
    fn a_label_ahead_of_a_faulty_one_that_is_valid_loads_up_to_the_fault() {
        let (label, _) = tier_refusal("[model_tiers]\nok = { claude = \"c\" }\nbad = {}\n");

        assert_eq!(label, "bad");
    }

    #[test]
    fn a_model_tiers_that_is_no_table_holds_no_label() {
        shared("model_tiers = 1\n");
        shared("model_tiers = \"x\"\n");
    }

    #[test]
    fn valid_tiers_load_whether_built_in_or_new_and_for_every_vendor() {
        shared(
            "[model_tiers]\nopus = { claude = \"c\" }\nnew = { claude = \"a\", codex = \"b\", opencode = \"c\" }\n",
        );
    }

    #[test]
    fn a_bare_string_that_is_no_tier_lists_the_four_built_in_labels_sorted() {
        let fault = fault_of_override("\"turbo\"");

        assert_eq!(
            fault,
            AgentOverrideFault::UnknownTier {
                label: vendor("turbo"),
                known: strings(&["fable", "haiku", "opus", "sonnet"])
            }
        );
    }

    #[test]
    fn the_known_labels_hold_the_declared_tiers_in_sorted_order() {
        let (_, fault) = override_refusal(
            "[model_tiers]\nzeta = { claude = \"z\" }\nalpha = { claude = \"a\" }\n\
             [agent_model_overrides]\na = \"turbo\"\n",
        );

        assert_eq!(
            fault,
            AgentOverrideFault::UnknownTier {
                label: vendor("turbo"),
                known: strings(&["alpha", "fable", "haiku", "opus", "sonnet", "zeta"])
            }
        );
    }

    #[test]
    fn the_known_labels_hold_a_label_declared_only_in_the_local_file_and_a_built_in_label_once() {
        let refusal = refused_layered(
            "[model_tiers]\nopus = { claude = \"o\" }\n[agent_model_overrides]\na = \"turbo\"\n",
            "[model_tiers]\nfast = { codex = \"f\" }\n",
        );

        assert!(
            matches!(
                &refusal,
                ConfigError::AgentOverride {
                    agent,
                    fault: AgentOverrideFault::UnknownTier { label, known },
                } if agent == "a"
                    && label == "turbo"
                    && *known == strings(&["fable", "fast", "haiku", "opus", "sonnet"])
            ),
            "{refusal:?}"
        );
    }

    #[test]
    fn an_empty_or_blank_value_is_an_empty_value() {
        for value in ["\"\"", "\"  \"", "\"\\t\\n\"", "\"\\u001f\""] {
            assert_eq!(
                fault_of_override(value),
                AgentOverrideFault::EmptyValue,
                "{value}"
            );
        }
    }

    #[test]
    fn a_value_of_the_wrong_type_is_refused_with_its_type() {
        for (value, found) in [
            ("1", ValueType::Integer),
            ("true", ValueType::Boolean),
            ("[\"opus\"]", ValueType::Array),
        ] {
            assert_eq!(
                fault_of_override(value),
                AgentOverrideFault::WrongType { found },
                "{value}"
            );
        }
    }

    #[test]
    fn an_override_vendor_beyond_the_three_is_an_unknown_vendor() {
        assert_eq!(
            fault_of_override("{ gemini = \"g\" }"),
            AgentOverrideFault::UnknownVendor {
                vendor: vendor("gemini")
            }
        );
    }

    #[test]
    fn an_override_table_without_a_vendor_is_refused() {
        assert_eq!(fault_of_override("{}"), AgentOverrideFault::NoVendor);
    }

    #[test]
    fn an_empty_vendor_model_id_is_refused() {
        for value in ["{ claude = \"\" }", "{ claude = \" \" }"] {
            assert_eq!(
                fault_of_override(value),
                AgentOverrideFault::VendorModelId {
                    vendor: vendor("claude")
                },
                "{value}"
            );
        }
    }

    #[test]
    fn a_vendor_value_that_is_neither_a_string_nor_a_table_is_refused_with_its_type() {
        assert_eq!(
            fault_of_override("{ claude = 1 }"),
            AgentOverrideFault::VendorValueType {
                vendor: vendor("claude"),
                found: ValueType::Integer
            }
        );
    }

    #[test]
    fn profile_keys_beyond_model_and_effort_are_listed_sorted() {
        assert_eq!(
            fault_of_override("{ claude = { size = 1 } }"),
            AgentOverrideFault::UnknownProfileKeys {
                vendor: vendor("claude"),
                keys: strings(&["size"])
            }
        );
        assert_eq!(
            fault_of_override("{ claude = { zz = 1, model = \"m\", size = 1 } }"),
            AgentOverrideFault::UnknownProfileKeys {
                vendor: vendor("claude"),
                keys: strings(&["size", "zz"])
            }
        );
    }

    #[test]
    fn a_profile_model_or_effort_that_is_no_non_empty_string_is_refused() {
        for value in [
            "{ claude = { model = 1 } }",
            "{ claude = { model = \"\" } }",
            "{ claude = { model = \" \", effort = \"high\" } }",
        ] {
            assert_eq!(
                fault_of_override(value),
                AgentOverrideFault::ProfileModel {
                    vendor: vendor("claude")
                },
                "{value}"
            );
        }
        for value in [
            "{ claude = { effort = 1 } }",
            "{ claude = { effort = \"\" } }",
            "{ claude = { model = \"m\", effort = true } }",
        ] {
            assert_eq!(
                fault_of_override(value),
                AgentOverrideFault::ProfileEffort {
                    vendor: vendor("claude")
                },
                "{value}"
            );
        }
    }

    #[test]
    fn a_profile_with_neither_model_nor_effort_is_empty() {
        for name in ["claude", "codex"] {
            assert_eq!(
                fault_of_override(&format!("{{ {name} = {{}} }}")),
                AgentOverrideFault::ProfileEmpty {
                    vendor: vendor(name)
                },
                "{name}"
            );
        }
    }

    #[test]
    fn a_profile_model_is_met_before_a_profile_effort() {
        assert_eq!(
            fault_of_override("{ claude = { effort = 1, model = 1 } }"),
            AgentOverrideFault::ProfileModel {
                vendor: vendor("claude")
            }
        );
    }

    #[test]
    fn two_faulty_agents_report_the_one_written_first() {
        let (agent, _) = override_refusal("[agent_model_overrides]\nz = 1\na = 1\n");

        assert_eq!(agent, "z");
    }

    #[test]
    fn a_built_in_label_a_model_id_a_profile_and_every_vendor_load() {
        shared(
            "[agent_model_overrides]\na = \"opus\"\nb = \"haiku\"\n\
             c = { claude = \"x\", codex = { model = \"m\" }, opencode = { effort = \"e\", model = \"m\" } }\n",
        );
    }

    #[test]
    fn a_label_declared_in_model_tiers_is_known_to_an_override() {
        shared("[model_tiers]\nfast = { claude = \"c\" }\n[agent_model_overrides]\na = \"fast\"\n");
    }

    #[test]
    fn a_label_defined_only_in_the_local_file_is_known_to_an_override_in_the_shared_file() {
        layered(
            "[agent_model_overrides]\na = \"fast\"\n",
            "[model_tiers]\nfast = { claude = \"c\" }\n",
        );
    }

    #[test]
    fn a_tier_fault_is_refused_before_an_override_whichever_is_written_first() {
        let tier = "[model_tiers]\nfast = {}\n";
        let agent = "[agent_model_overrides]\na = 1\n";

        for text in [format!("{tier}{agent}"), format!("{agent}{tier}")] {
            assert!(
                matches!(refused(&text), ConfigError::ModelTier { .. }),
                "{text}"
            );
        }
    }

    #[test]
    fn an_override_is_refused_after_a_faulty_band_and_before_a_main_branch() {
        let agent = "[agent_model_overrides]\na = 1\n";
        let band = "[env.alpha.vars]\nX = true\n";

        assert!(matches!(
            refused(&format!("{agent}{band}")),
            ConfigError::EnvVar { .. }
        ));
        assert!(matches!(
            refused(&format!("main_branch = 1\ngit_excludes = 1\n{agent}")),
            ConfigError::AgentOverride { .. }
        ));
    }

    #[test]
    fn a_refusal_of_a_tier_or_an_override_is_one_line_naming_the_label_or_the_agent() {
        let tier = refused("[model_tiers.\"a\\nb\"]\n").to_string();
        let agent = refused("[agent_model_overrides]\n\"x\\ny\" = \"turbo\"\n").to_string();

        for (message, name) in [(&tier, "a\\nb"), (&agent, "x\\ny")] {
            assert_eq!(message.lines().count(), 1, "{message}");
            assert!(message.contains(name), "{message}");
        }
        assert!(
            agent.contains("turbo") && agent.contains("haiku"),
            "{agent}"
        );
    }

    /// The order of the load's checks, one test per pair of adjacent steps that can refuse. Each fault is one line
    /// of root keys, so both written orders of a pair are tried, and each fault is shown to refuse alone with
    /// an error other than the earlier step's. Step 3 (`capabilities`) refuses nothing, so steps 2 and 4 are
    /// adjacent; the checks inside one step are in the order the slices state, and are not pinned here.
    mod load_order {
        use super::*;

        const REPOSITORY: &str = "project_repository = [{ name = \"web\", extension = 1 }]";
        const GIT: &str = "git = 1";
        const LAYOUT: &str = "tui.dashboard.layout = \"grid\"";
        const REMOVED: &str = "session_prefix = \"x\"";
        const ADOPT: &str = "adopt_extensions = \"some\"";
        const SLOTS: &str = "envs_per_workspace = 1";
        const COLLISION: &str = "standalone_repository = [{ name = \"ctx\", prefix = \"ws\" }]";
        const BAND: &str = "env.alpha.vars.X = true";
        const MAIN_BRANCH: &str = "main_branch = 1";
        const GIT_EXCLUDES: &str = "git_excludes = \"abc\"";

        /// Asserts that a load of `earlier` and `later`, written in either order in one file, ends with the
        /// error `is_earlier` accepts, and that `later` alone ends with another.
        fn earlier_wins(earlier: &str, later: &str, is_earlier: impl Fn(&ConfigError) -> bool) {
            let alone = refused(earlier);
            assert!(is_earlier(&alone), "{earlier}: {alone:?}");
            let alone = refused(later);
            assert!(!is_earlier(&alone), "{later}: {alone:?}");
            for text in [
                format!("{earlier}\n{later}\n"),
                format!("{later}\n{earlier}\n"),
            ] {
                let error = refused(&text);
                assert!(is_earlier(&error), "{text}: {error:?}");
            }
        }

        fn file_read_of(error: &ConfigError, file: &str) -> bool {
            matches!(error, ConfigError::FileRead { path, .. } if path == Path::new(file))
        }

        #[test]
        fn the_shared_file_is_refused_before_the_local_file() {
            let files = MemoryFiles::default()
                .with(SHARED, b"= broken\n")
                .with(LOCAL, b"= broken\n");
            let error = load_from(&files).expect_err("a refusal");
            assert!(file_read_of(&error, SHARED), "{error:?}");

            let files = MemoryFiles::default()
                .unreadable(SHARED, ErrorKind::PermissionDenied)
                .with(LOCAL, b"= broken\n");
            let error = load_from(&files).expect_err("a refusal");
            assert!(
                matches!(
                    &error,
                    ConfigError::FileRead { path, cause: FileCause::Io(_) } if path == Path::new(SHARED)
                ),
                "{error:?}"
            );

            let files = MemoryFiles::default()
                .with(SHARED, b"")
                .with(LOCAL, b"= broken\n");
            let error = load_from(&files).expect_err("a refusal");
            assert!(file_read_of(&error, LOCAL), "{error:?}");
        }

        #[test]
        fn the_local_file_is_refused_before_the_overrides() {
            let files = MemoryFiles::default()
                .with(SHARED, b"agent_model_overrides = 1\n")
                .with(LOCAL, b"= broken\n");

            let error = load_from(&files).expect_err("a refusal");

            assert!(file_read_of(&error, LOCAL), "{error:?}");
            let files = MemoryFiles::default().with(SHARED, b"agent_model_overrides = 1\n");
            let error = load_from(&files).expect_err("a refusal");
            assert!(
                matches!(&error, ConfigError::AgentOverridesNotTable { .. }),
                "{error:?}"
            );
        }

        #[test]
        fn the_overrides_are_refused_before_the_merge() {
            let merge_fault = "git = \"x\"\n";
            let local_user = "[git]\nuser = { name = \"B\" }\n";
            let alone = refused_layered(merge_fault, local_user);
            assert!(
                matches!(
                    &alone,
                    ConfigError::ValueType { key: TypedKey::Git, path: Some(path), .. }
                        if path == Path::new(SHARED)
                ),
                "{alone:?}"
            );
            for (shared, local, file) in [
                (
                    format!("agent_model_overrides = 1\n{merge_fault}"),
                    local_user.to_owned(),
                    SHARED,
                ),
                (
                    merge_fault.to_owned(),
                    format!("agent_model_overrides = 1\n{local_user}"),
                    LOCAL,
                ),
            ] {
                let error = refused_layered(&shared, &local);
                assert!(
                    matches!(
                        &error,
                        ConfigError::AgentOverridesNotTable { path, .. } if path == Path::new(file)
                    ),
                    "{shared} / {local}: {error:?}"
                );
            }
        }

        #[test]
        fn the_merge_is_refused_before_the_repository_entries() {
            let shared = format!("git = \"x\"\n{REPOSITORY}\n");
            let local = "[git]\nuser = { name = \"B\" }\n";

            let error = refused_layered(&shared, local);

            assert!(
                matches!(
                    &error,
                    ConfigError::ValueType { key: TypedKey::Git, path: Some(path), .. }
                        if path == Path::new(SHARED)
                ),
                "{error:?}"
            );
            assert!(matches!(
                refused(REPOSITORY),
                ConfigError::Repository { .. }
            ));
        }

        #[test]
        fn the_repository_entries_are_refused_before_the_git_identity_and_the_layout() {
            earlier_wins(REPOSITORY, GIT, |error| {
                matches!(error, ConfigError::Repository { .. })
            });
            earlier_wins(REPOSITORY, LAYOUT, |error| {
                matches!(error, ConfigError::Repository { .. })
            });
        }

        #[test]
        fn the_git_identity_and_the_layout_are_refused_before_the_removed_keys() {
            earlier_wins(GIT, REMOVED, |error| {
                matches!(
                    error,
                    ConfigError::ValueType {
                        key: TypedKey::Git,
                        ..
                    }
                )
            });
            earlier_wins(LAYOUT, REMOVED, |error| {
                matches!(
                    error,
                    ConfigError::InvalidChoice {
                        key: ChoiceKey::DashboardLayout,
                        ..
                    }
                )
            });
        }

        #[test]
        fn the_removed_keys_are_refused_before_adopt_extensions() {
            earlier_wins(REMOVED, ADOPT, |error| {
                matches!(error, ConfigError::RemovedKey(RemovedKey::SessionPrefix))
            });
        }

        #[test]
        fn adopt_extensions_is_refused_before_the_slot_count() {
            earlier_wins(ADOPT, SLOTS, |error| {
                matches!(
                    error,
                    ConfigError::InvalidChoice {
                        key: ChoiceKey::AdoptExtensions,
                        ..
                    }
                )
            });
        }

        #[test]
        fn the_slot_count_is_refused_before_the_prefix_collision() {
            earlier_wins(SLOTS, COLLISION, |error| {
                matches!(error, ConfigError::EnvsPerWorkspaceTooSmall { .. })
            });
        }

        #[test]
        fn the_prefix_collision_is_refused_before_the_env_bands() {
            earlier_wins(COLLISION, BAND, |error| {
                matches!(error, ConfigError::PrefixCollision { .. })
            });
        }

        #[test]
        fn the_env_bands_are_refused_before_the_main_branch() {
            earlier_wins(BAND, MAIN_BRANCH, |error| {
                matches!(error, ConfigError::EnvVar { .. })
            });
        }

        #[test]
        fn the_main_branch_is_refused_before_git_excludes() {
            earlier_wins(MAIN_BRANCH, GIT_EXCLUDES, |error| {
                matches!(
                    error,
                    ConfigError::ValueType {
                        key: TypedKey::MainBranch,
                        ..
                    }
                )
            });
        }
    }
}
