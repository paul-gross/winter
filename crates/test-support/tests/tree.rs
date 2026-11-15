//! The isolated tree: its layout, where it may be created, when it is removed, where git run in it stops
//! searching, and the digest of its environment.

use std::fs::Permissions;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use winter_workspace_test_support::{
    Exit, InvocationFailure, Observation, PinnedEnv, TempTree, invoke,
};

/// The test of this file that the tests below run again in a child process, under another environment.
const LAYOUT_TEST: &str = "a_tree_holds_its_layout_and_is_removed_on_drop_unless_kept";

/// The digest of the pinned environment on a host that has git: the SHA-1 of the sorted `NAME=VALUE` lines of
/// the pinned table and then the git configuration text, with the placeholders in place of the two paths.
const PINNED_DIGEST: &str = "f9432b7bd6bfdb84a8badef0f46cffad82b76a81";

/// Names the fixture a child test process was started to run. A fixture does nothing without its own name here.
const FIXTURE_VARIABLE: &str = "WINTER_TEST_FIXTURE";

/// A test of this file that drops a tree it has made unremovable. It fails by design, so it runs only in a
/// child process.
const UNREMOVABLE_TEST: &str = "fixture_a_tree_that_cannot_be_removed_is_dropped";

/// As [`UNREMOVABLE_TEST`], with the tree dropped by the unwinding of a panic.
const UNREMOVABLE_IN_A_PANIC_TEST: &str =
    "fixture_a_tree_that_cannot_be_removed_is_dropped_by_a_panic";

/// A test of this file that makes the directory its tree was created in a git repository, so it runs only in a
/// child process, whose trees are created inside the tree of the test that started it.
const ENCLOSING_REPOSITORY_TEST: &str =
    "fixture_git_run_in_a_tree_does_not_find_a_repository_that_encloses_the_tree";

/// Every fixture of this file.
const FIXTURES: [&str; 3] = [
    UNREMOVABLE_TEST,
    UNREMOVABLE_IN_A_PANIC_TEST,
    ENCLOSING_REPOSITORY_TEST,
];

/// What the fixture that panics on its own says.
const FIXTURE_PANIC: &str = "the panic of the fixture itself";

/// The directory a fixture makes unremovable, relative to the root of its tree.
const LOCKED_DIRECTORY: &str = "tmp/locked";

/// Runs [`LAYOUT_TEST`] alone in a child test process whose trees are created in `parent`, with `extra` added
/// to the pinned environment of `tree`.
fn layout_test_in(
    tree: &TempTree,
    parent: &Path,
    extra: &[(&str, &str)],
) -> Result<Observation, InvocationFailure> {
    child_test_in(tree, parent, &[LAYOUT_TEST], extra)
}

/// Runs one of the fixtures, which a plain run of this file leaves out, in a child test process whose trees
/// are created in `parent`, and tells the fixture it was started for this.
fn fixture_in(
    tree: &TempTree,
    parent: &Path,
    fixture: &str,
) -> Result<Observation, InvocationFailure> {
    child_test_in(
        tree,
        parent,
        &[fixture, "--ignored"],
        &[(FIXTURE_VARIABLE, fixture)],
    )
}

/// Whether this process is the child test process a test started to run the fixture `name`. Anywhere else a
/// fixture's tree would land outside a tree, with no test to clean up after it.
fn started_as_fixture(name: &str) -> bool {
    std::env::var_os(FIXTURE_VARIABLE).is_some_and(|fixture| fixture == name)
}

/// Runs the host's git with `args` in `cwd`, under the pinned environment of `tree`.
fn git(tree: &TempTree, cwd: &Path, args: &[&str]) -> Observation {
    git_under(tree.env(), tree, cwd, args)
}

/// As [`git`], under `env` in place of the pinned environment of `tree`.
fn git_under(env: PinnedEnv, tree: &TempTree, cwd: &Path, args: &[&str]) -> Observation {
    let path = std::env::var_os("PATH").expect("a PATH");
    let executable = std::env::split_paths(&path)
        .map(|directory| directory.join("git"))
        .find(|git| git.is_absolute() && git.is_file())
        .expect("git on the PATH of the test process");
    let mut invocation = tree.invocation(&executable);
    invocation.args = args.iter().map(Into::into).collect();
    invocation.cwd = cwd.to_path_buf();
    invocation.env = env;
    invoke(invocation).expect("git runs to its end")
}

