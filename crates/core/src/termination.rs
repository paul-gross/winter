//! How an invocation ends, and the errors an ending carries.

use std::backtrace::Backtrace;
use std::error::Error;
use std::fmt;
use std::path::{Path, PathBuf};

use crate::config_edit::CatalogError;
use crate::workspace_files::CONFIG_FILE;

/// One value per way an invocation ends.
///
/// Commands return these and write only their result. The process boundary alone writes the final diagnostic of
/// a termination and maps its exit code.
#[derive(Debug)]
pub enum Termination {
    Success,
    /// The command's own result already carries the failure; nothing more is written.
    Reported,
    Usage(Box<UsageError>),
    /// No command was given at the root or in a group; the diagnostic is that node's help.
    BareGroup {
        help: String,
    },
    Command(Box<CommandError>),
    Config(Box<ConfigError>),
    /// The reader closed stdout before the result was written; nothing more is written.
    BrokenPipe,
    Internal(Box<InternalError>),
}

impl Termination {
    /// The process exit code of this ending.
    pub fn exit_code(&self) -> u8 {
        match self {
            Self::Success => 0,
            Self::Reported
            | Self::Command(_)
            | Self::Config(_)
            | Self::BrokenPipe
            | Self::Internal(_) => 1,
            Self::Usage(_) | Self::BareGroup { .. } => 2,
        }
    }

    /// The text the process boundary writes to its error stream, or `None` when nothing more is written.
    ///
    /// A bare group's text is its help. Every other text opens with a line that starts with the error marker
    /// and carries the message; a usage error adds its usage line and the command that prints its help.
    pub fn diagnostic(&self) -> Option<String> {
        match self {
            Self::Success | Self::Reported | Self::BrokenPipe => None,
            Self::Usage(error) => Some(error.to_string()),
            Self::BareGroup { help } => Some(help.clone()),
            Self::Command(error) => Some(format!("{ERROR_MARKER}{error}")),
            Self::Config(error) => Some(format!("{ERROR_MARKER}{error}")),
            Self::Internal(error) => Some(format!("{ERROR_MARKER}{error}")),
        }
    }

    /// The name of this ending for the `error.type` attribute of a trace span, or `None` for an ending
    /// whose exit code is 0.
    ///
    /// The name is the variant's, as a literal, and never holds anything of the variant's payload. The names
    /// are a closed vocabulary: a new variant adds its name here, and a name is never renamed or given to
    /// another ending.
    pub fn error_type(&self) -> Option<&'static str> {
        match self {
            Self::Success => None,
            Self::Reported => Some("Reported"),
            Self::Usage(_) => Some("Usage"),
            Self::BareGroup { .. } => Some("BareGroup"),
            Self::Command(_) => Some("Command"),
            Self::Config(_) => Some("Config"),
            Self::BrokenPipe => Some("BrokenPipe"),
            Self::Internal(_) => Some("Internal"),
        }
    }

    /// How an invocation ends on a write or a flush of stdout that failed with `error`.
    ///
    /// The one classifier of such a failure. A reader that closed the handle is no failure of the program: that
    /// ending has no diagnostic. Every other error is an internal failure that carries it.
    pub fn from_write_error(error: std::io::Error) -> Self {
        match error.kind() {
            std::io::ErrorKind::BrokenPipe => Self::BrokenPipe,
            _ => Self::Internal(Box::new(InternalError::unexpected(error))),
        }
    }
}

/// What opens the first line of every error diagnostic.
const ERROR_MARKER: &str = "error: ";

/// What a usage diagnostic shows of the command at fault.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Usage {
    /// The usage line of the node, as its `--help` prints it.
    pub line: String,
    /// The command line that prints the node's help: the program, the node's path, and `--help`.
    pub help_command: String,
}

/// A fault in how the command line was written.
///
/// Its text is the whole usage diagnostic, one part per line: the error line with the fault, the usage line of
/// the node at fault, and where to ask for that node's help.
#[derive(Debug, thiserror::Error)]
pub enum UsageError {
    /// Raised by the argument parser. `command_path` is the path parsed before the fault, and `usage` is that
    /// of the node at that path.
    #[error("{}{fault}\n{}\nFor help, run '{}'.", ERROR_MARKER, .usage.line, .usage.help_command)]
    Parse {
        command_path: Vec<String>,
        usage: Usage,
        fault: ParseFault,
    },
}

