//! The replay of every scenario of a root: how each scenario ends, what is written of it, and what is
//! never run.
//!
//! The executable of every test is `/bin/sh`, and the stand-in it runs is a script of the scenario's
//! `files/`, which the fixture copies into the workspace directory: the first argument of each step. The
//! stand-in leaves a line with the id of its scenario in a marker file each time it runs, and exits with
//! another code once the test has asked that of its scenario, which changes no file of the scenario.

use std::path::{Path, PathBuf};

use winter_workspace_test_support::scenario::Facility;
use winter_workspace_test_support::scenario::compare::{Difference, Dimension};
use winter_workspace_test_support::scenario::record::{RecordOptions, record};
use winter_workspace_test_support::scenario::runner::{
    ReplayOptions, ReplayReport, list, replay_all,
};
use winter_workspace_test_support::{Exit, TempTree, invoke};

/// The commit the reference of every root is pinned at.
const COMMIT: &str = "b57b020e99e10a04d8bb621d0b03ddb2c983a090";

/// Another commit a reference may be pinned at.
const OTHER_COMMIT: &str = "0123456789abcdef0123456789abcdef01234567";

/// Names the fixture a child test process was started to run. A fixture does nothing without its own name here.
const FIXTURE_VARIABLE: &str = "WINTER_TEST_FIXTURE";

/// Names the scenarios root a fixture replays.
const ROOT_VARIABLE: &str = "WINTER_TEST_REPLAY_ROOT";

/// A test of this file that replays where trees are kept, so it runs only in a child process, whose trees
/// are created inside the tree of the test that started it.
const KEEP_FIXTURE: &str = "fixture_a_replay_keeps_the_tree_of_a_failed_scenario";

/// The stand-in of the scenario `@ID@`. `@HOST@` is a directory outside every tree a step runs in.
const STAND_IN: &str = "echo @ID@ >> @HOST@/marker\n\
                        echo made > made.txt\n\
                        if [ -e @HOST@/differ/@ID@ ]; then exit 3; fi\n";

/// A scenarios root under construction, in a tree of its own: the trees a replay runs in are others, so
/// everything of this one is outside them.
struct Root {
    host: TempTree,
    root: PathBuf,
}

impl Root {
    /// A root that holds its reference and nothing else.
    fn new() -> Self {
        let host = TempTree::new().expect("a tree for the scenarios root");
        let root = host.root().join("tmp/scenarios");
        std::fs::create_dir_all(&root).expect("the scenarios root is created");
        let written = Self { host, root };
        written.file("reference.toml", &format!("commit = \"{COMMIT}\"\n"));
        written
    }