/// Asserts a child test process ran exactly one test and that it passed.
fn assert_one_test_passed(observation: &Observation) {
    assert_eq!(observation.exit, Exit::Code(0), "{observation:?}");
    assert!(
        text(&observation.stdout).contains("test result: ok. 1 passed"),
        "{observation:?}"
    );
}

/// Runs the test `selection` names alone in a child test process whose trees are created in `parent`, with
/// `extra` added to the pinned environment of `tree`.
fn child_test_in(
    tree: &TempTree,
    parent: &Path,
    selection: &[&str],
    extra: &[(&str, &str)],
) -> Result<Observation, InvocationFailure> {
    let this_test_binary = std::env::current_exe().expect("the path of the test executable");
    let mut invocation = tree.invocation(&this_test_binary);
    invocation.args = selection
        .iter()
        .chain(&["--exact", "--nocapture"])
        .map(Into::into)
        .collect();
    invocation.env.set(
        "WINTER_TEST_TMPDIR",
        parent.to_str().expect("a path inside a tree is text"),
    );
    for (name, value) in extra {
        invocation.env.set(name, value);
    }
    invoke(invocation)
}

/// A new directory `name` under the temporary directory of `tree`.
fn directory_in(tree: &TempTree, name: &str) -> PathBuf {
    let directory = tree.root().join("tmp").join(name);
    std::fs::create_dir_all(&directory).expect("a directory inside the tree");
    directory
}

fn entries(directory: &Path) -> Vec<PathBuf> {
    std::fs::read_dir(directory)
        .expect("a readable directory")
        .map(|entry| entry.expect("a directory entry").path())
        .collect()
}

fn text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).into_owned()
}

/// Makes a directory of `tree` one whose content cannot be removed.
fn lock_a_directory_of(tree: &TempTree) {
    let locked = tree.root().join(LOCKED_DIRECTORY);
    std::fs::create_dir(&locked).expect("a directory inside the tree");
    std::fs::write(locked.join("file"), "").expect("a file inside the tree");
    std::fs::set_permissions(&locked, Permissions::from_mode(0o500))
        .expect("a read-only directory");
}

/// Makes the trees a fixture left in `parent` removable again, and returns them. Called before anything is
/// asserted, so that a failed assertion leaves nothing the tree around `parent` cannot remove.
fn unlocked_trees_in(parent: &Path) -> Vec<PathBuf> {
    let left = entries(parent);
    for root in &left {
        // A tree the fixture did remove, or never locked, has no such directory.
        let _ =
            std::fs::set_permissions(root.join(LOCKED_DIRECTORY), Permissions::from_mode(0o700));
    }
    left
}

/// Fails a test that cannot make a directory unremovable: permissions do not bind the superuser.
fn assert_permissions_bind_this_process() {
    // SAFETY: `geteuid` takes nothing, touches no memory, and cannot fail.
    let user = unsafe { libc::geteuid() };
    assert_ne!(
        user, 0,
        "this test makes a directory unremovable through its permissions, which do not bind the superuser"
    );
}

/// What a tree that could not be removed says of itself: its path, then the error of the operating system.
fn removal_failure_of(root: &Path) -> String {
    format!(
        "the isolated tree {} could not be removed: Permission denied",
        root.display()
    )
}

#[test]
fn a_tree_holds_its_layout_and_is_removed_on_drop_unless_kept() {
    let kept = std::env::var_os("WINTER_TEST_KEEP").is_some_and(|keep| keep == "1");
    let tree = TempTree::new().expect("an isolated tree");
    let root = tree.root().to_path_buf();

    for directory in [
        "ws",
        "remotes",
        "home",
        "home/.config",
        "cache",
        "tmp",
        "bin",
        "calls",
        "git-template",
    ] {
        assert!(root.join(directory).is_dir(), "{directory}");
    }
    assert_eq!(
        std::fs::read_to_string(root.join("gitconfig")).expect("the git configuration"),
        format!(
            "[url \"{}/remotes/\"]\n\tinsteadOf = https://fixture.invalid/\n",
            root.display()
        )
    );
    assert_eq!(entries(&root).len(), 9, "{:?}", entries(&root));
    assert_eq!(entries(&root.join("git-template")), Vec::<PathBuf>::new());
    assert_eq!(tree.workspace_dir(), root.join("ws"));

    drop(tree);

    assert_eq!(root.exists(), kept);
}

