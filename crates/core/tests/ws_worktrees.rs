//! `ws worktrees`: the locations of a fixture workspace, listed without a change to its tree.

use std::path::Path;

use serde_json::{Value, json};
use winter_workspace_core::Termination;
use winter_workspace_test_support::{TempTree, WorkspaceBuilder, snapshot};

#[path = "support/in_process.rs"]
mod in_process;

use in_process::{Ended, InProcess};

const CONFIG: &str = "\
[[project_repository]]
name = \"api\"

[[standalone_repository]]
name = \"ctx\"
path = \".winter/ext/context\"
";

fn worktrees(cwd: &Path, args: &[&str]) -> Ended {
    InProcess::new(&[&["ws", "worktrees"], args].concat())
        .cwd(cwd)
        .run()
}

fn text(path: &Path) -> &str {
    path.to_str().expect("a fixture path of plain characters")
}

#[test]
fn the_document_lists_worktrees_then_standalones_then_the_workspace_without_counts() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = WorkspaceBuilder::new(&tree)
        .config(CONFIG)
        .file("alpha/api/README.md", b"api\n")
        .file(".winter/ext/context/README.md", b"ctx\n")
        .build()
        .expect("a workspace")
        .root;
    let before = snapshot(&root);
    let at = |relative: &str| Value::from(text(&root.join(relative)));
    let base = root.file_name().and_then(|n| n.to_str()).expect("a name");

    let document = worktrees(&root, &["--json"]).document("worktrees");

    assert_eq!(
        document,
        json!([
            {"kind": "worktree", "env": "alpha", "repo": "api", "name": null,
             "label": "alpha/api", "path": at("alpha/api")},
            {"kind": "standalone", "env": null, "repo": null, "name": "ctx",
             "label": "ctx", "path": at(".winter/ext/context")},
            {"kind": "workspace", "env": null, "repo": null, "name": base,
             "label": "<workspace>", "path": text(&root)},
        ])
    );
    assert_eq!(snapshot(&root), before);
}

#[test]
fn an_alias_whose_project_entry_is_a_symbolic_link_to_a_directory_is_listed() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = WorkspaceBuilder::new(&tree)
        .config(CONFIG)
        .file("elsewhere/README.md", b"x\n")
        .build()
        .expect("a workspace")
        .root;
    std::fs::create_dir(root.join("alpha")).expect("a directory");
    std::os::unix::fs::symlink(root.join("elsewhere"), root.join("alpha/api")).expect("a link");

    let document = worktrees(&root, &["--json"]).document("worktrees");

    assert_eq!(document[0]["label"], "alpha/api");
}

#[test]
fn the_human_rows_carry_the_labels_in_the_order_of_the_document() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = WorkspaceBuilder::new(&tree)
        .config(CONFIG)
        .file("alpha/api/README.md", b"api\n")
        .build()
        .expect("a workspace")
        .root;

    let ended = worktrees(&root, &[]);

    let labels: Vec<&str> = ended
        .stdout
        .lines()
        .map(|line| line.split_whitespace().next().expect("a label"))
        .collect();
    assert_eq!(labels, ["alpha/api", "<workspace>"]);
}

#[test]
fn the_status_option_is_a_usage_error() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = WorkspaceBuilder::new(&tree)
        .config(CONFIG)
        .build()
        .expect("a workspace")
        .root;

    let ended = worktrees(&root, &["--status"]);

    assert!(matches!(ended.termination, Termination::Usage(_)));
    assert!(ended.stdout.is_empty());
}
