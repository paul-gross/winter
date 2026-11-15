//! The record of one scenario, made with a given executable: the fixture built twice, in two trees with
//! different roots, the steps run in each, the two runs compared, and `record/` written from the first
//! when they agree.
//!
//! Nothing is run for a description that may not be: [`record`] refuses a scenario that does not parse,
//! one with a `[[stub]]` while no call is captured, and one whose step asks for what the runner refuses
//! without a tree, each before any tree exists and before the executable is started. A step whose working
//! directory or argument the runner refuses on the built tree ends the record likewise, as a malformed
//! scenario.
//!
//! The two runs are compared by [`compare::fixture`], for the state before the first step, and by
//! [`compare::differences`], under the scenario's own rules, the first run standing as the record. The
//! first difference refuses the record as unstable. That is the comparison a replay makes, so stdout as
//! text and stderr are not compared, and a JSON output that is no stream of JSON values in either run is
//! refused as the difference the comparison reports for it.
//!
//! What is stored has the root of its tree replaced already, by the runner and the snapshot. After that,
//! every redaction of the options replaces its path, as bytes, by its token: in both streams, in the
//! content of every stored file, in the target of every link, and in every line of a repository's
//! section. The canonical form of a JSON output is made again from the redacted stdout, and the hash of a
//! file whose content a redaction changed is the hash of what is stored, on every line that lists that
//! content. A path of the tree is not redacted, and neither is the content of a file that is as the
//! fixture made it, of which a record stores the hash alone.
//!
//! A record is written whole or not at all: into a directory beside `record/`, read back as a stored
//! snapshot is read, and only then moved into place, over the record that was there when re-recording was
//! asked for. `expected/` is neither read nor written.

use std::collections::BTreeMap;
use std::fmt;
use std::fs::Permissions;
use std::io::{self, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use super::compare::{
    self, Difference, EXIT_CODE_FILE, STDERR_FILE, STDOUT_FILE, STDOUT_JSON_FILE, StepRecord,
};
use super::fixture::{self, FixtureError, ROOT_TOKEN};
use super::model::{self, Facility, InvalidKey, Malformed, Meta, Scenario};
use super::normalize;
use super::runner::{self, RunFailure};
use super::snapshot::{self, AFTER, BEFORE, EntryKind, Snapshot, SnapshotError};
use crate::env::PinnedEnv;
use crate::git::{Git, GitError};
use crate::tree::TempTree;

/// The directory of a record that holds its steps.
pub(super) const STEPS_DIRECTORY: &str = "steps";

/// What `git --version` writes before the version.
const GIT_VERSION_PREFIX: &str = "git version ";

/// The key of a description that declares a stubbed program.
pub(super) const STUB_KEY: &str = "stub";

/// What a record is made with.
#[derive(Clone, Debug)]
pub struct RecordOptions {
    /// The executable every step runs: an absolute path.
    pub executable: PathBuf,
    /// Additions to the environment of every step, after the step's own. They are not part of the digest
    /// of the environment.
    pub extra_env: Vec<(String, String)>,
    /// Each path, as bytes, becomes its token in everything that is stored, after the root is replaced.
    pub redactions: Vec<(PathBuf, String)>,
    /// The commit of the reference the record is made at, as `meta.toml` states it.
    pub reference_commit: String,
    /// The first line the reference prints for its version, as `meta.toml` states it.
    pub reference_runtime: String,
    /// Replace a record that is there.
    pub rerecord: bool,
    /// Leave both trees on disk and name them on stderr.
    pub keep: bool,
}

/// Why no record was written.
#[derive(Debug)]
pub enum RecordError {
    /// The scenario may not be recorded as it is described: it does not parse, names something that does
    /// not stay inside its tree, has a `[[stub]]` while no call is captured, or has a step that asks for
    /// what the runner refuses.
    Malformed(Malformed),
    /// The scenario needs these facilities, which the runner does not have. Nothing was started.
    Deferred(Vec<Facility>),
    /// The scenario has a record, and re-recording was not asked for.
    Exists,
    /// The fixture was not built.
    Fixture(FixtureError),
    /// A step gave no observation, or the state of a tree could not be taken.
    Run(RunFailure),
    /// The two runs differ: the first difference, under the rules of the comparison.
    Unstable(Difference),
    /// What the record would say it was made with does not read as a `meta.toml`.
    Meta(InvalidKey),
    /// Git did not tell its version.
    Git(GitError),
    /// The content a step stored for `path` holds the root of its tree, which is replaced only in content
    /// that is valid UTF-8.
    HoldsRoot { step: u32, path: String },
    /// The record does not write or read as a stored snapshot.
    Snapshot(SnapshotError),
    /// A file of the scenario directory could not be read, or one of the record not written.
    Io { path: PathBuf, error: io::Error },
}

impl fmt::Display for RecordError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Malformed(malformed) => malformed.fmt(f),
            Self::Deferred(facilities) => {
                let names: Vec<&str> = facilities.iter().map(|facility| facility.name()).collect();
                write!(f, "the scenario needs {}", names.join(", "))
            }
            Self::Exists => f.write_str("the scenario has a record"),
            Self::Fixture(error) => error.fmt(f),
            Self::Run(failure) => failure.fmt(f),
            Self::Unstable(difference) => {
                let step = difference
                    .step
                    .map_or(String::new(), |step| format!(" of step {step}"));
                write!(
                    f,
                    "the record is not stable between two roots: {:?}{step} at {}: {} against {}",
                    difference.dimension,
                    difference.subject,
                    difference.recorded,
                    difference.observed
                )
            }
            Self::Meta(invalid) => write!(f, "the record has no meta.toml: {invalid}"),
            Self::Git(error) => write!(f, "git did not tell its version: {error}"),
            Self::HoldsRoot { step, path } => write!(
                f,
                "the content step {step} stored for {path} holds the root of its tree"
            ),
            Self::Snapshot(error) => write!(f, "the record is no stored snapshot: {error}"),
            Self::Io { path, error } => write!(f, "{}: {error}", path.display()),
        }
    }
}

