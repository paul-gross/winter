//! `cargo dev record`: records scenarios with the reference tool, an executable this module is handed the
//! path of and knows nothing else about. It is the only code that is given that path.
//!
//! [`run`] is the command. It reads its arguments, takes the reference from `--reference` or from the
//! variable [`REFERENCE_VARIABLE`], asks [`refusals`] whether the run may start, and only then starts the
//! reference: once for its version line, and then through the record step for every scenario that was not
//! refused or skipped. What it reports goes to the writer it is given, one line per scenario.
//!
//! # What is refused before the reference is started
//!
//! [`refusals`] decides over explicit inputs, the reference path, the caller's `PATH` value, the scenarios
//! root, and the scenarios asked for, and starts nothing but git. It refuses the whole run when the
//! reference path is relative, missing, or no executable file; when it is the first `winter` on the caller's
//! `PATH`, the two compared by their canonical paths; when no directory above the executable holds `.git`;
//! when the canonical path of that checkout has a character outside `[A-Za-z0-9/._-]`; when the checkout's
//! `HEAD` is not the commit `reference.toml` of the scenarios root names; and when a tracked file of the
//! checkout is modified. It refuses one scenario when its description does not read as the model reads it,
//! a path that is not confined included, when it has a `[[stub]]` while no call is captured, when it has
//! a step that asks for what the runner refuses without a tree, a variable, a value, an argument that names
//! an absolute path, or an input, when its directory has an entry whose name starts with `record-`, which
//! only a run that did not end leaves there, and when it has `record/` and re-recording was not asked for.
//! A scenario that needs a facility the runner lacks is skipped, which fails nothing.
//!
//! What the runner can refuse only on the built tree, a working directory that resolves outside it and an
//! argument that leads out of it from there, is refused by the record step, after the version call.
//!
//! # The reference checkout is read, never written
//!
//! The checkout is inspected with three git commands, `rev-parse HEAD`,
//! `status --porcelain=v2 --untracked-files=no`, and `ls-files --others -z`, under a cleared environment
//! that holds [`GIT_ENVIRONMENT`], a `PATH` of the directory of git, `/usr/bin`, and `/bin`, and a `HOME`
//! that is a temporary directory. They run once inside [`refusals`], and once more after the last scenario
//! when the reference was started; a run that ends before its first start, the tree of the version call
//! not made included, reads the checkout once. The second pass is compared with the first: another `HEAD`, a tracked
//! file now modified, and a path added to or removed from the files git does not track each fail the run,
//! under one line `reference checkout changed` and the paths. A file that was untracked or ignored before
//! the run is no change, and a change to the content of such a file is not seen.
//!
//! # What the reference is given
//!
//! The reference is started by its path with every link resolved, whatever path named it, so that one
//! spelling of it reaches the reference and the record. Every start of it gets the pinned environment of an
//! isolated tree and three additions: the two interpreter variables that switch off bytecode files and fix
//! the hash seed, and a `PATH` that is the pinned one with the directory of that resolved executable after
//! the directory of git. The directory of git is the first on the caller's `PATH` that holds an executable
//! `git`. The canonical path of the checkout is handed to the record step as the one redaction, under the
//! token `<REFERENCE>`, and the version line is stored with it and the root of its tree replaced likewise.
//!
//! # After a record is written
//!
//! When the scenario directory lies in a git repository, every file of the new `record/` is checked against
//! it: a file an ignore rule matches, and a file whose path git would not take into an index, fail the
//! scenario, and its `record/` is removed. A record the run is to replace is moved aside before the record
//! step, into a directory `record-XXXXXX` of the scenario directory, and removed once its successor is
//! taken; when none is written, or the one written is refused, it is moved back by a rename. When it cannot
//! be moved back, `record/` is left absent and the report names where the record is kept; a refused record
//! that cannot be moved out of its place stays there, with the same note. A panic during the record step
//! removes neither the record set aside nor its directory: the record is moved back when `record/` is
//! absent and the rename succeeds, and otherwise stays whole in that directory. A run that is killed during
//! the record step leaves `record/` absent and the record whole in that directory. Whatever leaves the
//! directory there, the next run that names the scenario, and the next for every missing record, whether
//! `record/` is there or not, refuses the scenario and names it, and removes nothing. The check adds nothing
//! to the repository: the paths are taken into an index of their own in the temporary directory, without
//! their content.

use std::collections::BTreeSet;
use std::ffi::{OsStr, OsString};
use std::fmt;
use std::fs::File;
use std::io::Write;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};

use winter_workspace_test_support::scenario::compare::Dimension;
use winter_workspace_test_support::scenario::record::{RecordError, RecordOptions, record};
use winter_workspace_test_support::scenario::runner::{
    CALL_CAPTURE, RunFailure, SUPPORTED, refused,
};
use winter_workspace_test_support::scenario::{
    Facility, Malformed, Reference, Scenario, State, discover,
};
use winter_workspace_test_support::{Exit, FailureKind, TempTree, invoke};

/// Names the reference executable when `--reference` does not.
pub const REFERENCE_VARIABLE: &str = "WINTER_REFERENCE_BIN";

/// The interpreter variable that switches off bytecode files, and the value that does.
const BYTECODE_VARIABLE: (&str, &str) = ("PYTHONDONTWRITEBYTECODE", "1");

/// The interpreter variable that fixes the hash seed, and the value that switches the randomization off.
const HASH_SEED_VARIABLE: (&str, &str) = ("PYTHONHASHSEED", "0");

/// What every git command on the reference checkout is given, beside `PATH` and `HOME`.
pub const GIT_ENVIRONMENT: [(&str, &str); 4] = [
    ("GIT_CONFIG_GLOBAL", "/dev/null"),
    ("GIT_CONFIG_NOSYSTEM", "1"),
    ("GIT_OPTIONAL_LOCKS", "0"),
    ("LC_ALL", "C.UTF-8"),
];

