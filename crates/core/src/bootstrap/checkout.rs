//! One repository's checkout during reconciliation: its steps, in order.
//!
//! The steps are location, identity, excludes, `cmd`, and pin. Each runs only after the one before it
//! succeeded, and the first failure is the answer.

use std::ffi::{OsStr, OsString};
use std::fmt;
use std::io;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};

use crate::bootstrap::{Action, InitEvent};
use crate::config_lock::RefKind;
use crate::config_model::{GitIdentity, ProjectRepository, StandaloneRepository};
use crate::files::ReadFiles;
use crate::git::{self, GitFailure};
use crate::pins::PinError;
use crate::pins::materialize::materialize;
use crate::pins::outcome::rendered;
use crate::ports::Ports;
use crate::process::ProcessEnv;
use crate::programs::{Call, Exit, Program, Stdin};
use crate::termination::{RepoError, RepoMessage, UnsafeReason};
use crate::workspace_files::{PROJECTS_DIR, standalone_location};

/// One repository to reconcile.
pub struct Checkout<'r> {
    pub name: &'r str,
    pub url: Option<&'r str>,
    pub location: PathBuf,
    /// The workspace's `git_excludes`, then the repository's.
    pub git_excludes: Vec<String>,
    pub cmd: &'r [String],
    /// Whether this is a project with `nested` declared.
    pub nested: bool,
    /// A project's `main_branch`, which the clone checks out.
    pub branch: Option<&'r str>,
    /// `Some` for a standalone, which alone has a pin step.
    pub standalone: Option<&'r StandaloneRepository>,
}

/// What the workspace declares that bears on one repository's steps.
pub struct Declared<'r> {
    /// The workspace root, absolute.
    pub root: &'r Path,
    /// The identity written to every checkout.
    pub identity: Option<&'r GitIdentity>,
    pub projects: &'r [ProjectRepository],
    pub standalones: &'r [StandaloneRepository],
}

/// A step of a repository's reconciliation, in the order the steps run.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Step {
    Location,
    Identity,
    Excludes,
    Cmd,
    Pin,
}

impl Step {
    const ALL: [Self; 5] = [
        Self::Location,
        Self::Identity,
        Self::Excludes,
        Self::Cmd,
        Self::Pin,
    ];

    fn name(self) -> &'static str {
        match self {
            Self::Location => "location",
            Self::Identity => "identity",
            Self::Excludes => "excludes",
            Self::Cmd => "cmd",
            Self::Pin => "pin",
        }
    }
}

/// Why a step failed.
#[derive(Debug)]
pub enum Cause {
    Git(GitFailure),
    Repo(Box<RepoError>),
    Io(io::Error),
    Pin(PinError),
}

impl fmt::Display for Cause {
    /// A repo error reads as [`rendered`] gives it, wherever it is carried; any other error as its own text.
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Git(GitFailure::Repo(error))
            | Self::Repo(error)
            | Self::Pin(PinError::Repo(error)) => f.write_str(&rendered(error)),
            Self::Git(GitFailure::Internal(error)) => error.fmt(f),
            Self::Io(error) => error.fmt(f),
            Self::Pin(error) => error.fmt(f),
        }
    }
}

impl From<GitFailure> for Cause {
    fn from(failure: GitFailure) -> Self {
        Self::Git(failure)
    }
}

