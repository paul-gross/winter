//! `run_checkout` against the host's git and `/bin/sh` in fixture workspaces.

use std::io;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

use winter_workspace_core::{
    Action, Call, Cause, Checkout, CheckoutError, Declared, Exit, Finished, GitIdentity, InitEvent,
    LockEntry, OsFiles, OsPrograms, PinError, PinPorts, ProcessEnv, Program, ProjectRepository,
    RefKind, RepoError, RepoMessage, RepositoryCommon, RunPrograms, StandaloneRepository, Stdin,
    Step, UnsafeReason, read_lock, run_checkout, upsert_lock,
};
use winter_workspace_test_support::{CommitSpec, Git, TempTree, snapshot};

#[path = "support/spans.rs"]
#[allow(dead_code)]
mod spans;
use spans::Spans;

/// The programs of the operating system, recording how each streamed call was made.
struct Recording {
    programs: OsPrograms,
    streamed: Mutex<Vec<(Program, Stdin, Option<Duration>)>>,
    cannot_stream: bool,
}

impl RunPrograms for Recording {
    fn run(&self, call: &Call<'_>) -> io::Result<Finished> {
        self.programs.run(call)
    }

    fn stream(&self, call: &Call<'_>, line: &mut dyn FnMut(&[u8])) -> io::Result<Exit> {
        self.streamed
            .lock()
            .expect("the calls")
            .push((call.program, call.stdin, call.timeout));
        if self.cannot_stream {
            return Err(io::Error::other("no shell"));
        }
        self.programs.stream(call, line)
    }
}

struct Fixture {
    tree: TempTree,
    root: PathBuf,
    env: ProcessEnv,
    programs: Recording,
    identity: Option<GitIdentity>,
    projects: Vec<ProjectRepository>,
    standalones: Vec<StandaloneRepository>,
}

/// What one repository is declared as, beyond its name.
#[derive(Default)]
struct Repo<'a> {
    url: Option<&'a str>,
    excludes: &'a [&'a str],
    cmd: &'a [&'a str],
    nested: bool,
    branch: Option<&'a str>,
}

type Ran = (Vec<InitEvent>, Result<(), Box<CheckoutError>>);

fn commit(content: &str, tag: Option<&str>) -> CommitSpec {
    CommitSpec {
        message: content.to_owned(),
        files: vec![("a.txt".to_owned(), content.as_bytes().to_vec())],
        tag: tag.map(str::to_owned),
        ..CommitSpec::default()
    }
}

fn common(name: &str) -> RepositoryCommon {
    RepositoryCommon {
        name: name.to_owned(),
        url: None,
        main_branch: "main".to_owned(),
        git_excludes: Vec::new(),
        cmd: Vec::new(),
        extension: false,
        load: None,
        entry: None,
    }
}

fn standalone(name: &str, path: &str, reference: Option<&str>) -> StandaloneRepository {
    StandaloneRepository {
        common: common(name),
        reference: reference.map(str::to_owned),
        path: path.to_owned(),
        prefix: None,
        config_dir: String::new(),
    }
}

fn project(name: &str) -> ProjectRepository {
    ProjectRepository {
        common: common(name),
        pinned: false,
        nested: None,
    }
}

fn url(name: &str) -> String {
    format!("https://fixture.invalid/{name}.git")
}

fn fixture() -> Fixture {
    fixture_with(&[])
}

/// A fixture whose invocation holds `variables` over the pinned environment, with the remote `api` of one
/// commit tagged `v1`.
fn fixture_with(variables: &[(&str, &str)]) -> Fixture {
    let tree = TempTree::new().expect("an isolated tree");
    Git::new(&tree)
        .expect("git")
        .bare_remote("api", "main", &[commit("one\n", Some("v1"))])
        .expect("a remote");
    let mut pinned = tree.env();
    for (name, value) in variables {
        pinned.set(name, value);
    }
    let env = ProcessEnv::from_vars(
        pinned
            .variables()
            .map(|(name, value)| (name.to_owned(), value.to_owned())),
    );
    let programs = Recording {
        programs: OsPrograms::new(&env, false),
        streamed: Mutex::default(),
        cannot_stream: false,
    };
    let root = tree.workspace_dir();
    std::fs::create_dir_all(&root).expect("a workspace directory");
    Fixture {
        tree,
        root,
        env,
        programs,
        identity: None,
        projects: Vec::new(),
        standalones: Vec::new(),
    }
}