/// What the argument parser refused.
#[derive(Debug, thiserror::Error)]
pub enum ParseFault {
    #[error("unknown command '{name}'")]
    UnknownCommand { name: String },
    #[error("unknown option '{name}'")]
    UnknownOption { name: String },
    #[error("missing argument {metavar}")]
    MissingArgument { metavar: String },
    #[error("option '{option}' requires a value")]
    MissingOptionValue { option: String },
    #[error("invalid value '{value}' for {param}: expected {expected}")]
    InvalidValue {
        param: String,
        value: String,
        expected: String,
    },
    #[error("unexpected argument: {}", quoted(.values))]
    UnexpectedArgument { values: Vec<String> },
    /// A flag or a single-value option given more than once.
    #[error("option '{option}' is given more than once")]
    RepeatedOption { option: String },
    /// An argument of the command line that is not UTF-8. `argument` is its text with each invalid sequence
    /// replaced, and is rendered in quotes with its control characters escaped.
    #[error("argument is not valid UTF-8: {argument:?}")]
    NotUtf8 { argument: String },
}

/// Each value in quotes with its control characters escaped, so that an empty value shows and a value that
/// holds a line break stays on the error line.
fn quoted(values: &[String]) -> String {
    values
        .iter()
        .map(|value| format!("{value:?}"))
        .collect::<Vec<_>>()
        .join(" ")
}

/// A rule of a command that the invocation violates. Each variant wraps the error of the module that owns the
/// rule.
#[derive(Debug, thiserror::Error)]
pub enum CommandError {
    #[error(transparent)]
    Catalog(#[from] CatalogError),
}

/// A failure to locate, read, or accept the workspace configuration.
#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error(
        "no workspace found at or above {}: no directory from there upward holds {}",
        .start.display(),
        CONFIG_FILE
    )]
    RootNotFound { start: PathBuf },
    #[error("cannot load {}: {cause}", .path.display())]
    FileRead { path: PathBuf, cause: FileCause },
    /// A key the configuration no longer accepts.
    #[error(transparent)]
    RemovedKey(RemovedKey),
    /// `value` is not one of the choices of `key`.
    #[error("{key} is {value}, not one of {}", .key.choices())]
    InvalidChoice { key: ChoiceKey, value: ConfigValue },
    /// `[agent_model_overrides]` is not a table in the file at `path`.
    #[error("agent_model_overrides in {} is of type {found}, not table", .path.display())]
    AgentOverridesNotTable { path: PathBuf, found: ValueType },
    /// The value of `key` cannot be read as the kind it needs: `owner` is the repository whose field it is,
    /// and when `path` is set, the value is the shared file's, refused at the merge.
    #[error("{}", value_type_message(*.key, .owner.as_ref(), .path.as_deref(), *.found))]
    ValueType {
        key: TypedKey,
        owner: Option<RepoRef>,
        path: Option<PathBuf>,
        found: ValueType,
    },
    /// Two `[[standalone_repository]]` entries resolve to the same `name`.
    #[error("two standalone repositories are named {name:?}: {first} and {second}")]
    DuplicateStandaloneName {
        name: String,
        first: EntryIdent,
        second: EntryIdent,
    },
    /// The workspace `prefix` is also the `prefix` of the standalone repository `standalone`.
    #[error(
        "prefix {prefix:?} of the workspace is also the prefix of the standalone repository {standalone:?}"
    )]
    PrefixCollision { prefix: String, standalone: String },
    /// A repository entry that breaks a rule of its own fields.
    #[error("the {kind} repository {label:?}: {fault}")]
    Repository {
        kind: RepoKind,
        label: String,
        fault: RepositoryFault,
    },
    #[error(
        "envs_per_workspace is {envs_per_workspace}, too small for the {aliases} entries of env_aliases"
    )]
    EnvsPerWorkspaceTooSmall {
        envs_per_workspace: i64,
        aliases: usize,
    },
    /// A value of the `env` band `band` under `key` that breaks the rule `fault` names.
    #[error("the env band {band:?}: key {key:?} {fault}")]
    EnvVar {
        band: String,
        key: String,
        fault: EnvVarFault,
    },
    /// The `[model_tiers]` entry `label` breaks the rule `fault` names.
    #[error("the model tier {label:?}: {fault}")]
    ModelTier {
        label: String,
        fault: ModelTierFault,
    },
    /// The `[agent_model_overrides]` entry of `agent` breaks the rule `fault` names.
    #[error("the model override of the agent {agent:?}: {fault}")]
    AgentOverride {
        agent: String,
        fault: AgentOverrideFault,
    },
}

