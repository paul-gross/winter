//! `ws worktrees`: the checkout locations of the workspace that exist, and with `--status` the git counts of
//! each.

use clap::{Arg, ArgAction, ArgMatches};

use crate::context::Context;
use crate::diagnostics::Diagnostics;
use crate::locations::{self, Counts, ListError, Location, LocationKind};
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
        .arg(
            Arg::new("status")
                .long("status")
                .action(ArgAction::SetTrue)
                .help("Add the ahead, behind, and dirty counts git gives for each location"),
        )
}

fn run(matches: &ArgMatches, cx: &mut Context<'_>) -> Termination {
    let mut diagnostics = Diagnostics {
        err: &mut *cx.err,
        log: cx.log,
    };
    let listed = match locations::list(
        cx.files,
        cx.programs,
        cx.tracer,
        &mut diagnostics,
        cx.cwd,
        matches.get_flag("status"),
    ) {
        Ok(listed) => listed,
        Err(ListError::Config(error)) => return Termination::Config(Box::new(error)),
        Err(ListError::Repo(error)) => return Termination::Repo(error),
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

/// One object per location, in order. A location that carries its counts has the keys `ahead`, `behind`,
/// and `dirty` as well, each a number or null; one that carries none has none of the three.
fn document(listed: &[Location]) -> serde_json::Value {
    listed
        .iter()
        .map(|location| {
            let mut object = serde_json::json!({
                "kind": kind_name(location.kind),
                "env": location.env,
                "repo": location.repo,
                "name": location.name,
                "label": location.label,
                "path": location.path.to_string_lossy(),
            });
            if let Some(counts) = location.status {
                object["ahead"] = counts.ahead.into();
                object["behind"] = counts.behind.into();
                object["dirty"] = counts.dirty.into();
            }
            object
        })
        .collect()
}

/// One row per location: the label, the kind, and the path, and the status cell of a location that carries
/// its counts.
fn table(listed: &[Location]) -> String {
    let rows: Vec<Vec<String>> = listed
        .iter()
        .map(|location| {
            let mut row = vec![
                location.label.clone(),
                kind_name(location.kind).to_owned(),
                location.path.to_string_lossy().into_owned(),
            ];
            row.extend(location.status.map(status_cell));
            row
        })
        .collect();
    output::table(&rows)
}

/// The counts as one cell: `+<ahead>`, `-<behind>`, and `[+<dirty>]` when something is dirty, each left out
/// when it was not counted, with a space between them; `=` when nothing is left.
fn status_cell(counts: Counts) -> String {
    let parts: Vec<String> = [
        counts.ahead.map(|ahead| format!("+{ahead}")),
        counts.behind.map(|behind| format!("-{behind}")),
        counts
            .dirty
            .filter(|dirty| *dirty > 0)
            .map(|dirty| format!("[+{dirty}]")),
    ]
    .into_iter()
    .flatten()
    .collect();
    if parts.is_empty() {
        "=".to_owned()
    } else {
        parts.join(" ")
    }
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

    fn worktree(status: Option<Counts>) -> Location {
        Location {
            kind: LocationKind::Worktree,
            env: Some("alpha".into()),
            repo: Some("api".into()),
            name: None,
            label: "alpha/api".into(),
            path: "/ws/alpha/api".into(),
            status,
        }
    }

    fn counts(ahead: Option<u64>, behind: Option<u64>, dirty: Option<u64>) -> Counts {
        Counts {
            ahead,
            behind,
            dirty,
        }
    }

    #[test]
    fn the_two_flags_are_accepted_and_nothing_else() {
        assert!(parsed(&["--json"]).is_ok());
        assert!(parsed(&["--status"]).is_ok());
        assert!(parsed(&["--status", "--json"]).is_ok());
        assert!(parsed(&["x"]).is_err());
        assert!(parsed(&["--status=1"]).is_err());
    }

    #[test]
    fn the_document_has_one_object_per_location_without_counts() {
        assert_eq!(
            document(&[worktree(None)]),
            serde_json::json!([{"kind": "worktree", "env": "alpha", "repo": "api", "name": null,
                "label": "alpha/api", "path": "/ws/alpha/api"}])
        );
    }

    #[test]
    fn a_location_that_carries_its_counts_has_the_three_keys_each_a_number_or_null() {
        let listed = [
            worktree(Some(counts(Some(2), Some(1), Some(3)))),
            worktree(Some(counts(None, None, None))),
        ];

        assert_eq!(
            document(&listed),
            serde_json::json!([
                {"kind": "worktree", "env": "alpha", "repo": "api", "name": null,
                 "label": "alpha/api", "path": "/ws/alpha/api", "ahead": 2, "behind": 1, "dirty": 3},
                {"kind": "worktree", "env": "alpha", "repo": "api", "name": null,
                 "label": "alpha/api", "path": "/ws/alpha/api", "ahead": null, "behind": null,
                 "dirty": null},
            ])
        );
    }

    #[test]
    fn the_status_cell_leaves_out_what_was_not_counted_and_a_dirty_count_of_zero() {
        for (given, cell) in [
            (counts(Some(2), Some(1), Some(3)), "+2 -1 [+3]"),
            (counts(Some(0), Some(0), Some(0)), "+0 -0"),
            (counts(None, None, None), "="),
            (counts(None, None, Some(0)), "="),
            (counts(None, None, Some(4)), "[+4]"),
            (counts(Some(1), None, None), "+1"),
        ] {
            assert_eq!(status_cell(given), cell);
        }
    }

    #[test]
    fn the_table_has_the_status_cell_only_for_a_location_that_carries_its_counts() {
        assert_eq!(
            table(&[worktree(None)]),
            "alpha/api  worktree  /ws/alpha/api\n"
        );
        assert_eq!(
            table(&[worktree(Some(counts(Some(2), Some(1), Some(3))))]),
            "alpha/api  worktree  /ws/alpha/api  +2 -1 [+3]\n"
        );
    }
}
