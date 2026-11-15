//! The steps of one scenario, run with one executable in the tree its fixture was built in, and what each
//! step did, in the form the comparison reads; and the replay of every scenario of a root against its
//! record.
//!
//! The executable is whatever the caller names. Every step is one call of [`invoke`]: the child gets the
//! pinned environment of the tree, the additions of the step, and the additions of the caller, and nothing
//! of the environment of this process; it leads a process group of its own, and is killed with its group
//! at its deadline or once it has written more than is captured.
//!
//! # What a step may ask for
//!
//! A description decides what is run on the machine of whoever runs it, so [`run_steps`] trusts no caller:
//! it holds every step to the rules below itself, for a [`Scenario`] put together in code as for one the
//! model has read. The first three are checked for every step before anything is started, the last two
//! for each step immediately before it is started. A step that fails one is not started, and no step after
//! it is.
//!
//! - **Environment, by name.** A step adds variables and replaces none: a name the pinned environment
//!   holds, `PATH`, `HOME`, `TMPDIR`, and the ceiling of git's search among them, is refused. So is every
//!   name that starts with `GIT_` or `LD_`, which git and the loader read whatever else is pinned, a name
//!   the caller's own additions set, and an empty name or one with `=` or a NUL.
//! - **Environment, by value.** The model's rule, no value starts with `/`, and beyond it: no part of a
//!   value after a `:` or a `=` starts with one `/`, so a list names no absolute path either; no segment of
//!   it is `..`, so `<ROOT>/..` is no way out; and it holds no NUL. Every `<ROOT>` of a value becomes the
//!   root of the tree.
//! - **Input.** The file a step reads as its input is the model's: a confined relative path that, every
//!   link resolved, is a regular file under the scenario directory.
//! - **Working directory.** The directory about to be given to [`invoke`], every link resolved, is an
//!   existing directory at or below the root of the tree, by the check of the tree that the git helpers
//!   and the fixture builder use. Anything else is [`RunFailure::Unconfined`].
//! - **Arguments.** The model's rule, no argument starts with `/`, and beyond it: neither does a part of
//!   an argument after a `=`, nor what directly follows the letters of a short option; and none of the
//!   three, read segment by segment from the working directory, has a prefix that ends above the root of
//!   the tree. An argument holds no NUL. `<ROOT>` is not replaced in an argument.
//!
//! That is where it ends. The rules read an argument and a value as text: a path behind a symbolic link
//! the fixture or an earlier step made, a path in a syntax of the program's own, and what the program does
//! with a name it is given are not seen. The steps are kept inside their tree; they are not run in a
//! sandbox.
//!
//! # What is observed
//!
//! A step that ran to its end is a [`StepRecord`]: its exit, a code or the signal that ended it; the bytes
//! of both streams with the root of the tree replaced and nothing else changed; the canonical form of a
//! JSON output, which is always made here from those bytes of stdout; and the state of the tree before
//! and after, each taken by [`snapshot::take`]. A JSON output that does not read as its mode says has no
//! canonical form, and the record holds none: the comparison answers for that.
//!
//! A step that gave no observation is a [`RunFailure`] that says which way it ended, a deadline, more
//! output than is captured, a child that could not be started or reaped, and never an exit code. The steps
//! before it stay run, and none after it is started.
//!
//! # Replay
//!
//! [`replay_all`] replays every entry discovery reports under a scenarios root, or those the filters
//! choose, and each ends in exactly one way. It is deferred, and nothing is run for it, when it needs a
//! facility that is not in [`SUPPORTED`], whether it has a record or not. It fails without a tree and
//! without a start of the executable when it is malformed, has no `record/`, has a `[[stub]]` while
//! [`CALL_CAPTURE`] is false, has a step that is refused without a tree, has a record that is stale or
//! does not read whole, or has an expectation that cannot be laid over its record. Otherwise its fixture
//! is built in a tree of its own, and it fails when the fixture is not built or is not the one the record
//! holds from before its first step, when a step gives no observation, or when
//! [`compare::differences`] finds a difference from the record, under the expectation of
//! [`compare::overlay`] where the scenario has one. It passes when none of that is so. A step gets the
//! pinned environment of its tree and its own additions, and nothing from the caller.
//!
//! A record reads whole when `record/` holds `meta.toml` and `steps` alone, `steps` one directory for each
//! described step, and each of those the exit file, both streams, the canonical JSON only where the step
//! stores JSON, and the two snapshots, read as stored snapshots, with no other entry.
//!
//! The scenarios run on worker threads and are written in the order of their ids whatever order they end
//! in: `ok <id>`, `deferred <id> needs <facility>[,<facility>…]`, or `FAILED <id>` followed by one block
//! for each difference, which names the scenario, the dimension, the step, and the subject, and shows the
//! recorded and the observed value, as a unified diff where a value has more than one line. Under the
//! blocks stand the first lines of what each step a difference names wrote, and the tree when it is
//! still there: the tree of a scenario that passed is removed even where trees are kept, and that of one
//! that failed is removed where they are not. A tree that cannot be removed then is one more difference
//! of its scenario, beside those it already has. The last line is
//! `scenarios: <p> passed, <f> failed, <d> deferred`. A scenarios root that cannot be read is one failure
//! under its path.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::{self, Write as _};
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc;
use std::time::Duration;

use super::compare::{
    self, Difference, Dimension, EXIT_CODE_FILE, STDERR_FILE, STDOUT_FILE, STDOUT_JSON_FILE,
    StepRecord,
};
use super::fixture::{self, FixtureError, ROOT_TOKEN};
use super::model::{
    self, Discovered, Facility, Meta, Reference, Scenario, State, StdoutMode, Step,
};
use super::normalize;
use super::record::{STEPS_DIRECTORY, STUB_KEY};
use super::snapshot::{self, AFTER, BEFORE, Snapshot, SnapshotError};
use crate::env::PinnedEnv;
use crate::process::{FailureKind, InvocationFailure, invoke};
use crate::tree::{TempTree, Use};

/// The facilities a scenario may need and still be run.
pub const SUPPORTED: &[Facility] = &[];

/// Whether the calls of the programs a scenario stubs are captured.
pub const CALL_CAPTURE: bool = false;

/// The key of a step that names its working directory.
pub(super) const CWD_KEY: &str = "cwd";

/// The key of a step that holds its arguments.
const ARGS_KEY: &str = "args";

/// The key of a step that holds its additions to the environment.
const ENV_KEY: &str = "env";

/// The key of a step that names the file it reads as its input.
const STDIN_KEY: &str = "stdin";

/// The starts of the names no step sets: git reads the first and the loader the second, in every program
/// a step starts.
const REFUSED_NAME_STARTS: [&str; 2] = ["GIT_", "LD_"];