/// The rule of an env band value that a [`ConfigError::EnvVar`] refusal names.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum EnvVarFault {
    /// A value that is no string, integer, float, or command-entry table.
    #[error(
        "has a value of type {found}; only a string, an integer, a float, and a command-entry table are allowed"
    )]
    UnsupportedType { found: ValueType },
    /// A command entry with fields beyond `command`, `format`, `shell`, and `exports`, listed sorted.
    #[error(
        "has unknown field(s) {}; a command entry holds command, format, shell, and exports",
        .fields.join(", ")
    )]
    UnknownFields { fields: Vec<String> },
    /// A command entry under a key that starts with `WINTER_`.
    #[error(
        "is a command entry under a WINTER_ name; declare it as a plain string or use another key"
    )]
    ManagedName,
    #[error("is a command entry without a non-empty command string")]
    MissingCommand,
    #[error("has format {value}, not one of raw, dotenv, json")]
    InvalidFormat { value: ConfigValue },
    #[error("has shell of type {found}, not boolean")]
    ShellNotBoolean { found: ValueType },
    #[error("has exports that is not an array of strings")]
    ExportsNotStringList,
}

/// The rule of a `[model_tiers]` entry that a [`ConfigError::ModelTier`] refusal names.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum ModelTierFault {
    /// The label's value is no per-vendor table.
    #[error("is of type {found}, not a per-vendor table")]
    NotVendorTable { found: ValueType },
    /// A vendor other than `claude`, `codex`, and `opencode`.
    #[error("has the unknown vendor {vendor:?}; the vendors are claude, codex, and opencode")]
    UnknownVendor { vendor: String },
    /// A model id that is no non-empty string.
    #[error(
        "has for the vendor {vendor:?} a model id of type {found}; it must be a non-empty string"
    )]
    ModelIdNotString { vendor: String, found: ValueType },
    /// A per-vendor table with no vendor.
    #[error("has no vendor; it needs at least one")]
    NoVendor,
}

/// The rule of an `[agent_model_overrides]` entry that a [`ConfigError::AgentOverride`] refusal names.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum AgentOverrideFault {
    /// A bare string that is no label of the effective tier table; `known` lists the labels, sorted.
    #[error("{label:?} is not a tier label; the tier labels are {}", .known.join(", "))]
    UnknownTier { label: String, known: Vec<String> },
    /// A bare string that is empty or only whitespace.
    #[error("is empty; it must be a tier label or a per-vendor table")]
    EmptyValue,
    /// A value that is neither a string nor a table.
    #[error("is of type {found}; it must be a tier label or a per-vendor table")]
    WrongType { found: ValueType },
    /// A vendor other than `claude`, `codex`, and `opencode`.
    #[error("has the unknown vendor {vendor:?}; the vendors are claude, codex, and opencode")]
    UnknownVendor { vendor: String },
    /// A per-vendor table with no vendor.
    #[error("has no vendor; it needs at least one")]
    NoVendor,
    /// A model id that is empty or only whitespace.
    #[error("has for the vendor {vendor:?} an empty model id")]
    VendorModelId { vendor: String },
    /// A vendor value that is neither a string nor a profile table.
    #[error(
        "has for the vendor {vendor:?} a value of type {found}; it must be a model id or a profile table"
    )]
    VendorValueType { vendor: String, found: ValueType },
    /// A profile with keys beyond `model` and `effort`, listed sorted.
    #[error(
        "has for the vendor {vendor:?} a profile with unknown key(s) {}; a profile holds model and effort",
        .keys.join(", ")
    )]
    UnknownProfileKeys { vendor: String, keys: Vec<String> },
    /// A profile `model` that is no non-empty string.
    #[error("has for the vendor {vendor:?} a profile model that is not a non-empty string")]
    ProfileModel { vendor: String },
    /// A profile `effort` that is no non-empty string.
    #[error("has for the vendor {vendor:?} a profile effort that is not a non-empty string")]
    ProfileEffort { vendor: String },
    /// A profile with neither `model` nor `effort`.
    #[error("has for the vendor {vendor:?} a profile with neither model nor effort")]
    ProfileEmpty { vendor: String },
}

/// The words of a [`ConfigError::ValueType`] refusal.
fn value_type_message(
    key: TypedKey,
    owner: Option<&RepoRef>,
    path: Option<&Path>,
    found: ValueType,
) -> String {
    let wanted = match key {
        TypedKey::ProjectRepository | TypedKey::StandaloneRepository => "array",
        TypedKey::MainBranch
        | TypedKey::GitUserName
        | TypedKey::GitUserEmail
        | TypedKey::Ref
        | TypedKey::Name
        | TypedKey::Url
        | TypedKey::Prefix
        | TypedKey::Path
        | TypedKey::ConfigDir => "string",
        TypedKey::GitExcludes | TypedKey::Cmd => "array of strings",
        TypedKey::Git
        | TypedKey::GitUser
        | TypedKey::Keybindings
        | TypedKey::Tui
        | TypedKey::Capabilities
        | TypedKey::Env
        | TypedKey::ModelTiers => "table",
    };
    let subject = match owner {
        Some(owner) => format!("{key} of the {owner}"),
        None => key.to_string(),
    };
    match (key, path) {
        (TypedKey::GitExcludes | TypedKey::Cmd, _) => {
            format!("{subject} must be an array of strings, found {found}")
        }
        (_, Some(path)) => format!(
            "cannot layer {subject}: {} holds a value of type {found}, not {wanted}",
            path.display()
        ),
        (_, None) => format!("{subject} is of type {found}, not {wanted}"),
    }
}

