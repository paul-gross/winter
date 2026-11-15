//! The state snapshot and normalization: what a snapshot lists and leaves out, what it stores of a file,
//! what a section of a repository holds, and the forms it and a JSON stream are stored in.

use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use sha1::{Digest, Sha1};
use winter_workspace_test_support::scenario::fixture::build;
use winter_workspace_test_support::scenario::normalize::{self, InvalidStream};
use winter_workspace_test_support::scenario::snapshot::{
    AFTER, BEFORE, Entry, EntryKind, Repository, RepositoryKind, Snapshot, SnapshotError,
    stored_path, take, tree_path,
};
use winter_workspace_test_support::scenario::{Compare, Scenario, StdoutMode};
use winter_workspace_test_support::{CommitSpec, Git, GitError, TempTree};

/// One remote of one commit, a checkout of it, and a worktree of the checkout: three repositories.
const FIXTURE: &str = r#"
[[fixture.remote]]
name = "alpha"
[[fixture.remote.commit]]
message = "first"
files = { "README.md" = "one\n" }

[[fixture.checkout]]
remote = "alpha"
path = "projects/alpha"

[[fixture.worktree]]
checkout = "projects/alpha"
path = "feature-one/alpha"
branch = "feature-one"
start = "origin/main"
"#;

/// The content of the fixture file that holds the root of its tree, as the scenario directory has it and
/// as a snapshot stores it.
const NOTE: &str = "root: <ROOT>/ws\n";

/// The paths of the three repositories, in the order of their sections.
const BARE: &str = "remotes/alpha.git";
const WORKTREE: &str = "ws/feature-one/alpha";
const MAIN: &str = "ws/projects/alpha";

/// A scenario directory in a tree of its own, outside every tree a fixture is built in.
struct Written {
    host: TempTree,
    scenario: Scenario,
}

/// The scenario directory of [`FIXTURE`], with a workspace file that holds the root, an executable file,
/// and a link whose target holds the root.
fn written() -> Written {
    let host = TempTree::new().expect("a tree for the scenario directory");
    let dir = host.root().join("tmp/scenarios/ws-index/basic");
    let files = dir.join("files");
    std::fs::create_dir_all(files.join(".winter")).expect("the scenario directory is created");
    let description = format!("summary = \"s\"\n{FIXTURE}\n[[step]]\nargs = [\"doctor\"]\n");
    std::fs::write(dir.join("scenario.toml"), description).expect("the description is written");
    std::fs::write(files.join(".winter/config.toml"), "# empty\n").expect("a file is written");
    std::fs::write(files.join("note.txt"), NOTE).expect("a file is written");
    std::fs::write(files.join("run.sh"), "#!/bin/sh\n").expect("a file is written");
    std::fs::set_permissions(files.join("run.sh"), std::fs::Permissions::from_mode(0o755))
        .expect("the file is made executable");
    std::os::unix::fs::symlink("<ROOT>/ws/note.txt", files.join("link")).expect("a link is made");
    let scenario = Scenario::load(&dir).expect("the description parses");
    Written { host, scenario }
}

impl Written {
    /// The fixture, built in a new tree.
    fn build(&self) -> TempTree {
        build(&self.scenario).expect("the fixture is built")
    }

    /// A directory that does not exist yet, outside every tree a fixture is built in.
    fn outside(&self, name: &str) -> PathBuf {
        self.host.root().join("cache").join(name)
    }
}

fn snapshot(tree: &TempTree, previous: Option<&Snapshot>) -> Snapshot {
    take(tree, &Compare::default(), previous).expect("a snapshot is taken")
}

fn sha1(content: &[u8]) -> String {
    Sha1::digest(content)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn write(tree: &TempTree, path: &str, content: &[u8]) {
    let file = tree.root().join(path);
    std::fs::create_dir_all(file.parent().expect("a file has a parent"))
        .expect("the directories are created");
    std::fs::write(file, content).expect("the file is written");
}

fn root_bytes(tree: &TempTree) -> &[u8] {
    tree.root().as_os_str().as_bytes()
}

fn root_text(tree: &TempTree) -> &str {
    tree.root().to_str().expect("the root of a tree is UTF-8")
}

fn entry<'s>(snapshot: &'s Snapshot, path: &str) -> &'s Entry {
    snapshot
        .tree
        .iter()
        .find(|entry| entry.path == path)
        .unwrap_or_else(|| panic!("no entry {path} in\n{}", snapshot.tree_text()))
}

fn hash<'s>(snapshot: &'s Snapshot, path: &str) -> &'s str {
    match &entry(snapshot, path).kind {
        EntryKind::File { hash, .. } => hash,
        other => panic!("{path} is {other:?}"),
    }
}

fn repository<'s>(snapshot: &'s Snapshot, path: &str) -> &'s Repository {
    snapshot
        .git
        .iter()
        .find(|repository| repository.path == path)
        .unwrap_or_else(|| panic!("no section {path} in\n{}", snapshot.git_text()))
}

fn labels(repository: &Repository) -> Vec<&str> {
    repository
        .blocks
        .iter()
        .map(|block| block.label.as_str())
        .collect()
}

fn block<'s>(repository: &'s Repository, label: &str) -> &'s [String] {
    &repository
        .blocks
        .iter()
        .find(|block| block.label == label)
        .unwrap_or_else(|| panic!("no block {label} in {}", repository.path))
        .lines
}

/// The labels of the blocks of the section `path` that differ between `before` and `after`.
fn changed_blocks<'s>(before: &Snapshot, after: &'s Snapshot, path: &str) -> Vec<&'s str> {
    let (before, after) = (repository(before, path), repository(after, path));
    assert_eq!(labels(before), labels(after));
    after
        .blocks
        .iter()
        .zip(&before.blocks)
        .filter(|(after, before)| after != before)
        .map(|(after, _)| after.label.as_str())
        .collect()
}

#[test]
fn the_same_workspace_in_two_trees_gives_the_same_snapshot() {
    let written = written();
    let (first, second) = (written.build(), written.build());
    assert_ne!(first.root(), second.root());

    let (before_first, before_second) = (snapshot(&first, None), snapshot(&second, None));
    for tree in [&first, &second] {
        let content = format!("{}/ws\n", root_text(tree));
        write(tree, "ws/where.txt", content.as_bytes());
    }
    let after_first = snapshot(&first, Some(&before_first));
    let after_second = snapshot(&second, Some(&before_second));

    assert_eq!(before_first.tree_text(), before_second.tree_text());
    assert_eq!(before_first.git_text(), before_second.git_text());
    assert_eq!(before_first, before_second);
    assert_eq!(after_first, after_second);

    // Each file holds the root of its own tree, and hashes as the content with the token in its place.
    let note = std::fs::read(first.root().join("ws/note.txt")).expect("the file is read");
    assert_eq!(note, NOTE.replace("<ROOT>", root_text(&first)).as_bytes());
    assert_eq!(hash(&before_first, "ws/note.txt"), sha1(NOTE.as_bytes()));
    assert_eq!(hash(&after_first, "ws/where.txt"), sha1(b"<ROOT>/ws\n"));
    assert_eq!(after_first.files["ws/where.txt"], b"<ROOT>/ws\n");
    let gitconfig = std::fs::read(first.root().join("gitconfig")).expect("the file is read");
    assert_eq!(
        hash(&before_first, "gitconfig"),
        sha1(&normalize::root(&gitconfig, first.root()))
    );
    assert_ne!(hash(&before_first, "gitconfig"), sha1(&gitconfig));

    for text in [
        before_first.tree_text(),
        before_first.git_text(),
        after_first.tree_text(),
        after_first.git_text(),
    ] {
        assert!(!text.contains(root_text(&first)), "{text}");
    }
    assert!(before_first.git_text().contains("<ROOT>/ws/projects/alpha"));
}

#[test]
fn a_file_that_is_not_utf8_is_stored_and_hashed_as_its_bytes() {
    let written = written();
    let tree = written.build();
    let before = snapshot(&tree, None);
    let mut raw = vec![0xff, b' '];
    raw.extend_from_slice(root_bytes(&tree));
    raw.push(b'\n');
    write(&tree, "ws/raw.bin", &raw);

    let after = snapshot(&tree, Some(&before));

    assert_eq!(after.files["ws/raw.bin"], raw);
    assert_eq!(hash(&after, "ws/raw.bin"), sha1(&raw));
}