/// Stands for the root of the tree in a value added to the environment of a step.
const ROOT_TOKEN: &str = "<ROOT>";

/// Stands for the reference checkout in everything a record stores.
const REFERENCE_TOKEN: &str = "<REFERENCE>";

/// The name of the installed command, which is never run as the reference.
const INSTALLED: &str = "winter";

/// The entry that makes a directory a git checkout.
const GIT_ENTRY: &str = ".git";

/// The directory of a scenario that holds its record.
const RECORD: &str = "record";

/// How the name of a directory starts that a run makes in a scenario directory and removes before it ends:
/// those of the record step, and the one a record is set aside in.
const UNENDED: &str = "record-";

/// The key of a description that declares a stubbed program.
const STUB_KEY: &str = "stub";

/// How many lines the report of a changed checkout has before the rest is counted.
const CHANGE_LINES: usize = 20;

const USAGE: &str = "usage: cargo dev record <scenario id>... [--reference <path>] [--rerecord] [--keep]\n       \
                     cargo dev record --all-missing [--reference <path>]";

/// What the arguments of the command ask for.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Request {
    /// The scenarios named, each `<command>/<name>`.
    pub ids: Vec<String>,
    /// Record every scenario that has no record, and none that has. One that has a record and what a run
    /// that did not end left beside it is refused, as it is when it is named.
    pub all_missing: bool,
    /// Replace the record of a named scenario that has one.
    pub rerecord: bool,
    /// Leave both trees of every record on disk.
    pub keep: bool,
    /// The reference executable, when the arguments name it.
    pub reference: Option<PathBuf>,
}

impl Request {
    /// Reads the arguments after `record`. The error says what is wrong with them.
    pub fn parse(args: &[OsString]) -> Result<Self, String> {
        let mut request = Self::default();
        let mut args = args.iter();
        while let Some(arg) = args.next() {
            match arg.to_str() {
                Some("--reference") => {
                    let path = args.next().ok_or("--reference takes a path")?;
                    request.reference = Some(PathBuf::from(path));
                }
                Some("--rerecord") => request.rerecord = true,
                Some("--keep") => request.keep = true,
                Some("--all-missing") => request.all_missing = true,
                Some(id) if !id.starts_with('-') => {
                    if request.ids.iter().any(|named| named == id) {
                        return Err(format!("the scenario {id} is named twice"));
                    }
                    request.ids.push(id.to_owned());
                }
                _ => return Err(format!("unknown argument {}", arg.display())),
            }
        }
        if request.all_missing {
            if !request.ids.is_empty() {
                return Err("--all-missing takes no scenario id".to_owned());
            }
            if request.rerecord || request.keep {
                return Err("--all-missing is combined with --reference alone".to_owned());
            }
        } else if request.ids.is_empty() {
            return Err("no scenario is named".to_owned());
        }
        Ok(request)
    }
}

/// Why a run does not start, whatever scenarios it names.
#[derive(Debug)]
pub enum Refusal {
    /// The reference path is relative.
    Relative(PathBuf),
    /// Nothing is at the reference path.
    Missing(PathBuf),
    /// The reference path names no executable file.
    NotExecutable(PathBuf),
    /// The reference is the first `winter` on the caller's `PATH`, found at `found`.
    Installed { reference: PathBuf, found: PathBuf },
    /// No directory above the executable holds `.git`.
    NoCheckout(PathBuf),
    /// The canonical path of the checkout has a character outside `[A-Za-z0-9/._-]`.
    CheckoutPath(PathBuf),
    /// The scenarios root, or its `reference.toml`, does not read.
    Scenarios { path: PathBuf, reason: String },
    /// The caller's `PATH` names no directory that holds an executable `git`.
    NoGit,
    /// A git command on the checkout did not run to a zero exit, or no home could be made for it.
    Git { command: String, reason: String },
    /// The checkout is at another commit than the pinned one.
    Head {
        checkout: PathBuf,
        head: String,
        pinned: String,
    },
    /// Tracked files of the checkout are modified.
    Modified {
        checkout: PathBuf,
        files: Vec<String>,
    },
}

impl fmt::Display for Refusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Relative(path) => {
                write!(f, "the reference path {} is relative", path.display())
            }
            Self::Missing(path) => {
                write!(f, "the reference path {} is missing", path.display())
            }
            Self::NotExecutable(path) => {
                write!(
                    f,
                    "the reference path {} is not an executable file",
                    path.display()
                )
            }
            Self::Installed { reference, found } => write!(
                f,
                "the reference {} is the first `{INSTALLED}` on PATH, {}: the installed command is never run",
                reference.display(),
                found.display()
            ),
            Self::NoCheckout(path) => write!(
                f,
                "no directory above the reference {} holds {GIT_ENTRY}: its checkout cannot be found",
                path.display()
            ),
            Self::CheckoutPath(path) => write!(
                f,
                "the path of the reference checkout {:?} has a character outside [A-Za-z0-9/._-]",
                path.display().to_string()
            ),
            Self::Scenarios { path, reason } => write!(f, "{}: {reason}", path.display()),
            Self::NoGit => f.write_str("PATH names no directory that holds an executable git"),
            Self::Git { command, reason } => write!(f, "`{command}` failed: {reason}"),
            Self::Head {
                checkout,
                head,
                pinned,
            } => write!(
                f,
                "the reference checkout {} is at {head}, and reference.toml names {pinned}",
                checkout.display()
            ),
            Self::Modified { checkout, files } => write!(
                f,
                "the reference checkout {} has modified tracked files: {}",
                checkout.display(),
                files.join(", ")
            ),
        }
    }
}

impl std::error::Error for Refusal {}