impl std::error::Error for RecordError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Malformed(malformed) => Some(malformed),
            Self::Fixture(error) => Some(error),
            Self::Run(failure) => Some(failure),
            Self::Meta(invalid) => Some(invalid),
            Self::Git(error) => Some(error),
            Self::Snapshot(error) => Some(error),
            Self::Io { error, .. } => Some(error),
            Self::Deferred(_) | Self::Exists | Self::Unstable(_) | Self::HoldsRoot { .. } => None,
        }
    }
}

/// Records the scenario of the directory `scenario_dir` with the executable of `options`, and writes
/// `record/` with its `meta.toml` when two runs under two roots agree. The module says what is refused,
/// compared, and stored. An error leaves the scenario directory as it was, a record that was there included.
pub fn record(scenario_dir: &Path, options: &RecordOptions) -> Result<(), RecordError> {
    let scenario = Scenario::load(scenario_dir).map_err(RecordError::Malformed)?;
    let malformed = |key: &str, reason: String| {
        RecordError::Malformed(Malformed {
            scenario: scenario.id.clone(),
            key: key.to_owned(),
            reason,
        })
    };
    if !runner::CALL_CAPTURE && !scenario.stubs.is_empty() {
        let reason = "the calls of a stubbed program are not captured".to_owned();
        return Err(malformed(STUB_KEY, reason));
    }
    let not_run = |failure: RunFailure| match failure {
        RunFailure::Unconfined { step, cwd } => malformed(
            runner::CWD_KEY,
            format!(
                "step {step}: {} is no directory inside the tree",
                cwd.display()
            ),
        ),
        RunFailure::Refused { step, key, reason } => {
            malformed(key, format!("step {step}: {reason}"))
        }
        failure => RecordError::Run(failure),
    };
    if let Some(refused) = runner::refused(&scenario, &options.extra_env) {
        return Err(not_run(refused));
    }
    let missing: Vec<Facility> = scenario
        .needs
        .iter()
        .copied()
        .filter(|facility| !runner::SUPPORTED.contains(facility))
        .collect();
    if !missing.is_empty() {
        return Err(RecordError::Deferred(missing));
    }
    let place = scenario_dir.join(model::RECORD);
    if place.symlink_metadata().is_ok() && !options.rerecord {
        return Err(RecordError::Exists);
    }
    let unread = |path: &Path| {
        let path = path.to_path_buf();
        move |error| RecordError::Io { path, error }
    };
    let mut meta = Meta {
        reference_commit: options.reference_commit.clone(),
        git_version: String::new(),
        reference_runtime: options.reference_runtime.clone(),
        environment: PinnedEnv::for_root(&String::from_utf8_lossy(ROOT_TOKEN)).digest(),
        description: scenario
            .description_digest()
            .map_err(unread(scenario_dir))?,
    };
    Meta::parse(&meta.to_toml()).map_err(RecordError::Meta)?;

    let run = || -> Result<(Held, Vec<StepRecord>), RecordError> {
        let tree = fixture::build(&scenario).map_err(|error| match error {
            FixtureError::Malformed(malformed) => RecordError::Malformed(malformed),
            error => RecordError::Fixture(error),
        })?;
        let tree = Held {
            tree: Some(tree),
            keep: options.keep,
        };
        let steps = runner::run_steps(
            &scenario,
            tree.get(),
            &options.executable,
            &options.extra_env,
        )
        .map_err(not_run)?;
        Ok((tree, steps))
    };
    let (first_tree, first) = run()?;
    let (_second_tree, second) = run()?;
    let tree = first_tree.get();
    meta.environment = tree.env().digest();
    meta.git_version = Git::new(tree)
        .and_then(|git| git.run(tree.root(), &["--version"]))
        .map_err(RecordError::Git)?
        .trim()
        .trim_start_matches(GIT_VERSION_PREFIX)
        .to_owned();

    let first = redacted(first, &scenario, &options.redactions);
    let second = redacted(second, &scenario, &options.redactions);
    let before = |steps: &[StepRecord]| steps.first().map(|step| step.before.clone());
    let fixture = match (before(&first), before(&second)) {
        (Some(first), Some(second)) => compare::fixture(&first, &second),
        _ => None,
    };
    let differences = || {
        compare::differences(&scenario, &first, &second)
            .into_iter()
            .next()
    };
    if let Some(difference) = fixture.or_else(differences) {
        return Err(RecordError::Unstable(difference));
    }
    let root = tree.root().as_os_str().as_bytes();
    for (index, step) in first.iter().enumerate() {
        let holds = |content: &Vec<u8>| content.windows(root.len()).any(|window| window == root);
        if let Some((path, _)) = step.after.files.iter().find(|(_, content)| holds(content)) {
            return Err(RecordError::HoldsRoot {
                step: number(index),
                path: path.clone(),
            });
        }
    }

    let staging = tempfile::Builder::new()
        .prefix("record-")
        .tempdir_in(scenario_dir)
        .map_err(unread(scenario_dir))?;
    write(staging.path(), &first, &meta)?;
    // The record that is there is moved aside and removed only once the new one is in its place.
    let retired = tempfile::Builder::new()
        .prefix("record-")
        .tempdir_in(scenario_dir)
        .map_err(unread(scenario_dir))?;
    let aside = retired.path().join(model::RECORD);
    let replaced = place.symlink_metadata().is_ok();
    if replaced {
        std::fs::rename(&place, &aside).map_err(unread(&place))?;
    }
    if let Err(error) = std::fs::rename(staging.path(), &place) {
        if replaced {
            let _ = std::fs::rename(&aside, &place);
        }
        return Err(unread(&place)(error));
    }
    // The directory was moved, so there is none left to remove.
    let _ = staging.keep();
    Ok(())
}