#[test]
fn the_root_is_a_canonical_path_named_for_the_harness_and_free_of_characters_json_escapes() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = tree.root();

    assert_eq!(root.canonicalize().expect("a canonical path"), root);
    let name = root.file_name().and_then(|name| name.to_str());
    assert!(
        name.is_some_and(|name| name.starts_with("winter-workspace-test-")),
        "{name:?}"
    );
    let root = root.to_str().expect("a root that is text");
    assert!(
        root.chars()
            .all(|character| character.is_ascii_alphanumeric() || "/._-".contains(character)),
        "{root}"
    );
}

#[test]
fn a_dropped_tree_leaves_nothing_in_its_parent() {
    let tree = TempTree::new().expect("an isolated tree");
    let parent = directory_in(&tree, "parent");

    let observation = layout_test_in(&tree, &parent, &[]).expect("a child test run");

    assert_eq!(observation.exit, Exit::Code(0), "{observation:?}");
    assert_eq!(entries(&parent), Vec::<PathBuf>::new());
}

#[test]
fn a_tree_is_kept_and_its_path_printed_when_keeping_is_asked_for() {
    let tree = TempTree::new().expect("an isolated tree");
    let parent = directory_in(&tree, "parent");

    let observation =
        layout_test_in(&tree, &parent, &[("WINTER_TEST_KEEP", "1")]).expect("a child test run");

    assert_eq!(observation.exit, Exit::Code(0), "{observation:?}");
    let kept = entries(&parent);
    assert_eq!(kept.len(), 1, "{kept:?}");
    assert!(kept[0].join("ws").is_dir(), "{kept:?}");
    assert!(kept[0].join("gitconfig").is_file(), "{kept:?}");
    let stderr = text(&observation.stderr);
    let root = kept[0].to_str().expect("a root that is text");
    assert!(stderr.contains(root), "{stderr}");
}

#[test]
#[ignore = "fails by design: the tests of a failed removal run it in a child process"]
fn fixture_a_tree_that_cannot_be_removed_is_dropped() {
    if !started_as_fixture(UNREMOVABLE_TEST) {
        return;
    }
    let tree = TempTree::new().expect("an isolated tree");
    lock_a_directory_of(&tree);

    drop(tree);
}

#[test]
#[ignore = "fails by design: the tests of a failed removal run it in a child process"]
fn fixture_a_tree_that_cannot_be_removed_is_dropped_by_a_panic() {
    if !started_as_fixture(UNREMOVABLE_IN_A_PANIC_TEST) {
        return;
    }
    let tree = TempTree::new().expect("an isolated tree");
    lock_a_directory_of(&tree);

    panic!("{FIXTURE_PANIC}");
}

#[test]
fn a_tree_that_cannot_be_removed_fails_the_test_and_names_its_path_and_the_error() {
    assert_permissions_bind_this_process();
    let tree = TempTree::new().expect("an isolated tree");
    let parent = directory_in(&tree, "parent");

    let outcome = fixture_in(&tree, &parent, UNREMOVABLE_TEST);
    let left = unlocked_trees_in(&parent);

    let observation = outcome.expect("a child test run");
    assert_eq!(observation.exit, Exit::Code(101), "{observation:?}");
    assert_eq!(left.len(), 1, "{left:?}");
    assert!(
        left[0].join(LOCKED_DIRECTORY).join("file").is_file(),
        "{left:?}"
    );
    let stderr = text(&observation.stderr);
    assert!(stderr.contains(&removal_failure_of(&left[0])), "{stderr}");
    assert!(stderr.contains("panicked at"), "{stderr}");
    assert!(
        text(&observation.stdout).contains("1 failed"),
        "{observation:?}"
    );
}

#[test]
fn a_tree_that_cannot_be_removed_during_a_panic_is_named_and_the_panic_goes_on() {
    assert_permissions_bind_this_process();
    let tree = TempTree::new().expect("an isolated tree");
    let parent = directory_in(&tree, "parent");

    let outcome = fixture_in(&tree, &parent, UNREMOVABLE_IN_A_PANIC_TEST);
    let left = unlocked_trees_in(&parent);

    // A second panic during the unwinding would abort the child: a signal, and no test result.
    let observation = outcome.expect("a child test run");
    assert_eq!(observation.exit, Exit::Code(101), "{observation:?}");
    assert_eq!(left.len(), 1, "{left:?}");
    let stderr = text(&observation.stderr);
    assert!(stderr.contains(FIXTURE_PANIC), "{stderr}");
    assert_eq!(
        stderr.matches(&removal_failure_of(&left[0])).count(),
        1,
        "{stderr}"
    );
    assert_eq!(stderr.matches("panicked at").count(), 1, "{stderr}");
}

