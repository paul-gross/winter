//! The step runner and the record of a scenario: what a step is given, what is observed and stored of it,
//! and what is refused before anything is started.
//!
//! The executable of every test is `/bin/sh`, and the stand-in it runs is a script of the scenario's
//! `files/`, which the fixture copies into the workspace directory: the first argument of each step.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use winter_workspace_test_support::scenario::compare::Dimension;
use winter_workspace_test_support::scenario::fixture::build;
use winter_workspace_test_support::scenario::record::{RecordError, RecordOptions, record};
use winter_workspace_test_support::scenario::runner::{
    CALL_CAPTURE, RunFailure, SUPPORTED, run_steps, run_steps_within,
};
use winter_workspace_test_support::scenario::snapshot::Snapshot;
use winter_workspace_test_support::scenario::{Facility, Meta, Scenario};
use winter_workspace_test_support::{Exit, Observation, TempTree, invoke};

/// The id of the scenario every test writes.
const ID: &str = "ws-index/basic";

/// The name of the stand-in script among the files of a scenario.
const STAND_IN: &str = "stand-in";

/// A commit a record may name as the one of its reference.
const COMMIT: &str = "b57b020e99e10a04d8bb621d0b03ddb2c983a090";

/// Names the fixture a child test process was started to run. A fixture does nothing without its own name here.
const FIXTURE_VARIABLE: &str = "WINTER_TEST_FIXTURE";

/// Names the scenario directory a fixture records.
const DIRECTORY_VARIABLE: &str = "WINTER_TEST_RECORD_DIRECTORY";

/// Names the key a fixture expects a refusal to name.
const KEY_VARIABLE: &str = "WINTER_TEST_RECORD_KEY";

/// A test of this file that records a scenario which is refused. It runs in a child process whose trees
/// would be created, and kept, inside the tree of the test that started it.
const REFUSAL_FIXTURE: &str = "fixture_a_description_is_refused";

/// A test of this file that records a scenario and keeps both trees, so it runs only in a child process,
/// whose trees are created inside the tree of the test that started it.
const KEEP_FIXTURE: &str = "fixture_a_record_keeps_both_trees";

/// One step that runs the stand-in.
const ONE_STEP: &str = "[[step]]\nargs = [\"stand-in\"]\n";

/// A scenario directory under construction, in a tree of its own: the trees a record runs in are others,
/// so everything of this one is outside them.
struct Written {
    host: TempTree,
    dir: PathBuf,
}

impl Written {
    /// A scenario directory whose description is `description` after its summary, and whose stand-in is
    /// `script`. `@HOST@` in the script is a directory of this tree, outside every tree a step runs in.
    fn new(script: &str, description: &str) -> Self {
        let host = TempTree::new().expect("a tree for the scenario directory");
        let dir = host.root().join("tmp/scenarios").join(ID);
        std::fs::create_dir_all(&dir).expect("the scenario directory is created");
        let written = Self { host, dir };
        written.describe(description);
        written.stand_in(script);
        written
    }

    fn describe(&self, description: &str) {
        let description = format!("summary = \"s\"\n{description}");
        std::fs::write(self.dir.join("scenario.toml"), description)
            .expect("the description is written");
    }

    fn stand_in(&self, script: &str) {
        let outside = self.outside();
        let script = script.replace("@HOST@", outside.to_str().expect("a path that is text"));
        self.file(&format!("files/{STAND_IN}"), script.as_bytes());
    }

    /// Writes `content` to `path` of the scenario directory, with the directories on the way.
    fn file(&self, path: &str, content: &[u8]) {
        let file = self.dir.join(path);
        std::fs::create_dir_all(file.parent().expect("a file has a parent"))
            .expect("the directories are created");
        std::fs::write(file, content).expect("the file is written");
    }

    /// A directory outside every tree a step runs in.
    fn outside(&self) -> PathBuf {
        let outside = self.host.root().join("cache/outside");
        std::fs::create_dir_all(&outside).expect("the outside directory is created");
        outside
    }

    /// The file the stand-in of [`MARKED`] adds a line to each time it runs.
    fn marker(&self) -> PathBuf {
        self.outside().join("marker")
    }

    fn scenario(&self) -> Scenario {
        Scenario::load(&self.dir).expect("the description parses")
    }

    fn record(&self) -> Result<(), RecordError> {
        record(&self.dir, &options())
    }

    /// The bytes of `path` of the record.
    fn stored(&self, path: &str) -> Vec<u8> {
        let file = self.dir.join("record").join(path);
        std::fs::read(&file).unwrap_or_else(|error| panic!("{}: {error}", file.display()))
    }

    fn has_record(&self) -> bool {
        self.dir.join("record").symlink_metadata().is_ok()
    }
}

/// A stand-in that leaves a line in the marker file and does nothing else.
const MARKED: &str = "echo run >> @HOST@/marker\n";

fn options() -> RecordOptions {
    RecordOptions {
        executable: PathBuf::from("/bin/sh"),
        extra_env: Vec::new(),
        redactions: Vec::new(),
        reference_commit: COMMIT.to_owned(),
        reference_runtime: "stand-in 1".to_owned(),
        rerecord: false,
        keep: false,
    }
}

