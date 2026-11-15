//! `cargo dev record`, run with a stand-in for the reference tool: a shell script in a git checkout the test
//! makes inside a tree of its own. Nothing here starts the reference tool, and no test changes the
//! environment of its process: the command is given its reference, its `PATH`, and its scenarios root.

use std::collections::{BTreeMap, BTreeSet};
use std::ffi::{OsStr, OsString};
use std::fs::Permissions;
use std::os::unix::fs::PermissionsExt;
use std::panic::AssertUnwindSafe;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use winter_workspace_dev::record::{self, REFERENCE_VARIABLE, Refusal, Request, Verdict, refusals};
use winter_workspace_test_support::scenario::Meta;
use winter_workspace_test_support::{CommitSpec, Exit, Git, TempTree, invoke};

/// The id of the scenario every stage holds.
const ID: &str = "ws-index/basic";

/// A second scenario, which a test adds.
const OTHER: &str = "ws-index/other";

/// A commit no stand-in checkout is at.
const ELSEWHERE: &str = "b57b020e99e10a04d8bb621d0b03ddb2c983a090";

/// One step, whose argument the stand-in is given.
const ONE_STEP: &str = "[[step]]\nargs = [\"index\"]\n";

/// What the stand-in prints for its version, before a second line that is not stored.
const VERSION: &str = "stand-in 1.2.3";

/// Names the fixture a child test process was started to run. A fixture does nothing without its own name here.
const FIXTURE_VARIABLE: &str = "WINTER_TEST_FIXTURE";

/// Names the scenarios root a fixture runs the command over.
const ROOT_VARIABLE: &str = "WINTER_TEST_RECORD_ROOT";

/// Names the stand-in a fixture passes as the reference; unset, the fixture names none.
const STAND_IN_VARIABLE: &str = "WINTER_TEST_RECORD_STAND_IN";

/// Names the directory a fixture writes the exit code and the report of the command to.
const OUT_VARIABLE: &str = "WINTER_TEST_RECORD_OUT";

/// Set, it has a fixture ask for both trees of the record to be kept.
const KEEP_VARIABLE: &str = "WINTER_TEST_RECORD_KEEP";

/// Set, it has a fixture ask for the scenario to be recorded again.
const AGAIN_VARIABLE: &str = "WINTER_TEST_RECORD_AGAIN";

/// What a fixture writes as the exit code of a command that panicked.
const PANICKED: u8 = 101;

/// The test of this file that runs the command in a child test process, whose trees are made in a directory
/// the test that started it reads.
const FIXTURE: &str = "fixture_the_command_runs_where_its_trees_are_seen";

/// The directory of the first `git` on the `PATH` of this process.
fn directory_of_git() -> PathBuf {
    let path = std::env::var_os("PATH").expect("a PATH");
    std::env::split_paths(&path)
        .find(|directory| directory.is_absolute() && directory.join("git").is_file())
        .expect("git on PATH")
}

fn text(path: &Path) -> &str {
    path.to_str().expect("a path that is text")
}

fn write(path: &Path, content: &str) {
    std::fs::create_dir_all(path.parent().expect("a file has a parent")).expect("directories");
    std::fs::write(path, content).unwrap_or_else(|error| panic!("{}: {error}", path.display()));
}

/// Writes `content` to `path` as a file that can be started at once: the test process writes a file
/// beside it, and `/bin/cp`, a child of its own under the environment of `host`, makes `path` from that.
///
/// The process that starts a file must never have had it open for writing. A test thread that starts a
/// child duplicates every descriptor of the process until that child is another program, and a file some
/// process holds open for writing is not started: `Text file busy`. The descriptor `path` is written
/// through exists only in `/bin/cp`, which has ended when this returns.
fn executable(host: &TempTree, path: &Path, content: &str) {
    let mut source = path.as_os_str().to_owned();
    source.push(".text");
    let source = PathBuf::from(source);
    write(&source, content);
    let mut copy = host.invocation(Path::new("/bin/cp"));
    copy.args = vec![source.clone().into(), path.into()];
    let copied = invoke(copy).expect("a copy");
    assert_eq!(copied.exit, Exit::Code(0), "{copied:?}");
    std::fs::remove_file(&source).expect("the file beside it is removed");
    std::fs::set_permissions(path, Permissions::from_mode(0o755)).expect("an executable file");
}

/// Every file under `directory` with its bytes, by its path from `directory`.
fn files(directory: &Path) -> BTreeMap<String, Vec<u8>> {
    let mut found = BTreeMap::new();
    let mut pending = vec![directory.to_path_buf()];
    while let Some(next) = pending.pop() {
        for entry in std::fs::read_dir(&next).expect("a directory") {
            let entry = entry.expect("an entry");
            if entry.file_type().expect("a file type").is_dir() {
                pending.push(entry.path());
            } else {
                let relative = entry.path();
                let relative = relative
                    .strip_prefix(directory)
                    .expect("below the directory");
                let content = std::fs::read(entry.path()).unwrap_or_default();
                found.insert(text(relative).to_owned(), content);
            }
        }
    }
    found
}

/// The names of the entries of `directory`.
fn entries(directory: &Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(directory)
        .expect("a directory")
        .map(|entry| text(&entry.expect("an entry").path()).to_owned())
        .collect();
    names.sort();
    names
}

/// What a run of the command answered and reported.
#[derive(Debug)]
struct Outcome {
    code: u8,
    report: String,
}

impl Outcome {
    fn has_line(&self, line: &str) -> bool {
        self.report.lines().any(|reported| reported == line)
    }
}

/// A tree that holds a stand-in checkout and a scenarios root pinned to its commit, with one scenario.
struct Stage {
    host: TempTree,
    checkout: PathBuf,
    /// The stand-in: `bin/winter` of the checkout.
    stand_in: PathBuf,
    /// The text of the stand-in.
    script: String,
    commit: String,
    root: PathBuf,
}

impl Stage {
    /// A stage whose stand-in runs `body` for every call that does not ask for its version. In `body`,
    /// `@CHECKOUT@` is the stand-in checkout and `@HOST@` a directory outside it and outside every tree a
    /// step runs in.
    fn new(body: &str) -> Self {
        Self::with_root(body, "tmp/scenarios", None)
    }

    /// As [`Stage::new`], with the scenarios root at `root` of the tree, inside the git repository made at
    /// `repository` of the tree when one is named.
    fn with_root(body: &str, root: &str, repository: Option<&str>) -> Self {
        Self::telling(VERSION, body, root, repository)
    }

