//! `repo list`: the repositories of a fixture workspace, listed without a change to its tree.

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use serde_json::{Value, json};
use winter_workspace_core::{ConfigError, InternalError, Termination};
use winter_workspace_test_support::{TempTree, WorkspaceBuilder, snapshot};

#[path = "support/in_process.rs"]
mod in_process;

use in_process::{Ended, InProcess};

/// Two standalone repositories, one of them at a path of its own.
const STANDALONES: &str = "\
[[standalone_repository]]
name = \"context\"
url = \"https://fixture.invalid/ctx.git\"
path = \".winter/ext/context\"

[[standalone_repository]]
name = \"tools\"
";

/// Two project repositories of the shared file, the second pinned and named by its URL.
const PROJECTS: &str = "\
[[project_repository]]
name = \"web\"

[[project_repository]]
url = \"https://fixture.invalid/api.git\"
pinned = true
";

/// One project repository of the local file.
const LOCAL_PROJECT: &str = "\
[[project_repository]]
name = \"mine\"
url = \"https://fixture.invalid/mine.git\"
";

const NOTICE: &str = "warning: Config and projects/ have drifted — missing from projects/: api, web; \
                      undeclared in config: old\n";

/// `repo list` with `args` in `cwd`.
fn listed(cwd: &Path, args: &[&str]) -> Ended {
    InProcess::new(&[&["repo", "list"], args].concat())
        .cwd(cwd)
        .run()
}

/// A workspace that holds every type of repository, and a directory for each project it declares.
fn full_workspace(tree: &TempTree) -> PathBuf {
    WorkspaceBuilder::new(tree)
        .config(&[STANDALONES, PROJECTS].concat())
        .local_config(LOCAL_PROJECT)
        .file("product/README.md", b"product\n")
        .file("context/harness/.git", b"gitdir: elsewhere\n")
        .file("projects/web/README.md", b"web\n")
        .file("projects/api/README.md", b"api\n")
        .file("projects/mine/README.md", b"mine\n")
        .file("projects/.cache/kept", b"")
        .file("projects/notes.txt", b"no directory\n")
        .build()
        .expect("a workspace")
        .root
}

/// A workspace that declares the projects `web` and `api` and holds a directory for neither, beside one no
/// project declares and one whose name starts with a dot.
fn drifted_workspace(tree: &TempTree) -> PathBuf {
    WorkspaceBuilder::new(tree)
        .config(PROJECTS)
        .file("projects/old/README.md", b"old\n")
        .file("projects/.hidden/kept", b"")
        .build()
        .expect("a workspace")
        .root
}

/// The text of `path`, which a fixture root always has.
fn text(path: &Path) -> &str {
    path.to_str().expect("a fixture path of plain characters")
}

#[test]
fn the_command_is_listed_in_the_help_of_its_group() {
    let tree = TempTree::new().expect("an isolated tree");

    let group_help = InProcess::new(&["repo", "--help"]).cwd(tree.root()).run();

    assert!(group_help.stdout.contains("list"), "{}", group_help.stdout);
}

#[test]
fn the_document_lists_the_implicit_repositories_then_the_standalones_then_the_projects() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = full_workspace(&tree);
    let before = snapshot(&root);
    let at = |relative: &str| Value::from(text(&root.join(relative)));

    let document = listed(&root, &["--json"]).document("a full workspace");

    assert_eq!(
        document,
        json!([
            {"name": "product", "type": "singleton", "path": at("product")},
            {"name": "harness", "type": "singleton", "path": at("context/harness")},
            {"name": "context", "type": "standalone", "path": at(".winter/ext/context")},
            {"name": "tools", "type": "standalone", "path": at("tools")},
            {"name": "web", "type": "project", "main_path": at("projects/web"), "pinned": false},
            {"name": "api", "type": "project", "main_path": at("projects/api"), "pinned": true},
            {"name": "mine", "type": "project", "main_path": at("projects/mine"), "pinned": false},
        ])
    );
    assert_eq!(snapshot(&root), before);
}

#[test]
fn the_same_list_is_made_from_a_directory_below_the_root() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = full_workspace(&tree);
    let before = snapshot(&root);

    let from_root = listed(&root, &["--json"]).document("from the root");
    let from_below = listed(&root.join("projects/web"), &["--json"]).document("from below");

    assert_eq!(from_below, from_root);
    assert_eq!(snapshot(&root), before);
}

#[test]
fn without_the_json_flag_one_row_per_repository_is_written_and_nothing_to_stderr() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = full_workspace(&tree);
    let before = snapshot(&root);

    let ended = listed(&root, &[]);

    assert!(
        matches!(ended.termination, Termination::Success),
        "{:?}",
        ended.termination
    );
    assert_eq!(ended.stderr, "");
    let rows: Vec<Vec<&str>> = ended
        .stdout
        .lines()
        .map(|line| line.split_whitespace().collect())
        .collect();
    assert_eq!(
        rows,
        [
            vec!["-", "singleton", "product"],
            vec!["-", "singleton", "context/harness"],
            vec![
                "https://fixture.invalid/ctx.git",
                "standalone",
                ".winter/ext/context"
            ],
            vec!["-", "standalone", "tools"],
            vec!["-", "project", "projects/web"],
            vec![
                "https://fixture.invalid/api.git",
                "project",
                "projects/api",
                "[pinned]"
            ],
            vec![
                "https://fixture.invalid/mine.git",
                "project",
                "projects/mine"
            ],
        ],
        "{}",
        ended.stdout
    );
    assert!(ended.stdout.ends_with('\n'), "{:?}", ended.stdout);
    assert!(
        ended.stdout.lines().all(|line| line == line.trim_end()),
        "{:?}",
        ended.stdout
    );
    assert_eq!(snapshot(&root), before);
}

