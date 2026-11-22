//! Advancing a standalone's pin during a pull: holding a tag or commit pin, integrating a branch pin.

use std::path::Path;

use crate::config_lock::{LockEntry, RefKind, upsert_lock};
use crate::config_model::StandaloneRepository;
use crate::diagnostics::{self, Diagnostics, Record};
use crate::git::{self, GitFailure};
use crate::logging::Level;
use crate::pins::outcome::{PinOutcome, PinResult, rendered};
use crate::pins::resolve::resolve;
use crate::pins::{PinError, Ports};
use crate::termination::{RepoError, RepoMessage};
use crate::workspace_files::standalone_location;

/// The module a record of the advance names as its origin.
const ORIGIN: &str = "pins";

/// How a pull integrates a checkout onto its target when the checkout cannot simply be fast-forwarded.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PullMode {
    FfOnly,
    Merge,
    Rebase,
}

/// What the integration of one checkout onto a target ref did.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Integration {
    UpToDate,
    /// `commits` is `HEAD..target` before the integration.
    FastForwarded {
        commits: u32,
    },
    /// After a fast-forward was refused, in [`PullMode::Merge`].
    Merged {
        commits: u32,
    },
    /// After a fast-forward was refused, in [`PullMode::Rebase`].
    Rebased {
        commits: u32,
    },
    Diverged {
        ahead: u32,
        behind: u32,
    },
}

/// The pull's integration of one checkout onto a target ref, implemented by the pull.
pub trait IntegrateCheckout {
    fn integrate(
        &self,
        path: &Path,
        target: &str,
        mode: PullMode,
        autostash: bool,
    ) -> Result<Integration, GitFailure>;
}

/// What [`advance`] answers: a pin outcome of its own, or the pull's integration passed through.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Advanced {
    Pin(PinOutcome),
    Integrated(Integration),
}

/// Advances the pin of `repo`, a standalone of the workspace at `root`, during a pull.
///
/// A tag or a commit pin never moves: it is `held_pin` with the declared ref, in every mode, and nothing is
/// touched. A branch pin is guarded: a dirty checkout without `autostash` is `pin_error` and is not
/// integrated; with it the changes are stashed, the checkout is integrated onto `origin/<ref>` in `mode`
/// through `integration`, and the stash is popped. A pop that fails is a warning record naming the checkout and
/// changes no outcome; an error of the integration propagates without the pop. A fast-forward records the new
/// `HEAD` in the lock and is `re_pinned`, `pin_ref` its first 8 characters; any other integration is answered
/// as it is and the lock is left alone. A `repo` that declares no ref is a [`RepoMessage::NotPinned`] error.
pub fn advance(
    ports: &Ports<'_>,
    diagnostics: &mut Diagnostics<'_>,
    root: &Path,
    repo: &StandaloneRepository,
    mode: PullMode,
    autostash: bool,
    integration: &dyn IntegrateCheckout,
) -> Result<Advanced, PinError> {
    let name = repo.common.name.as_str();
    let Some(declared) = repo.reference.as_deref() else {
        return Err(PinError::Repo(Box::new(RepoError {
            message: RepoMessage::NotPinned {
                name: name.to_owned(),
            },
            program: None,
            subcommand: None,
            args: Vec::new(),
            cwd: None,
            exit: None,
            stderr: None,
        })));
    };
    let path = standalone_location(root, name, &repo.path);
    let programs = ports.programs;
    let tracer = ports.tracer;

    if resolve(ports, name, &path, declared)?.kind != RefKind::Branch {
        return Ok(Advanced::Pin(outcome(
            name,
            PinResult::HeldPin,
            declared.to_owned(),
        )));
    }

    let dirty = !git::clean(programs, tracer, name, &path)?;
    if dirty && !autostash {
        return Ok(Advanced::Pin(outcome(
            name,
            PinResult::PinError,
            format!(
                "branch pin '{name}' has uncommitted changes; commit/stash or pass --autostash"
            ),
        )));
    }
    if dirty {
        match git::stash_push(programs, tracer, name, &path) {
            Ok(()) => {}
            Err(GitFailure::Repo(error)) => {
                return Ok(Advanced::Pin(outcome(
                    name,
                    PinResult::PinError,
                    rendered(&error),
                )));
            }
            Err(internal) => return Err(internal.into()),
        }
    }
    let integrated =
        integration.integrate(&path, &format!("origin/{declared}"), mode, autostash)?;
    if dirty && git::stash_pop(programs, tracer, name, &path).is_err() {
        warn(
            diagnostics,
            &format!(
                "could not restore the stashed changes of {name} at {}",
                path.display()
            ),
        );
    }

    let Integration::FastForwarded { .. } = integrated else {
        return Ok(Advanced::Integrated(integrated));
    };
    let commit = git::head(programs, tracer, name, &path)?;
    upsert_lock(
        ports.writer,
        root,
        LockEntry {
            name: name.to_owned(),
            r#ref: declared.to_owned(),
            kind: RefKind::Branch,
            commit: commit.clone(),
        },
    )?;
    Ok(Advanced::Pin(outcome(
        name,
        PinResult::RePinned,
        commit.chars().take(8).collect(),
    )))
}

fn outcome(name: &str, result: PinResult, pin_ref: String) -> PinOutcome {
    PinOutcome {
        repo: name.to_owned(),
        result,
        pin_ref,
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