/// Which array a repository entry stands in.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RepoKind {
    Project,
    Standalone,
}

impl fmt::Display for RepoKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Project => "project",
            Self::Standalone => "standalone",
        })
    }
}

/// How a repository entry names itself, for the message of a refusal: by its `name`, or by its `url` when it
/// has no `name`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EntryIdent {
    Name(String),
    Url(String),
}

impl fmt::Display for EntryIdent {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Name(name) => write!(f, "name = {name:?}"),
            Self::Url(url) => write!(f, "url = {url:?}"),
        }
    }
}

/// The repository entry a field belongs to: the array it stands in, and its `name`, or its `url` when it has no
/// `name`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RepoRef {
    pub kind: RepoKind,
    pub label: String,
}

impl fmt::Display for RepoRef {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} repository {:?}", self.kind, self.label)
    }
}

/// A repository field that holds a path inside the workspace.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PathField {
    Path,
    ConfigDir,
}

impl fmt::Display for PathField {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Path => "path",
            Self::ConfigDir => "config_dir",
        })
    }
}

/// A repository key that only a nested workspace may set.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NestedOnlyKey {
    Envs,
    InheritLocal,
}

impl fmt::Display for NestedOnlyKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Envs => "envs",
            Self::InheritLocal => "inherit_local",
        })
    }
}

/// A repository key that a nested workspace may not set.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NestedExcludedKey {
    Load,
    Entry,
}

impl fmt::Display for NestedExcludedKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Load => "load",
            Self::Entry => "entry",
        })
    }
}

/// The rule of a repository entry that a [`ConfigError::Repository`] refusal names.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum RepositoryFault {
    /// A `path` or `config_dir` that is absolute or holds a `..` segment.
    #[error("{field} {value:?} must be relative to the workspace root and hold no `..` segment")]
    Path { field: PathField, value: String },
    #[error(
        "nested = true is for a project repository: a standalone is one checkout for the whole workspace"
    )]
    StandaloneNested,
    /// `envs` or `inherit_local` on an entry that is not `nested = true`.
    #[error("{key} is for a repository with nested = true")]
    NestedOnly { key: NestedOnlyKey },
    /// `load` or `entry` on an entry with `nested = true`.
    #[error(
        "{key} is not for a repository with nested = true: a nested workspace is never an extension"
    )]
    NestedWith { key: NestedExcludedKey },
    #[error("extension = true is not for a repository with nested = true")]
    NestedWithExtensionTrue,
    #[error("extension is {value}, not true or false")]
    Extension { value: ConfigValue },
    #[error("nested is {value}, not true or false")]
    Nested { value: ConfigValue },
    #[error("load is {value}, not one of eager, lazy, none")]
    Load { value: ConfigValue },
    #[error("entry is {value}, not a non-empty array of repository-relative paths")]
    EntryList { value: ConfigValue },
    #[error("entry path {value} is not a non-empty string")]
    EntryPathEmpty { value: ConfigValue },
    #[error("entry path {value:?} must be relative to the repository and hold no `..` segment")]
    EntryPathEscapes { value: String },
    #[error("envs is {value}, not an integer of at least 1")]
    Envs { value: ConfigValue },
    #[error("inherit_local is {value}, not an array of names, each non-empty and without a `.`")]
    InheritLocal { value: ConfigValue },
}

/// A key the configuration no longer accepts, with the key that replaces it in its message.
#[derive(Debug, thiserror::Error)]
pub enum RemovedKey {
    #[error("service_orchestrator is no longer supported; set [capabilities] service instead")]
    ServiceOrchestrator,
    #[error("session_prefix is no longer supported; set service_prefix instead")]
    SessionPrefix,
    /// The table `[env.vars]`, with the keys it holds in the order written.
    #[error("{}", env_vars_message(.keys))]
    EnvVars { keys: Vec<String> },
}

/// The words of a [`RemovedKey::EnvVars`] refusal.
fn env_vars_message(keys: &[String]) -> String {
    let found = if keys.is_empty() {
        String::new()
    } else {
        let listed: Vec<String> = keys.iter().map(|key| format!("{key:?}")).collect();
        format!(" (found keys: {})", listed.join(", "))
    };
    format!(
        "[env.vars] is no longer supported{found}; set [env.feature.vars] or [env.workspace.vars] instead"
    )
}