    /// Writes `content` to `path` of the root, with the directories on the way.
    fn file(&self, path: &str, content: &str) {
        let file = self.root.join(path);
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

    /// Writes the scenario `id`, which has one step that runs the stand-in. `top` are lines of the
    /// description before the step, and `rest` lines after it.
    fn describe(&self, id: &str, top: &str, rest: &str) {
        let description = format!("summary = \"s\"\n{top}[[step]]\nargs = [\"stand-in\"]\n{rest}");
        self.file(&format!("{id}/scenario.toml"), &description);
        let outside = self.outside();
        let script = STAND_IN
            .replace("@HOST@", outside.to_str().expect("a path that is text"))
            .replace("@ID@", id);
        self.file(&format!("{id}/files/stand-in"), &script);
    }

    /// Records the scenario `id` with the stand-in.
    fn record(&self, id: &str) {
        let options = RecordOptions {
            executable: PathBuf::from("/bin/sh"),
            extra_env: Vec::new(),
            redactions: Vec::new(),
            reference_commit: COMMIT.to_owned(),
            reference_runtime: "stand-in 1".to_owned(),
            rerecord: false,
            keep: false,
        };
        record(&self.root.join(id), &options).expect("the scenario records");
    }

    /// Writes and records each of the scenarios `ids`.
    fn recorded(&self, ids: &[&str]) {
        for id in ids {
            self.describe(id, "", "");
            self.record(id);
        }
    }

    /// Makes the stand-in of the scenario `id` exit with another code from now on.
    fn differ(&self, id: &str) {
        let asked = self.outside().join("differ").join(id);
        std::fs::create_dir_all(asked.parent().expect("a file has a parent"))
            .expect("the directories are created");
        std::fs::write(asked, "").expect("the file is written");
    }

    /// How many times the stand-in of the scenario `id` has run.
    fn runs(&self, id: &str) -> usize {
        let marker = std::fs::read_to_string(self.outside().join("marker")).unwrap_or_default();
        marker.lines().filter(|line| *line == id).count()
    }

    /// Replays the scenarios `filters` choose on `jobs` workers, and answers the report with what was
    /// written.
    fn replay_with(&self, filters: &[&str], jobs: usize) -> (ReplayReport, String) {
        replay(&self.root, filters, jobs)
    }

    /// Replays every scenario on two workers.
    fn replay(&self) -> (ReplayReport, String) {
        self.replay_with(&[], 2)
    }
}

/// Replays the scenarios of `root` that `filters` choose on `jobs` workers with the stand-in.
fn replay(root: &Path, filters: &[&str], jobs: usize) -> (ReplayReport, String) {
    let options = ReplayOptions {
        executable: PathBuf::from("/bin/sh"),
        scenarios_root: root.to_path_buf(),
        filters: filters.iter().map(|filter| (*filter).to_owned()).collect(),
        jobs,
    };
    let mut out = Vec::new();
    let report = replay_all(&options, &mut out);
    (report, String::from_utf8(out).expect("the output is text"))
}

/// The differences of the one scenario of `report` that failed, which is `id`.
#[track_caller]
fn failed<'r>(report: &'r ReplayReport, id: &str) -> &'r [Difference] {
    match report.failed.as_slice() {
        [(failed, differences)] if failed == id => differences,
        other => panic!("not the one failure {id}: {other:?}"),
    }
}

/// The lines of `output`.
fn lines(output: &str) -> Vec<&str> {
    output.lines().collect()
}

#[test]
fn recorded_scenarios_replayed_with_the_same_stand_in_pass_in_the_order_of_their_ids() {
    let root = Root::new();
    root.recorded(&["b-cmd/one", "a-cmd/two", "a-cmd/one"]);

    let (report, output) = root.replay();

    assert_eq!(
        output,
        "ok a-cmd/one\nok a-cmd/two\nok b-cmd/one\nscenarios: 3 passed, 0 failed, 0 deferred\n"
    );
    assert_eq!(report.passed, ["a-cmd/one", "a-cmd/two", "b-cmd/one"]);
    assert_eq!(report.failed, []);
    assert_eq!(report.deferred, []);
    // Twice for the record, once for the replay.
    for id in ["a-cmd/one", "a-cmd/two", "b-cmd/one"] {
        assert_eq!(root.runs(id), 3, "{id}");
    }
}

#[test]
fn a_scenario_whose_step_exits_with_another_code_fails_with_an_exit_block() {
    let root = Root::new();
    root.recorded(&["a-cmd/one", "a-cmd/two", "b-cmd/one"]);
    root.differ("a-cmd/two");

    let (report, output) = root.replay();

    assert_eq!(
        lines(&output),
        [
            "ok a-cmd/one",
            "FAILED a-cmd/two",
            "  a-cmd/two: Exit, step 1, exit-code",
            "    recorded: 0",
            "    observed: 3",
            "ok b-cmd/one",
            "scenarios: 2 passed, 1 failed, 0 deferred",
        ]
    );
    assert_eq!(report.passed, ["a-cmd/one", "b-cmd/one"]);
    let differences = failed(&report, "a-cmd/two");
    assert_eq!(differences.len(), 1, "{differences:?}");
    assert_eq!(differences[0].dimension, Dimension::Exit);
    assert_eq!(differences[0].step, Some(1));
}

#[test]
fn a_scenario_that_needs_a_facility_is_deferred_and_not_run() {
    let root = Root::new();
    root.recorded(&["a-cmd/one"]);
    root.describe("a-cmd/terminal", "needs = [\"pty\", \"signal\"]\n", "");

    let (report, output) = root.replay();

    assert_eq!(
        lines(&output),
        [
            "ok a-cmd/one",
            "deferred a-cmd/terminal needs pty,signal",
            "scenarios: 1 passed, 0 failed, 1 deferred",
        ]
    );
    assert_eq!(report.failed, []);
    assert_eq!(
        report.deferred,
        [(
            "a-cmd/terminal".to_owned(),
            vec![Facility::Pty, Facility::Signal]
        )]
    );
    assert_eq!(root.runs("a-cmd/terminal"), 0);
}