    /// As [`Stage::with_root`], with a stand-in that prints `version` as the first line for its version.
    fn telling(version: &str, body: &str, root: &str, repository: Option<&str>) -> Self {
        let host = TempTree::new().expect("a tree for the stage");
        let checkout = host.root().join("ws/checkout");
        let outside = host.root().join("cache");
        let script = format!(
            "#!/bin/sh\necho run >> {marker}\n\
             if [ \"$1\" = \"--version\" ]; then echo \"{version}\"; echo more; exit 0; fi\n{body}\n",
            marker = text(&outside.join("marker")),
        )
        .replace("@CHECKOUT@", text(&checkout))
        .replace("@HOST@", text(&outside));
        let stand_in = checkout.join("bin/winter");
        let commit = {
            let git = Git::new(&host).expect("git");
            git.init(&checkout, "main").expect("the stand-in checkout");
            if let Some(repository) = repository {
                git.init(&host.root().join(repository), "main")
                    .expect("the repository of the scenarios");
            }
            executable(&host, &stand_in, &script);
            write(&checkout.join("tracked.txt"), "tracked\n");
            write(&checkout.join(".gitignore"), "ignored/\n");
            let spec = CommitSpec {
                message: "stand-in".to_owned(),
                ..CommitSpec::default()
            };
            git.commit(&checkout, &spec).expect("a commit")
        };
        let root = host.root().join(root);
        write(
            &root.join("reference.toml"),
            &format!("commit = \"{commit}\"\n"),
        );
        let stage = Self {
            host,
            checkout,
            stand_in,
            script,
            commit,
            root,
        };
        stage.scenario(ID, ONE_STEP);
        stage
    }

    /// Writes the scenario `id` with `description` after its summary.
    fn scenario(&self, id: &str, description: &str) {
        let description = format!("summary = \"s\"\n{description}");
        write(&self.dir(id).join("scenario.toml"), &description);
    }

    fn dir(&self, id: &str) -> PathBuf {
        self.root.join(id)
    }

    fn has_record(&self, id: &str) -> bool {
        self.dir(id).join("record").symlink_metadata().is_ok()
    }

    /// The bytes of `path` of the record of [`ID`].
    fn stored(&self, path: &str) -> Vec<u8> {
        let file = self.dir(ID).join("record").join(path);
        std::fs::read(&file).unwrap_or_else(|error| panic!("{}: {error}", file.display()))
    }

    /// A directory outside the checkout and outside every tree a step runs in.
    fn outside(&self) -> PathBuf {
        self.host.root().join("cache")
    }

    /// Whether the stand-in was started, for its version or for a step.
    fn started(&self) -> bool {
        self.outside().join("marker").exists()
    }

    /// An untracked file and an ignored directory with a file in it, as a checkout holds them before a run.
    fn litter(&self) {
        write(&self.checkout.join("untracked.txt"), "untracked\n");
        write(&self.checkout.join("ignored/cache.bin"), "cache\n");
    }

    /// A `git` that writes the arguments of every call to a file and then runs git, in a directory of its
    /// own. Answers that directory and the file.
    fn recording_git(&self) -> (PathBuf, PathBuf) {
        let directory = self.outside().join("recorder");
        let log = self.recording_git_in(&directory);
        (directory, log)
    }

    /// As [`Stage::recording_git`], in `directory`. Answers the file.
    fn recording_git_in(&self, directory: &Path) -> PathBuf {
        let log = self.outside().join("git-calls");
        let script = format!(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> {}\nexec {} \"$@\"\n",
            text(&log),
            text(&directory_of_git().join("git"))
        );
        executable(&self.host, &directory.join("git"), &script);
        log
    }

    /// Runs the command with `args` and the stand-in as `--reference`.
    fn run(&self, args: &[&str]) -> Outcome {
        let mut args = args.to_vec();
        args.extend(["--reference", text(&self.stand_in)]);
        self.run_with(&args, directory_of_git().as_os_str(), None)
    }

    /// Runs the command with `args`, `path` as the caller's `PATH`, and `variable` as the value of the
    /// variable that names the reference.
    fn run_with(&self, args: &[&str], path: &OsStr, variable: Option<&Path>) -> Outcome {
        let args: Vec<OsString> = args.iter().map(OsString::from).collect();
        let env = |key: &str| match key {
            "PATH" => Some(path.to_owned()),
            REFERENCE_VARIABLE => variable.map(OsString::from),
            _ => None,
        };
        let mut report = Vec::new();
        let code = record::run(&args, &env, &self.root, &mut report);
        Outcome {
            code,
            report: String::from_utf8(report).expect("a report that is text"),
        }
    }

    /// Runs the command for [`ID`] in a child test process whose trees are made in a new directory of this
    /// stage, with the stand-in as its reference when `named`. Answers the outcome and the entries of that
    /// directory.
    fn run_in_child(&self, named: bool) -> (Outcome, Vec<String>) {
        let (outcome, trees, _) = self.run_in_child_keeping(named, false);
        (outcome, trees)
    }

    /// As [`Stage::run_in_child`], asking for both trees of the record to be kept when `keep`. Answers also
    /// what the child process wrote to its stderr.
    fn run_in_child_keeping(&self, named: bool, keep: bool) -> (Outcome, Vec<String>, String) {
        let variables: &[&str] = if keep { &[KEEP_VARIABLE] } else { &[] };
        self.run_in_child_with(named, variables)
    }

    /// As [`Stage::run_in_child`], with every one of `variables` set for the child. Answers also what the
    /// child process wrote to its stderr.
    fn run_in_child_with(&self, named: bool, variables: &[&str]) -> (Outcome, Vec<String>, String) {
        let trees = self.host.root().join("tmp/trees");
        let out = self.host.root().join("tmp/out");
        for directory in [&trees, &out] {
            std::fs::create_dir_all(directory).expect("a directory inside the stage");
        }
        let this_test_binary = std::env::current_exe().expect("the path of the test executable");
        let mut invocation = self.host.invocation(&this_test_binary);
        invocation.args = [FIXTURE, "--ignored", "--exact", "--nocapture"]
            .iter()
            .map(Into::into)
            .collect();
        invocation.env.set("WINTER_TEST_TMPDIR", text(&trees));
        invocation.env.set(FIXTURE_VARIABLE, FIXTURE);
        invocation.env.set(ROOT_VARIABLE, text(&self.root));
        invocation.env.set(OUT_VARIABLE, text(&out));
        if named {
            invocation.env.set(STAND_IN_VARIABLE, text(&self.stand_in));
        }
        for variable in variables {
            invocation.env.set(variable, "1");
        }
        let observation = invoke(invocation).expect("a child test run");
        assert_eq!(observation.exit, Exit::Code(0), "{observation:?}");
        let read =
            |name: &str| std::fs::read_to_string(out.join(name)).expect("what the child wrote");
        let outcome = Outcome {
            code: read("code").parse().expect("an exit code"),
            report: read("report"),
        };
        let stderr = String::from_utf8_lossy(&observation.stderr).into_owned();
        (outcome, entries(&trees), stderr)
    }
}

