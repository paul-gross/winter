//! How an invocation ends, and the errors an ending carries.

use std::backtrace::Backtrace;
use std::error::Error;
use std::fmt;
use std::path::PathBuf;

use crate::config_edit::CatalogError;
use crate::workspace_files::{CONFIG_FILE, LOCAL_CONFIG_FILE};

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
    /// A program run on a repository failed; the diagnostic is the message and the details of the call.
    Repo(Box<RepoError>),
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
            | Self::Repo(_)
            | Self::BrokenPipe
            | Self::Internal(_) => 1,
            Self::Usage(_) | Self::BareGroup { .. } => 2,
        }
    }

    /// The text the process boundary writes to its error stream, or `None` when nothing more is written.
    ///
    /// A bare group's text is its help. Every other text opens with a line that starts with the error marker
    /// and carries the message; a usage error adds its usage line and the command that prints its help, and a
    /// repo error one line for each detail of the failed call that is present.
    pub fn diagnostic(&self) -> Option<String> {
        match self {
            Self::Success | Self::Reported | Self::BrokenPipe => None,
            Self::Usage(error) => Some(error.to_string()),
            Self::BareGroup { help } => Some(help.clone()),
            Self::Command(error) => Some(format!("{ERROR_MARKER}{error}")),
            Self::Config(error) => Some(format!("{ERROR_MARKER}{error}")),
            Self::Repo(error) => Some(format!("{ERROR_MARKER}{}", error.detailed())),
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
            Self::Repo(_) => Some("Repo"),
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

/// A failure to locate, read, write, or accept the workspace configuration.
///
/// A variant carries the values its message names.
#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("no workspace found: no {} at or above {}", CONFIG_FILE, .start.display())]
    RootNotFound { start: PathBuf },
    #[error("cannot load {}: {cause}", .path.display())]
    FileRead { path: PathBuf, cause: FileCause },
    /// A file that cannot be replaced.
    #[error("cannot write {}: {cause}", .path.display())]
    FileWrite {
        path: PathBuf,
        cause: std::io::Error,
    },
    #[error(transparent)]
    RemovedKey(RemovedKey),
    #[error("{key} does not accept the {value}")]
    InvalidChoice { key: ChoiceKey, value: ConfigValue },
    #[error(
        "envs_per_workspace is {envs_per_workspace}, too small for the {aliases} entries of env_aliases"
    )]
    EnvsPerWorkspaceTooSmall {
        envs_per_workspace: i64,
        aliases: usize,
    },
    #[error(
        "two standalone repositories resolve to the name {name}: the entry with {first} and the entry with {second}"
    )]
    DuplicateStandaloneName {
        name: String,
        first: EntryIdent,
        second: EntryIdent,
    },
    #[error(
        "the workspace prefix {prefix} is also the prefix of the standalone repository {standalone}"
    )]
    PrefixCollision { prefix: String, standalone: String },
    #[error("{} in {file} must be an array, found a value of type {found}", .kind.array_key())]
    RepositoriesNotArray {
        kind: RepoKind,
        file: ConfigFileName,
        found: ValueType,
    },
    #[error("{kind} repository {label}: {fault}")]
    Repository {
        kind: RepoKind,
        label: String,
        fault: RepositoryFault,
    },
    /// A value of a type its key does not accept. `owner` is the repository entry that holds the key, when
    /// one does.
    #[error("{key}{} must be {}, found a value of type {found}", of_owner(.owner), .key.accepted())]
    ValueType {
        key: TypedKey,
        owner: Option<RepoRef>,
        found: ValueType,
    },
    /// An entry of a `vars` table of `[env]`. The message names the entry and holds no literal value of an
    /// entry and no command; of a command entry it can hold the value of the `format` field.
    #[error("env.{band}.vars.{key}: {fault}")]
    EnvVar {
        band: String,
        key: String,
        fault: EnvVarFault,
    },
    #[error("model_tiers.{label}: {fault}")]
    ModelTier {
        label: String,
        fault: ModelTierFault,
    },
    #[error("agent_model_overrides in {file} must be a table, found a value of type {found}")]
    AgentOverridesNotTable {
        file: ConfigFileName,
        found: ValueType,
    },
    #[error("agent_model_overrides.{agent}: {fault}")]
    AgentOverride {
        agent: String,
        fault: AgentOverrideFault,
    },
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

/// A key the configuration no longer reads. Each message names what replaces the key.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum RemovedKey {
    #[error("service_orchestrator is no longer read: declare [capabilities] service instead")]
    ServiceOrchestrator,
    #[error("session_prefix is no longer read: declare service_prefix instead")]
    SessionPrefix,
    /// `keys` are the keys found in the removed table; there are none when the table is empty or the value
    /// is no table.
    #[error(
        "[env.vars] is no longer read: use [env.feature.vars] or [env.workspace.vars] instead{}",
        holding(.keys)
    )]
    EnvVars { keys: Vec<String> },
}

