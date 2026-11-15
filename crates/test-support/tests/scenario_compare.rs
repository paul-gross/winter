//! The comparison of a record with an observation: what each dimension reports and names, what the rules of
//! a scenario relax, what a rule that does not read or relaxes nothing is answered with, and how an
//! expectation is laid over a record. Every value is built in memory; no tree and no process is used.

use std::collections::BTreeMap;
use std::path::PathBuf;

use sha1::{Digest, Sha1};
use winter_workspace_test_support::Exit;
use winter_workspace_test_support::scenario::compare::{
    Difference, Dimension, ExpectationError, StepRecord, differences, fixture, overlay,
    stale_record,
};
use winter_workspace_test_support::scenario::normalize;
use winter_workspace_test_support::scenario::snapshot::{
    Block, Entry, EntryKind, Repository, RepositoryKind, Snapshot, SnapshotError,
};
use winter_workspace_test_support::scenario::{
    Compare, FanOut, FileMode, FileRule, Fixture, Meta, Reference, Scenario, StdoutMode, Step,
};

/// The file every state holds, unchanged by the step unless a test says otherwise.
const CONFIG: &str = "ws/.winter/config.toml";

/// A file a step writes.
const STATE: &str = "ws/.winter/state.toml";

/// The repository every state holds.
const REPOSITORY: &str = "ws/projects/alpha";

