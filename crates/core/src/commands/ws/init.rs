//! `ws init`: reconcile source checkouts, standalones, and managed workspace content.

use clap::{Arg, ArgAction, ArgMatches};

use crate::bootstrap::{self, InitError, InitEvent, TextStream};
use crate::context::Context;
use crate::diagnostics::{self, Diagnostics};
use crate::output;
use crate::pins::repin::RunEnd;
use crate::ports::Ports;
use crate::registry::Registration;
use crate::termination::Termination;

pub(crate) const REGISTRATION: Registration = Registration::Command {
    args,
    run,
    hidden: false,
};

fn args(command: clap::Command) -> clap::Command {
    command
        .about("Reconcile source checkouts and standalone repositories")
        .arg(
            Arg::new("json")
                .long("json")
                .action(ArgAction::SetTrue)
                .help("Print each event as one line of JSON"),
        )
}

fn run(matches: &ArgMatches, cx: &mut Context<'_>) -> Termination {
    let json = matches.get_flag("json");
    let ports = Ports {
        files: cx.files,
        writer: cx.file_writer,
        programs: cx.programs,
        tracer: cx.tracer,
    };
    let mut diagnostics = Diagnostics {
        err: &mut *cx.err,
        log: cx.log,
    };
    let mut results = output::Results::new(&mut *cx.out);
    let mut sink = |event: &InitEvent, diagnostics: &mut Diagnostics<'_>| {
        if json {
            return results.event(&document(event));
        }
        match bootstrap::text(event) {
            Some((TextStream::Stdout, line)) => results.line(&line)?,
            Some((TextStream::Stderr, line)) => diagnostics::notice(diagnostics.err, &line),
            None => {}
        }
        Ok(())
    };
    match bootstrap::reconcile(&ports, cx.env, &mut diagnostics, cx.cwd, &mut sink) {
        Ok(RunEnd::Completed { success: true }) => Termination::Success,
        Ok(RunEnd::Completed { success: false }) => Termination::Reported,
        Ok(RunEnd::Stopped(termination)) => termination,
        Err(InitError::Config(error)) => Termination::Config(error),
        Err(InitError::Internal(error)) => Termination::Internal(error),
    }
}

/// One shape-C event, without an invocation-level envelope.
fn document(event: &InitEvent) -> serde_json::Value {
    match event {
        InitEvent::TargetStarted { target } => {
            serde_json::json!({"type": "target_started", "target": target})
        }
        InitEvent::TargetCompleted { target, success } => {
            serde_json::json!({"type": "target_completed", "target": target, "success": success})
        }
        InitEvent::RepoAction {
            repo,
            location,
            action,
            detail,
        } => {
            let action = action.wire_name();
            serde_json::json!({
                "type": "repo_action", "repo": repo, "location": location,
                "action": action, "detail": detail,
            })
        }
        InitEvent::RepoError { repo, error } => {
            serde_json::json!({"type": "repo_error", "repo": repo, "error": error})
        }
        InitEvent::CmdStarted { repo, command } => {
            serde_json::json!({"type": "cmd_started", "repo": repo, "command": command})
        }
        InitEvent::CmdOutputLine { repo, line } => {
            serde_json::json!({"type": "cmd_output_line", "repo": repo, "line": line})
        }
        InitEvent::CmdCompleted {
            repo,
            command,
            returncode,
        } => serde_json::json!({
            "type": "cmd_completed", "repo": repo, "command": command, "returncode": returncode,
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bootstrap::Action;

    fn parsed(arguments: &[&str]) -> Result<ArgMatches, clap::Error> {
        args(clap::Command::new("init").no_binary_name(true)).try_get_matches_from(arguments)
    }

    #[test]
    fn arguments_accept_only_bare_reconciliation_and_json() {
        args(clap::Command::new("init")).debug_assert();
        assert!(!parsed(&[]).expect("bare init").get_flag("json"));
        assert!(parsed(&["--json"]).expect("JSON init").get_flag("json"));
        for arguments in [&["alpha"][..], &["--all"][..]] {
            assert_eq!(
                parsed(arguments).expect_err("unsupported form").kind(),
                clap::error::ErrorKind::UnknownArgument
            );
        }
    }

    #[test]
    fn actions_keep_all_fields_and_their_wire_names() {
        for action in [
            Action::Cloned,
            Action::Exists,
            Action::ExcludesUpdated,
            Action::Pinned,
            Action::WorkspaceExcludesUpdated,
            Action::AgentsMdUpdated,
        ] {
            assert_eq!(
                document(&InitEvent::RepoAction {
                    repo: "api".into(),
                    location: "/w/projects/api".into(),
                    action,
                    detail: "".into(),
                }),
                serde_json::json!({
                    "type": "repo_action", "repo": "api", "location": "/w/projects/api",
                    "action": action.wire_name(), "detail": "",
                })
            );
        }
    }

    #[test]
    fn target_and_command_events_keep_their_fields() {
        assert_eq!(
            document(&InitEvent::TargetStarted {
                target: "projects/".into()
            }),
            serde_json::json!({"type": "target_started", "target": "projects/"})
        );
        for success in [true, false] {
            assert_eq!(
                document(&InitEvent::TargetCompleted {
                    target: "projects/".into(),
                    success
                }),
                serde_json::json!({"type": "target_completed", "target": "projects/", "success": success})
            );
        }
        assert_eq!(
            document(&InitEvent::RepoError {
                repo: "api".into(),
                error: "failed\nstep".into()
            }),
            serde_json::json!({"type": "repo_error", "repo": "api", "error": "failed\nstep"})
        );
        assert_eq!(
            document(&InitEvent::CmdStarted {
                repo: "api".into(),
                command: "make setup".into()
            }),
            serde_json::json!({"type": "cmd_started", "repo": "api", "command": "make setup"})
        );
        assert_eq!(
            document(&InitEvent::CmdOutputLine {
                repo: "api".into(),
                line: "".into()
            }),
            serde_json::json!({"type": "cmd_output_line", "repo": "api", "line": ""})
        );
        for returncode in [0, 7, -15] {
            assert_eq!(
                document(&InitEvent::CmdCompleted {
                    repo: "api".into(),
                    command: "make setup".into(),
                    returncode
                }),
                serde_json::json!({"type": "cmd_completed", "repo": "api", "command": "make setup", "returncode": returncode})
            );
        }
    }
}
