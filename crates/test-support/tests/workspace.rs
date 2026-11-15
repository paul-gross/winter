//! The workspace builder: what a built workspace holds, and where the root of its tree may be written.

use std::path::{Path, PathBuf};

use winter_workspace_test_support::{CommitSpec, Git, GitError, TempTree, WorkspaceBuilder};

const CONFIG: &str =
    "# kept as written\n[projects.alpha]\nurl = \"https://fixture.invalid/alpha.git\"\n\n";

/// A commit that writes `content` to `file`.
fn change(message: &str, file: &str, content: &str) -> CommitSpec {
    CommitSpec {
        message: message.to_owned(),
        files: vec![(file.to_owned(), content.as_bytes().to_vec())],
        ..CommitSpec::default()
    }
}

/// A builder of a workspace with the remote `alpha`, its project, and that project's worktree in the env
/// `feature-one`.
fn one_of_each(tree: &TempTree) -> WorkspaceBuilder<'_> {
    WorkspaceBuilder::new(tree)
        .config(CONFIG)
        .remote(
            "alpha",
            vec![
                change("first", "README.md", "one\n"),
                change("second", "README.md", "two\n"),
            ],
        )
        .project("alpha")
        .env_worktree("feature-one", "alpha")
}

/// Every file under `directory` that is outside object storage, links not followed.
fn files_outside_object_storage(directory: &Path, found: &mut Vec<PathBuf>) {
    for entry in std::fs::read_dir(directory).expect("a directory") {
        let entry = entry.expect("an entry");
        let kind = entry.file_type().expect("the kind of the entry");
        if kind.is_dir() {
            if entry.file_name() != "objects" {
                files_outside_object_storage(&entry.path(), found);
            }
        } else if kind.is_file() {
            found.push(entry.path());
        }
    }
}

#[test]
fn a_workspace_has_its_project_as_a_clone_and_its_env_worktree_on_the_env_branch() {
    let tree = TempTree::new().expect("an isolated tree");

    let workspace = one_of_each(&tree).build().expect("a workspace");

    assert_eq!(workspace.root, tree.workspace_dir());
    assert!(workspace.root.join("projects/alpha/.git").is_dir());
    assert!(workspace.root.join("feature-one/alpha/.git").is_file());
    assert!(tree.root().join("remotes/alpha.git").is_dir());
    assert!(!workspace.root.join(".git").exists());
    let git = Git::new(&tree).expect("git");
    let worktree = workspace.root.join("feature-one/alpha");
    assert_eq!(
        git.run(&worktree, &["symbolic-ref", "HEAD"])
            .expect("the head"),
        "refs/heads/feature-one\n"
    );
    assert_eq!(
        git.run(&worktree, &["rev-parse", "HEAD"])
            .expect("the head"),
        git.run(
            &workspace.root.join("projects/alpha"),
            &["rev-parse", "refs/remotes/origin/main"]
        )
        .expect("the start")
    );
    assert_eq!(
        std::fs::read(worktree.join("README.md")).expect("the file"),
        b"two\n"
    );
    assert_eq!(
        std::fs::read(workspace.root.join(".winter/config.toml")).expect("the configuration"),
        CONFIG.as_bytes()
    );
}

#[test]
fn every_file_is_written_as_given() {
    let tree = TempTree::new().expect("an isolated tree");
    let bytes: &[u8] = &[0, 159, 146, 150, b'\r', b'\n'];

    let workspace = WorkspaceBuilder::new(&tree)
        .config("config = 1\n")
        .local_config("local = 1\n")
        .state("state = 1\n")
        .lock("# lock\n")
        .file("notes/deep/raw.bin", bytes)
        .file("replaced.txt", b"first")
        .file("replaced.txt", b"second")
        .build()
        .expect("a workspace");

    let read = |relative: &str| std::fs::read(workspace.root.join(relative)).expect("the file");
    assert_eq!(read(".winter/config.toml"), b"config = 1\n");
    assert_eq!(read(".winter/config.local.toml"), b"local = 1\n");
    assert_eq!(read(".winter/state.toml"), b"state = 1\n");
    assert_eq!(read(".winter/config.lock"), b"# lock\n");
    assert_eq!(read("notes/deep/raw.bin"), bytes);
    assert_eq!(read("replaced.txt"), b"second");
    assert!(!workspace.root.join(".git").exists());
    assert!(!workspace.root.join("projects").exists());
}

