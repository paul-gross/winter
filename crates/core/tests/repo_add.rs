//! `repo add`: one repository block added to a configuration file of a fixture workspace.

use std::path::{Path, PathBuf};

use serde_json::{Value, json};
use winter_workspace_core::{ConfigError, Termination};
use winter_workspace_test_support::{TempTree, WorkspaceBuilder, snapshot};

#[path = "support/in_process.rs"]
mod in_process;

use in_process::InProcess;

const SHARED: &str = ".winter/config.toml";
const LOCAL: &str = ".winter/config.local.toml";
const ONE: &str =
    "# repositories\n[[project_repository]]\nurl = \"https://fixture.invalid/api.git\"\n";

fn workspace(tree: &TempTree, shared: &str, local: Option<&str>) -> PathBuf {
    let builder = WorkspaceBuilder::new(tree).config(shared);
    match local {
        Some(local) => builder.local_config(local),
        None => builder,
    }
    .build()
    .expect("a workspace")
    .root
}

fn add(cwd: &Path, args: &[&str]) -> Value {
    InProcess::new(&[&["repo", "add"], args].concat())
        .cwd(cwd)
        .run()
        .document(&format!("{args:?}"))
}

fn parsed(path: &Path) -> toml::Table {
    toml::from_str(&std::fs::read_to_string(path).expect("a file")).expect("valid toml")
}

#[test]
fn a_non_array_repository_value_is_refused_by_the_load_with_both_files_unchanged() {
    for (shared, local) in [
        ("project_repository = \"abc\"\n", ""),
        ("", "[standalone_repository]\nurl = \"x\"\n"),
    ] {
        let tree = TempTree::new().expect("an isolated tree");
        let root = workspace(&tree, shared, Some(local));
        let before = snapshot(&root);

        let termination = InProcess::new(&["repo", "add", "https://fixture.invalid/new.git"])
            .cwd(&root)
            .run()
            .wrote_nothing(1, shared);

        assert!(
            matches!(&termination, Termination::Config(error)
                if matches!(&**error, ConfigError::RepositoriesNotArray { .. })),
            "{termination:?}"
        );
        assert_eq!(snapshot(&root), before);
    }
}

#[test]
fn a_project_is_appended_to_the_shared_file_and_the_next_load_accepts_it() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = workspace(&tree, ONE, None);

    let document = add(&root, &["https://fixture.invalid/web.git", "--json"]);

    assert_eq!(
        document,
        json!({"added": true, "kind": "project", "url": "https://fixture.invalid/web.git"})
    );
    let text = std::fs::read_to_string(root.join(SHARED)).expect("file");
    assert!(text.starts_with("# repositories\n"), "{text}");
    assert_eq!(
        parsed(&root.join(SHARED))["project_repository"]
            .as_array()
            .map(Vec::len),
        Some(2)
    );
    let listed = InProcess::new(&["repo", "list", "--json"]).cwd(&root).run();
    assert_eq!(listed.termination.exit_code(), 0, "{}", listed.stderr);
    assert!(!root.join(".winter/config.lock").exists());
    assert!(!root.join(".winter/state.toml").exists());
}

#[test]
fn local_creates_the_local_file_and_leaves_the_shared_file_as_it_was() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = workspace(&tree, ONE, None);
    let shared = std::fs::read(root.join(SHARED)).expect("shared");
    assert!(!root.join(LOCAL).exists());

    let document = add(
        &root,
        &[
            "git@fixture.invalid:tools/x.git",
            "--standalone",
            "--path",
            "tools/x",
            "--git-exclude",
            "a",
            "--git-exclude",
            "b",
            "--local",
            "--json",
        ],
    );

    assert_eq!(document["kind"], "standalone");
    assert_eq!(std::fs::read(root.join(SHARED)).expect("shared"), shared);
    let block = &parsed(&root.join(LOCAL))["standalone_repository"][0];
    assert_eq!(block["path"].as_str(), Some("tools/x"));
    assert_eq!(block["git_excludes"].as_array().map(Vec::len), Some(2));
    let listed = InProcess::new(&["repo", "list", "--json"]).cwd(&root).run();
    assert_eq!(listed.termination.exit_code(), 0, "{}", listed.stderr);
    assert!(!root.join(".winter/config.lock").exists());
    assert!(!root.join(".winter/state.toml").exists());
}