#[test]
fn an_unrecorded_a_malformed_and_a_stubbed_scenario_each_fail_and_are_not_run() {
    let root = Root::new();
    root.describe("a-cmd/malformed", "bogus = 1\n", "");
    root.describe(
        "a-cmd/stubbed",
        "",
        "[[stub]]\nname = \"docker\"\nexit = 0\nenv = []\n",
    );
    root.describe("a-cmd/unrecorded", "", "");

    let (report, output) = root.replay();

    assert_eq!(report.passed, [] as [&str; 0]);
    assert_eq!(report.deferred, []);
    let subjects: Vec<(&str, Dimension, &str)> = report
        .failed
        .iter()
        .map(|(id, differences)| {
            assert_eq!(differences.len(), 1, "{differences:?}");
            (
                id.as_str(),
                differences[0].dimension,
                differences[0].subject.as_str(),
            )
        })
        .collect();
    assert_eq!(
        subjects,
        [
            ("a-cmd/malformed", Dimension::Malformed, "bogus"),
            ("a-cmd/stubbed", Dimension::Malformed, "stub"),
            ("a-cmd/unrecorded", Dimension::Malformed, "record"),
        ]
    );
    let output = lines(&output);
    assert!(output.contains(&"FAILED a-cmd/malformed"), "{output:?}");
    assert!(
        output.contains(&"  a-cmd/malformed: Malformed, no step, bogus"),
        "{output:?}"
    );
    assert!(
        output.contains(&"  a-cmd/stubbed: Malformed, no step, stub"),
        "{output:?}"
    );
    assert!(
        output.contains(&"    recorded: unrecorded: the scenario directory holds no record/"),
        "{output:?}"
    );
    assert_eq!(
        output.last(),
        Some(&"scenarios: 0 passed, 3 failed, 0 deferred")
    );
    for id in ["a-cmd/malformed", "a-cmd/stubbed", "a-cmd/unrecorded"] {
        assert_eq!(root.runs(id), 0, "{id}");
    }
}

#[test]
fn a_filter_replays_the_scenarios_whose_id_holds_it_and_no_other() {
    let root = Root::new();
    root.recorded(&["a-cmd/one", "a-cmd/two", "b-cmd/one"]);
    root.describe("b-cmd/unrecorded", "", "");

    let (report, output) = root.replay_with(&["a-cmd", "b-cmd/one"], 2);
    let (none, nothing) = root.replay_with(&["no-such"], 2);

    assert_eq!(
        output,
        "ok a-cmd/one\nok a-cmd/two\nok b-cmd/one\nscenarios: 3 passed, 0 failed, 0 deferred\n"
    );
    assert_eq!(report.failed, []);
    assert_eq!(nothing, "scenarios: 0 passed, 0 failed, 0 deferred\n");
    assert_eq!(none, ReplayReport::default());

    let (report, output) = root.replay_with(&["two"], 2);

    assert_eq!(
        output,
        "ok a-cmd/two\nscenarios: 1 passed, 0 failed, 0 deferred\n"
    );
    assert_eq!(report.passed, ["a-cmd/two"]);
    assert_eq!(root.runs("a-cmd/one"), 3);
    assert_eq!(root.runs("a-cmd/two"), 4);
}