/// Asserts that the run was refused with a report that holds every one of `named`, and that the stand-in
/// was never started.
#[track_caller]
fn assert_refused(stage: &Stage, outcome: &Outcome, named: &[&str]) {
    assert_eq!(outcome.code, 1, "{outcome:?}");
    for named in named {
        assert!(outcome.report.contains(named), "{named}: {outcome:?}");
    }
    assert!(!stage.started(), "the stand-in was started: {outcome:?}");
}

#[test]
#[ignore = "runs the command for the test that starts it in a child process"]
fn fixture_the_command_runs_where_its_trees_are_seen() {
    if std::env::var_os(FIXTURE_VARIABLE).is_none_or(|fixture| fixture != FIXTURE) {
        return;
    }
    let variable = |name: &str| std::env::var_os(name);
    let root = PathBuf::from(variable(ROOT_VARIABLE).expect("a scenarios root"));
    let out = PathBuf::from(variable(OUT_VARIABLE).expect("a directory to write to"));
    let env = |key: &str| match key {
        "PATH" => variable("PATH"),
        REFERENCE_VARIABLE => variable(STAND_IN_VARIABLE),
        _ => None,
    };
    let mut report = Vec::new();

    let mut args = vec![OsString::from(ID)];
    if variable(KEEP_VARIABLE).is_some() {
        args.push(OsString::from("--keep"));
    }

    if variable(AGAIN_VARIABLE).is_some() {
        args.push(OsString::from("--rerecord"));
    }

    let ran = AssertUnwindSafe(|| record::run(&args, &env, &root, &mut report));
    let code = std::panic::catch_unwind(ran).unwrap_or(PANICKED);

    std::fs::write(out.join("code"), code.to_string()).expect("the exit code is written");
    std::fs::write(out.join("report"), report).expect("the report is written");
}

#[test]
fn a_run_that_names_no_reference_is_an_argument_error_and_makes_no_tree() {
    let stage = Stage::new("");

    let (outcome, trees) = stage.run_in_child(false);

    assert_eq!(outcome.code, 2, "{outcome:?}");
    assert!(outcome.report.contains(REFERENCE_VARIABLE), "{outcome:?}");
    assert_eq!(trees, [] as [&str; 0]);
    assert!(!stage.started());
    assert!(!stage.has_record(ID));
}

#[test]
fn arguments_that_do_not_read_are_an_argument_error_and_start_nothing() {
    let stage = Stage::new("");

    for args in [
        &["--all-missing", "--rerecord"][..],
        &["--all-missing", ID],
        &[],
        &["--unknown", ID],
        &[ID, ID],
        &[ID, OTHER, "--rerecord", ID],
    ] {
        let outcome = stage.run(args);

        assert_eq!(outcome.code, 2, "{args:?}: {outcome:?}");
        assert!(outcome.report.contains("usage:"), "{outcome:?}");
    }
    assert!(!stage.started());
    assert!(!stage.has_record(ID));
}

#[test]
fn a_relative_reference_path_is_refused() {
    let stage = Stage::new("");
    let path = directory_of_git();

    let outcome = stage.run_with(&[ID, "--reference", "bin/winter"], path.as_os_str(), None);

    assert_refused(&stage, &outcome, &["bin/winter", "is relative"]);
}

#[test]
fn a_missing_reference_is_refused() {
    let stage = Stage::new("");
    let absent = stage.checkout.join("bin/absent");
    let path = directory_of_git();

    let outcome = stage.run_with(&[ID], path.as_os_str(), Some(&absent));

    assert_refused(&stage, &outcome, &[text(&absent), "is missing"]);
}

#[test]
fn a_reference_that_is_not_executable_is_refused() {
    let stage = Stage::new("");
    let plain = stage.outside().join("plain/winter");
    write(&plain, &stage.script);
    let path = directory_of_git();

    let outcome = stage.run_with(&[ID], path.as_os_str(), Some(&plain));

    assert_refused(
        &stage,
        &outcome,
        &[text(&plain), "is not an executable file"],
    );
}

#[test]
fn a_reference_that_is_the_first_winter_on_the_path_is_refused() {
    let stage = Stage::new("");
    let path = std::env::join_paths([stage.checkout.join("bin"), directory_of_git()]).unwrap();

    let outcome = stage.run_with(&[ID], &path, Some(&stage.stand_in));

    assert_refused(
        &stage,
        &outcome,
        &[text(&stage.stand_in), "the first `winter` on PATH"],
    );
}

#[test]
fn a_reference_the_first_winter_on_the_path_links_to_is_refused() {
    let stage = Stage::new("");
    let links = stage.outside().join("links");
    std::fs::create_dir_all(&links).unwrap();
    std::os::unix::fs::symlink(&stage.stand_in, links.join("winter")).unwrap();
    let path = std::env::join_paths([links.clone(), directory_of_git()]).unwrap();

    let outcome = stage.run_with(&[ID], &path, Some(&stage.stand_in));

    assert_refused(
        &stage,
        &outcome,
        &[
            text(&links.join("winter")),
            "the first `winter` on PATH",
            "never run",
        ],
    );
}

#[test]
fn a_reference_that_is_not_the_first_winter_on_the_path_is_not_refused_for_it() {
    let stage = Stage::new("");
    let first = stage.outside().join("first");
    executable(&stage.host, &first.join("winter"), "#!/bin/sh\nexit 0\n");
    let path =
        std::env::join_paths([first, stage.checkout.join("bin"), directory_of_git()]).unwrap();

    let outcome = stage.run_with(&[ID], &path, Some(&stage.stand_in));

    assert_eq!(outcome.code, 0, "{outcome:?}");
}

#[test]
fn a_reference_with_no_git_entry_above_it_is_refused() {
    let stage = Stage::new("");
    let loose = stage.outside().join("loose/bin/winter");
    executable(&stage.host, &loose, &stage.script);

    let outcome = stage.run_with(&[ID], directory_of_git().as_os_str(), Some(&loose));

    assert_refused(&stage, &outcome, &[text(&loose), ".git"]);
}

#[test]
fn a_checkout_under_a_directory_whose_name_holds_a_space_is_refused() {
    let stage = Stage::new("");
    let checkout = stage.outside().join("with space/checkout");
    std::fs::create_dir_all(checkout.join(".git")).unwrap();
    let spaced = checkout.join("bin/winter");
    executable(&stage.host, &spaced, &stage.script);

    let outcome = stage.run_with(&[ID], directory_of_git().as_os_str(), Some(&spaced));

    assert_refused(
        &stage,
        &outcome,
        &["with space/checkout", "outside [A-Za-z0-9/._-]"],
    );
}