#[test]
fn the_tree_lists_every_entry_in_its_line_form_sorted_and_leaves_out_what_is_no_state() {
    let written = written();
    let tree = written.build();
    for excluded in ["bin", "calls", "cache", "tmp", "git-template"] {
        write(&tree, &format!("{excluded}/inside/file"), b"x");
    }
    write(&tree, "ws/__pycache__/module.bin", b"x");
    write(&tree, "ws/src/__pycache__/deeper/module.bin", b"x");
    write(&tree, "ws/bin/tool", b"x");

    let text = snapshot(&tree, None).tree_text();
    let lines: Vec<&str> = text.lines().collect();

    let one_byte = sha1(b"x");
    for expected in [
        "d home".to_owned(),
        "d home/.config".to_owned(),
        "d remotes".to_owned(),
        "d remotes/alpha.git".to_owned(),
        "d ws".to_owned(),
        "d ws/.winter".to_owned(),
        format!("f - {} ws/.winter/config.toml", sha1(b"# empty\n")),
        format!("f - {one_byte} ws/bin/tool"),
        "l ws/link -> <ROOT>/ws/note.txt".to_owned(),
        format!("f - {} ws/note.txt", sha1(NOTE.as_bytes())),
        format!("f x {} ws/run.sh", sha1(b"#!/bin/sh\n")),
        "d ws/src".to_owned(),
        "d ws/projects/alpha/.git".to_owned(),
        format!("f - {} ws/projects/alpha/README.md", sha1(b"one\n")),
        format!(
            "f - {} ws/feature-one/alpha/.git",
            sha1(b"gitdir: <ROOT>/ws/projects/alpha/.git/worktrees/alpha\n")
        ),
    ] {
        assert!(lines.contains(&expected.as_str()), "{expected}\n{text}");
    }

    let paths: Vec<&str> = lines
        .iter()
        .map(|line| {
            let rest = &line[2..];
            match line.as_bytes()[0] {
                b'd' => rest,
                b'f' => &rest[43..],
                b'l' => rest.split_once(" -> ").expect("a link has a target").0,
                other => panic!("a line starts with {other}: {line}"),
            }
        })
        .collect();
    let mut sorted = paths.clone();
    sorted.sort_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
    assert_eq!(paths, sorted);
    // A path sorts by its bytes as a whole: `-` comes before `/`.
    write(&tree, "ws/src-two", b"x");
    write(&tree, "ws/src/inner", b"x");
    let again = snapshot(&tree, None);
    let order: Vec<&str> = again
        .tree
        .iter()
        .map(|entry| entry.path.as_str())
        .filter(|path| path.starts_with("ws/src"))
        .collect();
    assert_eq!(order, ["ws/src", "ws/src-two", "ws/src/inner"]);

    for path in paths {
        let top = path.split('/').next().expect("a path has a segment");
        assert!(
            !["bin", "calls", "cache", "tmp", "git-template"].contains(&top),
            "{path}"
        );
        assert!(!path.contains("/.git/"), "{path}");
        assert!(!path.contains("remotes/alpha.git/"), "{path}");
        assert!(!path.contains("__pycache__"), "{path}");
    }
}

#[test]
fn the_files_of_a_snapshot_are_those_that_changed_since_the_one_before() {
    let written = written();
    let tree = written.build();
    let before = snapshot(&tree, None);
    assert!(before.files.is_empty());

    write(&tree, "ws/note.txt", b"changed\n");
    write(&tree, "ws/new/file.txt", b"created\n");
    std::fs::set_permissions(
        tree.root().join("ws/run.sh"),
        std::fs::Permissions::from_mode(0o644),
    )
    .expect("the mode is changed");
    let after = snapshot(&tree, Some(&before));

    let changed: Vec<(&str, &[u8])> = after
        .files
        .iter()
        .map(|(path, content)| (path.as_str(), content.as_slice()))
        .collect();
    assert_eq!(
        changed,
        [
            ("ws/new/file.txt", &b"created\n"[..]),
            ("ws/note.txt", b"changed\n")
        ]
    );
    // The mode is part of the line, and no content.
    assert_ne!(entry(&before, "ws/run.sh"), entry(&after, "ws/run.sh"));

    let unchanged = snapshot(&tree, Some(&after));
    assert!(unchanged.files.is_empty());
    assert_eq!(unchanged.tree, after.tree);
}

#[test]
fn every_repository_has_a_section_with_the_blocks_of_its_kind() {
    let written = written();
    let tree = written.build();

    let taken = snapshot(&tree, None);

    let sections: Vec<(&str, RepositoryKind)> = taken
        .git
        .iter()
        .map(|repository| (repository.path.as_str(), repository.kind))
        .collect();
    assert_eq!(
        sections,
        [
            (BARE, RepositoryKind::Bare),
            (WORKTREE, RepositoryKind::Worktree),
            (MAIN, RepositoryKind::Main),
        ]
    );
    let (bare, worktree, main) = (
        repository(&taken, BARE),
        repository(&taken, WORKTREE),
        repository(&taken, MAIN),
    );
    assert_eq!(
        labels(bare),
        [
            "head",
            "refs",
            "worktrees",
            "config",
            "in-progress",
            "exclude",
            "log"
        ]
    );
    assert_eq!(
        labels(worktree),
        [
            "dot-git",
            "head",
            "refs",
            "status",
            "stash",
            "config",
            "in-progress",
            "exclude",
            "log"
        ]
    );
    assert_eq!(
        labels(main),
        [
            "dot-git",
            "head",
            "refs",
            "worktrees",
            "status",
            "stash",
            "config",
            "in-progress",
            "exclude",
            "log"
        ]
    );

    let git = Git::new(&tree).expect("git");
    let id = git
        .run(&tree.root().join(MAIN), &["rev-parse", "HEAD"])
        .expect("the commit of the checkout");
    let id = id.trim_end();
    assert_eq!(block(main, "dot-git"), ["directory"]);
    assert_eq!(
        block(worktree, "dot-git"),
        ["gitdir: <ROOT>/ws/projects/alpha/.git/worktrees/alpha"]
    );
    assert_eq!(block(main, "head"), [id, "refs/heads/main"]);
    assert_eq!(block(worktree, "head"), [id, "refs/heads/feature-one"]);
    assert_eq!(block(bare, "head"), [id, "refs/heads/main"]);
    assert_eq!(
        block(main, "refs"),
        [
            format!("refs/heads/feature-one {id}  "),
            format!("refs/heads/main {id} refs/remotes/origin/main "),
            format!("refs/remotes/origin/HEAD {id}  refs/remotes/origin/main"),
            format!("refs/remotes/origin/main {id}  "),
        ]
    );
    assert_eq!(
        block(main, "worktrees"),
        [
            "worktree <ROOT>/ws/projects/alpha".to_owned(),
            format!("HEAD {id}"),
            "branch refs/heads/main".to_owned(),
            String::new(),
            "worktree <ROOT>/ws/feature-one/alpha".to_owned(),
            format!("HEAD {id}"),
            "branch refs/heads/feature-one".to_owned(),
            String::new(),
        ]
    );
    assert_eq!(
        block(bare, "worktrees"),
        ["worktree <ROOT>/remotes/alpha.git", "bare", ""]
    );
    assert_eq!(
        block(worktree, "status"),
        [
            format!("# branch.oid {id}"),
            "# branch.head feature-one".to_owned()
        ]
    );
    assert!(block(main, "stash").is_empty());
    assert!(
        block(main, "config")
            .contains(&"remote.origin.url=https://fixture.invalid/alpha.git".to_owned())
    );
    let mut sorted = block(main, "config").to_vec();
    sorted.sort();
    assert_eq!(block(main, "config"), sorted);
    assert!(block(main, "in-progress").is_empty());
    assert!(block(main, "exclude").is_empty());
    let entry = format!(
        "{id}  Fixture Author author@fixture.invalid 946684800 Fixture Committer \
         committer@fixture.invalid 946684800 first"
    );
    assert_eq!(
        block(main, "log"),
        [
            "refs/heads/feature-one",
            entry.as_str(),
            "refs/heads/main",
            entry.as_str()
        ]
    );
    assert_eq!(block(bare, "log"), ["refs/heads/main", entry.as_str()]);
}

