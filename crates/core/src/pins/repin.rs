//! Re-pinning the selected standalones: each declared ref resolved again, checked out, and recorded in the lock.

use std::path::Path;

use crate::config_load;
use crate::config_lock::{LockEntry, RefKind, read_lock, upsert_lock};
use crate::config_model::StandaloneRepository;
use crate::diagnostics::{self, Diagnostics, Record};
use crate::drift;
use crate::git::{self, GitFailure};
use crate::logging::Level;
use crate::pattern_match;
use crate::pins::outcome::{PinOutcome, PinResult, rendered};
use crate::pins::resolve::resolve;
use crate::pins::{PinError, Ports};
use crate::process::ProcessEnv;
use crate::termination::{RepoError, RepoMessage, Termination, repr};
use crate::workspace_files::standalone_location;

/// The module a record of the run names as its origin.
const ORIGIN: &str = "pins";

/// How many characters of a commit a re-pinned outcome carries.
const SHORT_COMMIT: usize = 8;

/// What a pin run reports as it is reached: its start, the outcome of one standalone, and its end.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PinEvent {
    Started,
    Synced(PinOutcome),
    Completed { success: bool },
}

/// The command's sink, called once per event as it is reached, with the diagnostics of the entry so that a
/// row that belongs on stderr can be written. An `Err` stops the run at once, after any pending stash pop.
pub type EventSink<'s> = dyn FnMut(&PinEvent, &mut Diagnostics<'_>) -> Result<(), Termination> + 's;

/// How a run that raised no [`PinError`] ended.
#[derive(Debug)]
pub enum RunEnd {
    /// Every selected standalone was reached. The run succeeded unless an outcome is a pin error.
    Completed { success: bool },
    /// The sink refused an event, with the ending it gave.
    Stopped(Termination),
}

/// Re-pins the standalones of the workspace found from `start` that `patterns` select, every pinned one when
/// there is no pattern.
///
/// The drift notice of the workspace is written first. A literal pattern that names no pinned standalone is a
/// [`PinError::Repo`], before any event. A selected checkout that is missing on disk is a notice and is left
/// out. Each remaining standalone is fetched, resolved, and either reported up to date or checked out and
/// recorded in the lock; an outcome of one does not stop the next. A dirty checkout is a pin error unless
/// `autostash`, under which its changes are stashed and restored on every way out. A `HEAD` that cannot be
/// read, a lock that cannot be read or written, and a git that cannot be started end the run as that error,
/// after the restore and with no completion event.
pub fn repin(
    ports: Ports<'_>,
    env: &ProcessEnv,
    diagnostics: &mut Diagnostics<'_>,
    start: &Path,
    patterns: &[String],
    autostash: bool,
    sink: &mut EventSink<'_>,
) -> Result<RunEnd, PinError> {
    let workspace = config_load::load(ports.files, start)?;
    let root = workspace.root;
    let config = workspace.config;

    let drift = drift::detect(ports.files, &root, &config.project_repositories)
        .map_err(|error| PinError::Internal(Box::new(error)))?;
    if let Some(line) = drift::notice_line(&drift) {
        diagnostics::notice(diagnostics.err, &line);
    }

    let selected = select(&config.standalone_repositories, patterns).map_err(|message| {
        PinError::Repo(Box::new(RepoError {
            message,
            program: None,
            subcommand: None,
            args: Vec::new(),
            cwd: None,
            exit: None,
            stderr: None,
        }))
    })?;
    let mut present = Vec::new();
    for (repo, declared) in selected {
        let name = repo.common.name.as_str();
        let path = standalone_location(&root, name, &repo.path);
        if ports.files.exists(&path) {
            present.push((name, declared, path));
        } else {
            diagnostics::notice(
                diagnostics.err,
                &format!("warning: standalone/{name} — missing on disk (run `winter ws init`)"),
            );
        }
    }

    if let Err(termination) = sink(&PinEvent::Started, diagnostics) {
        return Ok(RunEnd::Stopped(termination));
    }
    let mut success = true;
    for (name, declared, path) in present {
        let target = Target {
            root: &root,
            name,
            declared,
            path: &path,
        };
        match one(&ports, env, diagnostics, &target, autostash, sink)? {
            Ok(result) => success &= result != PinResult::PinError,
            Err(termination) => return Ok(RunEnd::Stopped(termination)),
        }
    }
    match sink(&PinEvent::Completed { success }, diagnostics) {
        Ok(()) => Ok(RunEnd::Completed { success }),
        Err(termination) => Ok(RunEnd::Stopped(termination)),
    }
}

