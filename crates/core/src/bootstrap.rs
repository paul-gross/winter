//! Workspace reconciliation: bounded checkout scheduling, managed content, and shared event rendering.

use std::path::{Path, PathBuf};
use std::sync::mpsc;

use crate::agents_md::{self, Placement};
use crate::config_load;
use crate::config_model::{LoadMode, ProjectRepository, Workspace, WorkspaceConfig};
use crate::diagnostics::Diagnostics;
use crate::git::GitFailure;
use crate::pins::outcome::rendered;
use crate::pins::repin::RunEnd;
use crate::ports::Ports;
use crate::process::ProcessEnv;
use crate::termination::{ConfigError, InternalError, Termination};
use crate::workspace_exclude::{self, Eligible, Finalized};
use crate::workspace_files::{PROJECTS_DIR, standalone_location};

pub(crate) mod checkout;
pub(crate) mod create;
pub(crate) use create::{CreateError, create};

/// What reconciliation reports as it happens.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InitEvent {
    /// The target `projects/` or `standalone/` begins.
    TargetStarted {
        target: String,
    },
    TargetCompleted {
        target: String,
        success: bool,
    },
    /// `location` is the path the action concerns, and `detail` is as [`Action`] states.
    RepoAction {
        repo: String,
        location: String,
        action: Action,
        detail: String,
    },
    /// `error` is the rendered repo error, or the text of the I/O error.
    RepoError {
        repo: String,
        error: String,
    },
    CmdStarted {
        repo: String,
        command: String,
    },
    /// One line of a command's stdout or stderr, without its line end.
    CmdOutputLine {
        repo: String,
        line: String,
    },
    /// `returncode` is the exit code, or minus the number of the signal.
    CmdCompleted {
        repo: String,
        command: String,
        returncode: i32,
    },
}

/// What was done to a repository or to the workspace. The `detail` of the event is empty unless stated.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Action {
    Cloned,
    Exists,
    /// `detail`: the appended entries joined by `, `.
    ExcludesUpdated,
    /// `detail`: the kind of the pin and the first 8 characters of its commit.
    Pinned,
    /// `detail`: what the workspace's exclude block now holds.
    WorkspaceExcludesUpdated,
    AgentsMdUpdated,
}

impl Action {
    /// The action name in a reconciliation event's JSON representation.
    pub fn wire_name(self) -> &'static str {
        match self {
            Self::Cloned => "cloned",
            Self::Exists => "exists",
            Self::ExcludesUpdated => "excludes_updated",
            Self::Pinned => "pinned",
            Self::WorkspaceExcludesUpdated => "workspace_excludes_updated",
            Self::AgentsMdUpdated => "agents_md_updated",
        }
    }
}

/// A rule of workspace creation or pruning that the invocation violates.
#[derive(Debug, thiserror::Error)]
pub enum BootstrapError {
    #[error("{} already exists; run `winter-workspace ws init` to reconcile it", .path.display())]
    AlreadyInitialized { path: PathBuf },
    #[error("`ws prune --json` needs `--force` or `--dry-run`")]
    JsonNeedsForce,
}

/// A failure before reconciliation can start.
#[derive(Debug, thiserror::Error)]
pub enum InitError {
    #[error("{0}")]
    Config(#[source] Box<ConfigError>),
    #[error("{0}")]
    Internal(#[source] Box<InternalError>),
}

/// Events are delivered on the caller's thread. Refusal stops admission and joins active workers.
pub type InitSink<'s> = dyn FnMut(&InitEvent, &mut Diagnostics<'_>) -> Result<(), Termination> + 's;

/// The stream for a human-readable event row.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TextStream {
    Stdout,
    Stderr,
}

