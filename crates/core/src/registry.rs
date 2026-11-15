//! The command registry: what a command file declares, and the entries the build script generates from the
//! files under `commands/`.

use crate::context::Context;
use crate::termination::Termination;

/// What a command file declares as its `REGISTRATION`.
#[allow(
    dead_code,
    reason = "the registration contract: command files are discovered at build time, and a tree with no command or no alias is a supported state"
)]
pub(crate) enum Registration {
    Command {
        /// Receives the node, already named from its file, and returns it with its description, arguments,
        /// and options added.
        args: fn(clap::Command) -> clap::Command,
        /// Runs after parsing succeeded. Writes the result through the context; returns how the invocation
        /// ends.
        run: fn(&clap::ArgMatches, &mut Context<'_>) -> Termination,
        /// Omitted from help, still dispatched.
        hidden: bool,
    },
    /// A second name for a sibling in the same group: same arguments, same run function.
    Alias {
        /// The sibling's command name.
        of: &'static str,
    },
}

/// One discovered command file.
#[allow(dead_code, reason = "as Registration")]
pub(crate) struct Entry {
    /// The command path: `["ws", "index"]`.
    pub path: &'static [&'static str],
    /// The file that registers the command, relative to the repository root; for messages.
    pub file: &'static str,
    pub registration: &'static Registration,
}

// Defines `REGISTRY`, sorted by `path`, and declares one module per command file.
include!(concat!(env!("OUT_DIR"), "/registry.rs"));
