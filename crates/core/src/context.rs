//! What a command receives.

use std::io::{BufRead, Write};
use std::path::Path;

use crate::files::{ReadFiles, WriteFiles};
use crate::process::{Identity, ProcessEnv};
use crate::termination::Usage;

/// The working directory, environment, identity, and stream handles of one invocation, and the node being run.
#[allow(
    dead_code,
    reason = "the command contract: a field stays whether or not a present command reads it"
)]
pub(crate) struct Context<'a> {
    pub cwd: &'a Path,
    pub env: &'a ProcessEnv,
    pub identity: &'a Identity,
    pub stdin: &'a mut dyn BufRead,
    pub out: &'a mut dyn Write,
    pub err: &'a mut dyn Write,
    pub out_is_terminal: bool,
    pub err_is_terminal: bool,
    pub files: &'a dyn ReadFiles,
    pub file_writer: &'a dyn WriteFiles,
    pub invoked: &'a Invoked,
}

/// The node being run, as the user typed it. For an alias the path holds the alias name.
#[allow(dead_code, reason = "as Context")]
#[derive(Debug)]
pub(crate) struct Invoked {
    /// The command names from the root down: `["ws", "index"]`.
    pub path: Vec<String>,
    /// The usage of the node at `path`.
    pub usage: Usage,
}
