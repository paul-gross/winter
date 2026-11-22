//! Freezing the selected standalones: each one's current commit written as its `ref` in the configuration
//! file that declares it, and recorded in the lock. Nothing is fetched and no checkout changes.

use std::path::Path;

use crate::config_edit::{adds_one_ref, set_standalone_ref};
use crate::config_load;
use crate::config_lock::{LockEntry, RefKind, upsert_lock};
use crate::config_model::StandaloneRepository;
use crate::diagnostics::{self, Diagnostics};
use crate::drift;
use crate::files::WriteFiles;
use crate::git::{self, GitFailure};
use crate::pins::outcome::{PinOutcome, PinResult};
use crate::pins::repin::{EventSink, PinEvent, RunEnd};
use crate::pins::{PinError, Ports, selected};
use crate::termination::{ConfigError, RepoError, RepoMessage, repr};
use crate::workspace_files::{CONFIG_FILE, LOCAL_CONFIG_FILE, standalone_location};

/// How many characters of a commit a frozen outcome carries.
const SHORT_COMMIT: usize = 8;

/// Freezes the standalones of the workspace found from `start` that `patterns` select, every standalone when
/// there is no pattern, pinned or not.
///
/// The drift notice is written first. A literal pattern that names no standalone is a [`PinError::Repo`],
/// before any event. A standalone that declares a ref is reported as already pinned. Otherwise its `HEAD` is
/// written as its `ref` into the first of `config.toml` and `config.local.toml` that declares it, and
/// recorded in the lock. A checkout that is missing, that is no repository, or that is dirty unless `force`,
/// and a standalone no file declares, is refused; a refusal does not stop the next standalone. A lock that
/// cannot be written, a configuration file that cannot be written, and a git that cannot be started end the
/// run as that error, with no completion event; a ref already written stays written.
pub fn freeze(
    ports: Ports<'_>,
    diagnostics: &mut Diagnostics<'_>,
    start: &Path,
    patterns: &[String],
    force: bool,
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

    let chosen = select(&config.standalone_repositories, patterns).map_err(|message| {
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

    if let Err(termination) = sink(&PinEvent::Started, diagnostics) {
        return Ok(RunEnd::Stopped(termination));
    }
    let mut success = true;
    for repo in chosen {
        let outcome = one(&ports, &root, repo, force)?;
        success &= outcome.result != PinResult::Refused;
        if let Err(termination) = sink(&PinEvent::Synced(outcome), diagnostics) {
            return Ok(RunEnd::Stopped(termination));
        }
    }
    match sink(&PinEvent::Completed { success }, diagnostics) {
        Ok(()) => Ok(RunEnd::Completed { success }),
        Err(termination) => Ok(RunEnd::Stopped(termination)),
    }
}

/// The standalones of `standalones` that `patterns` select, in their order. A literal pattern that names no
/// standalone is [`RepoMessage::NoStandaloneNamed`], checked before anything is selected.
fn select<'r>(
    standalones: &'r [StandaloneRepository],
    patterns: &[String],
) -> Result<Vec<&'r StandaloneRepository>, RepoMessage> {
    for literal in patterns
        .iter()
        .filter(|pattern| !crate::pattern_match::is_glob(pattern))
    {
        if !standalones.iter().any(|repo| &repo.common.name == literal) {
            return Err(RepoMessage::NoStandaloneNamed {
                name: literal.clone(),
            });
        }
    }
    Ok(standalones
        .iter()
        .filter(|repo| selected(&repo.common.name, patterns))
        .collect())
}

/// Freezes one standalone. An error is one that ends the whole run.
fn one(
    ports: &Ports<'_>,
    root: &Path,
    repo: &StandaloneRepository,
    force: bool,
) -> Result<PinOutcome, PinError> {
    let name = repo.common.name.as_str();
    let outcome = |result, pin_ref: String| PinOutcome {
        repo: name.to_owned(),
        result,
        pin_ref,
    };
    let refused = |reason: String| outcome(PinResult::Refused, reason);

    if let Some(declared) = &repo.reference {
        return Ok(outcome(PinResult::AlreadyPinned, declared.clone()));
    }
    let path = standalone_location(root, name, &repo.path);
    if !ports.files.exists(&path) {
        return Ok(refused(format!(
            "{} is not cloned (run `winter ws init`)",
            repr(name)
        )));
    }
    let commit = match git::head(ports.programs, ports.tracer, name, &path) {
        Ok(commit) => commit,
        Err(GitFailure::Repo(error)) => return Ok(refused(error.message.to_string())),
        Err(failure) => return Err(failure.into()),
    };
    if !force && !git::clean(ports.programs, ports.tracer, name, &path)? {
        return Ok(refused(format!(
            "{} has uncommitted changes, so its commit does not describe it; commit/stash or pass --force",
            repr(name)
        )));
    }

    let mut guarded = None;
    for file in [CONFIG_FILE, LOCAL_CONFIG_FILE] {
        match write_ref(ports.writer, &root.join(file), name, &commit)? {
            Written::Done => {
                guarded = Some(Ok(()));
                break;
            }
            Written::Unguarded => {
                guarded = Some(Err(file));
                break;
            }
            Written::Absent => {}
        }
    }
    match guarded {
        None => {
            return Ok(refused(format!(
                "{} is not declared in config.toml or config.local.toml",
                repr(name)
            )));
        }
        Some(Err(file)) => {
            return Ok(refused(format!(
                "{} cannot take a ref in {file} without changing other entries",
                repr(name)
            )));
        }
        Some(Ok(())) => {}
    }

    let pin_ref = commit.chars().take(SHORT_COMMIT).collect();
    upsert_lock(
        ports.writer,
        root,
        LockEntry {
            name: name.to_owned(),
            r#ref: commit.clone(),
            kind: RefKind::Commit,
            commit,
        },
    )?;
    Ok(outcome(PinResult::Pinned, pin_ref))
}

/// What an attempt to write a ref into one file came to.
enum Written {
    /// The file holds the ref now.
    Done,
    /// The file has no block of the name, or does not exist; nothing was written.
    Absent,
    /// The file has the block, and the edit failed the guard; nothing was written.
    Unguarded,
}

/// Writes `ref = "<commit>"` into the first block of the file at `file` named `name`.
fn write_ref(
    writer: &dyn WriteFiles,
    file: &Path,
    name: &str,
    commit: &str,
) -> Result<Written, ConfigError> {
    let mut unguarded = false;
    let replaced = writer
        .update(file, &mut |current| {
            let text = std::str::from_utf8(current?).ok()?;
            let set = set_standalone_ref(text, name, commit)?;
            if !adds_one_ref(text, &set.text, set.index, commit) {
                unguarded = true;
                return None;
            }
            Some(set.text.into_bytes())
        })
        .map_err(|cause| ConfigError::FileWrite {
            path: file.to_path_buf(),
            cause,
        })?;
    Ok(match (replaced, unguarded) {
        (true, _) => Written::Done,
        (false, true) => Written::Unguarded,
        (false, false) => Written::Absent,
    })
}
