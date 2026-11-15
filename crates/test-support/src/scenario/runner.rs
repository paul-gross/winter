//! The steps of one scenario, run with one executable in the tree its fixture was built in, and what each
//! step did, in the form the comparison reads.
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

use std::fmt;
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use super::compare::StepRecord;
use super::fixture::{self, ROOT_TOKEN};
use super::model::{self, Facility, Scenario, Step};
use super::normalize;
use super::snapshot::{self, Snapshot, SnapshotError};
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
}
