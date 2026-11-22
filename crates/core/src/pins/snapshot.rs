//! The pin snapshot: what the lock records for each pinned standalone against its declaration and its checkout.

use std::path::Path;

use crate::config_lock::{LockEntry, RefKind, read_lock};
use crate::config_model::StandaloneRepository;
use crate::diagnostics::{self, Diagnostics, Record};
use crate::git::{Sha, head};
use crate::logging::Level;
use crate::pins::Ports;
use crate::workspace_files::location;

/// The module a record of the snapshot names as its origin.
const ORIGIN: &str = "pins";

/// One standalone that declares a `ref`, as the lock and its checkout stand.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PinSnapshot {
    pub name: String,
    /// The declared ref.
    pub r#ref: String,
    /// The kind of the lock entry; `None` without one.
    pub kind: Option<RefKind>,
    pub locked_commit: Option<Sha>,
    /// An entry exists and its ref differs from the declared ref.
    pub config_ref_drift: bool,
    /// An entry exists, `HEAD` was read, and `HEAD` differs from the commit of the entry.
    pub head_drift: bool,
    /// `None` when `HEAD` cannot be read.
    pub head_commit: Option<Sha>,
}

/// One snapshot per standalone of `standalones` that declares a `ref`, in the order given.
///
/// The lock is read once, and not at all when no standalone declares a `ref`. A lock that cannot be read is a
/// warning record and is read as empty: this is a read-only view. A `HEAD` that cannot be read is a debug
/// record and leaves `head_commit` empty and `head_drift` false.
pub fn snapshot(
    ports: &Ports<'_>,
    diagnostics: &mut Diagnostics<'_>,
    root: &Path,
    standalones: &[StandaloneRepository],
) -> Vec<PinSnapshot> {
    let pinned: Vec<_> = standalones
        .iter()
        .filter_map(|standalone| standalone.reference.as_deref().map(|r| (standalone, r)))
        .collect();
    if pinned.is_empty() {
        return Vec::new();
    }
    let lock = read_lock(ports.files, root).unwrap_or_else(|error| {
        report(diagnostics, Level::Warning, &error.to_string());
        Default::default()
    });
    pinned
        .into_iter()
        .map(|(standalone, declared)| {
            let name = &standalone.common.name;
            let relative = if standalone.path.is_empty() {
                name
            } else {
                &standalone.path
            };
            let path = location(root, Path::new(relative));
            let head_commit = match head(ports.programs, ports.tracer, name, &path) {
                Ok(sha) => Some(sha),
                Err(failure) => {
                    report(
                        diagnostics,
                        Level::Debug,
                        &format!("{name}: HEAD cannot be read: {failure:?}"),
                    );
                    None
                }
            };
            let entry: Option<&LockEntry> = lock.get(name);
            PinSnapshot {
                name: name.clone(),
                r#ref: declared.to_owned(),
                kind: entry.map(|entry| entry.kind),
                locked_commit: entry.map(|entry| entry.commit.clone()),
                config_ref_drift: entry.is_some_and(|entry| entry.r#ref != declared),
                head_drift: entry
                    .zip(head_commit.as_ref())
                    .is_some_and(|(entry, head)| &entry.commit != head),
                head_commit,
            }
        })
        .collect()
}

fn report(diagnostics: &mut Diagnostics<'_>, level: Level, message: &str) {
    diagnostics::record(
        diagnostics.err,
        diagnostics.log,
        &Record {
            level,
            origin: ORIGIN,
            message,
            only_when_switched: false,
        },
    );
}