/// A key that accepts one of a closed set of values. It renders as the key's path in the file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChoiceKey {
    AdoptExtensions,
    DashboardLayout,
}

impl fmt::Display for ChoiceKey {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::AdoptExtensions => "adopt_extensions",
            Self::DashboardLayout => "tui.dashboard.layout",
        })
    }
}

/// The array a repository entry belongs to. It renders as `project` or `standalone`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RepoKind {
    Project,
    Standalone,
}

impl RepoKind {
    /// The key of the array of tables that holds the entries of this kind.
    fn array_key(self) -> &'static str {
        match self {
            Self::Project => "project_repository",
            Self::Standalone => "standalone_repository",
        }
    }
}

impl fmt::Display for RepoKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Project => "project",
            Self::Standalone => "standalone",
        })
    }
}

/// What identifies a repository entry: its name, or its URL when it declares no name.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EntryIdent {
    Name(String),
    Url(String),
}

impl fmt::Display for EntryIdent {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Name(name) => write!(f, "name {name}"),
            Self::Url(url) => write!(f, "url {url}"),
        }
    }
}

/// One repository entry: its kind and the label that tells it from the others of that kind.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RepoRef {
    pub kind: RepoKind,
    pub label: String,
}

impl fmt::Display for RepoRef {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} repository {}", self.kind, self.label)
    }
}

/// A key that accepts values of one type. It renders as the key's name in the file.
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
    Table(MergedTable),
    GitUser,
    GitUserName,
    GitUserEmail,
}

impl TypedKey {
    /// The type the key accepts, as a message words it.
    fn accepted(self) -> &'static str {
        match self {
            Self::GitExcludes | Self::Cmd => "an array of strings",
            Self::MainBranch
            | Self::Ref
            | Self::Name
            | Self::Url
            | Self::Prefix
            | Self::Path
            | Self::ConfigDir
            | Self::GitUserName
            | Self::GitUserEmail => "a string",
            Self::Table(_) | Self::GitUser => "a table",
        }
    }
}

impl fmt::Display for TypedKey {
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
            Self::Table(table) => return table.fmt(f),
            Self::GitUser => "git.user",
            Self::GitUserName => "git.user.name",
            Self::GitUserEmail => "git.user.email",
        })
    }
}

/// A top-level table that the local file merges into the shared one. It renders as the table's key.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MergedTable {
    Git,
    Keybindings,
    Tui,
    Capabilities,
    Env,
    ModelTiers,
    AgentModelOverrides,
}

impl fmt::Display for MergedTable {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Git => "git",
            Self::Keybindings => "keybindings",
            Self::Tui => "tui",
            Self::Capabilities => "capabilities",
            Self::Env => "env",
            Self::ModelTiers => "model_tiers",
            Self::AgentModelOverrides => "agent_model_overrides",
        })
    }
}

/// One of the two configuration files. It renders as the file's path below the workspace root.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConfigFileName {
    Shared,
    Local,
}

impl fmt::Display for ConfigFileName {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Shared => CONFIG_FILE,
            Self::Local => LOCAL_CONFIG_FILE,
        })
    }
}

/// What is wrong with one repository entry.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum RepositoryFault {
    #[error("{field} must be a relative path with no `..` segment: {value}")]
    Path { field: PathField, value: String },
    #[error("a standalone repository cannot be nested")]
    StandaloneNested,
    #[error("{key} needs nested = true")]
    NestedOnly { key: NestedOnlyKey },
    #[error("{key} cannot be declared with nested = true")]
    NestedWith { key: NestedExcludedKey },
    #[error("extension = true cannot be declared with nested = true")]
    NestedWithExtensionTrue,
    #[error("extension must be a boolean, not the {value}")]
    Extension { value: ConfigValue },
    #[error("nested must be a boolean, not the {value}")]
    Nested { value: ConfigValue },
    #[error("load must be eager, lazy, or none, not the {value}")]
    Load { value: ConfigValue },
    #[error("entry must be a non-empty array of strings, not the {value}")]
    EntryList { value: ConfigValue },
    #[error("each path of entry must be a non-empty string, not the {value}")]
    EntryPathEmpty { value: ConfigValue },
    #[error("each path of entry must be relative with no `..` segment: {value}")]
    EntryPathEscapes { value: String },
    #[error("envs must be an integer of at least 1, not the {value}")]
    Envs { value: ConfigValue },
    #[error("inherit_local must be an array of non-empty names without `.`, not the {value}")]
    InheritLocal { value: ConfigValue },
}

/// A key of a standalone repository entry that holds a path. It renders as the key's name in the file.
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

/// A key that only a nested repository entry may declare. It renders as the key's name in the file.
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

/// A key that a nested repository entry may not declare. It renders as the key's name in the file.
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