#[test]
fn what_git_keeps_beside_its_history_shows_in_the_blocks_that_read_it() {
    let written = written();
    let tree = written.build();
    let git = Git::new(&tree).expect("git");
    let main = tree.root().join(MAIN);
    write(&tree, "ws/projects/alpha/README.md", b"stashed\n");
    git.run(&main, &["stash"]).expect("a stash");
    write(&tree, "ws/projects/alpha/.git/info/exclude", b"*.log\n");
    write(&tree, "ws/projects/alpha/.git/MERGE_HEAD", b"");
    std::fs::create_dir(main.join(".git/rebase-merge")).expect("a directory");
    write(&tree, "ws/projects/alpha/untracked.txt", b"x");

    let taken = snapshot(&tree, None);

    let main = repository(&taken, MAIN);
    let stash = block(main, "stash");
    assert_eq!(stash.len(), 1, "{stash:?}");
    assert!(stash[0].starts_with("stash@{0} "), "{stash:?}");
    assert_eq!(block(main, "exclude"), ["*.log"]);
    assert_eq!(block(main, "in-progress"), ["MERGE_HEAD", "rebase-merge/"]);
    assert!(block(main, "status").contains(&"? untracked.txt".to_owned()));
    assert!(block(repository(&taken, WORKTREE), "in-progress").is_empty());
}

#[test]
fn a_commit_pushed_to_the_remote_changes_its_head_refs_and_log_and_nothing_else() {
    let written = written();
    let tree = written.build();
    let before = snapshot(&tree, None);

    let spec = CommitSpec {
        message: "second".to_owned(),
        files: vec![("b.txt".to_owned(), b"two\n".to_vec())],
        ..CommitSpec::default()
    };
    Git::new(&tree)
        .expect("git")
        .push_to_remote("alpha", &spec)
        .expect("a commit is pushed");
    let after = snapshot(&tree, None);

    assert_eq!(
        changed_blocks(&before, &after, BARE),
        ["head", "refs", "log"]
    );
    assert_eq!(repository(&before, MAIN), repository(&after, MAIN));
    assert_eq!(repository(&before, WORKTREE), repository(&after, WORKTREE));
    assert_eq!(before.tree, after.tree);
}

#[test]
fn a_commit_in_a_work_tree_also_shows_where_git_reports_the_commit_checked_out() {
    let written = written();
    let tree = written.build();
    let before = snapshot(&tree, None);

    let spec = CommitSpec {
        message: "local".to_owned(),
        ..CommitSpec::default()
    };
    Git::new(&tree)
        .expect("git")
        .commit(&tree.root().join(WORKTREE), &spec)
        .expect("a commit is made");
    let after = snapshot(&tree, None);

    // The status block names the commit of the branch, and the worktree list the commit of each work tree.
    assert_eq!(
        changed_blocks(&before, &after, WORKTREE),
        ["head", "refs", "status", "log"]
    );
    assert_eq!(
        changed_blocks(&before, &after, MAIN),
        ["refs", "worktrees", "log"]
    );
    assert_eq!(repository(&before, BARE), repository(&after, BARE));
    assert_eq!(before.tree, after.tree);
}

#[test]
fn a_change_of_the_reflog_alone_changes_nothing() {
    let written = written();
    let tree = written.build();
    let main = tree.root().join(MAIN);
    let reflog = main.join(".git/logs/HEAD");
    let before = snapshot(&tree, None);
    let logged = std::fs::read(&reflog).expect("the checkout has a reflog");
    assert!(!logged.is_empty());

    Git::new(&tree)
        .expect("git")
        .run(&main, &["reflog", "expire", "--expire=now", "--all"])
        .expect("the reflog is expired");
    let after = snapshot(&tree, None);

    assert_ne!(std::fs::read(&reflog).unwrap_or_default(), logged);
    assert_eq!(before, after);
}

#[test]
fn a_snapshot_leaves_the_index_of_a_repository_as_it_was() {
    let written = written();
    let tree = written.build();
    // The same content, written again: the index no longer matches the file, and git would refresh it.
    write(&tree, "ws/projects/alpha/README.md", b"one\n");
    let index = tree.root().join(MAIN).join(".git/index");
    let content = std::fs::read(&index).expect("the index is read");
    let modified = |path: &Path| {
        std::fs::metadata(path)
            .and_then(|metadata| metadata.modified())
            .expect("a modification time")
    };
    let time = modified(&index);

    snapshot(&tree, None);

    assert_eq!(std::fs::read(&index).expect("the index is read"), content);
    assert_eq!(modified(&index), time);
}

#[test]
fn an_ignored_configuration_key_is_absent_from_the_config_block() {
    let written = written();
    let tree = written.build();
    let compare = Compare {
        git_config_ignore: vec!["remote.origin.url".to_owned()],
        ..Compare::default()
    };

    let all = snapshot(&tree, None);
    let without = take(&tree, &compare, None).expect("a snapshot is taken");

    for path in [MAIN, WORKTREE] {
        let key = |line: &&String| line.starts_with("remote.origin.url=");
        let kept = block(repository(&all, path), "config");
        let left = block(repository(&without, path), "config");
        assert_eq!(kept.iter().filter(key).count(), 1, "{kept:?}");
        assert_eq!(left.iter().filter(key).count(), 0, "{left:?}");
        assert_eq!(left.len() + 1, kept.len());
        assert!(
            left.iter()
                .any(|line| line.starts_with("remote.origin.fetch="))
        );
    }
}

#[test]
fn a_repository_whose_git_directory_leaves_the_tree_fails_the_snapshot() {
    let written = written();
    let tree = written.build();
    let other = written.build();
    let outside = other.root().join(MAIN).join(".git");
    write(
        &tree,
        "ws/elsewhere/.git",
        format!("gitdir: {}\n", outside.display()).as_bytes(),
    );

    let error = take(&tree, &Compare::default(), None).expect_err("no snapshot");

    assert!(
        matches!(&error, SnapshotError::Git(error) if error.to_string().contains("does not stay inside")),
        "{error}"
    );
}

#[test]
fn a_repository_whose_work_tree_is_another_directory_fails_the_snapshot() {
    let written = written();
    // A directory of a second tree, and one of the tree itself that is not the directory of the repository.
    for inside in [false, true] {
        let tree = written.build();
        let other = TempTree::new().expect("a second tree, for a directory outside the first");
        let elsewhere = if inside { &tree } else { &other }
            .root()
            .join("ws/elsewhere");
        std::fs::create_dir_all(&elsewhere).expect("the directory is created");
        std::fs::write(elsewhere.join("canary-of-another-place.txt"), "x").expect("a file");
        let main = tree.root().join(MAIN);
        let git = Git::new(&tree).expect("git");
        let named = elsewhere.to_str().expect("UTF-8");
        git.run(&main, &["config", "core.worktree", named])
            .expect("the work tree is named");
        let seen = git
            .run(&main, &["status", "--porcelain", "--untracked-files=all"])
            .expect("git answers");
        assert!(seen.contains("canary-of-another-place.txt"), "{seen}");

        let error = take(&tree, &Compare::default(), None).expect_err("no snapshot");

        assert!(
            matches!(&error, SnapshotError::Git(GitError::Unconfined { path }) if *path == main),
            "{error}"
        );
        assert!(
            error.to_string().contains("does not stay inside"),
            "{error}"
        );
        for told in [error.to_string(), format!("{error:?}")] {
            assert!(!told.contains("canary"), "{told}");
        }
    }
}

#[test]
fn an_exclude_file_that_is_a_link_out_of_the_tree_fails_the_snapshot() {
    let written = written();
    let tree = written.build();
    let outside = written.outside("exclude");
    write(&written.host, "cache/exclude", b"outside\n");
    let info = tree.root().join(MAIN).join(".git/info");
    std::fs::create_dir_all(&info).expect("the directory is created");
    let exclude = info.join("exclude");
    if exclude.symlink_metadata().is_ok() {
        std::fs::remove_file(&exclude).expect("the file is removed");
    }
    std::os::unix::fs::symlink(&outside, &exclude).expect("a link is made");

    let error = take(&tree, &Compare::default(), None).expect_err("no snapshot");

    assert!(
        matches!(&error, SnapshotError::Git(error) if error.to_string().contains("does not stay inside")),
        "{error}"
    );
}