#[test]
fn the_list_names_every_scenario_with_its_state_and_runs_none() {
    let root = Root::new();
    root.recorded(&["a-cmd/recorded"]);
    root.describe("a-cmd/deferred", "needs = [\"clock\"]\n", "");
    root.describe("a-cmd/malformed", "bogus = 1\n", "");
    root.describe("b-cmd/unrecorded", "", "");
    root.file("notes.txt", "stray\n");
    root.file("a-cmd/notes.txt", "stray\n");
    let listed = |filters: &[&str]| {
        let filters: Vec<String> = filters.iter().map(|filter| (*filter).to_owned()).collect();
        let mut out = Vec::new();
        list(&root.root, &filters, &mut out).expect("the root is read");
        String::from_utf8(out).expect("the output is text")
    };

    assert_eq!(
        listed(&[]),
        "a-cmd/deferred deferred\na-cmd/malformed malformed\na-cmd/notes.txt malformed\n\
         a-cmd/recorded recorded\nb-cmd/unrecorded unrecorded\nnotes.txt malformed\n"
    );
    assert_eq!(listed(&["b-cmd"]), "b-cmd/unrecorded unrecorded\n");
    // Twice for the record, and never for a list.
    assert_eq!(root.runs("a-cmd/recorded"), 2);
    assert_eq!(root.runs("b-cmd/unrecorded"), 0);
}

#[test]
fn what_is_written_is_the_same_on_one_worker_and_on_eight() {
    let root = Root::new();
    root.recorded(&[
        "a-cmd/one",
        "a-cmd/two",
        "b-cmd/one",
        "b-cmd/two",
        "c-cmd/one",
        "c-cmd/two",
    ]);
    root.describe("b-cmd/deferred", "needs = [\"pty\"]\n", "");
    root.describe("c-cmd/unrecorded", "", "");
    root.differ("b-cmd/two");

    let (one_report, one) = root.replay_with(&[], 1);
    let (eight_report, eight) = root.replay_with(&[], 8);

    assert_eq!(one, eight);
    assert_eq!(one_report, eight_report);
    let first: Vec<&str> = one.lines().filter(|line| !line.starts_with(' ')).collect();
    assert_eq!(
        first,
        [
            "ok a-cmd/one",
            "ok a-cmd/two",
            "deferred b-cmd/deferred needs pty",
            "ok b-cmd/one",
            "FAILED b-cmd/two",
            "ok c-cmd/one",
            "ok c-cmd/two",
            "FAILED c-cmd/unrecorded",
            "scenarios: 5 passed, 2 failed, 1 deferred",
        ]
    );
}

#[test]
fn a_record_that_does_not_read_whole_fails_naming_the_entry_and_is_not_run() {
    type Damage = fn(&Path);
    let damage: [(&str, Damage); 5] = [
        ("record/steps/01/after", |record| {
            std::fs::remove_file(record.join("steps/01/after/files/ws/made.txt"))
                .expect("the stored content is removed");
        }),
        ("record/steps/01/after", |record| {
            std::fs::write(record.join("steps/01/after/files/ws/made.txt"), "other\n")
                .expect("the stored content is changed");
        }),
        ("record/steps/01/exit-code", |record| {
            std::fs::write(record.join("steps/01/exit-code"), "none\n")
                .expect("the exit file is changed");
        }),
        ("record/steps/01/stderr", |record| {
            std::fs::remove_file(record.join("steps/01/stderr")).expect("the stream is removed");
        }),
        ("record/steps", |record| {
            std::fs::create_dir(record.join("steps/02")).expect("a step directory is added");
        }),
    ];
    for (subject, damage) in damage {
        let root = Root::new();
        root.recorded(&["a-cmd/one"]);
        damage(&root.root.join("a-cmd/one/record"));

        let (report, output) = root.replay();

        let differences = failed(&report, "a-cmd/one");
        assert_eq!(differences.len(), 1, "{differences:?}");
        assert_eq!(differences[0].dimension, Dimension::Malformed);
        assert_eq!(differences[0].subject, subject);
        assert!(
            differences[0].recorded.starts_with("unreadable: "),
            "{differences:?}"
        );
        assert!(output.contains("FAILED a-cmd/one\n"), "{output}");
        assert!(output.contains("  a-cmd/one: Malformed, "), "{output}");
        assert!(output.contains(subject), "{output}");
        assert_eq!(report.passed, [] as [&str; 0]);
        assert_eq!(root.runs("a-cmd/one"), 2, "{subject}");
    }
}