/// Why the steps of a scenario were not all run and observed. A step is counted from 1.
#[derive(Debug)]
pub enum RunFailure {
    /// The working directory of the step does not resolve to a directory inside the tree. The step was
    /// not started, and none after it.
    Unconfined {
        step: u32,
        /// The directory that was about to be used.
        cwd: PathBuf,
    },
    /// The step asks for something no step may: an argument, a variable, or an input, as the module
    /// lists. The step was not started, and none after it.
    Refused {
        step: u32,
        /// The key of the step at fault.
        key: &'static str,
        reason: String,
    },
    /// The file the step reads as its input could not be read. The step was not started.
    Stdin {
        step: u32,
        path: PathBuf,
        error: io::Error,
    },
    /// The executable is not an absolute path. Nothing was started.
    InvalidInvocation { step: u32 },
    /// The executable could not be started. Holds the cause.
    Spawn { step: u32, cause: String },
    /// The step outran its deadline and its process group was killed. Holds what it had written.
    Timeout {
        step: u32,
        stdout: Vec<u8>,
        stderr: Vec<u8>,
    },
    /// The step wrote more to one stream than is captured, and its process group was killed. Holds the
    /// start of what it wrote.
    OutputLimit {
        step: u32,
        stdout: Vec<u8>,
        stderr: Vec<u8>,
    },
    /// The step could not be reaped, or a descendant outside its process group kept a stream open. Holds
    /// what was written.
    Cleanup {
        step: u32,
        stdout: Vec<u8>,
        stderr: Vec<u8>,
    },
    /// The state of the tree could not be taken: after the step, or before the first one when `step` is
    /// `None`.
    Snapshot {
        step: Option<u32>,
        error: SnapshotError,
    },
}

impl fmt::Display for RunFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unconfined { step, cwd } => write!(
                f,
                "step {step} was not started: {} is no directory inside the tree",
                cwd.display()
            ),
            Self::Refused { step, key, reason } => {
                write!(f, "step {step} was not started, at `{key}`: {reason}")
            }
            Self::Stdin { step, path, error } => write!(
                f,
                "step {step} was not started: its input {} could not be read: {error}",
                path.display()
            ),
            Self::InvalidInvocation { step } => write!(
                f,
                "step {step} was not started: the executable is not an absolute path"
            ),
            Self::Spawn { step, cause } => {
                write!(
                    f,
                    "step {step}: the executable could not be started: {cause}"
                )
            }
            Self::Timeout { step, .. } => {
                write!(f, "step {step} outran its deadline and was killed")
            }
            Self::OutputLimit { step, .. } => {
                write!(f, "step {step} wrote more than is captured and was killed")
            }
            Self::Cleanup { step, .. } => {
                write!(f, "step {step} could not be reaped or left a stream open")
            }
            Self::Snapshot {
                step: Some(step),
                error,
            } => write!(f, "the state after step {step} was not taken: {error}"),
            Self::Snapshot { step: None, error } => {
                write!(f, "the state before the first step was not taken: {error}")
            }
        }
    }
}

impl std::error::Error for RunFailure {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Stdin { error, .. } => Some(error),
            Self::Snapshot { error, .. } => Some(error),
            Self::Unconfined { .. }
            | Self::Refused { .. }
            | Self::InvalidInvocation { .. }
            | Self::Spawn { .. }
            | Self::Timeout { .. }
            | Self::OutputLimit { .. }
            | Self::Cleanup { .. } => None,
        }
    }
}

/// Runs the steps of `scenario` in order with `executable`, in `tree`, where the fixture of the scenario
/// was built, and answers what each did.
///
/// A step runs under the pinned environment of the tree with its own additions and then `extra_env`, the
/// caller's, in its working directory, with its arguments and its input, to the deadline of an invocation
/// of the tree. The module says what a step may ask for and what is observed of it.
pub fn run_steps(
    scenario: &Scenario,
    tree: &TempTree,
    executable: &Path,
    extra_env: &[(String, String)],
) -> Result<Vec<StepRecord>, RunFailure> {
    let deadline = tree.invocation(executable).timeout;
    run_steps_within(scenario, tree, executable, extra_env, deadline)
}

/// As [`run_steps`], with `deadline` as how long each step may run.
pub fn run_steps_within(
    scenario: &Scenario,
    tree: &TempTree,
    executable: &Path,
    extra_env: &[(String, String)],
    deadline: Duration,
) -> Result<Vec<StepRecord>, RunFailure> {
    if let Some(refused) = refused(scenario, extra_env) {
        return Err(refused);
    }
    let taken = |step| move |error| RunFailure::Snapshot { step, error };
    let mut before = snapshot::take(tree, &scenario.compare, None).map_err(taken(None))?;
    let mut records = Vec::with_capacity(scenario.steps.len());
    for (index, step) in scenario.steps.iter().enumerate() {
        let number = number(index);
        let given = tree.workspace_dir().join(&step.cwd);
        let Some(cwd) = working_directory(tree, &given) else {
            return Err(RunFailure::Unconfined {
                step: number,
                cwd: given,
            });
        };
        let depth = cwd
            .strip_prefix(tree.root())
            .map_or(0, |below| below.components().count());
        if let Some(reason) = step.args.iter().find_map(|arg| leaving_arg(arg, depth)) {
            return Err(RunFailure::Refused {
                step: number,
                key: ARGS_KEY,
                reason,
            });
        }
        let stdin = match &step.stdin {
            None => Vec::new(),
            Some(stdin) => {
                let path = scenario.dir.join(stdin);
                std::fs::read(&path).map_err(|error| RunFailure::Stdin {
                    step: number,
                    path,
                    error,
                })?
            }
        };

        let mut invocation = tree.invocation(executable);
        invocation.args = step.args.iter().map(Into::into).collect();
        invocation.cwd = cwd;
        invocation.stdin = stdin;
        invocation.timeout = deadline;
        for (name, value) in &step.env {
            invocation.env.set(name, &with_root(value, tree.root()));
        }
        for (name, value) in extra_env {
            invocation.env.set(name, value);
        }
        let observation = invoke(invocation).map_err(|failure| not_observed(number, failure))?;

        let after =
            snapshot::take(tree, &scenario.compare, Some(&before)).map_err(taken(Some(number)))?;
        let stdout = normalize::root(&observation.stdout, tree.root());
        let stdout_json = normalize::stdout_json(&stdout, step.stdout).ok().flatten();
        // The state before the next step is this one, which stores no content a second time.
        let next = Snapshot {
            files: Default::default(),
            ..after.clone()
        };
        records.push(StepRecord {
            exit: observation.exit,
            stdout,
            stderr: normalize::root(&observation.stderr, tree.root()),
            stdout_json,
            before: std::mem::replace(&mut before, next),
            after,
        });
    }
    Ok(records)
}

