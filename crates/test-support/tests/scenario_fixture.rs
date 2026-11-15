//! The fixture builder: what a description builds, in which order, and what it refuses to write or run.

use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use winter_workspace_test_support::scenario::fixture::{FixtureError, build, build_in};
use winter_workspace_test_support::scenario::{Malformed, Scenario};
use winter_workspace_test_support::{Git, GitError, TempTree};

/// The id of the scenario every test writes.
const ID: &str = "ws-index/basic";

const CONFIG: &str =
    "# kept as written\n[projects.alpha]\nurl = \"https://fixture.invalid/alpha.git\"\n";

/// One remote of two commits, one checkout, one worktree, a workspace root repository, and one raw git call.
const ONE_OF_EACH: &str = r#"
[fixture]
workspace_git = true
gitignore = "target/\n"

[[fixture.remote]]
name = "alpha"
[[fixture.remote.commit]]
message = "first"
files = { "README.md" = "one\n" }
[[fixture.remote.commit]]
message = "second"
files = { "README.md" = "two\n", "src/lib.rs" = "" }

[[fixture.checkout]]
remote = "alpha"
path = "projects/alpha"

[[fixture.worktree]]
checkout = "projects/alpha"
path = "feature-one/alpha"
branch = "feature-one"
start = "origin/main"

[[fixture.git]]
cwd = "feature-one/alpha"
args = ["commit", "--allow-empty", "-m", "local"]
"#;

/// A remote `alpha` of one commit.
const REMOTE: &str = r#"
[[fixture.remote]]
name = "alpha"
[[fixture.remote.commit]]
message = "first"
files = { "README.md" = "one\n" }
"#;

/// A scenario directory under construction, in a tree of its own: the tree a fixture is built in is
/// another, so everything of this one is outside it.
struct Written {
    host: TempTree,
    dir: PathBuf,
}

impl Written {
    /// A scenario directory with a description that has `fixture` before its one step.
    fn new(fixture: &str) -> Self {
        let host = TempTree::new().expect("a tree for the scenario directory");
        let dir = host.root().join("tmp/scenarios").join(ID);
        std::fs::create_dir_all(&dir).expect("the scenario directory is created");
        let description = format!("summary = \"s\"\n{fixture}\n[[step]]\nargs = [\"doctor\"]\n");
        std::fs::write(dir.join("scenario.toml"), description).expect("the description is written");
        Self { host, dir }
    }

    /// Writes `content` to `path` of the scenario directory, with the directories on the way.
    fn file(self, path: &str, content: &[u8]) -> Self {
        let file = self.dir.join(path);
        std::fs::create_dir_all(file.parent().expect("a file has a parent"))
            .expect("the directories are created");
        std::fs::write(file, content).expect("the file is written");
        self
    }

    /// Makes `path` of the scenario directory a symbolic link to `target`.
    fn link(self, path: &str, target: &Path) -> Self {
        let link = self.dir.join(path);
        std::fs::create_dir_all(link.parent().expect("a link has a parent"))
            .expect("the directories are created");
        std::os::unix::fs::symlink(target, link).expect("the link is made");
        self
    }

    /// An empty directory outside every tree a fixture is built in.
    fn outside(&self) -> PathBuf {
        let outside = self.host.root().join("cache/outside");
        std::fs::create_dir_all(&outside).expect("the outside directory is created");
        outside
    }

    fn load(&self) -> Result<Scenario, Malformed> {
        Scenario::load(&self.dir)
    }

    fn scenario(&self) -> Scenario {
        self.load().expect("the description parses")
    }
}

/// The description [`ONE_OF_EACH`] with a workspace configuration and a file that is copied last.
fn one_of_each() -> Written {
    Written::new(ONE_OF_EACH)
        .file("files/.winter/config.toml", CONFIG.as_bytes())
        .file("dirty/notes.txt", b"not committed\n")
}

/// What git prints for `args` in `cwd`, a directory of `tree`.
fn git(tree: &TempTree, cwd: &Path, args: &[&str]) -> String {
    Git::new(tree)
        .expect("git")
        .run(cwd, args)
        .expect("git answers")
}

