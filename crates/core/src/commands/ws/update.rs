//! `ws update`: re-pins the selected standalone repositories and reports each as it is reached.

use clap::{Arg, ArgAction, ArgMatches};

use crate::arguments;
use crate::context::Context;
use crate::diagnostics::{self, Diagnostics};
use crate::output;
use crate::pattern_match;
use crate::pins::outcome::PinResult;
use crate::pins::repin::{self, PinEvent, RunEnd};
use crate::pins::{PinError, Ports};
use crate::registry::Registration;
use crate::termination::{CommandError, Termination};

pub(crate) const REGISTRATION: Registration = Registration::Command {
    args,
    run,
    hidden: false,
};

const REPOS: &str = "REPOS";

/// What the arguments ask for.
#[derive(Debug, PartialEq, Eq)]
struct Params {
    repos: Vec<String>,
    autostash: bool,
    json: bool,
}

fn args(command: clap::Command) -> clap::Command {
    command
        .about("Re-resolve `ref` pins for standalone repos and rewrite the lock")
        .arg(
            Arg::new("repos")
                .value_name(REPOS)
                .action(ArgAction::Append)
                .help("The standalone repositories to re-pin, by name or glob; all pinned ones when none is given"),
        )
        .arg(
            Arg::new("autostash")
                .long("autostash")
                .action(ArgAction::SetTrue)
                .help("Stash a dirty working tree before re-pinning, and restore it after"),
        )
        .arg(
            Arg::new("json")
                .long("json")
                .action(ArgAction::SetTrue)
                .help("Print each event as one line of JSON"),
        )
}

/// The parameters of the arguments, each REPO checked in order to be a bare name: the first that is none is
/// the command error of the invocation.
fn params(matches: &ArgMatches) -> Result<Params, Termination> {
    let repos: Vec<String> = arguments::repeated::<String>(matches, "repos")?
        .into_iter()
        .cloned()
        .collect();
    for repo in &repos {
        pattern_match::bare_name(repo)
            .map_err(|error| Termination::Command(Box::new(CommandError::Selection(error))))?;
    }
    Ok(Params {
        repos,
        autostash: matches.get_flag("autostash"),
        json: matches.get_flag("json"),
    })
}

fn run(matches: &ArgMatches, cx: &mut Context<'_>) -> Termination {
    let params = match params(matches) {
        Ok(params) => params,
        Err(termination) => return termination,
    };
    let mut diagnostics = Diagnostics {
        err: &mut *cx.err,
        log: cx.log,
    };
    let ports = Ports {
        files: cx.files,
        writer: cx.file_writer,
        programs: cx.programs,
        tracer: cx.tracer,
    };
    let mut results = output::Results::new(&mut *cx.out);
    let terminal = cx.out_is_terminal;
    let json = params.json;
    let mut synced = false;
    let mut sink = |event: &PinEvent, diagnostics: &mut Diagnostics<'_>| {
        if json {
            return results.event(&document(event));
        }
        match row(event) {
            Row::Out(line) => results.line(&line)?,
            Row::Err(line) => diagnostics::notice(diagnostics.err, &line),
        }
        match event {
            PinEvent::Synced(_) => synced = true,
            PinEvent::Completed { .. } if !synced => results.line(&nothing_to_update(terminal))?,
            PinEvent::Started | PinEvent::Completed { .. } => {}
        }
        Ok(())
    };
    let ended = repin::repin(
        ports,
        cx.env,
        &mut diagnostics,
        cx.cwd,
        &params.repos,
        params.autostash,
        &mut sink,
    );
    match ended {
        Ok(RunEnd::Completed { success: true }) => Termination::Success,
        Ok(RunEnd::Completed { success: false }) => Termination::Reported,
        Ok(RunEnd::Stopped(termination)) => termination,
        Err(PinError::Repo(error)) => Termination::Repo(error),
        Err(PinError::Config(error)) => Termination::Config(error),
        Err(PinError::Internal(error)) => Termination::Internal(error),
    }
}