#[test]
fn a_file_any_class_may_execute_is_listed_as_executable() {
    let written = written();
    let tree = written.build();
    for (name, mode) in [
        ("group", 0o650),
        ("other", 0o601),
        ("owner", 0o700),
        ("none", 0o666),
    ] {
        let path = format!("ws/modes/{name}");
        write(&tree, &path, b"x");
        std::fs::set_permissions(
            tree.root().join(&path),
            std::fs::Permissions::from_mode(mode),
        )
        .expect("the mode is set");
    }

    let taken = snapshot(&tree, None);

    for (name, expected) in [
        ("group", true),
        ("other", true),
        ("owner", true),
        ("none", false),
    ] {
        let path = format!("ws/modes/{name}");
        match &entry(&taken, &path).kind {
            EntryKind::File { executable, .. } => assert_eq!(*executable, expected, "{path}"),
            other => panic!("{path} is {other:?}"),
        }
    }
}

#[test]
fn a_link_is_listed_and_never_followed() {
    let written = written();
    let tree = written.build();
    let outside = written.outside("outside");
    std::fs::create_dir_all(outside.join("sub")).expect("a directory outside the tree");
    std::fs::write(outside.join("sub/file"), "x").expect("a file outside the tree");
    std::os::unix::fs::symlink(&outside, tree.root().join("ws/out")).expect("a link is made");
    std::os::unix::fs::symlink("nowhere", tree.root().join("ws/dangling")).expect("a link is made");

    let taken = snapshot(&tree, None);

    assert_eq!(
        entry(&taken, "ws/out").kind,
        EntryKind::Link {
            target: outside.to_str().expect("UTF-8").to_owned()
        }
    );
    assert_eq!(
        entry(&taken, "ws/dangling").kind,
        EntryKind::Link {
            target: "nowhere".to_owned()
        }
    );
    assert!(
        taken
            .tree
            .iter()
            .all(|entry| !entry.path.starts_with("ws/out/"))
    );
}

#[test]
fn a_buffer_that_holds_the_root_twice_holds_two_tokens_afterwards() {
    let tree = TempTree::new().expect("a tree");
    let root = root_text(&tree);
    let buffer = format!("{root}/ws/a\n\"{root}\"{root}-suffix");

    let normalized = normalize::root(buffer.as_bytes(), tree.root());

    assert_eq!(normalized, b"<ROOT>/ws/a\n\"<ROOT>\"<ROOT>-suffix");
    let buffer = format!("at {root} and {root}\n");
    let normalized = normalize::root(buffer.as_bytes(), tree.root());
    assert_eq!(normalized, b"at <ROOT> and <ROOT>\n");
    let name = tree.root().file_name().expect("a root has a name");
    assert!(
        !normalized
            .windows(name.len())
            .any(|window| window == name.as_bytes())
    );
}

#[test]
fn a_json_stream_is_stored_one_value_a_line_with_sorted_keys() {
    let stream = b"{\"b\": 1, \"a\": {\"d\": [1, 2], \"c\": \"x\"}}{\"z\": null,\n \"y\": 1.5}\n";

    let stored = normalize::stdout_json(stream, StdoutMode::Json).expect("a JSON stream");

    assert_eq!(
        stored.as_deref(),
        Some("{\"a\":{\"c\":\"x\",\"d\":[1,2]},\"b\":1}\n{\"y\":1.5,\"z\":null}\n")
    );
    assert_eq!(
        normalize::stdout_json(b"", StdoutMode::Json).expect("an empty stream"),
        Some(String::new())
    );
    assert_eq!(
        normalize::stdout_json(b"{\"a\": 1}\nnot json\n", StdoutMode::Json),
        Err(InvalidStream {
            value: 2,
            reason: "expected ident at line 2 column 2".to_owned()
        })
    );
    assert_eq!(normalize::stdout_json(stream, StdoutMode::Text), Ok(None));
}

#[test]
fn a_line_of_json_lines_that_is_not_json_is_stored_as_text() {
    let stream = b"{\"b\": 1, \"a\": 2}\nnot json\n\n{\"open\": \n[1, 2] trailing\n\xff\n3";

    let stored = normalize::stdout_json(stream, StdoutMode::JsonLines).expect("lines");

    assert_eq!(
        stored.as_deref(),
        Some(
            "{\"a\":2,\"b\":1}\n{\"$text\":\"not json\"}\n{\"$text\":\"\"}\n{\"$text\":\"{\\\"open\\\": \"}\n\
             {\"$text\":\"[1, 2] trailing\"}\n{\"$text\":\"\u{fffd}\"}\n3\n"
        )
    );
}

#[test]
fn the_root_is_replaced_before_a_stream_is_parsed() {
    let tree = TempTree::new().expect("a tree");
    let stream = format!("{{\"path\": \"{}/ws\"}}\n", root_text(&tree));

    let normalized = normalize::root(stream.as_bytes(), tree.root());
    let stored = normalize::stdout_json(&normalized, StdoutMode::Json).expect("a JSON stream");

    assert_eq!(stored.as_deref(), Some("{\"path\":\"<ROOT>/ws\"}\n"));
}

#[test]
fn a_snapshot_written_to_disk_reads_back_equal() {
    let written = written();
    let tree = written.build();
    let before = snapshot(&tree, None);
    let mut raw = vec![0xff];
    raw.extend_from_slice(root_bytes(&tree));
    write(&tree, "ws/raw.bin", &raw);
    write(&tree, "ws/new/deep/file.txt", b"created\n");
    write(&tree, "ws/empty", b"");
    write(
        &tree,
        "ws/projects/alpha/.git/info/exclude",
        b"\n    indented\nhead\n",
    );
    let after = snapshot(&tree, Some(&before));
    let step = written.outside("record/steps/01");

    before
        .write(&step.join(BEFORE))
        .expect("the snapshot is written");
    after
        .write(&step.join(AFTER))
        .expect("the snapshot is written");

    assert_eq!(
        Snapshot::read(&step.join(BEFORE), None).expect("it reads"),
        before
    );
    assert_eq!(
        Snapshot::read(&step.join(AFTER), Some(&before)).expect("it reads"),
        after
    );
    assert_eq!(after.files.len(), 3);
    let stored = |path: &str| std::fs::read(step.join(path)).expect("a stored file");
    assert_eq!(stored("after/tree.txt"), after.tree_text().as_bytes());
    assert_eq!(stored("after/git.txt"), after.git_text().as_bytes());
    assert_eq!(stored("after/files/ws/raw.bin"), raw);
    assert_eq!(stored("after/files/ws/new/deep/file.txt"), b"created\n");
    assert_eq!(stored("after/files/ws/empty"), b"");
    assert!(!step.join("before/files").exists());

    // A directory that holds something is not written into.
    let error = after
        .write(&step.join(BEFORE))
        .expect_err("no second snapshot");
    assert!(matches!(error, SnapshotError::Io { .. }), "{error}");
    assert_eq!(
        Snapshot::read(&step.join(BEFORE), None).expect("it reads"),
        before
    );
}

#[test]
fn a_stored_file_is_written_only_below_the_directory_of_its_snapshot() {
    let written = written();
    let directory = written.outside("escape/after");
    for path in ["../out", "/abs", "a/../b", "", "a//b", "./a"] {
        let snapshot = Snapshot {
            files: [(path.to_owned(), b"x".to_vec())].into(),
            ..Snapshot::default()
        };

        let error = snapshot.write(&directory).expect_err("nothing is written");

        assert!(
            matches!(error, SnapshotError::Unrepresentable { .. }),
            "{path:?}: {error}"
        );
        assert!(!directory.exists(), "{path:?}");
    }
}

/// Every regular file under `directory`, by its path from there, sorted.
fn files_under(directory: &Path) -> Vec<String> {
    fn walk(directory: &Path, relative: &str, found: &mut Vec<String>) {
        for entry in std::fs::read_dir(directory).expect("a directory is read") {
            let entry = entry.expect("an entry is read");
            let name = entry.file_name().into_string().expect("a UTF-8 name");
            let path = if relative.is_empty() {
                name
            } else {
                format!("{relative}/{name}")
            };
            if entry.file_type().expect("a file type").is_dir() {
                walk(&entry.path(), &path, found);
            } else {
                found.push(path);
            }
        }
    }
    let mut found = Vec::new();
    walk(directory, "", &mut found);
    found.sort();
    found
}

