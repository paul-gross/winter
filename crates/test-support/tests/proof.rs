//! Proof that a wrong implementation is caught: recorded `ws-index` and `repo-remove` scenarios of the
//! repository, copied into a scenarios root of a tree of the test and replayed there with a stand-in for
//! the binary.
//!
//! The stand-in is a shell script the test writes. It prints a document the test chose and exits with a
//! code the test chose, whatever its arguments are. A stand-in for `repo remove` may first replace the
//! configuration file of the workspace it is started in with a text the test chose, or create an empty
//! file there; every other stand-in writes no file. The scenarios of the repository are read and never written, and no test starts the
//! tool that made their records.

use std::fs::Permissions;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use winter_workspace_test_support::scenario::compare::{Difference, Dimension};
use winter_workspace_test_support::scenario::runner::{ReplayOptions, ReplayReport, replay_all};
use winter_workspace_test_support::{Exit, TempTree, invoke};

/// The scenarios root of the repository.
const SCENARIOS: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../scenarios");

/// A name that hashes to a slot under the default configuration, with nothing recorded.
const HASHED: &str = "ws-index/hashed-suggested";

/// The same name, with another name recorded at the slot it hashes to.
const SLOT_TAKEN: &str = "ws-index/suggested-slot-taken";

/// The last block of a configuration without comments beside a repository header is removed.
const LAST_BLOCK: &str = "repo-remove/removes-last-block";

/// A middle block is removed under the registered deviation: the scenario has an expectation.
const MIDDLE_BLOCK: &str = "repo-remove/removes-middle-block";

/// The configuration file `repo remove` edits, as a snapshot names it: from the root of a tree.
const CONFIGURATION: &str = "ws/.winter/config.toml";

/// The same file from the workspace directory, where a step is started.
const CONFIGURATION_IN_WORKSPACE: &str = ".winter/config.toml";

/// No block of the commented configuration has the name the step asks for: the step exits 1 and prints
/// nothing.
const NOT_FOUND: &str = "repo-remove/not-found";

/// The local configuration file, which no `repo-remove` scenario holds, from the root of a tree.
const LOCAL_CONFIGURATION: &str = "ws/.winter/config.local.toml";

/// The same file from the workspace directory.
const LOCAL_CONFIGURATION_IN_WORKSPACE: &str = ".winter/config.local.toml";

/// Where a step of a record or of an expectation stores that file as the step left it.
const CONFIGURATION_STORED: &str = "after/files/ws/.winter/config.toml";

/// A write of the configuration file that is not the one replay compares with differs in the line of
/// that file in the listing and in its content, and in nothing else: the status of the root repository
/// names the file as modified whatever it holds. The listing stands before the content: the order in which
/// a comparison reports the two.
const WRITTEN_FILE_DIFFERS: [(Dimension, Option<u32>, &str); 2] = [
    (Dimension::Tree, Some(1), CONFIGURATION),
    (Dimension::Files, Some(1), CONFIGURATION),
];

/// Copies the directory `from` to `to` with everything under it.
fn copy(from: &Path, to: &Path) {
    std::fs::create_dir_all(to).expect("the directory is created");
    for entry in std::fs::read_dir(from).expect("a directory") {
        let entry = entry.expect("an entry");
        let target = to.join(entry.file_name());
        if entry.file_type().expect("a file type").is_dir() {
            copy(&entry.path(), &target);
        } else {
            std::fs::copy(entry.path(), &target).expect("the file is copied");
        }
    }
}

/// The files under `directory`, each as its path from there, sorted.
fn files_under(directory: &Path) -> Vec<String> {
    fn collect(directory: &Path, prefix: &str, found: &mut Vec<String>) {
        for entry in std::fs::read_dir(directory).expect("a directory") {
            let entry = entry.expect("an entry");
            let name = entry.file_name();
            let path = format!("{prefix}{}", name.to_str().expect("a name that is text"));
            if entry.file_type().expect("a file type").is_dir() {
                collect(&entry.path(), &format!("{path}/"), found);
            } else {
                found.push(path);
            }
        }
    }
    let mut found = Vec::new();
    collect(directory, "", &mut found);
    found.sort();
    found
}

