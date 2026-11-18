//! The library of the `winter-workspace` binary: parsing, commands, domain, and adapters.
//!
//! The library returns values. It writes results to the handles of the [`Invocation`] it is given and never
//! names a process stream, the process environment, or a process exit; the binary binds those and maps the
//! returned [`Termination`] to an exit code.

mod arguments;
mod config;
mod config_edit;
mod context;
mod diagnostics;
#[cfg(test)]
#[path = "../build/discover.rs"]
mod discover;
mod env_index;
mod files;
mod groups;
mod index_lookup;
mod logging;
mod output;
mod parse;
mod process;
mod registry;
mod repository_catalog;
mod state_registry;
mod termination;
mod trace;
mod workspace_files;
mod workspace_root;

pub use config_edit::{CatalogError, RepositoryKind};
pub use process::{Identity, Interrupts, Invocation, ProcessEnv, run};
pub use termination::{
    AgentOverrideFault, ChoiceKey, CommandError, ConfigError, ConfigValue, EntryIdent, EnvVarFault,
    FileCause, InternalError, ModelTierFault, NestedExcludedKey, NestedOnlyKey, ParseFault,
    PathField, RemovedKey, RepoKind, RepoRef, RepositoryFault, Termination, TomlParseError,
    TypedKey, Usage, UsageError, ValueType,
};
pub use trace::{
    CommandTracer, NoTrace, TraceConfig, TraceLog, TraceSettings, settings, span_name,
};