impl Fixture {
    fn git(&self, cwd: &Path, args: &[&str]) -> String {
        Git::new(&self.tree)
            .expect("git")
            .run(cwd, args)
            .expect("git output")
            .trim()
            .to_owned()
    }

    fn run_at(
        &self,
        name: &str,
        location: PathBuf,
        repo: &Repo<'_>,
        standalone: Option<&StandaloneRepository>,
    ) -> Ran {
        let ports = PinPorts {
            files: &OsFiles,
            writer: &OsFiles,
            programs: &self.programs,
            tracer: &Spans::default(),
        };
        let declared = Declared {
            root: &self.root,
            identity: self.identity.as_ref(),
            projects: &self.projects,
            standalones: &self.standalones,
        };
        let cmd: Vec<String> = repo.cmd.iter().map(|&line| line.to_owned()).collect();
        let checkout = Checkout {
            name,
            url: repo.url,
            location,
            git_excludes: repo
                .excludes
                .iter()
                .map(|&entry| entry.to_owned())
                .collect(),
            cmd: &cmd,
            nested: repo.nested,
            branch: repo.branch,
            standalone,
        };
        let mut events = Vec::new();
        let result = run_checkout(&ports, &self.env, &declared, &checkout, &mut |event| {
            events.push(event);
        });
        (events, result)
    }

    /// Runs the project `name` at `projects/<name>`.
    fn project(&self, name: &str, repo: &Repo<'_>) -> Ran {
        self.run_at(name, self.root.join("projects").join(name), repo, None)
    }

    /// Runs the standalone `entry` at its declared location.
    fn standalone(&self, entry: &StandaloneRepository, repo: &Repo<'_>) -> Ran {
        let path = if entry.path.is_empty() {
            &entry.common.name
        } else {
            &entry.path
        };
        self.run_at(&entry.common.name, self.root.join(path), repo, Some(entry))
    }

    fn lock(&self) -> Option<Vec<u8>> {
        std::fs::read(self.root.join(".winter/config.lock")).ok()
    }

    fn record(&self, name: &str, r#ref: &str, kind: RefKind, commit: &str) {
        upsert_lock(
            &OsFiles,
            &self.root,
            LockEntry {
                name: name.to_owned(),
                r#ref: r#ref.to_owned(),
                kind,
                commit: commit.to_owned(),
            },
        )
        .expect("a lock entry");
    }
}

/// The actions of the events, each with its detail.
fn actions(events: &[InitEvent]) -> Vec<(Action, String)> {
    events
        .iter()
        .filter_map(|event| match event {
            InitEvent::RepoAction { action, detail, .. } => Some((*action, detail.clone())),
            _ => None,
        })
        .collect()
}

fn output(events: &[InitEvent]) -> Vec<&str> {
    events
        .iter()
        .filter_map(|event| match event {
            InitEvent::CmdOutputLine { line, .. } => Some(line.as_str()),
            _ => None,
        })
        .collect()
}

fn failure(ran: &Ran) -> &CheckoutError {
    ran.1.as_ref().expect_err("a failure")
}

fn repo_error(ran: &Ran) -> &RepoError {
    match &failure(ran).cause {
        Cause::Repo(error) | Cause::Pin(PinError::Repo(error)) => error,
        other => panic!("not a repo error: {other:?}"),
    }
}

fn done(ran: &Ran) {
    assert!(ran.1.is_ok(), "{:?}", ran.1);
}

fn cloned() -> (Action, String) {
    (Action::Cloned, String::new())
}

fn exists() -> (Action, String) {
    (Action::Exists, String::new())
}

#[test]
fn an_absent_project_is_cloned_on_its_main_branch_and_then_exists() {
    let f = fixture();
    let git = Git::new(&f.tree).expect("git");
    let mut main = commit("main\n", None);
    main.branch = Some("main".to_owned());
    git.bare_remote("trunked", "trunk", &[commit("trunk\n", None), main])
        .expect("a remote");
    let url = url("trunked");
    let repo = Repo {
        url: Some(&url),
        branch: Some("main"),
        ..Repo::default()
    };

    let first = f.project("trunked", &repo);
    let second = f.project("trunked", &repo);

    let location = f.root.join("projects/trunked");
    done(&first);
    done(&second);
    assert_eq!(
        first.0,
        [InitEvent::RepoAction {
            repo: "trunked".to_owned(),
            location: location.display().to_string(),
            action: Action::Cloned,
            detail: String::new(),
        }]
    );
    assert_eq!(actions(&second.0), [exists()]);
    assert_eq!(
        f.git(&location, &["rev-parse", "--abbrev-ref", "HEAD"]),
        "main"
    );
}