#[test]
fn no_tree_is_created_below_a_workspace_configuration() {
    let tree = TempTree::new().expect("an isolated tree");
    let workspace = directory_in(&tree, "workspace");
    std::fs::create_dir(workspace.join(".winter")).expect("a configuration directory");
    std::fs::write(workspace.join(".winter/config.toml"), "").expect("a configuration file");
    let parent = directory_in(&tree, "workspace/env/deeper");

    let observation = layout_test_in(&tree, &parent, &[]).expect("a child test run");

    assert_eq!(observation.exit, Exit::Code(101), "{observation:?}");
    let stderr = text(&observation.stderr);
    let configuration = workspace.join(".winter/config.toml");
    assert!(
        stderr.contains(configuration.to_str().expect("a path that is text")),
        "{stderr}"
    );
    assert_eq!(entries(&parent), Vec::<PathBuf>::new());
}

#[test]
fn no_tree_is_created_in_a_workspace_itself() {
    let tree = TempTree::new().expect("an isolated tree");
    let parent = directory_in(&tree, "workspace");
    std::fs::create_dir(parent.join(".winter")).expect("a configuration directory");
    std::fs::write(parent.join(".winter/config.toml"), "").expect("a configuration file");

    let observation = layout_test_in(&tree, &parent, &[]).expect("a child test run");

    assert_eq!(observation.exit, Exit::Code(101), "{observation:?}");
    let stderr = text(&observation.stderr);
    let configuration = parent.join(".winter/config.toml");
    assert!(
        stderr.contains(&format!(
            "{} is a workspace configuration",
            configuration.display()
        )),
        "{stderr}"
    );
    assert_eq!(entries(&parent), [parent.join(".winter")]);
}

#[test]
fn no_tree_is_created_through_a_link_that_leads_into_a_workspace() {
    let tree = TempTree::new().expect("an isolated tree");
    let workspace = directory_in(&tree, "workspace");
    std::fs::create_dir(workspace.join(".winter")).expect("a configuration directory");
    std::fs::write(workspace.join(".winter/config.toml"), "").expect("a configuration file");
    let inside = directory_in(&tree, "workspace/env");
    let link = tree.root().join("tmp/link");
    std::os::unix::fs::symlink(&inside, &link).expect("a symbolic link inside the tree");

    let observation = layout_test_in(&tree, &link, &[]).expect("a child test run");

    assert_eq!(observation.exit, Exit::Code(101), "{observation:?}");
    let stderr = text(&observation.stderr);
    let configuration = workspace.join(".winter/config.toml");
    assert!(
        stderr.contains(&format!(
            "{} is a workspace configuration",
            configuration.display()
        )),
        "{stderr}"
    );
    assert_eq!(entries(&inside), Vec::<PathBuf>::new());
    assert_eq!(entries(&workspace).len(), 2, "{:?}", entries(&workspace));
}

#[test]
fn no_tree_is_created_below_a_directory_that_holds_a_git_entry() {
    let tree = TempTree::new().expect("an isolated tree");
    let work_tree = directory_in(&tree, "work-tree");
    std::fs::create_dir(work_tree.join(".git")).expect("a .git directory");
    let linked = directory_in(&tree, "linked");
    std::fs::write(linked.join(".git"), "gitdir: elsewhere\n").expect("a .git file");

    for repository in [work_tree, linked] {
        let parent = repository.join("env/deeper");
        std::fs::create_dir_all(&parent).expect("a directory inside the tree");

        let observation = layout_test_in(&tree, &parent, &[]).expect("a child test run");

        assert_eq!(observation.exit, Exit::Code(101), "{observation:?}");
        let stderr = text(&observation.stderr);
        assert!(
            stderr.contains(&format!(
                "no isolated tree is created in {}: {} is a git repository",
                parent.display(),
                repository.join(".git").display()
            )),
            "{stderr}"
        );
        assert!(!stderr.contains("workspace configuration"), "{stderr}");
        assert_eq!(entries(&parent), Vec::<PathBuf>::new());
    }
}