impl From<io::Error> for Cause {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

/// A repository's first failure.
#[derive(Debug, thiserror::Error)]
#[error("{cause} {}", outcome(*.step, *.pins))]
pub struct CheckoutError {
    pub step: Step,
    pub cause: Cause,
    /// Whether the repository has a pin step: a standalone does, a project does not.
    pins: bool,
}

/// The end of a failure's text: the step that failed and the steps that were therefore not attempted.
fn outcome(failed: Step, pins: bool) -> String {
    let after: Vec<&str> = Step::ALL
        .into_iter()
        .skip_while(|step| *step != failed)
        .skip(1)
        .filter(|step| pins || *step != Step::Pin)
        .map(Step::name)
        .collect();
    if after.is_empty() {
        format!("(failed at {})", failed.name())
    } else {
        format!(
            "(failed at {}; not attempted: {})",
            failed.name(),
            after.join(", ")
        )
    }
}

/// Reconciles one repository: makes its checkout exist and verifies it, writes the git identity, appends its
/// excludes, runs its commands, and, for a standalone, materializes its pin.
///
/// What was done is handed to `emit` as it happens. The first failure ends the call and is the answer; no
/// [`InitEvent::RepoError`] is emitted for it.
pub fn run(
    ports: &Ports<'_>,
    env: &ProcessEnv,
    declared: &Declared<'_>,
    c: &Checkout<'_>,
    emit: &mut dyn FnMut(InitEvent),
) -> Result<(), Box<CheckoutError>> {
    let at = |step: Step| {
        move |cause: Cause| {
            Box::new(CheckoutError {
                step,
                cause,
                pins: c.standalone.is_some(),
            })
        }
    };
    location(ports, declared, c, emit).map_err(at(Step::Location))?;
    identity(ports, declared, c).map_err(at(Step::Identity))?;
    excludes(ports, c, emit).map_err(at(Step::Excludes))?;
    commands(ports, env, c, emit).map_err(at(Step::Cmd))?;
    pin(ports, declared, c, emit).map_err(at(Step::Pin))
}

fn action(c: &Checkout<'_>, action: Action, detail: String) -> InitEvent {
    InitEvent::RepoAction {
        repo: c.name.to_owned(),
        location: c.location.display().to_string(),
        action,
        detail,
    }
}

/// A repo error that no program call stands behind.
fn refused(message: RepoMessage) -> Cause {
    Cause::Repo(Box::new(RepoError {
        message,
        program: None,
        subcommand: None,
        args: Vec::new(),
        cwd: None,
        exit: None,
        stderr: None,
    }))
}

/// Clones an absent location, or verifies that a present one is the checkout the repository declares.
fn location(
    ports: &Ports<'_>,
    declared: &Declared<'_>,
    c: &Checkout<'_>,
    emit: &mut dyn FnMut(InitEvent),
) -> Result<(), Cause> {
    let name = || c.name.to_owned();
    let path = || c.location.clone();
    if !ports.files.exists(&c.location) {
        let Some(url) = c.url else {
            return Err(refused(RepoMessage::NoUrl { name: name() }));
        };
        if c.standalone.is_some() {
            confined(ports.files, declared, c).map_err(|reason| unsafe_location(c, reason))?;
        }
        if let Some(parent) = c.location.parent() {
            ports.writer.create_dir_all(parent)?;
        }
        git::clone(
            ports.programs,
            ports.tracer,
            c.name,
            declared.root,
            url,
            &c.location,
            c.branch,
        )?;
        emit(action(c, Action::Cloned, String::new()));
        return Ok(());
    }

    let resolved = ports.files.resolve(&c.location);
    // The root always exists, so the confinement of an absent location would never refuse it.
    if c.standalone.is_some() && resolved == ports.files.resolve(declared.root) {
        return Err(unsafe_location(c, UnsafeReason::OutsideWorkspace));
    }
    let not_a_repository = || {
        refused(RepoMessage::NotARepository {
            name: name(),
            path: path(),
        })
    };
    // Git cannot start in what is no directory.
    if !ports.files.is_dir(&c.location) {
        return Err(not_a_repository());
    }
    if git::toplevel(ports.programs, ports.tracer, c.name, &c.location)? != Some(resolved) {
        return Err(not_a_repository());
    }
    if let Some(url) = c.url {
        let found = git::origin_url(ports.programs, ports.tracer, c.name, &c.location)?;
        if found.as_deref() != Some(OsStr::new(url)) {
            return Err(refused(RepoMessage::OriginMismatch {
                name: name(),
                path: path(),
                declared: url.to_owned(),
                found: found.map(|found| found.to_string_lossy().into_owned()),
            }));
        }
    }
    emit(action(c, Action::Exists, String::new()));
    Ok(())
}

fn unsafe_location(c: &Checkout<'_>, reason: UnsafeReason) -> Cause {
    refused(RepoMessage::UnsafeLocation {
        name: c.name.to_owned(),
        path: c.location.clone(),
        reason,
    })
}

/// Whether a standalone may be cloned into its location, symbolic links resolved: below the root, outside the
/// reserved directories, and apart from every other declared repository.
fn confined(
    files: &dyn ReadFiles,
    declared: &Declared<'_>,
    c: &Checkout<'_>,
) -> Result<(), UnsafeReason> {
    let root = declared.root;
    let location = files.resolve(&c.location);
    let resolved_root = files.resolve(root);
    if location == resolved_root || !location.starts_with(&resolved_root) {
        return Err(UnsafeReason::OutsideWorkspace);
    }
    if [".winter", PROJECTS_DIR]
        .iter()
        .any(|reserved| location.starts_with(resolved_root.join(reserved)))
    {
        return Err(UnsafeReason::ReservedDirectory);
    }
    let projects = declared.projects.iter().map(|project| {
        let name = project.common.name.as_str();
        (name, root.join(PROJECTS_DIR).join(name))
    });
    // A standalone's name is unique among the standalones, though a project may share it.
    let own = c
        .standalone
        .map(|standalone| standalone.common.name.as_str());
    let standalones = declared
        .standalones
        .iter()
        .filter(|other| Some(other.common.name.as_str()) != own)
        .map(|other| {
            let name = other.common.name.as_str();
            (name, standalone_location(root, name, &other.path))
        });
    for (name, other) in projects.chain(standalones) {
        let other = files.resolve(&other);
        if location.starts_with(&other) || other.starts_with(&location) {
            return Err(UnsafeReason::Overlaps {
                name: name.to_owned(),
            });
        }
    }
    Ok(())
}

/// Writes the workspace's git identity, when it has one, to the repository's own configuration.
fn identity(ports: &Ports<'_>, declared: &Declared<'_>, c: &Checkout<'_>) -> Result<(), Cause> {
    let Some(identity) = declared.identity else {
        return Ok(());
    };
    for (key, value) in [
        ("user.name", &identity.name),
        ("user.email", &identity.email),
    ] {
        git::set_config(
            ports.programs,
            ports.tracer,
            c.name,
            &c.location,
            key,
            value,
        )?;
    }
    Ok(())
}

/// The line that heads the entries appended to an exclude file.
const EXCLUDES_HEADING: &str = "# winter-managed";

/// Appends the repository's exclude entries that its exclude file does not hold yet. Nothing is ever removed.
fn excludes(
    ports: &Ports<'_>,
    c: &Checkout<'_>,
    emit: &mut dyn FnMut(InitEvent),
) -> Result<(), Cause> {
    let entries: Vec<&str> = c
        .git_excludes
        .iter()
        .map(|entry| entry.trim())
        .filter(|entry| !entry.is_empty())
        .collect();
    if entries.is_empty() {
        return Ok(());
    }
    let Some(file) = exclude_path(ports.files, &c.location)? else {
        return Err(refused(RepoMessage::NoExcludeFile {
            name: c.name.to_owned(),
            path: c.location.clone(),
        }));
    };
    let mut appended = Vec::new();
    ports.writer.update(&file, &mut |current| {
        let (content, new) = with_entries(current.unwrap_or_default(), &entries)?;
        appended = new;
        Some(content)
    })?;
    if !appended.is_empty() {
        emit(action(c, Action::ExcludesUpdated, appended.join(", ")));
    }
    Ok(())
}

/// The content of an exclude file with the entries it lacks appended, and those entries, without repeats;
/// `None` when it lacks none.
fn with_entries<'e>(current: &[u8], entries: &[&'e str]) -> Option<(Vec<u8>, Vec<&'e str>)> {
    let held: Vec<&[u8]> = current
        .split(|byte| *byte == b'\n')
        .map(<[u8]>::trim_ascii)
        .collect();
    let mut new: Vec<&str> = Vec::new();
    for &entry in entries {
        if !held.contains(&entry.as_bytes()) && !new.contains(&entry) {
            new.push(entry);
        }
    }
    if new.is_empty() {
        return None;
    }
    let mut content = current.to_vec();
    if !content.is_empty() {
        if !content.ends_with(b"\n") {
            content.push(b'\n');
        }
        content.push(b'\n');
    }
    for line in std::iter::once(&EXCLUDES_HEADING).chain(&new) {
        content.extend_from_slice(line.as_bytes());
        content.push(b'\n');
    }
    Some((content, new))
}

