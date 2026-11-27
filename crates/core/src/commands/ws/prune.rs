//! `ws prune`: remove material the workspace once managed and no configuration declares now.

use clap::{Arg, ArgAction, ArgMatches};

use crate::bootstrap::{self, TextStream};
use crate::confirm;
use crate::context::Context;
use crate::diagnostics::{self, Diagnostics};
use crate::output;
use crate::ports::Ports;
use crate::prune::{
    self, Orphan, OrphanKind, PruneEnd, PruneEvent, Pruned, Removal, Request, Scan, SurveyError,
};
use crate::registry::Registration;
use crate::termination::{CommandError, Termination};

pub(crate) const REGISTRATION: Registration = Registration::Command {
    args,
    run,
    hidden: false,
};

fn args(command: clap::Command) -> clap::Command {
    command
        .about("Remove material no configuration declares")
        .arg(
            Arg::new("dry-run")
                .long("dry-run")
                .action(ArgAction::SetTrue)
                .help("List what would be removed and what would stay, and remove nothing"),
        )
        .arg(
            Arg::new("force")
                .long("force")
                .action(ArgAction::SetTrue)
                .help("Remove without asking; what could hold work still stays"),
        )
        .arg(
            Arg::new("json")
                .long("json")
                .action(ArgAction::SetTrue)
                .help("Print one JSON document; needs --force or --dry-run"),
        )
}

fn request(matches: &ArgMatches) -> Request {
    Request {
        dry_run: matches.get_flag("dry-run"),
        force: matches.get_flag("force"),
        json: matches.get_flag("json"),
    }
}

fn run(matches: &ArgMatches, cx: &mut Context<'_>) -> Termination {
    let request = request(matches);
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
    let survey = match prune::survey(&ports, &mut diagnostics, cx.cwd, request) {
        Ok(survey) => survey,
        Err(SurveyError::Config(error)) => return Termination::Config(error),
        Err(SurveyError::Refused(error)) => {
            return Termination::Command(Box::new(CommandError::Bootstrap(error)));
        }
    };
    let orphans = &survey.scan.orphans;
    if request.json {
        if request.dry_run {
            return output::document(cx.out, &document(&survey.scan, None));
        }
    } else {
        if orphans.is_empty() {
            return output::line(cx.out, "Nothing to prune. Workspace is clean.");
        }
        let shown = output::text(cx.out, &preview(orphans));
        if request.dry_run || !matches!(shown, Termination::Success) {
            return shown;
        }
        if !request.force {
            let safe = orphans
                .iter()
                .filter(|orphan| orphan.retained.is_none())
                .count();
            if safe == 0 {
                return output::line(
                    cx.out,
                    "Nothing to remove (all orphans are blocked). Resolve the notes above and re-run.",
                );
            }
            let question = format!("Remove {safe} orphan(s)?");
            if let Err(ending) = confirm::confirm(&mut *cx.out, &mut *cx.stdin, &question) {
                return ending;
            }
        }
    }
    let mut results = output::Results::new(&mut *cx.out);
    let mut sink = |event: &PruneEvent<'_>, diagnostics: &mut Diagnostics<'_>| {
        match event {
            PruneEvent::Attempted { .. } if request.json => {}
            PruneEvent::Attempted { relative, removal } => {
                results.line(&attempted(&relative.display().to_string(), removal))?;
            }
            PruneEvent::Reaggregated(event) if request.json => {
                results.event(&bootstrap::document(event))?;
            }
            PruneEvent::Reaggregated(event) => match bootstrap::text(event) {
                Some((TextStream::Stdout, line)) => results.line(&line)?,
                Some((TextStream::Stderr, line)) => diagnostics::notice(diagnostics.err, &line),
                None => {}
            },
        }
        Ok(())
    };
    let pruned = match prune::prune(&ports, &mut diagnostics, &survey, &mut sink) {
        PruneEnd::Completed(pruned) => pruned,
        PruneEnd::Stopped(ending) => return ending,
    };
    if request.json {
        output::document(cx.out, &document(&survey.scan, Some(&pruned)))
    } else {
        Termination::Success
    }
}

