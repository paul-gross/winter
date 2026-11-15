//! A stdout the reader closed: every write of a result ends the invocation as a broken pipe, silently.

use std::collections::BTreeMap;
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use winter_workspace_core::Termination;
use winter_workspace_test_support::{TempTree, WorkspaceBuilder};

#[path = "support/in_process.rs"]
mod in_process;

use in_process::InProcess;

const SHARED: &str = ".winter/config.toml";

const TWO_REPOSITORIES: &str = "\
[[project_repository]]
url = \"https://fixture.invalid/api.git\"

[[project_repository]]
url = \"https://fixture.invalid/web.git\"
";

/// The text the removal leaves: the blank line before the removed block stays.
const WITHOUT_WEB: &str = "\
[[project_repository]]
url = \"https://fixture.invalid/api.git\"

";

/// A stdout whose reader is gone: every write fails as a write to a closed pipe does.
struct Closed;

impl Write for Closed {
    fn write(&mut self, _: &[u8]) -> io::Result<usize> {
        Err(io::Error::from(io::ErrorKind::BrokenPipe))
    }

    fn flush(&mut self) -> io::Result<()> {
        Err(io::Error::from(io::ErrorKind::BrokenPipe))
    }
}

/// Asserts that `args` in `cwd` end as a broken pipe: exit code 1, no diagnostic, nothing on stderr.
fn assert_broken_pipe(cwd: &Path, args: &[&str]) {
    let ended = InProcess::new(args).cwd(cwd).run_with_stdout(&mut Closed);

    assert!(
        matches!(ended.termination, Termination::BrokenPipe),
        "{args:?}: {:?}",
        ended.termination
    );
    assert_eq!(ended.termination.exit_code(), 1, "{args:?}");
    assert_eq!(ended.termination.diagnostic(), None, "{args:?}");
    assert_eq!(ended.stderr, "", "{args:?}");
}

/// What an entry below a directory is, with the content of a file and the target of a link.
#[derive(Debug, PartialEq, Eq)]
enum Found {
    Directory,
    File(Vec<u8>),
    Link(PathBuf),
}

/// Every entry below `directory`, by its path from there.
fn snapshot(directory: &Path) -> BTreeMap<PathBuf, Found> {
    fn walk(root: &Path, directory: &Path, found: &mut BTreeMap<PathBuf, Found>) {
        for entry in std::fs::read_dir(directory).expect("a readable directory") {
            let path = entry.expect("a directory entry").path();
            let relative = path
                .strip_prefix(root)
                .expect("a path below the root")
                .to_path_buf();
            let kind = std::fs::symlink_metadata(&path)
                .expect("an entry")
                .file_type();
            if kind.is_symlink() {
                let target = std::fs::read_link(&path).expect("a link");
                found.insert(relative, Found::Link(target));
            } else if kind.is_dir() {
                found.insert(relative, Found::Directory);
                walk(root, &path, found);
            } else {
                let content = std::fs::read(&path).expect("a readable file");
                found.insert(relative, Found::File(content));
            }
        }
    }

    let mut found = BTreeMap::new();
    walk(directory, directory, &mut found);
    found
}

fn tree() -> TempTree {
    TempTree::new().expect("an isolated tree")
}

fn workspace(tree: &TempTree) -> PathBuf {
    WorkspaceBuilder::new(tree)
        .config(TWO_REPOSITORIES)
        .state("[env_index]\nother = 15\n")
        .build()
        .expect("a workspace")
        .root
}

#[test]
fn help_of_the_root_a_group_and_a_command_ends_as_a_broken_pipe() {
    let tree = tree();

    for args in [
        &["--help"][..],
        &["ws", "--help"],
        &["ws", "index", "--help"],
    ] {
        assert_broken_pipe(tree.root(), args);
    }
}

#[test]
fn the_version_line_ends_as_a_broken_pipe() {
    let tree = tree();

    assert_broken_pipe(tree.root(), &["--version"]);
}

#[test]
fn ws_index_ends_as_a_broken_pipe_and_changes_nothing() {
    for args in [
        &["ws", "index", "feature-x"][..],
        &["ws", "index", "feature-x", "--json"],
    ] {
        let tree = tree();
        let root = workspace(&tree);
        let before = snapshot(&root);

        assert_broken_pipe(&root, args);

        assert_eq!(snapshot(&root), before, "{args:?}");
    }
}

#[test]
fn repo_remove_ends_as_a_broken_pipe_after_the_file_is_edited() {
    for args in [
        &["repo", "remove", "project/web"][..],
        &["repo", "remove", "project/web", "--json"],
    ] {
        let tree = tree();
        let root = workspace(&tree);

        assert_broken_pipe(&root, args);

        assert_eq!(
            std::fs::read_to_string(root.join(SHARED)).expect("the shared file"),
            WITHOUT_WEB,
            "{args:?}"
        );
    }
}

#[test]
fn a_bare_root_and_a_bare_group_write_nothing_to_stdout_and_stay_bare_groups() {
    let tree = tree();

    for args in [&[][..], &["ws"]] {
        let ended = InProcess::new(args)
            .cwd(tree.root())
            .run_with_stdout(&mut Closed);

        assert!(
            matches!(ended.termination, Termination::BareGroup { .. }),
            "{args:?}: {:?}",
            ended.termination
        );
        assert_eq!(ended.termination.exit_code(), 2, "{args:?}");
        assert_eq!(ended.stderr, "", "{args:?}");
    }
}