/// What is done with one scenario of a run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Verdict {
    /// It is recorded.
    Record,
    /// It is not recorded, and the run fails: the reason, which names the scenario.
    Refused(String),
    /// It needs these facilities, which the runner lacks. It is not recorded, and the run does not fail.
    Skipped(Vec<Facility>),
}

/// One scenario of a run.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Named {
    pub id: String,
    pub dir: PathBuf,
    pub verdict: Verdict,
}

/// A run that may start: the checkout of the reference as it was found, and what is done with each
/// scenario.
#[derive(Debug)]
pub struct Admission {
    /// The reference executable with every link resolved: the one path it is started by, and the one its
    /// directory and its checkout are taken from.
    pub executable: PathBuf,
    /// The canonical path of the reference checkout.
    pub checkout: PathBuf,
    /// The commit the checkout is at, which is the pinned one.
    pub commit: String,
    /// The scenarios of the run, in the order they are recorded.
    pub scenarios: Vec<Named>,
    /// What every start of the reference is given beside the pinned environment.
    additions: Vec<(String, String)>,
    git: Git,
    before: CheckoutState,
}

/// The git every command of a run uses, and the environment it is given.
#[derive(Debug)]
struct Git {
    /// The directory of git.
    directory: PathBuf,
    /// The `HOME` of every command.
    home: tempfile::TempDir,
}

impl Git {
    /// Git as the first directory of `path` that is absolute and holds an executable `git` has it.
    fn find(path: Option<&OsStr>) -> Result<Self, Refusal> {
        let directory = path
            .and_then(|path| {
                std::env::split_paths(path).find(|directory| {
                    directory.is_absolute() && is_executable(&directory.join("git"))
                })
            })
            .ok_or(Refusal::NoGit)?;
        let home = tempfile::Builder::new()
            .prefix("winter-workspace-record-")
            .tempdir()
            .map_err(|error| Refusal::Git {
                command: "git".to_owned(),
                reason: format!("no temporary directory for its HOME: {error}"),
            })?;
        Ok(Self { directory, home })
    }

    /// `git -C <directory> <args>`, under the cleared environment and `extra`, reading `input` when one is
    /// given. The error names the command.
    fn run(
        &self,
        directory: &Path,
        args: &[&str],
        extra: &[(&str, &Path)],
        input: Option<&Path>,
    ) -> Result<Output, (String, String)> {
        let named = format!("git -C {} {}", directory.display(), args.join(" "));
        let failed = |error: std::io::Error| (named.clone(), error.to_string());
        let mut search = self.directory.clone().into_os_string();
        search.push(":/usr/bin:/bin");
        let mut command = Command::new(self.directory.join("git"));
        command
            .arg("-C")
            .arg(directory)
            .args(args)
            .env_clear()
            .env("PATH", search)
            .env("HOME", self.home.path())
            .envs(GIT_ENVIRONMENT)
            .envs(extra.iter().copied())
            .stdin(match input {
                Some(input) => Stdio::from(File::open(input).map_err(failed)?),
                None => Stdio::null(),
            });
        command.output().map_err(failed)
    }

    /// As [`Git::run`], with a non-zero exit as an error that holds what git wrote to stderr.
    fn read(&self, directory: &Path, args: &[&str]) -> Result<Vec<u8>, (String, String)> {
        let output = self.run(directory, args, &[], None)?;
        if output.status.success() {
            return Ok(output.stdout);
        }
        let named = format!("git -C {} {}", directory.display(), args.join(" "));
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        Err((named, format!("{}: {stderr}", output.status)))
    }
}

/// What the three git commands say of the reference checkout.
#[derive(Clone, Debug, PartialEq, Eq)]
struct CheckoutState {
    head: String,
    /// The tracked files that are modified, staged or not.
    modified: Vec<String>,
    /// Every file git does not track, the ignored ones included.
    untracked: BTreeSet<Vec<u8>>,
}

impl CheckoutState {
    /// One pass over `checkout`: the three git commands, each once.
    fn read(git: &Git, checkout: &Path) -> Result<Self, (String, String)> {
        let head = git.read(checkout, &["rev-parse", "HEAD"])?;
        let status = git.read(
            checkout,
            &["status", "--porcelain=v2", "--untracked-files=no"],
        )?;
        let others = git.read(checkout, &["ls-files", "--others", "-z"])?;
        Ok(Self {
            head: String::from_utf8_lossy(&head).trim().to_owned(),
            modified: String::from_utf8_lossy(&status)
                .lines()
                .map(status_path)
                .collect(),
            untracked: others
                .split(|byte| *byte == 0)
                .filter(|path| !path.is_empty())
                .map(<[u8]>::to_vec)
                .collect(),
        })
    }

    /// What `after` holds that this state does not, as the lines of the report: `HEAD` when it moved, each
    /// added path with `+`, each removed one with `-`, and each tracked file that is modified with `M`.
    fn changes(&self, after: &Self) -> Vec<String> {
        let shown = |path: &Vec<u8>| String::from_utf8_lossy(path).into_owned();
        let mut lines = Vec::new();
        if self.head != after.head {
            lines.push(format!("HEAD {} -> {}", self.head, after.head));
        }
        let added = after.untracked.difference(&self.untracked);
        lines.extend(added.map(|path| format!("+ {}", shown(path))));
        let removed = self.untracked.difference(&after.untracked);
        lines.extend(removed.map(|path| format!("- {}", shown(path))));
        let modified = after.modified.iter();
        lines.extend(
            modified
                .filter(|path| !self.modified.contains(path))
                .map(|path| format!("M {path}")),
        );
        lines
    }
}