/// The number of the step at `index`, counted from 1.
fn number(index: usize) -> u32 {
    u32::try_from(index + 1).unwrap_or(u32::MAX)
}

/// A tree a record runs in. When it is kept, dropping it leaves the tree on disk and names it on stderr.
struct Held {
    tree: Option<TempTree>,
    keep: bool,
}

impl Held {
    fn get(&self) -> &TempTree {
        self.tree.as_ref().expect("the tree is held until the drop")
    }
}

impl Drop for Held {
    fn drop(&mut self) {
        if !self.keep {
            return;
        }
        if let Some(tree) = self.tree.take() {
            let _ = writeln!(
                io::stderr(),
                "kept the isolated tree {}",
                tree.root().display()
            );
            std::mem::forget(tree);
        }
    }
}

/// `steps`, the steps of `scenario` as one run observed them, with every redaction applied: each path, as
/// bytes, replaced by its token, as the module describes.
fn redacted(
    steps: Vec<StepRecord>,
    scenario: &Scenario,
    redactions: &[(PathBuf, String)],
) -> Vec<StepRecord> {
    if redactions.is_empty() {
        return steps;
    }
    let bytes = |content: &[u8]| {
        redactions
            .iter()
            .fold(content.to_vec(), |content, (path, token)| {
                normalize::replace(&content, path.as_os_str().as_bytes(), token.as_bytes())
            })
    };
    let text = |text: &str| String::from_utf8_lossy(&bytes(text.as_bytes())).into_owned();

    // A hash stands for a content, so one content a redaction changes has one new hash wherever it is
    // listed, at the step that stored it and at every later one.
    let mut hashes = BTreeMap::new();
    for step in &steps {
        for content in step.before.files.values().chain(step.after.files.values()) {
            let (was, is) = (
                snapshot::hash_of(content),
                snapshot::hash_of(&bytes(content)),
            );
            if was != is {
                hashes.insert(was, is);
            }
        }
    }
    let state = |snapshot: Snapshot, previous: Option<&Snapshot>| {
        let mut snapshot = snapshot;
        for entry in &mut snapshot.tree {
            match &mut entry.kind {
                EntryKind::Directory => {}
                EntryKind::File { hash, .. } => {
                    if let Some(is) = hashes.get(hash) {
                        hash.clone_from(is);
                    }
                }
                EntryKind::Link { target } => *target = text(target),
            }
        }
        for repository in &mut snapshot.git {
            for block in &mut repository.blocks {
                for line in &mut block.lines {
                    *line = text(line);
                }
            }
        }
        // Two contents a redaction makes one leave the later of them with nothing to store.
        let implied: Vec<String> = snapshot::implied(&snapshot.tree, previous)
            .into_iter()
            .map(str::to_owned)
            .collect();
        snapshot.files = std::mem::take(&mut snapshot.files)
            .into_iter()
            .filter(|(path, _)| implied.contains(path))
            .map(|(path, content)| (path, bytes(&content)))
            .collect();
        snapshot
    };

    let mut redacted = Vec::with_capacity(steps.len());
    let mut previous: Option<Snapshot> = None;
    for (step, described) in steps.into_iter().zip(&scenario.steps) {
        let stdout = bytes(&step.stdout);
        let stdout_json = normalize::stdout_json(&stdout, described.stdout)
            .ok()
            .flatten();
        let before = state(step.before, previous.as_ref());
        let after = state(step.after, Some(&before));
        previous = Some(after.clone());
        redacted.push(StepRecord {
            exit: step.exit,
            stdout,
            stderr: bytes(&step.stderr),
            stdout_json,
            before,
            after,
        });
    }
    redacted
}

