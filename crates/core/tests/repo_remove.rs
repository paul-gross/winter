//! `repo remove`: one repository block removed from a configuration file of a fixture workspace.

use std::collections::BTreeMap;
use std::ffi::OsString;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::time::Duration;

use serde_json::{Value, json};
use winter_workspace_core::{
    CatalogError, CommandError, ConfigError, FileCause, RepositoryKind, Termination, UsageError,
};
use winter_workspace_test_support::{TempTree, WorkspaceBuilder};

#[path = "support/in_process.rs"]
mod in_process;

use in_process::InProcess;

const SHARED: &str = ".winter/config.toml";
const LOCAL: &str = ".winter/config.local.toml";

const COMMENTED: &str = "\
# workspace repositories

[[project_repository]]
url = \"https://fixture.invalid/api.git\"
# after api

# second: web
[[project_repository]]
url = \"https://fixture.invalid/web.git\"
# after web

# third: docs
[[project_repository]]
name = \"docs\"
url = \"https://fixture.invalid/docs.git\"
";

const COMMENTED_WITHOUT_WEB: &str = "\
# workspace repositories

[[project_repository]]
url = \"https://fixture.invalid/api.git\"
# after api

# third: docs
[[project_repository]]
name = \"docs\"
url = \"https://fixture.invalid/docs.git\"
";

/// A local file that declares one standalone repository and one project repository.
const LOCAL_TEXT: &str = "\
[[standalone_repository]]
url = \"https://fixture.invalid/tools.git\"

[[project_repository]]
name = \"mine\"
url = \"https://fixture.invalid/mine.git\"
";

/// The document a successful `repo remove` with `args` in `cwd` writes, which is all it writes.
fn removed(cwd: &Path, args: &[&str]) -> Value {
    let ended = InProcess::new(&[&["repo", "remove"], args].concat())
        .cwd(cwd)
        .run();
    assert!(
        matches!(ended.termination, Termination::Success),
        "{args:?}: {:?}",
        ended.termination
    );
    assert_eq!(ended.stderr, "", "{args:?}");
    serde_json::from_str(&ended.stdout)
        .unwrap_or_else(|error| panic!("{args:?}: {error}: {:?}", ended.stdout))
}