#[test]
fn the_root_repository_has_one_commit_of_everything_its_gitignore_does_not_exclude() {
    let tree = TempTree::new().expect("an isolated tree");
    let gitignore = "/projects/\n/feature-one/\n.winter/state.toml\n";

    let workspace = one_of_each(&tree)
        .state("state = 1\n")
        .file("AGENTS.md", b"# Workspace\n")
        .root_repository(gitignore)
        .build()
        .expect("a workspace");

    let git = Git::new(&tree).expect("git");
    assert!(workspace.root.join(".git").is_dir());
    assert_eq!(
        git.run(&workspace.root, &["rev-list", "--count", "--all"])
            .expect("the count"),
        "1\n"
    );
    assert_eq!(
        git.run(&workspace.root, &["status", "--porcelain"])
            .expect("the status"),
        ""
    );
    assert_eq!(
        git.run(&workspace.root, &["ls-tree", "-r", "--name-only", "HEAD"])
            .expect("the files"),
        ".gitignore\n.winter/config.toml\nAGENTS.md\n"
    );
    assert_eq!(
        std::fs::read_to_string(workspace.root.join(".gitignore")).expect("the file"),
        gitignore
    );
    // The project keeps its own repository: git run in it does not answer for the workspace root.
    assert_eq!(
        git.run(
            &workspace.root.join("projects/alpha"),
            &["rev-list", "--count", "HEAD"]
        )
        .expect("the count"),
        "2\n"
    );
}

#[test]
fn the_same_workspace_built_in_two_trees_has_the_same_object_ids() {
    let built: Vec<String> = (0..2)
        .map(|_| {
            let tree = TempTree::new().expect("an isolated tree");
            let workspace = one_of_each(&tree)
                .root_repository("/projects/\n/feature-one/\n")
                .build()
                .expect("a workspace");
            let git = Git::new(&tree).expect("git");
            let format = "--format=%(refname) %(objectname)";
            [
                workspace.root.clone(),
                workspace.root.join("projects/alpha"),
                tree.root().join("remotes/alpha.git"),
            ]
            .iter()
            .map(|repo| git.run(repo, &["for-each-ref", format]).expect("the refs"))
            .collect()
        })
        .collect();

    assert!(built[0].contains("refs/heads/feature-one"), "{}", built[0]);
    assert_eq!(built[0], built[1]);
}

#[test]
fn the_root_of_the_tree_is_written_only_to_worktree_pointers_and_the_record_of_a_fetch() {
    let tree = TempTree::new().expect("an isolated tree");
    let workspace = one_of_each(&tree)
        .remote("beta", vec![change("first", "b.txt", "b\n")])
        .project("beta")
        .root_repository("/projects/\n/feature-one/\n")
        .build()
        .expect("a workspace");
    let git = Git::new(&tree).expect("git");
    git.push_to_remote("alpha", &change("pushed", "pushed.txt", "pushed\n"))
        .expect("a pushed commit");
    git.run(
        &workspace.root.join("projects/alpha"),
        &["fetch", "--quiet"],
    )
    .expect("a fetch");
    git.run(
        &workspace.root.join("feature-one/alpha"),
        &["pull", "--quiet", "origin", "main"],
    )
    .expect("a pull");
    let root = tree
        .root()
        .to_str()
        .expect("a root that is text")
        .as_bytes();

    let mut files = Vec::new();
    files_outside_object_storage(&workspace.root, &mut files);
    files_outside_object_storage(&tree.root().join("remotes"), &mut files);
    let mut holders: Vec<String> = files
        .iter()
        .filter(|file| {
            let content = std::fs::read(file).expect("the content");
            content.windows(root.len()).any(|window| window == root)
        })
        .map(|file| {
            let relative = file.strip_prefix(tree.root()).expect("a file of the tree");
            relative.to_str().expect("a path that is text").to_owned()
        })
        .collect();
    holders.sort();

    assert!(files.len() > 20, "{files:?}");
    let pointers = [
        "ws/feature-one/alpha/.git",
        "ws/projects/alpha/.git/worktrees/alpha/gitdir",
    ];
    let allowed =
        |holder: &String| pointers.contains(&holder.as_str()) || holder.ends_with("/FETCH_HEAD");
    assert!(holders.iter().all(allowed), "{holders:?}");
    for pointer in pointers {
        assert!(
            holders.iter().any(|holder| holder == pointer),
            "{holders:?}"
        );
    }
}

#[test]
fn a_project_without_its_remote_fails_on_the_path_its_url_is_mapped_to() {
    let tree = TempTree::new().expect("an isolated tree");

    let error = WorkspaceBuilder::new(&tree)
        .config(CONFIG)
        .project("absent")
        .build()
        .expect_err("no remote to clone");

    let GitError::Failed { args, stderr, .. } = &error else {
        panic!("{error:?}");
    };
    assert_eq!(args[0], "clone");
    assert!(args.contains(&"https://fixture.invalid/absent.git".to_owned()));
    let mapped = tree.root().join("remotes/absent.git");
    assert!(
        stderr.contains(mapped.to_str().expect("a path that is text")),
        "{stderr}"
    );
    assert!(!stderr.contains("fixture.invalid"), "{stderr}");
    assert!(!tree.workspace_dir().join("projects/absent/.git").exists());
}