/// The first step of `scenario` that asks for what needs no tree to refuse, as the failure that says so:
/// a variable, a value, an argument that names an absolute path, or an input, under the rules of the
/// module. `extra_env` is what the caller adds to the environment of every step.
pub(super) fn refused(scenario: &Scenario, extra_env: &[(String, String)]) -> Option<RunFailure> {
    scenario.steps.iter().enumerate().find_map(|(index, step)| {
        refused_step(step, &scenario.dir, extra_env).map(|(key, reason)| RunFailure::Refused {
            step: number(index),
            key,
            reason,
        })
    })
}

/// The number of the step at `index`, counted from 1.
fn number(index: usize) -> u32 {
    u32::try_from(index + 1).unwrap_or(u32::MAX)
}

/// The key of `step` that asks for what no step may, with the reason, when one does and no tree is needed
/// to tell.
fn refused_step(
    step: &Step,
    dir: &Path,
    extra_env: &[(String, String)],
) -> Option<(&'static str, String)> {
    let at = |key: &'static str| move |reason: String| (key, reason);
    let env = step.env.iter().find_map(|(name, value)| {
        refused_env_name(name, extra_env).or_else(|| refused_env_value(name, value))
    });
    let args = || step.args.iter().find_map(|arg| refused_arg(arg));
    let stdin = || {
        let stdin = step.stdin.as_deref()?;
        model::unconfined(stdin).or_else(|| model::refused_stdin(dir, stdin))
    };
    env.map(at(ENV_KEY))
        .or_else(|| args().map(at(ARGS_KEY)))
        .or_else(|| stdin().map(at(STDIN_KEY)))
}

/// Why a step may not set the variable `name`, when it may not.
fn refused_env_name(name: &str, extra_env: &[(String, String)]) -> Option<String> {
    let token = String::from_utf8_lossy(ROOT_TOKEN);
    let reason = if name.is_empty() || name.contains(['=', '\0']) {
        "is no name of a variable"
    } else if PinnedEnv::for_root(&token)
        .variables()
        .any(|(pinned, _)| pinned == name)
    {
        "is a variable of the pinned environment, which a step adds to and never replaces"
    } else if REFUSED_NAME_STARTS
        .iter()
        .any(|start| name.starts_with(start))
    {
        "is read by git or by the loader in every program the step starts"
    } else if extra_env.iter().any(|(extra, _)| extra == name) {
        "is set by whoever runs the steps"
    } else {
        return None;
    };
    Some(format!("the variable {name:?} {reason}"))
}

/// Why a step may not set the variable `name` to `value`, when it may not.
fn refused_env_value(name: &str, value: &str) -> Option<String> {
    if let Some(reason) = model::refused_step_env_value(name, value) {
        return Some(reason);
    }
    let absolute = |part: &str| part.starts_with('/') && !part.starts_with("//");
    let reason = if value.contains('\0') {
        "holds a NUL"
    } else if value.split([':', '=']).skip(1).any(absolute) {
        "names an absolute path after a \":\" or a \"=\"; a path is named through <ROOT>"
    } else if value.split(['/', ':', '=']).any(|segment| segment == "..") {
        "has \"..\" as a segment"
    } else {
        return None;
    };
    Some(format!("the value {value:?} of {name} {reason}"))
}

/// What of `arg` may be read as a path: all of it, every part of it after a `=`, and what directly
/// follows the letters of a short option.
fn path_readings(arg: &str) -> impl Iterator<Item = &str> {
    let values = arg.match_indices('=').map(|(at, _)| &arg[at + 1..]);
    std::iter::once(arg)
        .chain(values)
        .chain(fixture::short_option_values(arg))
}

/// Why `arg` may not be an argument of a step, when it may not and no tree is needed to tell.
fn refused_arg(arg: &str) -> Option<String> {
    if let Some(reason) = model::refused_step_arg(arg) {
        return Some(reason);
    }
    if arg.contains('\0') {
        return Some(format!("{arg:?} holds a NUL"));
    }
    path_readings(arg)
        .any(|reading| reading.starts_with('/'))
        .then(|| format!("{arg:?} names an absolute path"))
}

/// Why `arg` may not be an argument of a step whose working directory lies `depth` directories below the
/// root of the tree, when it may not: read as a path from there, it has a prefix that ends above the root.
fn leaving_arg(arg: &str, depth: usize) -> Option<String> {
    let leaves = |reading: &str| {
        let mut depth = depth;
        for segment in reading.split('/') {
            match segment {
                "" | "." => {}
                ".." => match depth.checked_sub(1) {
                    Some(above) => depth = above,
                    None => return true,
                },
                _ => depth += 1,
            }
        }
        false
    };
    path_readings(arg).any(leaves).then(|| {
        format!("{arg:?}, read as a path from the working directory, leads out of the tree")
    })
}

/// `given`, the working directory a step asks for, with every link resolved, when that is an existing
/// directory at or below the root of `tree`.
fn working_directory(tree: &TempTree, given: &Path) -> Option<PathBuf> {
    let resolved = given.canonicalize().ok()?;
    let lands = tree.landing(&resolved, Use::Base)?;
    lands.is_dir().then_some(lands)
}

/// `value` with every root token replaced by the root of the tree.
fn with_root(value: &str, root: &Path) -> String {
    let replaced = normalize::replace(value.as_bytes(), ROOT_TOKEN, root.as_os_str().as_bytes());
    String::from_utf8_lossy(&replaced).into_owned()
}

/// The failure of the step `step` whose invocation gave no observation.
fn not_observed(step: u32, failure: InvocationFailure) -> RunFailure {
    let InvocationFailure {
        kind,
        stdout,
        stderr,
    } = failure;
    match kind {
        FailureKind::InvalidInvocation => RunFailure::InvalidInvocation { step },
        FailureKind::Spawn(cause) => RunFailure::Spawn { step, cause },
        FailureKind::Timeout => RunFailure::Timeout {
            step,
            stdout,
            stderr,
        },
        FailureKind::OutputLimit => RunFailure::OutputLimit {
            step,
            stdout,
            stderr,
        },
        FailureKind::Cleanup => RunFailure::Cleanup {
            step,
            stdout,
            stderr,
        },
    }
}

/// What a difference shows for the binary where it was not run.
const NOT_RUN: &str = "not run";

/// What a difference shows for a side that has nothing of the subject.
const NOTHING: &str = "absent";

/// The file of a record that says what it was made with, as a record names it.
const META_FILE: &str = "meta.toml";