/// The path of one line of `git status --porcelain=v2`: what follows the fields of its kind, without the
/// original path of a rename. A line of a kind this does not know is answered whole.
fn status_path(line: &str) -> String {
    let fields = match line.as_bytes().first() {
        Some(b'1') => 9,
        Some(b'2') => 10,
        Some(b'u') => 11,
        _ => return line.to_owned(),
    };
    let path = line.splitn(fields, ' ').last().unwrap_or(line);
    path.split('\t').next().unwrap_or(path).to_owned()
}

/// Whether `path`, every link followed, is a file some permission lets execute.
fn is_executable(path: &Path) -> bool {
    std::fs::metadata(path)
        .is_ok_and(|metadata| metadata.is_file() && metadata.permissions().mode() & 0o111 != 0)
}

/// Decides whether a run may start, and what it does with each scenario, starting nothing but git.
///
/// `reference` is the reference executable as it was named, `path` the value of the caller's `PATH`,
/// `scenarios_root` the directory that holds `reference.toml` and the scenarios, and `request` says which
/// scenarios are asked for and whether a record may be replaced. The module lists what is refused.
///
/// The three git commands on the checkout have each run once when this answers an [`Admission`], a
/// [`Refusal::Head`], a [`Refusal::Modified`], or the [`Refusal::Scenarios`] of scenarios that could not be
/// listed for a request for every missing record. A [`Refusal::Git`] that names one of the three commands
/// comes from within that pass: the commands before the one it names have run, and none after it. A
/// [`Refusal::Git`] that names no command on the checkout, no temporary `HOME` having been made, precedes
/// the pass, as every other refusal does, with no git command run on the checkout.
pub fn refusals(
    reference: &Path,
    path: Option<&OsStr>,
    scenarios_root: &Path,
    request: &Request,
) -> Result<Admission, Refusal> {
    if !reference.is_absolute() {
        return Err(Refusal::Relative(reference.to_path_buf()));
    }
    if reference.symlink_metadata().is_err() {
        return Err(Refusal::Missing(reference.to_path_buf()));
    }
    if !is_executable(reference) {
        return Err(Refusal::NotExecutable(reference.to_path_buf()));
    }
    let canonical = reference
        .canonicalize()
        .map_err(|_| Refusal::Missing(reference.to_path_buf()))?;
    let installed = path.and_then(|path| {
        std::env::split_paths(path)
            .map(|directory| directory.join(INSTALLED))
            .find(|candidate| is_executable(candidate))
    });
    if let Some(found) = installed
        && found.canonicalize().is_ok_and(|found| found == canonical)
    {
        return Err(Refusal::Installed {
            reference: reference.to_path_buf(),
            found,
        });
    }
    let checkout = canonical
        .ancestors()
        .skip(1)
        .find(|directory| directory.join(GIT_ENTRY).symlink_metadata().is_ok())
        .ok_or_else(|| Refusal::NoCheckout(reference.to_path_buf()))?
        .to_path_buf();
    let within = |byte: &u8| byte.is_ascii_alphanumeric() || b"/._-".contains(byte);
    if !checkout.as_os_str().as_bytes().iter().all(within) {
        return Err(Refusal::CheckoutPath(checkout));
    }
    let pinned = Reference::load(scenarios_root)
        .map_err(|error| Refusal::Scenarios {
            path: scenarios_root.join("reference.toml"),
            reason: error.to_string(),
        })?
        .commit;

    let git = Git::find(path)?;
    let before = CheckoutState::read(&git, &checkout)
        .map_err(|(command, reason)| Refusal::Git { command, reason })?;
    if before.head != pinned {
        return Err(Refusal::Head {
            checkout,
            head: before.head,
            pinned,
        });
    }
    if !before.modified.is_empty() {
        return Err(Refusal::Modified {
            checkout,
            files: before.modified,
        });
    }

    let additions = additions(&canonical, &git.directory);
    let scenarios = if request.all_missing {
        let found = discover(scenarios_root).map_err(|error| Refusal::Scenarios {
            path: scenarios_root.to_path_buf(),
            reason: error.to_string(),
        })?;
        // A scenario with a record is passed over unless a run that did not end left something beside it.
        let missing = found
            .into_iter()
            .filter(|entry| entry.state != State::Recorded || left_by_a_run(&entry.dir).is_some());
        missing
            .map(|entry| Named {
                verdict: match &entry.state {
                    State::Malformed(malformed) => Verdict::Refused(malformed.to_string()),
                    _ => verdict(&entry.id, &entry.dir, false, &additions),
                },
                id: entry.id,
                dir: entry.dir,
            })
            .collect()
    } else {
        let named = request.ids.iter().map(|id| {
            let dir = scenarios_root.join(id);
            let verdict = match id.split('/').collect::<Vec<_>>()[..] {
                [command, name] if !command.is_empty() && !name.is_empty() => {
                    verdict(id, &dir, request.rerecord, &additions)
                }
                _ => Verdict::Refused(format!("{id} is no scenario id: <command>/<name>")),
            };
            Named {
                id: id.clone(),
                dir,
                verdict,
            }
        });
        named.collect()
    };
    Ok(Admission {
        executable: canonical,
        checkout,
        commit: pinned,
        scenarios,
        additions,
        git,
        before,
    })
}