#[test]
fn a_clone_of_an_empty_remote_exists() {
    let f = fixture();
    Git::new(&f.tree)
        .expect("git")
        .bare_remote("empty", "main", &[])
        .expect("a remote");
    let url = url("empty");
    let repo = Repo {
        url: Some(&url),
        ..Repo::default()
    };

    let first = f.project("empty", &repo);
    let second = f.project("empty", &repo);

    done(&second);
    assert_eq!(actions(&first.0), [cloned()]);
    assert_eq!(actions(&second.0), [exists()]);
}

#[test]
fn a_present_directory_that_is_not_the_clone_declared_fails_before_any_later_step() {
    let f = fixture();
    let url = url("api");
    let repo = Repo {
        url: Some(&url),
        cmd: &["echo ran"],
        excludes: &["x"],
        ..Repo::default()
    };
    done(&f.project(
        "api",
        &Repo {
            url: Some(&url),
            ..Repo::default()
        },
    ));
    std::fs::create_dir_all(f.root.join("projects/plain")).expect("a directory");
    std::fs::create_dir_all(f.root.join("projects/api/inside")).expect("a directory");
    std::fs::write(f.root.join("projects/file"), "").expect("a file");

    for (name, location) in [
        ("plain", "projects/plain"),
        ("inside", "projects/api/inside"),
        ("file", "projects/file"),
    ] {
        let ran = f.run_at(name, f.root.join(location), &repo, None);

        assert_eq!(failure(&ran).step, Step::Location, "{name}");
        assert!(
            matches!(repo_error(&ran).message, RepoMessage::NotARepository { .. }),
            "{name}"
        );
        assert!(ran.0.is_empty(), "{name}: {:?}", ran.0);
    }

    let other = format!("{url}x");
    let ran = f.project(
        "api",
        &Repo {
            url: Some(&other),
            ..repo
        },
    );

    assert_eq!(failure(&ran).step, Step::Location);
    assert!(matches!(
        &repo_error(&ran).message,
        RepoMessage::OriginMismatch { found: Some(found), .. } if *found == url
    ));
    assert!(ran.0.is_empty(), "{:?}", ran.0);
}

#[test]
fn an_absent_location_without_a_url_creates_nothing() {
    let f = fixture();
    let before = snapshot(&f.root);

    let ran = f.project("api", &Repo::default());

    assert!(matches!(
        repo_error(&ran).message,
        RepoMessage::NoUrl { .. }
    ));
    assert_eq!(snapshot(&f.root), before);
}

fn refused(f: &Fixture, entry: &StandaloneRepository) -> UnsafeReason {
    let url = url("api");
    let before = snapshot(f.tree.root());

    let ran = f.standalone(
        entry,
        &Repo {
            url: Some(&url),
            ..Repo::default()
        },
    );

    assert_eq!(failure(&ran).step, Step::Location);
    assert!(ran.0.is_empty(), "{:?}", ran.0);
    assert_eq!(snapshot(f.tree.root()), before, "{}", entry.path);
    match &repo_error(&ran).message {
        RepoMessage::UnsafeLocation { reason, .. } => reason.clone(),
        other => panic!("{other:?}"),
    }
}

#[test]
fn a_standalone_outside_the_workspace_or_in_a_reserved_directory_is_refused() {
    let f = fixture();
    let outside = f.tree.root().join("outside");
    std::fs::create_dir(&outside).expect("a directory");
    symlink(&outside, f.root.join("link")).expect("a link");

    // Resolved, this ends inside the workspace; creating the way to it would not.
    let through = format!(
        "../escape/../{}/lib",
        f.root.file_name().expect("a name").to_string_lossy()
    );
    for path in [through.as_str(), "area/../lib"] {
        assert_eq!(
            refused(&f, &standalone("lib", path, None)),
            UnsafeReason::OutsideWorkspace,
            "{path}"
        );
    }
    // The root itself is present.
    assert_eq!(
        refused(&f, &standalone("lib", ".", None)),
        UnsafeReason::OutsideWorkspace
    );
    assert_eq!(
        refused(&f, &standalone("lib", "link/lib", None)),
        UnsafeReason::OutsideWorkspace
    );
    for path in [".winter/lib", "projects/lib"] {
        assert_eq!(
            refused(&f, &standalone("lib", path, None)),
            UnsafeReason::ReservedDirectory,
            "{path}"
        );
    }
}