/// How many lines around a changed one a unified diff shows.
const CONTEXT: usize = 3;

/// The size of the table a unified diff is made with, above which both values are shown in full instead.
const DIFF_CELLS: usize = 1_000_000;

/// How many lines of a stream are shown beside a failure.
const SHOWN_LINES: usize = 40;

/// What the scenarios of a root are replayed with.
#[derive(Clone, Debug)]
pub struct ReplayOptions {
    /// The executable every step runs: an absolute path.
    pub executable: PathBuf,
    /// The directory that holds the command directories and `reference.toml`.
    pub scenarios_root: PathBuf,
    /// A scenario is replayed when its id contains one of these; every scenario when there is none.
    pub filters: Vec<String>,
    /// How many scenarios run at once; at least one does.
    pub jobs: usize,
}

/// How a replay ended, each list in the order of the ids. Every entry discovery reported and the filters
/// chose is in exactly one of the three.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ReplayReport {
    pub passed: Vec<String>,
    /// Each with at least one difference.
    pub failed: Vec<(String, Vec<Difference>)>,
    /// Each with the facilities it needs and the runner lacks.
    pub deferred: Vec<(String, Vec<Facility>)>,
}

/// Where one entry of discovery stands before anything is run for it.
enum Standing<'a> {
    /// It is no scenario that can be read: the difference that says why.
    Malformed(Difference),
    /// It needs these facilities, which the runner lacks. It is not run, recorded or not.
    Deferred(Vec<Facility>),
    Recorded(&'a Scenario),
    Unrecorded(&'a Scenario),
}

/// How the replay of one entry ended.
enum Verdict {
    Passed,
    Deferred(Vec<Facility>),
    Failed(Failure),
}

/// What is printed under a scenario that failed.
struct Failure {
    differences: Vec<Difference>,
    /// What the steps the differences name wrote.
    streams: Vec<Streams>,
    /// The tree the scenario ran in, when it is still there.
    kept: Option<PathBuf>,
}

/// What one step wrote, with the root of its tree replaced.
struct Streams {
    step: u32,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
}

impl Failure {
    fn of(differences: Vec<Difference>) -> Self {
        Self {
            differences,
            streams: Vec::new(),
            kept: None,
        }
    }
}

/// Replays the scenarios of the root of `options` with its executable, writes the lines of the module to
/// `out`, and answers how each scenario ended. Something failed exactly when `failed` of the answer holds
/// an entry. What cannot be written to `out` is not written; the answer is the same.
pub fn replay_all(options: &ReplayOptions, out: &mut dyn io::Write) -> ReplayReport {
    let root = &options.scenarios_root;
    let mut report = ReplayReport::default();
    let found = match selected(root, &options.filters) {
        Ok(found) => found,
        Err(error) => {
            let id = root.display().to_string();
            let unread = malformed(&id, format!("the scenarios root cannot be read: {error}"));
            show(
                out,
                &mut report,
                &id,
                Verdict::Failed(Failure::of(vec![unread])),
            );
            Vec::new()
        }
    };
    let reference = Reference::load(root).map_err(|error| error.to_string());

    let next = AtomicUsize::new(0);
    let (send, receive) = mpsc::channel();
    let mut shown = 0;
    std::thread::scope(|scope| {
        for _ in 0..options.jobs.clamp(1, found.len().max(1)) {
            let send = send.clone();
            let (next, found, reference) = (&next, &found, &reference);
            scope.spawn(move || {
                while let Some(entry) = found.get(next.fetch_add(1, Ordering::Relaxed)) {
                    let replayed = catch_unwind(AssertUnwindSafe(|| {
                        replay_one(entry, reference, &options.executable)
                    }));
                    let verdict = replayed.unwrap_or_else(|_| lost("the replay panicked"));
                    if send.send((entry.id.as_str(), verdict)).is_err() {
                        break;
                    }
                }
            });
        }
        drop(send);
        // A verdict waits until every scenario before it has been shown.
        let mut waiting = BTreeMap::new();
        for (id, verdict) in receive {
            waiting.insert(id.as_bytes(), verdict);
            while let Some(entry) = found.get(shown) {
                let Some(verdict) = waiting.remove(entry.id.as_bytes()) else {
                    break;
                };
                show(out, &mut report, &entry.id, verdict);
                shown += 1;
            }
        }
    });
    for entry in found.iter().skip(shown) {
        show(
            out,
            &mut report,
            &entry.id,
            lost("the replay gave no verdict"),
        );
    }
    let _ = writeln!(
        out,
        "scenarios: {} passed, {} failed, {} deferred",
        report.passed.len(),
        report.failed.len(),
        report.deferred.len()
    );
    report
}

/// Writes to `out` one line for each entry of `scenarios_root` that `filters` choose, in the order of the
/// ids: the id and `recorded`, `unrecorded`, `deferred`, or `malformed`. Nothing is run and no tree is
/// created.
pub fn list(scenarios_root: &Path, filters: &[String], out: &mut dyn io::Write) -> io::Result<()> {
    for entry in selected(scenarios_root, filters)? {
        let state = match standing(&entry) {
            Standing::Malformed(_) => "malformed",
            Standing::Deferred(_) => "deferred",
            Standing::Recorded(_) => "recorded",
            Standing::Unrecorded(_) => "unrecorded",
        };
        writeln!(out, "{} {state}", entry.id)?;
    }
    Ok(())
}

/// What discovery reports under `root`, in its order, without the entries whose id holds none of `filters`
/// when there is one.
fn selected(root: &Path, filters: &[String]) -> io::Result<Vec<Discovered>> {
    let mut found = model::discover(root)?;
    if !filters.is_empty() {
        found.retain(|entry| {
            filters
                .iter()
                .any(|filter| entry.id.contains(filter.as_str()))
        });
    }
    Ok(found)
}

/// Where `entry` stands. A scenario that needs what the runner lacks is deferred whether it has a record
/// or not.
fn standing(entry: &Discovered) -> Standing<'_> {
    let scenario = match (&entry.state, &entry.scenario) {
        (State::Malformed(found), _) => {
            return Standing::Malformed(malformed(&found.key, found.reason.clone()));
        }
        (_, None) => {
            let reason = "discovery answered no description".to_owned();
            return Standing::Malformed(malformed("scenario.toml", reason));
        }
        (_, Some(scenario)) => scenario,
    };
    let missing: Vec<Facility> = scenario
        .needs
        .iter()
        .copied()
        .filter(|facility| !SUPPORTED.contains(facility))
        .collect();
    if !missing.is_empty() {
        Standing::Deferred(missing)
    } else if entry.state == State::Recorded {
        Standing::Recorded(scenario)
    } else {
        Standing::Unrecorded(scenario)
    }
}