/// What shows that the workspace once managed `orphan`.
fn evidence(orphan: &Orphan) -> String {
    match (orphan.kind, &orphan.block) {
        (OrphanKind::ProjectClone, _) => "projects/".to_owned(),
        (OrphanKind::BrokenSymlink, _) => "dangling link".to_owned(),
        (OrphanKind::StandaloneClone | OrphanKind::OrphanAgentCopy, block) => {
            format!("block {}", block.as_deref().unwrap_or_default())
        }
    }
}

/// One line per orphan, marked `!` when it stays, and under such a line why.
fn preview(orphans: &[Orphan]) -> String {
    let mut text = String::new();
    for orphan in orphans {
        let mark = if orphan.retained.is_some() { '!' } else { ' ' };
        text.push_str(&format!(
            "{mark} {:<18} {}   [{}]\n",
            orphan.kind.wire_name(),
            orphan.path.display(),
            evidence(orphan),
        ));
        if let Some(reason) = orphan.retained {
            text.push_str(&format!("  ({})\n", reason.notes()));
        }
    }
    text
}

/// The line of one attempted orphan at `relative`.
fn attempted(relative: &str, removal: &Removal) -> String {
    match removal {
        Removal::Removed => format!("  remove {relative}"),
        Removal::Skipped(skipped) => format!("  skip   {relative} ({})", skipped.notes()),
        Removal::Error(cause) => format!("  error  {relative} ({cause})"),
    }
}