#[test]
fn a_standalone_that_overlaps_another_declared_repository_is_refused() {
    let mut f = fixture();
    f.standalones = vec![standalone("other", "area/other", None)];
    f.projects = vec![project("lib")];
    // The project `lib` leads to `shared` under the root.
    std::fs::create_dir_all(f.root.join("projects")).expect("a directory");
    std::fs::create_dir(f.root.join("shared")).expect("a directory");
    symlink(f.root.join("shared"), f.root.join("projects/lib")).expect("a link");
    let overlaps = |name: &str| UnsafeReason::Overlaps {
        name: name.to_owned(),
    };

    for path in ["area/other/lib", "area", "area/other"] {
        assert_eq!(
            refused(&f, &standalone("lib", path, None)),
            overlaps("other"),
            "{path}"
        );
    }
    assert_eq!(
        refused(&f, &standalone("lib", "shared/lib", None)),
        overlaps("lib")
    );
}

#[test]
fn a_declared_standalone_does_not_overlap_itself() {
    let mut f = fixture();
    let entry = standalone("lib", "area/lib", None);
    f.standalones = vec![entry.clone()];
    let url = url("api");

    let ran = f.standalone(
        &entry,
        &Repo {
            url: Some(&url),
            ..Repo::default()
        },
    );

    done(&ran);
    assert_eq!(actions(&ran.0), [cloned()]);
}

#[test]
fn identity_and_excludes_are_written_to_the_clone_once() {
    let mut f = fixture();
    f.identity = Some(GitIdentity {
        name: "Ada".to_owned(),
        email: "ada@example.invalid".to_owned(),
    });
    let url = url("api");
    let repo = Repo {
        url: Some(&url),
        excludes: &[" build/ ", "", "*.log", "build/"],
        ..Repo::default()
    };

    let first = f.project("api", &repo);
    let location = f.root.join("projects/api");
    let exclude = location.join(".git/info/exclude");
    let written = std::fs::read_to_string(&exclude).expect("an exclude file");
    let second = f.project("api", &repo);

    done(&first);
    done(&second);
    assert_eq!(f.git(&location, &["config", "--local", "user.name"]), "Ada");
    assert_eq!(
        f.git(&location, &["config", "--local", "user.email"]),
        "ada@example.invalid"
    );
    assert_eq!(written, "# winter-managed\nbuild/\n*.log\n");
    assert_eq!(
        actions(&first.0),
        [
            cloned(),
            (Action::ExcludesUpdated, "build/, *.log".to_owned())
        ]
    );
    assert_eq!(actions(&second.0), [exists()]);
    assert_eq!(
        std::fs::read_to_string(&exclude).expect("an exclude file"),
        written
    );
}

#[test]
fn the_excludes_of_a_worktree_go_to_the_common_exclude_file() {
    let f = fixture();
    let git = Git::new(&f.tree).expect("git");
    let main = f.root.join("main-clone");
    git.clone_remote("api", &main).expect("a clone");
    let worktree = f.root.join("tree");
    git.add_worktree(&main, &worktree, "side", "HEAD")
        .expect("a worktree");

    let ran = f.run_at(
        "tree",
        worktree,
        &Repo {
            excludes: &["build/"],
            ..Repo::default()
        },
        None,
    );

    done(&ran);
    assert_eq!(
        std::fs::read_to_string(main.join(".git/info/exclude")).expect("an exclude file"),
        "# winter-managed\nbuild/\n"
    );
}