/// What is wrong with one entry of a `vars` table of `[env]`. No variant carries a literal value of an entry
/// or a command; `InvalidFormat` carries the value of the `format` field of a command entry.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum EnvVarFault {
    #[error("a value of type {found} is not supported")]
    UnsupportedType { found: ValueType },
    #[error("unknown fields in the command entry: {}", listed(.fields))]
    UnknownFields { fields: Vec<String> },
    /// A command entry under a key that starts with `WINTER_`.
    #[error("a command entry cannot be declared under a WINTER_ key")]
    ManagedName,
    #[error("a command entry needs a non-empty command")]
    MissingCommand,
    #[error("format must be raw, dotenv, or json, not the {value}")]
    InvalidFormat { value: ConfigValue },
    #[error("shell must be a boolean, found a value of type {found}")]
    ShellNotBoolean { found: ValueType },
    #[error("exports must be an array of strings")]
    ExportsNotStringList,
}

/// What is wrong with one label of `[model_tiers]`.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum ModelTierFault {
    #[error("must be a table of vendors, found a value of type {found}")]
    NotVendorTable { found: ValueType },
    #[error("unknown vendor {vendor}")]
    UnknownVendor { vendor: String },
    /// A model id that is not a string, or is the empty string.
    #[error("the model id of {vendor} must be a non-empty string, found a value of type {found}")]
    ModelIdNotString { vendor: String, found: ValueType },
    #[error("names no vendor")]
    NoVendor,
}

/// What is wrong with one agent of `[agent_model_overrides]`.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum AgentOverrideFault {
    /// `known` are the labels of the effective tier table.
    #[error("unknown tier {label}; the tiers are: {}", listed(.known))]
    UnknownTier { label: String, known: Vec<String> },
    #[error("the value is empty")]
    EmptyValue,
    #[error("must be a tier label or a table of vendors, found a value of type {found}")]
    WrongType { found: ValueType },
    #[error("unknown vendor {vendor}")]
    UnknownVendor { vendor: String },
    #[error("names no vendor")]
    NoVendor,
    #[error("the model id of {vendor} is not valid")]
    VendorModelId { vendor: String },
    #[error("{vendor} must be a model id or a profile table, found a value of type {found}")]
    VendorValueType { vendor: String, found: ValueType },
    #[error("unknown keys in the profile of {vendor}: {}", listed(.keys))]
    UnknownProfileKeys { vendor: String, keys: Vec<String> },
    #[error("model in the profile of {vendor} is not valid")]
    ProfileModel { vendor: String },
    #[error("effort in the profile of {vendor} is not valid")]
    ProfileEffort { vendor: String },
    #[error("the profile of {vendor} sets neither model nor effort")]
    ProfileEmpty { vendor: String },
}

/// A value of a configuration file: its TOML text and its type. It renders as the type and then the text.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConfigValue {
    pub text: String,
    pub found: ValueType,
}

impl fmt::Display for ConfigValue {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} {}", self.found, self.text)
    }
}

/// The type of a TOML value. It renders as TOML's own name of the type, in lower case.
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

impl fmt::Display for ValueType {
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

/// The values of a list, separated by a comma and a space.
fn listed(values: &[String]) -> String {
    values.join(", ")
}

/// What follows the message of the removed `[env.vars]` to name the keys found there; nothing when there are
/// none.
fn holding(keys: &[String]) -> String {
    if keys.is_empty() {
        String::new()
    } else {
        format!("; it holds {}", listed(keys))
    }
}

/// What follows a key in a message to name the repository entry that holds it; nothing for a key of the
/// document itself.
fn of_owner(owner: &Option<RepoRef>) -> String {
    owner
        .as_ref()
        .map(|owner| format!(" of the {owner}"))
        .unwrap_or_default()
}

/// A failed operation on a repository: what failed, and the call that failed with each detail that is known.
///
/// Its text is the message alone. The diagnostic of its ending adds the details.
#[derive(Debug, PartialEq, Eq, thiserror::Error)]
#[error("{message}")]
pub struct RepoError {
    pub message: RepoMessage,
    /// The program that was run.
    pub program: Option<String>,
    /// The first argument of the program.
    pub subcommand: Option<String>,
    /// The arguments after the subcommand.
    pub args: Vec<String>,
    /// The working directory of the call.
    pub cwd: Option<PathBuf>,
    /// The exit code of the program, or minus the number of the signal that ended it.
    pub exit: Option<i32>,
    /// What the program wrote to its stderr, without the blank space around it.
    pub stderr: Option<String>,
}

impl RepoError {
    /// The message, then one indented line for each detail that is present: the command line, the working
    /// directory, the exit, which tells the signal that ended the program from an exit code, and the stderr.
    fn detailed(&self) -> String {
        let mut lines = vec![self.message.to_string()];
        let command: Vec<&str> = self
            .program
            .iter()
            .chain(&self.subcommand)
            .chain(&self.args)
            .map(String::as_str)
            .collect();
        if self.program.is_some() || self.subcommand.is_some() {
            lines.push(format!("  $ {}", command.join(" ")));
        }
        if let Some(cwd) = &self.cwd {
            lines.push(format!("  cwd: {}", cwd.display()));
        }
        match self.exit {
            Some(exit) if exit < 0 => lines.push(format!(
                "  exit {exit} (killed by signal {})",
                exit.unsigned_abs()
            )),
            Some(exit) => lines.push(format!("  exit {exit}")),
            None => {}
        }
        if let Some(stderr) = &self.stderr {
            lines.push(format!("  stderr: {stderr}"));
        }
        lines.join("\n")
    }
}

/// What a repo error reports. A caller matches the variant, never the text.
#[derive(Debug, PartialEq, Eq, thiserror::Error)]
pub enum RepoMessage {
    /// A git call made for `operation` on the repository `repo` failed.
    #[error("{operation} failed for {repo}")]
    GitFailure {
        operation: GitOperation,
        repo: String,
    },
}

/// What a git call that failed was made for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GitOperation {
    /// The read of the state of a working tree.
    StatusProbe,
    /// The count of the commits a checkout is ahead of and behind the main branch of its remote.
    MainAheadBehindProbe,
}