/// Shared human rendering; successful command completion has no row.
pub fn text(event: &InitEvent) -> Option<(TextStream, String)> {
    use TextStream::{Stderr, Stdout};
    Some(match event {
        InitEvent::TargetStarted { target } => (Stdout, format!("→ {target}")),
        InitEvent::TargetCompleted {
            target,
            success: true,
        } => (Stdout, format!("✓ {target} done")),
        InitEvent::TargetCompleted {
            target,
            success: false,
        } => (Stderr, format!("✗ {target} failed")),
        InitEvent::RepoError { repo, error } => (Stderr, format!("[{repo}] ERROR: {error}")),
        InitEvent::CmdStarted { repo, command } => (Stdout, format!("[{repo}] $ {command}")),
        InitEvent::CmdOutputLine { repo, line } => (Stdout, format!("[{repo}] {line}")),
        InitEvent::CmdCompleted { returncode: 0, .. } => return None,
        InitEvent::CmdCompleted {
            repo,
            command,
            returncode,
        } => (Stderr, format!("[{repo}] ✗ {command} (exit {returncode})")),
        InitEvent::RepoAction {
            repo,
            location,
            action,
            detail,
        } => {
            let row = match action {
                Action::Cloned => format!("cloned to {location}"),
                Action::Exists => format!("exists at {location}"),
                Action::ExcludesUpdated => format!("excludes updated: {detail}"),
                Action::Pinned => format!("pinned at {detail}"),
                Action::WorkspaceExcludesUpdated => format!("workspace excludes updated: {detail}"),
                Action::AgentsMdUpdated => format!("AGENTS.md safety rules updated at {location}"),
            };
            (Stdout, format!("[{repo}] {row}"))
        }
    })
}

/// One shape-C event, without an invocation-level envelope.
pub(crate) fn document(event: &InitEvent) -> serde_json::Value {
    match event {
        InitEvent::TargetStarted { target } => {
            serde_json::json!({"type": "target_started", "target": target})
        }
        InitEvent::TargetCompleted { target, success } => {
            serde_json::json!({"type": "target_completed", "target": target, "success": success})
        }
        InitEvent::RepoAction {
            repo,
            location,
            action,
            detail,
        } => {
            let action = action.wire_name();
            serde_json::json!({
                "type": "repo_action", "repo": repo, "location": location,
                "action": action, "detail": detail,
            })
        }
        InitEvent::RepoError { repo, error } => {
            serde_json::json!({"type": "repo_error", "repo": repo, "error": error})
        }
        InitEvent::CmdStarted { repo, command } => {
            serde_json::json!({"type": "cmd_started", "repo": repo, "command": command})
        }
        InitEvent::CmdOutputLine { repo, line } => {
            serde_json::json!({"type": "cmd_output_line", "repo": repo, "line": line})
        }
        InitEvent::CmdCompleted {
            repo,
            command,
            returncode,
        } => serde_json::json!({
            "type": "cmd_completed", "repo": repo, "command": command, "returncode": returncode,
        }),
    }
}

/// Reconciles declared checkouts and refreshes only content owned by the workspace.
pub fn reconcile(
    ports: &Ports<'_>,
    env: &ProcessEnv,
    diagnostics: &mut Diagnostics<'_>,
    start: &Path,
    sink: &mut InitSink<'_>,
) -> Result<RunEnd, InitError> {
    let workspace = config_load::load(ports.files, start)
        .map_err(|error| InitError::Config(Box::new(error)))?;
    let mut emit = |event: InitEvent| sink(&event, diagnostics);
    Ok(match reconcile_loaded(ports, env, &workspace, &mut emit) {
        Ok(success) => RunEnd::Completed { success },
        Err(ending) => RunEnd::Stopped(ending),
    })
}

type Emit<'a> = dyn FnMut(InitEvent) -> Result<(), Termination> + 'a;

fn error(emit: &mut Emit<'_>, repo: &str, error: String) -> Result<(), Termination> {
    emit(InitEvent::RepoError {
        repo: repo.to_owned(),
        error,
    })
}

fn action(
    emit: &mut Emit<'_>,
    repo: &str,
    file: &Path,
    action: Action,
    detail: String,
) -> Result<(), Termination> {
    emit(InitEvent::RepoAction {
        repo: repo.to_owned(),
        location: file.display().to_string(),
        action,
        detail,
    })
}