#[test]
fn a_record_made_at_another_commit_or_of_another_description_is_stale_and_not_run() {
    let root = Root::new();
    root.recorded(&["a-cmd/one"]);
    root.file("reference.toml", &format!("commit = \"{OTHER_COMMIT}\"\n"));

    let (report, _) = root.replay();

    let differences = failed(&report, "a-cmd/one");
    assert_eq!(differences.len(), 1, "{differences:?}");
    assert_eq!(differences[0].dimension, Dimension::StaleRecord);
    assert_eq!(differences[0].subject, "reference_commit");
    assert_eq!(differences[0].recorded, COMMIT);
    assert_eq!(differences[0].observed, OTHER_COMMIT);

    let root = Root::new();
    root.recorded(&["a-cmd/one"]);
    root.file("a-cmd/one/files/added.txt", "added\n");

    let (report, _) = root.replay();

    let differences = failed(&report, "a-cmd/one");
    assert_eq!(differences.len(), 1, "{differences:?}");
    assert_eq!(differences[0].dimension, Dimension::StaleRecord);
    assert_eq!(differences[0].subject, "description");
    assert_eq!(root.runs("a-cmd/one"), 2);

    let root = Root::new();
    root.recorded(&["a-cmd/one"]);
    std::fs::remove_file(root.root.join("reference.toml")).expect("the reference is removed");

    let (report, _) = root.replay();

    let differences = failed(&report, "a-cmd/one");
    assert_eq!(differences[0].dimension, Dimension::StaleRecord);
    assert_eq!(differences[0].subject, "reference.toml");
    assert_eq!(root.runs("a-cmd/one"), 2);
}

#[test]
fn what_is_no_scenario_under_the_root_fails_the_replay() {
    let root = Root::new();
    root.recorded(&["a-cmd/one"]);
    root.file("notes.txt", "stray\n");
    root.file("a-cmd/notes.txt", "stray\n");
    std::fs::create_dir(root.root.join("empty")).expect("the directory is created");
    std::fs::create_dir_all(root.root.join("x-y/z")).expect("the directory is created");

    let (report, output) = root.replay();

    assert_eq!(report.passed, ["a-cmd/one"]);
    let failed: Vec<&str> = report.failed.iter().map(|(id, _)| id.as_str()).collect();
    assert_eq!(failed, ["a-cmd/notes.txt", "empty", "notes.txt", "x-y/z"]);
    assert!(output.contains("FAILED x-y/z\n"), "{output}");
    assert!(
        output.ends_with("scenarios: 1 passed, 4 failed, 0 deferred\n"),
        "{output}"
    );
}

#[test]
fn a_root_without_a_scenario_reports_none_and_a_root_that_is_not_there_fails() {
    let root = Root::new();

    let (report, output) = root.replay();

    assert_eq!(output, "scenarios: 0 passed, 0 failed, 0 deferred\n");
    assert_eq!(report, ReplayReport::default());

    let absent = root.root.join("absent");

    let (report, output) = replay(&absent, &[], 2);

    let id = absent.display().to_string();
    let differences = failed(&report, &id);
    assert_eq!(differences[0].dimension, Dimension::Malformed);
    assert_eq!(
        lines(&output).first(),
        Some(&format!("FAILED {id}").as_str())
    );
    assert!(
        output.ends_with("scenarios: 0 passed, 1 failed, 0 deferred\n"),
        "{output}"
    );
}

/// Writes and records a scenario with a registered deviation whose expectation is the files `expected`,
/// by their path under `expected/`.
fn deviating(root: &Root, id: &str, expected: &[(&str, &str)]) {
    for (path, content) in expected {
        root.file(&format!("{id}/expected/{path}"), content);
    }
    root.describe(id, "", "[deviation]\ndecision = \"decision:a/b\"\n");
    root.record(id);
}

#[test]
fn a_scenario_with_an_expectation_is_held_to_it_and_not_to_its_record() {
    let root = Root::new();
    deviating(&root, "a-cmd/one", &[("steps/01/exit-code", "3\n")]);

    let (report, output) = root.replay();

    assert_eq!(
        lines(&output),
        [
            "FAILED a-cmd/one",
            "  a-cmd/one: Exit, step 1, exit-code",
            "    recorded: 3",
            "    observed: 0",
            "scenarios: 0 passed, 1 failed, 0 deferred",
        ]
    );
    assert_eq!(failed(&report, "a-cmd/one").len(), 1);

    root.differ("a-cmd/one");

    let (report, output) = root.replay();

    assert_eq!(
        output,
        "ok a-cmd/one\nscenarios: 1 passed, 0 failed, 0 deferred\n"
    );
    assert_eq!(report.passed, ["a-cmd/one"]);
}

