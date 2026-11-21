//! The lock file: read and upserted against the real file port in a fixture workspace.

use std::path::Path;

use winter_workspace_core::{
    ConfigError, LockEntry, LockEntryFault, LockError, OsFiles, RefKind, read_lock, upsert_lock,
};
use winter_workspace_test_support::{TempTree, WorkspaceBuilder};

const COMMIT: &str = "0123456789abcdef0123456789abcdef01234567";

fn workspace(tree: &TempTree, lock: Option<&str>) -> std::path::PathBuf {
    let builder = WorkspaceBuilder::new(tree).config("# workspace\n");
    match lock {
        Some(lock) => builder.lock(lock),
        None => builder,
    }
    .build()
    .expect("a workspace")
    .root
}

fn entry(name: &str) -> LockEntry {
    LockEntry {
        name: name.to_owned(),
        r#ref: "main".to_owned(),
        kind: RefKind::Branch,
        commit: COMMIT.to_owned(),
    }
}

fn lock_error(result: Result<impl std::fmt::Debug, ConfigError>) -> LockError {
    match result {
        Err(ConfigError::Lock(error)) => error,
        other => panic!("expected a lock error, got {other:?}"),
    }
}

fn read(root: &Path) -> Result<std::collections::BTreeMap<String, LockEntry>, ConfigError> {
    read_lock(&OsFiles, root)
}

#[test]
fn a_missing_lock_reads_as_empty() {
    let tree = TempTree::new().expect("a tree");
    let root = workspace(&tree, None);

    assert!(read(&root).expect("a lock").is_empty());
}

#[test]
fn an_unreadable_lock_is_a_load_error() {
    let tree = TempTree::new().expect("a tree");
    let root = workspace(&tree, None);
    std::fs::create_dir(root.join(".winter/config.lock")).expect("a directory");

    assert!(matches!(
        lock_error(read(&root)),
        LockError::Load {
            cause: winter_workspace_core::FileCause::Io(_),
            ..
        }
    ));
}

#[test]
fn invalid_toml_is_a_load_error() {
    let tree = TempTree::new().expect("a tree");
    let root = workspace(&tree, Some("version = \n"));

    assert!(matches!(
        lock_error(read(&root)),
        LockError::Load {
            cause: winter_workspace_core::FileCause::Toml(_),
            ..
        }
    ));
}

#[test]
fn version_two_is_unsupported() {
    let tree = TempTree::new().expect("a tree");
    let root = workspace(&tree, Some("version = 2\n"));

    assert!(matches!(
        lock_error(read(&root)),
        LockError::UnsupportedVersion { found: Some(_), .. }
    ));
}

#[test]
fn an_entry_without_a_commit_is_malformed() {
    let tree = TempTree::new().expect("a tree");
    let root = workspace(
        &tree,
        Some("version = 1\n[[standalone]]\nname = \"n\"\nref = \"main\"\nkind = \"branch\"\n"),
    );

    assert!(matches!(
        lock_error(read(&root)),
        LockError::MalformedEntry {
            fault: LockEntryFault::MissingField { field: "commit" },
            ..
        }
    ));
}

#[test]
fn an_unknown_kind_is_malformed() {
    let tree = TempTree::new().expect("a tree");
    let root = workspace(
        &tree,
        Some(
            "version = 1\n[[standalone]]\nname = \"n\"\nref = \"main\"\nkind = \"branchy\"\ncommit = \"abc\"\n",
        ),
    );

    assert!(matches!(
        lock_error(read(&root)),
        LockError::MalformedEntry {
            fault: LockEntryFault::InvalidKind { .. },
            ..
        }
    ));
}

#[test]
fn an_upsert_into_a_malformed_lock_fails_and_leaves_the_bytes() {
    let tree = TempTree::new().expect("a tree");
    let malformed = "version = 2\n";
    let root = workspace(&tree, Some(malformed));

    let error = lock_error(upsert_lock(&OsFiles, &root, entry("a")));

    assert!(matches!(error, LockError::UnsupportedVersion { .. }));
    assert_eq!(
        std::fs::read_to_string(root.join(".winter/config.lock")).expect("the lock"),
        malformed
    );
}

#[test]
fn an_upsert_creates_the_lock_and_replaces_an_entry_by_name() {
    let tree = TempTree::new().expect("a tree");
    let root = workspace(&tree, None);

    upsert_lock(&OsFiles, &root, entry("b")).expect("an upsert");
    upsert_lock(&OsFiles, &root, entry("a")).expect("an upsert");
    let mut moved = entry("b");
    moved.r#ref = "v1".to_owned();
    moved.kind = RefKind::Tag;
    upsert_lock(&OsFiles, &root, moved.clone()).expect("an upsert");

    let read = read(&root).expect("a lock");
    assert_eq!(read.len(), 2);
    assert_eq!(read["b"], moved);
    let text = std::fs::read_to_string(root.join(".winter/config.lock")).expect("the lock");
    assert!(text.find("\"a\"").unwrap() < text.find("\"b\"").unwrap());
}

#[test]
fn two_threads_upserting_different_names_leave_both_entries() {
    let tree = TempTree::new().expect("a tree");
    let root = workspace(&tree, None);

    std::thread::scope(|scope| {
        for name in ["left", "right"] {
            let root = &root;
            scope.spawn(move || {
                for round in 0..20 {
                    let mut pinned = entry(name);
                    pinned.r#ref = format!("round-{round}");
                    upsert_lock(&OsFiles, root, pinned).expect("an upsert");
                }
            });
        }
    });

    let read = read(&root).expect("a lock");
    assert_eq!(
        read.keys().map(String::as_str).collect::<Vec<_>>(),
        ["left", "right"]
    );
}