/// The `--json` line of one event.
fn document(event: &PinEvent) -> serde_json::Value {
    match event {
        PinEvent::Started => serde_json::json!({"type": "pull_started"}),
        PinEvent::Synced(outcome) => serde_json::json!({
            "type": "repo_synced",
            "scope": "standalone",
            "repo": outcome.repo,
            "result": outcome.result.wire_name(),
            "commits": 0,
            "ahead": 0,
            "behind": 0,
            "pin_ref": outcome.pin_ref,
        }),
        PinEvent::Completed { success } => {
            serde_json::json!({"type": "pull_completed", "success": success})
        }
    }
}

/// One human line of an event, and the stream it belongs on. Neither holds its last newline.
#[derive(Debug, PartialEq, Eq)]
enum Row {
    Out(String),
    Err(String),
}

/// The human line of one event. The line of a completion starts with a newline, which gives the blank line
/// before it.
fn row(event: &PinEvent) -> Row {
    match event {
        PinEvent::Started => Row::Out("→ pulling".to_owned()),
        PinEvent::Synced(outcome) => {
            let prefix = format!("[standalone/{}]", outcome.repo);
            let detail = &outcome.pin_ref;
            match outcome.result {
                PinResult::UpToDate => Row::Out(format!("{prefix} up to date")),
                PinResult::RePinned => Row::Out(format!("{prefix} re-pinned → {detail}")),
                PinResult::HeldPin => Row::Out(format!("{prefix} held @ {detail}")),
                PinResult::Pinned => Row::Out(format!("{prefix} pinned → {detail}")),
                PinResult::AlreadyPinned => Row::Out(format!("{prefix} already pinned @ {detail}")),
                PinResult::Refused => Row::Err(format!("{prefix} refused: {detail}")),
                PinResult::PinError => Row::Err(format!("{prefix} pin error: {detail}")),
            }
        }
        PinEvent::Completed { success: true } => Row::Out("\n✓ pull complete".to_owned()),
        PinEvent::Completed { success: false } => Row::Err("\n✗ pull had errors".to_owned()),
    }
}