#[test]
fn a_checkout_at_another_commit_than_the_pinned_one_is_refused() {
    let stage = Stage::new("");
    write(
        &stage.root.join("reference.toml"),
        &format!("commit = \"{ELSEWHERE}\"\n"),
    );

    let outcome = stage.run(&[ID]);

    assert_refused(&stage, &outcome, &[&stage.commit, ELSEWHERE]);
}

#[test]
fn a_checkout_with_a_modified_tracked_file_is_refused() {
    let stage = Stage::new("");
    write(&stage.checkout.join("tracked.txt"), "tracked\nand more\n");

    let outcome = stage.run(&[ID]);

    assert_refused(&stage, &outcome, &["modified tracked files", "tracked.txt"]);
}

#[test]
fn a_scenario_that_has_a_record_is_refused_unless_it_is_recorded_again() {
    let stage = Stage::new("");
    write(&stage.dir(ID).join("record/placed-by-the-test"), "");

    let outcome = stage.run(&[ID]);

    assert_refused(&stage, &outcome, &[ID, "record/", "--rerecord"]);

    let outcome = stage.run(&[ID, "--rerecord"]);

    assert_eq!(outcome.code, 0, "{outcome:?}");
    assert!(!stage.dir(ID).join("record/placed-by-the-test").exists());
    assert!(stage.dir(ID).join("record/meta.toml").exists());
}

#[test]
fn a_scenario_with_what_a_run_that_did_not_end_left_is_refused_and_keeps_it() {
    let stage = Stage::new("");
    // What a run leaves that is killed while the record it replaces is set aside.
    write(
        &stage.dir(ID).join("record-Ab12Cd/record/meta.toml"),
        "kept",
    );
    let before = files(&stage.dir(ID));

    for args in [&[ID][..], &[ID, "--rerecord"], &["--all-missing"]] {
        let outcome = stage.run(args);

        let named = [ID, "record-Ab12Cd/, which", "is record-Ab12Cd/record"];
        assert_refused(&stage, &outcome, &named);
        assert_eq!(files(&stage.dir(ID)), before, "{args:?}");
    }
}

#[test]
fn a_scenario_with_a_stub_is_refused() {
    let stage = Stage::new("");
    let description = format!("{ONE_STEP}[[stub]]\nname = \"docker\"\nexit = 0\nenv = []\n");
    stage.scenario(ID, &description);

    let outcome = stage.run(&[ID]);

    assert_refused(&stage, &outcome, &[ID, "stub"]);
}

/// Asserts that the scenario of a stage described by `description` is refused as not confined at `key`,
/// with nothing started, no record, and no tree.
#[track_caller]
fn assert_not_confined(description: &str, key: &str) {
    let stage = Stage::new("");
    stage.scenario(ID, description);

    let (outcome, trees) = stage.run_in_child(true);

    assert_refused(&stage, &outcome, &[ID, key]);
    assert!(
        outcome.report.contains(&format!("`{key}`")) || outcome.report.contains(&format!("{key}:")),
        "{outcome:?}"
    );
    assert!(!stage.has_record(ID));
    assert_eq!(trees, [] as [&str; 0]);
}

#[test]
fn a_step_whose_working_directory_is_an_absolute_path_is_refused_before_anything_is_started() {
    assert_not_confined(&format!("{ONE_STEP}cwd = \"/tmp\"\n"), "cwd");
}

#[test]
fn a_step_whose_working_directory_leaves_the_tree_is_refused_before_anything_is_started() {
    assert_not_confined(&format!("{ONE_STEP}cwd = \"../..\"\n"), "cwd");
}

#[test]
fn a_checkout_whose_path_leaves_the_tree_is_refused_before_anything_is_started() {
    let description = format!(
        "[[fixture.remote]]\nname = \"alpha\"\n[[fixture.remote.commit]]\nmessage = \"m\"\n\
         files = {{ \"a\" = \"a\" }}\n\
         [[fixture.checkout]]\nremote = \"alpha\"\npath = \"../outside\"\n{ONE_STEP}"
    );

    assert_not_confined(&description, "path");
}

#[test]
fn a_step_that_sets_a_variable_git_reads_is_refused_before_anything_is_started() {
    assert_not_confined(&format!("{ONE_STEP}env = {{ GIT_DIR = \"x\" }}\n"), "env");
}

#[test]
fn a_step_whose_option_names_an_absolute_path_is_refused_before_anything_is_started() {
    let description = "[[step]]\nargs = [\"index\", \"--out=/tmp/x\"]\n";

    assert_not_confined(description, "args");
}

#[test]
fn a_step_that_sets_a_variable_the_reference_is_given_is_refused_before_anything_is_started() {
    let description = format!("{ONE_STEP}env = {{ PYTHONHASHSEED = \"7\" }}\n");

    assert_not_confined(&description, "env");
}

#[test]
fn both_trees_of_a_record_are_left_and_named_when_they_are_kept() {
    let stage = Stage::new("echo recorded-step");

    let (outcome, trees, stderr) = stage.run_in_child_keeping(true, true);

    assert_eq!(outcome.code, 0, "{outcome:?}");
    assert!(stage.has_record(ID));
    assert_eq!(trees.len(), 2, "{trees:?}");
    for tree in &trees {
        let line = format!("kept the isolated tree {tree}");
        assert!(stderr.lines().any(|written| written == line), "{stderr}");
        assert!(Path::new(tree).join("ws").is_dir(), "{tree}");
    }

    let stage = Stage::new("echo recorded-step");
    let (outcome, trees, stderr) = stage.run_in_child_keeping(true, false);
    assert_eq!(outcome.code, 0, "{outcome:?}");
    assert_eq!(trees, [] as [&str; 0]);
    assert!(!stderr.contains("kept the isolated tree"), "{stderr}");
}

#[test]
fn a_scenario_is_recorded_with_the_commit_and_the_version_line_of_the_stand_in() {
    let stage = Stage::new("echo recorded-step");

    let outcome = stage.run_with(&[ID], directory_of_git().as_os_str(), Some(&stage.stand_in));

    assert_eq!(outcome.code, 0, "{outcome:?}");
    assert!(outcome.has_line(&format!("recorded {ID}")), "{outcome:?}");
    let meta = Meta::load(&stage.dir(ID)).expect("a meta.toml that reads");
    assert_eq!(meta.reference_commit, stage.commit);
    assert_eq!(meta.reference_runtime, VERSION);
    assert_eq!(stage.stored("steps/01/stdout"), b"recorded-step\n");
    assert_eq!(stage.stored("steps/01/exit-code"), b"0\n");
}

