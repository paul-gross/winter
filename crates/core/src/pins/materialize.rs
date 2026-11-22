//! Materializing a standalone's pin: checking out what its lock entry, or its resolved ref, records.

use std::path::Path;

use crate::config_lock::{LockEntry, RefKind, read_lock, upsert_lock};
use crate::config_model::StandaloneRepository;
use crate::git::{self, Sha};
use crate::pins::resolve::resolve;
use crate::pins::{PinError, Ports};
use crate::termination::{RepoError, RepoMessage};
use crate::workspace_files::standalone_location;

/// Checks out the pin of `repo`, a standalone of the workspace at `root`, and answers what it pinned.
///
/// `None` when `repo` declares no ref. With a lock entry for the declared ref, the checkout lands on what the
/// entry records and the lock is left alone: a branch on `origin/<ref>` as last fetched, a tag or a commit on
/// the recorded commit, and the answer is the entry's own kind and commit. Otherwise the checkout must be clean
/// or the pin is refused as [`RepoMessage::DirtyRePin`]; the ref is resolved, checked out, and only then
/// recorded, so a refused checkout writes no entry. The lock is read first, so a malformed one leaves `HEAD`
/// alone ([`PinError::Config`]).
pub fn materialize(
    ports: &Ports<'_>,
    root: &Path,
    repo: &StandaloneRepository,
) -> Result<Option<(RefKind, Sha)>, PinError> {
    let Some(declared) = repo.reference.as_deref() else {
        return Ok(None);
    };
    let name = repo.common.name.as_str();
    let path = standalone_location(root, name, &repo.path);
    let programs = ports.programs;
    let tracer = ports.tracer;

    let lock = read_lock(ports.files, root)?;
    if let Some(entry) = lock.get(name).filter(|entry| entry.r#ref == declared) {
        match entry.kind {
            RefKind::Branch => git::checkout_branch(programs, tracer, name, &path, declared)?,
            RefKind::Tag | RefKind::Commit => {
                git::checkout_detached(programs, tracer, name, &path, &entry.commit)?;
            }
        }
        return Ok(Some((entry.kind, entry.commit.clone())));
    }

    if !git::clean(programs, tracer, name, &path)? {
        return Err(PinError::Repo(Box::new(RepoError {
            message: RepoMessage::DirtyRePin {
                name: name.to_owned(),
            },
            program: None,
            subcommand: None,
            args: Vec::new(),
            cwd: None,
            exit: None,
            stderr: None,
        })));
    }
    let resolved = resolve(ports, name, &path, declared)?;
    match resolved.kind {
        RefKind::Branch => git::checkout_branch(programs, tracer, name, &path, declared)?,
        RefKind::Tag | RefKind::Commit => {
            git::checkout_detached(programs, tracer, name, &path, &resolved.commit)?;
        }
    }
    upsert_lock(
        ports.writer,
        root,
        LockEntry {
            name: name.to_owned(),
            r#ref: declared.to_owned(),
            kind: resolved.kind,
            commit: resolved.commit.clone(),
        },
    )?;
    Ok(Some((resolved.kind, resolved.commit)))
}