/// The catalog error `repo remove` with `args` in `cwd` ends with, at exit code 1 and having written nothing
/// to either stream, beside the diagnostic of that ending.
fn catalog_error(cwd: &Path, args: &[&str]) -> (CatalogError, String) {
    let ended = InProcess::new(&[&["repo", "remove"], args].concat())
        .cwd(cwd)
        .run();
    assert_eq!(ended.stdout, "", "{args:?}");
    assert_eq!(ended.stderr, "", "{args:?}");
    assert_eq!(ended.termination.exit_code(), 1, "{args:?}");
    let diagnostic = ended.termination.diagnostic().expect("a diagnostic");
    match ended.termination {
        Termination::Command(CommandError::Catalog(error)) => (error, diagnostic),
        other => panic!("{args:?}: expected a catalog error, got {other:?}"),
    }
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

/// `before` with the file `relative` holding `content`.
fn with_file(
    mut before: BTreeMap<PathBuf, Found>,
    relative: &str,
    content: &str,
) -> BTreeMap<PathBuf, Found> {
    let old = before.insert(relative.into(), Found::File(content.as_bytes().to_vec()));
    assert!(matches!(old, Some(Found::File(_))), "{relative}: {old:?}");
    before
}

fn tree() -> TempTree {
    TempTree::new().expect("an isolated tree")
}

/// A workspace whose shared file is the commented configuration, with a state file, a lock file, and a file
/// of a checkout beside it.
fn commented_workspace(tree: &TempTree) -> PathBuf {
    WorkspaceBuilder::new(tree)
        .config(COMMENTED)
        .state("[env_index]\nfeature-x = 15\n")
        .lock("# lock\n")
        .file("projects/web/README.md", b"web\n")
        .build()
        .expect("a workspace")
        .root
}

#[test]
fn the_group_and_the_command_are_listed_in_help() {
    let tree = tree();

    let root_help = InProcess::new(&["--help"]).cwd(tree.root()).run();
    let group_help = InProcess::new(&["repo", "--help"]).cwd(tree.root()).run();

    assert!(root_help.stdout.contains("repo"), "{}", root_help.stdout);
    assert!(
        group_help.stdout.contains("remove"),
        "{}",
        group_help.stdout
    );
}

#[test]
fn a_removal_prints_the_document_and_changes_the_shared_file_and_no_other() {
    let tree = tree();
    let root = commented_workspace(&tree);
    let before = snapshot(&root);

    let document = removed(&root, &["project/web", "--json"]);

    assert_eq!(
        document,
        json!({"removed": true, "kind": "project", "name": "web"})
    );
    assert_eq!(
        snapshot(&root),
        with_file(before, SHARED, COMMENTED_WITHOUT_WEB)
    );
}

#[test]
fn a_removal_from_a_directory_below_the_root_edits_the_root_s_file() {
    let tree = tree();
    let root = commented_workspace(&tree);
    let before = snapshot(&root);

    removed(&root.join("projects/web"), &["project/web", "--json"]);

    assert_eq!(
        snapshot(&root),
        with_file(before, SHARED, COMMENTED_WITHOUT_WEB)
    );
}

#[test]
fn without_the_json_flag_one_line_names_the_type_and_the_name() {
    let tree = tree();
    let root = commented_workspace(&tree);
    let before = snapshot(&root);

    let ended = InProcess::new(&["repo", "remove", "project/web"])
        .cwd(&root)
        .run();

    assert!(
        matches!(ended.termination, Termination::Success),
        "{:?}",
        ended.termination
    );
    assert_eq!(ended.stderr, "");
    assert!(ended.stdout.ends_with('\n'), "{:?}", ended.stdout);
    assert_eq!(ended.stdout.lines().count(), 1, "{:?}", ended.stdout);
    assert!(
        ended.stdout.contains("project") && ended.stdout.contains("web"),
        "{:?}",
        ended.stdout
    );
    assert_eq!(
        snapshot(&root),
        with_file(before, SHARED, COMMENTED_WITHOUT_WEB)
    );
}

#[test]
fn the_document_holds_the_name_as_the_target_gives_it_not_as_the_block_does() {
    let tree = tree();
    let root = WorkspaceBuilder::new(&tree)
        .config("[[standalone_repository]]\nname = 7\n")
        .build()
        .expect("a workspace")
        .root;

    let document = removed(&root, &["standalone/7", "--json"]);

    assert_eq!(
        document,
        json!({"removed": true, "kind": "standalone", "name": "7"})
    );
    assert_eq!(
        std::fs::read_to_string(root.join(SHARED)).expect("the shared file"),
        ""
    );
}

#[test]
fn a_target_without_a_slash_is_refused_inside_and_outside_a_workspace() {
    let tree = tree();
    let root = commented_workspace(&tree);
    let before = snapshot(&root);
    let outside = tree.root().join("home");

    for cwd in [&root, &outside] {
        let (error, diagnostic) = catalog_error(cwd, &["nope"]);

        assert!(
            matches!(&error, CatalogError::TargetShape { target } if target == "nope"),
            "{error:?}"
        );
        assert!(diagnostic.contains("nope"), "{diagnostic}");
    }
    assert_eq!(snapshot(&root), before);
}

#[test]
fn a_well_formed_target_outside_a_workspace_is_a_root_not_found_error() {
    let tree = tree();
    let outside = tree.root().join("home");

    let ended = InProcess::new(&["repo", "remove", "other/x", "--json"])
        .cwd(&outside)
        .run();

    assert_eq!(ended.stdout, "");
    assert!(
        matches!(
            &ended.termination,
            Termination::Config(ConfigError::RootNotFound { start }) if *start == outside
        ),
        "{:?}",
        ended.termination
    );
}

#[test]
fn a_type_that_is_neither_project_nor_standalone_is_refused() {
    let tree = tree();
    let root = commented_workspace(&tree);
    let before = snapshot(&root);

    let (error, diagnostic) = catalog_error(&root, &["other/x"]);

    assert!(
        matches!(&error, CatalogError::UnknownKind { kind } if kind == "other"),
        "{error:?}"
    );
    assert!(diagnostic.contains("other"), "{diagnostic}");
    assert_eq!(snapshot(&root), before);
}

#[test]
fn a_name_no_block_has_is_not_found_and_changes_no_file() {
    let tree = tree();
    let root = commented_workspace(&tree);
    let before = snapshot(&root);

    let (error, diagnostic) = catalog_error(&root, &["project/nope"]);

    assert!(
        matches!(
            &error,
            CatalogError::NotFound { kind: RepositoryKind::Project, name } if name == "nope"
        ),
        "{error:?}"
    );
    assert!(diagnostic.contains("nope"), "{diagnostic}");
    assert_eq!(snapshot(&root), before);
}

#[test]
fn a_target_is_split_at_its_first_slash_so_the_name_may_hold_one() {
    let tree = tree();
    let root = commented_workspace(&tree);
    let before = snapshot(&root);

    let (error, _) = catalog_error(&root, &["project/api/web"]);

    assert!(
        matches!(
            &error,
            CatalogError::NotFound { kind: RepositoryKind::Project, name } if name == "api/web"
        ),
        "{error:?}"
    );
    assert_eq!(snapshot(&root), before);
}

#[test]
fn a_block_of_the_other_array_is_not_found() {
    let tree = tree();
    let root = commented_workspace(&tree);
    let before = snapshot(&root);

    let (error, _) = catalog_error(&root, &["standalone/web"]);

    assert!(
        matches!(
            &error,
            CatalogError::NotFound { kind: RepositoryKind::Standalone, name } if name == "web"
        ),
        "{error:?}"
    );
    assert_eq!(snapshot(&root), before);
}

#[test]
fn the_local_flag_edits_the_local_file_and_leaves_the_shared_file_byte_equal() {
    let tree = tree();
    let root = WorkspaceBuilder::new(&tree)
        .config(COMMENTED)
        .local_config(LOCAL_TEXT)
        .build()
        .expect("a workspace")
        .root;
    let before = snapshot(&root);

    let document = removed(&root, &["standalone/tools", "--local", "--json"]);

    assert_eq!(
        document,
        json!({"removed": true, "kind": "standalone", "name": "tools"})
    );
    assert_eq!(
        snapshot(&root),
        with_file(
            before,
            LOCAL,
            "[[project_repository]]\nname = \"mine\"\nurl = \"https://fixture.invalid/mine.git\"\n"
        )
    );
}

#[test]
fn the_local_flag_without_a_local_file_is_not_found_and_creates_nothing() {
    let tree = tree();
    let root = commented_workspace(&tree);
    let before = snapshot(&root);

    let (error, _) = catalog_error(&root, &["project/api", "--local", "--json"]);

    assert!(
        matches!(
            &error,
            CatalogError::NotFound { kind: RepositoryKind::Project, name } if name == "api"
        ),
        "{error:?}"
    );
    assert_eq!(snapshot(&root), before);
}

#[test]
fn a_repository_of_the_shared_file_is_not_found_with_the_local_flag() {
    let tree = tree();
    let root = WorkspaceBuilder::new(&tree)
        .config(COMMENTED)
        .local_config(LOCAL_TEXT)
        .build()
        .expect("a workspace")
        .root;
    let before = snapshot(&root);

    let (error, _) = catalog_error(&root, &["project/web", "--local"]);

    assert!(
        matches!(&error, CatalogError::NotFound { name, .. } if name == "web"),
        "{error:?}"
    );
    assert_eq!(snapshot(&root), before);
}

#[test]
fn a_repository_declared_only_in_the_local_file_is_not_found_without_the_local_flag() {
    let tree = tree();
    let root = WorkspaceBuilder::new(&tree)
        .config(COMMENTED)
        .local_config(LOCAL_TEXT)
        .build()
        .expect("a workspace")
        .root;
    let before = snapshot(&root);

    let (error, _) = catalog_error(&root, &["project/mine"]);

    assert!(
        matches!(&error, CatalogError::NotFound { name, .. } if name == "mine"),
        "{error:?}"
    );
    assert_eq!(snapshot(&root), before);
}

#[test]
fn a_file_that_is_not_valid_toml_is_a_file_read_error_and_is_left_byte_equal() {
    let tree = tree();
    let invalid = "[[project_repository]]\nurl = \"https://fixture.invalid/api.git\"\nname = \n";
    let root = WorkspaceBuilder::new(&tree)
        .config(invalid)
        .local_config("[[project_repository]\n")
        .build()
        .expect("a workspace")
        .root;
    let before = snapshot(&root);

    for (flags, file) in [(&[][..], SHARED), (&["--local"][..], LOCAL)] {
        let args = [&["repo", "remove", "project/api", "--json"], flags].concat();
        let ended = InProcess::new(&args).cwd(&root).run();

        assert_eq!(ended.stdout, "", "{file}");
        assert_eq!(ended.termination.exit_code(), 1, "{file}");
        assert!(
            matches!(
                &ended.termination,
                Termination::Config(ConfigError::FileRead { path, cause: FileCause::Toml(_) })
                    if *path == root.join(file)
            ),
            "{file}: {:?}",
            ended.termination
        );
    }
    assert_eq!(snapshot(&root), before);
}

#[test]
fn a_file_that_is_not_utf_8_is_a_file_read_error_and_is_left_byte_equal() {
    let tree = tree();
    let root = WorkspaceBuilder::new(&tree)
        .file(SHARED, &[0xff, 0xfe, b'\n'])
        .build()
        .expect("a workspace")
        .root;
    let before = snapshot(&root);

    let ended = InProcess::new(&["repo", "remove", "project/api"])
        .cwd(&root)
        .run();

    assert!(
        matches!(
            &ended.termination,
            Termination::Config(ConfigError::FileRead {
                cause: FileCause::Toml(_),
                ..
            })
        ),
        "{:?}",
        ended.termination
    );
    assert_eq!(snapshot(&root), before);
}

#[test]
fn a_repeated_option_is_a_usage_error_and_changes_no_file() {
    let tree = tree();
    let root = commented_workspace(&tree);
    let before = snapshot(&root);

    for repeated in ["--json", "--local"] {
        let ended = InProcess::new(&["repo", "remove", "project/web", repeated, repeated])
            .cwd(&root)
            .run();

        assert_eq!(ended.termination.exit_code(), 2, "{repeated}");
        assert!(
            matches!(
                &ended.termination,
                Termination::Usage(UsageError::Parse { .. })
            ),
            "{repeated}: {:?}",
            ended.termination
        );
    }
    assert_eq!(snapshot(&root), before);
}

#[test]
fn no_temporary_file_remains_beside_the_file_after_a_removal_and_after_not_found() {
    let tree = tree();
    let root = commented_workspace(&tree);
    let entries = |directory: &Path| -> Vec<OsString> {
        let mut names: Vec<_> = std::fs::read_dir(directory)
            .expect("a readable directory")
            .map(|entry| entry.expect("a directory entry").file_name())
            .collect();
        names.sort();
        names
    };
    let before = entries(&root.join(".winter"));
    assert_eq!(before, ["config.lock", "config.toml", "state.toml"]);

    catalog_error(&root, &["project/nope"]);
    assert_eq!(entries(&root.join(".winter")), before);

    removed(&root, &["project/web", "--json"]);
    assert_eq!(entries(&root.join(".winter")), before);

    catalog_error(&root, &["project/web"]);
    assert_eq!(entries(&root.join(".winter")), before);
}

#[test]
fn the_command_waits_while_another_handle_holds_the_lock_of_the_directory() {
    let tree = tree();
    let root = commented_workspace(&tree);
    let held = std::fs::File::open(root.join(".winter")).expect("the directory");
    held.lock().expect("the lock");
    let (sender, receiver) = mpsc::channel();

    let command = std::thread::spawn({
        let root = root.clone();
        move || {
            let ended = InProcess::new(&["repo", "remove", "project/web", "--json"])
                .cwd(&root)
                .run();
            sender.send(ended).expect("a receiver");
        }
    });

    let early = receiver.recv_timeout(Duration::from_millis(200));
    assert!(
        matches!(early, Err(mpsc::RecvTimeoutError::Timeout)),
        "the command returned while the lock was held"
    );
    assert_eq!(
        std::fs::read_to_string(root.join(SHARED)).expect("the shared file"),
        COMMENTED
    );

    held.unlock().expect("the lock released");
    let ended = receiver
        .recv_timeout(Duration::from_secs(30))
        .expect("the command returns once the lock is released");
    command.join().expect("the command thread");

    assert!(
        matches!(ended.termination, Termination::Success),
        "{:?}",
        ended.termination
    );
    assert_eq!(
        std::fs::read_to_string(root.join(SHARED)).expect("the shared file"),
        COMMENTED_WITHOUT_WEB
    );
}

#[test]
fn the_lock_is_released_after_a_removal_and_after_each_refusal() {
    let tree = tree();
    let root = WorkspaceBuilder::new(&tree)
        .config(COMMENTED)
        .local_config("[[project_repository]\n")
        .build()
        .expect("a workspace")
        .root;
    let free = || {
        let handle = std::fs::File::open(root.join(".winter")).expect("the directory");
        handle.try_lock().is_ok()
    };

    catalog_error(&root, &["project/nope"]);
    assert!(free(), "after not found");

    let invalid = InProcess::new(&["repo", "remove", "project/api", "--local"])
        .cwd(&root)
        .run();
    assert_eq!(invalid.termination.exit_code(), 1);
    assert!(free(), "after a file that is not valid TOML");

    removed(&root, &["project/web", "--json"]);
    assert!(free(), "after a removal");
}

#[test]
fn a_shared_file_that_is_a_symbolic_link_stays_one_and_its_target_is_replaced() {
    let tree = tree();
    let root = WorkspaceBuilder::new(&tree)
        .file("shared/config.toml", COMMENTED.as_bytes())
        .build()
        .expect("a workspace directory")
        .root;
    std::fs::create_dir(root.join(".winter")).expect("the directory");
    std::os::unix::fs::symlink("../shared/config.toml", root.join(SHARED)).expect("the link");
    let before = snapshot(&root);
    assert_eq!(
        before.get(Path::new(SHARED)),
        Some(&Found::Link("../shared/config.toml".into()))
    );

    removed(&root, &["project/web", "--json"]);

    let after = snapshot(&root);
    assert_eq!(
        after,
        with_file(before, "shared/config.toml", COMMENTED_WITHOUT_WEB)
    );
    assert_eq!(
        after.keys().collect::<Vec<_>>(),
        [
            Path::new(".winter"),
            Path::new(".winter/config.toml"),
            Path::new("shared"),
            Path::new("shared/config.toml"),
        ]
    );
}

#[test]
fn a_not_found_through_a_symbolic_link_changes_nothing() {
    let tree = tree();
    let root = WorkspaceBuilder::new(&tree)
        .file("shared/config.toml", COMMENTED.as_bytes())
        .build()
        .expect("a workspace directory")
        .root;
    std::fs::create_dir(root.join(".winter")).expect("the directory");
    std::os::unix::fs::symlink("../shared/config.toml", root.join(SHARED)).expect("the link");
    let before = snapshot(&root);

    catalog_error(&root, &["project/nope"]);

    assert_eq!(snapshot(&root), before);
}

#[test]
fn a_local_file_that_is_a_link_to_no_file_is_not_found_and_nothing_is_created() {
    let tree = tree();
    let root = commented_workspace(&tree);
    std::os::unix::fs::symlink("../shared/absent.toml", root.join(LOCAL)).expect("the link");
    let before = snapshot(&root);

    let (error, _) = catalog_error(&root, &["project/api", "--local"]);

    assert!(matches!(&error, CatalogError::NotFound { .. }), "{error:?}");
    assert_eq!(snapshot(&root), before);
}

#[test]
fn the_file_keeps_the_permission_bits_it_had() {
    for mode in [0o600, 0o640] {
        let tree = tree();
        let root = commented_workspace(&tree);
        let file = root.join(SHARED);
        std::fs::set_permissions(&file, std::fs::Permissions::from_mode(mode))
            .expect("the mode set");

        removed(&root, &["project/web", "--json"]);

        let after = std::fs::metadata(&file).expect("the file").permissions();
        assert_eq!(after.mode() & 0o7777, mode, "{mode:o}");
        assert_eq!(
            std::fs::read_to_string(&file).expect("the shared file"),
            COMMENTED_WITHOUT_WEB,
            "{mode:o}"
        );
    }
}

#[test]
fn a_file_that_cannot_be_opened_for_writing_is_a_file_read_error_and_is_left_as_it_is() {
    let tree = tree();
    let root = commented_workspace(&tree);
    let file = root.join(SHARED);
    std::fs::set_permissions(&file, std::fs::Permissions::from_mode(0o444)).expect("the mode set");
    if std::fs::OpenOptions::new().write(true).open(&file).is_ok() {
        // This user may write any file, and then no mode refuses a plain write either.
        return;
    }
    let before = snapshot(&root);

    let ended = InProcess::new(&["repo", "remove", "project/web", "--json"])
        .cwd(&root)
        .run();

    assert_eq!(ended.stdout, "");
    assert_eq!(ended.termination.exit_code(), 1);
    assert!(
        matches!(
            &ended.termination,
            Termination::Config(ConfigError::FileRead { path, cause: FileCause::Io(error) })
                if *path == file && error.kind() == std::io::ErrorKind::PermissionDenied
        ),
        "{:?}",
        ended.termination
    );
    assert_eq!(snapshot(&root), before);
    let after = std::fs::metadata(&file).expect("the file").permissions();
    assert_eq!(after.mode() & 0o7777, 0o444);
}

#[test]
fn a_sub_table_of_the_block_after_another_table_goes_with_the_block() {
    let tree = tree();
    let text = "\
[[project_repository]]
name = \"docs\"

[[project_repository]]
url = \"https://fixture.invalid/api.git\"

[tool]
x = 1

[project_repository.extra]
k = 1

[[project_repository]]
url = \"https://fixture.invalid/web.git\"
";
    let root = WorkspaceBuilder::new(&tree)
        .config(text)
        .build()
        .expect("a workspace")
        .root;

    removed(&root, &["project/api", "--json"]);

    assert_eq!(
        std::fs::read_to_string(root.join(SHARED)).expect("the shared file"),
        "\
[[project_repository]]
name = \"docs\"

[tool]
x = 1

[[project_repository]]
url = \"https://fixture.invalid/web.git\"
"
    );
}