/// The fixture after a step that adds a worktree and writes files whose names a repository reads as its
/// own, and the snapshots before and after it.
fn with_names_git_reads(written: &Written) -> (TempTree, Snapshot, Snapshot) {
    let tree = written.build();
    let before = snapshot(&tree, None);
    Git::new(&tree)
        .expect("git")
        .add_worktree(
            &tree.root().join(MAIN),
            &tree.root().join("ws/feature-two/alpha"),
            "feature-two",
            "origin/main",
        )
        .expect("a worktree is added");
    write(&tree, "ws/.gitignore", b"kept.txt\n");
    write(&tree, "ws/kept.txt", b"kept\n");
    write(&tree, "ws/docs/.gitattributes", b"* text\n");
    write(&tree, "ws/50%.txt", b"half\n");
    write(&tree, "ws/%2Egit", b"no entry of git\n");
    write(&tree, "ws/.github/x", b"x\n");
    write(&tree, "ws/a.git/x", b"x\n");
    let after = snapshot(&tree, Some(&before));
    (tree, before, after)
}

#[test]
fn a_name_a_repository_reads_as_its_own_is_stored_in_another_form_and_read_back() {
    let written = written();
    let (_tree, before, after) = with_names_git_reads(&written);
    let directory = written.outside("names/after");

    after.write(&directory).expect("the snapshot is written");

    let expected = [
        ("ws/%2Egit", "ws/%252Egit"),
        ("ws/.github/x", "ws/%2Egithub/x"),
        ("ws/.gitignore", "ws/%2Egitignore"),
        ("ws/50%.txt", "ws/50%25.txt"),
        ("ws/a.git/x", "ws/a.git/x"),
        ("ws/docs/.gitattributes", "ws/docs/%2Egitattributes"),
        ("ws/feature-two/alpha/.git", "ws/feature-two/alpha/%2Egit"),
        (
            "ws/feature-two/alpha/README.md",
            "ws/feature-two/alpha/README.md",
        ),
        ("ws/kept.txt", "ws/kept.txt"),
    ];
    let in_tree: Vec<&str> = after.files.keys().map(String::as_str).collect();
    assert_eq!(in_tree, expected.map(|(path, _)| path));
    let mut stored = expected.map(|(_, stored)| stored.to_owned());
    stored.sort();
    assert_eq!(files_under(&directory.join("files")), stored);
    for (path, stored) in expected {
        assert_eq!(stored_path(path), stored);
        assert_eq!(tree_path(stored).as_deref(), Some(path));
        assert_eq!(
            std::fs::read(directory.join("files").join(stored)).expect("a stored file"),
            after.files[path]
        );
    }

    // The listing holds the paths of the tree, and so does what is read back.
    let listing = std::fs::read_to_string(directory.join("tree.txt")).expect("the listing");
    assert_eq!(listing, after.tree_text());
    for (path, _) in expected {
        assert!(
            listing
                .lines()
                .any(|line| line.ends_with(&format!(" {path}"))),
            "{path}"
        );
    }
    // The one stored name the listing holds is the name a file of the tree has.
    let escaped: Vec<&str> = listing.lines().filter(|line| line.contains('%')).collect();
    assert_eq!(escaped.len(), 2, "{escaped:?}");
    assert!(escaped[0].ends_with(" ws/%2Egit") && escaped[1].ends_with(" ws/50%.txt"));
    assert_eq!(
        Snapshot::read(&directory, Some(&before)).expect("it reads"),
        after
    );
}

#[test]
fn every_file_of_a_written_snapshot_is_tracked_once_its_directory_is_added_to_a_repository() {
    let written = written();
    let (_tree, _before, after) = with_names_git_reads(&written);
    let git = Git::new(&written.host).expect("git");
    let repository = written.host.root().join("ws");
    git.init(&repository, "main").expect("a repository");
    let directory = repository.join("record/steps/01/after");

    after.write(&directory).expect("the snapshot is written");
    git.run(&repository, &["add", "--", "record"])
        .expect("the directory is added");
    let listed = git
        .run(&repository, &["ls-files", "-z"])
        .expect("the tracked files are listed");

    let mut tracked: Vec<&str> = listed.split_terminator('\0').collect();
    tracked.sort_unstable();
    let on_disk: Vec<String> = files_under(&repository.join("record"))
        .into_iter()
        .map(|path| format!("record/{path}"))
        .collect();
    assert_eq!(tracked, on_disk);
    assert_eq!(on_disk.len(), after.files.len() + 2);
    for stored in [
        "files/ws/feature-two/alpha/%2Egit",
        "files/ws/%2Egitignore",
        "files/ws/kept.txt",
    ] {
        let path = format!("record/steps/01/after/{stored}");
        assert!(tracked.contains(&path.as_str()), "{path}\n{tracked:?}");
    }
}

#[test]
fn a_stored_snapshot_that_lacks_a_file_its_listing_implies_does_not_read() {
    let written = written();
    let tree = written.build();
    let before = snapshot(&tree, None);
    write(&tree, "ws/note.txt", b"changed\n");
    write(&tree, "ws/.gitignore", b"created\n");
    std::fs::set_permissions(
        tree.root().join("ws/run.sh"),
        std::fs::Permissions::from_mode(0o644),
    )
    .expect("the mode is changed");
    let after = snapshot(&tree, Some(&before));
    let step = written.outside("lost/steps/01");
    before
        .write(&step.join(BEFORE))
        .expect("the snapshot is written");
    after
        .write(&step.join(AFTER))
        .expect("the snapshot is written");
    // A file that differs in its execute bit alone implies no content, and neither does anything of the
    // snapshot that has none before it, which reads without a directory of stored files.
    assert!(!step.join(BEFORE).join("files").exists());
    assert!(!after.files.contains_key("ws/run.sh"));
    Snapshot::read(&step.join(AFTER), Some(&before)).expect("it reads");
    Snapshot::read(&step.join(BEFORE), None).expect("it reads");

    for (path, stored) in [
        ("ws/note.txt", "ws/note.txt"),
        ("ws/.gitignore", "ws/%2Egitignore"),
    ] {
        let file = step.join(AFTER).join("files").join(stored);
        let content = std::fs::read(&file).expect("the stored file");
        std::fs::remove_file(&file).expect("the stored file is removed");

        let error = Snapshot::read(&step.join(AFTER), Some(&before)).expect_err("no snapshot");

        assert!(
            matches!(&error, SnapshotError::Absent { path: absent, file: looked } if absent == path && *looked == file),
            "{error}"
        );
        assert!(error.to_string().contains(path), "{error}");
        std::fs::write(&file, content).expect("the stored file is put back");
    }
    Snapshot::read(&step.join(AFTER), Some(&before)).expect("it reads");
}

#[test]
fn a_name_under_the_stored_files_that_is_not_in_the_stored_form_does_not_read() {
    let written = written();
    for name in [".gitignore", ".git", "50%.txt", "%2Efoo", "%2egit", "a%2"] {
        let directory = written.outside(&format!("form/{}", stored_path(name)));
        Snapshot::default()
            .write(&directory)
            .expect("the snapshot is written");
        let file = directory.join("files/ws").join(name);
        std::fs::create_dir_all(file.parent().expect("a parent")).expect("a directory");
        std::fs::write(&file, "x").expect("a file");

        let error = Snapshot::read(&directory, None).expect_err("no snapshot");

        assert!(
            matches!(&error, SnapshotError::Stored { file: named, .. } if *named == file),
            "{name}: {error}"
        );
        assert!(error.to_string().contains(name), "{name}: {error}");
    }
}

#[test]
fn a_repository_under_an_excluded_entry_of_the_root_has_no_section_and_nothing_stored() {
    let written = written();
    let tree = written.build();
    let git = Git::new(&tree).expect("git");
    git.init(&tree.root().join("cache/kept/repo"), "main")
        .expect("a repository");
    let before = snapshot(&tree, None);
    write(&tree, "cache/kept/repo/file.txt", b"x");
    write(&tree, "cache/loose.txt", b"x");
    write(&tree, "ws/seen.txt", b"x");

    let after = snapshot(&tree, Some(&before));

    let sections: Vec<&str> = after.git.iter().map(|r| r.path.as_str()).collect();
    assert_eq!(sections, [BARE, WORKTREE, MAIN]);
    let stored: Vec<&str> = after.files.keys().map(String::as_str).collect();
    assert_eq!(stored, ["ws/seen.txt"]);
    assert!(
        after
            .tree
            .iter()
            .all(|entry| entry.path.split('/').next() != Some("cache")),
        "{}",
        after.tree_text()
    );
}

#[test]
fn a_git_entry_that_is_no_repository_inside_one_fails_the_snapshot() {
    let written = written();
    let tree = written.build();
    let inner = tree.root().join(MAIN).join("sub");
    std::fs::create_dir_all(inner.join(".git")).expect("an empty directory");

    let error = take(&tree, &Compare::default(), None).expect_err("no snapshot");

    assert!(
        matches!(&error, SnapshotError::Foreign { path, .. } if *path == inner),
        "{error}"
    );
}