#[test]
fn an_expectation_that_holds_what_none_may_fails_and_is_not_run() {
    for path in [
        "steps/01/before/tree.txt",
        "steps/02/exit-code",
        "steps/1/exit-code",
        "notes.txt",
    ] {
        let root = Root::new();
        deviating(&root, "a-cmd/one", &[(path, "0\n")]);

        let (report, _) = root.replay();

        let differences = failed(&report, "a-cmd/one");
        assert_eq!(differences.len(), 1, "{differences:?}");
        assert_eq!(differences[0].dimension, Dimension::Malformed, "{path}");
        assert_eq!(differences[0].subject, "expected", "{path}");
        assert_eq!(root.runs("a-cmd/one"), 2, "{path}");
    }
}

#[test]
fn a_difference_of_more_than_one_line_is_written_as_a_unified_diff() {
    let root = Root::new();
    root.recorded(&["a-cmd/one"]);
    // The record now states another content for the file the step makes, with the hash of that content.
    let record = root.root.join("a-cmd/one/record/steps/01/after");
    std::fs::write(record.join("files/ws/made.txt"), "made\nonce\n").expect("the content");
    let tree = std::fs::read_to_string(record.join("tree.txt")).expect("the listing");
    let tree = tree.replace(
        "c924b71ea6613bd011834f42d0b441afadffaa30",
        "8b9f40fdc178c8414f100fa52462024021b56d0e",
    );
    std::fs::write(record.join("tree.txt"), tree).expect("the listing");

    let (report, output) = root.replay();

    let differences = failed(&report, "a-cmd/one");
    assert!(
        differences
            .iter()
            .any(|found| found.dimension == Dimension::Files && found.subject == "ws/made.txt"),
        "{differences:?}"
    );
    assert!(
        output.contains(
            "  a-cmd/one: Files, step 1, ws/made.txt\n    --- recorded\n    +++ observed\n    \
             @@ -1,3 +1,2 @@\n     made\n    -once\n     \n"
        ),
        "{output}"
    );
}

#[test]
fn a_failed_scenario_whose_tree_cannot_be_removed_keeps_its_differences() {
    use std::os::unix::fs::PermissionsExt;

    let writable = |directory: &Path, mode: u32| {
        std::fs::set_permissions(directory, std::fs::Permissions::from_mode(mode))
    };
    let root = Root::new();
    root.describe("a-cmd/one", "", "");
    let outside = root.outside();
    let host = outside.to_str().expect("a path that is text");
    // Once asked to differ, the stand-in leaves a directory that holds a file and may not be written.
    let script = format!(
        "echo made > made.txt\n\
         if [ -e {host}/differ/a-cmd/one ]; then\n\
         pwd > {host}/where; mkdir locked; echo held > locked/held; chmod 555 locked; exit 3\n\
         fi\n"
    );
    root.file("a-cmd/one/files/stand-in", &script);
    root.record("a-cmd/one");
    root.differ("a-cmd/one");
    // Permissions do not bind the superuser, who removes the tree as if nothing in it were locked.
    let probe = outside.join("probe");
    std::fs::create_dir(&probe).expect("a directory outside the trees");
    let locked = writable(&probe, 0o555);
    let binds = std::fs::write(probe.join("file"), "").is_err();
    let unlocked = writable(&probe, 0o755);
    locked
        .and(unlocked)
        .expect("the modes of the probe are set");
    // Where trees are kept the replay does not try to remove this one.
    let kept = std::env::var_os("WINTER_TEST_KEEP").is_some_and(|keep| keep == "1");
    let unremovable = binds && !kept;

    let (report, output) = root.replay();

    // The tree is made removable again, and removed, before anything that can fail.
    let ran_in = std::fs::read_to_string(outside.join("where"));
    let left = ran_in.as_ref().ok().map(|ran_in| {
        let workspace = Path::new(ran_in.trim_end());
        let tree = workspace.parent().unwrap_or(workspace);
        let left = tree.exists();
        let _ = writable(&workspace.join("locked"), 0o755);
        (left, std::fs::remove_dir_all(tree))
    });
    ran_in.expect("where the step ran");
    let (left, removed) = left.expect("the tree of the step");
    assert_eq!(left, binds || kept);
    assert_eq!(removed.is_ok(), left, "{removed:?}");

    let differences = failed(&report, "a-cmd/one");
    let exit = &differences[0];
    assert_eq!((exit.dimension, exit.step), (Dimension::Exit, Some(1)));
    assert_eq!((exit.recorded.as_str(), exit.observed.as_str()), ("0", "3"));
    let of_the_tree: Vec<&Difference> = differences
        .iter()
        .filter(|found| found.dimension == Dimension::Fixture)
        .collect();
    assert_eq!(
        of_the_tree.len(),
        usize::from(unremovable),
        "{differences:?}"
    );
    if unremovable {
        let unremoved = differences.last().expect("a difference");
        assert_eq!((unremoved.step, unremoved.subject.as_str()), (None, "tree"));
        assert!(
            unremoved.observed.starts_with("the isolated tree: "),
            "{unremoved:?}"
        );
    }
    assert!(
        output.contains("  a-cmd/one: Exit, step 1, exit-code\n"),
        "{output}"
    );
    assert_eq!(
        output.contains("  a-cmd/one: Fixture, no step, tree\n"),
        unremovable,
        "{output}"
    );
    assert!(!output.contains("panicked"), "{output}");
}