/// What a stand-in does in the workspace it is started in before it prints.
enum Writes<'a> {
    Nothing,
    /// Leaves this text as the configuration file.
    Configuration(&'a str),
    /// Creates an empty file at this path from the workspace directory.
    EmptyFile(&'a str),
}

/// A copy of one recorded scenario of the repository in a scenarios root of its own, in a tree of its
/// own: the trees a replay runs in are others.
struct Copied {
    host: TempTree,
    root: PathBuf,
    id: &'static str,
    /// How many stand-ins were written.
    written: usize,
}

impl Copied {
    /// A root that holds the reference of the repository and the scenario `id`.
    fn new(id: &'static str) -> Self {
        let host = TempTree::new().expect("a tree for the scenarios root");
        let root = host.root().join("tmp/scenarios");
        let scenarios = Path::new(SCENARIOS);
        copy(&scenarios.join(id), &root.join(id));
        std::fs::copy(
            scenarios.join("reference.toml"),
            root.join("reference.toml"),
        )
        .expect("the reference is copied");
        Self {
            host,
            root,
            id,
            written: 0,
        }
    }

    /// The content of `file` of the one step under `directory` of the scenario.
    fn stored(&self, directory: &str, file: &str) -> String {
        let path = self
            .root
            .join(self.id)
            .join(directory)
            .join("steps/01")
            .join(file);
        std::fs::read_to_string(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display()))
    }

    /// The content of `file` of the record of the one step.
    fn recorded(&self, file: &str) -> String {
        self.stored("record", file)
    }

    /// The content of `file` of the expectation of the one step.
    fn expected(&self, file: &str) -> String {
        self.stored("expected", file)
    }

    /// The document the record holds as the output of the step.
    fn document(&self) -> serde_json::Value {
        serde_json::from_str(&self.recorded("stdout.json")).expect("a recorded document")
    }

    /// The recorded document with `index` in the place of its index.
    fn document_with_index(&self, index: i64) -> String {
        let mut document = self.document();
        document["index"] = index.into();
        format!("{document}\n")
    }

    /// Replays the scenario with a stand-in that prints `stdout` and exits with `code`, and answers the
    /// report.
    fn replay(&mut self, stdout: &str, code: u8) -> ReplayReport {
        self.replay_with(&Writes::Nothing, stdout, code)
    }

    /// Replays the scenario with a stand-in that leaves `configuration` as the configuration file of the
    /// workspace it is started in, prints the recorded output, and exits with the recorded code, which
    /// is 0, and answers the report.
    fn replay_writing(&mut self, configuration: &str) -> ReplayReport {
        let stdout = self.recorded("stdout");
        assert_eq!(self.recorded("exit-code"), "0\n");
        self.replay_with(&Writes::Configuration(configuration), &stdout, 0)
    }

    /// Replays the scenario with a stand-in that does what `writes` says in the workspace it is started
    /// in, prints `stdout`, and exits with `code`, and answers the report.
    fn replay_with(&mut self, writes: &Writes<'_>, stdout: &str, code: u8) -> ReplayReport {
        self.written += 1;
        let directory = self.host.root().join("cache");
        let printed = directory.join(format!("stdout-{}", self.written));
        std::fs::write(&printed, stdout).expect("the output is written");
        let write = match writes {
            Writes::Nothing => String::new(),
            Writes::Configuration(configuration) => {
                let source = directory.join(format!("configuration-{}", self.written));
                std::fs::write(&source, configuration).expect("the configuration is written");
                format!(
                    "cat {} > {CONFIGURATION_IN_WORKSPACE} || exit 70\n",
                    source.to_str().expect("a path that is text")
                )
            }
            Writes::EmptyFile(path) => format!(": > {path} || exit 70\n"),
        };
        let script = format!(
            "#!/bin/sh\n{write}cat {}\nexit {code}\n",
            printed.to_str().expect("a path that is text")
        );
        let stand_in = directory.join(format!("stand-in-{}", self.written));
        executable(&self.host, &stand_in, &script);
        let options = ReplayOptions {
            executable: stand_in,
            scenarios_root: self.root.clone(),
            filters: vec![self.id.to_owned()],
            jobs: 1,
        };
        replay_all(&options, &mut Vec::new())
    }
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
    std::fs::write(&source, content).expect("the text is written");
    let mut copy = host.invocation(Path::new("/bin/cp"));
    copy.args = vec![source.clone().into(), path.into()];
    let copied = invoke(copy).expect("a copy");
    assert_eq!(copied.exit, Exit::Code(0), "{copied:?}");
    std::fs::remove_file(&source).expect("the file beside it is removed");
    std::fs::set_permissions(path, Permissions::from_mode(0o755)).expect("an executable file");
}