#[test]
fn no_tree_is_created_in_a_directory_that_holds_a_git_entry_itself() {
    let tree = TempTree::new().expect("an isolated tree");
    let parent = directory_in(&tree, "work-tree");
    std::fs::create_dir(parent.join(".git")).expect("a .git directory");

    let observation = layout_test_in(&tree, &parent, &[]).expect("a child test run");

    assert_eq!(observation.exit, Exit::Code(101), "{observation:?}");
    let stderr = text(&observation.stderr);
    assert!(
        stderr.contains(&format!(
            "{} is a git repository",
            parent.join(".git").display()
        )),
        "{stderr}"
    );
    assert_eq!(entries(&parent), [parent.join(".git")]);
}

#[test]
fn no_tree_is_created_inside_a_bare_repository() {
    let tree = TempTree::new().expect("an isolated tree");
    let bare = tree.root().join("tmp/bare.git");
    let made = git(
        &tree,
        &tree.root().join("tmp"),
        &["init", "--bare", "bare.git"],
    );
    assert_eq!(made.exit, Exit::Code(0), "{made:?}");

    for parent in [bare.join("refs/heads"), bare.clone()] {
        let before = entries(&parent);

        let observation = layout_test_in(&tree, &parent, &[]).expect("a child test run");

        assert_eq!(observation.exit, Exit::Code(101), "{observation:?}");
        let stderr = text(&observation.stderr);
        assert!(
            stderr.contains(&format!(
                "no isolated tree is created in {}: {} is a git repository",
                parent.display(),
                bare.display()
            )),
            "{stderr}"
        );
        assert!(!stderr.contains("workspace configuration"), "{stderr}");
        assert_eq!(entries(&parent), before);
    }
}

#[test]
fn a_workspace_that_is_also_a_repository_is_refused_as_a_workspace() {
    let tree = TempTree::new().expect("an isolated tree");
    let workspace = directory_in(&tree, "workspace");
    std::fs::create_dir(workspace.join(".winter")).expect("a configuration directory");
    std::fs::write(workspace.join(".winter/config.toml"), "").expect("a configuration file");
    let parent = directory_in(&tree, "workspace/projects/project");
    std::fs::create_dir(parent.join(".git")).expect("a .git directory");

    let observation = layout_test_in(&tree, &parent, &[]).expect("a child test run");

    assert_eq!(observation.exit, Exit::Code(101), "{observation:?}");
    let stderr = text(&observation.stderr);
    assert!(
        stderr.contains(&format!(
            "{} is a workspace configuration",
            workspace.join(".winter/config.toml").display()
        )),
        "{stderr}"
    );
    assert!(!stderr.contains("is a git repository"), "{stderr}");
    assert_eq!(entries(&parent), [parent.join(".git")]);
}

#[test]
#[ignore = "makes the directory its tree is created in a repository: its test runs it in a child process"]
fn fixture_git_run_in_a_tree_does_not_find_a_repository_that_encloses_the_tree() {
    if !started_as_fixture(ENCLOSING_REPOSITORY_TEST) {
        return;
    }
    let tree = TempTree::new().expect("an isolated tree");
    let root = tree.root();
    let workspace = tree.workspace_dir();
    let enclosing = root
        .parent()
        .expect("the directory the tree was created in");
    let made = git(&tree, enclosing, &["init"]);
    assert_eq!(made.exit, Exit::Code(0), "{made:?}");

    // Without the ceiling the enclosing repository is what git finds from inside the tree.
    let mut unbounded = tree.env();
    unbounded.set("GIT_CEILING_DIRECTORIES", "");
    let found = git_under(
        unbounded,
        &tree,
        &workspace,
        &["rev-parse", "--show-toplevel"],
    );
    assert_eq!(found.exit, Exit::Code(0), "{found:?}");
    assert_eq!(Path::new(text(&found.stdout).trim_end()), enclosing);

    for directory in [workspace.as_path(), root] {
        let found = git(&tree, directory, &["rev-parse", "--show-toplevel"]);
        assert_eq!(found.exit, Exit::Code(128), "{found:?}");
        assert_eq!(text(&found.stdout), "");
    }
    let committed = git(
        &tree,
        &workspace,
        &["commit", "--allow-empty", "-m", "probe"],
    );
    assert_eq!(committed.exit, Exit::Code(128), "{committed:?}");
    let git_directory = format!("--git-dir={}", enclosing.join(".git").display());
    let commits = git(&tree, enclosing, &[&git_directory, "rev-list", "--all"]);
    assert_eq!(commits.exit, Exit::Code(0), "{commits:?}");
    assert_eq!(text(&commits.stdout), "");
    let objects = git(&tree, enclosing, &[&git_directory, "count-objects"]);
    assert_eq!(text(&objects.stdout), "0 objects, 0 kilobytes\n");

    let made = git(&tree, &workspace, &["init"]);
    assert_eq!(made.exit, Exit::Code(0), "{made:?}");
    let below = workspace.join("below/deeper");
    std::fs::create_dir_all(&below).expect("a directory inside the tree");
    for directory in [workspace.as_path(), below.as_path()] {
        let found = git(&tree, directory, &["rev-parse", "--show-toplevel"]);
        assert_eq!(found.exit, Exit::Code(0), "{found:?}");
        assert_eq!(Path::new(text(&found.stdout).trim_end()), workspace);
    }
}