/// The line of a run that reached no standalone: dim on a terminal, plain otherwise.
fn nothing_to_update(terminal: bool) -> String {
    const LINE: &str = "Nothing to update";
    if terminal {
        format!("\x1b[2m{LINE}\x1b[0m")
    } else {
        LINE.to_owned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pins::outcome::PinOutcome;
    use crate::termination::SelectionError;

    fn parsed(arguments: &[&str]) -> Result<ArgMatches, clap::Error> {
        args(clap::Command::new("update").no_binary_name(true)).try_get_matches_from(arguments)
    }

    fn read(arguments: &[&str]) -> Result<Params, Termination> {
        params(&parsed(arguments).expect("accepted arguments"))
    }

    fn synced(result: PinResult, pin_ref: &str) -> PinEvent {
        PinEvent::Synced(PinOutcome {
            repo: "lib".to_owned(),
            result,
            pin_ref: pin_ref.to_owned(),
        })
    }

    #[test]
    fn the_arguments_pass_the_parser_s_own_checks() {
        args(clap::Command::new("update")).debug_assert();
    }

    #[test]
    fn no_argument_asks_for_every_pinned_standalone_in_text_without_a_stash() {
        assert_eq!(
            read(&[]).expect("parameters"),
            Params {
                repos: Vec::new(),
                autostash: false,
                json: false,
            }
        );
    }

    #[test]
    fn the_repos_come_in_command_line_order_with_both_flags() {
        assert_eq!(
            read(&["b", "--json", "lib-[ab]", "--autostash", "a*"]).expect("parameters"),
            Params {
                repos: vec!["b".to_owned(), "lib-[ab]".to_owned(), "a*".to_owned()],
                autostash: true,
                json: true,
            }
        );
    }

    #[test]
    fn the_freeze_and_force_options_are_unknown() {
        for option in ["--freeze", "--force"] {
            let error = parsed(&[option]).expect_err("an unknown option");

            assert_eq!(
                error.kind(),
                clap::error::ErrorKind::UnknownArgument,
                "{option}"
            );
        }
    }

    #[test]
    fn an_empty_repo_is_refused_as_an_empty_pattern() {
        let termination = read(&["lib", ""]).expect_err("a refusal");

        assert!(
            matches!(
                &termination,
                Termination::Command(error)
                    if matches!(**error, CommandError::Selection(SelectionError::EmptyPattern))
            ),
            "{termination:?}"
        );
        assert_eq!(termination.exit_code(), 1);
    }

    #[test]
    fn the_first_repo_that_holds_a_slash_is_refused_by_name() {
        let termination = read(&["lib", "a/b", "c/d", ""]).expect_err("a refusal");

        assert!(
            matches!(
                &termination,
                Termination::Command(error) if matches!(
                    &**error,
                    CommandError::Selection(SelectionError::NotBareName { pattern }) if pattern == "a/b"
                )
            ),
            "{termination:?}"
        );
    }

    #[test]
    fn each_event_has_its_json_line() {
        assert_eq!(
            document(&PinEvent::Started),
            serde_json::json!({"type": "pull_started"})
        );
        assert_eq!(
            document(&synced(PinResult::RePinned, "0123abcd")),
            serde_json::json!({
                "type": "repo_synced",
                "scope": "standalone",
                "repo": "lib",
                "result": "re_pinned",
                "commits": 0,
                "ahead": 0,
                "behind": 0,
                "pin_ref": "0123abcd",
            })
        );
        assert_eq!(
            document(&synced(PinResult::UpToDate, ""))["pin_ref"],
            serde_json::json!("")
        );
        for success in [true, false] {
            assert_eq!(
                document(&PinEvent::Completed { success }),
                serde_json::json!({"type": "pull_completed", "success": success})
            );
        }
    }

    #[test]
    fn the_start_and_the_results_that_are_no_failure_are_rows_of_stdout() {
        let out = |line: &str| Row::Out(line.to_owned());

        assert_eq!(row(&PinEvent::Started), out("→ pulling"));
        assert_eq!(
            row(&synced(PinResult::UpToDate, "")),
            out("[standalone/lib] up to date")
        );
        assert_eq!(
            row(&synced(PinResult::RePinned, "0123abcd")),
            out("[standalone/lib] re-pinned → 0123abcd")
        );
        assert_eq!(
            row(&synced(PinResult::HeldPin, "v1")),
            out("[standalone/lib] held @ v1")
        );
        assert_eq!(
            row(&synced(PinResult::Pinned, "0123abcd")),
            out("[standalone/lib] pinned → 0123abcd")
        );
        assert_eq!(
            row(&synced(PinResult::AlreadyPinned, "v1")),
            out("[standalone/lib] already pinned @ v1")
        );
    }

    #[test]
    fn a_pin_error_and_a_refusal_are_rows_of_stderr() {
        assert_eq!(
            row(&synced(PinResult::PinError, "the reason\n  cwd: /w/lib")),
            Row::Err("[standalone/lib] pin error: the reason\n  cwd: /w/lib".to_owned())
        );
        assert_eq!(
            row(&synced(PinResult::Refused, "the reason")),
            Row::Err("[standalone/lib] refused: the reason".to_owned())
        );
    }

    #[test]
    fn a_completion_follows_a_blank_line_on_stdout_when_it_succeeded_and_on_stderr_otherwise() {
        assert_eq!(
            row(&PinEvent::Completed { success: true }),
            Row::Out("\n✓ pull complete".to_owned())
        );
        assert_eq!(
            row(&PinEvent::Completed { success: false }),
            Row::Err("\n✗ pull had errors".to_owned())
        );
    }

    #[test]
    fn nothing_to_update_is_dim_on_a_terminal_and_plain_otherwise() {
        assert_eq!(nothing_to_update(false), "Nothing to update");
        assert_eq!(nothing_to_update(true), "\x1b[2mNothing to update\x1b[0m");
    }
}