/// The document of a dry run, without `pruned`, or of a removal run, with it.
fn document(scan: &Scan, pruned: Option<&Pruned>) -> serde_json::Value {
    let orphans: Vec<serde_json::Value> = scan
        .orphans
        .iter()
        .enumerate()
        .map(|(index, orphan)| {
            let scanned = orphan.retained.map_or("", |reason| reason.notes());
            let (safe, notes, action, error) =
                match pruned.and_then(|pruned| pruned.removals.get(index)) {
                    None if orphan.retained.is_none() => (true, "", "would_remove", None),
                    None => (false, scanned, "skipped", None),
                    Some(Removal::Removed) => (true, "", "removed", None),
                    Some(Removal::Skipped(skipped)) => (false, skipped.notes(), "skipped", None),
                    Some(Removal::Error(cause)) => (true, "", "error", Some(cause)),
                };
            let mut entry = serde_json::json!({
                "kind": orphan.kind.wire_name(),
                "path": orphan.path.display().to_string(),
                "safe_to_remove": safe,
                "notes": notes,
                "action": action,
            });
            if let Some(cause) = error {
                entry["error"] = serde_json::Value::from(cause.as_str());
            }
            entry
        })
        .collect();
    match pruned {
        None => serde_json::json!({"dry_run": true, "orphans": orphans}),
        Some(pruned) => serde_json::json!({
            "dry_run": false, "orphans": orphans, "excludes_updated": pruned.excludes_updated,
        }),
    }
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::*;
    use crate::prune::{Entry, Evidence, Retained, Scanned, Skipped};

    fn parsed(arguments: &[&str]) -> Result<ArgMatches, clap::Error> {
        args(clap::Command::new("prune").no_binary_name(true)).try_get_matches_from(arguments)
    }

    #[test]
    fn the_three_options_are_read_and_nothing_else_is_accepted() {
        args(clap::Command::new("prune")).debug_assert();
        let none = Request {
            dry_run: false,
            force: false,
            json: false,
        };
        assert_eq!(request(&parsed(&[]).expect("bare")), none);
        assert_eq!(
            request(&parsed(&["--dry-run"]).expect("a dry run")),
            Request {
                dry_run: true,
                ..none
            }
        );
        assert_eq!(
            request(&parsed(&["--force", "--json"]).expect("forced JSON")),
            Request {
                force: true,
                json: true,
                ..none
            }
        );
        for arguments in [&["old"][..], &["--yes"][..]] {
            assert_eq!(
                parsed(arguments).expect_err("unsupported form").kind(),
                clap::error::ErrorKind::UnknownArgument
            );
        }
    }

    fn orphan(
        kind: OrphanKind,
        path: &str,
        block: Option<&str>,
        retained: Option<Retained>,
    ) -> Orphan {
        Orphan {
            kind,
            path: PathBuf::from(path),
            block: block.map(str::to_owned),
            retained,
            scanned: Scanned {
                parent: PathBuf::from("/ws"),
                entry: Entry::Directory,
            },
        }
    }

    fn scan() -> Scan {
        Scan {
            orphans: vec![
                orphan(OrphanKind::ProjectClone, "/ws/projects/old", None, None),
                orphan(
                    OrphanKind::StandaloneClone,
                    "/ws/lib",
                    Some("lib"),
                    Some(Retained::Dirty),
                ),
                orphan(
                    OrphanKind::BrokenSymlink,
                    "/ws/.claude/skills/x",
                    None,
                    None,
                ),
                orphan(
                    OrphanKind::OrphanAgentCopy,
                    "/ws/.claude/agents/ext-a.md",
                    Some("ext"),
                    None,
                ),
            ],
            evidence: Evidence::Read,
        }
    }

    #[test]
    fn the_preview_marks_what_stays_pads_the_kind_and_names_the_evidence() {
        assert_eq!(
            preview(&scan().orphans),
            "  project_clone      /ws/projects/old   [projects/]\n\
             ! standalone_clone   /ws/lib   [block lib]\n\
             \x20 (uncommitted or untracked changes)\n\
             \x20 broken_symlink     /ws/.claude/skills/x   [dangling link]\n\
             \x20 orphan_agent_copy  /ws/.claude/agents/ext-a.md   [block ext]\n"
        );
    }

    #[test]
    fn each_attempt_has_its_line() {
        assert_eq!(
            attempted("projects/old", &Removal::Removed),
            "  remove projects/old"
        );
        assert_eq!(
            attempted(
                "lib",
                &Removal::Skipped(Skipped::Retained(Retained::Stashes))
            ),
            "  skip   lib (has stashes)"
        );
        assert_eq!(
            attempted("lib", &Removal::Skipped(Skipped::Changed)),
            "  skip   lib (changed since the scan)"
        );
        assert_eq!(
            attempted("lib", &Removal::Error("permission denied".into())),
            "  error  lib (permission denied)"
        );
    }

    #[test]
    fn the_dry_run_document_gives_each_orphan_the_action_it_would_get() {
        let document = document(&scan(), None);

        assert_eq!(
            document,
            serde_json::json!({"dry_run": true, "orphans": [
                {"kind": "project_clone", "path": "/ws/projects/old", "safe_to_remove": true,
                 "notes": "", "action": "would_remove"},
                {"kind": "standalone_clone", "path": "/ws/lib", "safe_to_remove": false,
                 "notes": "uncommitted or untracked changes", "action": "skipped"},
                {"kind": "broken_symlink", "path": "/ws/.claude/skills/x", "safe_to_remove": true,
                 "notes": "", "action": "would_remove"},
                {"kind": "orphan_agent_copy", "path": "/ws/.claude/agents/ext-a.md",
                 "safe_to_remove": true, "notes": "", "action": "would_remove"},
            ]})
        );
    }

    #[test]
    fn the_removal_document_gives_each_orphan_what_happened_and_an_error_its_cause() {
        let pruned = Pruned {
            removals: vec![
                Removal::Removed,
                Removal::Skipped(Skipped::Retained(Retained::Dirty)),
                Removal::Skipped(Skipped::Changed),
                Removal::Error("permission denied".into()),
            ],
            excludes_updated: true,
        };

        let document = document(&scan(), Some(&pruned));

        assert_eq!(
            document,
            serde_json::json!({"dry_run": false, "excludes_updated": true, "orphans": [
                {"kind": "project_clone", "path": "/ws/projects/old", "safe_to_remove": true,
                 "notes": "", "action": "removed"},
                {"kind": "standalone_clone", "path": "/ws/lib", "safe_to_remove": false,
                 "notes": "uncommitted or untracked changes", "action": "skipped"},
                {"kind": "broken_symlink", "path": "/ws/.claude/skills/x", "safe_to_remove": false,
                 "notes": "changed since the scan", "action": "skipped"},
                {"kind": "orphan_agent_copy", "path": "/ws/.claude/agents/ext-a.md",
                 "safe_to_remove": true, "notes": "", "action": "error",
                 "error": "permission denied"},
            ]})
        );
    }
}