/// What is done with the scenario `id` of the directory `dir`, read without a tree and without a start of
/// anything. `additions` is what the reference would be given beside the pinned environment.
fn verdict(id: &str, dir: &Path, rerecord: bool, additions: &[(String, String)]) -> Verdict {
    let scenario = match Scenario::load(dir) {
        Ok(scenario) => scenario,
        Err(malformed) => return Verdict::Refused(malformed.to_string()),
    };
    if !CALL_CAPTURE && !scenario.stubs.is_empty() {
        let malformed = Malformed {
            scenario: scenario.id,
            key: STUB_KEY.to_owned(),
            reason: "the calls of a stubbed program are not captured".to_owned(),
        };
        return Verdict::Refused(malformed.to_string());
    }
    match refused(&scenario, additions) {
        None => {}
        Some(RunFailure::Refused { step, key, reason }) => {
            let malformed = Malformed {
                scenario: scenario.id,
                key: key.to_owned(),
                reason: format!("step {step}: {reason}"),
            };
            return Verdict::Refused(malformed.to_string());
        }
        Some(failure) => return Verdict::Refused(format!("the scenario {id}: {failure}")),
    }
    if let Some(left) = left_by_a_run(dir) {
        return Verdict::Refused(format!(
            "the scenario {id} has {left}/, which a run that did not end left there: the record that \
             run set aside, when there was one, is {left}/{RECORD}"
        ));
    }
    let missing: Vec<Facility> = scenario
        .needs
        .iter()
        .copied()
        .filter(|facility| !SUPPORTED.contains(facility))
        .collect();
    if !missing.is_empty() {
        return Verdict::Skipped(missing);
    }
    if dir.join(RECORD).symlink_metadata().is_ok() && !rerecord {
        return Verdict::Refused(format!(
            "the scenario {id} has {RECORD}/, and --rerecord was not given"
        ));
    }
    Verdict::Record
}

/// The name of the first entry of the scenario directory `dir`, in the order of the names, that starts with
/// [`UNENDED`]: what a run that did not end left there.
fn left_by_a_run(dir: &Path) -> Option<String> {
    let entries = std::fs::read_dir(dir).ok()?;
    let names = entries.filter_map(|entry| entry.ok()?.file_name().into_string().ok());
    names.filter(|name| name.starts_with(UNENDED)).min()
}

/// The names of `facilities`, joined.
fn names(facilities: &[Facility]) -> String {
    let names: Vec<&str> = facilities.iter().map(|facility| facility.name()).collect();
    names.join(", ")
}

/// Runs `cargo dev record` with the arguments `args`, those after `record`, and answers the exit code: 0
/// when every scenario asked for was recorded or skipped and the reference checkout is unchanged, 1 when
/// the run or a scenario was refused, a record was unstable, or the checkout changed, 2 for arguments that
/// do not read and for a run that names no reference.
///
/// `env` looks up one variable of the caller's environment, and is asked for [`REFERENCE_VARIABLE`] and
/// `PATH`. `scenarios_root` is the directory of the scenarios. What the command reports is written to `err`.
pub fn run(
    args: &[OsString],
    env: &dyn Fn(&str) -> Option<OsString>,
    scenarios_root: &Path,
    err: &mut dyn Write,
) -> u8 {
    let request = match Request::parse(args) {
        Ok(request) => request,
        Err(reason) => {
            let _ = writeln!(err, "record: {reason}\n{USAGE}");
            return 2;
        }
    };
    let named = request.reference.clone();
    let Some(reference) = named.or_else(|| env(REFERENCE_VARIABLE).map(PathBuf::from)) else {
        let _ = writeln!(
            err,
            "record: no reference executable: pass --reference <path> or set {REFERENCE_VARIABLE}"
        );
        return 2;
    };
    let path = env("PATH");
    let admission = match refusals(&reference, path.as_deref(), scenarios_root, &request) {
        Ok(admission) => admission,
        Err(refusal) => {
            let _ = writeln!(err, "record: refused: {refusal}");
            return 1;
        }
    };

    let mut failed = false;
    let mut admitted = Vec::new();
    for scenario in &admission.scenarios {
        match &scenario.verdict {
            Verdict::Record => admitted.push(scenario),
            Verdict::Refused(reason) => {
                failed = true;
                let _ = writeln!(err, "refused {}: {reason}", scenario.id);
            }
            Verdict::Skipped(facilities) => {
                let _ = writeln!(
                    err,
                    "skipped {}: it needs {}, which the runner lacks",
                    scenario.id,
                    names(facilities)
                );
            }
        }
    }
    if admitted.is_empty() {
        return u8::from(failed);
    }

    // The checkout is read a second time once the reference was started, whatever happens after that, and
    // not when the run ends before its first start.
    match runtime(&admission) {
        Err(NoRuntime { started, reason }) => {
            let _ = writeln!(err, "record: refused: {reason}");
            if !started {
                return 1;
            }
            failed = true;
        }
        Ok(runtime) => {
            let options = RecordOptions {
                executable: admission.executable.clone(),
                extra_env: admission.additions.clone(),
                redactions: vec![(admission.checkout.clone(), REFERENCE_TOKEN.to_owned())],
                reference_commit: admission.commit.clone(),
                reference_runtime: runtime,
                rerecord: request.rerecord,
                keep: request.keep,
            };
            for scenario in admitted {
                match record_one(scenario, &options, &admission.git) {
                    Ok(Recorded::Written) => {
                        let _ = writeln!(err, "recorded {}", scenario.id);
                    }
                    Ok(Recorded::Skipped(facilities)) => {
                        let _ = writeln!(
                            err,
                            "skipped {}: it needs {}, which the runner lacks",
                            scenario.id,
                            names(&facilities)
                        );
                    }
                    Err(reason) => {
                        failed = true;
                        let _ = writeln!(err, "refused {}: {reason}", scenario.id);
                    }
                }
            }
        }
    }

    match CheckoutState::read(&admission.git, &admission.checkout) {
        Err((command, reason)) => {
            failed = true;
            let _ = writeln!(
                err,
                "record: the reference checkout was not read after the run: `{command}` failed: {reason}"
            );
        }
        Ok(after) => {
            let changes = admission.before.changes(&after);
            if !changes.is_empty() {
                failed = true;
                let _ = writeln!(err, "reference checkout changed");
                for line in changes.iter().take(CHANGE_LINES) {
                    let _ = writeln!(err, "{line}");
                }
                if let Some(rest) = changes.len().checked_sub(CHANGE_LINES)
                    && rest > 0
                {
                    let _ = writeln!(err, "and {rest} more");
                }
            }
        }
    }
    u8::from(failed)
}