impl fmt::Display for GitOperation {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::StatusProbe => "status probe",
            Self::MainAheadBehindProbe => "main-branch ahead/behind probe",
        })
    }
}

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

    fn failed_status() -> RepoError {
        RepoError {
            message: RepoMessage::GitFailure {
                operation: GitOperation::StatusProbe,
                repo: "api".to_owned(),
            },
            program: Some("git".to_owned()),
            subcommand: Some("status".to_owned()),
            args: vec!["--porcelain=v2".to_owned(), "-z".to_owned()],
            cwd: Some(PathBuf::from("/ws/alpha/api")),
            exit: Some(128),
            stderr: Some("fatal: not a git repository".to_owned()),
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
        assert_eq!(Termination::Repo(Box::new(failed_status())).exit_code(), 1);
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
            Termination::Repo(Box::new(failed_status())),
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
                "Repo",
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
    fn the_text_of_a_repo_error_is_its_message_alone() {
        assert_eq!(failed_status().to_string(), "status probe failed for api");
        let counted = RepoMessage::GitFailure {
            operation: GitOperation::MainAheadBehindProbe,
            repo: "web".to_owned(),
        };
        assert_eq!(
            counted.to_string(),
            "main-branch ahead/behind probe failed for web"
        );
    }

    #[test]
    fn a_repo_diagnostic_is_the_error_line_and_then_each_detail_of_the_call() {
        let diagnostic = Termination::Repo(Box::new(failed_status())).diagnostic();

        assert_eq!(
            diagnostic.as_deref(),
            Some(
                "error: status probe failed for api\n\
                 \x20 $ git status --porcelain=v2 -z\n\
                 \x20 cwd: /ws/alpha/api\n\
                 \x20 exit 128\n\
                 \x20 stderr: fatal: not a git repository"
            )
        );
    }

    #[test]
    fn a_repo_diagnostic_leaves_out_each_detail_that_is_absent() {
        let bare = RepoError {
            program: None,
            subcommand: None,
            args: Vec::new(),
            cwd: None,
            exit: None,
            stderr: None,
            ..failed_status()
        };

        assert_eq!(
            Termination::Repo(Box::new(bare)).diagnostic().as_deref(),
            Some("error: status probe failed for api")
        );
    }

    #[test]
    fn a_repo_diagnostic_tells_a_signal_from_an_exit_code() {
        let killed = RepoError {
            exit: Some(-9),
            ..failed_status()
        };

        let diagnostic = Termination::Repo(Box::new(killed))
            .diagnostic()
            .expect("a diagnostic");

        assert!(
            diagnostic.contains("\n  exit -9 (killed by signal 9)\n"),
            "{diagnostic}"
        );
    }

    #[test]
    fn the_constructor_of_an_unexpected_failure_keeps_its_source() {
        let error = InternalError::unexpected(std::io::Error::other("disk on fire"));

        assert!(matches!(error, InternalError::Unexpected { .. }));
        assert_eq!(error.to_string(), "disk on fire");
        assert!(error.source().is_some());
    }

    fn value(text: &str, found: ValueType) -> ConfigValue {
        ConfigValue {
            text: text.to_owned(),
            found,
        }
    }

    fn strings(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_owned()).collect()
    }

    /// The name of the variant. The match has no catch-all arm, so a variant added to the enum does not
    /// compile until it is named here.
    fn variant_of(error: &ConfigError) -> &'static str {
        match error {
            ConfigError::RootNotFound { .. } => "RootNotFound",
            ConfigError::FileRead { .. } => "FileRead",
            ConfigError::FileWrite { .. } => "FileWrite",
            ConfigError::RemovedKey(_) => "RemovedKey",
            ConfigError::InvalidChoice { .. } => "InvalidChoice",
            ConfigError::EnvsPerWorkspaceTooSmall { .. } => "EnvsPerWorkspaceTooSmall",
            ConfigError::DuplicateStandaloneName { .. } => "DuplicateStandaloneName",
            ConfigError::PrefixCollision { .. } => "PrefixCollision",
            ConfigError::RepositoriesNotArray { .. } => "RepositoriesNotArray",
            ConfigError::Repository { .. } => "Repository",
            ConfigError::ValueType { .. } => "ValueType",
            ConfigError::EnvVar { .. } => "EnvVar",
            ConfigError::ModelTier { .. } => "ModelTier",
            ConfigError::AgentOverridesNotTable { .. } => "AgentOverridesNotTable",
            ConfigError::AgentOverride { .. } => "AgentOverride",
        }
    }

    fn repository(fault: RepositoryFault) -> ConfigError {
        ConfigError::Repository {
            kind: RepoKind::Project,
            label: "docs".to_owned(),
            fault,
        }
    }

    fn env_var(fault: EnvVarFault) -> ConfigError {
        ConfigError::EnvVar {
            band: "feature".to_owned(),
            key: "API_TOKEN".to_owned(),
            fault,
        }
    }

    fn model_tier(fault: ModelTierFault) -> ConfigError {
        ConfigError::ModelTier {
            label: "quick".to_owned(),
            fault,
        }
    }

    fn agent_override(fault: AgentOverrideFault) -> ConfigError {
        ConfigError::AgentOverride {
            agent: "reviewer".to_owned(),
            fault,
        }
    }

    /// Configuration errors of the variants that carry a key, a file, a repository, or a fault, each with the
    /// texts its message has to hold: one per value the error carries.
    fn keyed_config_errors() -> Vec<(ConfigError, Vec<&'static str>)> {
        vec![
            (
                ConfigError::FileWrite {
                    path: PathBuf::from("/ws/.winter/config.toml"),
                    cause: std::io::Error::other("no room on the device"),
                },
                vec!["/ws/.winter/config.toml", "no room on the device"],
            ),
            (
                ConfigError::RemovedKey(RemovedKey::ServiceOrchestrator),
                vec!["service_orchestrator", "[capabilities] service"],
            ),
            (
                ConfigError::RemovedKey(RemovedKey::SessionPrefix),
                vec!["session_prefix", "service_prefix"],
            ),
            (
                ConfigError::RemovedKey(RemovedKey::EnvVars {
                    keys: strings(&["API_TOKEN", "REGION"]),
                }),
                vec![
                    "[env.vars]",
                    "API_TOKEN",
                    "REGION",
                    "[env.feature.vars]",
                    "[env.workspace.vars]",
                ],
            ),
            (
                ConfigError::RemovedKey(RemovedKey::EnvVars { keys: Vec::new() }),
                vec!["[env.vars]", "[env.feature.vars]", "[env.workspace.vars]"],
            ),
            (
                ConfigError::InvalidChoice {
                    key: ChoiceKey::AdoptExtensions,
                    value: value("\"some\"", ValueType::String),
                },
                vec!["adopt_extensions", "\"some\"", "string"],
            ),
            (
                ConfigError::InvalidChoice {
                    key: ChoiceKey::DashboardLayout,
                    value: value("7", ValueType::Integer),
                },
                vec!["tui.dashboard.layout", "7", "integer"],
            ),
            (
                ConfigError::DuplicateStandaloneName {
                    name: "tools".to_owned(),
                    first: EntryIdent::Name("tools".to_owned()),
                    second: EntryIdent::Url("https://fixture.invalid/tools.git".to_owned()),
                },
                vec![
                    "the name tools",
                    "with name tools",
                    "url https://fixture.invalid/tools.git",
                ],
            ),
            (
                ConfigError::PrefixCollision {
                    prefix: "wsx".to_owned(),
                    standalone: "tools".to_owned(),
                },
                vec!["wsx", "tools"],
            ),
            (
                ConfigError::RepositoriesNotArray {
                    kind: RepoKind::Project,
                    file: ConfigFileName::Shared,
                    found: ValueType::Table,
                },
                vec!["project_repository", ".winter/config.toml", "table"],
            ),
            (
                ConfigError::RepositoriesNotArray {
                    kind: RepoKind::Standalone,
                    file: ConfigFileName::Local,
                    found: ValueType::Boolean,
                },
                vec![
                    "standalone_repository",
                    ".winter/config.local.toml",
                    "boolean",
                ],
            ),
            (
                ConfigError::Repository {
                    kind: RepoKind::Standalone,
                    label: "tools".to_owned(),
                    fault: RepositoryFault::Path {
                        field: PathField::ConfigDir,
                        value: "../outside".to_owned(),
                    },
                },
                vec!["standalone", "tools", "config_dir", "../outside"],
            ),
            (
                repository(RepositoryFault::Path {
                    field: PathField::Path,
                    value: "/abs".to_owned(),
                }),
                vec!["project", "docs", "path", "/abs"],
            ),
            (
                repository(RepositoryFault::StandaloneNested),
                vec!["project", "docs", "nested"],
            ),
            (
                repository(RepositoryFault::NestedOnly {
                    key: NestedOnlyKey::InheritLocal,
                }),
                vec!["project", "docs", "inherit_local"],
            ),
            (
                repository(RepositoryFault::NestedWith {
                    key: NestedExcludedKey::Entry,
                }),
                vec!["project", "docs", "entry"],
            ),
            (
                repository(RepositoryFault::NestedWithExtensionTrue),
                vec!["project", "docs", "extension"],
            ),
            (
                repository(RepositoryFault::Extension {
                    value: value("\"yes\"", ValueType::String),
                }),
                vec!["project", "docs", "extension", "\"yes\"", "string"],
            ),
            (
                repository(RepositoryFault::Nested {
                    value: value("1", ValueType::Integer),
                }),
                vec!["project", "docs", "nested", "1", "integer"],
            ),
            (
                repository(RepositoryFault::Load {
                    value: value("\"soon\"", ValueType::String),
                }),
                vec!["project", "docs", "load", "\"soon\"", "string"],
            ),
            (
                repository(RepositoryFault::EntryList {
                    value: value("[]", ValueType::Array),
                }),
                vec!["project", "docs", "entry", "[]", "array"],
            ),
            (
                repository(RepositoryFault::EntryPathEmpty {
                    value: value("2.5", ValueType::Float),
                }),
                vec!["project", "docs", "entry", "2.5", "float"],
            ),
            (
                repository(RepositoryFault::EntryPathEscapes {
                    value: "../skills".to_owned(),
                }),
                vec!["project", "docs", "entry", "../skills"],
            ),
            (
                repository(RepositoryFault::Envs {
                    value: value("true", ValueType::Boolean),
                }),
                vec!["project", "docs", "envs", "true", "boolean"],
            ),
            (
                repository(RepositoryFault::InheritLocal {
                    value: value("{ a = 1 }", ValueType::Table),
                }),
                vec!["project", "docs", "inherit_local", "{ a = 1 }", "table"],
            ),
            (
                ConfigError::ValueType {
                    key: TypedKey::MainBranch,
                    owner: None,
                    found: ValueType::Datetime,
                },
                vec!["main_branch", "datetime"],
            ),
            (
                ConfigError::ValueType {
                    key: TypedKey::Cmd,
                    owner: Some(RepoRef {
                        kind: RepoKind::Standalone,
                        label: "#3".to_owned(),
                    }),
                    found: ValueType::Integer,
                },
                vec!["cmd", "standalone", "#3", "integer"],
            ),
            (
                ConfigError::ValueType {
                    key: TypedKey::Table(MergedTable::Keybindings),
                    owner: None,
                    found: ValueType::Array,
                },
                vec!["keybindings", "array"],
            ),
            (
                env_var(EnvVarFault::UnsupportedType {
                    found: ValueType::Boolean,
                }),
                vec!["feature", "API_TOKEN", "boolean"],
            ),
            (
                env_var(EnvVarFault::UnknownFields {
                    fields: strings(&["cwd", "timeout"]),
                }),
                vec!["feature", "API_TOKEN", "cwd", "timeout"],
            ),
            (
                env_var(EnvVarFault::ManagedName),
                vec!["feature", "API_TOKEN", "WINTER_"],
            ),
            (
                env_var(EnvVarFault::MissingCommand),
                vec!["feature", "API_TOKEN", "command"],
            ),
            (
                env_var(EnvVarFault::InvalidFormat {
                    value: value("\"yaml\"", ValueType::String),
                }),
                vec!["feature", "API_TOKEN", "format", "\"yaml\"", "string"],
            ),
            (
                env_var(EnvVarFault::ShellNotBoolean {
                    found: ValueType::Integer,
                }),
                vec!["feature", "API_TOKEN", "shell", "integer"],
            ),
            (
                env_var(EnvVarFault::ExportsNotStringList),
                vec!["feature", "API_TOKEN", "exports"],
            ),
            (
                model_tier(ModelTierFault::NotVendorTable {
                    found: ValueType::String,
                }),
                vec!["quick", "string"],
            ),
            (
                model_tier(ModelTierFault::UnknownVendor {
                    vendor: "lantern".to_owned(),
                }),
                vec!["quick", "lantern"],
            ),
            (
                model_tier(ModelTierFault::ModelIdNotString {
                    vendor: "codex".to_owned(),
                    found: ValueType::Float,
                }),
                vec!["quick", "codex", "float"],
            ),
            (
                model_tier(ModelTierFault::ModelIdNotString {
                    vendor: "claude".to_owned(),
                    found: ValueType::String,
                }),
                vec!["quick", "claude", "non-empty string", "type string"],
            ),
            (
                model_tier(ModelTierFault::NoVendor),
                vec!["quick", "vendor"],
            ),
            (
                ConfigError::AgentOverridesNotTable {
                    file: ConfigFileName::Local,
                    found: ValueType::Array,
                },
                vec![
                    "agent_model_overrides",
                    ".winter/config.local.toml",
                    "array",
                ],
            ),
            (
                agent_override(AgentOverrideFault::UnknownTier {
                    label: "huge".to_owned(),
                    known: strings(&["quick", "deep"]),
                }),
                vec!["reviewer", "huge", "quick", "deep"],
            ),
            (
                agent_override(AgentOverrideFault::EmptyValue),
                vec!["reviewer", "empty"],
            ),
            (
                agent_override(AgentOverrideFault::WrongType {
                    found: ValueType::Integer,
                }),
                vec!["reviewer", "integer"],
            ),
            (
                agent_override(AgentOverrideFault::UnknownVendor {
                    vendor: "lantern".to_owned(),
                }),
                vec!["reviewer", "lantern"],
            ),
            (
                agent_override(AgentOverrideFault::NoVendor),
                vec!["reviewer", "vendor"],
            ),
            (
                agent_override(AgentOverrideFault::VendorModelId {
                    vendor: "claude".to_owned(),
                }),
                vec!["reviewer", "claude"],
            ),
            (
                agent_override(AgentOverrideFault::VendorValueType {
                    vendor: "codex".to_owned(),
                    found: ValueType::Boolean,
                }),
                vec!["reviewer", "codex", "boolean"],
            ),
            (
                agent_override(AgentOverrideFault::UnknownProfileKeys {
                    vendor: "opencode".to_owned(),
                    keys: strings(&["speed", "depth"]),
                }),
                vec!["reviewer", "opencode", "speed", "depth"],
            ),
            (
                agent_override(AgentOverrideFault::ProfileModel {
                    vendor: "claude".to_owned(),
                }),
                vec!["reviewer", "claude", "model"],
            ),
            (
                agent_override(AgentOverrideFault::ProfileEffort {
                    vendor: "codex".to_owned(),
                }),
                vec!["reviewer", "codex", "effort"],
            ),
            (
                agent_override(AgentOverrideFault::ProfileEmpty {
                    vendor: "opencode".to_owned(),
                }),
                vec!["reviewer", "opencode"],
            ),
        ]
    }

    #[test]
    fn a_keyed_config_error_names_each_value_it_carries_and_ends_with_exit_code_1() {
        let mut variants = Vec::new();
        for (error, carried) in keyed_config_errors() {
            let name = format!("{error:?}");
            let message = error.to_string();
            let variant = variant_of(&error);
            if !variants.contains(&variant) {
                variants.push(variant);
            }

            for text in carried {
                assert!(message.contains(text), "{name}: {text:?} in {message:?}");
            }
            assert_eq!(message.lines().count(), 1, "{name}: {message:?}");
            assert_eq!(
                Termination::Config(Box::new(error)).exit_code(),
                1,
                "{name}"
            );
        }

        assert_eq!(
            variants,
            [
                "FileWrite",
                "RemovedKey",
                "InvalidChoice",
                "DuplicateStandaloneName",
                "PrefixCollision",
                "RepositoriesNotArray",
                "Repository",
                "ValueType",
                "EnvVar",
                "ModelTier",
                "AgentOverridesNotTable",
                "AgentOverride"
            ]
        );
    }

    #[test]
    fn an_env_var_message_is_the_path_of_the_entry_and_its_fault_and_holds_nothing_else() {
        let cases = [
            (
                EnvVarFault::UnsupportedType {
                    found: ValueType::Array,
                },
                "env.feature.vars.API_TOKEN: a value of type array is not supported",
            ),
            (
                EnvVarFault::UnknownFields {
                    fields: strings(&["cwd", "timeout"]),
                },
                "env.feature.vars.API_TOKEN: unknown fields in the command entry: cwd, timeout",
            ),
            (
                EnvVarFault::ManagedName,
                "env.feature.vars.API_TOKEN: a command entry cannot be declared under a WINTER_ key",
            ),
            (
                EnvVarFault::MissingCommand,
                "env.feature.vars.API_TOKEN: a command entry needs a non-empty command",
            ),
            (
                EnvVarFault::InvalidFormat {
                    value: value("\"yaml\"", ValueType::String),
                },
                "env.feature.vars.API_TOKEN: format must be raw, dotenv, or json, not the string \"yaml\"",
            ),
            (
                EnvVarFault::ShellNotBoolean {
                    found: ValueType::Integer,
                },
                "env.feature.vars.API_TOKEN: shell must be a boolean, found a value of type integer",
            ),
            (
                EnvVarFault::ExportsNotStringList,
                "env.feature.vars.API_TOKEN: exports must be an array of strings",
            ),
        ];

        for (fault, expected) in cases {
            assert_eq!(env_var(fault).to_string(), expected);
        }
    }

    #[test]
    fn a_key_of_a_repository_fault_renders_as_its_name_in_the_file() {
        assert_eq!(PathField::Path.to_string(), "path");
        assert_eq!(PathField::ConfigDir.to_string(), "config_dir");
        assert_eq!(NestedOnlyKey::Envs.to_string(), "envs");
        assert_eq!(NestedOnlyKey::InheritLocal.to_string(), "inherit_local");
        assert_eq!(NestedExcludedKey::Load.to_string(), "load");
        assert_eq!(NestedExcludedKey::Entry.to_string(), "entry");
    }

    #[test]
    fn a_typed_key_and_a_merged_table_render_as_their_name_in_the_file() {
        let typed_keys = [
            (TypedKey::GitExcludes, "git_excludes"),
            (TypedKey::Cmd, "cmd"),
            (TypedKey::MainBranch, "main_branch"),
            (TypedKey::Ref, "ref"),
            (TypedKey::Name, "name"),
            (TypedKey::Url, "url"),
            (TypedKey::Prefix, "prefix"),
            (TypedKey::Path, "path"),
            (TypedKey::ConfigDir, "config_dir"),
            (TypedKey::GitUser, "git.user"),
            (TypedKey::GitUserName, "git.user.name"),
            (TypedKey::GitUserEmail, "git.user.email"),
        ];
        let merged_tables = [
            (MergedTable::Git, "git"),
            (MergedTable::Keybindings, "keybindings"),
            (MergedTable::Tui, "tui"),
            (MergedTable::Capabilities, "capabilities"),
            (MergedTable::Env, "env"),
            (MergedTable::ModelTiers, "model_tiers"),
            (MergedTable::AgentModelOverrides, "agent_model_overrides"),
        ];

        for (key, name) in typed_keys {
            assert_eq!(key.to_string(), name, "{key:?}");
        }
        for (table, name) in merged_tables {
            assert_eq!(table.to_string(), name, "{table:?}");
            assert_eq!(TypedKey::Table(table).to_string(), name, "{table:?}");
        }
    }

    #[test]
    fn the_removed_env_vars_message_lists_the_keys_found_and_is_whole_without_any() {
        let with_keys = RemovedKey::EnvVars {
            keys: strings(&["API_TOKEN", "REGION"]),
        };
        let without_keys = RemovedKey::EnvVars { keys: Vec::new() };

        assert_eq!(
            ConfigError::RemovedKey(with_keys).to_string(),
            "[env.vars] is no longer read: use [env.feature.vars] or [env.workspace.vars] instead; \
             it holds API_TOKEN, REGION"
        );
        assert_eq!(
            ConfigError::RemovedKey(without_keys).to_string(),
            "[env.vars] is no longer read: use [env.feature.vars] or [env.workspace.vars] instead"
        );
    }

    #[test]
    fn the_message_of_a_model_id_is_true_of_an_empty_string_and_of_another_type() {
        let empty = model_tier(ModelTierFault::ModelIdNotString {
            vendor: "claude".to_owned(),
            found: ValueType::String,
        });
        let another_type = model_tier(ModelTierFault::ModelIdNotString {
            vendor: "codex".to_owned(),
            found: ValueType::Float,
        });

        assert_eq!(
            empty.to_string(),
            "model_tiers.quick: the model id of claude must be a non-empty string, \
             found a value of type string"
        );
        assert_eq!(
            another_type.to_string(),
            "model_tiers.quick: the model id of codex must be a non-empty string, \
             found a value of type float"
        );
    }

    #[test]
    fn a_value_type_error_names_the_type_its_key_accepts_and_the_repository_that_holds_the_key() {
        let of_the_document = ConfigError::ValueType {
            key: TypedKey::GitExcludes,
            owner: None,
            found: ValueType::String,
        };
        let of_a_repository = ConfigError::ValueType {
            key: TypedKey::Ref,
            owner: Some(RepoRef {
                kind: RepoKind::Standalone,
                label: "tools".to_owned(),
            }),
            found: ValueType::Integer,
        };
        let of_a_table = ConfigError::ValueType {
            key: TypedKey::GitUser,
            owner: None,
            found: ValueType::Array,
        };

        assert_eq!(
            of_the_document.to_string(),
            "git_excludes must be an array of strings, found a value of type string"
        );
        assert_eq!(
            of_a_repository.to_string(),
            "ref of the standalone repository tools must be a string, found a value of type integer"
        );
        assert_eq!(
            of_a_table.to_string(),
            "git.user must be a table, found a value of type array"
        );
    }

    #[test]
    fn the_root_not_found_message_names_the_start_directory_and_the_file_looked_for() {
        let message = root_not_found().to_string();

        assert!(message.contains("/somewhere/below"), "{message}");
        assert!(message.contains(".winter/config.toml"), "{message}");
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
}