/// The text of a git failure in an event or a warning.
pub(crate) fn git_text(error: GitFailure) -> String {
    match error {
        GitFailure::Repo(error) => rendered(&error),
        GitFailure::Internal(error) => error.to_string(),
    }
}

fn block(
    ports: &Ports<'_>,
    root: &Path,
    name: &str,
    lines: &[String],
    report: bool,
    emit: &mut Emit<'_>,
) -> Result<bool, Termination> {
    match workspace_exclude::write_block(ports, root, name, lines) {
        Ok(Some(file)) if report => action(
            emit,
            "winter",
            &file,
            Action::WorkspaceExcludesUpdated,
            lines.join(", "),
        )?,
        Ok(_) => {}
        Err(cause) => {
            let (repo, label) = if report {
                ("winter", "workspace exclude")
            } else {
                ("workspace", "workspace skills")
            };
            error(emit, repo, format!("{label} — {}", git_text(cause)))?;
            return Ok(false);
        }
    }
    Ok(true)
}

/// Whether `project` is an extension: it does not opt out, and its checkout holds `winter-ext.toml`.
pub(crate) fn extension(ports: &Ports<'_>, root: &Path, project: &ProjectRepository) -> bool {
    project.common.extension
        && ports.files.is_file(
            &root
                .join(PROJECTS_DIR)
                .join(&project.common.name)
                .join("winter-ext.toml"),
        )
}

fn projects_only(config: &WorkspaceConfig) -> impl Iterator<Item = &ProjectRepository> {
    config.project_repositories.iter().filter(|project| {
        !config
            .standalone_repositories
            .iter()
            .any(|standalone| standalone.common.name == project.common.name)
    })
}

/// The extensions that get an exclude block of their own: each standalone whose location exists, then each
/// project that is an extension, in merged order. A location outside the root is left out.
pub(crate) fn eligible(ports: &Ports<'_>, workspace: &Workspace) -> Vec<Eligible> {
    let root = &workspace.root;
    let config = &workspace.config;
    let standalone = config
        .standalone_repositories
        .iter()
        .map(|repo| {
            (
                &repo.common.name,
                standalone_location(root, &repo.common.name, &repo.path),
            )
        })
        .filter(|(_, location)| ports.files.exists(location));
    let projects = projects_only(config)
        .filter(|project| extension(ports, root, project))
        .map(|project| {
            (
                &project.common.name,
                root.join(PROJECTS_DIR).join(&project.common.name),
            )
        });
    standalone
        .chain(projects)
        .filter_map(|(name, location)| {
            let resolved = ports.files.resolve(&location);
            let resolved_root = ports.files.resolve(root);
            if resolved == resolved_root || !resolved.starts_with(&resolved_root) {
                return None;
            }
            // Confinement follows links; the exclude pattern names the declared checkout entry,
            // not the in-workspace directory to which that entry happens to point.
            let relative = location.strip_prefix(root).ok()?;
            Some(Eligible {
                name: name.clone(),
                relative: relative.display().to_string(),
                extra: Vec::new(),
            })
        })
        .collect()
}

/// The event of a finalization of the extension blocks: the update, the failure, or none for a file that
/// stays as it is.
pub(crate) fn finalized_event(finalized: Result<Finalized, GitFailure>) -> Option<InitEvent> {
    const REPO: &str = "winter-extensions";
    match finalized {
        Ok(Finalized::Updated { file, names }) => Some(InitEvent::RepoAction {
            repo: REPO.to_owned(),
            location: file.display().to_string(),
            action: Action::WorkspaceExcludesUpdated,
            detail: if names.is_empty() {
                "cleared".to_owned()
            } else {
                names.join(", ")
            },
        }),
        Ok(_) => None,
        Err(cause) => Some(InitEvent::RepoError {
            repo: REPO.to_owned(),
            error: git_text(cause),
        }),
    }
}