#[test]
fn a_repository_git_cannot_read_fails_the_snapshot() {
    let tree = TempTree::new().expect("a tree");
    let nowhere = tree.root().join("ws/nowhere");
    write(
        &tree,
        "ws/broken/.git",
        format!("gitdir: {}\n", nowhere.display()).as_bytes(),
    );

    let error = take(&tree, &Compare::default(), None).expect_err("no snapshot");

    assert!(
        matches!(&error, SnapshotError::Git(GitError::Failed { .. })),
        "{error}"
    );
}

#[test]
fn what_a_repository_says_that_is_not_utf8_fails_the_snapshot() {
    let written = written();
    for (file, content) in [
        (".git/config", &b"[note]\n\ttext = \xff\n"[..]),
        (".git/info/exclude", b"\xff\n"),
    ] {
        let tree = written.build();
        let main = tree.root().join(MAIN);
        std::fs::create_dir_all(main.join(".git/info")).expect("the directory is created");
        let mut all = std::fs::read(main.join(file)).unwrap_or_default();
        all.extend_from_slice(content);
        std::fs::write(main.join(file), all).expect("the file is written");

        let error = take(&tree, &Compare::default(), None).expect_err("no snapshot");

        // The worktree reads both files of its main repository, and its section comes first.
        let first = tree.root().join(WORKTREE);
        assert!(
            matches!(&error, SnapshotError::Unrepresentable { path, .. } if *path == first),
            "{file}: {error}"
        );
    }
}

#[test]
fn an_entry_that_has_no_line_form_fails_the_snapshot() {
    use std::ffi::OsStr;
    // A link is read at the first arrow of its line: one in the path, and one that the end of the path
    // forms with the arrow written after it, would be taken for the written one.
    let cases: [(&[u8], Option<&str>); 6] = [
        (b"not-utf8-\xff", None),
        (b"line\nbreak", None),
        (b"a -> b", Some("b")),
        (b"a ->", Some("x")),
        (b"a ->", Some("-> x")),
        (b" -> a ->", Some("x")),
    ];
    for (name, target) in cases {
        let tree = TempTree::new().expect("a tree");
        let path = tree.workspace_dir().join(OsStr::from_bytes(name));
        if let Some(target) = target {
            std::os::unix::fs::symlink(target, &path).expect("a link is made");
        } else {
            std::fs::write(&path, "x").expect("a file is written");
        }

        let error = take(&tree, &Compare::default(), None).expect_err("no snapshot");

        assert!(
            matches!(&error, SnapshotError::Unrepresentable { path: named, .. } if *named == path),
            "{name:?}: {error}"
        );
    }

    // A file may hold the arrow in its name: its line has the path last.
    let tree = TempTree::new().expect("a tree");
    write(&tree, "ws/a -> b", b"x");
    let taken = snapshot(&tree, None);
    assert_eq!(hash(&taken, "ws/a -> b"), sha1(b"x"));
}

#[test]
fn a_link_whose_line_holds_more_than_one_arrow_reads_back_as_it_was_taken() {
    let written = written();
    // The line `l ws/a -> -> x` is that of the link `ws/a` to `-> x` alone: the link `ws/a ->` to `x`
    // would be written the same, and is refused.
    let links = [
        ("a", "-> x"),
        ("b", " -> "),
        ("c", "x -> y -> z"),
        ("d -", "> x"),
        ("e -", "-> x"),
        ("f", " ->"),
        ("g->", "x"),
        ("h", "x "),
    ];
    let tree = TempTree::new().expect("a tree");
    for (name, target) in links {
        std::os::unix::fs::symlink(target, tree.workspace_dir().join(name))
            .expect("a link is made");
    }

    let taken = snapshot(&tree, None);

    for (name, target) in links {
        assert_eq!(
            entry_kind(&taken, &format!("ws/{name}")),
            &EntryKind::Link {
                target: target.to_owned()
            }
        );
    }
    assert!(taken.tree_text().contains("\nl ws/a -> -> x\n"));
    let directory = written.outside("arrows/before");
    taken.write(&directory).expect("the snapshot is written");
    assert_eq!(Snapshot::read(&directory, None).expect("it reads"), taken);
}

#[test]
fn a_listing_whose_lines_are_not_in_the_order_of_their_paths_does_not_read() {
    let written = written();
    let (step, before, after) = step_written(&written, "order/steps/01");
    let file = step.join(AFTER).join("tree.txt");
    let text = after.tree_text();
    let lines: Vec<&str> = text.lines().collect();
    let at = lines
        .iter()
        .position(|line| line.ends_with(" ws/note.txt"))
        .expect("the line of the file");
    let rewrite = |lines: &[&str]| {
        std::fs::write(&file, lines.join("\n") + "\n").expect("the listing is rewritten");
        Snapshot::read(&step.join(AFTER), Some(&before)).expect_err("no snapshot")
    };

    // The line of a stored file a second time, two lines exchanged, and a line moved to the end: each
    // names the first line that does not follow the one before it.
    let mut repeated = lines.clone();
    repeated.insert(at, lines[at]);
    let mut exchanged = lines.clone();
    exchanged.swap(at - 1, at);
    let mut moved = lines.clone();
    let line = moved.remove(at);
    moved.push(line);
    for (lines, named) in [
        (repeated, at + 2),
        (exchanged, at + 1),
        (moved, lines.len()),
    ] {
        let error = rewrite(&lines);
        assert!(
            matches!(&error, SnapshotError::Stored { file: read, line, .. }
                if *read == file && *line == named),
            "line {named}: {error}"
        );
    }

    std::fs::write(&file, text).expect("the listing is put back");
    assert_eq!(
        Snapshot::read(&step.join(AFTER), Some(&before)).expect("it reads"),
        after
    );
}

#[test]
fn the_head_block_tells_an_unborn_branch_and_a_detached_head() {
    let written = written();
    let tree = written.build();
    let git = Git::new(&tree).expect("git");
    git.init(&tree.root().join("ws/fresh"), "main")
        .expect("a repository");
    let worktree = tree.root().join(WORKTREE);
    let id = git
        .run(&worktree, &["rev-parse", "HEAD"])
        .expect("the commit");
    git.run(&worktree, &["checkout", "--detach"])
        .expect("the head is detached");

    let taken = snapshot(&tree, None);

    assert_eq!(
        block(repository(&taken, "ws/fresh"), "head"),
        ["unborn", "refs/heads/main"]
    );
    assert_eq!(
        block(repository(&taken, WORKTREE), "head"),
        [id.trim_end(), "detached"]
    );
    assert_eq!(
        block(repository(&taken, MAIN), "head")[1],
        "refs/heads/main"
    );
}

/// A step that changes one file and creates another, with the snapshots before and after it written to
/// the step directory `name` outside the tree.
fn step_written(written: &Written, name: &str) -> (PathBuf, Snapshot, Snapshot) {
    let tree = written.build();
    let before = snapshot(&tree, None);
    write(&tree, "ws/note.txt", b"changed\nsecond line\n");
    write(&tree, "ws/.gitignore", b"created\n");
    let after = snapshot(&tree, Some(&before));
    let step = written.outside(name);
    before
        .write(&step.join(BEFORE))
        .expect("the snapshot is written");
    after
        .write(&step.join(AFTER))
        .expect("the snapshot is written");
    (step, before, after)
}

#[test]
fn a_stored_file_whose_content_no_longer_hashes_to_its_line_does_not_read() {
    let written = written();
    let (step, before, after) = step_written(&written, "altered/steps/01");

    for (path, stored, content) in [
        // What a checkout that rewrites line ends leaves of the file.
        (
            "ws/note.txt",
            "ws/note.txt",
            &b"changed\r\nsecond line\r\n"[..],
        ),
        ("ws/.gitignore", "ws/%2Egitignore", b""),
    ] {
        let file = step.join(AFTER).join("files").join(stored);
        let original = std::fs::read(&file).expect("the stored file");
        std::fs::write(&file, content).expect("the stored file is rewritten");

        let error = Snapshot::read(&step.join(AFTER), Some(&before)).expect_err("no snapshot");

        assert!(
            matches!(
                &error,
                SnapshotError::Altered { path: altered, file: hashed, listed, found }
                    if altered == path
                        && *hashed == file
                        && listed == hash(&after, path)
                        && *found == sha1(content)
            ),
            "{error}"
        );
        assert!(error.to_string().contains(path), "{error}");
        std::fs::write(&file, original).expect("the stored file is put back");
    }
    assert_eq!(
        Snapshot::read(&step.join(AFTER), Some(&before)).expect("it reads"),
        after
    );
}

