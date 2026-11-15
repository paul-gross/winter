//! The library of the `winter-workspace` binary: parsing, commands, domain, and adapters.
//!
//! The library returns values. It writes results to the handles of the [`Invocation`] it is given and never
//! names a process stream, the process environment, or a process exit; the binary binds those and maps the
//! returned [`Termination`] to an exit code.

mod arguments;
mod config_edit;
mod config_raw;
mod context;
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
mod workspace_files;
mod workspace_root;

pub use config_edit::{CatalogError, RepositoryKind};
pub use process::{Identity, Invocation, ProcessEnv, run};
pub use termination::{
    CommandError, ConfigError, FileCause, InternalError, ParseFault, Termination, TomlParseError,
    Usage, UsageError,
};
