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
            return results.event(&bootstrap::document(event));
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

#[cfg(test)]
mod tests {
    use super::*;

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
}