#[test]
fn an_empty_inventory_is_an_empty_array_and_no_human_output_at_all() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = WorkspaceBuilder::new(&tree)
        .config("")
        .build()
        .expect("a workspace")
        .root;
    let before = snapshot(&root);

    let document = listed(&root, &["--json"]).document("an empty inventory");
    let human = listed(&root, &[]).wrote_nothing(0, "an empty inventory");

    assert_eq!(document, json!([]));
    assert!(matches!(human, Termination::Success), "{human:?}");
    assert_eq!(snapshot(&root), before);
}

#[test]
fn the_notice_of_a_drift_is_on_stderr_and_the_document_alone_on_stdout() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = drifted_workspace(&tree);
    let before = snapshot(&root);
    let at = |relative: &str| Value::from(text(&root.join(relative)));

    let ended = listed(&root, &["--json"]);

    assert!(
        matches!(ended.termination, Termination::Success),
        "{:?}",
        ended.termination
    );
    assert_eq!(ended.stderr, NOTICE);
    assert!(!ended.stdout.contains("drifted"), "{:?}", ended.stdout);
    let document: Value = serde_json::from_str(&ended.stdout).expect("one JSON document");
    assert_eq!(
        document,
        json!([
            {"name": "web", "type": "project", "main_path": at("projects/web"), "pinned": false},
            {"name": "api", "type": "project", "main_path": at("projects/api"), "pinned": true},
        ])
    );
    assert_eq!(snapshot(&root), before);
}

#[test]
fn the_notice_of_a_drift_is_on_stderr_and_the_rows_alone_on_stdout() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = drifted_workspace(&tree);
    let before = snapshot(&root);

    let ended = listed(&root, &[]);

    assert!(
        matches!(ended.termination, Termination::Success),
        "{:?}",
        ended.termination
    );
    assert_eq!(ended.stderr, NOTICE);
    assert_eq!(ended.stdout.lines().count(), 2, "{:?}", ended.stdout);
    assert!(!ended.stdout.contains("drifted"), "{:?}", ended.stdout);
    assert_eq!(snapshot(&root), before);
}

#[test]
fn the_notice_is_written_as_it_is_whatever_the_log_level_asks_for() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = drifted_workspace(&tree);

    let quiet = InProcess::new(&["repo", "list", "--json"])
        .cwd(&root)
        .env("WINTER_LOG_LEVEL", "CRITICAL")
        .run();
    let verbose = InProcess::new(&["-v", "repo", "list", "--json"])
        .cwd(&root)
        .run();

    assert_eq!(quiet.stderr, NOTICE);
    assert!(
        verbose.stderr.lines().any(|line| line == NOTICE.trim_end()),
        "{:?}",
        verbose.stderr
    );
}

#[test]
fn outside_a_workspace_the_list_ends_as_the_missing_root_with_nothing_written() {
    let tree = TempTree::new().expect("an isolated tree");
    let outside = tree.root().join("ws");
    let before = snapshot(&outside);

    for args in [&["--json"][..], &[]] {
        let termination = listed(&outside, args).wrote_nothing(1, &format!("{args:?}"));

        assert!(
            matches!(
                &termination,
                Termination::Config(error)
                    if matches!(&**error, ConfigError::RootNotFound { start } if *start == outside)
            ),
            "{args:?}: {termination:?}"
        );
    }
    assert_eq!(snapshot(&outside), before);
}

#[test]
fn a_configuration_the_load_refuses_ends_the_list_with_nothing_written() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = WorkspaceBuilder::new(&tree)
        .config(&["envs_per_workspace = 1\n\n", PROJECTS].concat())
        .file("projects/old/README.md", b"old\n")
        .build()
        .expect("a workspace")
        .root;
    let before = snapshot(&root);

    for args in [&["--json"][..], &[]] {
        let termination = listed(&root, args).wrote_nothing(1, &format!("{args:?}"));

        assert!(
            matches!(termination, Termination::Config(_)),
            "{args:?}: {termination:?}"
        );
    }
    assert_eq!(snapshot(&root), before);
}

#[test]
fn a_projects_directory_that_cannot_be_listed_ends_the_list_as_an_internal_failure() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = drifted_workspace(&tree);
    let projects = root.join("projects");
    let mode = |mode| std::fs::set_permissions(&projects, std::fs::Permissions::from_mode(mode));
    mode(0o000).expect("a mode");
    if std::fs::read_dir(&projects).is_ok() {
        // This user may read any directory, and then no mode keeps one from being listed.
        mode(0o755).expect("the mode restored");
        return;
    }

    let ended = listed(&root, &["--json"]);
    mode(0o755).expect("the mode restored");

    let termination = ended.wrote_nothing(1, "an unlistable projects directory");
    assert!(
        matches!(
            &termination,
            Termination::Internal(error) if matches!(**error, InternalError::Unexpected { .. })
        ),
        "{termination:?}"
    );
}