/// The `info/exclude` the checkout at the absolute `location` reads, as its `.git` tells it; `None` when it
/// has no `.git` that is a directory or a `gitdir:` file. A worktree reads that of its common directory.
fn exclude_path(files: &dyn ReadFiles, location: &Path) -> io::Result<Option<PathBuf>> {
    let dot_git = location.join(".git");
    if files.is_dir(&dot_git) {
        return Ok(Some(dot_git.join("info/exclude")));
    }
    if !files.is_file(&dot_git) {
        return Ok(None);
    }
    let Some(content) = files.read(&dot_git)? else {
        return Ok(None);
    };
    let Some(named) = content.strip_prefix(b"gitdir:") else {
        return Ok(None);
    };
    let directory = location.join(OsStr::from_bytes(named.trim_ascii()));
    let commondir = directory.join("commondir");
    let common = if files.is_file(&commondir) {
        match files.read(&commondir)? {
            Some(content) => {
                files.resolve(&directory.join(OsStr::from_bytes(content.trim_ascii())))
            }
            None => directory,
        }
    } else {
        directory
    };
    Ok(Some(common.join("info/exclude")))
}

/// The variables every command is given, over any value the invocation holds.
const COMMAND_VARIABLES: [(&str, &str); 10] = [
    ("CI", "1"),
    ("TERM", "dumb"),
    ("NO_COLOR", "1"),
    ("FORCE_COLOR", "0"),
    ("NPM_CONFIG_PROGRESS", "false"),
    ("PNPM_PROGRESS", "false"),
    ("PYTHONUNBUFFERED", "1"),
    ("PIP_PROGRESS_BAR", "off"),
    ("DOTNET_NOLOGO", "1"),
    ("DOTNET_CLI_TELEMETRY_OPTOUT", "1"),
];