#[test]
fn a_replay_where_trees_are_kept_keeps_the_tree_of_a_failed_scenario_alone() {
    let root = Root::new();
    root.recorded(&["a-cmd/one", "a-cmd/two"]);
    root.differ("a-cmd/two");
    let trees = root.host.root().join("tmp/trees");
    std::fs::create_dir_all(&trees).expect("a directory inside the tree");
    let this_test_binary = std::env::current_exe().expect("the path of the test executable");
    let mut invocation = root.host.invocation(&this_test_binary);
    invocation.args = [KEEP_FIXTURE, "--ignored", "--exact", "--nocapture"]
        .iter()
        .map(Into::into)
        .collect();
    let text = |path: &Path| path.to_str().expect("a path that is text").to_owned();
    for (name, value) in [
        ("WINTER_TEST_TMPDIR", text(&trees)),
        ("WINTER_TEST_KEEP", "1".to_owned()),
        (FIXTURE_VARIABLE, KEEP_FIXTURE.to_owned()),
        (ROOT_VARIABLE, text(&root.root)),
    ] {
        invocation.env.set(name, &value);
    }

    let observation = invoke(invocation).expect("a child test run");

    assert_eq!(observation.exit, Exit::Code(0), "{observation:?}");
    let stdout = String::from_utf8_lossy(&observation.stdout);
    assert!(
        stdout.contains("test result: ok. 1 passed"),
        "{observation:?}"
    );
    let kept: Vec<PathBuf> = std::fs::read_dir(&trees)
        .expect("a directory")
        .map(|entry| entry.expect("an entry").path())
        .collect();
    assert_eq!(kept.len(), 1, "{kept:?}");
    assert!(kept[0].join("ws/made.txt").is_file());
    let named = format!(
        "  a-cmd/two: kept the isolated tree {}\n",
        kept[0].display()
    );
    assert!(stdout.contains(&named), "{stdout}");
    assert_eq!(root.runs("a-cmd/one"), 3);
}

#[test]
#[ignore = "keeps a tree: the test of a kept replay runs it in a child process"]
fn fixture_a_replay_keeps_the_tree_of_a_failed_scenario() {
    if std::env::var_os(FIXTURE_VARIABLE).is_none_or(|fixture| fixture != KEEP_FIXTURE) {
        return;
    }
    let root: PathBuf = std::env::var_os(ROOT_VARIABLE)
        .expect("the root a fixture replays")
        .into();

    let (report, output) = replay(&root, &[], 2);

    print!("{output}");
    assert_eq!(report.passed, ["a-cmd/one"]);
    assert_eq!(failed(&report, "a-cmd/two").len(), 1);
}