/// Every file under `directory` with its bytes, by its path relative to `directory`; a directory that holds
/// nothing is listed with no bytes.
fn files(directory: &Path) -> BTreeMap<String, Option<Vec<u8>>> {
    fn walk(directory: &Path, relative: &str, found: &mut BTreeMap<String, Option<Vec<u8>>>) {
        let mut empty = true;
        for entry in std::fs::read_dir(directory).expect("a directory") {
            empty = false;
            let entry = entry.expect("an entry");
            let name = entry.file_name().to_string_lossy().into_owned();
            let relative = match relative {
                "" => name,
                relative => format!("{relative}/{name}"),
            };
            if entry.file_type().expect("a file type").is_dir() {
                walk(&entry.path(), &relative, found);
            } else {
                let content = std::fs::read(entry.path()).expect("a file that reads");
                found.insert(relative, Some(content));
            }
        }
        if empty {
            found.insert(relative.to_owned(), None);
        }
    }
    let mut found = BTreeMap::new();
    walk(directory, "", &mut found);
    found
}

/// The names of the entries of `directory`.
fn entries(directory: &Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(directory)
        .expect("a directory")
        .map(|entry| {
            entry
                .expect("an entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .collect();
    names.sort();
    names
}

fn holds(content: &[u8], path: &Path) -> bool {
    let path = path.to_str().expect("a path that is text").as_bytes();
    content.windows(path.len()).any(|window| window == path)
}

/// Asserts that `error` says the scenario is malformed, and names the scenario and `key`.
#[track_caller]
fn assert_malformed(error: &RecordError, key: &str) {
    let RecordError::Malformed(malformed) = error else {
        panic!("not malformed: {error:?}");
    };
    assert_eq!(malformed.scenario, ID);
    assert_eq!(malformed.key, key, "{malformed}");
    let message = error.to_string();
    assert!(message.contains(ID), "{message}");
    assert!(message.contains(key), "{message}");
}

/// The dimension of the difference `result` refuses a record for.
#[track_caller]
fn unstable(result: Result<(), RecordError>) -> Dimension {
    unstable_at(result).0
}

/// The dimension and the subject of the difference `result` refuses a record for.
#[track_caller]
fn unstable_at(result: Result<(), RecordError>) -> (Dimension, String) {
    match result {
        Err(RecordError::Unstable(difference)) => (difference.dimension, difference.subject),
        other => panic!("not refused as unstable: {other:?}"),
    }
}

/// Whether this process is the child test process a test started to run the fixture `name`.
fn started_as_fixture(name: &str) -> bool {
    std::env::var_os(FIXTURE_VARIABLE).is_some_and(|fixture| fixture == name)
}

/// Runs the fixture `fixture` of this file in a child test process whose trees are created, and kept, in a
/// new directory of the tree of `written`, to record the scenario of `written`. Answers that directory.
fn fixture_in(written: &Written, fixture: &str, key: &str) -> (PathBuf, Observation) {
    let trees = written.host.root().join("tmp/trees");
    std::fs::create_dir_all(&trees).expect("a directory inside the tree");
    let this_test_binary = std::env::current_exe().expect("the path of the test executable");
    let mut invocation = written.host.invocation(&this_test_binary);
    invocation.args = [fixture, "--ignored", "--exact", "--nocapture"]
        .iter()
        .map(Into::into)
        .collect();
    let text = |path: &Path| path.to_str().expect("a path that is text").to_owned();
    for (name, value) in [
        ("WINTER_TEST_TMPDIR", text(&trees)),
        ("WINTER_TEST_KEEP", "1".to_owned()),
        (FIXTURE_VARIABLE, fixture.to_owned()),
        (DIRECTORY_VARIABLE, text(&written.dir)),
        (KEY_VARIABLE, key.to_owned()),
    ] {
        invocation.env.set(name, &value);
    }
    let observation = invoke(invocation).expect("a child test run");
    assert_eq!(observation.exit, Exit::Code(0), "{observation:?}");
    assert!(
        String::from_utf8_lossy(&observation.stdout).contains("test result: ok. 1 passed"),
        "{observation:?}"
    );
    (trees, observation)
}

/// The scenario directory a fixture was started to record.
fn directory_of_the_fixture() -> PathBuf {
    std::env::var_os(DIRECTORY_VARIABLE)
        .expect("the directory a fixture records")
        .into()
}

/// Whether `process` is running. A process that has ended and waits to be reaped is not.
fn is_alive(process: libc::pid_t) -> bool {
    // SAFETY: `kill` takes two integers and touches no memory; signal 0 delivers nothing.
    if unsafe { libc::kill(process, 0) } != 0 {
        return false;
    }
    let ended_unreaped = std::fs::read_to_string(format!("/proc/{process}/stat"))
        .ok()
        .and_then(|stat| {
            stat.rsplit_once(") ")
                .map(|(_, state)| state.starts_with('Z'))
        });
    ended_unreaped != Some(true)
}

#[test]
fn the_runner_declares_no_facility_and_no_call_capture() {
    assert_eq!(SUPPORTED, [] as [Facility; 0]);
    assert_eq!([CALL_CAPTURE], [false]);
}

#[test]
fn a_record_of_two_steps_holds_what_each_step_did_and_what_it_was_made_with() {
    let script = "pwd >> @HOST@/roots\nprintf '{\"step\": \"%s\", \"n\": 1}\\n' \"$1\"\n\
                  echo warned >&2\necho \"$1\" >> log.txt\n";
    let step =
        |name: &str| format!("[[step]]\nargs = [\"stand-in\", \"{name}\"]\nstdout = \"json\"\n");
    let written = Written::new(script, &format!("{}{}", step("one"), step("two")));

    written.record().expect("the scenario records");

    for (step, name, log) in [("01", "one", "one\n"), ("02", "two", "one\ntwo\n")] {
        let stored = |file: &str| written.stored(&format!("steps/{step}/{file}"));
        assert_eq!(stored("exit-code"), b"0\n");
        assert_eq!(
            String::from_utf8_lossy(&stored("stdout")),
            format!("{{\"step\": \"{name}\", \"n\": 1}}\n")
        );
        assert_eq!(stored("stderr"), b"warned\n");
        assert_eq!(
            String::from_utf8_lossy(&stored("stdout.json")),
            format!("{{\"n\":1,\"step\":\"{name}\"}}\n")
        );
        assert_eq!(stored("after/files/ws/log.txt"), log.as_bytes());
    }
    let record = written.dir.join("record");
    assert_eq!(entries(&record), ["meta.toml", "steps"]);
    assert_eq!(entries(&record.join("steps")), ["01", "02"]);
    assert_eq!(
        entries(&record.join("steps/01")),
        [
            "after",
            "before",
            "exit-code",
            "stderr",
            "stdout",
            "stdout.json"
        ]
    );
    assert_eq!(
        entries(&record.join("steps/01/before")),
        ["git.txt", "tree.txt"]
    );
    assert_eq!(entries(&written.dir), ["files", "record", "scenario.toml"]);

    // The record reads as the chain of stored snapshots it is.
    let read = |path: &str, previous: Option<&Snapshot>| {
        Snapshot::read(&record.join(path), previous).expect("a stored snapshot")
    };
    let before = read("steps/01/before", None);
    let after = read("steps/01/after", Some(&before));
    let before = read("steps/02/before", Some(&after));
    assert_eq!(before.tree, after.tree);
    let after = read("steps/02/after", Some(&before));
    assert_eq!(
        after.files.keys().collect::<Vec<_>>(),
        ["ws/log.txt"],
        "the second step changed one file"
    );

    let meta = Meta::load(&written.dir).expect("a meta.toml");
    assert_eq!(meta.environment, written.host.env().digest());
    assert_eq!(
        meta.description,
        written
            .scenario()
            .description_digest()
            .expect("a description digest")
    );
    assert_eq!(meta.reference_commit, COMMIT);
    assert_eq!(meta.reference_runtime, "stand-in 1");
    assert!(
        meta.git_version
            .starts_with(|first: char| first.is_ascii_digit()),
        "{meta:?}"
    );

    // Each step ran once in each of two trees, and neither root is in the record. Both trees are gone.
    let roots = std::fs::read_to_string(written.outside().join("roots")).expect("the roots");
    let roots: Vec<&Path> = roots
        .lines()
        .map(|line| Path::new(line.strip_suffix("/ws").expect("the workspace directory")))
        .collect();
    assert_eq!(roots.len(), 4);
    assert_eq!((roots[0], roots[2]), (roots[1], roots[3]));
    assert_ne!(roots[0], roots[2]);
    for (path, content) in files(&record) {
        for root in &roots {
            assert!(!root.exists(), "{}", root.display());
            let content = content.as_deref().unwrap_or_default();
            assert!(!holds(content, root), "{path} holds {}", root.display());
        }
    }
}

#[test]
fn a_description_that_is_not_confined_is_refused_before_any_tree_exists() {
    let remote = "[[fixture.remote]]\nname = \"alpha\"\n[[fixture.remote.commit]]\nmessage = \"m\"\n\
                  files = { \"a\" = \"a\" }\n";
    let cases = [
        (format!("{ONE_STEP}cwd = \"/tmp\"\n"), "cwd"),
        (format!("{ONE_STEP}cwd = \"../..\"\n"), "cwd"),
        (
            format!(
                "{remote}[[fixture.checkout]]\nremote = \"alpha\"\npath = \"../outside\"\n{ONE_STEP}"
            ),
            "path",
        ),
        (
            format!("{ONE_STEP}env = {{ HOME = \"elsewhere\" }}\n"),
            "env",
        ),
        (
            format!("{ONE_STEP}env = {{ PROBE = \"<ROOT>/..\" }}\n"),
            "env",
        ),
        (
            "[[step]]\nargs = [\"stand-in\", \"--out=/tmp/x\"]\n".to_owned(),
            "args",
        ),
        (
            format!("{ONE_STEP}[[stub]]\nname = \"docker\"\nexit = 0\nenv = []\n"),
            "stub",
        ),
    ];
    for (description, key) in cases {
        let written = Written::new(MARKED, &description);

        let (trees, _) = fixture_in(&written, REFUSAL_FIXTURE, key);

        assert_eq!(entries(&trees), [] as [&str; 0], "{description}");
        assert!(!written.has_record(), "{description}");
        assert!(!written.marker().exists(), "{description}");
    }
}

#[test]
#[ignore = "its trees would be kept: the test of a refused description runs it in a child process"]
fn fixture_a_description_is_refused() {
    if !started_as_fixture(REFUSAL_FIXTURE) {
        return;
    }
    let key = std::env::var(KEY_VARIABLE).expect("the key a refusal names");

    let error = record(&directory_of_the_fixture(), &options()).expect_err("no record");

    assert_malformed(&error, &key);
}

#[test]
fn a_record_that_is_kept_leaves_both_trees_on_disk_and_names_them() {
    let written = Written::new(MARKED, ONE_STEP);

    let (trees, observation) = fixture_in(&written, KEEP_FIXTURE, "");

    let kept = entries(&trees);
    assert_eq!(kept.len(), 2, "{kept:?}");
    let stderr = String::from_utf8_lossy(&observation.stderr);
    for tree in &kept {
        let root = trees.join(tree);
        assert!(root.join("ws").join(STAND_IN).is_file());
        let named = format!("kept the isolated tree {}\n", root.display());
        assert_eq!(stderr.matches(&named).count(), 1, "{stderr}");
    }
    assert!(written.has_record());
}

#[test]
#[ignore = "keeps its trees: the test of a kept record runs it in a child process"]
fn fixture_a_record_keeps_both_trees() {
    if !started_as_fixture(KEEP_FIXTURE) {
        return;
    }
    let options = RecordOptions {
        keep: true,
        ..options()
    };

    record(&directory_of_the_fixture(), &options).expect("the scenario records");
}

#[test]
fn a_step_of_a_scenario_built_in_code_is_not_started_outside_the_tree() {
    let written = Written::new(MARKED, ONE_STEP);
    let outside = written.outside();
    for cwd in [outside.to_str().expect("a path that is text"), "../.."] {
        let mut scenario = written.scenario();
        let tree = build(&scenario).expect("the fixture builds");
        scenario.steps[0].cwd = cwd.to_owned();

        let failure = run_steps(&scenario, &tree, Path::new("/bin/sh"), &[]).expect_err("no run");

        assert!(
            matches!(&failure, RunFailure::Unconfined { step: 1, cwd: used }
                if *used == tree.workspace_dir().join(cwd)),
            "{failure:?}"
        );
        assert!(!written.marker().exists());
    }
}

#[test]
fn a_step_is_not_started_in_a_directory_a_link_of_the_fixture_leads_out_to() {
    let written = Written::new(MARKED, &format!("{ONE_STEP}cwd = \"out\"\n"));
    let outside = written.outside();
    std::os::unix::fs::symlink(&outside, written.dir.join("files/out")).expect("the link is made");
    let scenario = written.scenario();
    let tree = build(&scenario).expect("the fixture builds");
    assert_eq!(
        std::fs::read_link(tree.workspace_dir().join("out")).expect("a link"),
        outside
    );

    let failure = run_steps(&scenario, &tree, Path::new("/bin/sh"), &[]).expect_err("no run");
    let error = written.record().expect_err("no record");

    assert!(
        matches!(failure, RunFailure::Unconfined { step: 1, .. }),
        "{failure:?}"
    );
    assert_malformed(&error, "cwd");
    assert!(!written.has_record());
    assert!(!written.marker().exists());
    assert_eq!(entries(&written.dir), ["files", "scenario.toml"]);
}

#[test]
fn a_step_is_not_started_in_a_directory_a_link_of_an_earlier_step_leads_out_to() {
    let script = "echo run >> @HOST@/marker\nln -s @HOST@ out\n";
    let written = Written::new(script, &format!("{ONE_STEP}{ONE_STEP}cwd = \"out\"\n"));
    let scenario = written.scenario();
    let tree = build(&scenario).expect("the fixture builds");

    let failure = run_steps(&scenario, &tree, Path::new("/bin/sh"), &[]).expect_err("no run");
    let error = written.record().expect_err("no record");

    assert!(
        matches!(failure, RunFailure::Unconfined { step: 2, .. }),
        "{failure:?}"
    );
    assert_malformed(&error, "cwd");
    assert!(!written.has_record());
    assert_eq!(
        std::fs::read_link(tree.workspace_dir().join("out")).expect("a link"),
        written.outside()
    );
    // The first step ran once here and once for the record, which ended at its first tree.
    let marker = std::fs::read_to_string(written.marker()).expect("the marker");
    assert_eq!(marker, "run\nrun\n");
}

#[test]
fn a_step_that_is_not_confined_stops_the_run_after_the_steps_before_it() {
    let written = Written::new(MARKED, &format!("{ONE_STEP}{ONE_STEP}"));
    let mut scenario = written.scenario();
    let tree = build(&scenario).expect("the fixture builds");
    scenario.steps[1].cwd = "../..".to_owned();

    let failure = run_steps(&scenario, &tree, Path::new("/bin/sh"), &[]).expect_err("no run");

    assert!(
        matches!(failure, RunFailure::Unconfined { step: 2, .. }),
        "{failure:?}"
    );
    let marker = std::fs::read_to_string(written.marker()).expect("the marker");
    assert_eq!(marker, "run\n");
}

#[test]
fn a_step_of_a_scenario_built_in_code_asks_for_nothing_the_runner_refuses() {
    type Change = fn(&mut Scenario);
    let cases: [(&str, Change); 7] = [
        ("env", |scenario| {
            let path = ("PATH".to_owned(), "<ROOT>/ws".to_owned());
            scenario.steps[1].env.extend([path]);
        }),
        ("env", |scenario| {
            let ceiling = ("GIT_CEILING_DIRECTORIES".to_owned(), String::new());
            scenario.steps[1].env.extend([ceiling]);
        }),
        ("env", |scenario| {
            let above = ("PROBE".to_owned(), "<ROOT>/../x".to_owned());
            scenario.steps[1].env.extend([above]);
        }),
        ("env", |scenario| {
            let absolute = ("PROBE".to_owned(), "/etc".to_owned());
            scenario.steps[1].env.extend([absolute]);
        }),
        ("args", |scenario| {
            scenario.steps[1].args.push("/etc/passwd".to_owned());
        }),
        ("stdin", |scenario| {
            scenario.steps[1].stdin = Some("../../../cache/outside/marker".to_owned());
        }),
        ("stdin", |scenario| {
            scenario.steps[1].stdin = Some("/etc/hostname".to_owned());
        }),
    ];
    for (key, change) in cases {
        let written = Written::new(MARKED, &format!("{ONE_STEP}{ONE_STEP}"));
        let mut scenario = written.scenario();
        let tree = build(&scenario).expect("the fixture builds");
        change(&mut scenario);

        let failure = run_steps(&scenario, &tree, Path::new("/bin/sh"), &[]).expect_err("no run");

        // What needs no tree to refuse is refused before the first step is started.
        assert!(
            matches!(&failure, RunFailure::Refused { step: 2, key: at, .. } if *at == key),
            "{failure:?}"
        );
        assert!(!written.marker().exists(), "{failure}");
    }
}

#[test]
fn an_argument_that_leads_out_of_the_tree_from_the_working_directory_is_refused() {
    let steps = format!("{ONE_STEP}[[step]]\nargs = [\"stand-in\", \"../../x\"]\n");
    let written = Written::new(MARKED, &steps);
    let scenario = written.scenario();
    let tree = build(&scenario).expect("the fixture builds");

    let failure = run_steps(&scenario, &tree, Path::new("/bin/sh"), &[]).expect_err("no run");
    let error = written.record().expect_err("no record");

    assert!(
        matches!(
            failure,
            RunFailure::Refused {
                step: 2,
                key: "args",
                ..
            }
        ),
        "{failure:?}"
    );
    assert_malformed(&error, "args");
    assert!(!written.has_record());
    // The first step ran once here and once for the record, which ended at its first tree.
    let marker = std::fs::read_to_string(written.marker()).expect("the marker");
    assert_eq!(marker, "run\nrun\n");

    // The same argument stays in the tree from a directory that lies deeper.
    let steps = "[[step]]\nargs = [\"../stand-in\", \"../../x\"]\ncwd = \"deep\"\n";
    let written = Written::new(MARKED, steps);
    written.file("files/deep/kept", b"");
    written.record().expect("the scenario records");
}

#[test]
fn a_variable_the_caller_adds_is_not_one_a_step_sets() {
    let steps = format!("{ONE_STEP}env = {{ CALLER = \"step\" }}\n");
    let written = Written::new("echo \"$CALLER\"\n", &steps);
    let scenario = written.scenario();
    let tree = build(&scenario).expect("the fixture builds");
    let caller = [("CALLER".to_owned(), "caller".to_owned())];

    let failure = run_steps(&scenario, &tree, Path::new("/bin/sh"), &caller).expect_err("no run");
    assert!(
        matches!(
            failure,
            RunFailure::Refused {
                step: 1,
                key: "env",
                ..
            }
        ),
        "{failure:?}"
    );

    written.describe(ONE_STEP);
    let records =
        run_steps(&written.scenario(), &tree, Path::new("/bin/sh"), &caller).expect("a run");
    assert_eq!(records[0].stdout, b"caller\n");
}

#[test]
fn a_value_the_caller_adds_names_the_root_of_the_tree_the_step_runs_in() {
    let script = "echo \"$PATH\"\ntest -d \"$CALLER\" && echo \"$CALLER\"\n";
    let written = Written::new(script, ONE_STEP);
    let scenario = written.scenario();
    let tree = build(&scenario).expect("the fixture builds");
    let caller = [
        ("PATH".to_owned(), "<ROOT>/bin:/usr/bin:/bin".to_owned()),
        ("CALLER".to_owned(), "<ROOT>/ws".to_owned()),
    ];

    let records = run_steps(&scenario, &tree, Path::new("/bin/sh"), &caller).expect("a run");

    // What is stored has the root replaced again, so the directory test of the script is what shows that
    // the child saw a path of its own tree.
    assert_eq!(records[0].stdout, b"<ROOT>/bin:/usr/bin:/bin\n<ROOT>/ws\n");
}

#[test]
fn a_json_output_that_differs_between_two_roots_is_refused() {
    let written = Written::new(
        "printf '%s\\n' \"$$\"\n",
        &format!("{ONE_STEP}stdout = \"json\"\n"),
    );

    assert_eq!(unstable(written.record()), Dimension::Json);
    assert!(!written.has_record());
    assert_eq!(entries(&written.dir), ["files", "scenario.toml"]);
}

#[test]
fn a_text_output_that_differs_between_two_roots_is_recorded() {
    let written = Written::new("printf '%s\\n' \"$$\"\n", ONE_STEP);
    written.record().expect("the scenario records");
    let stdout = written.stored("steps/01/stdout");
    assert!(
        stdout.len() > 1
            && stdout
                .iter()
                .all(|byte| byte.is_ascii_digit() || *byte == b'\n')
    );
    assert!(!written.dir.join("record/steps/01/stdout.json").exists());

    let written = Written::new("echo \"$$\" >&2\n", ONE_STEP);
    written.record().expect("the scenario records");
    assert_eq!(written.stored("steps/01/stdout"), b"");
    assert!(written.stored("steps/01/stderr").len() > 1);
}

#[test]
fn a_file_that_differs_between_two_roots_is_refused_unless_a_rule_leaves_it_out() {
    let rule = |path: &str, mode: &str| {
        format!("[[compare.file]]\npath = \"ws/{path}\"\nmode = \"{mode}\"\nreason = \"r\"\n")
    };

    // By its bytes the file differs in the listing first, which holds its hash.
    let written = Written::new("echo \"$$\" > made\n", ONE_STEP);
    assert_eq!(
        unstable_at(written.record()),
        (Dimension::Tree, "ws/made".to_owned())
    );
    assert!(!written.has_record());

    // Read as TOML its hash is not compared, and the file differs in its content.
    let script = "echo \"process = $$\" > made.toml\n";
    let written = Written::new(script, &format!("{ONE_STEP}{}", rule("made.toml", "toml")));
    assert_eq!(
        unstable_at(written.record()),
        (Dimension::Files, "ws/made.toml".to_owned())
    );
    assert!(!written.has_record());

    for (script, path) in [("echo \"$$\" > made\n", "made"), (script, "made.toml")] {
        let written = Written::new(script, &format!("{ONE_STEP}{}", rule(path, "ignore")));
        written.record().expect("the scenario records");
        assert!(
            written
                .stored(&format!("steps/01/after/files/ws/{path}"))
                .len()
                > 1
        );
    }
}

#[test]
fn the_working_directory_a_step_prints_is_recorded_under_the_root_token() {
    let written = Written::new("pwd\n", ONE_STEP);

    written.record().expect("the scenario records");

    assert_eq!(written.stored("steps/01/stdout"), b"<ROOT>/ws\n");
}

#[test]
fn a_redaction_replaces_its_path_in_everything_that_is_stored() {
    let script = "echo @HOST@/elsewhere/out\necho @HOST@/elsewhere/err >&2\n\
                  echo @HOST@/elsewhere/file > made\nln -s @HOST@/elsewhere/link link\n";
    let written = Written::new(script, &format!("{ONE_STEP}{ONE_STEP}"));
    let elsewhere = written.outside().join("elsewhere");
    std::fs::create_dir_all(&elsewhere).expect("the directory is created");
    let options = RecordOptions {
        redactions: vec![(elsewhere.clone(), "<ELSEWHERE>".to_owned())],
        ..options()
    };

    record(&written.dir, &options).expect("the scenario records");

    assert_eq!(written.stored("steps/01/stdout"), b"<ELSEWHERE>/out\n");
    assert_eq!(written.stored("steps/01/stderr"), b"<ELSEWHERE>/err\n");
    assert_eq!(
        written.stored("steps/01/after/files/ws/made"),
        b"<ELSEWHERE>/file\n"
    );
    let listing = String::from_utf8(written.stored("steps/02/after/tree.txt")).expect("text");
    assert!(
        listing.contains("l ws/link -> <ELSEWHERE>/link\n"),
        "{listing}"
    );
    let record = written.dir.join("record");
    for (path, content) in files(&record) {
        let content = content.as_deref().unwrap_or_default();
        assert!(!holds(content, &elsewhere), "{path}");
    }
    // The file the second step wrote again as it was is listed under the hash of what is stored.
    let read = |path: &str, previous: Option<&Snapshot>| {
        Snapshot::read(&record.join(path), previous).expect("a stored snapshot")
    };
    let before = read("steps/01/before", None);
    let after = read("steps/01/after", Some(&before));
    let before = read("steps/02/before", Some(&after));
    let after = read("steps/02/after", Some(&before));
    assert_eq!(after.files, BTreeMap::new());
}

#[test]
fn a_record_that_exists_is_replaced_only_when_that_is_asked_for() {
    let written = Written::new("echo first\n", ONE_STEP);
    written.record().expect("the scenario records");
    let recorded = files(&written.dir);

    let error = written.record().expect_err("no second record");

    assert!(matches!(error, RecordError::Exists), "{error:?}");
    assert_eq!(files(&written.dir), recorded);

    written.stand_in("echo second\n");
    let again = RecordOptions {
        rerecord: true,
        ..options()
    };
    record(&written.dir, &again).expect("the scenario records again");

    assert_eq!(written.stored("steps/01/stdout"), b"second\n");
    assert_eq!(entries(&written.dir), ["files", "record", "scenario.toml"]);
    let meta = Meta::load(&written.dir).expect("a meta.toml");
    let digest = written.scenario().description_digest().expect("a digest");
    assert_eq!(meta.description, digest);
}

#[test]
fn a_record_that_is_refused_leaves_the_one_that_exists() {
    let written = Written::new("echo first\n", ONE_STEP);
    written.record().expect("the scenario records");
    written.stand_in("echo \"$$\" > made\n");
    let recorded = files(&written.dir);
    let again = RecordOptions {
        rerecord: true,
        ..options()
    };

    assert_eq!(unstable(record(&written.dir, &again)), Dimension::Tree);

    assert_eq!(files(&written.dir), recorded);
}

#[test]
fn a_step_reads_the_bytes_of_its_input_or_the_end_of_input_at_once() {
    let steps = format!("{ONE_STEP}stdin = \"input\"\n{ONE_STEP}");
    let written = Written::new("cat\n", &steps);
    written.file("input", b"fed \xff\x00 bytes\n");

    written.record().expect("the scenario records");

    assert_eq!(written.stored("steps/01/stdout"), b"fed \xff\x00 bytes\n");
    assert_eq!(written.stored("steps/02/stdout"), b"");
    assert_eq!(written.stored("steps/02/exit-code"), b"0\n");
}

#[test]
fn a_step_sees_its_own_additions_to_the_pinned_environment_and_nothing_of_this_process() {
    // SAFETY: the tests of this file run on several threads, and every read or write of the environment they
    // make goes through `std::env`, which serializes them; no thread reads it through the C library.
    unsafe { std::env::set_var("WINTER_TEST_LEAK_PROBE", "leaked") };
    let script = "test -d \"$PROBE\" && echo \"$PROBE\"\necho \"${WINTER_TEST_LEAK_PROBE-unset}\"\n\
                  echo \"$HOME\"\n";
    let steps = format!("{ONE_STEP}env = {{ PROBE = \"<ROOT>/ws\" }}\n");
    let written = Written::new(script, &steps);

    written.record().expect("the scenario records");

    assert!(std::env::var_os("WINTER_TEST_LEAK_PROBE").is_some());
    assert_eq!(
        written.stored("steps/01/stdout"),
        b"<ROOT>/ws\nunset\n<ROOT>/home\n"
    );
}

#[test]
fn a_step_that_outruns_its_deadline_fails_the_run_and_leaves_no_process() {
    let script = "echo \"$$\" > @HOST@/process\nsleep 30 &\necho \"$!\" >> @HOST@/process\nwait\n";
    let written = Written::new(script, ONE_STEP);
    let scenario = written.scenario();
    let tree = build(&scenario).expect("the fixture builds");
    let started = Instant::now();

    let failure = run_steps_within(
        &scenario,
        &tree,
        Path::new("/bin/sh"),
        &[],
        Duration::from_millis(500),
    )
    .expect_err("no run");

    assert!(
        matches!(failure, RunFailure::Timeout { step: 1, .. }),
        "{failure:?}"
    );
    assert!(started.elapsed() < Duration::from_secs(20));
    let processes = std::fs::read_to_string(written.outside().join("process"))
        .expect("the processes the step started");
    assert_eq!(processes.lines().count(), 2, "{processes}");
    for process in processes.lines() {
        let process: libc::pid_t = process.parse().expect("a process id");
        let patience = Instant::now() + Duration::from_secs(5);
        while is_alive(process) {
            assert!(
                Instant::now() < patience,
                "process {process} is still alive"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}

#[test]
fn a_step_a_signal_ends_is_recorded_as_that_signal() {
    let written = Written::new("echo before\nkill -9 \"$$\"\n", ONE_STEP);

    written.record().expect("the scenario records");

    assert_eq!(written.stored("steps/01/exit-code"), b"signal:9\n");
    assert_eq!(written.stored("steps/01/stdout"), b"before\n");
}

#[test]
fn a_step_that_exits_with_a_code_is_recorded_with_it() {
    let written = Written::new("exit 3\n", ONE_STEP);

    written.record().expect("the scenario records");

    assert_eq!(written.stored("steps/01/exit-code"), b"3\n");
}

#[test]
fn a_step_that_writes_more_than_is_captured_fails_the_run() {
    let written = Written::new("head -c 9000000 /dev/zero\n", ONE_STEP);

    let error = written.record().expect_err("no record");

    assert!(
        matches!(
            error,
            RecordError::Run(RunFailure::OutputLimit { step: 1, .. })
        ),
        "{error}"
    );
    assert!(!written.has_record());
}

#[test]
fn an_executable_that_cannot_be_started_fails_the_run() {
    let written = Written::new(MARKED, ONE_STEP);
    let scenario = written.scenario();
    let tree = build(&scenario).expect("the fixture builds");

    let absent = tree.root().join("bin/absent");
    let failure = run_steps(&scenario, &tree, &absent, &[]).expect_err("no run");
    assert!(
        matches!(failure, RunFailure::Spawn { step: 1, .. }),
        "{failure:?}"
    );

    let failure = run_steps(&scenario, &tree, Path::new("sh"), &[]).expect_err("no run");
    assert!(
        matches!(failure, RunFailure::InvalidInvocation { step: 1 }),
        "{failure:?}"
    );
}

#[test]
fn a_json_output_that_is_no_json_has_no_canonical_form_and_is_not_recorded() {
    let written = Written::new("echo not json\n", &format!("{ONE_STEP}stdout = \"json\"\n"));
    let scenario = written.scenario();
    let tree = build(&scenario).expect("the fixture builds");

    let records = run_steps(&scenario, &tree, Path::new("/bin/sh"), &[]).expect("a run");

    assert_eq!(records[0].stdout, b"not json\n");
    assert_eq!(records[0].stdout_json, None);
    assert_eq!(unstable(written.record()), Dimension::Json);
    assert!(!written.has_record());
}

#[test]
fn the_steps_of_a_run_are_observed_with_the_state_before_and_after_each() {
    let steps = format!("{ONE_STEP}stdout = \"json-lines\"\n{ONE_STEP}");
    let written = Written::new("echo '{\"b\": 1, \"a\": 2}'\npwd\npwd >> seen\n", &steps);
    let scenario = written.scenario();
    let tree = build(&scenario).expect("the fixture builds");

    let records = run_steps(&scenario, &tree, Path::new("/bin/sh"), &[]).expect("a run");

    assert_eq!(records.len(), 2);
    assert_eq!(records[0].exit, Exit::Code(0));
    assert_eq!(records[0].stdout, b"{\"b\": 1, \"a\": 2}\n<ROOT>/ws\n");
    assert_eq!(
        records[0].stdout_json.as_deref(),
        Some("{\"a\":2,\"b\":1}\n{\"$text\":\"<ROOT>/ws\"}\n")
    );
    assert_eq!(records[1].stdout_json, None);
    assert_eq!(records[0].before.files, BTreeMap::new());
    assert_eq!(records[0].after.files["ws/seen"], b"<ROOT>/ws\n");
    assert_eq!(records[1].before.tree, records[0].after.tree);
    assert_eq!(records[1].before.files, BTreeMap::new());
    assert_eq!(records[1].after.files["ws/seen"], b"<ROOT>/ws\n<ROOT>/ws\n");
}

#[test]
fn a_scenario_that_needs_a_facility_the_runner_lacks_is_not_recorded() {
    let written = Written::new(MARKED, &format!("needs = [\"pty\"]\n{ONE_STEP}"));

    let error = written.record().expect_err("no record");

    assert!(
        matches!(&error, RecordError::Deferred(missing) if *missing == [Facility::Pty]),
        "{error:?}"
    );
    assert!(!written.has_record());
    assert!(!written.marker().exists());
}

#[test]
fn a_record_names_a_reference_commit_its_meta_file_reads() {
    let written = Written::new(MARKED, ONE_STEP);
    let options = RecordOptions {
        reference_commit: "main".to_owned(),
        ..options()
    };

    let error = record(&written.dir, &options).expect_err("no record");

    assert!(
        matches!(&error, RecordError::Meta(invalid) if invalid.key == "reference_commit"),
        "{error:?}"
    );
    assert!(!written.has_record());
    assert!(!written.marker().exists());
}

#[test]
fn stored_content_that_still_holds_the_root_is_not_recorded() {
    // A file that is not UTF-8 keeps the root, and a rule that ignores it keeps the comparison from seeing.
    let rule = "[[compare.file]]\npath = \"ws/made\"\nmode = \"ignore\"\nreason = \"r\"\n";
    let written = Written::new(
        "printf '\\377' > made\npwd >> made\n",
        &format!("{ONE_STEP}{rule}"),
    );

    let error = written.record().expect_err("no record");

    assert!(
        matches!(&error, RecordError::HoldsRoot { step: 1, path } if path == "ws/made"),
        "{error:?}"
    );
    assert!(!written.has_record());
}