/// The pinned standalones of `standalones` that `patterns` select, each with its declared ref, in the order
/// of `standalones`.
///
/// A standalone is pinned when it declares a ref, an empty one included. No pattern selects every pinned
/// standalone. A pattern that is a glob selects the pinned standalones it matches, and none is no error. Any
/// other pattern is a literal name, and each is checked before anything is selected: one that names a
/// standalone without a ref is [`RepoMessage::NotPinned`], and one that names no standalone is
/// [`RepoMessage::NoPinnedNamed`].
fn select<'r>(
    standalones: &'r [StandaloneRepository],
    patterns: &[String],
) -> Result<Vec<(&'r StandaloneRepository, &'r str)>, RepoMessage> {
    let pinned: Vec<(&StandaloneRepository, &str)> = standalones
        .iter()
        .filter_map(|repo| repo.reference.as_deref().map(|declared| (repo, declared)))
        .collect();
    if patterns.is_empty() {
        return Ok(pinned);
    }
    for literal in patterns
        .iter()
        .filter(|pattern| !pattern_match::is_glob(pattern))
    {
        if pinned.iter().any(|(repo, _)| &repo.common.name == literal) {
            continue;
        }
        let name = literal.clone();
        return Err(
            if standalones.iter().any(|repo| &repo.common.name == literal) {
                RepoMessage::NotPinned { name }
            } else {
                RepoMessage::NoPinnedNamed { name }
            },
        );
    }
    Ok(pinned
        .into_iter()
        .filter(|(repo, _)| crate::pins::selected(&repo.common.name, patterns))
        .collect())
}

/// One selected standalone whose checkout exists.
struct Target<'t> {
    root: &'t Path,
    name: &'t str,
    declared: &'t str,
    path: &'t Path,
}

/// Re-pins one standalone and hands its outcome to `sink`.
///
/// The inner answer is the result that was reported, or the ending the sink gave. The outer error is one that
/// ends the whole run. A stash this call pushed is popped before it returns, whatever it returns, and after
/// the outcome reached the sink.
fn one(
    ports: &Ports<'_>,
    env: &ProcessEnv,
    diagnostics: &mut Diagnostics<'_>,
    target: &Target<'_>,
    autostash: bool,
    sink: &mut EventSink<'_>,
) -> Result<Result<PinResult, Termination>, PinError> {
    let Target { name, path, .. } = *target;
    let programs = ports.programs;
    let tracer = ports.tracer;

    match git::fetch(programs, tracer, env, diagnostics, name, path) {
        Ok(()) => {}
        Err(GitFailure::Repo(error)) => warn(
            diagnostics,
            &format!("Fetch failed for standalone {name}: {}", rendered(&error)),
        ),
        Err(failure) => return Err(failure.into()),
    }

    let dirty = !git::clean(programs, tracer, name, path)?;
    if dirty && !autostash {
        let reason = format!(
            "refusing to re-pin {}: uncommitted changes; commit/stash or pass --autostash",
            repr(name)
        );
        return Ok(report(sink, diagnostics, pin_error(name, reason)));
    }
    if dirty {
        match git::stash_push(programs, tracer, name, path) {
            Ok(()) => {}
            Err(GitFailure::Repo(error)) => {
                return Ok(report(sink, diagnostics, pin_error(name, rendered(&error))));
            }
            Err(failure) => return Err(failure.into()),
        }
    }

    let settled =
        settle(ports, diagnostics, target).map(|outcome| report(sink, diagnostics, outcome));
    if dirty && let Err(failure) = git::stash_pop(programs, tracer, name, path) {
        match failure {
            GitFailure::Repo(error) => warn(
                diagnostics,
                &format!(
                    "stash pop failed for {name}; leftover stash at '{}' needs manual resolution: {}",
                    path.display(),
                    rendered(&error)
                ),
            ),
            // An error the run already has comes first.
            GitFailure::Internal(error) => {
                return Err(settled.err().unwrap_or(PinError::Internal(error)));
            }
        }
    }
    settled
}

/// What a clean checkout of `target` comes to: up to date, re-pinned, or a pin error of its own. An error is
/// one that ends the whole run.
fn settle(
    ports: &Ports<'_>,
    diagnostics: &mut Diagnostics<'_>,
    target: &Target<'_>,
) -> Result<PinOutcome, PinError> {
    let Target {
        root,
        name,
        declared,
        path,
    } = *target;
    let programs = ports.programs;
    let tracer = ports.tracer;

    let resolved = match resolve(ports, name, path, declared) {
        Ok(resolved) => resolved,
        Err(GitFailure::Repo(error)) => {
            let reason = rendered(&error);
            warn(
                diagnostics,
                &format!("resolve_ref failed for standalone {name}: {reason}"),
            );
            return Ok(pin_error(name, reason));
        }
        Err(failure) => return Err(failure.into()),
    };

    let head = git::head(programs, tracer, name, path)?;
    let lock = read_lock(ports.files, root)?;
    if resolved.commit == head
        && lock
            .get(name)
            .is_some_and(|entry| entry.commit == resolved.commit)
    {
        return Ok(PinOutcome {
            repo: name.to_owned(),
            result: PinResult::UpToDate,
            pin_ref: String::new(),
        });
    }

    let checked_out = match resolved.kind {
        RefKind::Branch => git::checkout_branch(programs, tracer, name, path, declared),
        RefKind::Tag | RefKind::Commit => {
            git::checkout_detached(programs, tracer, name, path, &resolved.commit)
        }
    };
    match checked_out {
        Ok(()) => {}
        Err(GitFailure::Repo(error)) => return Ok(pin_error(name, rendered(&error))),
        Err(failure) => return Err(failure.into()),
    }

    let pin_ref = resolved.commit.chars().take(SHORT_COMMIT).collect();
    upsert_lock(
        ports.writer,
        root,
        LockEntry {
            name: name.to_owned(),
            r#ref: declared.to_owned(),
            kind: resolved.kind,
            commit: resolved.commit,
        },
    )?;
    Ok(PinOutcome {
        repo: name.to_owned(),
        result: PinResult::RePinned,
        pin_ref,
    })
}