#[test]
fn commands_run_in_the_location_with_the_added_environment() {
    let f = fixture_with(&[
        ("NO_COLOR", "0"),
        ("CI", "0"),
        ("LC_ALL", "POSIX"),
        ("LANGUAGE", "fr"),
        ("WINTER_LAUNCH_TIME", "1"),
    ]);
    let url = url("api");

    let ran = f.project(
        "api",
        &Repo {
            url: Some(&url),
            cmd: &[
                "pwd; echo \"$CI $NO_COLOR $LC_ALL $LANGUAGE ${WINTER_LAUNCH_TIME-unset}\"",
                "echo to-stderr >&2; printf unterminated",
            ],
            ..Repo::default()
        },
    );

    done(&ran);
    let location = f.root.join("projects/api").display().to_string();
    assert_eq!(
        output(&ran.0),
        [
            location.as_str(),
            "1 1 POSIX fr unset",
            "to-stderr",
            "unterminated"
        ]
    );
    let completed = ran
        .0
        .iter()
        .filter(|event| matches!(event, InitEvent::CmdCompleted { returncode: 0, .. }))
        .count();
    assert_eq!(completed, 2);
    assert_eq!(
        *f.programs.streamed.lock().expect("the calls"),
        [(Program::Shell, Stdin::Inherit, None); 2]
    );
}

#[test]
fn a_failing_command_skips_the_later_commands_and_the_pin() {
    let f = fixture();
    let url = url("api");
    let entry = standalone("lib", "", Some("v1"));

    let ran = f.standalone(
        &entry,
        &Repo {
            url: Some(&url),
            cmd: &["exit 3", "echo later"],
            ..Repo::default()
        },
    );

    assert_eq!(failure(&ran).step, Step::Cmd);
    assert!(matches!(
        repo_error(&ran).message,
        RepoMessage::CommandFailed { returncode: 3, .. }
    ));
    assert_eq!(
        ran.0[1..],
        [
            InitEvent::CmdStarted {
                repo: "lib".to_owned(),
                command: "exit 3".to_owned(),
            },
            InitEvent::CmdCompleted {
                repo: "lib".to_owned(),
                command: "exit 3".to_owned(),
                returncode: 3,
            },
        ]
    );
    assert_eq!(f.lock(), None);
}

#[test]
fn a_shell_that_cannot_be_started_fails_the_command() {
    let mut f = fixture();
    f.programs.cannot_stream = true;
    let url = url("api");

    let ran = f.project(
        "api",
        &Repo {
            url: Some(&url),
            cmd: &["true"],
            ..Repo::default()
        },
    );

    assert_eq!(failure(&ran).step, Step::Cmd);
    assert!(matches!(
        &repo_error(&ran).message,
        RepoMessage::CommandNotStarted { cause, .. } if cause == "no shell"
    ));
}

#[test]
fn a_nested_project_command_sees_nothing_of_the_enclosing_workspace() {
    // `/usr` stands in for a virtual environment, so that the pinned `PATH` already holds its `bin`.
    let f = fixture_with(&[
        ("WINTER_ENV", "alpha"),
        ("WINTER_LOG_LEVEL", "debug"),
        ("WINTER_OTEL_ENDPOINT", "kept"),
        ("VIRTUAL_ENV", "/usr"),
    ]);
    let url = url("api");

    let ran = f.project(
        "api",
        &Repo {
            url: Some(&url),
            cmd: &["env"],
            nested: true,
            ..Repo::default()
        },
    );

    done(&ran);
    let lines = output(&ran.0);
    let mut winter: Vec<&str> = lines
        .iter()
        .copied()
        .filter(|line| line.starts_with("WINTER_") || line.starts_with("VIRTUAL_ENV="))
        .collect();
    winter.sort_unstable();
    assert_eq!(
        winter,
        ["WINTER_LOG_LEVEL=debug", "WINTER_OTEL_ENDPOINT=kept"]
    );
    let path = lines
        .iter()
        .find_map(|line| line.strip_prefix("PATH="))
        .expect("a PATH");
    assert!(path.split(':').any(|entry| entry == "/bin"), "{path}");
    assert!(path.split(':').all(|entry| entry != "/usr/bin"), "{path}");
}

/// The standalone `lib` cloned at the first commit of `api`, whose id is answered, with a second commit on
/// the remote.
fn cloned_standalone(f: &Fixture) -> String {
    let url = url("api");
    let entry = standalone("lib", "", None);
    done(&f.standalone(
        &entry,
        &Repo {
            url: Some(&url),
            ..Repo::default()
        },
    ));
    let location = f.root.join("lib");
    let first = f.git(&location, &["rev-parse", "HEAD"]);
    Git::new(&f.tree)
        .expect("git")
        .push_to_remote("api", &commit("two\n", None))
        .expect("a push");
    f.git(&location, &["pull", "--quiet", "origin", "main"]);
    first
}