/// A difference of `dimension` between `recorded` and `observed` at `subject`.
fn difference(
    dimension: Dimension,
    step: Option<u32>,
    subject: &str,
    recorded: String,
    observed: String,
) -> Difference {
    Difference {
        dimension,
        step,
        subject: subject.to_owned(),
        recorded,
        observed,
    }
}

/// The difference of a scenario that is malformed at `key`, for which the binary is not run.
fn malformed(key: &str, reason: String) -> Difference {
    difference(Dimension::Malformed, None, key, reason, NOT_RUN.to_owned())
}

/// `found`, as a difference of the step `step`.
fn at_step(found: Difference, step: u32) -> Difference {
    Difference {
        step: Some(step),
        ..found
    }
}

/// The verdict of a scenario whose replay did not end: it failed, and `reason` says how.
fn lost(reason: &str) -> Verdict {
    let lost = difference(
        Dimension::Malformed,
        None,
        "replay",
        NOTHING.to_owned(),
        reason.to_owned(),
    );
    Verdict::Failed(Failure::of(vec![lost]))
}

/// Replays `entry` with `executable`. `reference` is the pinned reference of the root, or why it does not
/// read.
fn replay_one(
    entry: &Discovered,
    reference: &Result<Reference, String>,
    executable: &Path,
) -> Verdict {
    let (scenario, has_record) = match standing(entry) {
        Standing::Malformed(found) => return Verdict::Failed(Failure::of(vec![found])),
        Standing::Deferred(missing) => return Verdict::Deferred(missing),
        Standing::Unrecorded(scenario) => (scenario, false),
        Standing::Recorded(scenario) => (scenario, true),
    };
    if !CALL_CAPTURE && !scenario.stubs.is_empty() {
        let reason = "the calls of a stubbed program are not captured".to_owned();
        return Verdict::Failed(Failure::of(vec![malformed(STUB_KEY, reason)]));
    }
    if !has_record {
        let unrecorded = difference(
            Dimension::Malformed,
            None,
            model::RECORD,
            "unrecorded: the scenario directory holds no record/".to_owned(),
            NOT_RUN.to_owned(),
        );
        return Verdict::Failed(Failure::of(vec![unrecorded]));
    }
    let recorded = match stored(scenario, reference) {
        Ok(recorded) => recorded,
        Err(differences) => return Verdict::Failed(Failure::of(differences)),
    };
    let unkept = |error: io::Error| {
        let observed = format!("the isolated tree: {error}");
        difference(
            Dimension::Fixture,
            None,
            "tree",
            NOTHING.to_owned(),
            observed,
        )
    };
    let tree = match TempTree::new() {
        Ok(tree) => tree,
        Err(error) => return Verdict::Failed(Failure::of(vec![unkept(error)])),
    };
    let (mut differences, streams) = observed(scenario, &recorded, &tree, executable);
    if differences.is_empty() {
        return match tree.discard() {
            Ok(()) => Verdict::Passed,
            Err(error) => Verdict::Failed(Failure::of(vec![unkept(error)])),
        };
    }
    let root = tree.root().to_path_buf();
    if TempTree::kept_on_drop() {
        drop(tree);
    } else if let Err(error) = tree.discard() {
        // Dropped, a tree that cannot be removed would unwind and take the differences with it.
        differences.push(unkept(error));
    }
    Verdict::Failed(Failure {
        differences,
        streams,
        kept: root.symlink_metadata().is_ok().then_some(root),
    })
}

/// What `scenario` is held to: its record, read whole, with its expectation laid over it when it has one.
/// Fails with the differences of a scenario that may not be run or whose record is stale or does not read;
/// nothing was started and no tree created.
fn stored(
    scenario: &Scenario,
    reference: &Result<Reference, String>,
) -> Result<Vec<StepRecord>, Vec<Difference>> {
    match refused(scenario, &[]) {
        Some(RunFailure::Refused { step, key, reason }) => {
            let refused = malformed(key, format!("step {step}: {reason}"));
            return Err(vec![at_step(refused, step)]);
        }
        Some(failure) => return Err(vec![malformed("step", failure.to_string())]),
        None => {}
    }

    let unreadable = |file: &str, error: String| {
        let reason = format!("unreadable: {error}");
        vec![difference(
            Dimension::StaleRecord,
            None,
            file,
            reason,
            NOT_RUN.to_owned(),
        )]
    };
    let meta =
        Meta::load(&scenario.dir).map_err(|error| unreadable(model::META, error.to_string()))?;
    let reference = reference
        .as_ref()
        .map_err(|error| unreadable("reference.toml", error.clone()))?;
    let description = scenario
        .description_digest()
        .map_err(|error| unreadable("description", error.to_string()))?;
    let environment = PinnedEnv::for_root(&String::from_utf8_lossy(ROOT_TOKEN)).digest();
    let stale = compare::stale_record(&meta, reference, &environment, &description);
    if !stale.is_empty() {
        return Err(stale);
    }

    let recorded = stored_steps(scenario).map_err(|unread| vec![unread])?;
    if scenario.deviation.is_none() {
        return Ok(recorded);
    }
    let refused = |reason: String| vec![malformed(model::EXPECTED, reason)];
    let expected = expectation(scenario).map_err(refused)?;
    compare::overlay(&recorded, &expected).map_err(|error| refused(error.to_string()))
}

/// The names of the entries of `directory`.
fn names(directory: &Path) -> Result<BTreeSet<String>, String> {
    let unread = |error: io::Error| format!("{}: {error}", directory.display());
    std::fs::read_dir(directory)
        .map_err(unread)?
        .map(|entry| {
            Ok(entry
                .map_err(unread)?
                .file_name()
                .to_string_lossy()
                .into_owned())
        })
        .collect()
}

/// The bytes of the regular file `path`. A link in its place is not followed.
fn regular(path: &Path) -> Result<Vec<u8>, String> {
    let unread = |error: io::Error| format!("{}: {error}", path.display());
    if !path.symlink_metadata().map_err(unread)?.is_file() {
        return Err(format!("{} is no regular file", path.display()));
    }
    std::fs::read(path).map_err(unread)
}

