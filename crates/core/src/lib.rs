//! The library of the `winter-workspace` binary: parsing, commands, domain, and adapters.
//!
//! The library returns values. It writes results to the handles of the [`Invocation`] it is given and never
//! names a process stream, the process environment, or a process exit; the binary binds those and maps the
//! returned [`Termination`] to an exit code.

mod context;
#[cfg(test)]
#[path = "../build/discover.rs"]
mod discover;
mod groups;
mod parse;
mod process;
mod registry;
mod termination;

pub use process::{Identity, Invocation, ProcessEnv, run};
pub use termination::{
    CommandError, ConfigError, FileCause, InternalError, ParseFault, Termination, TomlParseError,
    UsageError,
};