/// A configuration key whose value must be one of a closed set of words.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChoiceKey {
    AdoptExtensions,
    DashboardLayout,
}

impl ChoiceKey {
    /// The words the key accepts, as the configuration file spells them.
    fn choices(self) -> &'static str {
        match self {
            Self::AdoptExtensions => "none, winter, all",
            Self::DashboardLayout => "auto, repos-as-columns, repos-as-rows, list",
        }
    }
}

impl fmt::Display for ChoiceKey {
    /// The key as the configuration file spells it.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::AdoptExtensions => "adopt_extensions",
            Self::DashboardLayout => "tui.dashboard.layout",
        })
    }
}

/// A parsed TOML value a refusal names. The value is held behind a pointer, so a refusal that names one
/// stays narrow.
#[derive(Debug, Clone, PartialEq)]
pub struct ConfigValue(Box<toml::Value>);

impl ConfigValue {
    pub(crate) fn new(value: &toml::Value) -> Self {
        Self(Box::new(value.clone()))
    }
}

impl fmt::Display for ConfigValue {
    /// The value as TOML writes it inline, on one line.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        inline(&self.0, f)
    }
}

/// Writes `value` as an inline TOML value: a string escaped, an array and a table on one line.
fn inline(value: &toml::Value, f: &mut fmt::Formatter<'_>) -> fmt::Result {
    match value {
        toml::Value::String(text) => write!(f, "{text:?}"),
        toml::Value::Array(items) => {
            f.write_str("[")?;
            for (position, item) in items.iter().enumerate() {
                if position > 0 {
                    f.write_str(", ")?;
                }
                inline(item, f)?;
            }
            f.write_str("]")
        }
        toml::Value::Table(table) if table.is_empty() => f.write_str("{}"),
        toml::Value::Table(table) => {
            f.write_str("{ ")?;
            for (position, (key, item)) in table.iter().enumerate() {
                if position > 0 {
                    f.write_str(", ")?;
                }
                write!(f, "{key:?} = ")?;
                inline(item, f)?;
            }
            f.write_str(" }")
        }
        scalar => write!(f, "{scalar}"),
    }
}

/// A configuration key whose value a [`ConfigError::ValueType`] refusal names.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TypedKey {
    GitExcludes,
    Cmd,
    MainBranch,
    Ref,
    Name,
    Url,
    Prefix,
    Path,
    ConfigDir,
    ProjectRepository,
    StandaloneRepository,
    Git,
    GitUser,
    GitUserName,
    GitUserEmail,
    Keybindings,
    Tui,
    Capabilities,
    Env,
    ModelTiers,
}

impl fmt::Display for TypedKey {
    /// The key as the configuration file spells it.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::GitExcludes => "git_excludes",
            Self::Cmd => "cmd",
            Self::MainBranch => "main_branch",
            Self::Ref => "ref",
            Self::Name => "name",
            Self::Url => "url",
            Self::Prefix => "prefix",
            Self::Path => "path",
            Self::ConfigDir => "config_dir",
            Self::ProjectRepository => "project_repository",
            Self::StandaloneRepository => "standalone_repository",
            Self::Git => "git",
            Self::GitUser => "git.user",
            Self::GitUserName => "git.user.name",
            Self::GitUserEmail => "git.user.email",
            Self::Keybindings => "keybindings",
            Self::Tui => "tui",
            Self::Capabilities => "capabilities",
            Self::Env => "env",
            Self::ModelTiers => "model_tiers",
        })
    }
}

/// A TOML value's type.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ValueType {
    Boolean,
    Integer,
    Float,
    String,
    Array,
    Table,
    Datetime,
}

impl ValueType {
    /// The type of `value`.
    pub(crate) fn of(value: &toml::Value) -> Self {
        match value {
            toml::Value::Boolean(_) => Self::Boolean,
            toml::Value::Integer(_) => Self::Integer,
            toml::Value::Float(_) => Self::Float,
            toml::Value::String(_) => Self::String,
            toml::Value::Array(_) => Self::Array,
            toml::Value::Table(_) => Self::Table,
            toml::Value::Datetime(_) => Self::Datetime,
        }
    }
}

impl fmt::Display for ValueType {
    /// TOML's own name for the type.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Boolean => "boolean",
            Self::Integer => "integer",
            Self::Float => "float",
            Self::String => "string",
            Self::Array => "array",
            Self::Table => "table",
            Self::Datetime => "datetime",
        })
    }
}