/// How the environment of a command differs from that of the invocation: the names left out, and the values
/// laid over it.
#[derive(Debug, Default)]
struct CommandEnvironment {
    remove: Vec<OsString>,
    set: Vec<(OsString, OsString)>,
}

/// The environment of a repository's commands, from the invocation's. A nested project's commands also see
/// nothing that ties them to the enclosing workspace: its `WINTER_*` variables but the logging and tracing
/// ones, and its virtual environment.
fn command_environment(env: &ProcessEnv, nested: bool) -> CommandEnvironment {
    let mut environment = CommandEnvironment {
        remove: Vec::new(),
        set: COMMAND_VARIABLES
            .iter()
            .map(|&(name, value)| (name.into(), value.into()))
            .collect(),
    };
    if !nested {
        return environment;
    }
    environment.remove = env
        .names()
        .filter(|name| {
            let name = name.as_bytes();
            name.starts_with(b"WINTER_")
                && name != b"WINTER_LOG_LEVEL"
                && !name.starts_with(b"WINTER_OTEL_")
        })
        .map(OsStr::to_owned)
        .collect();
    environment.remove.push("VIRTUAL_ENV".into());
    if let (Some(virtual_env), Some(path)) = (env.get("VIRTUAL_ENV"), env.get("PATH")) {
        let dropped = Path::new(virtual_env).join("bin");
        let kept: Vec<&[u8]> = path
            .as_bytes()
            .split(|byte| *byte == b':')
            .filter(|entry| *entry != dropped.as_os_str().as_bytes())
            .collect();
        environment
            .set
            .push(("PATH".into(), OsString::from_vec(kept.join(&b':'))));
    }
    environment
}