fn reconcile_loaded(
    ports: &Ports<'_>,
    env: &ProcessEnv,
    workspace: &Workspace,
    emit: &mut Emit<'_>,
) -> Result<bool, Termination> {
    let root = &workspace.root;
    let config = &workspace.config;
    emit(InitEvent::TargetStarted {
        target: "projects/".to_owned(),
    })?;
    let mut projects_ok = true;
    if let Err(cause) = ports.writer.create_dir_all(&root.join(PROJECTS_DIR)) {
        error(emit, "winter", cause.to_string())?;
        projects_ok = false;
    }
    for (name, line) in [
        ("winter-dir/projects", "/projects/"),
        ("winter-workspace/artifacts", "/.winter/logs/"),
    ] {
        projects_ok &= block(ports, root, name, &[line.to_owned()], true, emit)?;
    }
    let declared = checkout::Declared {
        root,
        identity: config.git_identity.as_ref(),
        projects: &config.project_repositories,
        standalones: &config.standalone_repositories,
    };
    let excludes = |own: &[String]| config.git_excludes.iter().chain(own).cloned().collect();
    let projects: Vec<_> = config
        .project_repositories
        .iter()
        .map(|repo| checkout::Checkout {
            name: &repo.common.name,
            url: repo.common.url.as_deref(),
            location: root.join(PROJECTS_DIR).join(&repo.common.name),
            git_excludes: excludes(&repo.common.git_excludes),
            cmd: &repo.common.cmd,
            nested: repo.nested.is_some(),
            branch: Some(&repo.common.main_branch),
            standalone: None,
        })
        .collect();
    projects_ok &= fan_out(ports, env, &declared, &projects, emit)?;
    let lines = [".claude/skills", ".codex/skills", ".opencode/skill"]
        .into_iter()
        .flat_map(|directory| {
            [
                format!("{directory}/{}-*", config.prefix),
                format!("{directory}/{}", config.prefix),
            ]
        })
        .collect::<Vec<_>>();
    projects_ok &= block(
        ports,
        root,
        "winter-workspace/workspace-skills",
        &lines,
        false,
        emit,
    )?;
    emit(InitEvent::TargetCompleted {
        target: "projects/".to_owned(),
        success: projects_ok,
    })?;

    let active = !config.standalone_repositories.is_empty()
        || projects_only(config).any(|project| {
            let common = &project.common;
            !common.extension
                || extension(ports, root, project)
                || (common.load != Some(LoadMode::None)
                    && (common.load.is_some() || common.entry.is_some()))
        });
    let mut standalone_ok = true;
    if active {
        emit(InitEvent::TargetStarted {
            target: "standalone/".to_owned(),
        })?;
        let standalones: Vec<_> = config
            .standalone_repositories
            .iter()
            .map(|repo| checkout::Checkout {
                name: &repo.common.name,
                url: repo.common.url.as_deref(),
                location: standalone_location(root, &repo.common.name, &repo.path),
                git_excludes: excludes(&repo.common.git_excludes),
                cmd: &repo.common.cmd,
                nested: false,
                branch: None,
                standalone: Some(repo),
            })
            .collect();
        standalone_ok &= fan_out(ports, env, &declared, &standalones, emit)?;
    }
    let eligible = if active {
        eligible(ports, workspace)
    } else {
        Vec::new()
    };
    let finalized =
        workspace_exclude::finalize(ports, root, config.adopt_extensions, &eligible, &[]);
    standalone_ok &= finalized.is_ok();
    if let Some(event) = finalized_event(finalized) {
        emit(event)?;
    }
    if active {
        emit(InitEvent::TargetCompleted {
            target: "standalone/".to_owned(),
            success: standalone_ok,
        })?;
    }
    let mut safety_ok = true;
    match agents_md::refresh(ports.writer, root) {
        Ok(Placement::Updated) => action(
            emit,
            "workspace",
            &root.join("AGENTS.md"),
            Action::AgentsMdUpdated,
            String::new(),
        )?,
        Ok(Placement::Conflict(conflict)) => {
            error(
                emit,
                "workspace",
                match conflict {
                    agents_md::Conflict::MalformedBlock { path } => {
                        format!("{}: malformed safety block", path.display())
                    }
                    agents_md::Conflict::NotUtf8 { path } => {
                        format!("{}: safety file is not UTF-8", path.display())
                    }
                },
            )?;
            safety_ok = false;
        }
        Ok(_) => {}
        Err(cause) => {
            error(emit, "workspace", cause.to_string())?;
            safety_ok = false;
        }
    }
    Ok(projects_ok && standalone_ok && safety_ok)
}