/// What the reference is given beside the pinned environment: the two interpreter variables, and the pinned
/// `PATH` with the directory of `reference` after `git`, the directory of git.
fn additions(reference: &Path, git: &Path) -> Vec<(String, String)> {
    let directory = reference.parent().unwrap_or(Path::new("/"));
    let search = format!(
        "{ROOT_TOKEN}/bin:{}:{}:/usr/bin:/bin",
        git.display(),
        directory.display()
    );
    let pair = |(name, value): (&str, &str)| (name.to_owned(), value.to_owned());
    vec![
        pair(BYTECODE_VARIABLE),
        pair(HASH_SEED_VARIABLE),
        ("PATH".to_owned(), search),
    ]
}

/// Why a run has no version line of the reference.
struct NoRuntime {
    /// Whether the reference was started on the way.
    started: bool,
    reason: String,
}

/// The first line the reference of `admission` writes to stdout for `--version`, run once in the workspace
/// directory of a tree of its own under the pinned environment and the additions. The line is given as a
/// record stores output: the root of that tree as `<ROOT>`, and then the path of the checkout as
/// `<REFERENCE>`. The error says why there is none, and whether the reference was started.
fn runtime(admission: &Admission) -> Result<String, NoRuntime> {
    let reference = &admission.executable;
    let ended = |started: bool| move |reason: String| NoRuntime { started, reason };
    let tree = TempTree::new()
        .map_err(|error| format!("no isolated tree for the reference could be made: {error}"))
        .map_err(ended(false))?;
    let root = tree.root().to_string_lossy().into_owned();
    let mut invocation = tree.invocation(reference);
    invocation.args = vec![OsString::from("--version")];
    for (name, value) in &admission.additions {
        invocation.env.set(name, &value.replace(ROOT_TOKEN, &root));
    }
    let asked = format!("{} --version", reference.display());
    let observation = invoke(invocation).map_err(|failure| {
        let started = !matches!(
            failure.kind,
            FailureKind::InvalidInvocation | FailureKind::Spawn(_)
        );
        ended(started)(format!("`{asked}` did not run: {failure}"))
    })?;
    if observation.exit != Exit::Code(0) {
        let reason = format!("`{asked}` ended with {:?}", observation.exit);
        return Err(ended(true)(reason));
    }
    let stdout = String::from_utf8_lossy(&observation.stdout);
    match stdout.lines().next().map(str::trim_end) {
        Some(line) if !line.is_empty() => Ok(line
            .replace(&root, ROOT_TOKEN)
            .replace(&*admission.checkout.to_string_lossy(), REFERENCE_TOKEN)),
        _ => Err(ended(true)(format!("`{asked}` printed no version line"))),
    }
}

/// How the record step ended for a scenario it did not fail.
enum Recorded {
    Written,
    Skipped(Vec<Facility>),
}

/// The name a refused record has in the directory that holds the record set aside.
const REFUSED: &str = "refused";

/// A record set aside while its successor is made: `record` of a directory `record-XXXXXX` beside its
/// place, the name the record step gives its own temporary directories there, which discovery and replay
/// pass over. It got there by a rename, and leaves by one.
///
/// Nothing removes that directory but [`Aside::discard`] and a put-back that moved the record back. Dropped
/// while the thread unwinds from a panic, this moves the record back when nothing is at its place, and then
/// removes the directory only when it is empty; when something is at its place, or the record cannot be
/// moved, the directory is left as it is.
struct Aside {
    /// The directory `record-XXXXXX`.
    directory: PathBuf,
    /// Where the record was, and goes back to.
    place: PathBuf,
}

impl Aside {
    /// Moves the record at `place`, in the scenario directory `dir`, aside. An error leaves it in its place.
    fn take(dir: &Path, place: &Path) -> std::io::Result<Self> {
        let directory = tempfile::Builder::new()
            .prefix(UNENDED)
            .tempdir_in(dir)?
            .keep();
        if let Err(error) = std::fs::rename(place, directory.join(RECORD)) {
            // It holds nothing, and what is not empty is not removed by this.
            let _ = std::fs::remove_dir(&directory);
            return Err(error);
        }
        Ok(Self {
            directory,
            place: place.to_path_buf(),
        })
    }

    /// Removes the record set aside and its directory, once its successor stands. The error names what is
    /// left.
    fn discard(self) -> Result<(), String> {
        std::fs::remove_dir_all(&self.directory).map_err(|error| {
            format!(
                "the new record is in its place, and the one before it could not be removed from {}: {error}",
                self.directory.display()
            )
        })
    }

    /// Moves the record set aside back to its place and removes the directory it was in. When `occupied`,
    /// its place holds a refused record, which is first moved into that directory and so removed with it.
    ///
    /// An error is the rest of the report. Whenever the record set aside is not back, its directory is
    /// kept and the error names where the record is: its place is then empty, or, when the refused record
    /// could not be moved out of it, holds that record whole.
    fn put_back(self, occupied: bool) -> Result<(), String> {
        let previous = self.directory.join(RECORD);
        let refused = self.directory.join(REFUSED);
        if occupied && let Err(error) = std::fs::rename(&self.place, &refused) {
            return Err(format!(
                "the record that was refused could not be moved out of its place: {error}; the one before \
                 it is kept at {}",
                previous.display()
            ));
        }
        if let Err(error) = std::fs::rename(&previous, &self.place) {
            let left = match occupied.then(|| std::fs::remove_dir_all(&refused)) {
                Some(Err(error)) => {
                    format!(
                        "; the one that was refused is left at {}: {error}",
                        refused.display()
                    )
                }
                _ => String::new(),
            };
            return Err(format!(
                "the one before it could not be put back: {error}; it is kept at {}{left}",
                previous.display()
            ));
        }
        std::fs::remove_dir_all(&self.directory).map_err(|error| {
            format!(
                "the one before it is back in its place, and {} could not be removed: {error}",
                self.directory.display()
            )
        })
    }
}