#[test]
fn a_file_that_would_leave_the_workspace_directory_is_refused() {
    let tree = TempTree::new().expect("an isolated tree");

    let error = WorkspaceBuilder::new(&tree)
        .file("../home/escaped.txt", b"x")
        .build()
        .expect_err("a path that leads upward");

    assert!(matches!(error, GitError::Unconfined { .. }), "{error:?}");
    assert!(!tree.root().join("home/escaped.txt").exists());
}

#[test]
fn a_file_behind_a_link_that_leads_out_of_the_tree_is_refused() {
    let tree = TempTree::new().expect("an isolated tree");
    let other = TempTree::new().expect("a second tree");
    let outside = other.workspace_dir();
    std::os::unix::fs::symlink(&outside, tree.workspace_dir().join("notes"))
        .expect("a symbolic link");

    let error = WorkspaceBuilder::new(&tree)
        .file("notes/escaped.txt", b"x")
        .build()
        .expect_err("a path that a link leads out of the tree");

    assert!(matches!(error, GitError::Unconfined { .. }), "{error:?}");
    assert_eq!(
        std::fs::read_dir(&outside).expect("the directory").count(),
        0
    );
}

#[test]
fn a_file_of_the_builder_neither_replaces_an_entry_of_the_root_of_the_tree_nor_adds_one() {
    let tree = TempTree::new().expect("an isolated tree");
    let configuration = tree.root().join("gitconfig");
    let before = std::fs::read(&configuration).expect("the git configuration");
    // The workspace directory as a link to the root: a file of the workspace would be an entry of the root,
    // or lie under one made for it.
    std::fs::remove_dir(tree.workspace_dir()).expect("no workspace directory");
    std::os::unix::fs::symlink(tree.root(), tree.workspace_dir()).expect("a symbolic link");

    let refused = [
        WorkspaceBuilder::new(&tree).file("gitconfig", b"").build(),
        WorkspaceBuilder::new(&tree)
            .file("planted.txt", b"x")
            .build(),
        WorkspaceBuilder::new(&tree).config(CONFIG).build(),
        WorkspaceBuilder::new(&tree).file(".git/HEAD", b"x").build(),
    ];

    for result in &refused {
        assert!(
            matches!(result, Err(GitError::Unconfined { .. })),
            "{result:?}"
        );
    }
    assert_eq!(
        std::fs::read(&configuration).expect("the git configuration"),
        before
    );
    assert!(!tree.root().join("planted.txt").exists());
    assert!(!tree.root().join(".winter").exists());
    assert!(!tree.root().join(".git").exists());
}

#[test]
fn a_file_of_the_builder_is_a_new_file_and_a_hard_link_carries_nothing_to_the_git_configuration() {
    let tree = TempTree::new().expect("an isolated tree");
    let configuration = tree.root().join("gitconfig");
    let before = std::fs::read(&configuration).expect("the git configuration");
    let linked = tree.workspace_dir().join("linked.txt");
    std::fs::hard_link(&configuration, &linked).expect("a hard link");

    WorkspaceBuilder::new(&tree)
        .file("linked.txt", b"")
        .build()
        .expect("a workspace");

    assert_eq!(std::fs::read(&linked).expect("the file"), b"");
    assert_eq!(
        std::fs::read(&configuration).expect("the git configuration"),
        before
    );
}

#[test]
fn the_root_repository_records_a_project_it_does_not_exclude_as_the_commit_its_head_is_on() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");

    let workspace = one_of_each(&tree)
        .root_repository("")
        .build()
        .expect("a workspace");

    let head = |repository: &str| {
        let id = git
            .run(&workspace.root.join(repository), &["rev-parse", "HEAD"])
            .expect("the head");
        id.trim_end().to_owned()
    };
    let recorded = git
        .run(&workspace.root, &["ls-tree", "-r", "HEAD"])
        .expect("the tree of the commit");
    let entries: Vec<&str> = recorded.lines().collect();
    let project = format!("160000 commit {}\tprojects/alpha", head("projects/alpha"));
    let worktree = format!(
        "160000 commit {}\tfeature-one/alpha",
        head("feature-one/alpha")
    );
    assert!(entries.contains(&project.as_str()), "{recorded}");
    assert!(entries.contains(&worktree.as_str()), "{recorded}");
    assert!(
        !entries.iter().any(|entry| entry.contains("alpha/")),
        "{recorded}"
    );
}