fn sha1(content: &[u8]) -> String {
    Sha1::digest(content)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// A scenario of one step for each of `modes`, under the rules `compare`.
fn scenario(modes: &[StdoutMode], compare: Compare) -> Scenario {
    Scenario {
        id: "ws-index/basic".to_owned(),
        dir: PathBuf::from("scenarios/ws-index/basic"),
        summary: "s".to_owned(),
        needs: Vec::new(),
        fixture: Fixture::default(),
        steps: modes
            .iter()
            .map(|mode| Step {
                args: vec!["doctor".to_owned()],
                cwd: ".".to_owned(),
                stdin: None,
                stdout: *mode,
                env: BTreeMap::new(),
            })
            .collect(),
        compare,
        stubs: Vec::new(),
        deviation: None,
    }
}

fn file_rule(path: &str, mode: FileMode) -> FileRule {
    FileRule {
        path: path.to_owned(),
        mode,
        reason: "not function".to_owned(),
    }
}

fn file_rules(rules: &[(&str, FileMode)]) -> Compare {
    Compare {
        files: rules
            .iter()
            .map(|(path, mode)| file_rule(path, *mode))
            .collect(),
        ..Compare::default()
    }
}

fn directory(path: &str) -> Entry {
    Entry {
        path: path.to_owned(),
        kind: EntryKind::Directory,
    }
}

fn file(path: &str, content: &[u8]) -> Entry {
    Entry {
        path: path.to_owned(),
        kind: EntryKind::File {
            executable: false,
            hash: sha1(content),
        },
    }
}

fn block(label: &str, lines: &[&str]) -> Block {
    Block {
        label: label.to_owned(),
        lines: lines.iter().map(|line| (*line).to_owned()).collect(),
    }
}

/// The state of the fixture: two directories, the configuration file, and one repository.
fn base() -> Snapshot {
    Snapshot {
        tree: vec![
            directory("ws"),
            directory("ws/.winter"),
            file(CONFIG, b"# empty\n"),
        ],
        git: vec![Repository {
            path: REPOSITORY.to_owned(),
            kind: RepositoryKind::Main,
            blocks: vec![
                block(
                    "head",
                    &[
                        "1111111111111111111111111111111111111111",
                        "refs/heads/main",
                    ],
                ),
                block(
                    "refs",
                    &[
                        "refs/heads/main 1111111111111111111111111111111111111111  ",
                        "refs/remotes/origin/main 1111111111111111111111111111111111111111  ",
                    ],
                ),
                block("config", &["core.bare=false"]),
            ],
        }],
        files: BTreeMap::new(),
    }
}

/// `snapshot` with the file `path` holding `content`, written by the step the snapshot follows.
fn with_file(mut snapshot: Snapshot, path: &str, content: &[u8]) -> Snapshot {
    snapshot.tree.retain(|entry| entry.path != path);
    snapshot.tree.push(file(path, content));
    snapshot.tree.sort_by(|a, b| a.path.cmp(&b.path));
    snapshot.files.insert(path.to_owned(), content.to_vec());
    snapshot
}

/// A step that ended with 0, printed `stdout` under `mode`, and changed nothing.
fn step(stdout: &str, mode: StdoutMode) -> StepRecord {
    let stdout_json = normalize::stdout_json(stdout.as_bytes(), mode).expect("a JSON stream");
    StepRecord {
        exit: Exit::Code(0),
        stdout: stdout.as_bytes().to_vec(),
        stderr: Vec::new(),
        stdout_json,
        before: base(),
        after: base(),
    }
}

/// A step that printed nothing and left the state `after`.
fn step_with(after: Snapshot) -> StepRecord {
    StepRecord {
        after,
        ..step("", StdoutMode::Text)
    }
}

/// The dimension, the step, and the subject of each difference.
fn named(found: &[Difference]) -> Vec<(Dimension, Option<u32>, &str)> {
    found
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

/// The differences of two one-step runs under `mode` and `compare`.
fn compared(
    mode: StdoutMode,
    compare: Compare,
    recorded: &StepRecord,
    observed: &StepRecord,
) -> Vec<Difference> {
    differences(
        &scenario(&[mode], compare),
        std::slice::from_ref(recorded),
        std::slice::from_ref(observed),
    )
}

/// The differences of two one-step outputs under `mode` and `compare`.
fn compared_output(
    mode: StdoutMode,
    compare: Compare,
    recorded: &str,
    observed: &str,
) -> Vec<Difference> {
    compared(mode, compare, &step(recorded, mode), &step(observed, mode))
}

#[test]
fn equal_runs_have_no_difference() {
    let first = StepRecord {
        after: with_file(base(), STATE, b"[env_index]\none = 1\n"),
        ..step(
            "{\"index\": 4, \"source\": \"suggested\"}\n",
            StdoutMode::Json,
        )
    };
    let second = StepRecord {
        exit: Exit::Signal(15),
        before: first.after.clone(),
        after: with_file(first.after.clone(), CONFIG, &[0xff, 0x00, 0xfe]),
        ..step("{\"type\":\"start\"}\nnot json\n", StdoutMode::JsonLines)
    };
    let third = step("human text\n", StdoutMode::Text);
    let steps = [first, second, third];
    let scenario = scenario(
        &[StdoutMode::Json, StdoutMode::JsonLines, StdoutMode::Text],
        Compare::default(),
    );

    assert_eq!(differences(&scenario, &steps, &steps), []);
}

#[test]
fn another_exit_is_one_difference_of_the_exit() {
    let recorded = step("", StdoutMode::Text);
    for (exit, shown) in [(Exit::Code(1), "1"), (Exit::Signal(0), "signal:0")] {
        let observed = StepRecord {
            exit,
            ..recorded.clone()
        };
        let found = compared(StdoutMode::Text, Compare::default(), &recorded, &observed);

        assert_eq!(named(&found), [(Dimension::Exit, Some(1), "exit-code")]);
        assert_eq!(
            (found[0].recorded.as_str(), found[0].observed.as_str()),
            ("0", shown)
        );
    }
}

#[test]
fn another_json_value_is_one_difference_named_by_its_pointer() {
    let found = compared_output(
        StdoutMode::Json,
        Compare::default(),
        r#"{"index": 40, "source": "suggested", "names": ["a", {"b/c": 1}]}"#,
        r#"{"index": 41, "source": "suggested", "names": ["a", {"b/c": 1}]}"#,
    );
    assert_eq!(named(&found), [(Dimension::Json, Some(1), "/index")]);
    assert_eq!(
        (found[0].recorded.as_str(), found[0].observed.as_str()),
        ("40", "41")
    );

    let found = compared_output(
        StdoutMode::Json,
        Compare::default(),
        r#"{"names": ["a", {"b/c": 1, "~": null}], "gone": true}"#,
        r#"{"names": ["a", {"b/c": "1", "~": null}, "more"], "new": true}"#,
    );
    assert_eq!(
        named(&found),
        [
            (Dimension::Json, Some(1), "/gone"),
            (Dimension::Json, Some(1), "/names/1/b~1c"),
            (Dimension::Json, Some(1), "/names/2"),
            (Dimension::Json, Some(1), "/new"),
        ]
    );
    assert_eq!(
        (found[0].recorded.as_str(), found[0].observed.as_str()),
        ("true", "absent")
    );
    assert_eq!(
        (found[1].recorded.as_str(), found[1].observed.as_str()),
        ("1", "\"1\"")
    );
    assert_eq!(
        (found[2].recorded.as_str(), found[2].observed.as_str()),
        ("absent", "\"more\"")
    );
}

#[test]
fn the_values_of_a_stream_are_compared_in_order_and_named_by_their_place() {
    let stream = "{\"n\": 1}\n{\"n\": 2}\n{\"n\": 3}\n";
    let found = compared_output(
        StdoutMode::JsonLines,
        Compare::default(),
        stream,
        "{\"n\": 1}\n{\"n\": 3}\n{\"n\": 2}\n",
    );
    assert_eq!(
        named(&found),
        [
            (Dimension::Json, Some(1), "value 2: /n"),
            (Dimension::Json, Some(1), "value 3: /n"),
        ]
    );

    let found = compared_output(
        StdoutMode::Json,
        Compare::default(),
        stream,
        "{\"n\": 1} {\"n\": 2}",
    );
    assert_eq!(named(&found), [(Dimension::Json, Some(1), "value 3")]);
    assert_eq!(
        (found[0].recorded.as_str(), found[0].observed.as_str()),
        ("{\"n\":3}", "absent")
    );

    let found = compared_output(StdoutMode::Json, Compare::default(), "", "[]");
    assert_eq!(
        named(&found),
        [(Dimension::Json, Some(1), "observed value 1")]
    );

    // The root of the one value of a stream is the empty pointer.
    let found = compared_output(StdoutMode::Json, Compare::default(), "[]", "{}");
    assert_eq!(named(&found), [(Dimension::Json, Some(1), "")]);
}

#[test]
fn white_space_and_the_order_of_keys_are_no_difference_and_numbers_compare_by_value() {
    for (recorded, observed) in [
        ("{\"a\": 1, \"b\": [1, 2]}\n", "{ \"b\":[1,2],\n \"a\":1 }"),
        ("{\"a\": 1}", "{\"a\": 1.0}"),
        ("{\"a\": 100}", "{\"a\": 1e2}"),
        ("[0.5]", "[5E-1]"),
    ] {
        let found = compared_output(StdoutMode::Json, Compare::default(), recorded, observed);
        assert_eq!(found, [], "{recorded} against {observed}");
    }
    let found = compared_output(StdoutMode::Json, Compare::default(), "[1]", "[true]");
    assert_eq!(named(&found), [(Dimension::Json, Some(1), "/0")]);
}

#[test]
fn what_the_canonical_form_merges_is_still_a_difference() {
    for (mode, recorded, observed, subject) in [
        (
            StdoutMode::Json,
            "{\"id\": 18446744073709551616}",
            "{\"id\": 18446744073709551617}",
            "/id",
        ),
        (StdoutMode::Json, "{\"a\": 2, \"a\": 2}", "{\"a\": 2}", "/a"),
        (StdoutMode::JsonLines, "{\"$text\": \"x\"}\n", "x\n", ""),
        (StdoutMode::JsonLines, "x\n", "{\"$text\": \"x\"}\n", ""),
        (
            StdoutMode::JsonLines,
            "{\"$text\": \"x\"}\n",
            "{\"$text\": \"y\"}\n",
            "/$text",
        ),
        (StdoutMode::Json, "[0.1]", "[0.10000000000000000001]", "/0"),
    ] {
        let (first, second) = (step(recorded, mode), step(observed, mode));
        if subject != "/$text" {
            assert_eq!(first.stdout_json, second.stdout_json, "{recorded}");
        }
        let found = compared(mode, Compare::default(), &first, &second);
        assert_eq!(
            named(&found),
            [(Dimension::Json, Some(1), subject)],
            "{recorded} against {observed}"
        );
    }
}

#[test]
fn a_repeated_key_is_compared_member_by_member() {
    let found = |recorded: &str, observed: &str| {
        compared_output(StdoutMode::Json, Compare::default(), recorded, observed)
    };
    assert_eq!(
        found("{\"a\": 1, \"a\": 2}", "{\"a\":1,\"b\":0,\"a\":2}").len(),
        1
    );
    assert_eq!(found("{\"a\": 1, \"a\": 2}", "{\"a\": 1, \"a\": 2}"), []);
    // The last member is the one the canonical form keeps, and the two forms are equal.
    let merged = found("{\"a\": 1, \"a\": 2}", "{\"a\": 2}");
    assert_eq!(
        named(&merged),
        [
            (Dimension::Json, Some(1), "/a"),
            (Dimension::Json, Some(1), "/a"),
        ]
    );
    assert_eq!(
        named(&found("{\"a\": 2, \"a\": 1}", "{\"a\": 1, \"a\": 2}")).len(),
        2
    );
}

#[test]
fn a_line_that_is_not_json_equals_any_other_and_nothing_else() {
    let lines = |observed: &str| {
        compared_output(
            StdoutMode::JsonLines,
            Compare::default(),
            "{\"n\": 1}\nCloning alpha\n{\"n\": 2}\n",
            observed,
        )
    };
    assert_eq!(
        lines("{\"n\": 1}\nsomething else entirely\n{\"n\": 2}\n"),
        []
    );
    assert_eq!(lines("{\"n\": 1}\n\n{\"n\": 2}\n"), []);

    let found = lines("{\"n\": 1}\n{\"n\": 2}\nCloning alpha\n");
    assert_eq!(
        named(&found),
        [
            (Dimension::Json, Some(1), "value 2"),
            (Dimension::Json, Some(1), "value 3"),
        ]
    );
    let found = lines("{\"n\": 1}\n{\"n\": 2}\n");
    assert_eq!(found.len(), 2, "{found:?}");
    let found = lines("{\"n\": 1}\n\"Cloning alpha\"\n{\"n\": 2}\n");
    assert_eq!(named(&found), [(Dimension::Json, Some(1), "value 2")]);
}

#[test]
fn a_stdout_json_that_is_absent_or_does_not_read_is_a_difference() {
    let recorded = step("{\"a\": 1}", StdoutMode::Json);
    for stdout_json in [
        None,
        Some("{\"a\": ".to_owned()),
        Some("{\"a\": 1} x".to_owned()),
    ] {
        let observed = StepRecord {
            stdout_json,
            ..recorded.clone()
        };
        for (first, second) in [
            (&recorded, &observed),
            (&observed, &recorded),
            (&observed, &observed),
        ] {
            let found = compared(StdoutMode::Json, Compare::default(), first, second);
            assert_eq!(named(&found), [(Dimension::Json, Some(1), "stdout.json")]);
        }
    }
}

#[test]
fn a_stdout_json_that_is_not_the_form_of_its_stdout_is_read_as_it_is_written() {
    // An expectation that replaces `stdout.json` leaves the stdout of the record beside it.
    let recorded = StepRecord {
        stdout_json: Some("{\"index\":41}\n".to_owned()),
        ..step("{\"index\": 40}", StdoutMode::Json)
    };
    let found = |observed: &str| {
        let observed = step(observed, StdoutMode::Json);
        compared(StdoutMode::Json, Compare::default(), &recorded, &observed)
    };
    assert_eq!(found("{\"index\": 41}"), []);
    assert_eq!(
        named(&found("{\"index\": 40}")),
        [(Dimension::Json, Some(1), "/index")]
    );

    // The observation is still read from its stdout, so what the canonical form merges differs from the
    // expectation: an integer beyond 64 bits, a repeated key, and an object at `$text` against a line.
    let subjects = |stored: &str, observed: &str, mode: StdoutMode| -> Vec<String> {
        let expecting = StepRecord {
            stdout_json: Some(stored.to_owned()),
            ..step("{\"other\": 0}\n", mode)
        };
        let found = compared(mode, Compare::default(), &expecting, &step(observed, mode));
        found.into_iter().map(|found| found.subject).collect()
    };
    let exact = "{\"n\":18446744073709551617}\n";
    assert_eq!(subjects(exact, exact, StdoutMode::Json), [""; 0]);
    assert_eq!(
        subjects(exact, "{\"n\": 18446744073709551618}", StdoutMode::Json),
        ["/n"]
    );
    let rounded = normalize::stdout_json(exact.as_bytes(), StdoutMode::Json)
        .expect("a JSON stream")
        .expect("a canonical form");
    assert_eq!(subjects(&rounded, exact, StdoutMode::Json), ["/n"]);
    assert_eq!(
        subjects("{\"a\":2}\n", "{\"a\": 1, \"a\": 2}", StdoutMode::Json),
        ["/a", "/a"]
    );
    let line = "{\"$text\":\"a line\"}\n";
    assert_eq!(subjects(line, line, StdoutMode::JsonLines), [""]);

    // Read from `stdout.json`, an object at `$text` stands for a line that is not JSON, under `json-lines`
    // only.
    let stored = |mode: StdoutMode| StepRecord {
        stdout_json: Some("{\"$text\":\"expected text\"}\n".to_owned()),
        ..step("", mode)
    };
    let line = step("some line\n", StdoutMode::JsonLines);
    let recorded = stored(StdoutMode::JsonLines);
    assert_eq!(
        compared(StdoutMode::JsonLines, Compare::default(), &recorded, &line),
        []
    );
    let object = step("{\"$text\": \"other\"}", StdoutMode::Json);
    let found = compared(
        StdoutMode::Json,
        Compare::default(),
        &stored(StdoutMode::Json),
        &object,
    );
    assert_eq!(named(&found), [(Dimension::Json, Some(1), "/$text")]);
}

#[test]
fn an_extra_entry_of_the_tree_is_one_difference_named_by_its_path() {
    let recorded = step_with(base());
    let mut after = base();
    after.tree.push(directory("ws/feature-one"));
    let observed = step_with(after);

    let found = compared(StdoutMode::Text, Compare::default(), &recorded, &observed);
    assert_eq!(
        named(&found),
        [(Dimension::Tree, Some(1), "ws/feature-one")]
    );
    assert_eq!(
        (found[0].recorded.as_str(), found[0].observed.as_str()),
        ("absent", "d ws/feature-one")
    );
    let found = compared(StdoutMode::Text, Compare::default(), &observed, &recorded);
    assert_eq!(
        named(&found),
        [(Dimension::Tree, Some(1), "ws/feature-one")]
    );
}

#[test]
fn an_entry_of_another_kind_flag_or_target_is_a_difference_of_the_tree() {
    let entry = |kind: EntryKind| {
        let mut after = base();
        after.tree.push(Entry {
            path: "ws/run".to_owned(),
            kind,
        });
        step_with(after)
    };
    let hash = sha1(b"#!/bin/sh\n");
    let plain = entry(EntryKind::File {
        executable: false,
        hash: hash.clone(),
    });
    let executable = entry(EntryKind::File {
        executable: true,
        hash: hash.clone(),
    });
    let link = |target: &str| {
        entry(EntryKind::Link {
            target: target.to_owned(),
        })
    };
    for (recorded, observed) in [
        (&plain, &executable),
        (&plain, &entry(EntryKind::Directory)),
        (&plain, &link("<ROOT>/ws/a")),
        (&link("<ROOT>/ws/a"), &link("<ROOT>/ws/b")),
    ] {
        for rules in [
            Compare::default(),
            file_rules(&[("ws/run", FileMode::Presence)]),
        ] {
            let found = compared(StdoutMode::Text, rules, recorded, observed);
            assert_eq!(named(&found), [(Dimension::Tree, Some(1), "ws/run")]);
        }
    }
    let found = compared(StdoutMode::Text, Compare::default(), &plain, &executable);
    assert_eq!(found[0].recorded, format!("f - {hash} ws/run"));
    assert_eq!(found[0].observed, format!("f x {hash} ws/run"));
}

#[test]
fn one_byte_of_a_file_compared_by_its_bytes_is_one_difference_of_the_files() {
    let recorded = step_with(with_file(base(), STATE, b"[env_index]\none = 1\n"));
    let observed = step_with(with_file(base(), STATE, b"[env_index]\none = 2\n"));

    // The line of the file holds the hash of its content, so the listing differs with it.
    let found = compared(StdoutMode::Text, Compare::default(), &recorded, &observed);
    assert_eq!(
        named(&found),
        [
            (Dimension::Tree, Some(1), STATE),
            (Dimension::Files, Some(1), STATE)
        ]
    );
    assert_eq!(
        (found[1].recorded.as_str(), found[1].observed.as_str()),
        ("[env_index]\none = 1\n", "[env_index]\none = 2\n")
    );

    // Content alone, under lines that are equal, is the one difference of the files.
    let mut altered = recorded.clone();
    altered
        .after
        .files
        .insert(STATE.to_owned(), b"[env_index]\none = 3\n".to_vec());
    let found = compared(StdoutMode::Text, Compare::default(), &recorded, &altered);
    assert_eq!(named(&found), [(Dimension::Files, Some(1), STATE)]);
}

#[test]
fn a_file_that_is_not_utf8_is_shown_byte_for_byte() {
    let recorded = step_with(with_file(base(), STATE, &[0xff, 0x00]));
    let observed = step_with(with_file(base(), STATE, &[0xfe, 0x00]));

    let found = compared(StdoutMode::Text, Compare::default(), &recorded, &observed);
    assert_eq!(found.len(), 2);
    assert_eq!(found[1].dimension, Dimension::Files);
    assert!(found[1].recorded.ends_with("ff00"), "{}", found[1].recorded);
    assert!(found[1].observed.ends_with("fe00"), "{}", found[1].observed);
}

#[test]
fn a_file_only_one_run_changed_is_compared_by_its_hash() {
    // The record wrote the file in this step; the observation had the same content before it.
    let content = b"[env_index]\none = 1\n";
    let recorded = step_with(with_file(base(), STATE, content));
    let mut held = with_file(base(), STATE, content);
    held.files.clear();
    let observed = StepRecord {
        before: held.clone(),
        after: held,
        ..step("", StdoutMode::Text)
    };
    assert_eq!(
        compared(StdoutMode::Text, Compare::default(), &recorded, &observed),
        []
    );

    let mut other = with_file(base(), STATE, b"[env_index]\none = 2\n");
    other.files.clear();
    let observed = StepRecord {
        before: other.clone(),
        after: other,
        ..step("", StdoutMode::Text)
    };
    let found = compared(StdoutMode::Text, Compare::default(), &recorded, &observed);
    assert_eq!(
        named(&found),
        [
            (Dimension::Tree, Some(1), STATE),
            (Dimension::Files, Some(1), STATE)
        ]
    );
    assert!(
        found[1].observed.contains("no step stored"),
        "{}",
        found[1].observed
    );
}

#[test]
fn one_line_of_a_block_is_one_difference_named_by_repository_and_block() {
    let recorded = step_with(base());
    let mut after = base();
    after.git[0].blocks[1].lines[1] =
        "refs/remotes/origin/main 2222222222222222222222222222222222222222  ".to_owned();
    let observed = step_with(after);

    let found = compared(StdoutMode::Text, Compare::default(), &recorded, &observed);
    assert_eq!(
        named(&found),
        [(Dimension::Git, Some(1), "ws/projects/alpha refs")]
    );
    assert!(
        found[0].recorded.contains("origin/main 1111"),
        "{}",
        found[0].recorded
    );
    assert!(
        found[0].observed.contains("origin/main 2222"),
        "{}",
        found[0].observed
    );
}

#[test]
fn a_repository_or_block_that_one_side_lacks_is_a_difference_of_the_repositories() {
    let recorded = step_with(base());
    let with = |change: &dyn Fn(&mut Snapshot)| {
        let mut after = base();
        change(&mut after);
        step_with(after)
    };

    let twice = with(&|after| {
        let section = after.git[0].clone();
        after.git.push(section);
    });
    let found = compared(StdoutMode::Text, Compare::default(), &recorded, &twice);
    assert_eq!(named(&found), [(Dimension::Git, Some(1), REPOSITORY)]);
    assert_eq!(
        (found[0].recorded.as_str(), found[0].observed.as_str()),
        ("one section", "2 sections")
    );

    let gone = with(&|after| after.git.clear());
    let found = compared(StdoutMode::Text, Compare::default(), &recorded, &gone);
    assert_eq!(named(&found), [(Dimension::Git, Some(1), REPOSITORY)]);
    assert_eq!(
        (found[0].recorded.as_str(), found[0].observed.as_str()),
        ("one section", "absent")
    );

    let kind = with(&|after| after.git[0].kind = RepositoryKind::Worktree);
    let found = compared(StdoutMode::Text, Compare::default(), &recorded, &kind);
    assert_eq!(
        named(&found),
        [(Dimension::Git, Some(1), "ws/projects/alpha repository")]
    );

    let fewer = with(&|after| {
        after.git[0].blocks.remove(1);
    });
    for (first, second) in [(&recorded, &fewer), (&fewer, &recorded)] {
        let found = compared(StdoutMode::Text, Compare::default(), first, second);
        assert_eq!(named(&found), [(Dimension::Git, Some(1), REPOSITORY)]);
    }

    let swapped = with(&|after| {
        after.git[0].blocks.swap(0, 1);
        after.git[0].blocks[2].lines.push("core.x=1".to_owned());
    });
    let found = compared(StdoutMode::Text, Compare::default(), &recorded, &swapped);
    assert_eq!(
        named(&found),
        [
            (Dimension::Git, Some(1), REPOSITORY),
            (Dimension::Git, Some(1), "ws/projects/alpha config"),
        ]
    );
}

fn message_text(pointers: &[&str]) -> Compare {
    Compare {
        message_text: pointers
            .iter()
            .map(|pointer| (*pointer).to_owned())
            .collect(),
        ..Compare::default()
    }
}

#[test]
fn a_string_at_a_message_text_pointer_equals_any_string_and_nothing_else() {
    let rules = || message_text(&["/warnings/*", "/error/message"]);
    let recorded = r#"{"warnings": ["alpha is dirty", "beta is behind"], "error": {"message": "failed", "code": "E1"}}"#;
    let found = |observed: &str| compared_output(StdoutMode::Json, rules(), recorded, observed);

    assert_eq!(
        found(r#"{"warnings": ["other", ""], "error": {"message": "broke", "code": "E1"}}"#),
        []
    );
    let number =
        found(r#"{"warnings": ["other", 7], "error": {"message": "broke", "code": "E1"}}"#);
    assert_eq!(named(&number), [(Dimension::Json, Some(1), "/warnings/1")]);
    assert_eq!(
        (number[0].recorded.as_str(), number[0].observed.as_str()),
        ("\"beta is behind\"", "7")
    );
    // A string beside the pointer, and one more or one less under it, are compared as ever.
    let beside = found(r#"{"warnings": ["a", "b"], "error": {"message": "failed", "code": "E2"}}"#);
    assert_eq!(named(&beside), [(Dimension::Json, Some(1), "/error/code")]);
    let fewer = found(r#"{"warnings": ["a"], "error": {"message": "failed", "code": "E1"}}"#);
    assert_eq!(named(&fewer), [(Dimension::Json, Some(1), "/warnings/1")]);
    let deeper =
        found(r#"{"warnings": [["a"], "b"], "error": {"message": "failed", "code": "E1"}}"#);
    assert_eq!(named(&deeper), [(Dimension::Json, Some(1), "/warnings/0")]);

    // Without the rule the text is compared.
    let plain = compared_output(
        StdoutMode::Json,
        Compare::default(),
        r#"{"warnings": ["a"]}"#,
        r#"{"warnings": ["b"]}"#,
    );
    assert_eq!(named(&plain), [(Dimension::Json, Some(1), "/warnings/0")]);
}

fn fan_out(types: &[&str], key: &str) -> Compare {
    Compare {
        fan_out: vec![FanOut {
            types: types.iter().map(|name| (*name).to_owned()).collect(),
            key: key.to_owned(),
        }],
        ..Compare::default()
    }
}

/// A stream of the lines `lines`, each one value.
fn stream(lines: &[&str]) -> String {
    lines.iter().map(|line| format!("{line}\n")).collect()
}

const START: &str = r#"{"type": "start"}"#;
const DONE: &str = r#"{"type": "done"}"#;
const A1: &str = r#"{"type": "progress", "repo": "a", "n": 1}"#;
const A2: &str = r#"{"type": "progress", "repo": "a", "n": 2}"#;
const A3: &str = r#"{"type": "result", "repo": "a", "ok": true}"#;
const B1: &str = r#"{"type": "progress", "repo": "b", "n": 1}"#;
const B2: &str = r#"{"type": "result", "repo": "b", "ok": true}"#;

#[test]
fn the_partitions_of_a_fan_out_run_are_a_set_and_each_is_in_order() {
    let rules = || fan_out(&["progress", "result"], "/repo");
    let recorded = stream(&[START, A1, A2, A3, B1, B2, DONE]);
    let found = |observed: &[&str]| {
        compared_output(StdoutMode::JsonLines, rules(), &recorded, &stream(observed))
    };

    // The partitions interleaved differently, and in another order.
    assert_eq!(found(&[START, A1, B1, A2, B2, A3, DONE]), []);
    assert_eq!(found(&[START, B1, B2, A1, A2, A3, DONE]), []);

    // Two values of one partition in another order.
    let reordered = found(&[START, A2, A1, A3, B1, B2, DONE]);
    assert_eq!(
        named(&reordered),
        [
            (Dimension::Json, Some(1), "value 2: /n"),
            (Dimension::Json, Some(1), "value 3: /n"),
        ]
    );
    let reordered = found(&[START, A1, A3, A2, B1, B2, DONE]);
    assert!(!reordered.is_empty());
    assert!(
        reordered
            .iter()
            .all(|found| found.dimension == Dimension::Json)
    );

    // A value outside the run that moves: across the run, and into it.
    let moved = found(&[DONE, A1, A2, A3, B1, B2, START]);
    assert_eq!(
        named(&moved),
        [
            (Dimension::Json, Some(1), "value 1: /type"),
            (Dimension::Json, Some(1), "value 7: /type"),
        ]
    );
    assert!(!found(&[A1, START, A2, A3, B1, B2, DONE]).is_empty());
    assert!(!found(&[START, A1, A2, A3, DONE, B1, B2]).is_empty());

    // A value of the run that changes, a partition that is gone, and one that is new.
    let changed = found(&[
        START,
        A1,
        A2,
        A3,
        B1,
        r#"{"type": "result", "repo": "b", "ok": false}"#,
        DONE,
    ]);
    assert_eq!(
        named(&changed),
        [(Dimension::Json, Some(1), "value 6: /ok")]
    );
    let gone = found(&[START, A1, A2, A3, DONE]);
    assert_eq!(gone.len(), 1, "{gone:?}");
    assert!(
        gone[0].subject.contains("fan-out partition /repo = \"b\""),
        "{}",
        gone[0].subject
    );
    assert_eq!(
        (gone[0].recorded.as_str(), gone[0].observed.as_str()),
        ("2 values", "absent")
    );
    let renamed = found(&[
        START,
        A1,
        A2,
        A3,
        r#"{"type": "progress", "repo": "c", "n": 1}"#,
        B2,
        DONE,
    ]);
    assert_eq!(
        (
            renamed[0].subject.as_str(),
            renamed[0].recorded.as_str(),
            renamed[0].observed.as_str()
        ),
        (
            "observed value 5: fan-out partition /repo = \"c\"",
            "absent",
            "1 value"
        )
    );
    assert!(renamed.len() > 1, "{renamed:?}");
    let shorter = found(&[START, A1, A3, B1, B2, DONE]);
    assert!(!shorter.is_empty());
    // A partition that ends one value earlier, or one later.
    let ended = found(&[START, A1, A2, B1, B2, DONE]);
    assert_eq!(
        named(&ended),
        [(
            Dimension::Json,
            Some(1),
            "value 4: fan-out partition /repo = \"a\""
        )]
    );
    let longer = found(&[START, A1, A2, A3, B1, B2, B2, DONE]);
    assert_eq!(
        named(&longer),
        [(
            Dimension::Json,
            Some(1),
            "observed value 7: fan-out partition /repo = \"b\""
        )]
    );

    // Without the rule the order of the stream is compared.
    let plain = compared_output(
        StdoutMode::JsonLines,
        Compare::default(),
        &recorded,
        &stream(&[START, A1, B1, A2, B2, A3, DONE]),
    );
    assert!(!plain.is_empty());
}

#[test]
fn a_rule_that_does_not_read_is_malformed_and_nothing_else_is_compared() {
    let both = |first: Compare, second: Compare| Compare {
        message_text: first.message_text,
        fan_out: [first.fan_out, second.fan_out].concat(),
        ..Compare::default()
    };
    for (rules, subject, written) in [
        (message_text(&["warnings/*"]), "message_text", "warnings/*"),
        (message_text(&["/a", "*"]), "message_text", "*"),
        (message_text(&["/a~2"]), "message_text", "/a~2"),
        (fan_out(&[], "/repo"), "types", "[]"),
        (fan_out(&["progress"], "repo"), "key", "repo"),
        (
            fan_out(&["progress", "progress"], "/repo"),
            "types",
            "progress",
        ),
        (
            both(
                fan_out(&["progress"], "/repo"),
                fan_out(&["result", "progress"], "/repo"),
            ),
            "types",
            "progress",
        ),
        (file_rules(&[("/ws/a", FileMode::Ignore)]), "path", "/ws/a"),
        (file_rules(&[("", FileMode::Ignore)]), "path", ""),
        (
            file_rules(&[("../ws/a", FileMode::Ignore)]),
            "path",
            "../ws/a",
        ),
        (
            file_rules(&[("ws/a/../b", FileMode::Ignore)]),
            "path",
            "ws/a/../b",
        ),
        (file_rules(&[("ws//a", FileMode::Toml)]), "path", "ws//a"),
        (
            file_rules(&[("ws/a**", FileMode::Presence)]),
            "path",
            "ws/a**",
        ),
        (
            Compare {
                git_config_ignore: vec!["core.fsmonitor".to_owned(), "fsmonitor".to_owned()],
                ..Compare::default()
            },
            "git_config_ignore",
            "fsmonitor",
        ),
    ] {
        // The two runs differ in their exit, which is not reported beside the rule.
        let recorded = step("", StdoutMode::Text);
        let observed = StepRecord {
            exit: Exit::Code(1),
            ..recorded.clone()
        };
        let found = compared(StdoutMode::Text, rules, &recorded, &observed);
        assert_eq!(
            named(&found),
            [(Dimension::Malformed, None, subject)],
            "{written}"
        );
        assert_eq!(found[0].recorded, written);
    }
}

#[test]
fn a_rule_that_relaxes_nothing_is_a_difference() {
    let output = r#"{"warnings": [], "count": 3, "error": {"message": "failed"}}"#;
    let found = |rules: Compare| compared_output(StdoutMode::Json, rules, output, output);

    assert_eq!(found(message_text(&["/error/message"])), []);
    for pointer in ["/warnings/*", "/count", "/error", "/nothing", ""] {
        let subject = format!("message_text {pointer}");
        assert_eq!(
            named(&found(message_text(&[pointer]))),
            [(Dimension::Json, None, subject.as_str())]
        );
    }
    assert_eq!(
        named(&found(fan_out(&["progress", "result"], "/repo"))),
        [(Dimension::Json, None, "fan_out progress, result")]
    );

    assert_eq!(
        found(file_rules(&[("ws/.winter/*.toml", FileMode::Toml)])),
        []
    );
    assert_eq!(
        named(&found(file_rules(&[(
            ".winter/config.toml",
            FileMode::Toml
        )]))),
        [(Dimension::Files, None, "file .winter/config.toml")]
    );
    // The first rule that matches decides, so a rule behind it that matches nothing else decides nothing.
    assert_eq!(
        named(&found(file_rules(&[
            ("ws/**", FileMode::Presence),
            (CONFIG, FileMode::Toml)
        ]))),
        [(Dimension::Files, None, "file ws/.winter/config.toml")]
    );

    // A rule of the JSON output in a scenario whose steps print text relaxes nothing.
    let text = compared_output(
        StdoutMode::Text,
        message_text(&["/error/message"]),
        output,
        output,
    );
    assert_eq!(
        named(&text),
        [(Dimension::Json, None, "message_text /error/message")]
    );
}

#[test]
fn a_file_compared_as_toml_is_equal_when_its_values_are() {
    let rules = || file_rules(&[(STATE, FileMode::Toml)]);
    let recorded = step_with(with_file(base(), STATE, b"[env_index]\none = 1\ntwo = 2\n"));
    let found = |rules: Compare, content: &[u8]| {
        let observed = step_with(with_file(base(), STATE, content));
        compared(StdoutMode::Text, rules, &recorded, &observed)
    };

    let reordered = b"# written by another writer\nenv_index = { two = 2, one = 1 }\n";
    assert_eq!(found(rules(), reordered), []);
    assert_eq!(
        named(&found(Compare::default(), reordered)),
        [
            (Dimension::Tree, Some(1), STATE),
            (Dimension::Files, Some(1), STATE)
        ]
    );

    let changed = found(rules(), b"[env_index]\none = 1\ntwo = 3\n");
    assert_eq!(named(&changed), [(Dimension::Files, Some(1), STATE)]);
    let typed = found(rules(), b"[env_index]\none = 1\ntwo = 2.0\n");
    assert_eq!(named(&typed), [(Dimension::Files, Some(1), STATE)]);
    let broken = found(rules(), b"[env_index\n");
    assert_eq!(named(&broken), [(Dimension::Files, Some(1), STATE)]);
}

#[test]
fn a_toml_file_only_one_run_changed_is_parsed_from_the_step_that_stored_it() {
    let rules = || file_rules(&[(STATE, FileMode::Toml)]);
    let scenario = scenario(&[StdoutMode::Text, StdoutMode::Text], rules());
    let first = b"[env_index]\none = 1\n";
    let reordered = b"env_index = { one = 1 }\n";
    let wrote = |content: &[u8]| step_with(with_file(base(), STATE, content));
    // A step that leaves the state as the step before it left it.
    let kept = |last: &StepRecord| {
        let mut state = last.after.clone();
        state.files.clear();
        StepRecord {
            before: state.clone(),
            after: state,
            ..step("", StdoutMode::Text)
        }
    };
    let rewrote = |last: &StepRecord, content: &[u8]| StepRecord {
        before: kept(last).after,
        after: with_file(kept(last).after, STATE, content),
        ..step("", StdoutMode::Text)
    };

    // The record rewrites the file in its second step, with the same values; the observation does not.
    let recorded = [wrote(first), rewrote(&wrote(first), reordered)];
    let observed = [wrote(first), kept(&wrote(first))];
    assert_eq!(differences(&scenario, &recorded, &observed), []);

    let recorded = [
        wrote(first),
        rewrote(&wrote(first), b"env_index = { one = 2 }\n"),
    ];
    let found = differences(&scenario, &recorded, &observed);
    assert_eq!(named(&found), [(Dimension::Files, Some(2), STATE)]);

    // Content that no step stored cannot be parsed, and is a difference.
    let fixture_made = |content: &[u8]| {
        let mut state = with_file(base(), STATE, content);
        state.files.clear();
        StepRecord {
            before: state.clone(),
            after: state,
            ..step("", StdoutMode::Text)
        }
    };
    let found = compared(
        StdoutMode::Text,
        rules(),
        &wrote(reordered),
        &fixture_made(first),
    );
    assert_eq!(named(&found), [(Dimension::Files, Some(1), STATE)]);
    assert!(
        found[0].observed.contains("no step stored"),
        "{}",
        found[0].observed
    );
    assert_eq!(
        compared(
            StdoutMode::Text,
            rules(),
            &fixture_made(first),
            &fixture_made(first)
        ),
        []
    );
}

#[test]
fn presence_accepts_any_content_and_fails_on_absence_and_ignore_accepts_absence() {
    let recorded = step_with(with_file(base(), STATE, b"[env_index]\none = 1\n"));
    let other = step_with(with_file(base(), STATE, &[0xff, 0x01]));
    let absent = step_with(base());

    let presence = || file_rules(&[(STATE, FileMode::Presence)]);
    assert_eq!(
        compared(StdoutMode::Text, presence(), &recorded, &other),
        []
    );
    for (first, second) in [(&recorded, &absent), (&absent, &recorded)] {
        let found = compared(StdoutMode::Text, presence(), first, second);
        assert_eq!(
            named(&found),
            [
                (Dimension::Tree, Some(1), STATE),
                (Dimension::Files, Some(1), STATE)
            ]
        );
    }
    let found = compared(StdoutMode::Text, presence(), &recorded, &absent);
    assert_eq!(found[1].observed, "absent");

    let ignore = || file_rules(&[("ws/.winter/state.*", FileMode::Ignore)]);
    assert_eq!(compared(StdoutMode::Text, ignore(), &recorded, &other), []);
    assert_eq!(compared(StdoutMode::Text, ignore(), &recorded, &absent), []);
    assert_eq!(compared(StdoutMode::Text, ignore(), &absent, &recorded), []);

    // An entry beside the ignored path is compared as ever.
    let mut after = recorded.after.clone();
    after.tree.push(directory("ws/feature-one"));
    let found = compared(StdoutMode::Text, ignore(), &recorded, &step_with(after));
    assert_eq!(
        named(&found),
        [(Dimension::Tree, Some(1), "ws/feature-one")]
    );
}

#[test]
fn stderr_and_text_stdout_are_never_a_difference() {
    let recorded = StepRecord {
        stderr: b"warning: alpha is dirty\n".to_vec(),
        ..step("Indexed feature-one at 4\n", StdoutMode::Text)
    };
    let observed = StepRecord {
        stderr: b"something else\n".to_vec(),
        ..step("{\"index\": 4}\n", StdoutMode::Text)
    };
    assert_eq!(
        compared(StdoutMode::Text, Compare::default(), &recorded, &observed),
        []
    );

    // The error output of a step that prints JSON is no difference either.
    let recorded = step("{\"index\": 4}\n", StdoutMode::Json);
    let observed = StepRecord {
        stderr: b"note\n".to_vec(),
        ..recorded.clone()
    };
    assert_eq!(
        compared(StdoutMode::Json, Compare::default(), &recorded, &observed),
        []
    );

    // A `stdout.json` beside a step that prints text is not read.
    let observed = StepRecord {
        stdout_json: Some("{\"index\":5}\n".to_owned()),
        ..step("{\"index\": 5}\n", StdoutMode::Text)
    };
    let recorded = step("{\"index\": 4}\n", StdoutMode::Text);
    assert_eq!(
        compared(StdoutMode::Text, Compare::default(), &recorded, &observed),
        []
    );
}

#[test]
fn another_number_of_steps_than_the_scenario_describes_is_one_difference() {
    let scenario = scenario(&[StdoutMode::Text, StdoutMode::Text], Compare::default());
    let one = [step("", StdoutMode::Text)];
    let two = [step("", StdoutMode::Text), step("", StdoutMode::Text)];

    assert_eq!(differences(&scenario, &two, &two), []);
    for (recorded, observed) in [
        (&one[..], &two[..]),
        (&two[..], &one[..]),
        (&one[..], &one[..]),
    ] {
        let found = differences(&scenario, recorded, observed);
        assert_eq!(named(&found), [(Dimension::Malformed, None, "step")]);
    }
}

/// The files of the expectation of one step.
fn expectation(step: u32, files: &[(&str, &[u8])]) -> BTreeMap<u32, BTreeMap<String, Vec<u8>>> {
    let files = files
        .iter()
        .map(|(file, content)| ((*file).to_owned(), content.to_vec()))
        .collect();
    BTreeMap::from([(step, files)])
}

#[test]
fn an_expected_exit_replaces_the_recorded_one() {
    let record = [step("", StdoutMode::Text)];
    let expected = overlay(&record, &expectation(1, &[("exit-code", b"1\n")])).expect("it reads");
    let scenario = scenario(&[StdoutMode::Text], Compare::default());
    let as_expected = [StepRecord {
        exit: Exit::Code(1),
        ..record[0].clone()
    }];

    assert_eq!(differences(&scenario, &expected, &as_expected), []);
    let found = differences(&scenario, &expected, &record);
    assert_eq!(named(&found), [(Dimension::Exit, Some(1), "exit-code")]);
    assert_eq!(
        (found[0].recorded.as_str(), found[0].observed.as_str()),
        ("1", "0")
    );

    // Nothing else of the record changes, and a record without an expectation is as it was.
    assert_eq!(expected, as_expected);
    assert_eq!(
        overlay(&record, &BTreeMap::new()).expect("it reads"),
        record
    );
}

#[test]
fn expected_output_and_repositories_replace_the_recorded_ones() {
    let record = [step("{\"index\": 40}", StdoutMode::Json)];
    let git = "repository\n    ws/projects/alpha\n    kind: main\nhead\n    unborn\n";
    let expected = overlay(
        &record,
        &expectation(
            1,
            &[
                ("stdout", b"expected text"),
                ("stderr", b"expected error"),
                ("stdout.json", b"{\"index\":41}\n"),
                ("after/git.txt", git.as_bytes()),
            ],
        ),
    )
    .expect("it reads");

    assert_eq!(expected[0].stdout, b"expected text");
    assert_eq!(expected[0].stderr, b"expected error");
    assert_eq!(expected[0].stdout_json.as_deref(), Some("{\"index\":41}\n"));
    assert_eq!(
        expected[0].after.git[0].blocks,
        [block("head", &["unborn"])]
    );
    assert_eq!(expected[0].after.tree, record[0].after.tree);

    let scenario = scenario(&[StdoutMode::Json], Compare::default());
    let found = differences(&scenario, &expected, &record);
    assert_eq!(
        named(&found),
        [
            (Dimension::Json, Some(1), "/index"),
            (Dimension::Git, Some(1), REPOSITORY),
            (Dimension::Git, Some(1), "ws/projects/alpha head"),
        ]
    );
}

#[test]
fn expected_content_and_its_listing_replace_the_recorded_state() {
    let (recorded_content, expected_content) = (b"# second: web\n", b"# third: docs\n");
    let record = [step_with(with_file(base(), CONFIG, recorded_content))];
    let listing = with_file(base(), CONFIG, expected_content).tree_text();
    let files = expectation(
        1,
        &[
            ("after/tree.txt", listing.as_bytes()),
            ("after/files/ws/.winter/config.toml", expected_content),
        ],
    );
    let expected = overlay(&record, &files).expect("it reads");
    let scenario = scenario(&[StdoutMode::Text], Compare::default());

    let as_expected = [step_with(with_file(base(), CONFIG, expected_content))];
    assert_eq!(expected, as_expected);
    assert_eq!(differences(&scenario, &expected, &as_expected), []);
    let found = differences(&scenario, &expected, &record);
    assert_eq!(
        named(&found),
        [
            (Dimension::Tree, Some(1), CONFIG),
            (Dimension::Files, Some(1), CONFIG)
        ]
    );

    // A listing alone under which the step did not change the file: the content of the record is left out.
    let unchanged = expectation(1, &[("after/tree.txt", base().tree_text().as_bytes())]);
    let expected = overlay(&record, &unchanged).expect("it reads");
    assert_eq!(expected, [step_with(base())]);

    // Content the record stored, and the expectation does not replace, is taken from the record.
    let both = with_file(
        with_file(base(), CONFIG, recorded_content),
        STATE,
        b"one = 1\n",
    );
    let record = [step_with(both.clone())];
    let listing = with_file(both, STATE, b"one = 2\n").tree_text();
    let files = expectation(
        1,
        &[
            ("after/tree.txt", listing.as_bytes()),
            ("after/files/ws/.winter/state.toml", b"one = 2\n"),
        ],
    );
    let expected = overlay(&record, &files).expect("it reads");
    assert_eq!(expected[0].after.files[CONFIG], recorded_content);
    assert_eq!(expected[0].after.files[STATE], b"one = 2\n");
}

#[test]
fn a_stored_path_of_an_expectation_is_read_in_its_stored_form() {
    let record = [step_with(base())];
    let listing = with_file(base(), "ws/.gitignore", b"target\n").tree_text();
    let files = expectation(
        1,
        &[
            ("after/tree.txt", listing.as_bytes()),
            ("after/files/ws/%2Egitignore", b"target\n"),
        ],
    );
    let expected = overlay(&record, &files).expect("it reads");
    assert_eq!(expected[0].after.files["ws/.gitignore"], b"target\n");

    let files = expectation(
        1,
        &[
            ("after/tree.txt", listing.as_bytes()),
            ("after/files/ws/.gitignore", b"target\n"),
        ],
    );
    let error = overlay(&record, &files).expect_err("no stored form");
    assert!(
        matches!(&error, ExpectationError::Unreadable { step: 1, file, .. }
            if file == "after/files/ws/.gitignore"),
        "{error}"
    );
}

#[test]
fn an_expectation_that_does_not_read_as_a_stored_snapshot_is_refused() {
    let content = b"# second: web\n";
    let record = [step_with(with_file(base(), CONFIG, content))];
    let changed = with_file(base(), CONFIG, b"# third: docs\n").tree_text();
    let added = with_file(with_file(base(), CONFIG, content), STATE, b"one = 1\n").tree_text();
    let refused = |files: &[(&str, &[u8])]| match overlay(&record, &expectation(1, files)) {
        Err(ExpectationError::Snapshot { step: 1, error }) => error,
        other => panic!("{other:?}"),
    };

    // Content without its listing, and a listing without its content, do not hash to their lines.
    let error = refused(&[("after/files/ws/.winter/config.toml", b"# third: docs\n")]);
    assert!(
        matches!(&error, SnapshotError::Altered { path, .. } if path == CONFIG),
        "{error}"
    );
    assert!(
        error.to_string().contains("expected/steps/01/after/files"),
        "{error}"
    );
    let error = refused(&[("after/tree.txt", changed.as_bytes())]);
    assert!(
        matches!(&error, SnapshotError::Altered { path, .. } if path == CONFIG),
        "{error}"
    );
    assert!(
        error.to_string().contains("record/steps/01/after/files"),
        "{error}"
    );

    // A listing that implies content nothing holds, and content that no line implies.
    let error = refused(&[("after/tree.txt", added.as_bytes())]);
    assert!(
        matches!(&error, SnapshotError::Absent { path, .. } if path == STATE),
        "{error}"
    );
    let error = refused(&[("after/files/ws/.winter/state.toml", b"one = 1\n")]);
    assert!(
        matches!(&error, SnapshotError::Unimplied { path, .. } if path == STATE),
        "{error}"
    );
    let error = refused(&[
        ("after/tree.txt", base().tree_text().as_bytes()),
        ("after/files/ws/.winter/config.toml", b"# empty\n"),
    ]);
    assert!(
        matches!(&error, SnapshotError::Unimplied { path, .. } if path == CONFIG),
        "{error}"
    );

    // A listing and sections that are none.
    let error = refused(&[("after/tree.txt", b"x ws\n")]);
    assert!(
        matches!(error, SnapshotError::Stored { line: 1, .. }),
        "{error}"
    );
    let error = refused(&[("after/git.txt", b"head\n    unborn\n")]);
    assert!(matches!(error, SnapshotError::Stored { .. }), "{error}");
}

#[test]
fn an_expectation_of_no_step_or_of_a_file_it_does_not_replace_is_refused() {
    let record = [step("", StdoutMode::Text)];
    for step in [0, 2] {
        let error = overlay(&record, &expectation(step, &[("exit-code", b"1\n")]))
            .expect_err("no such step");
        assert!(matches!(error, ExpectationError::NoStep { step: named } if named == step));
    }
    for file in [
        "before/tree.txt",
        "before/git.txt",
        "before/files/ws/a",
        "calls/001.json",
        "after",
        "after/files",
        "exit",
        "meta.toml",
    ] {
        let error = overlay(&record, &expectation(1, &[(file, b"")])).expect_err("not replaced");
        assert!(
            matches!(&error, ExpectationError::NotReplaced { step: 1, file: named } if named == file),
            "{error}"
        );
    }
    for (file, content) in [
        ("exit-code", &b"one\n"[..]),
        ("exit-code", b""),
        ("exit-code", b"1\n2\n"),
        ("exit-code", &[0xff]),
        ("stdout.json", &[0xff]),
        ("after/tree.txt", &[0xff]),
        ("after/git.txt", &[0xff]),
        ("after/files/", b""),
    ] {
        let error = overlay(&record, &expectation(1, &[(file, content)])).expect_err("unreadable");
        assert!(
            matches!(&error, ExpectationError::Unreadable { step: 1, file: named, .. } if named == file),
            "{error}"
        );
    }
    let signal =
        overlay(&record, &expectation(1, &[("exit-code", b"signal:9")])).expect("it reads");
    assert_eq!(signal[0].exit, Exit::Signal(9));
}

#[test]
fn a_record_made_with_something_else_is_stale() {
    let hex = |digit: &str| digit.repeat(40);
    let meta = Meta {
        reference_commit: hex("a"),
        git_version: "2.47.1".to_owned(),
        reference_runtime: "winter 1".to_owned(),
        environment: hex("b"),
        description: hex("c"),
    };
    let reference = |commit: String| Reference { commit };

    assert_eq!(
        stale_record(&meta, &reference(hex("a")), &hex("b"), &hex("c")),
        []
    );
    for (commit, environment, description, subject) in [
        ("d", "b", "c", "reference_commit"),
        ("a", "d", "c", "environment"),
        ("a", "b", "d", "description"),
    ] {
        let found = stale_record(
            &meta,
            &reference(hex(commit)),
            &hex(environment),
            &hex(description),
        );
        assert_eq!(named(&found), [(Dimension::StaleRecord, None, subject)]);
        assert_ne!(found[0].recorded, found[0].observed);
        assert_eq!(found[0].observed, hex("d"));
    }
    let found = stale_record(&meta, &reference(hex("d")), &hex("d"), &hex("d"));
    assert_eq!(found.len(), 3);
}

#[test]
fn a_fixture_that_is_not_the_recorded_one_is_one_difference_of_the_fixture() {
    assert_eq!(fixture(&base(), &base()), None);

    let found = |built: Snapshot| fixture(&base(), &built).expect("a difference");
    let mut hashed = base();
    hashed.files.clear();
    hashed.tree[2] = file(CONFIG, b"# other\n");
    let difference = found(hashed);
    assert_eq!(
        (
            difference.dimension,
            difference.step,
            difference.subject.as_str()
        ),
        (
            Dimension::Fixture,
            Some(1),
            "tree.txt ws/.winter/config.toml"
        )
    );

    let mut repository = base();
    repository.git[0].blocks[0].lines[0] = "unborn".to_owned();
    let difference = found(repository);
    assert_eq!(
        (difference.dimension, difference.subject.as_str()),
        (Dimension::Fixture, "git.txt ws/projects/alpha head")
    );

    let mut stored = base();
    stored
        .files
        .insert(CONFIG.to_owned(), b"# empty\n".to_vec());
    let difference = found(stored);
    assert_eq!(
        (difference.dimension, difference.subject.as_str()),
        (Dimension::Fixture, "files")
    );

    // Only the first of several is answered: the listing before the repositories.
    let mut both = base();
    both.tree.push(directory("ws/feature-one"));
    both.git.clear();
    assert_eq!(found(both).subject, "tree.txt ws/feature-one");
}

#[test]
fn the_runs_of_two_fan_outs_that_follow_each_other_are_two_runs() {
    let rules = || Compare {
        fan_out: [
            fan_out(&["progress"], "/repo").fan_out,
            fan_out(&["result"], "/repo").fan_out,
        ]
        .concat(),
        ..Compare::default()
    };
    let recorded = stream(&[A1, B1, A3, B2]);
    let found = |observed: &[&str]| {
        compared_output(StdoutMode::JsonLines, rules(), &recorded, &stream(observed))
    };

    assert_eq!(found(&[B1, A1, B2, A3]), []);
    // A value of the second fan-out that stands among those of the first ends their run.
    assert!(!found(&[A1, A3, B1, B2]).is_empty());
}