/// The steps `record/` of `scenario` stores, as many as the scenario describes. Every snapshot is read as
/// a stored snapshot is, against the one before it. A record that holds an entry no record has, lacks one,
/// or holds one that does not read fails, as the difference that names the entry.
fn stored_steps(scenario: &Scenario) -> Result<Vec<StepRecord>, Difference> {
    let record = scenario.dir.join(model::RECORD);
    let steps = record.join(STEPS_DIRECTORY);
    let unreadable = |step: Option<u32>, path: &Path, reason: String| {
        let subject = path.strip_prefix(&scenario.dir).unwrap_or(path);
        difference(
            Dimension::Malformed,
            step,
            &subject.display().to_string(),
            format!("unreadable: {reason}"),
            NOT_RUN.to_owned(),
        )
    };
    let stray = |step: Option<u32>, directory: &Path, known: &[&str]| {
        let held = names(directory).map_err(|error| unreadable(step, directory, error))?;
        match held.iter().find(|name| !known.contains(&name.as_str())) {
            Some(name) => Err(unreadable(
                step,
                &directory.join(name),
                "no record holds this entry".to_owned(),
            )),
            None => Ok(held),
        }
    };

    stray(None, &record, &[META_FILE, STEPS_DIRECTORY])?;
    let described: BTreeSet<String> = (1..=scenario.steps.len())
        .map(|step| format!("{step:02}"))
        .collect();
    let held = names(&steps).map_err(|error| unreadable(None, &steps, error))?;
    if held != described {
        let reason = format!(
            "holds the steps {held:?}, and the scenario describes {}",
            described.len()
        );
        return Err(unreadable(None, &steps, reason));
    }

    let mut recorded = Vec::with_capacity(scenario.steps.len());
    let mut previous: Option<Snapshot> = None;
    for (index, step) in scenario.steps.iter().enumerate() {
        let number = number(index);
        let directory = steps.join(format!("{number:02}"));
        let unreadable = |path: &Path, reason: String| unreadable(Some(number), path, reason);
        let known = [
            EXIT_CODE_FILE,
            STDOUT_FILE,
            STDERR_FILE,
            STDOUT_JSON_FILE,
            BEFORE,
            AFTER,
        ];
        let held = stray(Some(number), &directory, &known)?;
        let file = |name: &str| {
            let path = directory.join(name);
            regular(&path).map_err(|error| unreadable(&path, error))
        };
        let text = |name: &str| {
            String::from_utf8(file(name)?)
                .map_err(|_| unreadable(&directory.join(name), "the file is not UTF-8".to_owned()))
        };
        let exit = compare::parse_exit(&text(EXIT_CODE_FILE)?).ok_or_else(|| {
            let reason = "not a code or a signal on one line".to_owned();
            unreadable(&directory.join(EXIT_CODE_FILE), reason)
        })?;
        let stdout_json = match held.contains(STDOUT_JSON_FILE) {
            true if step.stdout == StdoutMode::Text => {
                let reason = "the stdout of the step is text, which stores no JSON".to_owned();
                return Err(unreadable(&directory.join(STDOUT_JSON_FILE), reason));
            }
            true => Some(text(STDOUT_JSON_FILE)?),
            false => None,
        };
        let state = |name: &str, previous: Option<&Snapshot>| {
            let path = directory.join(name);
            Snapshot::read(&path, previous).map_err(|error| unreadable(&path, error.to_string()))
        };
        let before = state(BEFORE, previous.as_ref())?;
        let after = state(AFTER, Some(&before))?;
        previous = Some(after.clone());
        recorded.push(StepRecord {
            exit,
            stdout: file(STDOUT_FILE)?,
            stderr: file(STDERR_FILE)?,
            stdout_json,
            before,
            after,
        });
    }
    Ok(recorded)
}

/// The files of `expected/` of `scenario`, by the number of their step and by their path under the
/// directory of the step. Fails, naming the entry, for one that is neither `steps`, a step directory
/// named as a record names one, nor a regular file or a directory below such a one. No link is followed.
fn expectation(scenario: &Scenario) -> Result<BTreeMap<u32, BTreeMap<String, Vec<u8>>>, String> {
    fn gather(
        directory: &Path,
        prefix: &str,
        files: &mut BTreeMap<String, Vec<u8>>,
    ) -> Result<(), String> {
        for name in names(directory)? {
            let path = directory.join(&name);
            let relative = match prefix {
                "" => name,
                prefix => format!("{prefix}/{name}"),
            };
            let kind = path
                .symlink_metadata()
                .map_err(|error| format!("{}: {error}", path.display()))?;
            if kind.is_dir() {
                gather(&path, &relative, files)?;
            } else {
                files.insert(relative, regular(&path)?);
            }
        }
        Ok(())
    }

    let expected = scenario.dir.join(model::EXPECTED);
    if let Some(stray) = names(&expected)?
        .iter()
        .find(|name| *name != STEPS_DIRECTORY)
    {
        return Err(format!(
            "{} is no entry of an expectation",
            expected.join(stray).display()
        ));
    }
    let steps = expected.join(STEPS_DIRECTORY);
    let mut found = BTreeMap::new();
    for name in names(&steps)? {
        let directory = steps.join(&name);
        let number = name
            .parse::<u32>()
            .ok()
            .filter(|number| *number > 0 && format!("{number:02}") == name)
            .ok_or_else(|| format!("{} is not named as a step is", directory.display()))?;
        if !directory.symlink_metadata().is_ok_and(|kind| kind.is_dir()) {
            return Err(format!("{} is no directory", directory.display()));
        }
        let mut files = BTreeMap::new();
        gather(&directory, "", &mut files)?;
        found.insert(number, files);
    }
    Ok(found)
}

/// Builds the fixture of `scenario` in `tree`, runs its steps there with `executable`, and answers every
/// difference from `recorded`, with what the steps the differences name wrote.
///
/// A fixture that is not built, or is not the one the record was made in, is the one difference, and the
/// executable is not started.
fn observed(
    scenario: &Scenario,
    recorded: &[StepRecord],
    tree: &TempTree,
    executable: &Path,
) -> (Vec<Difference>, Vec<Streams>) {
    let unbuilt = |subject: &str, error: String| {
        difference(Dimension::Fixture, None, subject, NOTHING.to_owned(), error)
    };
    match fixture::build_in(scenario, tree) {
        Ok(()) => {}
        Err(FixtureError::Malformed(found)) => {
            return (vec![malformed(&found.key, found.reason)], Vec::new());
        }
        Err(error) => return (vec![unbuilt("fixture", error.to_string())], Vec::new()),
    }
    let built = match snapshot::take(tree, &scenario.compare, None) {
        Ok(built) => built,
        Err(error) => return (vec![unbuilt("snapshot", error.to_string())], Vec::new()),
    };
    let changed = recorded
        .first()
        .and_then(|first| compare::fixture(&first.before, &built));
    if let Some(changed) = changed {
        return (vec![changed], Vec::new());
    }

    let observed = match run_steps(scenario, tree, executable, &[]) {
        Ok(observed) => observed,
        Err(failure) => {
            let (unobserved, streams) = unobserved(failure, recorded, tree.root());
            return (vec![unobserved], streams.into_iter().collect());
        }
    };
    let differences = compare::differences(scenario, recorded, &observed);
    let named: BTreeSet<u32> = differences.iter().filter_map(|found| found.step).collect();
    let streams = named
        .into_iter()
        .filter_map(|step| {
            let wrote = observed.get(usize::try_from(step).ok()?.checked_sub(1)?)?;
            Some(Streams {
                step,
                stdout: wrote.stdout.clone(),
                stderr: wrote.stderr.clone(),
            })
        })
        .collect();
    (differences, streams)
}