/// Every ref of the repository at `repository` with the object it names.
fn refs(tree: &TempTree, repository: &str) -> String {
    let format = "--format=%(refname) %(objectname)";
    git(
        tree,
        &tree.root().join(repository),
        &["for-each-ref", format],
    )
}

/// The names of the entries of `directory`.
fn entries(directory: &Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(directory)
        .expect("a directory")
        .map(|entry| {
            entry
                .expect("an entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .collect();
    names.sort();
    names
}

/// Asserts that `error` says the description is malformed, and names the scenario and `key`.
#[track_caller]
fn assert_malformed(error: &FixtureError, key: &str) {
    let FixtureError::Malformed(malformed) = error else {
        panic!("not malformed: {error}");
    };
    assert_eq!(malformed.scenario, ID);
    assert_eq!(malformed.key, key, "{malformed}");
    let message = error.to_string();
    assert!(message.contains(ID), "{message}");
    assert!(message.contains(key), "{message}");
}

/// Builds the fixture of `written` in a fresh tree, expecting the build to fail, and answers the tree with
/// the error.
#[track_caller]
fn refused(written: &Written) -> (TempTree, FixtureError) {
    let tree = TempTree::new().expect("an isolated tree");
    let error = build_in(&written.scenario(), &tree).expect_err("the build fails");
    (tree, error)
}

#[test]
fn a_description_with_one_of_each_builds() {
    let written = one_of_each();

    let tree = build(&written.scenario()).expect("the fixture builds");

    let workspace = tree.workspace_dir();
    let worktree = workspace.join("feature-one/alpha");
    assert_eq!(
        git(&tree, &worktree, &["symbolic-ref", "HEAD"]),
        "refs/heads/feature-one\n"
    );
    assert_eq!(
        git(&tree, &worktree, &["log", "--format=%s"]),
        "local\nsecond\nfirst\n"
    );
    assert_eq!(
        git(
            &tree,
            &workspace.join("projects/alpha"),
            &["log", "--format=%s"]
        ),
        "second\nfirst\n"
    );
    assert_eq!(
        git(&tree, &workspace, &["log", "--format=%s"]),
        "Workspace root\n"
    );
    assert_eq!(
        git(&tree, &workspace, &["show", "HEAD:.winter/config.toml"]),
        CONFIG
    );
    assert_eq!(
        git(&tree, &workspace, &["show", "HEAD:.gitignore"]),
        "target/\n"
    );
    let status = git(
        &tree,
        &workspace,
        &["status", "--porcelain", "--untracked-files=all"],
    );
    assert!(
        status.lines().any(|line| line == "?? notes.txt"),
        "{status}"
    );
    assert_eq!(
        std::fs::read(workspace.join("notes.txt")).expect("the dirty file"),
        b"not committed\n"
    );
}

#[test]
fn two_builds_of_one_description_have_equal_object_ids() {
    let written = one_of_each();
    let scenario = written.scenario();

    let first = build(&scenario).expect("the fixture builds");
    let second = build(&scenario).expect("the fixture builds again");

    assert_ne!(first.root(), second.root());
    for repository in [
        "remotes/alpha.git",
        "ws",
        "ws/projects/alpha",
        "ws/feature-one/alpha",
    ] {
        let ids = refs(&first, repository);
        assert!(!ids.is_empty(), "{repository}");
        assert_eq!(ids, refs(&second, repository), "{repository}");
    }
}

#[test]
fn a_description_of_files_alone_builds_those_files_and_no_repository() {
    let written = Written::new("").file("files/.winter/config.toml", CONFIG.as_bytes());

    let tree = build(&written.scenario()).expect("the fixture builds");

    let workspace = tree.workspace_dir();
    assert_eq!(entries(&workspace), [".winter"]);
    assert_eq!(entries(&workspace.join(".winter")), ["config.toml"]);
    assert_eq!(
        std::fs::read(workspace.join(".winter/config.toml")).expect("the file"),
        CONFIG.as_bytes()
    );
    assert_eq!(entries(&tree.root().join("remotes")), [] as [&str; 0]);
}

#[test]
fn the_root_token_of_a_copied_file_becomes_the_root_of_the_tree() {
    let written = Written::new("")
        .file(
            "files/.winter/config.toml",
            b"url = \"<ROOT>/remotes/x.git\"\n",
        )
        .file("dirty/twice.bin", b"\xff<ROOT>|<ROOT>\n");

    let tree = build(&written.scenario()).expect("the fixture builds");

    let root = tree.root().to_str().expect("a root of plain characters");
    let workspace = tree.workspace_dir();
    assert_eq!(
        std::fs::read_to_string(workspace.join(".winter/config.toml")).expect("the file"),
        format!("url = \"{root}/remotes/x.git\"\n")
    );
    let mut twice = b"\xff".to_vec();
    twice.extend_from_slice(format!("{root}|{root}\n").as_bytes());
    assert_eq!(
        std::fs::read(workspace.join("twice.bin")).expect("the file"),
        twice
    );
}

#[test]
fn a_copied_file_keeps_its_execute_bit() {
    let written = Written::new("")
        .file("files/run.sh", b"#!/bin/sh\n")
        .file("files/plain.txt", b"plain\n");
    let script = written.dir.join("files/run.sh");
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).expect("its mode");

    let tree = build(&written.scenario()).expect("the fixture builds");

    let mode = |name: &str| {
        let metadata = std::fs::metadata(tree.workspace_dir().join(name)).expect("the file");
        metadata.permissions().mode() & 0o111
    };
    assert_eq!(mode("run.sh"), 0o111);
    assert_eq!(mode("plain.txt"), 0);
}

#[test]
fn a_link_among_the_files_is_copied_as_a_link() {
    let written = Written::new("");
    let outside = written.outside();
    let written = written
        .link("files/out", &outside)
        .link("files/sub/root", Path::new("<ROOT>/remotes"));

    let tree = build(&written.scenario()).expect("the fixture builds");

    let workspace = tree.workspace_dir();
    assert_eq!(
        std::fs::read_link(workspace.join("out")).expect("a link"),
        outside
    );
    assert_eq!(
        std::fs::read_link(workspace.join("sub/root")).expect("a link"),
        tree.root().join("remotes")
    );
    assert_eq!(entries(&outside), [] as [&str; 0]);
}

#[test]
fn a_checkout_of_an_undeclared_remote_fails_before_anything_is_built() {
    let fixture =
        format!("{REMOTE}\n[[fixture.checkout]]\nremote = \"beta\"\npath = \"projects/beta\"\n");
    let written = Written::new(&fixture).file("files/.winter/config.toml", CONFIG.as_bytes());

    let (tree, error) = refused(&written);

    assert_malformed(&error, "remote");
    assert!(
        error.to_string().contains("[[fixture.checkout]] 1"),
        "{error}"
    );
    assert!(!tree.workspace_dir().join("projects").exists());
    assert_eq!(entries(&tree.workspace_dir()), [] as [&str; 0]);
    assert_eq!(entries(&tree.root().join("remotes")), [] as [&str; 0]);
    let error = build(&written.scenario()).expect_err("no tree is answered");
    assert_malformed(&error, "remote");
}

#[test]
fn a_worktree_of_an_undeclared_checkout_fails_before_anything_is_built() {
    let fixture = format!(
        "{REMOTE}\n[[fixture.checkout]]\nremote = \"alpha\"\npath = \"projects/alpha\"\n\n\
         [[fixture.worktree]]\ncheckout = \"projects/beta\"\npath = \"one/beta\"\nbranch = \"one\"\n\
         start = \"origin/main\"\n"
    );
    let written = Written::new(&fixture);

    let (tree, error) = refused(&written);

    assert_malformed(&error, "checkout");
    assert!(
        error.to_string().contains("[[fixture.worktree]] 1"),
        "{error}"
    );
    assert_eq!(entries(&tree.workspace_dir()), [] as [&str; 0]);
    assert_eq!(entries(&tree.root().join("remotes")), [] as [&str; 0]);
}

#[test]
fn a_remote_declared_twice_fails_before_anything_is_built() {
    let written = Written::new(&format!("{REMOTE}\n{REMOTE}"));

    let (tree, error) = refused(&written);

    assert_malformed(&error, "name");
    assert!(
        error.to_string().contains("[[fixture.remote]] 2"),
        "{error}"
    );
    assert_eq!(entries(&tree.root().join("remotes")), [] as [&str; 0]);
}

#[test]
fn a_worktree_names_its_checkout_by_any_spelling_of_the_path() {
    let fixture = format!(
        "{REMOTE}\n[[fixture.checkout]]\nremote = \"alpha\"\npath = \"projects/x/../alpha\"\n\n\
         [[fixture.worktree]]\ncheckout = \"./projects/alpha\"\npath = \"one/alpha\"\nbranch = \"one\"\n\
         start = \"origin/main\"\n"
    );
    let written = Written::new(&fixture);

    let tree = build(&written.scenario()).expect("the fixture builds");

    let worktree = tree.workspace_dir().join("one/alpha");
    assert_eq!(
        git(&tree, &worktree, &["symbolic-ref", "HEAD"]),
        "refs/heads/one\n"
    );
    assert!(!tree.workspace_dir().join("projects/x").exists());
}

#[test]
fn a_checkout_path_that_leaves_the_workspace_directory_is_malformed_when_parsed() {
    let fixture =
        format!("{REMOTE}\n[[fixture.checkout]]\nremote = \"alpha\"\npath = \"../outside\"\n");
    let written = Written::new(&fixture);

    let malformed = written.load().expect_err("the description is malformed");

    // The builder takes a parsed scenario, and there is none: no tree is created for this directory.
    assert_eq!(malformed.scenario, ID);
    assert_eq!(malformed.key, "path");
}

#[test]
fn a_checkout_through_a_link_that_leads_out_of_the_tree_is_not_cloned() {
    let fixture =
        format!("{REMOTE}\n[[fixture.checkout]]\nremote = \"alpha\"\npath = \"out/alpha\"\n");
    let written = Written::new(&fixture);
    let outside = written.outside();
    let written = written.link("files/out", &outside);

    let (tree, error) = refused(&written);

    assert_malformed(&error, "path");
    assert_eq!(entries(&outside), [] as [&str; 0]);
    assert_eq!(
        std::fs::read_link(tree.workspace_dir().join("out")).expect("a link"),
        outside
    );
}

#[test]
fn a_git_call_in_a_directory_that_leads_out_of_the_tree_is_not_made() {
    let written = Written::new("[[fixture.git]]\ncwd = \"out\"\nargs = [\"init\", \"made\"]\n");
    let outside = written.outside();
    let written = written.link("files/out", &outside);

    let (_tree, error) = refused(&written);

    assert_malformed(&error, "cwd");
    assert_eq!(entries(&outside), [] as [&str; 0]);
}

#[test]
fn a_git_call_with_an_argument_that_leads_out_of_the_tree_is_not_made() {
    for args in [
        "[\"init\", \"out/made\"]",
        "[\"init\", \"--separate-git-dir=out/made\", \"in\"]",
        "[\"format-patch\", \"-oout\", \"HEAD\"]",
        "[\"format-patch\", \"-koout/made\", \"HEAD\"]",
    ] {
        let written = Written::new(&format!("[[fixture.git]]\ncwd = \".\"\nargs = {args}\n"));
        let outside = written.outside();
        let written = written.link("files/out", &outside);

        let (tree, error) = refused(&written);

        assert_malformed(&error, "args");
        assert!(error.to_string().contains(" reads as "), "{error}");
        assert_eq!(entries(&outside), [] as [&str; 0], "{args}");
        assert!(!tree.workspace_dir().join("in").exists(), "{args}");
    }
}

#[test]
fn a_git_call_that_names_a_repository_outside_the_fixture_is_not_made() {
    let written = Written::new(
        "[[fixture.git]]\ncwd = \".\"\nargs = [\"clone\", \"file:///tmp/x\", \"made\"]\n",
    );

    let (tree, error) = refused(&written);

    assert_malformed(&error, "args");
    assert_eq!(entries(&tree.workspace_dir()), [] as [&str; 0]);
}

#[test]
fn a_git_call_with_a_short_option_that_names_a_path_outside_the_tree_is_not_made() {
    let fixture = format!(
        "{REMOTE}\n[[fixture.git]]\ncwd = \".\"\nargs = [\"init\", \"-q\", \"made\"]\n\
         [[fixture.git]]\ncwd = \"made\"\nargs = [\"format-patch\", \"-o/tmp/x\", \"HEAD\"]\n"
    );
    let written = Written::new(&fixture);

    let (tree, error) = refused(&written);

    assert_malformed(&error, "args");
    assert_eq!(entries(&tree.workspace_dir()), [] as [&str; 0]);
    assert_eq!(entries(&tree.root().join("remotes")), [] as [&str; 0]);
}

#[test]
fn a_git_call_names_a_fixture_remote_by_the_address_the_tree_maps() {
    let fixture = format!(
        "{REMOTE}\n[[fixture.git]]\ncwd = \".\"\n\
         args = [\"clone\", \"--quiet\", \"https://fixture.invalid/alpha.git\", \"made\"]\n"
    );
    let written = Written::new(&fixture);
    let tree = TempTree::new().expect("an isolated tree");
    // The address the builder lets a call name is the one the tree maps to its own remotes: were it any
    // other, the call would leave the tree for the network.
    let mapped = std::fs::read_to_string(tree.root().join("gitconfig")).expect("the configuration");
    assert!(
        mapped.contains("insteadOf = https://fixture.invalid/\n"),
        "{mapped}"
    );

    build_in(&written.scenario(), &tree).expect("the fixture builds");

    let made = tree.workspace_dir().join("made");
    assert_eq!(
        git(&tree, &made, &["rev-parse", "HEAD"]),
        git(
            &tree,
            &tree.root().join("remotes/alpha.git"),
            &["rev-parse", "main"]
        )
    );
}

#[test]
fn a_git_call_in_a_repository_whose_git_directory_is_outside_the_tree_is_not_made() {
    let written = Written::new(
        "[[fixture.git]]\ncwd = \"sub\"\nargs = [\"commit\", \"--allow-empty\", \"-m\", \"escaped\"]\n",
    );
    let outside = written.outside().join("repository");
    let host_git = Git::new(&written.host).expect("git");
    host_git
        .init(&outside, "main")
        .expect("a repository outside the tree");
    let tree = TempTree::new().expect("an isolated tree");
    let sub = tree.workspace_dir().join("sub");
    std::fs::create_dir_all(&sub).expect("the directory");
    let pointer = format!("gitdir: {}\n", outside.join(".git").display());
    std::fs::write(sub.join(".git"), pointer).expect("the pointer out of the tree");

    let error = build_in(&written.scenario(), &tree).expect_err("the build fails");

    assert_malformed(&error, "cwd");
    host_git
        .run(&outside, &["rev-parse", "--verify", "--quiet", "HEAD"])
        .expect_err("the repository outside has no commit");
}

#[test]
fn a_git_call_under_a_git_entry_git_cannot_read_is_not_made() {
    let written = Written::new("[[fixture.git]]\ncwd = \"sub\"\nargs = [\"init\"]\n");
    let outside = written.outside();
    let tree = TempTree::new().expect("an isolated tree");
    let sub = tree.workspace_dir().join("sub");
    std::fs::create_dir_all(&sub).expect("the directory");
    let pointer = format!("gitdir: {}\n", outside.join("absent").display());
    std::fs::write(sub.join(".git"), pointer).expect("the pointer out of the tree");

    let error = build_in(&written.scenario(), &tree).expect_err("the build fails");

    assert_malformed(&error, "cwd");
    assert_eq!(entries(&outside), [] as [&str; 0]);
}

#[test]
fn a_name_that_git_would_read_as_an_option_fails_before_anything_is_built() {
    let worktree = |branch: &str, start: &str| {
        format!(
            "{REMOTE}\n[[fixture.checkout]]\nremote = \"alpha\"\npath = \"projects/alpha\"\n\n\
             [[fixture.worktree]]\ncheckout = \"projects/alpha\"\npath = \"one/alpha\"\n\
             branch = \"{branch}\"\nstart = \"{start}\"\n"
        )
    };
    let commit = |line: &str| {
        format!(
            "[[fixture.remote]]\nname = \"alpha\"\n[[fixture.remote.commit]]\nmessage = \"first\"\n{line}\n"
        )
    };
    for (fixture, key) in [
        (worktree("--detach", "origin/main"), "branch"),
        (worktree("one", "--force"), "start"),
        (worktree("one", "/tmp/x"), "start"),
        (commit("branch = \"-f\""), "branch"),
        (commit("tag = \"-d\""), "tag"),
        (
            "[[fixture.remote]]\nname = \"alpha\"\ndefault_branch = \"--bare\"\n".to_owned(),
            "default_branch",
        ),
    ] {
        let written = Written::new(&fixture);

        let (tree, error) = refused(&written);

        assert_malformed(&error, key);
        assert_eq!(
            entries(&tree.root().join("remotes")),
            [] as [&str; 0],
            "{key}"
        );
        assert_eq!(entries(&tree.root().join("tmp")), [] as [&str; 0], "{key}");
    }
}

#[test]
fn a_commit_file_in_the_git_directory_fails_before_anything_is_built() {
    for (line, key) in [
        (
            "files = { \".git/hooks/pre-commit\" = \"#!/bin/sh\\n\" }",
            "files",
        ),
        ("files = { \"a/../.GIT/config\" = \"\" }", "files"),
        ("delete = [\"sub/.git/HEAD\"]", "delete"),
    ] {
        let fixture = format!(
            "[[fixture.remote]]\nname = \"alpha\"\n[[fixture.remote.commit]]\nmessage = \"first\"\n{line}\n"
        );
        let written = Written::new(&fixture);

        let (tree, error) = refused(&written);

        assert_malformed(&error, key);
        assert_eq!(
            entries(&tree.root().join("remotes")),
            [] as [&str; 0],
            "{line}"
        );
    }
}

#[test]
fn a_copied_entry_that_lands_in_a_git_directory_is_not_written() {
    let fixture = "[fixture]\nworkspace_git = true\n";
    let planted = Written::new(fixture).file("files/.git/hooks/pre-commit", b"#!/bin/sh\n");
    let through_a_link = Written::new(fixture)
        .link("files/g", Path::new(".git"))
        .file("dirty/g/hooks/pre-commit", b"#!/bin/sh\n");

    let (first, planted) = refused(&planted);
    let (second, through_a_link) = refused(&through_a_link);

    assert_malformed(&planted, "files");
    assert_malformed(&through_a_link, "dirty");
    for tree in [&first, &second] {
        assert!(!tree.workspace_dir().join(".git/hooks").exists());
    }
}

#[test]
fn a_copied_file_through_a_link_that_leads_out_of_the_tree_is_not_written() {
    let written = Written::new("");
    let outside = written.outside();
    let written = written
        .link("files/out", &outside)
        .file("dirty/out/escaped.txt", b"escaped\n");

    let (_tree, error) = refused(&written);

    assert_malformed(&error, "dirty");
    assert_eq!(entries(&outside), [] as [&str; 0]);
}

#[test]
fn a_description_with_a_stub_is_refused_and_nothing_is_built() {
    let fixture = format!("{REMOTE}\n[[stub]]\nname = \"docker\"\nexit = 0\n");
    let written = Written::new(&fixture).file("files/.winter/config.toml", CONFIG.as_bytes());

    let (tree, error) = refused(&written);

    assert_malformed(&error, "stub");
    assert_eq!(entries(&tree.root().join("bin")), [] as [&str; 0]);
    assert_eq!(entries(&tree.workspace_dir()), [] as [&str; 0]);
    assert_eq!(entries(&tree.root().join("remotes")), [] as [&str; 0]);
    let error = build(&written.scenario()).expect_err("no tree is answered");
    assert_malformed(&error, "stub");
}

#[test]
fn a_copied_entry_that_lands_in_a_remote_of_the_fixture_is_not_written() {
    let written = Written::new(REMOTE)
        .link("files/r", Path::new("<ROOT>/remotes/alpha.git"))
        .file("dirty/r/hooks/pre-receive", b"#!/bin/sh\n");
    let beside = Written::new(REMOTE)
        .link("files/r", Path::new("<ROOT>/remotes"))
        .file("dirty/r/alpha.git/planted.txt", b"planted\n")
        .file("dirty/r/beside.txt", b"beside\n");

    let (first, through_a_link) = refused(&written);
    let (second, below_a_link) = refused(&beside);

    assert_malformed(&through_a_link, "dirty");
    assert_malformed(&below_a_link, "dirty");
    let remote = first.root().join("remotes/alpha.git");
    assert!(!remote.join("hooks/pre-receive").exists());
    assert!(!second.root().join("remotes/alpha.git/planted.txt").exists());
    assert_eq!(
        git(&first, &remote, &["rev-parse", "--is-bare-repository"]),
        "true\n"
    );
}

#[test]
fn a_checkout_or_a_worktree_in_a_git_directory_fails_before_anything_is_built() {
    let checkout = |path: &str| {
        format!(
            "[fixture]\nworkspace_git = true\n{REMOTE}\n[[fixture.checkout]]\nremote = \"alpha\"\n\
             path = \"{path}\"\n"
        )
    };
    let worktree = |path: &str| {
        format!(
            "{}\n[[fixture.worktree]]\ncheckout = \"projects/alpha\"\npath = \"{path}\"\n\
             branch = \"one\"\nstart = \"origin/main\"\n",
            checkout("projects/alpha")
        )
    };
    for (fixture, place) in [
        (checkout(".git/x"), "[[fixture.checkout]] 1"),
        (checkout("a/../.GIT"), "[[fixture.checkout]] 1"),
        (worktree("one/.git/alpha"), "[[fixture.worktree]] 1"),
    ] {
        let written = Written::new(&fixture);

        let (tree, error) = refused(&written);

        assert_malformed(&error, "path");
        assert!(error.to_string().contains(place), "{error}");
        assert_eq!(entries(&tree.workspace_dir()), [] as [&str; 0], "{place}");
        assert_eq!(entries(&tree.root().join("remotes")), [] as [&str; 0]);
    }
}

#[test]
fn a_checkout_through_a_link_into_a_git_directory_is_not_cloned() {
    let checkout = |path: &str| {
        format!(
            "[fixture]\nworkspace_git = true\n{REMOTE}\n[[fixture.checkout]]\nremote = \"alpha\"\n\
             path = \"{path}\"\n"
        )
    };
    let root_repository = Written::new(&checkout("g/x")).link("files/g", Path::new(".git"));
    let remote =
        Written::new(&checkout("r/x")).link("files/r", Path::new("<ROOT>/remotes/alpha.git"));

    let (first, into_the_root_repository) = refused(&root_repository);
    let (second, into_the_remote) = refused(&remote);

    assert_malformed(&into_the_root_repository, "path");
    assert_malformed(&into_the_remote, "path");
    assert!(!first.workspace_dir().join(".git/x").exists());
    assert!(!second.root().join("remotes/alpha.git/x").exists());
}

/// A remote, a checkout of it, and a worktree whose start point no repository has; a link that leads out
/// of the tree is copied before the worktree, and a file after it.
fn failing_at_the_worktree() -> Written {
    let fixture = format!(
        "{REMOTE}\n[[fixture.checkout]]\nremote = \"alpha\"\npath = \"projects/alpha\"\n\n\
         [[fixture.worktree]]\ncheckout = \"projects/alpha\"\npath = \"one/alpha\"\nbranch = \"one\"\n\
         start = \"origin/absent\"\n"
    );
    Written::new(&fixture).file("dirty/notes.txt", b"not committed\n")
}

/// A remote and a raw git call that clones a remote the fixture does not have, which git refuses naming
/// where the tree keeps its remotes.
fn failing_at_the_call() -> Written {
    let fixture = format!(
        "{REMOTE}\n[[fixture.git]]\ncwd = \".\"\n\
         args = [\"clone\", \"--quiet\", \"https://fixture.invalid/absent.git\", \"made\"]\n"
    );
    Written::new(&fixture).file("dirty/notes.txt", b"not committed\n")
}

/// The key and git's own words of `error`, a step of a build that git refused.
#[track_caller]
fn refused_by_git(error: &FixtureError) -> (&str, &str) {
    let FixtureError::Failed {
        scenario,
        key,
        error: source,
    } = error
    else {
        panic!("not a failed step: {error}");
    };
    assert_eq!(scenario, ID);
    let message = error.to_string();
    assert!(message.contains(ID), "{message}");
    assert!(message.contains(&format!("`{key}`")), "{message}");
    let GitError::Failed { stderr, .. } = source.as_ref() else {
        panic!("not refused by git: {source}");
    };
    (key, stderr)
}

#[test]
fn a_build_that_fails_midway_names_the_key_and_keeps_what_was_built_in_the_tree_it_was_given() {
    let written = failing_at_the_worktree();
    let outside = written.outside();
    let written = written.link("files/out", &outside);

    let (tree, error) = refused(&written);

    let (key, _) = refused_by_git(&error);
    assert_eq!(key, "start");
    let workspace = tree.workspace_dir();
    assert_eq!(entries(&workspace), ["out", "projects"]);
    assert_eq!(
        git(
            &tree,
            &workspace.join("projects/alpha"),
            &["log", "--format=%s"]
        ),
        "first\n"
    );
    assert_eq!(entries(&tree.root().join("remotes")), ["alpha.git"]);
    assert_eq!(entries(&outside), [] as [&str; 0]);

    let (tree, error) = refused(&failing_at_the_call());

    let (key, stderr) = refused_by_git(&error);
    assert_eq!(key, "args");
    let absent = tree.root().join("remotes/absent.git");
    assert!(stderr.contains(absent.to_str().expect("plain")), "{stderr}");
    assert_eq!(entries(&tree.workspace_dir()), [] as [&str; 0]);
}

#[test]
fn a_build_that_fails_midway_in_a_tree_of_its_own_removes_the_tree() {
    let written = failing_at_the_call();

    let error = build(&written.scenario()).expect_err("no tree is answered");

    // Git names the remote it did not find by where the tree kept its remotes, which is how the tree
    // that was made and is not answered is known here.
    let (key, stderr) = refused_by_git(&error);
    assert_eq!(key, "args");
    let named = "/remotes/absent.git";
    let end = stderr.find(named).expect("git names the remote");
    let start = stderr[..end].rfind('\'').expect("git quotes the path") + 1;
    let root = Path::new(&stderr[start..end]);
    assert!(root.is_absolute(), "{stderr}");
    assert!(!root.exists(), "{}", root.display());

    let error = build(&failing_at_the_worktree().scenario()).expect_err("no tree is answered");
    assert_eq!(refused_by_git(&error).0, "start");
}

#[test]
fn a_worktree_git_refuses_with_a_start_point_the_checkout_has_is_reported_at_the_checkout() {
    let fixture = format!(
        "{REMOTE}\n[[fixture.checkout]]\nremote = \"alpha\"\npath = \"projects/alpha\"\n\n\
         [[fixture.worktree]]\ncheckout = \"projects/alpha\"\npath = \"projects/alpha\"\nbranch = \"one\"\n\
         start = \"origin/main\"\n"
    );
    let written = Written::new(&fixture).file("dirty/notes.txt", b"not committed\n");

    let (tree, error) = refused(&written);

    assert_eq!(refused_by_git(&error).0, "checkout");
    assert_eq!(entries(&tree.workspace_dir()), ["projects"]);
}
