//! Overlapping executable reconciliation, with real Git and locked workspace persistence.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::thread;

use winter_workspace_core::{OsFiles, read_lock};
use winter_workspace_test_support::{
    CommitSpec, Exit, Git, Invocation, Observation, TempTree, WorkspaceBuilder, invoke,
};

fn invocation(tree: &TempTree, participant: &str) -> Invocation {
    let mut invocation = tree.invocation(Path::new(env!("CARGO_BIN_EXE_winter-workspace")));
    invocation.args = ["ws", "init"].map(Into::into).into();
    invocation.env.set("WINTER_TEST_PARTICIPANT", participant);
    invocation
}

fn success(observed: &Observation) {
    assert_eq!(observed.exit, Exit::Code(0), "{observed:?}");
    assert!(observed.stderr.is_empty(), "{observed:?}");
}

fn unique_state(tree: &TempTree) {
    let exclude = fs::read_to_string(tree.workspace_dir().join(".git/info/exclude"))
        .expect("workspace exclude");
    for name in [
        "winter-dir/projects",
        "winter-workspace/artifacts",
        "winter-workspace/workspace-skills",
        "winter-config",
        "tools",
    ] {
        for marker in [
            format!("# >>> {name} (managed by winter)"),
            format!("# <<< {name}"),
        ] {
            assert_eq!(
                exclude.lines().filter(|line| *line == marker).count(),
                1,
                "{marker}: {exclude}"
            );
        }
    }
    for entry in ["/projects/", "/tools/", ".claude/skills/concurrent-*"] {
        assert_eq!(
            exclude.lines().filter(|line| *line == entry).count(),
            1,
            "{entry}: {exclude}"
        );
    }
    let root = tree.workspace_dir();
    let lock = fs::read_to_string(root.join(".winter/config.lock")).expect("pinned lock");
    // The reader merges duplicate names, so check raw records as well as their interpreted values.
    assert_eq!(lock.matches("[[standalone]]").count(), 1, "{lock}");
    let entries = read_lock(&OsFiles, &root).expect("valid lock");
    assert_eq!(entries.len(), 1, "{entries:?}");
    assert_eq!(entries["tools"].r#ref, "v1");
    let git = Git::new(tree).expect("git");
    for (remote, location) in [("api", "projects/api"), ("tools", "tools")] {
        let expected = git
            .run(
                &tree.root().join(format!("remotes/{remote}.git")),
                &["rev-parse", "HEAD"],
            )
            .expect("seed HEAD");
        let actual = git
            .run(&root.join(location), &["rev-parse", "HEAD"])
            .expect("clone HEAD");
        assert_eq!(actual, expected, "{location}");
        if remote == "tools" {
            assert_eq!(entries["tools"].commit.as_str(), actual.trim());
        }
    }
}

#[test]
fn overlapping_initializations_preserve_unique_state_and_recover_on_rerun() {
    let tree = TempTree::new().expect("isolated tree");
    let seed = CommitSpec {
        message: "seed".into(),
        files: vec![("README".into(), b"fixture\n".to_vec())],
        tag: Some("v1".into()),
        ..CommitSpec::default()
    };
    WorkspaceBuilder::new(&tree)
        .config(
            r#"
prefix = "concurrent"
[[project_repository]]
name = "api"
url = "https://fixture.invalid/api.git"
[[standalone_repository]]
name = "tools"
url = "https://fixture.invalid/tools.git"
ref = "v1"
"#,
        )
        .remote("api", vec![seed.clone()])
        .remote("tools", vec![seed])
        .root_repository("")
        .build()
        .expect("workspace and seeded remotes");

    // Both binaries must reach each clone *before* either can create its destination. This
    // witnesses overlap inside reconciliation, rather than guessing from launch times. Once
    // both have observed absence, let the first Git finish before the second Git attempts the
    // same clone: the latter reports the real, permitted destination-exists race, not an
    // unrelated index/ref lock conflict. No persistence operation is replaced by the fixture.
    //
    // All waits belong to invoke's process groups and 30-second deadlines; joining both calls
    // before inspecting results also guarantees cleanup if either participant fails.
    let shim = tree.root().join("bin/git");
    fs::write(
        &shim,
        format!(
            r#"#!/bin/sh
PATH=${{PATH#*:}}
export PATH
if [ "$1" = clone ] && [ "$WINTER_TEST_PARTICIPANT" != rerun ]; then
    repo=
    for arg do
        case "$arg" in
            https://fixture.invalid/api.git) repo=api ;;
            https://fixture.invalid/tools.git) repo=tools ;;
        esac
    done
    if [ -n "$repo" ]; then
        calls='{calls}'
        : > "$calls/$repo.$WINTER_TEST_PARTICIPANT.ready"
        while [ ! -f "$calls/$repo.first.ready" ] || [ ! -f "$calls/$repo.second.ready" ]; do
            sleep 0.01
        done
        if [ "$WINTER_TEST_PARTICIPANT" = first ]; then
            git "$@"
            result=$?
            : > "$calls/$repo.finished"
            exit "$result"
        fi
        while [ ! -f "$calls/$repo.finished" ]; do sleep 0.01; done
    fi
fi
exec git "$@"
"#,
            calls = tree.root().join("calls").display(),
        ),
    )
    .expect("clone barrier");
    fs::set_permissions(&shim, fs::Permissions::from_mode(0o755)).expect("executable shim");

    let (first, second) = thread::scope(|scope| {
        let first = scope.spawn(|| invoke(invocation(&tree, "first")));
        let second = scope.spawn(|| invoke(invocation(&tree, "second")));
        (first.join(), second.join())
    });
    let first = first
        .expect("first worker")
        .expect("first bounded invocation");
    let second = second
        .expect("second worker")
        .expect("second bounded invocation");
    success(&first);
    assert_eq!(second.exit, Exit::Code(1), "{second:?}");
    let errors = String::from_utf8(second.stderr.clone()).expect("UTF-8 stderr");
    let mut expected_errors = String::new();
    for (repo, target, location, branch, remaining) in [
        (
            "api",
            "projects/",
            "projects/api",
            "--branch main ",
            "identity, excludes, cmd",
        ),
        (
            "tools",
            "standalone/",
            "tools",
            "",
            "identity, excludes, cmd, pin",
        ),
    ] {
        let root = tree.workspace_dir();
        let destination = root.join(location);
        expected_errors.push_str(&format!(
            "[{repo}] ERROR: clone failed for {repo}\n  $ git clone {branch}-- https://fixture.invalid/{repo}.git {destination}\n  cwd: {root}\n  exit 128\n  stderr: fatal: destination path '{destination}' already exists and is not an empty directory. (failed at location; not attempted: {remaining})\n✗ {target} failed\n",
            root = root.display(),
            destination = destination.display(),
        ));
        for participant in ["first", "second"] {
            assert!(
                tree.root()
                    .join(format!("calls/{repo}.{participant}.ready"))
                    .is_file()
            );
        }
    }
    assert_eq!(errors, expected_errors, "only the permitted clone races");
    unique_state(&tree);

    let third = invoke(invocation(&tree, "rerun")).expect("bounded recovery invocation");
    success(&third);
    assert!(
        !String::from_utf8_lossy(&third.stdout).contains("cloned to"),
        "{third:?}"
    );
    unique_state(&tree);
}