/// Runs the repository's commands in order through the shell, in its location, up to the first that fails.
fn commands(
    ports: &Ports<'_>,
    env: &ProcessEnv,
    c: &Checkout<'_>,
    emit: &mut dyn FnMut(InitEvent),
) -> Result<(), Cause> {
    if c.cmd.is_empty() {
        return Ok(());
    }
    let environment = command_environment(env, c.nested);
    let remove: Vec<&OsStr> = environment.remove.iter().map(OsString::as_os_str).collect();
    let set: Vec<(&OsStr, &OsStr)> = environment
        .set
        .iter()
        .map(|(name, value)| (name.as_os_str(), value.as_os_str()))
        .collect();
    let repo = || c.name.to_owned();
    for command in c.cmd {
        emit(InitEvent::CmdStarted {
            repo: repo(),
            command: command.clone(),
        });
        let args = [OsStr::new(command)];
        let call = Call {
            program: Program::Shell,
            args: &args,
            cwd: &c.location,
            timeout: None,
            env: &set,
            remove: &remove,
            stdin: Stdin::Inherit,
        };
        let exit: Exit = ports
            .programs
            .stream(&call, &mut |line| {
                emit(InitEvent::CmdOutputLine {
                    repo: repo(),
                    line: String::from_utf8_lossy(line).into_owned(),
                });
            })
            .map_err(|error| {
                refused(RepoMessage::CommandNotStarted {
                    name: repo(),
                    command: command.clone(),
                    cause: error.to_string(),
                })
            })?;
        let returncode = exit.number();
        emit(InitEvent::CmdCompleted {
            repo: repo(),
            command: command.clone(),
            returncode,
        });
        if returncode != 0 {
            return Err(refused(RepoMessage::CommandFailed {
                name: repo(),
                command: command.clone(),
                returncode,
            }));
        }
    }
    Ok(())
}