impl Drop for Aside {
    fn drop(&mut self) {
        if !std::thread::panicking() || self.place.symlink_metadata().is_ok() {
            return;
        }
        if std::fs::rename(self.directory.join(RECORD), &self.place).is_ok() {
            let _ = std::fs::remove_dir(&self.directory);
        }
    }
}

/// Why a scenario has no new record that stands.
struct Unrecorded {
    reason: String,
    /// Whether a record was written and is in its place, refused.
    written: bool,
}

/// Records `scenario` and checks the record that was written against the repository it lies in. The error
/// is the reason the scenario is reported as refused.
///
/// A record the run is to replace is moved aside before the record step, by a rename inside the scenario
/// directory, and removed only once its successor is written and the repository takes it. When the step
/// writes none, or the check refuses the one it wrote, the refused record is moved out of its place and the
/// one set aside is moved back, each by a rename, so `record/` is the record that was there, whole, or,
/// when it cannot be moved back, absent, and the error names where it is kept. Only a refused record that
/// cannot be moved out of its place stays there, whole, with the same note. A record that replaces none
/// and is refused is removed, and the error says so when it could not be.
///
/// A panic that unwinds through this while a record is set aside removes neither that record nor its
/// directory: the record is moved back when `record/` is absent and the rename succeeds, and is otherwise
/// left whole in its directory, as it is beside a record the step wrote, which was then not checked against
/// the repository. A process killed during the record step leaves `record/` absent and the record that was
/// there whole in that directory. The next run refuses a scenario with such a directory and names it.
fn record_one(scenario: &Named, options: &RecordOptions, git: &Git) -> Result<Recorded, String> {
    let place = scenario.dir.join(RECORD);
    let aside = if options.rerecord && place.symlink_metadata().is_ok() {
        Some(Aside::take(&scenario.dir, &place).map_err(|error| {
            format!("the record that is there could not be set aside, and nothing was run: {error}")
        })?)
    } else {
        None
    };
    match (written(scenario, options, git), aside) {
        (Ok(Recorded::Written), Some(aside)) => aside.discard().map(|()| Recorded::Written),
        (Ok(Recorded::Skipped(facilities)), Some(aside)) => match aside.put_back(false) {
            Ok(()) => Ok(Recorded::Skipped(facilities)),
            Err(rest) => Err(format!(
                "it needs {}, which the runner lacks; {rest}",
                names(&facilities)
            )),
        },
        (Ok(recorded), None) => Ok(recorded),
        (Err(unrecorded), Some(aside)) => {
            let reason = unrecorded.reason;
            Err(
                match (aside.put_back(unrecorded.written), unrecorded.written) {
                    (Ok(()), false) => reason,
                    (Ok(()), true) => format!(
                        "{reason}; the record was removed, and the one before it is back in its place"
                    ),
                    (Err(rest), _) => format!("{reason}; {rest}"),
                },
            )
        }
        (Err(unrecorded), None) if unrecorded.written => {
            let reason = unrecorded.reason;
            Err(match std::fs::remove_dir_all(&place) {
                Ok(()) => format!("{reason}; the record was removed"),
                Err(error) => format!("{reason}; the record could not be removed: {error}"),
            })
        }
        (Err(unrecorded), None) => Err(unrecorded.reason),
    }
}

/// The record step for `scenario`, and the check of what it wrote against the repository. A scenario that
/// has a record in its place when this is called is not recorded.
fn written(scenario: &Named, options: &RecordOptions, git: &Git) -> Result<Recorded, Unrecorded> {
    let unwritten = |reason: String| Unrecorded {
        reason,
        written: false,
    };
    match record(&scenario.dir, options) {
        Ok(()) => {}
        Err(RecordError::Deferred(facilities)) => return Ok(Recorded::Skipped(facilities)),
        // A comparison rule that does not read, or relaxes nothing, is a fault of the description: the
        // comparison answers it as a difference without a step, and the record step passes that on.
        Err(RecordError::Unstable(difference))
            if difference.dimension == Dimension::Malformed
                || (difference.step.is_none()
                    && matches!(difference.dimension, Dimension::Json | Dimension::Files)) =>
        {
            return Err(unwritten(format!(
                "the scenario {} is malformed at the comparison rule `{}`: {}",
                scenario.id, difference.subject, difference.recorded
            )));
        }
        Err(error) => return Err(unwritten(error.to_string())),
    }
    let reason = match unaccepted(git, &scenario.dir) {
        Ok(None) => return Ok(Recorded::Written),
        Ok(Some((path, why))) => {
            format!("the repository of the scenarios would not take {path}: {why}")
        }
        Err((command, reason)) => format!(
            "the record could not be checked against the repository of the scenarios: `{command}` failed: \
             {reason}"
        ),
    };
    Err(Unrecorded {
        reason,
        written: true,
    })
}

