//! What a command receives.

use std::io::{BufRead, Write};
use std::path::Path;

use crate::process::{Identity, ProcessEnv};

/// The working directory, environment, identity, and stream handles of one invocation.
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
}