/// The difference of a run that `failure` ended without an observation, and what the step had written,
/// with `root`, the root of its tree, replaced.
fn unobserved(
    failure: RunFailure,
    recorded: &[StepRecord],
    root: &Path,
) -> (Difference, Option<Streams>) {
    let reason = failure.to_string();
    let exit = |step: u32| {
        let recorded = usize::try_from(step)
            .ok()
            .and_then(|step| recorded.get(step.checked_sub(1)?))
            .map_or(NOTHING.to_owned(), |step| compare::exit_text(step.exit));
        difference(
            Dimension::Exit,
            Some(step),
            EXIT_CODE_FILE,
            recorded,
            reason.clone(),
        )
    };
    match failure {
        RunFailure::Unconfined { step, .. } => (at_step(malformed(CWD_KEY, reason), step), None),
        RunFailure::Refused { step, key, .. } => (at_step(malformed(key, reason), step), None),
        RunFailure::Stdin { .. } => (malformed(STDIN_KEY, reason), None),
        RunFailure::InvalidInvocation { step } | RunFailure::Spawn { step, .. } => {
            (exit(step), None)
        }
        RunFailure::Timeout {
            step,
            stdout,
            stderr,
        }
        | RunFailure::OutputLimit {
            step,
            stdout,
            stderr,
        }
        | RunFailure::Cleanup {
            step,
            stdout,
            stderr,
        } => {
            let streams = Streams {
                step,
                stdout: normalize::root(&stdout, root),
                stderr: normalize::root(&stderr, root),
            };
            (exit(step), Some(streams))
        }
        RunFailure::Snapshot {
            step: Some(step), ..
        } => {
            let untaken = difference(
                Dimension::Tree,
                Some(step),
                snapshot::TREE_FILE,
                "the state after the step".to_owned(),
                reason,
            );
            (untaken, None)
        }
        RunFailure::Snapshot { step: None, .. } => {
            let untaken = difference(
                Dimension::Fixture,
                None,
                "snapshot",
                NOTHING.to_owned(),
                reason,
            );
            (untaken, None)
        }
    }
}

/// Writes the lines of the scenario `id`, which ended as `verdict`, to `out`, and counts it in `report`.
fn show(out: &mut dyn io::Write, report: &mut ReplayReport, id: &str, verdict: Verdict) {
    let mut text = String::new();
    match verdict {
        Verdict::Passed => {
            let _ = writeln!(text, "ok {id}");
            report.passed.push(id.to_owned());
        }
        Verdict::Deferred(missing) => {
            let names: Vec<&str> = missing.iter().map(|facility| facility.name()).collect();
            let _ = writeln!(text, "deferred {id} needs {}", names.join(","));
            report.deferred.push((id.to_owned(), missing));
        }
        Verdict::Failed(failure) => {
            let _ = writeln!(text, "FAILED {id}");
            for found in &failure.differences {
                block(&mut text, id, found);
            }
            for streams in &failure.streams {
                for (name, wrote) in [
                    (STDOUT_FILE, &streams.stdout),
                    (STDERR_FILE, &streams.stderr),
                ] {
                    stream(&mut text, id, streams.step, name, wrote);
                }
            }
            if let Some(kept) = &failure.kept {
                let _ = writeln!(text, "  {id}: kept the isolated tree {}", kept.display());
            }
            report.failed.push((id.to_owned(), failure.differences));
        }
    }
    let _ = out.write_all(text.as_bytes());
}

/// Adds the block of `found`, a difference of the scenario `id`, to `text`: the scenario, the dimension,
/// the step, and the subject, then both values, as a unified diff where one of them has more than a line.
fn block(text: &mut String, id: &str, found: &Difference) {
    let step = found
        .step
        .map_or("no step".to_owned(), |step| format!("step {step}"));
    let _ = writeln!(
        text,
        "  {id}: {:?}, {step}, {}",
        found.dimension, found.subject
    );
    if found.recorded.contains('\n') || found.observed.contains('\n') {
        for line in unified(&found.recorded, &found.observed) {
            let _ = writeln!(text, "    {line}");
        }
    } else {
        let _ = writeln!(text, "    recorded: {}", found.recorded);
        let _ = writeln!(text, "    observed: {}", found.observed);
    }
}

/// Adds the first lines of `wrote`, what the step `step` of the scenario `id` wrote to the stream `name`,
/// to `text`; nothing for a stream nothing was written to.
fn stream(text: &mut String, id: &str, step: u32, name: &str, wrote: &[u8]) {
    if wrote.is_empty() {
        return;
    }
    let _ = writeln!(text, "  {id}: step {step}, {name} as observed");
    let wrote = String::from_utf8_lossy(wrote);
    for line in wrote.lines().take(SHOWN_LINES) {
        let _ = writeln!(text, "    | {line}");
    }
    let more = wrote.lines().count().saturating_sub(SHOWN_LINES);
    if more > 0 {
        let _ = writeln!(text, "    | and {more} more lines");
    }
}