/// Why a TOML file could not be loaded.
#[derive(Debug, thiserror::Error)]
pub enum FileCause {
    /// The file exists but cannot be read.
    #[error(transparent)]
    Io(#[from] std::io::Error),
    /// The file was read but is not valid TOML.
    #[error(transparent)]
    Toml(#[from] TomlParseError),
}

/// The TOML parser's own error, with its position.
#[derive(Debug, thiserror::Error)]
#[error(transparent)]
pub struct TomlParseError(#[from] toml::de::Error);

/// A failure with no other class, and any panic.
///
/// The backtrace is a public field of each variant: the process boundary reads it from there.
#[derive(Debug)]
pub enum InternalError {
    Unexpected {
        source: Box<dyn Error + Send + Sync>,
        backtrace: Backtrace,
    },
    Panic {
        message: String,
        backtrace: Backtrace,
    },
}

impl InternalError {
    /// The one constructor of an unexpected failure: it captures the backtrace.
    pub(crate) fn unexpected(source: impl Into<Box<dyn Error + Send + Sync>>) -> Self {
        Self::Unexpected {
            source: source.into(),
            backtrace: Backtrace::capture(),
        }
    }
}

impl fmt::Display for InternalError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unexpected { source, .. } => source.fmt(f),
            Self::Panic { message, .. } => f.write_str(message),
        }
    }
}