#[test]
fn git_run_in_a_tree_stops_at_the_tree_and_finds_a_repository_made_inside_it() {
    let tree = TempTree::new().expect("an isolated tree");
    let enclosing = directory_in(&tree, "enclosing");

    let observation =
        fixture_in(&tree, &enclosing, ENCLOSING_REPOSITORY_TEST).expect("a child test run");

    assert_one_test_passed(&observation);
    // The fixture did run: it made the directory a repository, after its tree was created there.
    assert_eq!(entries(&enclosing), [enclosing.join(".git")]);
    assert!(enclosing.join(".git/HEAD").is_file());
}

#[test]
fn a_fixture_started_without_its_own_name_does_nothing() {
    let tree = TempTree::new().expect("an isolated tree");

    for fixture in FIXTURES {
        let other = FIXTURES
            .into_iter()
            .find(|other| *other != fixture)
            .expect("another fixture");
        for marker in [None, Some(other)] {
            let parent = directory_in(&tree, &format!("{fixture}-{}", marker.is_some()));
            let extra: Vec<(&str, &str)> = marker
                .map(|name| (FIXTURE_VARIABLE, name))
                .into_iter()
                .collect();

            let outcome = child_test_in(&tree, &parent, &[fixture, "--ignored"], &extra);
            let left = unlocked_trees_in(&parent);

            let observation = outcome.expect("a child test run");
            assert_one_test_passed(&observation);
            assert_eq!(left, Vec::<PathBuf>::new(), "{fixture} {marker:?}");
        }
    }
}

#[test]
fn no_tree_is_created_where_the_root_would_need_escaping() {
    let tree = TempTree::new().expect("an isolated tree");
    let parent = directory_in(&tree, "a \"quoted\" parent");

    let observation = layout_test_in(&tree, &parent, &[]).expect("a child test run");

    assert_eq!(observation.exit, Exit::Code(101), "{observation:?}");
    let stderr = text(&observation.stderr);
    assert!(stderr.contains("[A-Za-z0-9/._-]"), "{stderr}");
    assert_eq!(entries(&parent), Vec::<PathBuf>::new());
}

#[test]
fn the_digests_of_two_trees_are_equal() {
    let first = TempTree::new().expect("an isolated tree");
    let second = TempTree::new().expect("an isolated tree");
    assert_ne!(first.root(), second.root());

    assert_eq!(first.env().digest(), second.env().digest());
    assert_eq!(first.env().digest(), PINNED_DIGEST);
}

#[test]
fn setting_a_variable_changes_the_digest() {
    let tree = TempTree::new().expect("an isolated tree");
    let mut env = tree.env();
    let pinned = env.digest();

    env.set("FIXTURE_VARIABLE", "1");

    let added = env.digest();
    assert_ne!(added, pinned);
    env.set("FIXTURE_VARIABLE", "2");
    assert_ne!(env.digest(), added);
    env.set("TERM", "xterm");
    assert_ne!(env.digest(), added);
}

#[test]
fn a_set_value_that_holds_the_root_gives_the_same_digest_in_any_tree() {
    let first = TempTree::new().expect("an isolated tree");
    let second = TempTree::new().expect("an isolated tree");
    let with_stub_directory = |tree: &TempTree| {
        let mut env = tree.env();
        let stubs = tree.root().join("bin/stubs");
        env.set(
            "FIXTURE_STUBS",
            stubs.to_str().expect("a path that is text"),
        );
        env.digest()
    };

    assert_eq!(with_stub_directory(&first), with_stub_directory(&second));
    assert_ne!(with_stub_directory(&first), first.env().digest());
}