#[test]
fn the_version_line_is_stored_as_output_is_with_the_tree_and_the_checkout_replaced() {
    let version = "stand-in of @CHECKOUT@ in $PWD";
    let stage = Stage::telling(version, "echo recorded-step", "tmp/scenarios", None);

    let outcome = stage.run(&[ID]);

    assert_eq!(outcome.code, 0, "{outcome:?}");
    let meta = Meta::load(&stage.dir(ID)).expect("a meta.toml that reads");
    assert_eq!(
        meta.reference_runtime,
        "stand-in of <REFERENCE> in <ROOT>/ws"
    );
}

#[test]
fn the_reference_is_started_by_one_spelling_whatever_path_names_it() {
    for through_a_directory in [true, false] {
        let stage = Stage::new("echo \"$0\" >&2\necho \"$PATH\"");
        let links = stage.outside().join("links-of-the-test");
        std::fs::create_dir_all(&links).expect("a directory outside the checkout");
        // The checkout behind a link to it, and the executable behind a link outside the checkout.
        let named = if through_a_directory {
            std::os::unix::fs::symlink(&stage.checkout, links.join("tool")).expect("a link");
            links.join("tool/bin/winter")
        } else {
            std::os::unix::fs::symlink(&stage.stand_in, links.join("winter")).expect("a link");
            links.join("winter")
        };

        let outcome = stage.run_with(
            &[ID, "--reference", text(&named)],
            directory_of_git().as_os_str(),
            None,
        );

        assert_eq!(outcome.code, 0, "{outcome:?}");
        assert_eq!(stage.stored("steps/01/stderr"), b"<REFERENCE>/bin/winter\n");
        let search = String::from_utf8(stage.stored("steps/01/stdout")).expect("text");
        assert!(search.contains(":<REFERENCE>/bin:"), "{search}");
        for (path, content) in files(&stage.dir(ID).join("record")) {
            let content = String::from_utf8_lossy(&content);
            assert!(!content.contains("links-of-the-test"), "{path}: {content}");
        }
    }
}

#[test]
fn the_path_of_the_checkout_is_stored_as_the_reference_token_and_the_checkout_is_left_as_it_was() {
    for touched in [false, true] {
        let stage = Stage::new("echo \"trace: @CHECKOUT@/lib/core line 3\" >&2");
        let tracked = stage.checkout.join("tracked.txt");
        if touched {
            let earlier = SystemTime::now() - Duration::from_secs(3600);
            let file = std::fs::File::options().write(true).open(&tracked).unwrap();
            file.set_modified(earlier).unwrap();
        }
        let index = stage.checkout.join(".git/index");
        let state = || {
            let modified = index.metadata().unwrap().modified().unwrap();
            (std::fs::read(&index).unwrap(), modified)
        };
        let listing = || -> BTreeSet<String> { files(&stage.checkout).into_keys().collect() };
        let (index_before, listing_before) = (state(), listing());

        let outcome = stage.run(&[ID]);

        assert_eq!(outcome.code, 0, "{outcome:?}");
        assert_eq!(
            stage.stored("steps/01/stderr"),
            b"trace: <REFERENCE>/lib/core line 3\n"
        );
        let path = text(&stage.checkout).as_bytes();
        for (file, content) in files(&stage.dir(ID).join("record")) {
            assert!(
                !content.windows(path.len()).any(|window| window == path),
                "{file} holds the path of the checkout"
            );
        }
        assert_eq!(state(), index_before, "touched: {touched}");
        assert_eq!(listing(), listing_before, "touched: {touched}");
        assert_eq!(std::fs::read(&tracked).unwrap(), b"tracked\n");
    }
}

/// Runs a stage whose checkout holds an untracked file and an ignored directory before the run, and whose
/// stand-in runs `body`.
fn closing_check(body: &str) -> (Stage, Outcome) {
    let stage = Stage::new(body);
    stage.litter();
    let outcome = stage.run(&[ID]);
    (stage, outcome)
}

#[test]
fn files_git_does_not_track_that_were_there_before_the_run_are_no_change() {
    let (stage, outcome) = closing_check("");

    assert_eq!(outcome.code, 0, "{outcome:?}");
    assert!(!outcome.report.contains("reference checkout changed"));
    assert!(stage.has_record(ID));
}

#[test]
fn an_untracked_file_the_reference_creates_in_its_checkout_fails_the_run() {
    let (stage, outcome) = closing_check("echo x > @CHECKOUT@/created");

    assert_eq!(outcome.code, 1, "{outcome:?}");
    assert!(
        outcome.has_line("reference checkout changed"),
        "{outcome:?}"
    );
    assert!(outcome.has_line("+ created"), "{outcome:?}");
    assert!(stage.has_record(ID), "the record stays on disk");
}

#[test]
fn a_file_the_reference_creates_in_an_ignored_directory_fails_the_run() {
    let (_stage, outcome) = closing_check("echo x > @CHECKOUT@/ignored/created");

    assert_eq!(outcome.code, 1, "{outcome:?}");
    assert!(
        outcome.has_line("reference checkout changed"),
        "{outcome:?}"
    );
    assert!(outcome.has_line("+ ignored/created"), "{outcome:?}");
    assert!(!outcome.report.contains("cache.bin"), "{outcome:?}");
}

#[test]
fn an_untracked_file_the_reference_deletes_fails_the_run() {
    let (_stage, outcome) = closing_check("rm -f @CHECKOUT@/untracked.txt");

    assert_eq!(outcome.code, 1, "{outcome:?}");
    assert!(
        outcome.has_line("reference checkout changed"),
        "{outcome:?}"
    );
    assert!(outcome.has_line("- untracked.txt"), "{outcome:?}");
}

#[test]
fn a_tracked_file_the_reference_appends_to_fails_the_run() {
    let (_stage, outcome) = closing_check("echo more >> @CHECKOUT@/tracked.txt");

    assert_eq!(outcome.code, 1, "{outcome:?}");
    assert!(
        outcome.has_line("reference checkout changed"),
        "{outcome:?}"
    );
    assert!(outcome.has_line("M tracked.txt"), "{outcome:?}");
}

/// How often each of the three git commands on `checkout` is among the calls `log` lists.
fn git_calls(log: &Path, checkout: &Path) -> [usize; 3] {
    let calls = std::fs::read_to_string(log).unwrap_or_default();
    [
        "rev-parse HEAD",
        "status --porcelain=v2 --untracked-files=no",
        "ls-files --others -z",
    ]
    .map(|command| {
        let call = format!("-C {} {command}", text(checkout));
        calls.lines().filter(|line| *line == call).count()
    })
}