impl Error for InternalError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Unexpected { source, .. } => Some(source.as_ref()),
            Self::Panic { .. } => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn usage_of_index() -> Usage {
        Usage {
            line: "Usage: winter-workspace ws index [OPTIONS] <NAME>".to_owned(),
            help_command: "winter-workspace ws index --help".to_owned(),
        }
    }

    fn usage_with(fault: ParseFault) -> UsageError {
        UsageError::Parse {
            command_path: vec!["ws".to_owned(), "index".to_owned()],
            usage: usage_of_index(),
            fault,
        }
    }

    fn usage() -> UsageError {
        usage_with(ParseFault::UnknownOption {
            name: "--nope".to_owned(),
        })
    }

    /// One fault of each variant, with a text its error line has to carry.
    fn faults() -> Vec<(ParseFault, &'static str)> {
        vec![
            (
                ParseFault::UnknownCommand {
                    name: "nope".to_owned(),
                },
                "nope",
            ),
            (
                ParseFault::UnknownOption {
                    name: "--nope".to_owned(),
                },
                "--nope",
            ),
            (
                ParseFault::MissingArgument {
                    metavar: "<NAME>".to_owned(),
                },
                "<NAME>",
            ),
            (
                ParseFault::MissingOptionValue {
                    option: "--tail".to_owned(),
                },
                "--tail",
            ),
            (
                ParseFault::InvalidValue {
                    param: "--tail".to_owned(),
                    value: "many".to_owned(),
                    expected: "a number".to_owned(),
                },
                "many",
            ),
            (
                ParseFault::UnexpectedArgument {
                    values: vec!["extra".to_owned()],
                },
                "\"extra\"",
            ),
            (
                ParseFault::RepeatedOption {
                    option: "--json".to_owned(),
                },
                "--json",
            ),
            (
                ParseFault::NotUtf8 {
                    argument: "na\u{fffd}me".to_owned(),
                },
                "\"na\u{fffd}me\"",
            ),
        ]
    }

    fn root_not_found() -> ConfigError {
        ConfigError::RootNotFound {
            start: PathBuf::from("/somewhere/below"),
        }
    }

    fn panic() -> InternalError {
        InternalError::Panic {
            message: "index out of range".to_owned(),
            backtrace: Backtrace::disabled(),
        }
    }

    #[test]
    fn a_termination_is_at_most_four_words_wide() {
        assert!(
            std::mem::size_of::<Termination>() <= 4 * std::mem::size_of::<usize>(),
            "{} bytes",
            std::mem::size_of::<Termination>()
        );
    }

    #[test]
    fn exit_codes_follow_the_class_of_the_ending() {
        assert_eq!(Termination::Success.exit_code(), 0);
        assert_eq!(Termination::Reported.exit_code(), 1);
        assert_eq!(Termination::Usage(Box::new(usage())).exit_code(), 2);
        assert_eq!(
            Termination::BareGroup {
                help: "help".to_owned()
            }
            .exit_code(),
            2
        );
        assert_eq!(
            Termination::Config(Box::new(root_not_found())).exit_code(),
            1
        );
        assert_eq!(Termination::Internal(Box::new(panic())).exit_code(), 1);
    }

    #[test]
    fn an_ending_has_an_error_type_exactly_when_its_exit_code_is_not_0() {
        let one_of_every_variant = [
            Termination::Success,
            Termination::Reported,
            Termination::Usage(Box::new(usage())),
            Termination::BareGroup {
                help: "help".to_owned(),
            },
            Termination::Command(Box::new(CommandError::Catalog(CatalogError::NotFound {
                kind: crate::RepositoryKind::Project,
                name: "docs".to_owned(),
            }))),
            Termination::Config(Box::new(root_not_found())),
            Termination::BrokenPipe,
            Termination::Internal(Box::new(panic())),
        ];

        let mut names = Vec::new();
        for termination in &one_of_every_variant {
            let name = termination.error_type();
            assert_eq!(
                name.is_none(),
                termination.exit_code() == 0,
                "{termination:?}"
            );
            names.extend(name);
        }

        assert_eq!(
            names,
            [
                "Reported",
                "Usage",
                "BareGroup",
                "Command",
                "Config",
                "BrokenPipe",
                "Internal"
            ]
        );
    }

    #[test]
    fn the_error_type_of_an_ending_holds_nothing_of_its_payload() {
        for fault in faults().into_iter().map(|(fault, _)| fault) {
            assert_eq!(
                Termination::Usage(Box::new(usage_with(fault))).error_type(),
                Some("Usage")
            );
        }
    }

    #[test]
    fn success_and_reported_have_no_diagnostic() {
        assert_eq!(Termination::Success.diagnostic(), None);
        assert_eq!(Termination::Reported.diagnostic(), None);
    }

    #[test]
    fn a_broken_pipe_exits_1_and_has_no_diagnostic() {
        assert_eq!(Termination::BrokenPipe.exit_code(), 1);
        assert_eq!(Termination::BrokenPipe.diagnostic(), None);
    }

    #[test]
    fn a_failed_write_to_a_handle_the_reader_closed_is_a_broken_pipe() {
        let termination =
            Termination::from_write_error(std::io::Error::from(std::io::ErrorKind::BrokenPipe));

        assert!(
            matches!(termination, Termination::BrokenPipe),
            "{termination:?}"
        );
    }

    #[test]
    fn a_failed_write_of_any_other_kind_is_an_internal_failure_that_carries_the_error() {
        for kind in [
            std::io::ErrorKind::StorageFull,
            std::io::ErrorKind::PermissionDenied,
            std::io::ErrorKind::Other,
        ] {
            let termination =
                Termination::from_write_error(std::io::Error::new(kind, "the device is full"));

            let Termination::Internal(error) = &termination else {
                panic!("{kind:?}: expected an internal failure, got {termination:?}");
            };
            assert!(
                matches!(**error, InternalError::Unexpected { .. }),
                "{kind:?}: {error:?}"
            );
            let source = error.source().expect("a source");
            let source = source
                .downcast_ref::<std::io::Error>()
                .expect("the write error");
            assert_eq!(source.kind(), kind);
            assert_eq!(termination.exit_code(), 1, "{kind:?}");
            assert_eq!(
                termination.diagnostic().as_deref(),
                Some("error: the device is full"),
                "{kind:?}"
            );
        }
    }

    #[test]
    fn bare_group_diagnostic_is_its_help() {
        let help = "Usage: winter-workspace <COMMAND>\n".to_owned();

        assert_eq!(
            Termination::BareGroup { help: help.clone() }.diagnostic(),
            Some(help)
        );
    }

    #[test]
    fn root_not_found_diagnostic_names_the_start_path() {
        let diagnostic = Termination::Config(Box::new(root_not_found()))
            .diagnostic()
            .expect("a diagnostic");

        assert!(diagnostic.contains("/somewhere/below"), "{diagnostic}");
    }

    #[test]
    fn a_usage_diagnostic_is_the_error_line_the_usage_line_and_the_help_command_in_that_order() {
        for (fault, carried) in faults() {
            let name = format!("{fault:?}");
            let usage = usage_of_index();

            let diagnostic = Termination::Usage(Box::new(usage_with(fault)))
                .diagnostic()
                .expect("a diagnostic");

            let lines: Vec<&str> = diagnostic.lines().collect();
            let [error_line, usage_line, help_line] = lines.as_slice() else {
                panic!("{name}: expected three lines, got {diagnostic:?}");
            };
            assert!(error_line.starts_with(ERROR_MARKER), "{name}: {diagnostic}");
            assert!(error_line.contains(carried), "{name}: {diagnostic}");
            assert_eq!(*usage_line, usage.line, "{name}");
            assert!(
                help_line.contains("winter-workspace ws index --help"),
                "{name}: {diagnostic}"
            );
        }
    }

    #[test]
    fn unexpected_arguments_are_each_quoted_and_stay_on_the_error_line() {
        let diagnostic = Termination::Usage(Box::new(usage_with(ParseFault::UnexpectedArgument {
            values: vec!["two".to_owned(), String::new(), "a\nb".to_owned()],
        })))
        .diagnostic()
        .expect("a diagnostic");

        let lines: Vec<&str> = diagnostic.lines().collect();
        assert_eq!(lines.len(), 3, "{diagnostic:?}");
        assert!(lines[0].ends_with(r#""two" "" "a\nb""#), "{diagnostic}");
    }

    #[test]
    fn an_argument_that_is_not_utf_8_is_quoted_and_stays_on_the_error_line() {
        let diagnostic = Termination::Usage(Box::new(usage_with(ParseFault::NotUtf8 {
            argument: "a\nb\u{fffd}".to_owned(),
        })))
        .diagnostic()
        .expect("a diagnostic");

        let lines: Vec<&str> = diagnostic.lines().collect();
        assert_eq!(lines.len(), 3, "{diagnostic:?}");
        assert!(lines[0].ends_with("\"a\\nb\u{fffd}\""), "{diagnostic}");
    }

    #[test]
    fn every_other_error_ending_starts_with_the_error_marker() {
        let command =
            Termination::Command(Box::new(CommandError::Catalog(CatalogError::NotFound {
                kind: crate::RepositoryKind::Project,
                name: "docs".to_owned(),
            })));
        let config = Termination::Config(Box::new(root_not_found()));
        let unexpected = Termination::Internal(Box::new(InternalError::unexpected(
            std::io::Error::other("disk on fire"),
        )));
        let panicked = Termination::Internal(Box::new(panic()));

        for (termination, carried) in [
            (command, "docs"),
            (config, "/somewhere/below"),
            (unexpected, "disk on fire"),
            (panicked, "index out of range"),
        ] {
            let diagnostic = termination.diagnostic().expect("a diagnostic");

            assert!(diagnostic.starts_with(ERROR_MARKER), "{diagnostic}");
            assert!(diagnostic.contains(carried), "{diagnostic}");
            assert_eq!(diagnostic.lines().count(), 1, "{diagnostic}");
        }
    }

    #[test]
    fn the_constructor_of_an_unexpected_failure_keeps_its_source() {
        let error = InternalError::unexpected(std::io::Error::other("disk on fire"));

        assert!(matches!(error, InternalError::Unexpected { .. }));
        assert_eq!(error.to_string(), "disk on fire");
        assert!(error.source().is_some());
    }

    #[test]
    fn file_read_names_the_path_and_the_cause() {
        let cause = FileCause::from(std::io::Error::other("permission denied"));
        let error = ConfigError::FileRead {
            path: PathBuf::from("/ws/.winter/config.toml"),
            cause,
        };

        let text = error.to_string();
        assert!(text.contains("/ws/.winter/config.toml"), "{text}");
        assert!(text.contains("permission denied"), "{text}");
    }

    #[test]
    fn unexpected_displays_its_source_and_exposes_it() {
        let error = InternalError::Unexpected {
            source: Box::new(std::io::Error::other("disk on fire")),
            backtrace: Backtrace::disabled(),
        };

        assert_eq!(error.to_string(), "disk on fire");
        assert!(error.source().is_some());
    }

    #[test]
    fn panic_displays_its_message_and_has_no_source() {
        let error = panic();

        assert_eq!(error.to_string(), "index out of range");
        assert!(error.source().is_none());
    }

    #[test]
    fn a_config_value_is_written_inline_on_one_line() {
        let value: toml::Value = toml::from_str("v = { a = [1, \"x\\ny\"], b = {} }")
            .map(|table: toml::Table| table["v"].clone())
            .expect("a value");

        assert_eq!(
            ConfigValue::new(&value).to_string(),
            "{ \"a\" = [1, \"x\\ny\"], \"b\" = {} }"
        );
    }

    #[test]
    fn a_repository_refusal_names_the_kind_the_label_and_the_key() {
        let owner = RepoRef {
            kind: RepoKind::Standalone,
            label: "ctx".to_owned(),
        };
        let typed = ConfigError::ValueType {
            key: TypedKey::Ref,
            owner: Some(owner),
            path: None,
            found: ValueType::Integer,
        };
        let fault = ConfigError::Repository {
            kind: RepoKind::Project,
            label: "we\nb".to_owned(),
            fault: RepositoryFault::Path {
                field: PathField::ConfigDir,
                value: "/abs".to_owned(),
            },
        };

        let typed = typed.to_string();
        assert!(
            typed.contains("ref") && typed.contains("standalone") && typed.contains("ctx"),
            "{typed}"
        );
        assert!(
            typed.contains("integer") && typed.contains("string"),
            "{typed}"
        );
        let fault = fault.to_string();
        assert!(
            fault.contains("project") && fault.contains("config_dir") && fault.contains("/abs"),
            "{fault}"
        );
        assert_eq!(fault.lines().count(), 1, "{fault}");
    }
}