#[test]
fn a_file_under_the_stored_files_that_nothing_implies_does_not_read() {
    let written = written();
    let (step, before, after) = step_written(&written, "unimplied/steps/01");
    let unimplied =
        |directory: &Path, previous: Option<&Snapshot>, path: &str, stored: &str, why: &str| {
            let error = Snapshot::read(directory, previous).expect_err("no snapshot");
            let file = directory.join("files").join(stored);
            assert!(
                matches!(
                    &error,
                    SnapshotError::Unimplied { path: named, file: found, reason }
                        if named == path && *found == file && *reason == why
                ),
                "{path}: {error}"
            );
            assert!(error.to_string().contains(path), "{error}");
        };
    let no_file = "tree.txt lists no file at the path";
    let unchanged = "the snapshot before lists the file with the same hash";
    let first = "a snapshot that has none before it stores no content";

    // A path without a line of a file: one the listing does not hold, the same under a name that is stored
    // in another form, and one the listing holds as a directory.
    for (path, stored) in [
        ("ws/stray.txt", "ws/stray.txt"),
        ("ws/.gitkeep", "ws/%2Egitkeep"),
        ("ws/projects", "ws/projects"),
    ] {
        let file = step.join(AFTER).join("files").join(stored);
        std::fs::write(&file, "x").expect("a file is added");
        unimplied(&step.join(AFTER), Some(&before), path, stored, no_file);
        std::fs::remove_file(&file).expect("the file is removed");
    }

    // A file whose hash the snapshot before already lists, with the very content that hashes to it.
    assert_eq!(hash(&after, "ws/run.sh"), hash(&before, "ws/run.sh"));
    let file = step.join(AFTER).join("files/ws/run.sh");
    std::fs::write(&file, "#!/bin/sh\n").expect("a file is added");
    unimplied(
        &step.join(AFTER),
        Some(&before),
        "ws/run.sh",
        "ws/run.sh",
        unchanged,
    );
    std::fs::remove_file(&file).expect("the file is removed");
    assert_eq!(
        Snapshot::read(&step.join(AFTER), Some(&before)).expect("it reads"),
        after
    );

    // Any file where there is no snapshot before: the stored files of a later snapshot read as a first
    // one, and one file added to the snapshot that was taken first.
    unimplied(
        &step.join(AFTER),
        None,
        "ws/.gitignore",
        "ws/%2Egitignore",
        first,
    );
    let file = step.join(BEFORE).join("files/ws/note.txt");
    std::fs::create_dir_all(file.parent().expect("a parent")).expect("a directory");
    std::fs::write(&file, NOTE).expect("a file is added");
    assert_eq!(hash(&before, "ws/note.txt"), sha1(NOTE.as_bytes()));
    unimplied(
        &step.join(BEFORE),
        None,
        "ws/note.txt",
        "ws/note.txt",
        first,
    );
    std::fs::remove_file(&file).expect("the file is removed");

    // A path without a line of a file is told as that, with and without a snapshot before.
    let file = step.join(BEFORE).join("files/ws/stray.txt");
    std::fs::write(&file, "x").expect("a file is added");
    unimplied(
        &step.join(BEFORE),
        None,
        "ws/stray.txt",
        "ws/stray.txt",
        no_file,
    );
}

#[test]
fn a_snapshot_that_stores_no_file_reads_without_a_directory_of_stored_files() {
    let written = written();
    let tree = written.build();
    let before = snapshot(&tree, None);
    std::fs::set_permissions(
        tree.root().join("ws/run.sh"),
        std::fs::Permissions::from_mode(0o644),
    )
    .expect("the mode is changed");
    let after = snapshot(&tree, Some(&before));
    let step = written.outside("empty/steps/01");
    before
        .write(&step.join(BEFORE))
        .expect("the snapshot is written");
    after
        .write(&step.join(AFTER))
        .expect("the snapshot is written");

    assert_ne!(after.tree, before.tree);
    for (name, previous, taken) in [(BEFORE, None, &before), (AFTER, Some(&before), &after)] {
        assert!(taken.files.is_empty());
        assert!(!step.join(name).join("files").exists());
        assert_eq!(
            &Snapshot::read(&step.join(name), previous).expect("it reads"),
            taken
        );
        // A directory that holds no file reads the same.
        std::fs::create_dir_all(step.join(name).join("files/ws")).expect("a directory");
        assert_eq!(
            &Snapshot::read(&step.join(name), previous).expect("it reads"),
            taken
        );
    }
}

#[test]
fn a_link_in_the_place_of_an_entry_of_a_stored_snapshot_is_not_followed_and_does_not_read() {
    let written = written();
    let (step, before, after) = step_written(&written, "linked/steps/01");
    let directory = step.join(AFTER);
    let aside = step.join("aside");
    std::fs::create_dir_all(&aside).expect("a directory");

    // Each entry is moved aside and a link to it left in its place: followed, the link would read as the
    // entry did. So for the three entries of the directory, and for a directory and a file under `files`.
    for name in [
        "tree.txt",
        "git.txt",
        "files",
        "files/ws",
        "files/ws/note.txt",
    ] {
        let entry = directory.join(name);
        let moved = aside.join("moved");
        std::fs::rename(&entry, &moved).expect("the entry is moved aside");
        std::os::unix::fs::symlink(&moved, &entry).expect("a link is made");
        assert!(entry.exists(), "the link leads to the entry");

        let error = Snapshot::read(&directory, Some(&before)).expect_err("no snapshot");

        assert!(
            matches!(&error, SnapshotError::Stored { file, line: 0, .. } if *file == entry),
            "{name}: {error}"
        );
        assert!(
            error.to_string().contains(entry.to_str().expect("UTF-8")),
            "{error}"
        );
        std::fs::remove_file(&entry).expect("the link is removed");
        std::fs::rename(&moved, &entry).expect("the entry is put back");
    }
    assert_eq!(
        Snapshot::read(&directory, Some(&before)).expect("it reads"),
        after
    );

    // Nor is an entry of another kind taken for the one a snapshot holds.
    for (name, is_directory) in [("tree.txt", true), ("git.txt", true), ("files", false)] {
        let entry = directory.join(name);
        let moved = aside.join(name);
        std::fs::rename(&entry, &moved).expect("the entry is moved aside");
        if is_directory {
            std::fs::create_dir(&entry).expect("a directory");
        } else {
            std::fs::write(&entry, "").expect("a file");
        }

        let error = Snapshot::read(&directory, Some(&before)).expect_err("no snapshot");

        assert!(
            matches!(&error, SnapshotError::Stored { file, line: 0, .. } if *file == entry),
            "{name}: {error}"
        );
        if is_directory {
            std::fs::remove_dir(&entry).expect("the directory is removed");
        } else {
            std::fs::remove_file(&entry).expect("the file is removed");
        }
        std::fs::rename(&moved, &entry).expect("the entry is put back");
    }
}

#[test]
fn a_git_entry_that_is_a_link_shows_as_a_link_with_its_target() {
    let written = written();
    let tree = with_git_directory_moved(&written, Named::Link);
    let entry = tree.root().join(MAIN).join(".git");

    let taken = snapshot(&tree, None);

    let main = repository(&taken, MAIN);
    assert_eq!(main.kind, RepositoryKind::Main);
    assert_eq!(
        block(main, "dot-git"),
        ["link -> <ROOT>/ws/projects/alpha-git"]
    );
    assert_eq!(
        entry_kind(&taken, "ws/projects/alpha/.git"),
        &EntryKind::Link {
            target: "<ROOT>/ws/projects/alpha-git".to_owned()
        }
    );
    assert!(
        taken
            .git_text()
            .contains("\ndot-git\n    link -> <ROOT>/ws/projects/alpha-git\nhead\n"),
        "{}",
        taken.git_text()
    );
    assert!(!taken.git_text().contains(root_text(&tree)));

    // A relative target holds no root and is kept as it is.
    std::fs::remove_file(&entry).expect("the link is removed");
    std::os::unix::fs::symlink("../alpha-git", &entry).expect("a link is made");
    let relative = snapshot(&tree, None);
    assert_eq!(
        block(repository(&relative, MAIN), "dot-git"),
        ["link -> ../alpha-git"]
    );

    // The form is written and read back like every other block.
    let directory = written.outside("link/before");
    taken.write(&directory).expect("the snapshot is written");
    assert_eq!(Snapshot::read(&directory, None).expect("it reads"), taken);
}