#[test]
fn the_checkout_is_read_once_by_a_refused_run_and_twice_by_one_that_started_the_reference() {
    let stage = Stage::new("");
    let (recorder, log) = stage.recording_git();
    write(&stage.dir(ID).join("record/placed-by-the-test"), "");

    let outcome = stage.run_with(&[ID], recorder.as_os_str(), Some(&stage.stand_in));

    assert_refused(&stage, &outcome, &[ID]);
    assert_eq!(git_calls(&log, &stage.checkout), [1, 1, 1]);

    std::fs::remove_file(&log).unwrap();
    let outcome = stage.run_with(
        &[ID, "--rerecord"],
        recorder.as_os_str(),
        Some(&stage.stand_in),
    );

    assert_eq!(outcome.code, 0, "{outcome:?}");
    assert!(stage.started());
    assert_eq!(git_calls(&log, &stage.checkout), [2, 2, 2]);
    let calls = std::fs::read_to_string(&log).unwrap();
    let on_the_checkout = calls
        .lines()
        .filter(|line| line.contains(text(&stage.checkout)));
    assert_eq!(on_the_checkout.count(), 6, "{calls}");
}

#[test]
fn a_run_whose_tree_for_the_version_call_cannot_be_made_reads_the_checkout_once() {
    let stage = Stage::new("");
    // The child process finds git in the directory of its own tree that leads its pinned `PATH`, and no
    // tree is made below a directory that holds the configuration of a workspace.
    let log = stage.recording_git_in(&stage.host.root().join("bin"));
    write(&stage.host.root().join("tmp/.winter/config.toml"), "");

    let (outcome, trees) = stage.run_in_child(true);

    assert_refused(&stage, &outcome, &["no isolated tree"]);
    assert!(!stage.has_record(ID));
    assert_eq!(trees, [] as [&str; 0]);
    assert_eq!(git_calls(&log, &stage.checkout), [1, 1, 1]);
}

#[test]
fn a_run_whose_reference_cannot_be_spawned_reads_the_checkout_once() {
    let stage = Stage::new("");
    let (recorder, log) = stage.recording_git();
    // A file git does not track, so the checkout is as its commit has it.
    let unspawned = stage.checkout.join("bin/unspawned");
    executable(&stage.host, &unspawned, "#!/no/such/interpreter\n");

    let outcome = stage.run_with(&[ID], recorder.as_os_str(), Some(&unspawned));

    assert_refused(&stage, &outcome, &["--version` did not run"]);
    assert!(!stage.has_record(ID));
    assert_eq!(git_calls(&log, &stage.checkout), [1, 1, 1]);
}

#[test]
fn scenarios_that_cannot_be_listed_refuse_the_run_after_the_checkout_was_read_once() {
    let stage = Stage::new("");
    let (recorder, log) = stage.recording_git();
    let closed = stage.root.join("closed");
    std::fs::create_dir_all(&closed).expect("a command directory");
    std::fs::set_permissions(&closed, Permissions::from_mode(0o000)).expect("no permission");
    if std::fs::read_dir(&closed).is_ok() {
        // Whoever runs this reads every directory, so no listing fails.
        return;
    }
    let request = Request {
        all_missing: true,
        ..Request::default()
    };

    let refusal = refusals(
        &stage.stand_in,
        Some(recorder.as_os_str()),
        &stage.root,
        &request,
    );

    std::fs::set_permissions(&closed, Permissions::from_mode(0o755)).expect("permission again");
    let refusal = refusal.expect_err("scenarios that are not listed");
    assert!(matches!(refusal, Refusal::Scenarios { .. }), "{refusal:?}");
    assert_eq!(git_calls(&log, &stage.checkout), [1, 1, 1]);
    assert!(!stage.started());
}

#[test]
fn every_missing_record_is_made_and_a_record_that_is_there_is_left_as_it_is() {
    let stage = Stage::new("echo \"$1\"");
    assert_eq!(stage.run(&[ID]).code, 0);
    let record = stage.dir(ID).join("record");
    let modified = |path: &Path| path.metadata().unwrap().modified().unwrap();
    let before = (files(&record), modified(&record.join("meta.toml")));
    stage.scenario(OTHER, "[[step]]\nargs = [\"other\"]\n");

    let outcome = stage.run(&["--all-missing"]);

    assert_eq!(outcome.code, 0, "{outcome:?}");
    assert!(
        outcome.has_line(&format!("recorded {OTHER}")),
        "{outcome:?}"
    );
    assert!(!outcome.report.contains(ID), "{outcome:?}");
    assert_eq!(
        std::fs::read(stage.dir(OTHER).join("record/steps/01/stdout")).unwrap(),
        b"other\n"
    );
    assert_eq!(
        (files(&record), modified(&record.join("meta.toml"))),
        before
    );
}

#[test]
fn every_missing_record_is_never_asked_for_together_with_recording_again() {
    let stage = Stage::new("");

    let outcome = stage.run(&["--all-missing", "--rerecord"]);

    assert_eq!(outcome.code, 2, "{outcome:?}");
    assert!(!stage.started());
    assert!(!stage.has_record(ID));
}

#[test]
fn the_reference_is_given_the_two_interpreter_variables_and_its_directory_on_the_path() {
    let stage = Stage::new(
        "echo \"$PYTHONDONTWRITEBYTECODE $PYTHONHASHSEED\" > @HOST@/seen\necho \"$PATH\" > @HOST@/path",
    );

    let outcome = stage.run(&[ID]);

    assert_eq!(outcome.code, 0, "{outcome:?}");
    let seen = |name: &str| std::fs::read_to_string(stage.outside().join(name)).unwrap();
    assert_eq!(seen("seen"), "1 0\n");
    let path = seen("path");
    let directories: Vec<&str> = path.trim_end().split(':').collect();
    let [bin, rest @ ..] = &directories[..] else {
        panic!("{path}")
    };
    assert!(bin.starts_with('/') && bin.ends_with("/bin"), "{path}");
    assert_eq!(
        rest,
        [
            text(&directory_of_git()),
            text(&stage.checkout.join("bin")),
            "/usr/bin",
            "/bin"
        ]
    );
    let meta = Meta::load(&stage.dir(ID)).expect("a meta.toml that reads");
    assert_eq!(meta.environment, stage.host.env().digest());
}

#[test]
fn a_scenario_that_needs_a_facility_the_runner_lacks_is_skipped_and_fails_nothing() {
    let stage = Stage::new("");
    stage.scenario(ID, &format!("needs = [\"network\"]\n{ONE_STEP}"));

    let outcome = stage.run(&[ID]);

    assert_eq!(outcome.code, 0, "{outcome:?}");
    assert!(
        outcome.has_line(&format!(
            "skipped {ID}: it needs network, which the runner lacks"
        )),
        "{outcome:?}"
    );
    assert!(!stage.started());
    assert!(!stage.has_record(ID));
}