/// The lines of a unified diff from `recorded` to `observed`, each read as the lines a line feed ends or
/// separates, with [`CONTEXT`] lines around a change. Two values too long for the table of the diff are
/// answered in full, the recorded one removed and the observed one added.
fn unified(recorded: &str, observed: &str) -> Vec<String> {
    let from: Vec<&str> = recorded.split('\n').collect();
    let to: Vec<&str> = observed.split('\n').collect();
    let mut lines = vec!["--- recorded".to_owned(), "+++ observed".to_owned()];
    if from.len().saturating_mul(to.len()) > DIFF_CELLS {
        lines.extend(from.iter().map(|line| format!("-{line}")));
        lines.extend(to.iter().map(|line| format!("+{line}")));
        return lines;
    }

    // common[i][j]: how many lines the longest sequence common to from[i..] and to[j..] has.
    let mut common = vec![vec![0_u32; to.len() + 1]; from.len() + 1];
    for i in (0..from.len()).rev() {
        for j in (0..to.len()).rev() {
            common[i][j] = match from[i] == to[j] {
                true => common[i + 1][j + 1] + 1,
                false => common[i + 1][j].max(common[i][j + 1]),
            };
        }
    }
    let mut edits = Vec::new();
    let (mut i, mut j) = (0, 0);
    while i < from.len() || j < to.len() {
        if i < from.len() && j < to.len() && from[i] == to[j] {
            edits.push((' ', from[i]));
            (i, j) = (i + 1, j + 1);
        } else if j == to.len() || (i < from.len() && common[i + 1][j] >= common[i][j + 1]) {
            edits.push(('-', from[i]));
            i += 1;
        } else {
            edits.push(('+', to[j]));
            j += 1;
        }
    }

    let changed: Vec<usize> = (0..edits.len()).filter(|at| edits[*at].0 != ' ').collect();
    let near = |at: usize| changed.iter().any(|change| change.abs_diff(at) <= CONTEXT);
    let (mut from_line, mut to_line) = (1, 1);
    let mut at = 0;
    while at < edits.len() {
        if !near(at) {
            (from_line, to_line, at) = (from_line + 1, to_line + 1, at + 1);
            continue;
        }
        let (from_start, to_start) = (from_line, to_line);
        let mut hunk = Vec::new();
        while at < edits.len() && near(at) {
            let (mark, line) = edits[at];
            if mark != '+' {
                from_line += 1;
            }
            if mark != '-' {
                to_line += 1;
            }
            hunk.push(format!("{mark}{line}"));
            at += 1;
        }
        lines.push(format!(
            "@@ -{from_start},{} +{to_start},{} @@",
            from_line - from_start,
            to_line - to_start
        ));
        lines.extend(hunk);
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    fn name(name: &str) -> Option<String> {
        refused_env_name(name, &[("CALLER_SET".to_owned(), "1".to_owned())])
    }

    #[test]
    fn a_step_adds_a_variable_and_replaces_none() {
        for refused in [
            "PATH",
            "HOME",
            "TMPDIR",
            "XDG_CONFIG_HOME",
            "GIT_CEILING_DIRECTORIES",
            "GIT_CONFIG_GLOBAL",
            "TZ",
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_SSH_COMMAND",
            "GIT_CONFIG_COUNT",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "CALLER_SET",
            "",
            "A=B",
            "A\0B",
        ] {
            assert!(name(refused).is_some(), "{refused:?} is not refused");
        }
        for added in ["WINTER_LOG_LEVEL", "GITHUB_TOKEN", "LDFLAGS", "path"] {
            assert_eq!(name(added), None);
        }
    }

    #[test]
    fn a_value_names_a_path_through_the_root_token_alone_and_never_above_it() {
        for refused in [
            "/etc",
            "a:/etc",
            "<ROOT>/bin:/usr/local/bin",
            "key=/etc",
            "<ROOT>/..",
            "<ROOT>/ws/../../x",
            "..",
            "a:../b",
            "a\0b",
        ] {
            assert!(
                refused_env_value("X", refused).is_some(),
                "{refused:?} is not refused"
            );
        }
        for added in [
            "debug",
            "<ROOT>/ws/state",
            "<ROOT>/bin:<ROOT>/ws/bin",
            "https://fixture.invalid/alpha.git",
            "a..b",
            "",
        ] {
            assert_eq!(refused_env_value("X", added), None);
        }
    }

    #[test]
    fn an_argument_names_no_absolute_path() {
        for refused in ["/etc", "--config=/etc/x", "-C/etc", "a=b=/c", "a\0b"] {
            assert!(refused_arg(refused).is_some(), "{refused:?} is not refused");
        }
        for passed in ["ws", "--json", "a/b", "--name=value", "", "-v", "~"] {
            assert_eq!(refused_arg(passed), None);
        }
    }

    #[test]
    fn an_argument_read_as_a_path_stays_at_or_below_the_root() {
        // One directory below the root, as the workspace directory is.
        for refused in ["../..", "a/../../..", "--dir=../..", "-C../.."] {
            assert!(
                leaving_arg(refused, 1).is_some(),
                "{refused:?} is not refused"
            );
        }
        for passed in ["..", "../remotes/alpha.git", "a/../..", "--dir=..", "x..y"] {
            assert_eq!(leaving_arg(passed, 1), None);
        }
        assert!(leaving_arg("..", 0).is_some());
        assert_eq!(leaving_arg("../../..", 3), None);
    }

    #[test]
    fn a_unified_diff_shows_each_change_with_the_lines_around_it() {
        let recorded = "a\nb\nc\nd\ne\nf\ng\nh\ni\nj\nk\nl";
        let observed = "a\nB\nc\nd\ne\nf\ng\nh\ni\nj\nk\nl\nm";

        assert_eq!(
            unified(recorded, observed),
            [
                "--- recorded",
                "+++ observed",
                "@@ -1,5 +1,5 @@",
                " a",
                "-b",
                "+B",
                " c",
                " d",
                " e",
                "@@ -10,3 +10,4 @@",
                " j",
                " k",
                " l",
                "+m",
            ]
        );
        // A line end that only one value has is a line of its own.
        assert_eq!(
            unified("a\n", "a"),
            ["--- recorded", "+++ observed", "@@ -1,2 +1,1 @@", " a", "-"]
        );
    }

    #[test]
    fn a_step_that_outran_its_deadline_is_a_difference_of_its_exit() {
        let failure = RunFailure::Timeout {
            step: 2,
            stdout: b"at /root/ws".to_vec(),
            stderr: Vec::new(),
        };

        let (found, streams) = unobserved(failure, &[], Path::new("/root"));

        assert_eq!(found.dimension, Dimension::Exit);
        assert_eq!(found.step, Some(2));
        assert_eq!(found.recorded, NOTHING);
        assert_eq!(found.observed, "step 2 outran its deadline and was killed");
        let streams = streams.expect("what the step had written");
        assert_eq!(
            (streams.step, streams.stdout),
            (2, b"at <ROOT>/ws".to_vec())
        );
    }

    #[test]
    fn a_step_that_is_not_started_is_malformed_at_its_key() {
        let unconfined = RunFailure::Unconfined {
            step: 2,
            cwd: PathBuf::from("/elsewhere"),
        };
        let refused = RunFailure::Refused {
            step: 2,
            key: ARGS_KEY,
            reason: "no".to_owned(),
        };
        for (failure, key) in [(unconfined, CWD_KEY), (refused, ARGS_KEY)] {
            let (found, streams) = unobserved(failure, &[], Path::new("/root"));

            assert_eq!(
                (found.dimension, found.step),
                (Dimension::Malformed, Some(2))
            );
            assert_eq!(found.subject, key);
            assert!(streams.is_none());
        }
    }
}
