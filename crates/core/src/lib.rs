//! The library of the `winter-workspace` binary: parsing, commands, domain, and adapters.
//!
//! The library returns values. It writes results to the handles of the [`Invocation`] it is given and never
//! names a process stream, the process environment, or a process exit; the binary binds those and maps the
//! returned [`Termination`] to an exit code.

mod arguments;
mod config_edit;
mod config_file;
mod config_load;
mod config_lock;
mod config_merge;
mod config_model;
mod context;
mod diagnostics;
#[cfg(test)]
#[path = "../build/discover.rs"]
mod discover;
mod drift;
mod env_index;
mod environments;
mod files;
mod fingerprint;
mod git;
mod groups;
mod index_lookup;
mod locations;
mod logging;
mod output;
mod parse;
mod pins;
mod process;
mod programs;
mod registry;
mod repository_catalog;
mod state_registry;
mod termination;
mod trace;
mod workspace_files;
mod workspace_root;

pub use config_edit::{CatalogError, RepositoryKind, StandaloneOption};
pub use config_lock::{LockEntry, LockEntryFault, LockError, RefKind, read_lock, upsert_lock};
pub use config_model::{
    AdoptExtensions, AgentOverride, DashboardLayout, EnvBands, EnvCommand, EnvFormat, EnvValue,
    EnvVar, FileSizeLimits, GitIdentity, Keybindings, LoadMode, ModelTier, Nested, Ports,
    ProjectRepository, RawArray, RawTable, RepositoryCommon, Space, StandaloneRepository, Vendor,
    VendorModel, Workspace, WorkspaceConfig,
};
pub use diagnostics::Diagnostics;
pub use files::{OsFiles, ReadFiles, WriteFiles};
pub use git::{
    GitFailure, Sha, checkout_branch, checkout_detached, clean, fetch, head, stash_pop, stash_push,
    verify,
};
pub use logging::{Level, Log};
pub use pins::snapshot::{PinSnapshot, snapshot};
pub use pins::{PinError, Ports as PinPorts};
pub use process::{Identity, Interrupts, Invocation, ProcessEnv, run};
pub use programs::{Call, Exit, Finished, OsPrograms, Pacing, Program, RunPrograms};
pub use termination::{
    AgentOverrideFault, ChoiceKey, CommandError, ConfigError, ConfigFileName, ConfigValue,
    EntryIdent, EnvVarFault, FileCause, GitOperation, InternalError, MergedTable, ModelTierFault,
    NestedExcludedKey, NestedOnlyKey, ParseFault, PathField, RemovedKey, RepoError, RepoKind,
    RepoMessage, RepoRef, RepositoryFault, SelectionError, Termination, TomlParseError, TypedKey,
    Usage, UsageError, UsageRule, ValueType,
};
pub use trace::{
    CommandTracer, NoTrace, OperationSpan, TraceConfig, TraceLog, TraceSettings, settings,
    span_name,
};