/// How the `.git` entry of the main checkout names its git directory once that is moved beside it.
#[derive(Clone, Copy, Debug)]
enum Named {
    /// A symbolic link to the directory, by its whole path.
    Link,
    /// A file with a `gitdir:` line, relative to the checkout.
    File,
}

/// The directory the git directory of the main checkout is moved to.
const MOVED: &str = "ws/projects/alpha-git";

/// The fixture in a new tree, with the git directory of its main checkout moved to [`MOVED`], named by the
/// `.git` entry as `named` says, still the git directory its worktree names, and holding what an operation leaves behind.
fn with_git_directory_moved(written: &Written, named: Named) -> TempTree {
    let tree = written.build();
    let entry = tree.root().join(MAIN).join(".git");
    let moved = tree.root().join(MOVED);
    std::fs::rename(&entry, &moved).expect("the git directory is moved");
    match named {
        Named::Link => std::os::unix::fs::symlink(&moved, &entry).expect("a link is made"),
        Named::File => {
            std::fs::write(&entry, "gitdir: ../alpha-git\n").expect("a file is written");
            // The worktree named its git directory through the entry that is now a file.
            let line = format!("gitdir: {}/worktrees/alpha\n", moved.display());
            write(&tree, "ws/feature-one/alpha/.git", line.as_bytes());
        }
    }
    let head = std::fs::read(moved.join("refs/heads/main")).expect("the branch is read");
    std::fs::write(moved.join("ORIG_HEAD"), head).expect("a file is written");
    tree
}

#[test]
fn what_a_git_directory_under_another_name_holds_is_neither_listed_nor_stored() {
    let written = written();
    for named in [Named::Link, Named::File] {
        let (first, second) = (
            with_git_directory_moved(&written, named),
            with_git_directory_moved(&written, named),
        );
        for inside in [
            "index",
            "ORIG_HEAD",
            "logs/HEAD",
            "objects",
            "worktrees/alpha/index",
        ] {
            let entry = first.root().join(MOVED).join(inside);
            assert!(entry.exists(), "{named:?}: {}", entry.display());
        }
        let index = |tree: &TempTree| {
            std::fs::read(tree.root().join(MOVED).join("index")).expect("the index is read")
        };
        assert_ne!(
            index(&first),
            index(&second),
            "the index holds what its host has"
        );

        let (before_first, before_second) = (snapshot(&first, None), snapshot(&second, None));
        // Every file under the git directory is written again, so each would be stored were it listed.
        for tree in [&first, &second] {
            write(tree, "ws/projects/alpha/README.md", b"two\n");
            Git::new(tree)
                .expect("git")
                .run(&tree.root().join(MAIN), &["add", "README.md"])
                .expect("the file is staged");
        }
        let after_first = snapshot(&first, Some(&before_first));
        let after_second = snapshot(&second, Some(&before_second));

        for taken in [&before_first, &after_first] {
            assert_eq!(entry_kind(taken, MOVED), &EntryKind::Directory);
            let beneath = format!("{MOVED}/");
            let listed: Vec<&str> = taken
                .tree
                .iter()
                .map(|entry| entry.path.as_str())
                .filter(|path| path.starts_with(&beneath))
                .collect();
            assert_eq!(listed, [""; 0], "{named:?}");
            for name in ["index", "ORIG_HEAD", "logs", "objects", "reflog"] {
                assert!(
                    !taken
                        .tree_text()
                        .lines()
                        .any(|line| line.rsplit('/').next().is_some_and(|last| last == name)),
                    "{named:?}: {name} in\n{}",
                    taken.tree_text()
                );
            }
            assert!(!taken.files.keys().any(|path| path.starts_with(&beneath)));
        }
        assert_eq!(
            after_first.files.keys().collect::<Vec<_>>(),
            ["ws/projects/alpha/README.md"]
        );

        // The checkout and its worktree are read as before, and the same in both trees.
        let main = repository(&before_first, MAIN);
        assert_eq!(main.kind, RepositoryKind::Main);
        assert_eq!(
            repository(&before_first, WORKTREE).kind,
            RepositoryKind::Worktree
        );
        let dot_git = match named {
            Named::Link => "link -> <ROOT>/ws/projects/alpha-git",
            Named::File => "gitdir: ../alpha-git",
        };
        assert_eq!(block(main, "dot-git"), [dot_git]);
        assert_eq!(before_first.tree_text(), before_second.tree_text());
        assert_eq!(before_first.git_text(), before_second.git_text());
        assert_eq!(before_first, before_second);
        assert_eq!(after_first, after_second);
        assert_ne!(before_first.git_text(), after_first.git_text());

        // A change of the reflog alone still changes nothing.
        Git::new(&first)
            .expect("git")
            .run(
                &first.root().join(MAIN),
                &["reflog", "expire", "--expire=now", "--all"],
            )
            .expect("the reflog is expired");
        assert_eq!(snapshot(&first, Some(&before_first)), after_first);

        let step = written.outside(&format!("moved-{named:?}/steps/01"));
        for (name, previous, taken) in [
            (BEFORE, None, &before_first),
            (AFTER, Some(&before_first), &after_first),
        ] {
            taken
                .write(&step.join(name))
                .expect("the snapshot is written");
            assert_eq!(
                &Snapshot::read(&step.join(name), previous).expect("it reads"),
                taken
            );
        }
        assert_eq!(
            files_under(&step.join(AFTER).join("files")),
            ["ws/projects/alpha/README.md"]
        );
    }
}

#[test]
fn a_repository_inside_a_git_directory_fails_the_snapshot() {
    let written = written();
    let tree = with_git_directory_moved(&written, Named::File);
    let inner = tree.root().join(MOVED).join("inner");
    Git::new(&tree)
        .expect("git")
        .run(
            tree.root(),
            &["init", "--quiet", inner.to_str().expect("UTF-8")],
        )
        .expect("a repository is made");

    let error = take(&tree, &Compare::default(), None).expect_err("no snapshot");

    assert!(
        matches!(&error, SnapshotError::Unrepresentable { path, .. } if *path == inner),
        "{error}"
    );
}

fn entry_kind<'s>(snapshot: &'s Snapshot, path: &str) -> &'s EntryKind {
    &entry(snapshot, path).kind
}

#[test]
fn a_git_file_that_names_the_git_directory_of_another_work_tree_has_a_section() {
    let written = written();
    let tree = written.build();
    let named = std::fs::read(tree.root().join(WORKTREE).join(".git")).expect("the file is read");
    write(&tree, "ws/second/.git", &named);

    let taken = snapshot(&tree, None);

    let second = repository(&taken, "ws/second");
    assert_eq!(second.kind, RepositoryKind::Worktree);
    assert_eq!(
        block(second, "dot-git"),
        block(repository(&taken, WORKTREE), "dot-git")
    );
}

#[test]
fn the_git_directory_and_the_common_directory_a_work_tree_alone_names_are_not_entered() {
    let written = written();
    let tree = written.build();
    // The repository of the checkout moves to a directory no `.git` entry names, and the git directory of
    // its worktree to one beside it: git is led to either by the worktree alone.
    let (store, own) = (tree.root().join("ws/store"), tree.root().join("ws/own"));
    std::fs::rename(tree.root().join(MAIN).join(".git"), &store).expect("the repository is moved");
    std::fs::rename(store.join("worktrees/alpha"), &own).expect("the git directory is moved");
    std::fs::write(own.join("commondir"), "../store\n").expect("a file is written");
    let line = format!("gitdir: {}\n", own.display());
    write(&tree, "ws/feature-one/alpha/.git", line.as_bytes());

    let taken = snapshot(&tree, None);

    assert_eq!(repository(&taken, WORKTREE).kind, RepositoryKind::Worktree);
    for directory in ["ws/store", "ws/own"] {
        assert!(tree.root().join(directory).join("HEAD").is_file());
        assert_eq!(entry_kind(&taken, directory), &EntryKind::Directory);
        let beneath = format!("{directory}/");
        let listed: Vec<&str> = taken
            .tree
            .iter()
            .map(|entry| entry.path.as_str())
            .filter(|path| path.starts_with(&beneath))
            .collect();
        assert_eq!(listed, Vec::<&str>::new());
    }
}