#[test]
fn a_refused_scenario_fails_the_run_and_the_others_are_recorded() {
    let stage = Stage::new("");
    stage.scenario(OTHER, &format!("{ONE_STEP}cwd = \"../..\"\n"));

    let outcome = stage.run(&[OTHER, ID, "ws-index"]);

    assert_eq!(outcome.code, 1, "{outcome:?}");
    assert!(outcome.report.contains(&format!("refused {OTHER}: ")));
    assert!(outcome.report.contains("refused ws-index: "), "{outcome:?}");
    assert!(outcome.has_line(&format!("recorded {ID}")), "{outcome:?}");
    assert!(stage.has_record(ID) && !stage.has_record(OTHER));
}

#[test]
fn a_comparison_rule_that_relaxes_nothing_is_reported_as_a_malformed_scenario() {
    let stage = Stage::new("echo plain");
    stage.scenario(
        ID,
        &format!("{ONE_STEP}[compare]\nmessage_text = [\"/message\"]\n"),
    );

    let outcome = stage.run(&[ID]);

    assert_eq!(outcome.code, 1, "{outcome:?}");
    assert!(
        outcome.report.contains(&format!(
            "refused {ID}: the scenario {ID} is malformed at the comparison rule `message_text /message`"
        )),
        "{outcome:?}"
    );
    assert!(!outcome.report.contains("stable"), "{outcome:?}");
    assert!(!stage.has_record(ID));
}

#[test]
fn a_reference_that_does_not_tell_its_version_is_refused_and_the_checkout_is_read_again() {
    let stage = Stage::new("");
    let silent = stage.checkout.join("bin/silent");
    executable(&stage.host, &silent, "#!/bin/sh\nexit 3\n");
    let (recorder, log) = stage.recording_git();

    let outcome = stage.run_with(
        &[ID, "--reference", text(&silent)],
        recorder.as_os_str(),
        None,
    );

    // The file the test added is untracked before both passes, so the run fails for the version alone.
    assert_eq!(outcome.code, 1, "{outcome:?}");
    assert!(outcome.report.contains("--version"), "{outcome:?}");
    assert!(!outcome.report.contains("reference checkout changed"));
    assert!(!stage.has_record(ID));
    assert_eq!(git_calls(&log, &stage.checkout), [2, 2, 2]);
}

/// A stage whose scenarios root lies in a git repository of the stage, and whose stand-in writes a file
/// under `.GIT` for the argument `odd` and prints for every other.
fn stage_in_a_repository() -> Stage {
    let body = "if [ \"$1\" = \"odd\" ]; then mkdir -p .GIT; echo x > .GIT/config; fi\necho done";
    Stage::with_root(body, "ws/repository/scenarios", Some("ws/repository"))
}

#[test]
fn a_record_the_repository_of_the_scenarios_would_not_take_is_removed_and_fails_the_scenario() {
    let stage = stage_in_a_repository();
    stage.scenario(OTHER, "[[step]]\nargs = [\"odd\"]\n");

    let outcome = stage.run(&[ID, OTHER]);

    assert_eq!(outcome.code, 1, "{outcome:?}");
    assert!(outcome.has_line(&format!("recorded {ID}")), "{outcome:?}");
    let refused =
        format!("refused {OTHER}: the repository of the scenarios would not take record/");
    let line = outcome
        .report
        .lines()
        .find(|line| line.starts_with(&refused));
    let line = line.unwrap_or_else(|| panic!("{outcome:?}"));
    assert!(line.contains("/.GIT/config"), "{line}");
    assert!(stage.has_record(ID) && !stage.has_record(OTHER));
    let status = Git::new(&stage.host)
        .unwrap()
        .run(
            &stage.host.root().join("ws/repository"),
            &["status", "--porcelain"],
        )
        .unwrap();
    assert_eq!(status, "?? scenarios/\n", "nothing was staged");
}

#[test]
fn a_record_an_ignore_rule_of_the_repository_matches_is_removed_and_fails_the_scenario() {
    let stage = stage_in_a_repository();
    write(
        &stage.host.root().join("ws/repository/.gitignore"),
        "stderr\n",
    );

    let outcome = stage.run(&[ID]);

    assert_eq!(outcome.code, 1, "{outcome:?}");
    assert!(
        outcome
            .report
            .contains("record/steps/01/stderr: an ignore rule matches it"),
        "{outcome:?}"
    );
    assert!(!stage.has_record(ID));
}

/// The directories beside `record/` of the scenario [`ID`] that a record is set aside in.
fn asides(stage: &Stage) -> Vec<PathBuf> {
    let entries = entries(&stage.dir(ID)).into_iter().map(PathBuf::from);
    entries
        .filter(|entry| text(Path::new(entry.file_name().expect("a name"))).starts_with("record-"))
        .collect()
}

#[test]
fn a_record_that_was_there_is_back_as_it_was_when_the_one_that_replaces_it_is_not_taken() {
    let stage = stage_in_a_repository();
    let outcome = stage.run(&[ID]);
    assert_eq!(outcome.code, 0, "{outcome:?}");
    let before = files(&stage.dir(ID));
    write(
        &stage.host.root().join("ws/repository/.gitignore"),
        "stderr\n",
    );

    let outcome = stage.run(&["--rerecord", ID]);

    assert_eq!(outcome.code, 1, "{outcome:?}");
    assert!(
        outcome
            .report
            .contains("record/steps/01/stderr: an ignore rule matches it"),
        "{outcome:?}"
    );
    assert!(
        outcome
            .report
            .contains("the one before it is back in its place"),
        "{outcome:?}"
    );
    assert!(before.contains_key("record/meta.toml"), "{before:?}");
    assert_eq!(files(&stage.dir(ID)), before);
    assert_eq!(asides(&stage), [] as [PathBuf; 0]);
}

#[test]
fn a_record_that_replaces_one_leaves_nothing_beside_it() {
    let stage = stage_in_a_repository();
    let outcome = stage.run(&[ID]);
    assert_eq!(outcome.code, 0, "{outcome:?}");
    write(&stage.dir(ID).join("record/placed-by-the-test"), "");

    let outcome = stage.run(&["--rerecord", ID]);

    assert_eq!(outcome.code, 0, "{outcome:?}");
    let after = files(&stage.dir(ID));
    assert!(after.contains_key("record/meta.toml"), "{after:?}");
    assert!(
        !after.contains_key("record/placed-by-the-test"),
        "{after:?}"
    );
    assert_eq!(asides(&stage), [] as [PathBuf; 0]);
}

/// Gives the owner every permission on every directory under `directory`.
fn unlock(directory: &Path) {
    for entry in std::fs::read_dir(directory).expect("a directory") {
        let entry = entry.expect("an entry");
        if entry.file_type().expect("a file type").is_dir() {
            std::fs::set_permissions(entry.path(), Permissions::from_mode(0o755))
                .expect("permission again");
            unlock(&entry.path());
        }
    }
}

