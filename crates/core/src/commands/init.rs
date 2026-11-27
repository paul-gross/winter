//! Create a workspace in the caller's directory.

use crate::agents_md::{Conflict, Placement, Shim};
use crate::bootstrap::{CreateError, create};
use crate::context::Context;
use crate::diagnostics;
use crate::output;
use crate::registry::Registration;
use crate::termination::{CommandError, Termination};
use clap::{Arg, ArgAction, ArgMatches};

pub(crate) const REGISTRATION: Registration = Registration::Command {
    args,
    run,
    hidden: false,
};

fn args(command: clap::Command) -> clap::Command {
    command
        .about("Create a workspace in the current directory")
        .arg(
            Arg::new("json")
                .long("json")
                .action(ArgAction::SetTrue)
                .help("Print one JSON document"),
        )
}

fn run(matches: &ArgMatches, cx: &mut Context<'_>) -> Termination {
    let created = match create(cx.file_writer, cx.files, cx.cwd, cx.identity.version) {
        Ok(created) => created,
        Err(CreateError::Bootstrap(error)) => {
            return Termination::Command(Box::new(CommandError::Bootstrap(error)));
        }
        Err(CreateError::Config(error)) => return Termination::Config(error),
    };
    let (agents, agents_text) = match &created.agents_md {
        Placement::Created => ("created", "placed the workspace safety rules"),
        Placement::Updated => ("updated", "updated the workspace safety rules"),
        Placement::Unchanged => ("unchanged", "workspace safety rules already current"),
        Placement::Absent => unreachable!("placement always places or reports a conflict"),
        Placement::Conflict(conflict) => {
            let (path, reason) = match conflict {
                Conflict::MalformedBlock { path } => (path, "malformed safety block"),
                Conflict::NotUtf8 { path } => (path, "not UTF-8"),
            };
            diagnostics::notice(
                cx.err,
                &format!(
                    "warning: {}: {reason}; the rules were not placed",
                    path.display()
                ),
            );
            ("conflict", "conflict; rules not placed")
        }
    };
    let (claude, claude_text) = match &created.claude_md {
        Shim::Created => ("created", "created"),
        Shim::Present => ("present", "already imports AGENTS.md"),
        Shim::Conflict { path } => {
            diagnostics::notice(
                cx.err,
                &format!(
                    "warning: {} does not import AGENTS.md; add a line `@AGENTS.md` to it",
                    path.display()
                ),
            );
            ("conflict", "conflict")
        }
    };
    if matches.get_flag("json") {
        output::document(
            cx.out,
            &serde_json::json!({"root": created.root, "config": "created", "agents_md": agents, "claude_md": claude}),
        )
    } else {
        output::text(
            cx.out,
            &format!(
                "created {}/.winter/config.toml\nAGENTS.md: {agents_text}\nCLAUDE.md: {claude_text}\nnext, declare repositories with `winter-workspace repo add` and run `winter-workspace ws init`\n",
                created.root.display()
            ),
        )
    }
}