/// Materializes the pin of a standalone.
fn pin(
    ports: &Ports<'_>,
    declared: &Declared<'_>,
    c: &Checkout<'_>,
    emit: &mut dyn FnMut(InitEvent),
) -> Result<(), Cause> {
    let Some(standalone) = c.standalone else {
        return Ok(());
    };
    if let Some((kind, commit)) =
        materialize(ports, declared.root, standalone).map_err(Cause::Pin)?
    {
        let kind = match kind {
            RefKind::Branch => "branch",
            RefKind::Tag => "tag",
            RefKind::Commit => "commit",
        };
        // A recorded commit is kept as read, so it may be shorter than the 8 characters shown.
        let short: String = commit.chars().take(8).collect();
        emit(action(c, Action::Pinned, format!("{kind} {short}")));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::files::MemoryFiles;

    fn failure(step: Step, pins: bool) -> String {
        CheckoutError {
            step,
            cause: Cause::Io(io::Error::other("boom")),
            pins,
        }
        .to_string()
    }

    #[test]
    fn a_failure_names_its_step_and_the_steps_not_attempted() {
        for (step, pins, text) in [
            (
                Step::Location,
                true,
                "boom (failed at location; not attempted: identity, excludes, cmd, pin)",
            ),
            (
                Step::Location,
                false,
                "boom (failed at location; not attempted: identity, excludes, cmd)",
            ),
            (
                Step::Identity,
                false,
                "boom (failed at identity; not attempted: excludes, cmd)",
            ),
            (
                Step::Excludes,
                true,
                "boom (failed at excludes; not attempted: cmd, pin)",
            ),
            (Step::Cmd, true, "boom (failed at cmd; not attempted: pin)"),
            (Step::Cmd, false, "boom (failed at cmd)"),
            (Step::Pin, true, "boom (failed at pin)"),
        ] {
            assert_eq!(failure(step, pins), text);
        }
    }

    #[test]
    fn a_repo_error_reads_the_same_wherever_it_is_carried() {
        let error = || {
            Box::new(RepoError {
                message: RepoMessage::NoUrl {
                    name: "api".to_owned(),
                },
                program: None,
                subcommand: None,
                args: Vec::new(),
                cwd: Some(PathBuf::from("/ws")),
                exit: Some(1),
                stderr: None,
            })
        };
        let text = rendered(&error());

        for cause in [
            Cause::Repo(error()),
            Cause::Git(GitFailure::Repo(error())),
            Cause::Pin(PinError::Repo(error())),
        ] {
            assert_eq!(cause.to_string(), text);
        }
    }

    fn env(pairs: &[(&str, &str)]) -> ProcessEnv {
        ProcessEnv::from_vars(
            pairs
                .iter()
                .map(|&(name, value)| (name.into(), value.into())),
        )
    }

    fn texts(environment: &CommandEnvironment) -> (Vec<String>, Vec<String>) {
        let mut remove: Vec<String> = environment
            .remove
            .iter()
            .map(|name| name.to_string_lossy().into_owned())
            .collect();
        remove.sort();
        let set = environment
            .set
            .iter()
            .map(|(name, value)| format!("{}={}", name.to_string_lossy(), value.to_string_lossy()))
            .collect();
        (remove, set)
    }

    const ADDED: [&str; 10] = [
        "CI=1",
        "TERM=dumb",
        "NO_COLOR=1",
        "FORCE_COLOR=0",
        "NPM_CONFIG_PROGRESS=false",
        "PNPM_PROGRESS=false",
        "PYTHONUNBUFFERED=1",
        "PIP_PROGRESS_BAR=off",
        "DOTNET_NOLOGO=1",
        "DOTNET_CLI_TELEMETRY_OPTOUT=1",
    ];

    fn invocation() -> ProcessEnv {
        env(&[
            ("WINTER_ENV", "alpha"),
            ("WINTER_WORKSPACE", "/ws"),
            ("WINTER_LOG_LEVEL", "debug"),
            ("WINTER_OTEL_ENDPOINT", "x"),
            ("VIRTUAL_ENV", "/venv"),
            ("PATH", "/venv/bin:/usr/bin:/venv/bin:/bin"),
            ("HOME", "/home/a"),
        ])
    }

    #[test]
    fn a_command_is_given_the_added_variables_and_loses_none() {
        let (remove, set) = texts(&command_environment(&invocation(), false));

        assert!(remove.is_empty(), "{remove:?}");
        assert_eq!(set, ADDED);
    }

    #[test]
    fn a_nested_command_loses_the_enclosing_workspace_and_virtual_environment() {
        let (remove, set) = texts(&command_environment(&invocation(), true));

        assert_eq!(remove, ["VIRTUAL_ENV", "WINTER_ENV", "WINTER_WORKSPACE"]);
        assert_eq!(&set[..10], ADDED);
        assert_eq!(&set[10..], ["PATH=/usr/bin:/bin"]);
    }

    #[test]
    fn a_nested_command_without_a_virtual_environment_keeps_its_path() {
        let environment = env(&[("PATH", "/venv/bin:/bin")]);

        let (remove, set) = texts(&command_environment(&environment, true));

        assert_eq!(remove, ["VIRTUAL_ENV"]);
        assert_eq!(set, ADDED);
    }

    #[test]
    fn the_exclude_file_is_found_from_what_dot_git_is() {
        let files = MemoryFiles::default()
            .dir("/ws/plain/.git")
            .with("/ws/absolute/.git", b"gitdir: /store/absolute\n")
            .with("/ws/relative/.git", b"gitdir: ../store/relative\n")
            .with("/ws/tree/.git", b"gitdir: /main/.git/worktrees/tree\n")
            .with("/main/.git/worktrees/tree/commondir", b"../..\n")
            .with("/ws/other/.git", b"something else\n")
            .dir("/ws/none");

        for (location, expected) in [
            ("/ws/plain", Some("/ws/plain/.git/info/exclude")),
            ("/ws/absolute", Some("/store/absolute/info/exclude")),
            (
                "/ws/relative",
                Some("/ws/relative/../store/relative/info/exclude"),
            ),
            ("/ws/tree", Some("/main/.git/info/exclude")),
            ("/ws/other", None),
            ("/ws/none", None),
        ] {
            let found = exclude_path(&files, Path::new(location)).expect("a readable .git");
            assert_eq!(found, expected.map(PathBuf::from), "{location}");
        }
    }

    #[test]
    fn only_the_entries_a_file_lacks_are_appended_under_the_heading() {
        let appended = |current: &str, entries: &[&str]| {
            with_entries(current.as_bytes(), entries)
                .map(|(content, new)| (String::from_utf8_lossy(&content).into_owned(), new.len()))
        };

        assert_eq!(
            appended("", &["a", "b", "a"]),
            Some(("# winter-managed\na\nb\n".to_owned(), 2))
        );
        assert_eq!(
            appended("  a  \nx", &["a", "b"]),
            Some(("  a  \nx\n\n# winter-managed\nb\n".to_owned(), 1))
        );
        assert_eq!(appended("a\n", &["a"]), None);
    }
}