/// The differences of the scenario `id`, which is the one `report` holds and which failed.
#[track_caller]
fn failed<'r>(report: &'r ReplayReport, id: &str) -> &'r [Difference] {
    assert_eq!(report.passed, [] as [&str; 0]);
    assert_eq!(report.deferred, []);
    match report.failed.as_slice() {
        [(failed, differences)] if failed == id => differences,
        other => panic!("not the one failure {id}: {other:?}"),
    }
}

/// Asserts that the scenario `id` is the one `report` holds, and that it passed.
#[track_caller]
fn assert_passed(report: &ReplayReport, id: &str) {
    assert_eq!(report.failed, []);
    assert_eq!(report.deferred, []);
    assert_eq!(report.passed, [id]);
}

/// The dimension, the step, and the subject of each of `differences`.
fn named(differences: &[Difference]) -> Vec<(Dimension, Option<u32>, &str)> {
    differences
        .iter()
        .map(|difference| {
            (
                difference.dimension,
                difference.step,
                difference.subject.as_str(),
            )
        })
        .collect()
}

#[test]
fn a_stand_in_that_reproduces_the_record_of_the_hashed_name_passes() {
    let mut copied = Copied::new(HASHED);
    let stdout = copied.recorded("stdout");
    assert_eq!(copied.recorded("exit-code"), "0\n");

    let report = copied.replay(&stdout, 0);

    assert_passed(&report, HASHED);
}

#[test]
fn a_stand_in_that_exits_with_another_code_differs_in_the_exit_alone() {
    let mut copied = Copied::new(HASHED);
    let stdout = copied.recorded("stdout");

    let report = copied.replay(&stdout, 1);

    let differences = failed(&report, HASHED);
    assert_eq!(
        named(differences),
        [(Dimension::Exit, Some(1), "exit-code")]
    );
    assert_eq!(
        (
            differences[0].recorded.as_str(),
            differences[0].observed.as_str()
        ),
        ("0", "1")
    );
}

#[test]
fn a_stand_in_that_prints_another_index_differs_in_the_document_at_the_index() {
    let mut copied = Copied::new(HASHED);
    assert_eq!(copied.document()["index"], 40);

    let report = copied.replay(&copied.document_with_index(12), 0);

    let differences = failed(&report, HASHED);
    assert_eq!(named(differences), [(Dimension::Json, Some(1), "/index")]);
    assert_eq!(
        (
            differences[0].recorded.as_str(),
            differences[0].observed.as_str()
        ),
        ("40", "12")
    );
}

#[test]
fn a_stand_in_that_reprints_the_recorded_document_passes_whatever_its_layout() {
    let mut copied = Copied::new(HASHED);

    let report = copied.replay(&copied.document_with_index(40), 0);

    assert_passed(&report, HASHED);
}

/// A lookup that probed past the slot another name is recorded at would answer the next one.
#[test]
fn a_stand_in_that_answers_the_slot_after_the_taken_one_differs_at_the_index() {
    let mut copied = Copied::new(SLOT_TAKEN);
    assert_eq!(copied.document()["index"], 40);
    let state = copied
        .root
        .join(SLOT_TAKEN)
        .join("files/.winter/state.toml");
    assert_eq!(
        std::fs::read_to_string(state).expect("the state file of the fixture"),
        "[env_index]\nother = 40\n"
    );

    let report = copied.replay(&copied.document_with_index(41), 0);

    let differences = failed(&report, SLOT_TAKEN);
    assert_eq!(named(differences), [(Dimension::Json, Some(1), "/index")]);
    assert_eq!(
        (
            differences[0].recorded.as_str(),
            differences[0].observed.as_str()
        ),
        ("40", "41")
    );
}

#[test]
fn a_stand_in_that_writes_the_recorded_file_of_the_last_block_passes() {
    let mut copied = Copied::new(LAST_BLOCK);
    assert!(!copied.root.join(LAST_BLOCK).join("expected").exists());
    let recorded = copied.recorded(CONFIGURATION_STORED);
    assert!(
        recorded.ends_with("url = \"https://fixture.invalid/api.git\"\n\n"),
        "{recorded:?}"
    );

    let report = copied.replay_writing(&recorded);

    assert_passed(&report, LAST_BLOCK);
}