/// Hands `outcome` to `sink`, and answers its result or the ending the sink gave.
fn report(
    sink: &mut EventSink<'_>,
    diagnostics: &mut Diagnostics<'_>,
    outcome: PinOutcome,
) -> Result<PinResult, Termination> {
    let result = outcome.result;
    sink(&PinEvent::Synced(outcome), diagnostics).map(|()| result)
}

fn pin_error(name: &str, reason: String) -> PinOutcome {
    PinOutcome {
        repo: name.to_owned(),
        result: PinResult::PinError,
        pin_ref: reason,
    }
}

fn warn(diagnostics: &mut Diagnostics<'_>, message: &str) {
    diagnostics::record(
        diagnostics.err,
        diagnostics.log,
        &Record {
            level: Level::Warning,
            origin: ORIGIN,
            message,
            only_when_switched: false,
        },
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config_model::RepositoryCommon;

    fn standalone(name: &str, reference: Option<&str>) -> StandaloneRepository {
        StandaloneRepository {
            common: RepositoryCommon {
                name: name.to_owned(),
                url: None,
                main_branch: "main".to_owned(),
                git_excludes: Vec::new(),
                cmd: Vec::new(),
                extension: false,
                load: None,
                entry: None,
            },
            reference: reference.map(str::to_owned),
            path: String::new(),
            prefix: None,
            config_dir: String::new(),
        }
    }

    /// `lib-b`, `free` without a ref, `lib-a`, `Lib-a`, and `blank` with an empty ref, in that order.
    fn declared() -> Vec<StandaloneRepository> {
        vec![
            standalone("lib-b", Some("v2")),
            standalone("free", None),
            standalone("lib-a", Some("main")),
            standalone("Lib-a", Some("v1")),
            standalone("blank", Some("")),
        ]
    }

    fn selected(patterns: &[&str]) -> Result<Vec<(String, String)>, RepoMessage> {
        let standalones = declared();
        let patterns: Vec<String> = patterns.iter().map(|&pattern| pattern.to_owned()).collect();
        select(&standalones, &patterns).map(|selected| {
            selected
                .into_iter()
                .map(|(repo, declared)| (repo.common.name.clone(), declared.to_owned()))
                .collect()
        })
    }

    fn names(patterns: &[&str]) -> Vec<String> {
        selected(patterns)
            .expect("a selection")
            .into_iter()
            .map(|(name, _)| name)
            .collect()
    }

    #[test]
    fn no_pattern_selects_every_pinned_standalone_in_declared_order_with_its_ref() {
        assert_eq!(
            selected(&[]).expect("a selection"),
            [
                ("lib-b".to_owned(), "v2".to_owned()),
                ("lib-a".to_owned(), "main".to_owned()),
                ("Lib-a".to_owned(), "v1".to_owned()),
                ("blank".to_owned(), String::new()),
            ]
        );
    }

    #[test]
    fn literals_select_exactly_the_named_standalones_in_declared_order() {
        assert_eq!(names(&["lib-a", "lib-b"]), ["lib-b", "lib-a"]);
        assert_eq!(names(&["Lib-a"]), ["Lib-a"]);
        assert_eq!(names(&["lib-a", "lib-a"]), ["lib-a"]);
    }

    #[test]
    fn a_glob_selects_the_pinned_standalones_it_matches_by_case() {
        assert_eq!(names(&["lib-[ab]"]), ["lib-b", "lib-a"]);
        assert_eq!(names(&["?ib-a"]), ["lib-a", "Lib-a"]);
        assert_eq!(names(&["*"]), ["lib-b", "lib-a", "Lib-a", "blank"]);
    }

    #[test]
    fn a_glob_that_matches_nothing_selects_nothing_and_never_an_unpinned_standalone() {
        assert_eq!(names(&["nope-*"]), Vec::<String>::new());
        assert_eq!(names(&["fre*"]), Vec::<String>::new());
    }

    #[test]
    fn a_glob_and_a_literal_select_together() {
        assert_eq!(names(&["L*", "blank"]), ["Lib-a", "blank"]);
    }

    #[test]
    fn a_literal_that_names_an_unpinned_standalone_is_refused_as_not_pinned() {
        assert_eq!(
            selected(&["lib-a", "free"]),
            Err(RepoMessage::NotPinned {
                name: "free".to_owned()
            })
        );
    }

    #[test]
    fn a_literal_that_names_no_standalone_is_refused_whatever_else_is_given() {
        assert_eq!(
            selected(&["lib-*", "nope", "free"]),
            Err(RepoMessage::NoPinnedNamed {
                name: "nope".to_owned()
            })
        );
    }
}