#[test]
fn a_recorded_tag_or_branch_is_checked_out_and_reported_pinned() {
    let f = fixture();
    let first = cloned_standalone(&f);
    let location = f.root.join("lib");
    let second = f.git(&location, &["rev-parse", "HEAD"]);

    f.record("lib", "v1", RefKind::Tag, &first);
    let tag = f.standalone(&standalone("lib", "", Some("v1")), &Repo::default());
    let at_tag = f.git(&location, &["rev-parse", "HEAD"]);
    f.record("lib", "main", RefKind::Branch, &second);
    let branch = f.standalone(&standalone("lib", "", Some("main")), &Repo::default());

    done(&tag);
    done(&branch);
    assert_eq!(at_tag, first);
    assert_eq!(
        actions(&tag.0),
        [exists(), (Action::Pinned, format!("tag {}", &first[..8]))]
    );
    assert_eq!(
        actions(&branch.0),
        [
            exists(),
            (Action::Pinned, format!("branch {}", &second[..8]))
        ]
    );
    assert_eq!(
        f.git(&location, &["rev-parse", "--abbrev-ref", "HEAD"]),
        "main"
    );
}

#[test]
fn a_dirty_checkout_with_a_stale_record_refuses_the_pin() {
    let f = fixture();
    let first = cloned_standalone(&f);
    f.record("lib", "old", RefKind::Tag, &first);
    std::fs::write(f.root.join("lib/a.txt"), "changed\n").expect("a change");

    let ran = f.standalone(&standalone("lib", "", Some("v1")), &Repo::default());

    assert_eq!(failure(&ran).step, Step::Pin);
    assert!(matches!(
        repo_error(&ran).message,
        RepoMessage::DirtyRePin { .. }
    ));
    assert_eq!(actions(&ran.0), [exists()]);
}

#[test]
fn a_malformed_lock_fails_the_pin_with_its_text_and_is_left_alone() {
    let f = fixture();
    cloned_standalone(&f);
    std::fs::create_dir_all(f.root.join(".winter")).expect("a directory");
    std::fs::write(f.root.join(".winter/config.lock"), "not = [toml").expect("a lock");
    let expected = read_lock(&OsFiles, &f.root)
        .expect_err("a malformed lock")
        .to_string();

    let ran = f.standalone(&standalone("lib", "", Some("v1")), &Repo::default());

    assert_eq!(
        failure(&ran).to_string(),
        format!("{expected} (failed at pin)")
    );
    assert_eq!(f.lock().as_deref(), Some(&b"not = [toml"[..]));
}

#[test]
fn a_standalone_that_fails_its_pin_leaves_the_record_of_another() {
    let f = fixture();
    let url = url("api");
    let repo = Repo {
        url: Some(&url),
        ..Repo::default()
    };
    done(&f.standalone(&standalone("one", "", Some("v1")), &repo));
    let recorded = f.lock().expect("a lock");

    let ran = f.standalone(&standalone("two", "", Some("absent")), &repo);

    assert_eq!(failure(&ran).step, Step::Pin);
    assert_eq!(f.lock().expect("a lock"), recorded);
}

#[test]
fn a_present_standalone_is_verified_and_never_confined() {
    let mut f = fixture();
    f.standalones = vec![standalone("other", "area", None)];
    let git = Git::new(&f.tree).expect("git");
    let url = url("api");
    let repo = Repo {
        url: Some(&url),
        ..Repo::default()
    };

    for path in [".winter/ext/lib", "area/lib"] {
        let location = f.root.join(path);
        std::fs::create_dir_all(location.parent().expect("a parent")).expect("a directory");
        git.clone_remote("api", &location).expect("a clone");

        let ran = f.standalone(&standalone("lib", path, None), &repo);

        done(&ran);
        assert_eq!(actions(&ran.0), [exists()], "{path}");
    }
}