#[test]
fn a_stand_in_that_writes_one_other_byte_differs_in_the_file_and_its_listing() {
    let mut copied = Copied::new(LAST_BLOCK);
    let recorded = copied.recorded(CONFIGURATION_STORED);
    let changed = recorded.replacen("api.git", "apj.git", 1);
    assert_eq!(changed.len(), recorded.len());
    let bytes_changed = changed
        .bytes()
        .zip(recorded.bytes())
        .filter(|(changed, recorded)| changed != recorded)
        .count();
    assert_eq!(bytes_changed, 1);

    let report = copied.replay_writing(&changed);

    assert_eq!(named(failed(&report, LAST_BLOCK)), WRITTEN_FILE_DIFFERS);
}

#[test]
fn a_stand_in_that_writes_the_expected_file_of_the_middle_block_passes() {
    let mut copied = Copied::new(MIDDLE_BLOCK);
    let expected = copied.expected(CONFIGURATION_STORED);
    assert!(expected.contains("\n# third: docs\n"), "{expected:?}");
    assert!(!expected.contains("# second: web"), "{expected:?}");
    assert_eq!(
        files_under(&copied.root.join(MIDDLE_BLOCK).join("expected")),
        [
            format!("steps/01/{CONFIGURATION_STORED}"),
            "steps/01/after/tree.txt".to_owned()
        ]
    );
    let recorded = copied.recorded(CONFIGURATION_STORED);
    assert_eq!(expected.lines().count(), recorded.lines().count());
    let lines_that_differ: Vec<(&str, &str)> = expected
        .lines()
        .zip(recorded.lines())
        .filter(|(expected, recorded)| expected != recorded)
        .collect();
    assert_eq!(lines_that_differ, [("# third: docs", "# second: web")]);

    let report = copied.replay_writing(&expected);

    assert_passed(&report, MIDDLE_BLOCK);
}

/// What the tool that made the record wrote is not what the expectation holds: its own behaviour fails.
#[test]
fn a_stand_in_that_writes_the_recorded_file_of_the_middle_block_differs_from_the_expectation() {
    let mut copied = Copied::new(MIDDLE_BLOCK);
    let recorded = copied.recorded(CONFIGURATION_STORED);
    assert!(recorded.contains("\n# second: web\n"), "{recorded:?}");
    assert!(!recorded.contains("# third: docs"), "{recorded:?}");

    let report = copied.replay_writing(&recorded);

    assert_eq!(named(failed(&report, MIDDLE_BLOCK)), WRITTEN_FILE_DIFFERS);
}

#[test]
fn a_stand_in_that_exits_1_and_prints_nothing_where_nothing_is_found_passes() {
    let mut copied = Copied::new(NOT_FOUND);
    assert_eq!(copied.recorded("exit-code"), "1\n");
    assert_eq!(copied.recorded("stdout"), "");

    let report = copied.replay("", 1);

    assert_passed(&report, NOT_FOUND);
}

/// The file is new in the listing, has content no record stores, and is untracked in the root repository.
#[test]
fn a_stand_in_that_exits_1_and_creates_the_local_configuration_file_differs_in_the_listing() {
    let mut copied = Copied::new(NOT_FOUND);

    let report = copied.replay_with(&Writes::EmptyFile(LOCAL_CONFIGURATION_IN_WORKSPACE), "", 1);

    assert_eq!(
        named(failed(&report, NOT_FOUND)),
        [
            (Dimension::Tree, Some(1), LOCAL_CONFIGURATION),
            (Dimension::Files, Some(1), LOCAL_CONFIGURATION),
            (Dimension::Git, Some(1), "ws status"),
        ]
    );
}

#[test]
fn a_stand_in_that_exits_1_and_prints_a_document_where_none_is_recorded_differs_in_the_output() {
    let mut copied = Copied::new(NOT_FOUND);
    assert_eq!(copied.recorded("stdout.json"), "");

    let report = copied.replay("{\"error\":\"not found\"}\n", 1);

    let [difference] = failed(&report, NOT_FOUND) else {
        panic!("not one difference: {report:?}");
    };
    assert_eq!(
        (
            difference.dimension,
            difference.step,
            difference.subject.as_str()
        ),
        (Dimension::Json, Some(1), "observed value 1")
    );
    assert_eq!(
        (difference.recorded.as_str(), difference.observed.as_str()),
        ("absent", "{\"error\":\"not found\"}")
    );
}
