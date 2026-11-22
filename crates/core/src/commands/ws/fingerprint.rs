//! `ws fingerprint`: the content digest of the workspace definition, and with `--json` the parts it is made
//! from.

use std::path::PathBuf;

use clap::{Arg, ArgAction, ArgMatches};

use crate::context::Context;
use crate::fingerprint::{self, FingerprintError, RepoFingerprint, WorkspaceFingerprint};
use crate::output;
use crate::process::ProcessEnv;
use crate::registry::Registration;
use crate::termination::Termination;

pub(crate) const REGISTRATION: Registration = Registration::Command {
    args,
    run,
    hidden: false,
};

fn args(command: clap::Command) -> clap::Command {
    command
        .about("Print the content digest of the workspace definition")
        .arg(
            Arg::new("json")
                .long("json")
                .action(ArgAction::SetTrue)
                .help("Print the digest and the parts it is made from as one JSON document"),
        )
}

/// The directory scratch indexes are made under: `TMPDIR`, or `/tmp` when it is unset or empty.
fn temp(env: &ProcessEnv) -> PathBuf {
    env.get("TMPDIR")
        .filter(|value| !value.is_empty())
        .map_or_else(|| PathBuf::from("/tmp"), PathBuf::from)
}

fn json(matches: &ArgMatches) -> bool {
    matches.get_flag("json")
}

fn run(matches: &ArgMatches, cx: &mut Context<'_>) -> Termination {
    let temp = temp(cx.env);
    let fingerprint =
        match fingerprint::compute(cx.files, cx.scratch, cx.programs, cx.tracer, &temp, cx.cwd) {
            Ok(fingerprint) => fingerprint,
            Err(FingerprintError::Config(error)) => return Termination::Config(Box::new(error)),
            Err(FingerprintError::Repo(error)) => return Termination::Repo(error),
            Err(FingerprintError::Internal(error)) => return Termination::Internal(error),
        };
    if json(matches) {
        output::document(cx.out, &document(&fingerprint))
    } else {
        output::line(cx.out, &fingerprint.digest)
    }
}

fn part(repo: &RepoFingerprint) -> serde_json::Value {
    serde_json::json!({
        "name": repo.name,
        "commit": repo.commit,
        "tree": repo.tree,
        "dirty": repo.dirty,
    })
}

fn document(fingerprint: &WorkspaceFingerprint) -> serde_json::Value {
    serde_json::json!({
        "digest": fingerprint.digest,
        "workspace": part(&fingerprint.workspace),
        "standalones": fingerprint.standalones.iter().map(part).collect::<Vec<_>>(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env(pairs: &[(&str, &str)]) -> ProcessEnv {
        ProcessEnv::from_vars(
            pairs
                .iter()
                .map(|(name, value)| ((*name).into(), (*value).into())),
        )
    }

    #[test]
    fn the_temp_directory_is_tmpdir_and_falls_back_to_tmp_when_it_is_unset_or_empty() {
        assert_eq!(temp(&env(&[("TMPDIR", "/x")])), PathBuf::from("/x"));
        assert_eq!(temp(&env(&[])), PathBuf::from("/tmp"));
        assert_eq!(temp(&env(&[("TMPDIR", "")])), PathBuf::from("/tmp"));
    }

    #[test]
    fn the_json_flag_is_read_from_the_arguments() {
        let command = args(clap::Command::new("fingerprint"));

        let plain = command.clone().get_matches_from(["fingerprint"]);
        let asked = command.get_matches_from(["fingerprint", "--json"]);

        assert!(!json(&plain));
        assert!(json(&asked));
    }
}