/// Admission belongs to the receiver, so a refused event cannot start another repository.
fn fan_out(
    ports: &Ports<'_>,
    env: &ProcessEnv,
    declared: &checkout::Declared<'_>,
    checkouts: &[checkout::Checkout<'_>],
    emit: &mut Emit<'_>,
) -> Result<bool, Termination> {
    schedule(checkouts.len(), emit, &|index, send| {
        let checkout = &checkouts[index];
        let result = checkout::run(ports, env, declared, checkout, &mut |event| {
            let _ = send.send(Message::Event(event));
        });
        if let Err(cause) = &result {
            let _ = send.send(Message::Event(InitEvent::RepoError {
                repo: checkout.name.to_owned(),
                error: cause.to_string(),
            }));
        }
        result.is_ok()
    })
}

// Bound queued events independently of checkout count and command output volume.
const EVENT_CAPACITY: usize = 32;

enum Message {
    Event(InitEvent),
    Done(bool),
}

fn schedule(
    count: usize,
    emit: &mut Emit<'_>,
    run: &(impl Fn(usize, &mpsc::SyncSender<Message>) -> bool + Sync),
) -> Result<bool, Termination> {
    // A worker that unwinds must still wake the receiver; the scope then propagates its panic.
    struct Completion<'a> {
        send: &'a mpsc::SyncSender<Message>,
        success: bool,
    }
    impl Drop for Completion<'_> {
        fn drop(&mut self) {
            let _ = self.send.send(Message::Done(self.success));
        }
    }
    std::thread::scope(|scope| {
        // Keep the receiver inside the scope callback: every exit (including refusal or
        // unwinding) disconnects blocked event and completion senders before scope joins.
        let (send, receive) = mpsc::sync_channel(EVENT_CAPACITY);
        let mut next = 0..count;
        let spawn = |index: usize| {
            let send = send.clone();
            scope.spawn(move || {
                let mut completion = Completion {
                    send: &send,
                    success: false,
                };
                completion.success = run(index, &send);
            });
        };
        let mut active = 0;
        for checkout in next.by_ref().take(4) {
            spawn(checkout);
            active += 1;
        }
        let mut success = true;
        while active > 0 {
            match receive.recv().expect("active workers retain their sender") {
                Message::Event(event) => emit(event)?,
                Message::Done(ok) => {
                    success &= ok;
                    active -= 1;
                    if let Some(checkout) = next.next() {
                        spawn(checkout);
                        active += 1;
                    }
                }
            }
        }
        Ok(success)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::files::{Edit, MemoryFiles, WriteFiles};
    use crate::programs::{Call, Exit, Finished, RunPrograms};
    use crate::trace::NoTrace;
    use std::io;
    use std::sync::{Condvar, Mutex};

    #[test]
    fn action_wire_names_are_stable() {
        for (action, name) in [
            (Action::Cloned, "cloned"),
            (Action::Exists, "exists"),
            (Action::ExcludesUpdated, "excludes_updated"),
            (Action::Pinned, "pinned"),
            (
                Action::WorkspaceExcludesUpdated,
                "workspace_excludes_updated",
            ),
            (Action::AgentsMdUpdated, "agents_md_updated"),
        ] {
            assert_eq!(action.wire_name(), name);
        }
    }

    #[test]
    fn actions_keep_all_fields_and_their_wire_names() {
        for action in [
            Action::Cloned,
            Action::Exists,
            Action::ExcludesUpdated,
            Action::Pinned,
            Action::WorkspaceExcludesUpdated,
            Action::AgentsMdUpdated,
        ] {
            assert_eq!(
                document(&InitEvent::RepoAction {
                    repo: "api".into(),
                    location: "/w/projects/api".into(),
                    action,
                    detail: "".into(),
                }),
                serde_json::json!({
                    "type": "repo_action", "repo": "api", "location": "/w/projects/api",
                    "action": action.wire_name(), "detail": "",
                })
            );
        }
    }

    #[test]
    fn target_and_command_events_keep_their_fields() {
        assert_eq!(
            document(&InitEvent::TargetStarted {
                target: "projects/".into()
            }),
            serde_json::json!({"type": "target_started", "target": "projects/"})
        );
        for success in [true, false] {
            assert_eq!(
                document(&InitEvent::TargetCompleted {
                    target: "projects/".into(),
                    success
                }),
                serde_json::json!({"type": "target_completed", "target": "projects/", "success": success})
            );
        }
        assert_eq!(
            document(&InitEvent::RepoError {
                repo: "api".into(),
                error: "failed\nstep".into()
            }),
            serde_json::json!({"type": "repo_error", "repo": "api", "error": "failed\nstep"})
        );
        assert_eq!(
            document(&InitEvent::CmdStarted {
                repo: "api".into(),
                command: "make setup".into()
            }),
            serde_json::json!({"type": "cmd_started", "repo": "api", "command": "make setup"})
        );
        assert_eq!(
            document(&InitEvent::CmdOutputLine {
                repo: "api".into(),
                line: "".into()
            }),
            serde_json::json!({"type": "cmd_output_line", "repo": "api", "line": ""})
        );
        for returncode in [0, 7, -15] {
            assert_eq!(
                document(&InitEvent::CmdCompleted {
                    repo: "api".into(),
                    command: "make setup".into(),
                    returncode
                }),
                serde_json::json!({"type": "cmd_completed", "repo": "api", "command": "make setup", "returncode": returncode})
            );
        }
    }

    struct Writer;
    impl WriteFiles for Writer {
        fn create_dir_all(&self, _: &Path) -> io::Result<()> {
            Ok(())
        }
        fn remove(&self, _: &Path) -> io::Result<()> {
            unreachable!()
        }
        fn update(&self, _: &Path, _: &mut Edit<'_>) -> io::Result<bool> {
            unreachable!()
        }
    }

    #[derive(Default)]
    struct Activity {
        active: usize,
        maximum: usize,
        started: usize,
        ended: usize,
        released: bool,
    }
    #[derive(Default)]
    struct Recording {
        activity: Mutex<Activity>,
        changed: Condvar,
    }
    impl RunPrograms for Recording {
        fn run(&self, _: &Call<'_>) -> io::Result<Finished> {
            Ok(Finished {
                exit: Exit::Code(0),
                stdout: Vec::new(),
                stderr: Vec::new(),
            })
        }
        fn stream(&self, _: &Call<'_>, line: &mut dyn FnMut(&[u8])) -> io::Result<Exit> {
            {
                let mut state = self.activity.lock().expect("activity");
                state.active += 1;
                state.started += 1;
                state.maximum = state.maximum.max(state.active);
                self.changed.notify_all();
            }
            line(b"ready");
            let mut state = self.activity.lock().expect("activity");
            while !state.released {
                state = self.changed.wait(state).expect("release");
            }
            state.active -= 1;
            state.ended += 1;
            Ok(Exit::Code(0))
        }
    }
    impl Recording {
        fn release_first_batch(&self) {
            let state = self.activity.lock().expect("activity");
            // A broken admission limit fails within a bound rather than hanging the test suite.
            let (mut state, timeout) = self
                .changed
                .wait_timeout_while(state, std::time::Duration::from_secs(5), |state| {
                    state.started < 4
                })
                .expect("first batch");
            state.released = true;
            self.changed.notify_all();
            assert!(!timeout.timed_out(), "four workers must reach the barrier");
        }
    }

    fn concurrent(stop: bool) {
        let files = MemoryFiles::default();
        let programs = Recording::default();
        let ports = Ports {
            files: &files,
            writer: &Writer,
            programs: &programs,
            tracer: &NoTrace,
        };
        let declared = checkout::Declared {
            root: Path::new("/ws"),
            identity: None,
            projects: &[],
            standalones: &[],
        };
        let names: Vec<_> = (0..9).map(|i| format!("repo{i}")).collect();
        let commands = vec!["setup".to_owned()];
        let checkouts: Vec<_> = names
            .iter()
            .map(|name| checkout::Checkout {
                name,
                url: Some("fixture"),
                location: Path::new("/ws").join(name),
                git_excludes: Vec::new(),
                cmd: &commands,
                nested: false,
                branch: None,
                standalone: None,
            })
            .collect();
        let caller = std::thread::current().id();
        let mut events = Vec::new();
        let mut released = false;
        let result = fan_out(
            &ports,
            &ProcessEnv::from_vars([]),
            &declared,
            &checkouts,
            &mut |event| {
                assert_eq!(std::thread::current().id(), caller);
                let ready = matches!(event, InitEvent::CmdOutputLine { .. });
                events.push(event);
                if ready && !released {
                    programs.release_first_batch();
                    released = true;
                    if stop {
                        return Err(Termination::BareGroup {
                            help: "original sink failure".into(),
                        });
                    }
                }
                Ok(())
            },
        );
        let state = programs.activity.lock().expect("activity");
        assert_eq!(state.active, 0, "workers joined before return");
        assert_eq!(state.maximum, 4, "bounded repository fan-out");
        assert_eq!(state.started, if stop { 4 } else { 9 });
        assert_eq!(state.ended, state.started);
        if stop {
            assert!(
                matches!(result, Err(Termination::BareGroup { help }) if help == "original sink failure")
            );
            assert!(
                matches!(events.last(), Some(InitEvent::CmdOutputLine { .. })),
                "nothing delivered after refusal"
            );
        } else {
            assert!(matches!(result, Ok(true)));
            for name in &names {
                let rows: Vec<_> = events
                    .iter()
                    .filter(|event| match event {
                        InitEvent::RepoAction { repo, .. }
                        | InitEvent::CmdStarted { repo, .. }
                        | InitEvent::CmdOutputLine { repo, .. }
                        | InitEvent::CmdCompleted { repo, .. } => repo == name,
                        _ => false,
                    })
                    .collect();
                assert!(
                    matches!(
                        rows.as_slice(),
                        [
                            InitEvent::RepoAction {
                                action: Action::Cloned,
                                ..
                            },
                            InitEvent::CmdStarted { .. },
                            InitEvent::CmdOutputLine { .. },
                            InitEvent::CmdCompleted { returncode: 0, .. }
                        ]
                    ),
                    "{rows:?}"
                );
            }
        }
    }

    #[test]
    fn four_workers_deliver_ordered_events_on_the_calling_thread() {
        concurrent(false);
    }

    #[test]
    fn refusal_stops_admission_and_joins_workers_preserving_the_ending() {
        concurrent(true);
    }

    fn saturated_events(stop: bool, completion: bool) {
        let (finished, joined) = mpsc::channel();
        let worker = std::thread::spawn(move || {
            saturated_events_run(stop, completion);
            finished.send(()).expect("test awaits completion");
        });
        // Do not use a scope here: on a deadlock regression its implicit join
        // would defeat the deadline. A failed test leaves only in-memory threads.
        joined
            .recv_timeout(std::time::Duration::from_secs(10))
            .expect("scheduler must disconnect blocked senders and join within the deadline");
        worker.join().expect("scheduler assertions");
    }

    fn saturated_events_run(stop: bool, completion: bool) {
        use std::sync::Barrier;
        use std::sync::atomic::{AtomicUsize, Ordering};

        let filled = Barrier::new(4);
        let (ready, waiting) = mpsc::channel();
        let ended = AtomicUsize::new(0);
        let output = |repo: usize, line: usize| {
            Message::Event(InitEvent::CmdOutputLine {
                repo: repo.to_string(),
                line: line.to_string(),
            })
        };
        let mut events = Vec::new();
        let result = schedule(
            if stop { 9 } else { 4 },
            &mut |event| {
                if events.is_empty() {
                    // The receiver holds the first event while all four producers
                    // witness a full queue. No elapsed-time assertion or sleep.
                    for _ in 0..4 {
                        waiting
                            .recv_timeout(std::time::Duration::from_secs(5))
                            .expect("each producer must encounter backpressure");
                    }
                    if !completion {
                        assert_eq!(ended.load(Ordering::SeqCst), 0);
                    }
                }
                events.push(event);
                if stop {
                    Err(Termination::BareGroup {
                        help: "saturated sink refusal".into(),
                    })
                } else {
                    Ok(())
                }
            },
            &|index, send| {
                assert!(index < 4, "refusal must not admit another worker");
                if index == 0 {
                    // The first event reaches the sink; the remainder fill the queue.
                    for line in 0..=EVENT_CAPACITY {
                        assert!(send.send(output(index, line)).is_ok());
                    }
                }
                filled.wait();
                let message = match send.try_send(output(index, EVENT_CAPACITY + 1)) {
                    Err(mpsc::TrySendError::Full(message)) => message,
                    _ => panic!("the stalled sink must backpressure each producer"),
                };
                ready.send(()).expect("sink is waiting");
                if !completion {
                    let sent = send.send(message);
                    assert_eq!(sent.is_err(), stop);
                }
                // In completion mode, return with the queue still full. The
                // scheduler's Completion guard must send Done into that queue.
                ended.fetch_add(1, Ordering::SeqCst);
                true
            },
        );
        assert_eq!(ended.load(Ordering::SeqCst), 4, "all workers joined");
        if stop {
            assert!(matches!(
                result,
                Err(Termination::BareGroup { help }) if help == "saturated sink refusal"
            ));
            assert_eq!(events.len(), 1, "no delivery after refusal");
        } else {
            assert!(matches!(result, Ok(true)));
            assert_eq!(events.len(), EVENT_CAPACITY + 5);
            let leader: Vec<_> = events
                .iter()
                .filter_map(|event| match event {
                    InitEvent::CmdOutputLine { repo, line } if repo == "0" => Some(line.clone()),
                    _ => None,
                })
                .collect();
            assert_eq!(
                leader,
                (0..=EVENT_CAPACITY + 1)
                    .map(|line| line.to_string())
                    .collect::<Vec<_>>()
            );
        }
    }

    #[test]
    fn full_event_queue_backpressures_workers_until_sink_resumes() {
        saturated_events(false, false);
    }

    #[test]
    fn refusal_disconnects_full_queue_before_joining_blocked_workers() {
        saturated_events(true, false);
    }

    #[test]
    fn refusal_disconnects_full_queue_before_joining_completion_senders() {
        saturated_events(true, true);
    }

    #[test]
    fn rendering_selects_streams_and_omits_successful_command_completion() {
        assert_eq!(
            text(&InitEvent::CmdCompleted {
                repo: "r".into(),
                command: "c".into(),
                returncode: 0
            }),
            None
        );
        assert_eq!(
            text(&InitEvent::CmdCompleted {
                repo: "r".into(),
                command: "c".into(),
                returncode: -9
            }),
            Some((TextStream::Stderr, "[r] ✗ c (exit -9)".into()))
        );
        assert_eq!(
            text(&InitEvent::TargetStarted {
                target: "projects/".into()
            }),
            Some((TextStream::Stdout, "→ projects/".into()))
        );
        assert_eq!(
            text(&InitEvent::TargetCompleted {
                target: "projects/".into(),
                success: false
            }),
            Some((TextStream::Stderr, "✗ projects/ failed".into()))
        );
        assert_eq!(
            text(&InitEvent::RepoError {
                repo: "r".into(),
                error: "oops".into()
            }),
            Some((TextStream::Stderr, "[r] ERROR: oops".into()))
        );
    }
}
