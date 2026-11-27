//! `ws prune` through the built binary: the confirmation is answered from the process's stdin.

use std::path::Path;

use winter_workspace_test_support::{
    CommitSpec, Exit, Observation, TempTree, WorkspaceBuilder, invoke,
};

/// A workspace that declares no repository and holds a clean clone at `projects/old`.
fn fixture() -> TempTree {
    let tree = TempTree::new().expect("an isolated tree");
    let seed = CommitSpec {
        message: "seed".into(),
        files: vec![("README".into(), b"fixture\n".to_vec())],
        ..CommitSpec::default()
    };
    WorkspaceBuilder::new(&tree)
        .config("")
        .remote("old", vec![seed])
        .project("old")
        .build()
        .expect("a workspace");
    tree
}

fn prune(tree: &TempTree, stdin: &[u8]) -> Observation {
    let mut invocation = tree.invocation(Path::new(env!("CARGO_BIN_EXE_winter-workspace")));
    invocation.args = ["ws", "prune"].map(Into::into).into();
    invocation.stdin = stdin.to_vec();
    invoke(invocation).expect("an observation")
}

fn text(bytes: &[u8]) -> &str {
    std::str::from_utf8(bytes).expect("UTF-8")
}

#[test]
fn a_yes_on_a_piped_stdin_removes_a_safe_orphan() {
    let tree = fixture();
    let orphan = tree.workspace_dir().join("projects/old");

    let observed = prune(&tree, b"y\n");

    assert_eq!(observed.exit, Exit::Code(0), "{observed:?}");
    assert_eq!(text(&observed.stderr), "", "{observed:?}");
    assert_eq!(
        text(&observed.stdout),
        format!(
            "  {:<18} {}   [projects/]\nRemove 1 orphan(s)? [y/N]:   remove projects/old\n",
            "project_clone",
            orphan.display()
        )
    );
    assert!(!orphan.exists());
}

#[test]
fn an_empty_stdin_exits_1_with_the_aborted_notice_and_removes_nothing() {
    let tree = fixture();
    let orphan = tree.workspace_dir().join("projects/old");

    let observed = prune(&tree, b"");

    assert_eq!(observed.exit, Exit::Code(1), "{observed:?}");
    assert_eq!(text(&observed.stderr), "aborted\n", "{observed:?}");
    assert!(
        text(&observed.stdout).ends_with("Remove 1 orphan(s)? [y/N]: "),
        "{observed:?}"
    );
    assert!(orphan.join("README").is_file());
}
