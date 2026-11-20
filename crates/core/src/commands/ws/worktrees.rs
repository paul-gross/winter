//! `ws worktrees`: the checkout locations of the workspace that exist.

use clap::{Arg, ArgAction, ArgMatches};

use crate::context::Context;
use crate::locations::{self, ListError, Location, LocationKind};
use crate::output;
use crate::registry::Registration;
use crate::termination::Termination;

pub(crate) const REGISTRATION: Registration = Registration::Command {
    args,
    run,
    hidden: false,
};

fn args(command: clap::Command) -> clap::Command {
    command
        .about("List the worktree, standalone, and workspace locations that exist")
        .arg(
            Arg::new("json")
                .long("json")
                .action(ArgAction::SetTrue)
                .help("Print the result as one JSON document"),
        )
}

fn run(matches: &ArgMatches, cx: &mut Context<'_>) -> Termination {
    let listed = match locations::list(cx.files, cx.cwd) {
        Ok(listed) => listed,
        Err(ListError::Config(error)) => return Termination::Config(Box::new(error)),
        Err(ListError::Internal(error)) => return Termination::Internal(Box::new(error)),
    };
    if matches.get_flag("json") {
        output::document(cx.out, &document(&listed))
    } else {
        output::text(cx.out, &table(&listed))
    }
}

fn kind_name(kind: LocationKind) -> &'static str {
    match kind {
        LocationKind::Worktree => "worktree",
        LocationKind::Standalone => "standalone",
        LocationKind::Workspace => "workspace",
    }
}

/// One object per location, in order.
fn document(listed: &[Location]) -> serde_json::Value {
    listed
        .iter()
        .map(|location| {
            serde_json::json!({
                "kind": kind_name(location.kind),
                "env": location.env,
                "repo": location.repo,
                "name": location.name,
                "label": location.label,
                "path": location.path.to_string_lossy(),
            })
        })
        .collect()
}

/// One row per location: the label, the kind, and the path.
fn table(listed: &[Location]) -> String {
    let rows: Vec<Vec<String>> = listed
        .iter()
        .map(|location| {
            vec![
                location.label.clone(),
                kind_name(location.kind).to_owned(),
                location.path.to_string_lossy().into_owned(),
            ]
        })
        .collect();
    output::table(&rows)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parsed(arguments: &[&str]) -> Result<ArgMatches, clap::Error> {
        args(clap::Command::new("worktrees").no_binary_name(true)).try_get_matches_from(arguments)
    }

    #[test]
    fn the_arguments_pass_the_parser_s_own_checks() {
        args(clap::Command::new("worktrees")).debug_assert();
    }

    #[test]
    fn only_the_json_flag_is_accepted() {
        assert!(parsed(&["--json"]).is_ok());
        assert!(parsed(&["--status"]).is_err());
        assert!(parsed(&["x"]).is_err());
    }

    #[test]
    fn the_document_has_one_object_per_location_without_counts() {
        let listed = vec![Location {
            kind: LocationKind::Worktree,
            env: Some("alpha".into()),
            repo: Some("api".into()),
            name: None,
            label: "alpha/api".into(),
            path: "/ws/alpha/api".into(),
        }];

        assert_eq!(
            document(&listed),
            serde_json::json!([{"kind": "worktree", "env": "alpha", "repo": "api", "name": null,
                "label": "alpha/api", "path": "/ws/alpha/api"}])
        );
    }
}