/// Writes `steps` and `meta` to `directory` as a record, and reads every snapshot of it back.
fn write(directory: &Path, steps: &[StepRecord], meta: &Meta) -> Result<(), RecordError> {
    let unwritten = |path: &Path| {
        let path = path.to_path_buf();
        move |error| RecordError::Io { path, error }
    };
    let file =
        |path: PathBuf, content: &[u8]| std::fs::write(&path, content).map_err(unwritten(&path));
    // A temporary directory is its owner's alone, and a record is read by whoever reads the scenario.
    std::fs::set_permissions(directory, Permissions::from_mode(0o755))
        .map_err(unwritten(directory))?;
    let meta_file = Path::new(model::META)
        .strip_prefix(model::RECORD)
        .unwrap_or(Path::new(model::META));
    file(directory.join(meta_file), meta.to_toml().as_bytes())?;

    let mut previous: Option<Snapshot> = None;
    for (index, step) in steps.iter().enumerate() {
        let step_directory = directory
            .join(STEPS_DIRECTORY)
            .join(format!("{:02}", number(index)));
        std::fs::create_dir_all(&step_directory).map_err(unwritten(&step_directory))?;
        let exit = format!("{}\n", compare::exit_text(step.exit));
        file(step_directory.join(EXIT_CODE_FILE), exit.as_bytes())?;
        file(step_directory.join(STDOUT_FILE), &step.stdout)?;
        file(step_directory.join(STDERR_FILE), &step.stderr)?;
        if let Some(json) = &step.stdout_json {
            file(step_directory.join(STDOUT_JSON_FILE), json.as_bytes())?;
        }
        let (before, after) = (step_directory.join(BEFORE), step_directory.join(AFTER));
        step.before.write(&before).map_err(RecordError::Snapshot)?;
        step.after.write(&after).map_err(RecordError::Snapshot)?;
        let before = Snapshot::read(&before, previous.as_ref()).map_err(RecordError::Snapshot)?;
        let after = Snapshot::read(&after, Some(&before)).map_err(RecordError::Snapshot)?;
        previous = Some(after);
    }
    Ok(())
}