#[test]
fn a_mismatch_carries_the_git_call_it_rests_on() {
    let f = fixture();
    let url = url("api");
    let repo = Repo {
        url: Some(&url),
        ..Repo::default()
    };
    let own = f.root.join("projects/own");
    Git::new(&f.tree)
        .expect("git")
        .init(&own, "main")
        .expect("an init");
    std::fs::create_dir_all(f.root.join("projects/plain")).expect("a directory");

    let unset = f.project("own", &repo);
    let plain = f.project("plain", &repo);

    assert_eq!(failure(&unset).step, Step::Location);
    let error = repo_error(&unset);
    assert!(matches!(
        error.message,
        RepoMessage::OriginMismatch { found: None, .. }
    ));
    assert_eq!(error.subcommand.as_deref(), Some("config"));
    assert_eq!(error.exit, Some(1));
    assert!(unset.0.is_empty(), "{:?}", unset.0);

    let error = repo_error(&plain);
    assert!(matches!(error.message, RepoMessage::NotARepository { .. }));
    assert_eq!(error.subcommand.as_deref(), Some("rev-parse"));
    let stderr = error.stderr.as_deref().expect("what git said");
    assert!(failure(&plain).to_string().contains(stderr), "{stderr}");
}

#[test]
fn a_clone_of_a_relative_url_exists_on_the_next_run() {
    let f = fixture();
    let repo = Repo {
        url: Some("../remotes/api.git"),
        ..Repo::default()
    };

    let first = f.project("api", &repo);
    let second = f.project("api", &repo);

    done(&first);
    done(&second);
    assert_eq!(actions(&second.0), [exists()]);
}

#[test]
fn a_failing_identity_or_excludes_step_is_named_and_ends_the_run() {
    let mut f = fixture();
    let url = url("api");
    let repo = Repo {
        url: Some(&url),
        excludes: &["build/"],
        cmd: &["echo ran"],
        ..Repo::default()
    };
    done(&f.project(
        "api",
        &Repo {
            url: Some(&url),
            ..Repo::default()
        },
    ));
    let git_dir = f.root.join("projects/api/.git");

    // A directory where the exclude file belongs cannot be read.
    std::fs::create_dir_all(git_dir.join("info/exclude")).expect("a directory");
    let unwritable = f.project("api", &repo);

    assert_eq!(failure(&unwritable).step, Step::Excludes);
    assert!(matches!(failure(&unwritable).cause, Cause::Io(_)));
    assert_eq!(actions(&unwritable.0), [exists()]);
    assert_eq!(output(&unwritable.0), Vec::<&str>::new());

    f.identity = Some(GitIdentity {
        name: "Ada".to_owned(),
        email: "ada@example.invalid".to_owned(),
    });
    std::fs::write(git_dir.join("config.lock"), "").expect("a held lock");
    let locked = f.project("api", &repo);

    assert_eq!(failure(&locked).step, Step::Identity);
    assert_eq!(actions(&locked.0), [exists()]);
}

#[test]
fn a_work_tree_without_a_dot_git_has_no_exclude_file() {
    let f = fixture();
    let holder = f.root.join("holder");
    let inner = holder.join("inner");
    Git::new(&f.tree)
        .expect("git")
        .init(&holder, "main")
        .expect("an init");
    std::fs::create_dir(&inner).expect("a directory");
    let worktree = inner.display().to_string();
    f.git(&holder, &["config", "core.worktree", &worktree]);

    let ran = f.run_at(
        "inner",
        inner,
        &Repo {
            excludes: &["build/"],
            ..Repo::default()
        },
        None,
    );

    assert_eq!(failure(&ran).step, Step::Excludes);
    assert!(matches!(
        repo_error(&ran).message,
        RepoMessage::NoExcludeFile { .. }
    ));
}

#[test]
fn a_short_recorded_commit_is_reported_as_it_is() {
    let f = fixture();
    let first = cloned_standalone(&f);
    let short = &first[..6];
    f.record("lib", "v1", RefKind::Commit, short);

    let ran = f.standalone(&standalone("lib", "", Some("v1")), &Repo::default());

    done(&ran);
    assert_eq!(
        actions(&ran.0),
        [exists(), (Action::Pinned, format!("commit {short}"))]
    );
}

#[test]
fn output_that_is_no_utf8_is_decoded_with_replacements() {
    let f = fixture();
    let url = url("api");

    let ran = f.project(
        "api",
        &Repo {
            url: Some(&url),
            cmd: &["printf 'a\\377b\\n'"],
            ..Repo::default()
        },
    );

    done(&ran);
    assert_eq!(output(&ran.0), ["a\u{fffd}b"]);
}