/// Records [`ID`], then records it again in a child test process with a stand-in that leaves a directory
/// in its tree that cannot be emptied, and runs `step` after that, so the record step panics when it drops
/// the tree. Answers the stage, the files of the first record, and the outcome of the second run; `None`
/// when that run did not panic.
fn panic_while_a_record_is_aside(
    step: &str,
) -> Option<(Stage, BTreeMap<String, Vec<u8>>, Outcome)> {
    let body = format!(
        "if [ -e @HOST@/again ]; then mkdir -p locked/held; chmod 555 locked; {step}; fi\necho done"
    );
    let stage = Stage::with_root(&body, "ws/repository/scenarios", Some("ws/repository"));
    let outcome = stage.run(&[ID]);
    assert_eq!(outcome.code, 0, "{outcome:?}");
    let before = files(&stage.dir(ID).join("record"));
    assert!(before.contains_key("meta.toml"), "{before:?}");
    write(&stage.outside().join("again"), "");

    let (outcome, _, stderr) = stage.run_in_child_with(true, &[AGAIN_VARIABLE]);

    unlock(&stage.host.root().join("tmp/trees"));
    if outcome.code != PANICKED {
        eprintln!(
            "not tested: whoever runs this empties a directory that may not be written to, so the \
             record step did not panic: {outcome:?}"
        );
        return None;
    }
    assert!(stderr.contains("could not be removed"), "{stderr}");
    Some((stage, before, outcome))
}

#[test]
fn a_panic_after_the_step_wrote_no_record_puts_the_record_that_was_set_aside_back() {
    // The two runs of the step end with another exit code each, so no record is written.
    let step = "echo run >> @HOST@/runs; exit $(wc -l < @HOST@/runs)";
    let Some((stage, before, outcome)) = panic_while_a_record_is_aside(step) else {
        return;
    };

    assert_eq!(files(&stage.dir(ID).join("record")), before, "{outcome:?}");
    assert_eq!(asides(&stage), [] as [PathBuf; 0]);
}

#[test]
fn a_panic_after_the_step_wrote_a_record_leaves_the_one_set_aside_whole_and_the_next_run_names_it()
{
    let Some((stage, before, outcome)) = panic_while_a_record_is_aside("echo again") else {
        return;
    };

    let asides = asides(&stage);
    let [aside] = &asides[..] else {
        panic!("{asides:?}: {outcome:?}");
    };
    assert_eq!(files(&aside.join("record")), before);
    assert_eq!(entries(aside), [text(&aside.join("record"))]);
    assert!(stage.has_record(ID));
    let name = text(Path::new(aside.file_name().expect("a name")));
    for args in [&[ID][..], &["--rerecord", ID], &["--all-missing"]] {
        let next = stage.run(args);
        assert_eq!(next.code, 1, "{args:?}: {next:?}");
        assert!(next.report.contains(&format!("{name}/record")), "{next:?}");
        assert_eq!(files(&aside.join("record")), before, "{args:?}");
    }
}

#[test]
fn a_record_that_cannot_be_put_back_is_kept_beside_its_place_and_named() {
    let stage = stage_in_a_repository();
    // A directory that may not be written to is not moved to another parent, unless whoever runs this
    // writes to every directory.
    let probe = stage.outside().join("probe");
    std::fs::create_dir_all(probe.join("from/moved")).expect("a directory to move");
    std::fs::set_permissions(probe.join("from/moved"), Permissions::from_mode(0o555)).unwrap();
    let moves = std::fs::rename(probe.join("from/moved"), probe.join("moved")).is_ok();
    for moved in [probe.join("from/moved"), probe.join("moved")] {
        let _ = std::fs::set_permissions(moved, Permissions::from_mode(0o755));
    }
    if moves {
        eprintln!(
            "not tested: whoever runs this moves a directory that may not be written to, so a record \
             set aside cannot be kept from being put back"
        );
        return;
    }
    let outcome = stage.run(&[ID]);
    assert_eq!(outcome.code, 0, "{outcome:?}");
    let before = files(&stage.dir(ID).join("record"));
    assert!(before.contains_key("meta.toml"), "{before:?}");
    write(
        &stage.host.root().join("ws/repository/.gitignore"),
        "stderr\n",
    );
    // A `git` that takes the write permission from the record set aside when the new one is checked.
    let closing = stage.outside().join("closing");
    let script = format!(
        "#!/bin/sh\ncase \"$*\" in *check-ignore*) chmod a-w {}/record-*/record ;; esac\nexec {} \"$@\"\n",
        text(&stage.dir(ID)),
        text(&directory_of_git().join("git"))
    );
    executable(&stage.host, &closing.join("git"), &script);

    let outcome = stage.run_with(
        &["--rerecord", ID],
        closing.as_os_str(),
        Some(&stage.stand_in),
    );

    let asides = asides(&stage);
    let [aside] = &asides[..] else {
        panic!("{asides:?}: {outcome:?}");
    };
    let kept = aside.join("record");
    std::fs::set_permissions(&kept, Permissions::from_mode(0o755)).expect("permission again");
    assert_eq!(outcome.code, 1, "{outcome:?}");
    assert!(
        outcome
            .report
            .contains("record/steps/01/stderr: an ignore rule matches it"),
        "{outcome:?}"
    );
    assert!(
        outcome.report.contains("could not be put back: "),
        "{outcome:?}"
    );
    let named = format!("it is kept at {}\n", text(&kept));
    assert!(outcome.report.contains(&named), "{outcome:?}");
    assert!(!stage.has_record(ID));
    assert_eq!(files(&kept), before);
    assert_eq!(entries(aside), [text(&kept)]);
}

#[test]
fn the_refusals_are_decided_over_the_inputs_they_are_given() {
    let stage = Stage::new("");
    let request = Request {
        ids: vec![ID.to_owned()],
        ..Request::default()
    };
    let git = directory_of_git();

    let admission = refusals(
        &stage.stand_in,
        Some(git.as_os_str()),
        &stage.root,
        &request,
    )
    .expect("a run that may start");

    assert_eq!(admission.checkout, stage.checkout);
    assert_eq!(admission.commit, stage.commit);
    let [scenario] = &admission.scenarios[..] else {
        panic!("{:?}", admission.scenarios)
    };
    assert_eq!(
        (scenario.id.as_str(), &scenario.verdict),
        (ID, &Verdict::Record)
    );

    let refusal = refusals(&stage.stand_in, None, &stage.root, &request).expect_err("no git");
    assert!(matches!(refusal, Refusal::NoGit), "{refusal:?}");
    let refusal = refusals(
        Path::new("winter"),
        Some(git.as_os_str()),
        &stage.root,
        &request,
    )
    .expect_err("a relative path");
    assert!(matches!(refusal, Refusal::Relative(_)), "{refusal:?}");
    assert!(!stage.started());
}