/// The first file of `record/` of the scenario directory `dir` that the git repository `dir` lies in would
/// not accept, by its path from `dir`, with the reason; `None` when it accepts every one, and when `dir`
/// lies in no repository.
fn unaccepted(git: &Git, dir: &Path) -> Result<Option<(String, &'static str)>, (String, String)> {
    let inside = git.run(dir, &["rev-parse", "--show-toplevel"], &[], None)?;
    if !inside.status.success() {
        return Ok(None);
    }
    let unread = |path: &Path| {
        let path = path.display().to_string();
        move |error: std::io::Error| (path, error.to_string())
    };
    let mut files = Vec::new();
    let mut pending = vec![PathBuf::from(RECORD)];
    while let Some(directory) = pending.pop() {
        let absolute = dir.join(&directory);
        for entry in std::fs::read_dir(&absolute).map_err(unread(&absolute))? {
            let entry = entry.map_err(unread(&absolute))?;
            let path = directory.join(entry.file_name());
            if entry.file_type().map_err(unread(&absolute))?.is_dir() {
                pending.push(path);
            } else {
                files.push(path.into_os_string().into_vec());
            }
        }
    }
    files.sort();
    let list = git.home.path().join("paths");
    let index = git.home.path().join("index");
    let mut listed = Vec::new();
    for file in &files {
        listed.extend_from_slice(file);
        listed.push(0);
    }
    std::fs::write(&list, listed).map_err(unread(&list))?;
    let _ = std::fs::remove_file(&index);
    let paths = |output: &[u8]| -> BTreeSet<Vec<u8>> {
        let paths = output.split(|byte| *byte == 0);
        paths
            .filter(|path| !path.is_empty())
            .map(<[u8]>::to_vec)
            .collect()
    };
    let shown = |path: &[u8]| String::from_utf8_lossy(path).into_owned();
    let failed = |args: &[&str], output: &Output| {
        let named = format!("git -C {} {}", dir.display(), args.join(" "));
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        (named, format!("{}: {stderr}", output.status))
    };

    // Exit 0 lists the paths an ignore rule matches, exit 1 says none is matched.
    let ignore = ["check-ignore", "-z", "--stdin"];
    let ignored = git.run(dir, &ignore, &[], Some(&list))?;
    match ignored.status.code() {
        Some(0) => {
            if let Some(path) = paths(&ignored.stdout).first() {
                return Ok(Some((shown(path), "an ignore rule matches it")));
            }
        }
        Some(1) => {}
        _ => return Err(failed(&ignore, &ignored)),
    }

    // The paths are taken into an index of their own, without their content: git leaves out, with a zero
    // exit, each path it would refuse in the index of the repository.
    let own_index = [("GIT_INDEX_FILE", index.as_path())];
    let add = ["update-index", "--add", "--info-only", "-z", "--stdin"];
    let added = git.run(dir, &add, &own_index, Some(&list))?;
    if !added.status.success() {
        return Err(failed(&add, &added));
    }
    let list_index = ["ls-files", "-z"];
    let taken = git.run(dir, &list_index, &own_index, None)?;
    if !taken.status.success() {
        return Err(failed(&list_index, &taken));
    }
    let taken = paths(&taken.stdout);
    let refused = files.iter().find(|file| !taken.contains(*file));
    Ok(refused.map(|path| (shown(path), "git takes no such path into an index")))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(words: &[&str]) -> Vec<OsString> {
        words.iter().map(OsString::from).collect()
    }

    #[test]
    fn the_arguments_name_scenarios_or_every_missing_one() {
        let request = Request::parse(&args(&[
            "ws-index/basic",
            "--reference",
            "/r/winter",
            "--rerecord",
            "doctor/clean",
            "--keep",
        ]))
        .unwrap();

        assert_eq!(request.ids, ["ws-index/basic", "doctor/clean"]);
        assert_eq!(request.reference, Some(PathBuf::from("/r/winter")));
        assert!(request.rerecord && request.keep && !request.all_missing);

        let request = Request::parse(&args(&["--all-missing"])).unwrap();
        assert!(request.all_missing && request.ids.is_empty());
    }

    #[test]
    fn arguments_that_do_not_read_are_refused() {
        for unread in [
            &[][..],
            &["--rerecord"],
            &["--reference"],
            &["a/b", "--reference"],
            &["a/b", "--unknown"],
            &["--all-missing", "a/b"],
            &["--all-missing", "--rerecord"],
            &["--all-missing", "--keep"],
            &["a/b", "a/b"],
            &["a/b", "c/d", "--rerecord", "a/b"],
        ] {
            assert!(Request::parse(&args(unread)).is_err(), "{unread:?}");
        }
    }

    #[test]
    fn the_path_of_a_status_line_is_what_follows_the_fields_of_its_kind() {
        let ordinary = "1 .M N... 100644 100644 100644 1111111 2222222 a dir/tracked file.txt";
        let renamed = "2 R. N... 100644 100644 100644 1111111 2222222 R100 new name\told name";
        let unmerged = "u UU N... 100644 100644 100644 100644 1111111 2222222 3333333 both.txt";

        assert_eq!(status_path(ordinary), "a dir/tracked file.txt");
        assert_eq!(status_path(renamed), "new name");
        assert_eq!(status_path(unmerged), "both.txt");
        assert_eq!(status_path("? other"), "? other");
    }

    fn state(head: &str, modified: &[&str], untracked: &[&str]) -> CheckoutState {
        CheckoutState {
            head: head.to_owned(),
            modified: modified.iter().map(|path| (*path).to_owned()).collect(),
            untracked: untracked
                .iter()
                .map(|path| path.as_bytes().to_vec())
                .collect(),
        }
    }

    #[test]
    fn a_checkout_that_holds_the_same_untracked_files_is_unchanged() {
        let before = state("a", &[], &["cache/x", "notes"]);

        assert_eq!(before.changes(&before.clone()), [] as [&str; 0]);
    }

    #[test]
    fn a_moved_head_and_added_removed_and_modified_paths_are_each_a_change() {
        let before = state("a", &[], &["cache/x", "notes"]);
        let after = state("b", &["src/lib"], &["cache/x", "cache/y"]);

        assert_eq!(
            before.changes(&after),
            ["HEAD a -> b", "+ cache/y", "- notes", "M src/lib"]
        );
    }
}
