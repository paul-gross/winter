//! What a command receives.

use std::ffi::OsString;
use std::io::{BufRead, Write};
use std::path::Path;

use crate::files::{ReadFiles, WriteFiles};
use crate::logging::Log;
use crate::process::{Identity, Interrupts, ProcessEnv};
use crate::programs::RunPrograms;
use crate::termination::Usage;
use crate::trace::CommandTracer;

/// The working directory, environment, identity, and stream handles of one invocation, its file and program
/// ports, the tracer, the node being run, the root option values, what is logged, and the interrupt state of the
/// process.
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
    pub programs: &'a dyn RunPrograms,
    pub tracer: &'a dyn CommandTracer,
    pub invoked: &'a Invoked,
    pub root: &'a RootOptions,
    pub log: Log,
    pub interrupts: &'a Interrupts,
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

/// The root option values a command can read. Verbosity is not among them: it reaches logging and nothing else.
#[allow(dead_code, reason = "as Context")]
#[derive(Debug, Default, PartialEq, Eq)]
pub(crate) struct RootOptions {
    /// The effective provider override: the root option when its value is not empty, else the variable
    /// `WINTER_SERVICE_ORCHESTRATOR` when it is set and not empty, its bytes as given.
    pub service_orchestrator: Option<OsString>,
    /// The value of `--source-override` as given.
    pub source_override: Option<String>,
}
