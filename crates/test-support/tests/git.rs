//! The git fixture helpers: deterministic commits, bare remotes, and clones that reach them without a network.

use std::collections::{BTreeMap, BTreeSet};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use winter_workspace_test_support::{CommitSpec, Exit, Git, GitError, TempTree};

/// 2000-01-01T00:00:00+0000 in seconds since the epoch.
const FIRST_COMMIT: u64 = 946_684_800;

/// The commits of `main` in the remote `pinned`, oldest first, as every host builds them. They follow from the
/// commits of [`pinned`], the dates of the first three commits of a tree, the formats repositories are
/// created with, and the identities of the pinned environment, and from nothing of the host.
const PINNED: [&str; 3] = [
    "ca20c6984a4637a357461f873d0e29f87e31d9ae",
    "555f73ef22ffbc6057414992d83222c4dd55638a",
    "a1bed5349d40d0c6ea3b36b1442f5fad6d530554",
];

/// A commit that writes `content` to `file`.
fn change(message: &str, file: &str, content: &str) -> CommitSpec {
    CommitSpec {
        message: message.to_owned(),
        files: vec![(file.to_owned(), content.as_bytes().to_vec())],
        ..CommitSpec::default()
    }
}

fn three_commits() -> Vec<CommitSpec> {
    vec![
        change("first", "README.md", "one\n"),
        change("second", "src/lib.txt", "two\n"),
        change("third", "README.md", "three\n"),
    ]
}

/// The three commits of the remote `pinned`.
fn pinned() -> Vec<CommitSpec> {
    vec![
        change("one", "a.txt", "one\n"),
        change("two", "b.txt", "two\n"),
        CommitSpec {
            message: "three".to_owned(),
            delete: vec!["a.txt".to_owned()],
            tag: Some("v1".to_owned()),
            ..CommitSpec::default()
        },
    ]
}

/// The paths of the entries of `directory`.
fn entries(directory: &Path) -> Vec<PathBuf> {
    std::fs::read_dir(directory)
        .expect("the directory")
        .map(|entry| entry.expect("an entry").path())
        .collect()
}

/// The names of the entries of the root of `tree`, sorted, and the bytes of its git configuration.
fn own_entries(tree: &TempTree) -> (BTreeSet<PathBuf>, Vec<u8>) {
    let names = entries(tree.root()).into_iter().collect();
    let configuration =
        std::fs::read(tree.root().join("gitconfig")).expect("the git configuration");
    (names, configuration)
}

/// Everything under `directory`: each path with what is there, the bytes of a file and the target of a link.
fn everything_under(directory: &Path) -> BTreeMap<PathBuf, Option<Vec<u8>>> {
    let mut found = BTreeMap::new();
    let mut pending = vec![directory.to_path_buf()];
    while let Some(directory) = pending.pop() {
        for path in entries(&directory) {
            let kind = path.symlink_metadata().expect("the entry").file_type();
            let content = if kind.is_symlink() {
                let target = std::fs::read_link(&path).expect("the target of the link");
                Some(target.into_os_string().into_encoded_bytes())
            } else if kind.is_dir() {
                pending.push(path.clone());
                None
            } else {
                Some(std::fs::read(&path).expect("the file"))
            };
            found.insert(path, content);
        }
    }
    found
}

/// Everything under the root of `other`, and the bytes of the git configuration of `tree`.
type BothTrees = (BTreeMap<PathBuf, Option<Vec<u8>>>, Vec<u8>);

fn both_trees(tree: &TempTree, other: &TempTree) -> BothTrees {
    let configuration =
        std::fs::read(tree.root().join("gitconfig")).expect("the git configuration");
    (everything_under(other.root()), configuration)
}

/// Fails unless every one of `results` is the refusal of a path.
fn all_unconfined<T: std::fmt::Debug>(results: &[Result<T, GitError>]) {
    for result in results {
        assert!(
            matches!(result, Err(GitError::Unconfined { .. })),
            "{result:?}"
        );
    }
}

/// Commits on `main` and `topic`, the first of them tagged `v1`, ending back on `main`.
fn two_branches_and_a_tag() -> Vec<CommitSpec> {
    vec![
        CommitSpec {
            tag: Some("v1".to_owned()),
            ..change("first", "README.md", "one\n")
        },
        CommitSpec {
            branch: Some("topic".to_owned()),
            ..change("on topic", "topic.txt", "topic\n")
        },
        CommitSpec {
            branch: Some("main".to_owned()),
            ..change("second on main", "README.md", "two\n")
        },
    ]
}

/// The lines of what git printed.
fn lines(output: &str) -> Vec<String> {
    output.lines().map(str::to_owned).collect()
}

/// The object ids of the commits of `branch` in the repository at `repo`, oldest first.
fn history(git: &Git, repo: &Path, branch: &str) -> Vec<String> {
    lines(
        &git.run(repo, &["rev-list", "--reverse", branch])
            .expect("the history"),
    )
}

/// The committer timestamps of every commit the repository at `repo` reaches from a ref.
fn committer_dates(git: &Git, repo: &Path) -> Vec<u64> {
    git.run(repo, &["log", "--all", "--format=%ct"])
        .expect("the dates")
        .lines()
        .map(|line| line.parse().expect("a timestamp"))
        .collect()
}

/// The entries left in the `tmp` directory of `tree`.
fn scratch_entries(tree: &TempTree) -> Vec<PathBuf> {
    std::fs::read_dir(tree.root().join("tmp"))
        .expect("the tmp directory")
        .map(|entry| entry.expect("an entry").path())
        .collect()
}

#[test]
fn the_same_remote_built_in_two_trees_has_the_same_object_ids() {
    let built: Vec<Vec<String>> = (0..2)
        .map(|_| {
            let tree = TempTree::new().expect("an isolated tree");
            let git = Git::new(&tree).expect("git");
            let remote = git
                .bare_remote("alpha", "main", &three_commits())
                .expect("a remote");
            history(&git, &remote, "main")
        })
        .collect();

    assert_eq!(built[0].len(), 3, "{built:?}");
    assert_eq!(built[0], built[1]);
}

#[test]
fn the_pinned_remote_has_the_object_ids_every_host_gives_it() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");

    let remote = git
        .bare_remote("pinned", "main", &pinned())
        .expect("a remote");

    assert_eq!(history(&git, &remote, "main"), PINNED);
    let config = git
        .run(&remote, &["config", "--local", "--list"])
        .expect("the configuration");
    assert!(!config.is_empty());
    assert!(!config.contains("extensions."), "{config}");
}

#[test]
fn the_default_formats_of_another_git_reach_no_repository() {
    let tree = TempTree::new().expect("an isolated tree");
    // The tree's own git configuration stands in for a git that was built with other defaults.
    let configuration = tree.root().join("gitconfig");
    let mut text = std::fs::read_to_string(&configuration).expect("the git configuration");
    text.push_str("[init]\n\tdefaultObjectFormat = sha256\n\tdefaultRefFormat = reftable\n");
    std::fs::write(&configuration, text).expect("the git configuration");
    let git = Git::new(&tree).expect("git");
    let local = |repo: &Path| {
        git.run(repo, &["config", "--local", "--list"])
            .expect("the configuration")
    };

    let remote = git
        .bare_remote("pinned", "main", &pinned())
        .expect("a remote");
    let clone = tree.workspace_dir().join("clone");
    git.clone_remote("pinned", &clone).expect("a clone");
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "main").expect("a repository");
    git.run(&tree.workspace_dir(), &["init", "--quiet", "unnamed"])
        .expect("a repository with the formats left to git");

    assert_eq!(history(&git, &remote, "main"), PINNED);
    for repository in [&remote, &clone, &repo] {
        assert!(!local(repository).contains("extensions."));
    }
    // The defaults were in force: a repository that names no format has them.
    let unnamed = local(&tree.workspace_dir().join("unnamed"));
    assert!(
        unnamed.contains("extensions.objectformat=sha256"),
        "{unnamed}"
    );
    assert!(
        unnamed.contains("extensions.refstorage=reftable"),
        "{unnamed}"
    );
}

#[test]
fn a_clone_and_a_worktree_name_no_format_in_their_configuration() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    git.bare_remote("pinned", "main", &pinned())
        .expect("a remote");
    let clone = tree.workspace_dir().join("clone");
    git.clone_remote("pinned", &clone).expect("a clone");
    let worktree = tree.workspace_dir().join("worktree");
    git.add_worktree(&clone, &worktree, "side", "origin/main")
        .expect("a worktree");

    for repo in [&clone, &worktree] {
        let config = git
            .run(repo, &["config", "--local", "--list"])
            .expect("the configuration");
        assert!(config.contains("remote.origin.url="), "{config}");
        assert!(!config.contains("extensions."), "{config}");
        assert_eq!(history(&git, repo, "HEAD"), PINNED);
    }
}

#[test]
fn a_commit_git_refuses_consumes_no_date() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "main").expect("a repository");

    let refused = git.commit(&repo, &change("", "a.txt", "a\n"));
    git.commit(&repo, &change("first", "b.txt", "b\n"))
        .expect("a commit");

    assert!(
        matches!(&refused, Err(GitError::Failed { args, .. }) if args[0] == "commit"),
        "{refused:?}"
    );
    assert_eq!(
        git.run(
            &repo,
            &["log", "--format=%cd", "--date=format:%Y-%m-%dT%H:%M:%S%z"]
        )
        .expect("the dates"),
        "2000-01-01T00:00:00+0000\n"
    );
}

#[test]
fn the_commits_of_two_trees_are_counted_apart() {
    let first = TempTree::new().expect("an isolated tree");
    let second = TempTree::new().expect("a second tree");
    let dates: Vec<Vec<u64>> = [&first, &second, &first]
        .iter()
        .enumerate()
        .map(|(number, tree)| {
            let git = Git::new(tree).expect("git");
            let repo = tree.workspace_dir().join(format!("repo-{number}"));
            git.init(&repo, "main").expect("a repository");
            git.commit(&repo, &change("first", "a.txt", "a\n"))
                .expect("a commit");
            committer_dates(&git, &repo)
        })
        .collect();

    assert_eq!(dates, [[FIRST_COMMIT], [FIRST_COMMIT], [FIRST_COMMIT + 60]]);
}

#[test]
fn commits_made_at_once_in_one_tree_share_no_date() {
    let tree = TempTree::new().expect("an isolated tree");
    let repos: Vec<PathBuf> = (0..4)
        .map(|number| tree.workspace_dir().join(format!("repo-{number}")))
        .collect();

    let mut dates: Vec<u64> = std::thread::scope(|scope| {
        let threads: Vec<_> = repos
            .iter()
            .map(|repo| {
                let tree = &tree;
                scope.spawn(move || {
                    let git = Git::new(tree).expect("git");
                    git.init(repo, "main").expect("a repository");
                    for message in ["first", "second", "third"] {
                        git.commit(repo, &change(message, "a.txt", message))
                            .expect("a commit");
                    }
                    committer_dates(&git, repo)
                })
            })
            .collect();
        threads
            .into_iter()
            .flat_map(|thread| thread.join().expect("a thread that ended"))
            .collect()
    });

    dates.sort_unstable();
    let expected: Vec<u64> = (0..12).map(|n| FIRST_COMMIT + n * 60).collect();
    assert_eq!(dates, expected);
}

#[test]
fn a_push_on_a_branch_the_remote_lacks_adds_the_branch_from_the_default_branch() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    let remote = git
        .bare_remote("alpha", "main", &two_branches_and_a_tag())
        .expect("a remote");
    let tip = |branch: &str| {
        git.run(&remote, &["rev-parse", &format!("refs/heads/{branch}")])
            .expect("the tip")
            .trim_end()
            .to_owned()
    };
    let default_before = tip("main");
    let topic_before = tip("topic");

    let pushed = git
        .push_to_remote(
            "alpha",
            &CommitSpec {
                branch: Some("fresh".to_owned()),
                ..change("on a new branch", "fresh.txt", "fresh\n")
            },
        )
        .expect("a pushed commit");

    assert_eq!(tip("fresh"), pushed);
    assert_eq!(
        git.run(&remote, &["log", "-1", "--format=%P", &pushed])
            .expect("the parents"),
        format!("{default_before}\n")
    );
    assert_eq!(tip("main"), default_before);
    assert_eq!(tip("topic"), topic_before);

    let on_default = git
        .push_to_remote("alpha", &change("on the default branch", "more.txt", "m\n"))
        .expect("a pushed commit");

    assert_eq!(tip("main"), on_default);
    assert_ne!(on_default, default_before);
    assert_eq!(
        git.run(&remote, &["log", "-1", "--format=%P", &on_default])
            .expect("the parents"),
        format!("{default_before}\n")
    );
    assert_eq!(tip("fresh"), pushed);
    assert_eq!(scratch_entries(&tree), Vec::<PathBuf>::new());
}

#[test]
fn commit_answers_the_object_id_of_the_commit_it_made() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "trunk").expect("a repository");

    let id = git
        .commit(&repo, &change("first", "a.txt", "a\n"))
        .expect("a commit");

    assert_eq!(id.len(), 40, "{id}");
    assert_eq!(history(&git, &repo, "trunk"), [id]);
    assert_eq!(
        git.run(&repo, &["log", "-1", "--format=%an <%ae>|%cn <%ce>|%s"])
            .expect("the log"),
        "Fixture Author <author@fixture.invalid>|Fixture Committer <committer@fixture.invalid>|first\n"
    );
}

#[test]
fn no_two_commits_of_one_tree_share_a_committer_timestamp() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    let remote = git
        .bare_remote("alpha", "main", &two_branches_and_a_tag())
        .expect("a remote");
    git.bare_remote("beta", "main", &three_commits())
        .expect("a second remote");
    git.push_to_remote("alpha", &change("pushed", "pushed.txt", "pushed\n"))
        .expect("a pushed commit");
    let local = tree.workspace_dir().join("local");
    git.init(&local, "main").expect("a repository");
    // A second value for the same tree counts on from the first.
    let again = Git::new(&tree).expect("git");
    again
        .commit(&local, &change("local", "local.txt", "local\n"))
        .expect("a local commit");
    again
        .commit(
            &local,
            &CommitSpec {
                message: "nothing changed".to_owned(),
                ..CommitSpec::default()
            },
        )
        .expect("an empty commit");

    let mut dates = committer_dates(&git, &remote);
    dates.extend(committer_dates(&git, &tree.root().join("remotes/beta.git")));
    dates.extend(committer_dates(&git, &local));

    let distinct: BTreeSet<u64> = dates.iter().copied().collect();
    assert_eq!(dates.len(), 9, "{dates:?}");
    assert_eq!(distinct.len(), dates.len(), "{dates:?}");
    let expected: BTreeSet<u64> = (0..9).map(|n| FIRST_COMMIT + n * 60).collect();
    assert_eq!(distinct, expected);
}

#[test]
fn the_author_date_of_a_commit_is_its_committer_date() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "main").expect("a repository");
    git.commit(&repo, &change("first", "a.txt", "a\n"))
        .expect("a commit");
    git.commit(&repo, &change("second", "a.txt", "b\n"))
        .expect("a commit");

    let dates = git
        .run(&repo, &["log", "--format=%at %ct %ai"])
        .expect("the dates");

    assert_eq!(
        dates,
        "946684860 946684860 2000-01-01 00:01:00 +0000\n\
         946684800 946684800 2000-01-01 00:00:00 +0000\n"
    );
}

#[test]
fn a_clone_names_the_fixture_url_and_fetches_what_was_pushed_without_a_network() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    let remote = git
        .bare_remote("alpha", "main", &three_commits())
        .expect("a remote");
    let clone = tree.workspace_dir().join("projects/alpha");
    git.clone_remote("alpha", &clone).expect("a clone");

    assert_eq!(
        git.run(&clone, &["config", "--get", "remote.origin.url"])
            .expect("the URL"),
        "https://fixture.invalid/alpha.git\n"
    );
    assert_eq!(
        history(&git, &clone, "main"),
        history(&git, &remote, "main")
    );

    let pushed = git
        .push_to_remote("alpha", &change("pushed", "pushed.txt", "pushed\n"))
        .expect("a pushed commit");
    let before = git
        .run(&clone, &["rev-parse", "refs/remotes/origin/main"])
        .expect("the ref");
    assert_ne!(before.trim_end(), pushed);

    git.run(&clone, &["fetch", "--quiet"]).expect("a fetch");

    let fetched = git
        .run(&clone, &["rev-parse", "refs/remotes/origin/main"])
        .expect("the fetched ref");
    let theirs = git
        .run(&remote, &["rev-parse", "refs/heads/main"])
        .expect("the remote's ref");
    assert_eq!(fetched, theirs);
    assert_eq!(fetched.trim_end(), pushed);
    assert_eq!(scratch_entries(&tree), Vec::<PathBuf>::new());
}

#[test]
fn a_bare_remote_has_its_branches_its_tag_its_head_and_no_remote_of_its_own() {
    let built: Vec<(Vec<String>, String, String)> = (0..2)
        .map(|_| {
            let tree = TempTree::new().expect("an isolated tree");
            let git = Git::new(&tree).expect("git");
            let remote = git
                .bare_remote("alpha", "main", &two_branches_and_a_tag())
                .expect("a remote");

            assert_eq!(remote, tree.root().join("remotes/alpha.git"));
            assert_eq!(
                git.run(&remote, &["rev-parse", "--is-bare-repository"])
                    .expect("whether it is bare"),
                "true\n"
            );
            assert_eq!(
                git.run(&remote, &["symbolic-ref", "HEAD"])
                    .expect("the head"),
                "refs/heads/main\n"
            );
            assert_eq!(history(&git, &remote, "main").len(), 2);
            assert_eq!(history(&git, &remote, "topic").len(), 2);
            assert_eq!(
                git.run(&remote, &["rev-parse", "v1"]).expect("the tag"),
                git.run(&remote, &["rev-parse", "main~1"])
                    .expect("the first commit")
            );
            assert_eq!(scratch_entries(&tree), Vec::<PathBuf>::new());

            let refs = lines(
                &git.run(&remote, &["for-each-ref", "--format=%(refname)"])
                    .expect("the refs"),
            );
            let config = git
                .run(&remote, &["config", "--local", "--list"])
                .expect("the configuration");

            git.push_to_remote(
                "alpha",
                &CommitSpec {
                    branch: Some("topic".to_owned()),
                    tag: Some("v2".to_owned()),
                    ..change("pushed", "pushed.txt", "pushed\n")
                },
            )
            .expect("a pushed commit");
            let config_after_push = git
                .run(&remote, &["config", "--local", "--list"])
                .expect("the configuration");
            assert_eq!(history(&git, &remote, "topic").len(), 3);
            assert_eq!(
                git.run(&remote, &["rev-parse", "v2"]).expect("the tag"),
                git.run(&remote, &["rev-parse", "topic"])
                    .expect("the branch")
            );

            (refs, config, config_after_push)
        })
        .collect();

    let (refs, config, config_after_push) = &built[0];
    assert_eq!(
        refs,
        &["refs/heads/main", "refs/heads/topic", "refs/tags/v1"]
    );
    assert!(!config.is_empty());
    assert!(!config.contains("remote."), "{config}");
    assert!(
        !config_after_push.contains("remote."),
        "{config_after_push}"
    );
    assert_eq!(config, config_after_push);
    assert_eq!(built[0], built[1]);
}

#[test]
fn a_remote_without_commits_is_an_empty_bare_repository() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");

    let remote = git.bare_remote("empty", "main", &[]).expect("a remote");

    assert_eq!(git.run(&remote, &["for-each-ref"]).expect("the refs"), "");
    assert_eq!(
        git.run(&remote, &["symbolic-ref", "HEAD"])
            .expect("the head"),
        "refs/heads/main\n"
    );
}

#[test]
fn a_commit_replaces_and_deletes_files_and_tags_the_new_commit() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "main").expect("a repository");
    git.commit(&repo, &change("first", "a.txt", "a\n"))
        .expect("a commit");
    git.commit(&repo, &change("second", "nested/b.txt", "b\n"))
        .expect("a commit");

    let id = git
        .commit(
            &repo,
            &CommitSpec {
                message: "third".to_owned(),
                files: vec![("nested/b.txt".to_owned(), b"changed\n".to_vec())],
                delete: vec!["a.txt".to_owned()],
                branch: None,
                tag: Some("marked".to_owned()),
            },
        )
        .expect("a commit");

    assert_eq!(
        git.run(&repo, &["ls-tree", "-r", "--name-only", "HEAD"])
            .expect("the files"),
        "nested/b.txt\n"
    );
    assert_eq!(
        git.run(&repo, &["show", "HEAD:nested/b.txt"])
            .expect("the file"),
        "changed\n"
    );
    assert_eq!(
        git.run(&repo, &["rev-parse", "refs/tags/marked"])
            .expect("the tag")
            .trim_end(),
        id
    );
    assert_eq!(
        git.run(&repo, &["cat-file", "-t", "refs/tags/marked"])
            .expect("the kind of the tag"),
        "commit\n"
    );
    assert_eq!(
        git.run(&repo, &["status", "--porcelain"])
            .expect("the status"),
        ""
    );
}

#[test]
fn a_commit_on_an_absent_branch_starts_it_from_head_and_one_on_a_present_branch_continues_it() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "main").expect("a repository");
    let commits = two_branches_and_a_tag();

    let first = git.commit(&repo, &commits[0]).expect("a commit");
    let on_topic = git.commit(&repo, &commits[1]).expect("a commit");
    let second = git.commit(&repo, &commits[2]).expect("a commit");

    assert_eq!(history(&git, &repo, "topic"), [first.clone(), on_topic]);
    assert_eq!(history(&git, &repo, "main"), [first, second]);
    assert_eq!(
        git.run(&repo, &["symbolic-ref", "HEAD"]).expect("the head"),
        "refs/heads/main\n"
    );
}

#[test]
fn a_worktree_is_added_on_a_new_branch_without_an_upstream() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    let remote = git
        .bare_remote("alpha", "main", &three_commits())
        .expect("a remote");
    let clone = tree.workspace_dir().join("projects/alpha");
    git.clone_remote("alpha", &clone).expect("a clone");
    let worktree = tree.workspace_dir().join("feature-one/alpha");

    git.add_worktree(&clone, &worktree, "feature-one", "origin/main")
        .expect("a worktree");

    assert!(worktree.join(".git").is_file());
    assert_eq!(
        git.run(&worktree, &["symbolic-ref", "HEAD"])
            .expect("the head"),
        "refs/heads/feature-one\n"
    );
    assert_eq!(
        history(&git, &worktree, "feature-one"),
        history(&git, &remote, "main")
    );
    assert_eq!(
        git.run(
            &clone,
            &[
                "for-each-ref",
                "--format=%(upstream)",
                "refs/heads/feature-one"
            ]
        )
        .expect("the upstream"),
        "\n"
    );
}

#[test]
fn a_git_call_that_exits_non_zero_carries_its_arguments_and_stderr() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "main").expect("a repository");

    let error = git
        .run(&repo, &["rev-parse", "--verify", "refs/heads/absent"])
        .expect_err("a ref that is absent");

    let GitError::Failed { args, stderr, exit } = &error else {
        panic!("{error:?}");
    };
    assert_eq!(args, &["rev-parse", "--verify", "refs/heads/absent"]);
    assert_eq!(stderr, "fatal: Needed a single revision\n");
    assert_eq!(*exit, Exit::Code(128));
    assert_eq!(
        error.to_string(),
        "git rev-parse --verify refs/heads/absent ended with Code(128): fatal: Needed a single revision"
    );
}

#[test]
fn git_run_at_the_root_of_a_tree_finds_no_repository() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");

    let error = git
        .run(tree.root(), &["rev-parse", "--git-dir"])
        .expect_err("no repository");

    assert!(
        matches!(&error, GitError::Failed { stderr, .. } if stderr.contains("not a git repository")),
        "{error:?}"
    );
}

#[test]
fn no_helper_runs_git_or_writes_outside_the_tree() {
    let tree = TempTree::new().expect("an isolated tree");
    let other = TempTree::new().expect("a second tree");
    let git = Git::new(&tree).expect("git");
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "main").expect("a repository");
    let outside = other.workspace_dir();
    let through_parent = tree.root().join("../escaped");
    // Nothing of it exists past the workspace directory, so only its spelling tells where it leads.
    let through_an_absent_parent = tree.workspace_dir().join("absent/../made");

    let refused = [
        git.run(&outside, &["status"]).map(drop),
        git.run(&through_parent, &["status"]).map(drop),
        git.init(&through_an_absent_parent, "main"),
        git.init(&outside.join("repo"), "main"),
        git.clone_remote("alpha", &outside.join("clone")),
        git.add_worktree(&repo, &outside.join("worktree"), "side", "HEAD"),
        git.bare_remote("../../ws/alpha", "main", &[]).map(drop),
        git.clone_remote("nested/alpha", &tree.workspace_dir().join("clone")),
        git.commit(&repo, &change("escapes", "../escaped.txt", "x"))
            .map(drop),
        // Refused before the branch it names is switched to.
        git.commit(
            &repo,
            &CommitSpec {
                branch: Some("side".to_owned()),
                ..change("escapes", "../escaped.txt", "x")
            },
        )
        .map(drop),
        git.commit(&repo, &change("absolute", "/escaped.txt", "x"))
            .map(drop),
        git.commit(
            &repo,
            &CommitSpec {
                delete: vec!["../gitconfig".to_owned()],
                branch: Some("side".to_owned()),
                ..CommitSpec::default()
            },
        )
        .map(drop),
    ];

    for result in &refused {
        assert!(
            matches!(result, Err(GitError::Unconfined { .. })),
            "{result:?}"
        );
    }
    assert_eq!(
        std::fs::read_dir(&outside).expect("the directory").count(),
        0
    );
    assert!(tree.root().join("gitconfig").is_file());
    assert!(!tree.root().join("escaped.txt").exists());
    assert!(!tree.workspace_dir().join("made").exists());
    assert_eq!(
        git.run(&repo, &["rev-list", "--all"]).expect("the commits"),
        ""
    );
    assert_eq!(
        git.run(&repo, &["symbolic-ref", "HEAD"]).expect("the head"),
        "refs/heads/main\n"
    );
}

#[test]
fn commit_refuses_a_repository_outside_the_tree_or_a_relative_one_before_any_effect() {
    let tree = TempTree::new().expect("an isolated tree");
    let other = TempTree::new().expect("a second tree");
    let git = Git::new(&tree).expect("git");
    let outside = other.workspace_dir().join("held");
    std::fs::create_dir(&outside).expect("a directory in the second tree");
    std::fs::write(outside.join("victim.txt"), "kept").expect("a file in the second tree");
    let spec = CommitSpec {
        delete: vec!["victim.txt".to_owned()],
        ..change("plants and removes", "planted.txt", "x")
    };
    let relative = Path::new("probe-rel");

    let refused = [git.commit(&outside, &spec), git.commit(relative, &spec)];

    // Each refusal names the repository: no file of the commit was looked at before it.
    assert!(
        matches!(&refused[0], Err(GitError::Unconfined { path }) if *path == outside),
        "{refused:?}"
    );
    assert!(
        matches!(&refused[1], Err(GitError::Unconfined { path }) if path == relative),
        "{refused:?}"
    );
    assert_eq!(entries(&outside), [outside.join("victim.txt")]);
    assert_eq!(
        std::fs::read(outside.join("victim.txt")).expect("the file"),
        b"kept"
    );
    let working_directory = std::env::current_dir().expect("the working directory");
    assert!(working_directory.join(relative).symlink_metadata().is_err());
}

#[test]
fn a_relative_path_is_refused_by_every_helper() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    git.bare_remote("alpha", "main", &three_commits())
        .expect("a remote");
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "main").expect("a repository");
    let relative = Path::new("probe-rel");

    all_unconfined(&[
        git.run(relative, &["init", "--quiet"]).map(drop),
        git.init(relative, "main"),
        git.clone_remote("alpha", relative),
        git.add_worktree(&repo, relative, "side", "HEAD"),
        git.add_worktree(
            relative,
            &tree.workspace_dir().join("worktree"),
            "side",
            "HEAD",
        ),
    ]);

    let working_directory = std::env::current_dir().expect("the working directory");
    assert!(working_directory.join(relative).symlink_metadata().is_err());
    assert!(!tree.workspace_dir().join("worktree").exists());
}

#[test]
fn a_path_reached_through_a_link_that_leaves_the_tree_is_refused() {
    let tree = TempTree::new().expect("an isolated tree");
    let other = TempTree::new().expect("a second tree");
    let git = Git::new(&tree).expect("git");
    git.bare_remote("alpha", "main", &three_commits())
        .expect("a remote");
    let outside = other.workspace_dir().join("empty");
    std::fs::create_dir(&outside).expect("an empty directory in the second tree");
    let source = tree.workspace_dir().join("source");
    git.init(&source, "main").expect("a repository");
    let out = tree.workspace_dir().join("out");
    std::os::unix::fs::symlink(&outside, &out).expect("a symbolic link");
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "main").expect("a repository");
    std::os::unix::fs::symlink(&outside, repo.join("out")).expect("a symbolic link");
    let destination = out.join("repo");

    all_unconfined(&[
        git.init(&destination, "main"),
        git.clone_remote("alpha", &destination),
        git.add_worktree(&source, &destination, "side", "HEAD"),
        git.commit(&repo, &change("below a link", "out/planted.txt", "x"))
            .map(drop),
        // Refused before the branch it names is switched to.
        git.commit(
            &repo,
            &CommitSpec {
                branch: Some("side".to_owned()),
                ..change("below a link", "out/planted.txt", "x")
            },
        )
        .map(drop),
    ]);

    assert_eq!(entries(&outside), Vec::<PathBuf>::new());
    assert_eq!(
        git.run(&repo, &["symbolic-ref", "HEAD"]).expect("the head"),
        "refs/heads/main\n"
    );
}

#[test]
fn a_file_is_refused_when_the_branch_of_its_commit_brings_a_link_that_leaves_the_tree() {
    let tree = TempTree::new().expect("an isolated tree");
    let other = TempTree::new().expect("a second tree");
    let git = Git::new(&tree).expect("git");
    let outside = other.workspace_dir().join("held");
    std::fs::create_dir(&outside).expect("a directory in the second tree");
    std::fs::write(outside.join("kept.txt"), "kept").expect("a file in the second tree");
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "main").expect("a repository");
    git.commit(&repo, &change("first", "a.txt", "a\n"))
        .expect("a commit");
    git.run(&repo, &["checkout", "--quiet", "-b", "linked"])
        .expect("a branch");
    std::os::unix::fs::symlink(&outside, repo.join("out")).expect("a symbolic link");
    git.commit(
        &repo,
        &CommitSpec {
            message: "a link".to_owned(),
            ..CommitSpec::default()
        },
    )
    .expect("a commit of the link");
    let on_linked = |spec: CommitSpec| {
        // From the first branch nothing of the link is there to see before the switch.
        git.run(&repo, &["checkout", "--quiet", "main"])
            .expect("the first branch");
        assert!(repo.join("out").symlink_metadata().is_err());
        git.commit(
            &repo,
            &CommitSpec {
                branch: Some("linked".to_owned()),
                ..spec
            },
        )
    };

    all_unconfined(&[
        on_linked(change("below a link", "out/planted.txt", "x")),
        on_linked(CommitSpec {
            delete: vec!["out/kept.txt".to_owned()],
            ..CommitSpec::default()
        }),
    ]);

    assert_eq!(entries(&outside), [outside.join("kept.txt")]);
    assert_eq!(
        git.run(&repo, &["rev-list", "--count", "--all"])
            .expect("the count"),
        "2\n"
    );
}

#[test]
fn the_root_of_a_tree_is_never_made_a_repository() {
    let tree = TempTree::new().expect("an isolated tree");
    let other = TempTree::new().expect("a second tree");
    let git = Git::new(&tree).expect("git");
    git.bare_remote("alpha", "main", &three_commits())
        .expect("a remote");
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "main").expect("a repository");
    git.commit(&repo, &change("first", "a.txt", "a\n"))
        .expect("a commit");
    let root = tree.root();
    // The root under two other spellings: through a link in the tree, and through one in a second tree,
    // under which a path resolves below the root without being spelled below it.
    let to_root = tree.workspace_dir().join("to-root");
    std::os::unix::fs::symlink(root, &to_root).expect("a symbolic link");
    let from_outside = other.workspace_dir().join("in");
    std::os::unix::fs::symlink(tree.workspace_dir(), &from_outside).expect("a symbolic link");

    all_unconfined(&[
        git.init(root, "main"),
        git.clone_remote("alpha", root),
        git.add_worktree(&repo, root, "side", "HEAD"),
        git.init(&to_root, "main"),
        git.clone_remote("alpha", &to_root),
        git.add_worktree(&repo, &to_root, "side-two", "HEAD"),
        git.init(&from_outside.join("made"), "main"),
        // Where git would look for the repository of the root, and anything under it.
        git.init(&root.join(".git"), "main"),
        git.init(&root.join(".git/nested"), "main"),
        git.init(&to_root.join(".git"), "main"),
        git.clone_remote("alpha", &root.join(".git")),
        git.add_worktree(&repo, &root.join(".git"), "side-three", "HEAD"),
    ]);

    assert!(root.join(".git").symlink_metadata().is_err());
    assert!(!tree.workspace_dir().join("made").exists());
    assert_eq!(
        git.run(&repo, &["branch", "--format=%(refname)"])
            .expect("the branches"),
        "refs/heads/main\n"
    );
    // Below the root, a repository is made, the link followed.
    git.init(&to_root.join("ws/below"), "main")
        .expect("a repository below the root");
    assert!(tree.workspace_dir().join("below/.git").is_dir());
    // An entry of that name made by other means is not entered through a link either.
    std::fs::create_dir(root.join(".git")).expect("a directory");
    let to_entry = tree.workspace_dir().join("to-entry");
    std::os::unix::fs::symlink(root.join(".git"), &to_entry).expect("a symbolic link");
    all_unconfined(&[
        git.init(&to_entry.join("made"), "main"),
        git.clone_remote("alpha", &to_entry.join("made")),
        git.add_worktree(&repo, &to_entry.join("made"), "side-four", "HEAD"),
    ]);
    // Nor does a commit write a file into it.
    std::os::unix::fs::symlink(root.join(".git"), repo.join("to-entry")).expect("a symbolic link");
    all_unconfined(&[
        git.commit(&repo, &change("into it", "to-entry/HEAD", "x")),
        git.commit(&repo, &change("into it", "to-entry/refs/made", "x")),
    ]);
    assert_eq!(entries(&root.join(".git")), Vec::<PathBuf>::new());
    // Nor is it passed through when it is itself a link to somewhere else in the tree.
    let elsewhere = tree.workspace_dir().join("elsewhere");
    std::fs::create_dir(&elsewhere).expect("a directory");
    std::fs::remove_dir(root.join(".git")).expect("no directory");
    std::os::unix::fs::symlink(&elsewhere, root.join(".git")).expect("a symbolic link");
    all_unconfined(&[
        git.init(&root.join(".git/made"), "main"),
        git.clone_remote("alpha", &root.join(".git/made")),
        git.add_worktree(&repo, &root.join(".git/made"), "side-five", "HEAD"),
    ]);
    assert_eq!(entries(&elsewhere), Vec::<PathBuf>::new());
}

#[test]
fn commit_at_the_root_of_a_tree_leaves_the_git_configuration_of_the_tree_as_it_was() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    let root = tree.root();
    let to_root = tree.workspace_dir().join("to-root");
    std::os::unix::fs::symlink(root, &to_root).expect("a symbolic link");
    let before = own_entries(&tree);
    let blanks = CommitSpec {
        delete: vec!["gitconfig".to_owned()],
        ..change("blanks", "gitconfig", "")
    };
    let on_a_branch = CommitSpec {
        branch: Some("side".to_owned()),
        ..blanks.clone()
    };
    let plants = change("plants", "planted.txt", "x");

    all_unconfined(&[
        git.commit(root, &blanks),
        git.commit(root, &on_a_branch),
        git.commit(root, &plants),
        git.commit(&to_root, &blanks),
        git.commit(&to_root, &plants),
    ]);
    assert_eq!(own_entries(&tree), before);

    // Even were the root a repository, made by other means, no commit is made in it, whatever its files.
    git.run(root, &["init", "--quiet"]).expect("a repository");
    let before = own_entries(&tree);
    let below_an_entry = change("plants", "ws/planted.txt", "x");
    all_unconfined(&[
        git.commit(root, &blanks),
        git.commit(root, &on_a_branch),
        git.commit(&to_root, &plants),
        git.commit(root, &below_an_entry),
        git.commit(&to_root, &below_an_entry),
        git.commit(root, &CommitSpec::default()),
    ]);
    assert_eq!(own_entries(&tree), before);
    assert!(before.0.contains(&root.join(".git")));
    assert!(!tree.workspace_dir().join("planted.txt").exists());
    assert_eq!(
        git.run(root, &["rev-list", "--all"]).expect("the commits"),
        ""
    );
}

#[test]
fn commit_in_a_directory_that_is_not_the_top_of_a_work_tree_writes_and_removes_nothing() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    let plain = tree.workspace_dir().join("plain");
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "main").expect("a repository");
    let inside = repo.join("inside");
    let bare = git.bare_remote("alpha", "main", &[]).expect("a remote");
    for directory in [&plain, &inside, &bare] {
        std::fs::create_dir_all(directory).expect("a directory");
        std::fs::write(directory.join("victim.txt"), "kept").expect("a file");
    }
    let held = |directory: &Path| -> BTreeSet<PathBuf> { entries(directory).into_iter().collect() };
    let before = [held(&plain), held(&inside), held(&bare)];
    let spec = CommitSpec {
        delete: vec!["victim.txt".to_owned()],
        ..change("plants and removes", "planted.txt", "x")
    };
    let on_a_branch = CommitSpec {
        branch: Some("side".to_owned()),
        ..spec.clone()
    };

    let no_repository = [git.commit(&plain, &spec), git.commit(&plain, &on_a_branch)];
    let no_work_tree = [git.commit(&bare, &spec), git.commit(&bare, &on_a_branch)];
    let not_the_top = [
        git.commit(&inside, &spec),
        git.commit(&inside, &on_a_branch),
    ];

    for result in &no_repository {
        assert!(
            matches!(result, Err(GitError::Failed { stderr, .. }) if stderr.contains("not a git repository")),
            "{result:?}"
        );
    }
    for result in &no_work_tree {
        assert!(matches!(result, Err(GitError::Failed { .. })), "{result:?}");
    }
    for result in &not_the_top {
        assert!(
            matches!(result, Err(GitError::Unconfined { path }) if *path == inside),
            "{result:?}"
        );
    }
    assert_eq!([held(&plain), held(&inside), held(&bare)], before);
    for directory in [&plain, &inside, &bare] {
        assert_eq!(
            std::fs::read(directory.join("victim.txt")).expect("the file"),
            b"kept"
        );
    }
    assert_eq!(
        git.run(&repo, &["symbolic-ref", "HEAD"]).expect("the head"),
        "refs/heads/main\n"
    );
    assert_eq!(
        git.run(&repo, &["branch", "--format=%(refname)"])
            .expect("the branches"),
        ""
    );
    assert_eq!(git.run(&bare, &["for-each-ref"]).expect("the refs"), "");
}

#[test]
fn no_helper_writes_or_removes_an_entry_of_the_root_of_the_tree() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    git.bare_remote("alpha", "main", &three_commits())
        .expect("a remote");
    let root = tree.root();
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "main").expect("a repository");
    // Two ways from the repository to the entries of the root: a link to one of them, and a link to the
    // root itself.
    std::os::unix::fs::symlink(root.join("gitconfig"), repo.join("configuration"))
        .expect("a symbolic link");
    std::os::unix::fs::symlink(root, repo.join("up")).expect("a symbolic link");
    let before = own_entries(&tree);
    let removes = |file: &str| CommitSpec {
        delete: vec![file.to_owned()],
        ..CommitSpec::default()
    };
    let on_a_branch = |spec: CommitSpec| CommitSpec {
        branch: Some("side".to_owned()),
        ..spec
    };

    all_unconfined(&[
        git.commit(&repo, &change("through a link", "configuration", "")),
        git.commit(
            &repo,
            &on_a_branch(change("through a link", "configuration", "")),
        ),
        git.commit(&repo, &change("below a link", "up/gitconfig", "")),
        git.commit(&repo, &change("a new entry", "up/planted.txt", "x")),
        git.commit(&repo, &change("in place of a directory", "up/home", "x")),
        // Nor is an entry added to the root on the way to a file: these would make it a repository, a
        // workspace.
        git.commit(&repo, &change("below a new entry", "up/.git/HEAD", "x")),
        git.commit(
            &repo,
            &change("below a new entry", "up/.winter/config.toml", "x"),
        ),
        git.commit(&repo, &change("below a new entry", "up/made/file.txt", "x")),
        git.commit(&repo, &removes("up/gitconfig")),
        git.commit(&repo, &on_a_branch(removes("up/gitconfig"))),
        git.commit(&repo, &removes("up/remotes")),
        git.commit(&repo, &removes("up/bin")),
    ]);
    // Nor does git replace the configuration, given it as the place of a new repository.
    let configuration = root.join("gitconfig");
    let made = [
        git.init(&configuration, "main"),
        git.clone_remote("alpha", &configuration),
        git.add_worktree(&repo, &configuration, "side", "HEAD"),
    ];
    assert!(made.iter().all(Result::is_err), "{made:?}");

    assert_eq!(own_entries(&tree), before);
    assert_eq!(
        git.run(&repo, &["rev-list", "--all"]).expect("the commits"),
        ""
    );
    assert_eq!(
        git.run(&repo, &["symbolic-ref", "HEAD"]).expect("the head"),
        "refs/heads/main\n"
    );
    // Under an entry of the root, the same link is followed.
    git.commit(&repo, &change("below an entry", "up/ws/landed.txt", "x"))
        .expect("a commit");
    assert!(tree.workspace_dir().join("landed.txt").is_file());

    // Nor when the links come with the branch of the commit, after its paths were first looked at.
    let other_repo = tree.workspace_dir().join("other");
    git.init(&other_repo, "main").expect("a repository");
    git.commit(&other_repo, &change("first", "a.txt", "a\n"))
        .expect("a commit");
    git.run(&other_repo, &["checkout", "--quiet", "-b", "linked"])
        .expect("a branch");
    std::os::unix::fs::symlink(root.join("gitconfig"), other_repo.join("configuration"))
        .expect("a symbolic link");
    std::os::unix::fs::symlink(root, other_repo.join("up")).expect("a symbolic link");
    git.commit(&other_repo, &change("the links", "b.txt", "b\n"))
        .expect("a commit of the links");
    let on_linked = |spec: CommitSpec| {
        git.run(&other_repo, &["checkout", "--quiet", "main"])
            .expect("the first branch");
        assert!(other_repo.join("up").symlink_metadata().is_err());
        git.commit(
            &other_repo,
            &CommitSpec {
                branch: Some("linked".to_owned()),
                ..spec
            },
        )
    };
    all_unconfined(&[
        on_linked(change("through a link", "configuration", "")),
        on_linked(removes("up/gitconfig")),
    ]);
    assert_eq!(own_entries(&tree), before);
}

#[test]
fn no_written_or_removed_path_makes_or_touches_the_git_entry_of_the_root() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    let root = tree.root();
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "main").expect("a repository");
    std::os::unix::fs::symlink(root, repo.join("up")).expect("a symbolic link");
    let configuration = std::fs::read(root.join("gitconfig")).expect("the git configuration");

    let written = git.commit(&repo, &change("makes it", "up/.git/HEAD", "x"));

    all_unconfined(&[written]);
    assert!(root.join(".git").symlink_metadata().is_err());

    std::fs::create_dir(root.join(".git")).expect("a directory");
    std::fs::write(root.join(".git/kept"), "kept").expect("a file");
    let removed = git.commit(
        &repo,
        &CommitSpec {
            message: "touches it".to_owned(),
            delete: vec!["up/.git/kept".to_owned()],
            ..CommitSpec::default()
        },
    );

    all_unconfined(&[removed]);
    assert_eq!(
        std::fs::read(root.join(".git/kept")).expect("the file"),
        b"kept"
    );
    assert_eq!(
        std::fs::read(root.join("gitconfig")).expect("the git configuration"),
        configuration
    );
}

#[test]
fn a_written_file_is_a_new_file_and_a_hard_link_carries_nothing_out_of_the_repository() {
    let tree = TempTree::new().expect("an isolated tree");
    let other = TempTree::new().expect("a second tree");
    let git = Git::new(&tree).expect("git");
    let outside = other.root().join("outside.txt");
    std::fs::write(&outside, "kept").expect("a file in the second tree");
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "main").expect("a repository");
    std::fs::hard_link(tree.root().join("gitconfig"), repo.join("cfg")).expect("a hard link");
    std::fs::hard_link(&outside, repo.join("v")).expect("a hard link");
    let before = both_trees(&tree, &other);
    let spec = CommitSpec {
        message: "over hard links".to_owned(),
        files: vec![
            ("cfg".to_owned(), Vec::new()),
            ("v".to_owned(), b"clobbered".to_vec()),
        ],
        ..CommitSpec::default()
    };

    let id = git.commit(&repo, &spec).expect("a commit");

    assert_eq!(history(&git, &repo, "main"), [id]);
    assert_eq!(std::fs::read(repo.join("cfg")).expect("the file"), b"");
    assert_eq!(
        std::fs::read(repo.join("v")).expect("the file"),
        b"clobbered"
    );
    assert_eq!(both_trees(&tree, &other), before);
}

#[test]
fn a_repository_whose_git_directory_lies_outside_the_tree_takes_no_commit_and_no_worktree() {
    let tree = TempTree::new().expect("an isolated tree");
    let other = TempTree::new().expect("a second tree");
    let git = Git::new(&tree).expect("git");
    let outside = other.workspace_dir().join("repo");
    {
        let git = Git::new(&other).expect("git in the second tree");
        git.init(&outside, "main").expect("a repository");
        git.commit(&outside, &change("first", "a.txt", "a\n"))
            .expect("a commit");
    }
    let git_directory = outside.join(".git");
    let linked = tree.workspace_dir().join("linked");
    std::fs::create_dir(&linked).expect("a directory");
    std::os::unix::fs::symlink(&git_directory, linked.join(".git")).expect("a symbolic link");
    let pointed = tree.workspace_dir().join("pointed");
    std::fs::create_dir(&pointed).expect("a directory");
    let pointer = format!("gitdir: {}\n", git_directory.display());
    std::fs::write(pointed.join(".git"), pointer).expect("a file that names a git directory");
    // A git directory of its own in the tree, whose objects and refs are those of the outside one.
    let shared = tree.workspace_dir().join("shared");
    std::fs::create_dir_all(shared.join(".git")).expect("a git directory");
    std::fs::write(shared.join(".git/HEAD"), "ref: refs/heads/main\n").expect("a head");
    let common = format!("{}\n", git_directory.display());
    std::fs::write(shared.join(".git/commondir"), common).expect("the name of a common directory");
    assert_eq!(
        git.run(&shared, &["rev-parse", "--show-toplevel"])
            .expect("a work tree"),
        format!("{}\n", shared.display())
    );
    // And the reverse: objects and refs in the tree, the git directory itself outside it.
    let inside = tree.workspace_dir().join("inside");
    git.init(&inside, "main").expect("a repository");
    let admin = other.workspace_dir().join("admin");
    std::fs::create_dir(&admin).expect("a git directory in the second tree");
    std::fs::write(admin.join("HEAD"), "ref: refs/heads/split\n").expect("a head");
    let common = format!("{}\n", inside.join(".git").display());
    std::fs::write(admin.join("commondir"), common).expect("the name of a common directory");
    let split = tree.workspace_dir().join("split");
    std::fs::create_dir(&split).expect("a directory");
    let pointer = format!("gitdir: {}\n", admin.display());
    std::fs::write(split.join(".git"), pointer).expect("a file that names a git directory");
    assert_eq!(
        git.run(&split, &["rev-parse", "--show-toplevel"])
            .expect("a work tree"),
        format!("{}\n", split.display())
    );
    let worktree = tree.workspace_dir().join("wt");
    let before = both_trees(&tree, &other);
    let spec = change("lands outside", "planted.txt", "x");
    let on_a_branch = CommitSpec {
        branch: Some("side".to_owned()),
        ..spec.clone()
    };

    all_unconfined(&[
        git.commit(&linked, &spec).map(drop),
        git.commit(&linked, &on_a_branch).map(drop),
        git.commit(&pointed, &spec).map(drop),
        git.commit(&pointed, &on_a_branch).map(drop),
        git.add_worktree(&linked, &worktree, "added", "HEAD"),
        git.add_worktree(&pointed, &worktree, "added", "HEAD"),
        git.commit(&shared, &spec).map(drop),
        git.add_worktree(&shared, &worktree, "added", "HEAD"),
        git.commit(&split, &spec).map(drop),
    ]);

    assert!(worktree.symlink_metadata().is_err());
    assert!(!linked.join("planted.txt").exists());
    assert!(!pointed.join("planted.txt").exists());
    assert!(!shared.join("planted.txt").exists());
    assert!(!split.join("planted.txt").exists());
    assert_eq!(both_trees(&tree, &other), before);
}

#[test]
fn a_repository_is_created_only_where_nothing_is_or_in_an_empty_directory() {
    let tree = TempTree::new().expect("an isolated tree");
    let other = TempTree::new().expect("a second tree");
    let git = Git::new(&tree).expect("git");
    let first = other.workspace_dir().join("first");
    let second = other.workspace_dir().join("second");
    std::fs::create_dir(&first).expect("an empty directory in the second tree");
    std::fs::create_dir(&second).expect("another empty directory in the second tree");
    let destination = tree.workspace_dir().join("dest");
    std::fs::create_dir(&destination).expect("a directory");
    std::os::unix::fs::symlink(&first, destination.join(".git")).expect("a symbolic link");
    let remote = tree.root().join("remotes/x.git");
    std::fs::create_dir(&remote).expect("a directory");
    std::os::unix::fs::symlink(&second, remote.join("objects")).expect("a symbolic link");
    // A place that holds no link is refused as well: what is there is not looked into.
    let held = tree.workspace_dir().join("held");
    std::fs::create_dir(&held).expect("a directory");
    std::fs::write(held.join("file.txt"), "x").expect("a file");
    let before = both_trees(&tree, &other);

    all_unconfined(&[
        git.init(&destination, "main"),
        git.bare_remote("x", "main", &three_commits()).map(drop),
        git.bare_remote("x", "main", &[]).map(drop),
        git.init(&held, "main"),
        git.init(&held.join("file.txt"), "main"),
    ]);

    assert_eq!(entries(&first), Vec::<PathBuf>::new());
    assert_eq!(entries(&second), Vec::<PathBuf>::new());
    assert_eq!(both_trees(&tree, &other), before);
    assert_eq!(entries(&held), [held.join("file.txt")]);
    assert_eq!(entries(&remote), [remote.join("objects")]);
    assert_eq!(scratch_entries(&tree), Vec::<PathBuf>::new());

    let empty = tree.workspace_dir().join("empty");
    std::fs::create_dir(&empty).expect("an empty directory");
    git.init(&empty, "main")
        .expect("a repository in an empty directory");
    assert!(empty.join(".git").is_dir());
    let empty_remote = tree.root().join("remotes/y.git");
    std::fs::create_dir(&empty_remote).expect("an empty directory");
    git.bare_remote("y", "main", &three_commits())
        .expect("a remote in an empty directory");
    assert_eq!(history(&git, &empty_remote, "main").len(), 3);
}

#[test]
fn a_commit_removes_a_link_itself_and_never_what_it_leads_to() {
    let tree = TempTree::new().expect("an isolated tree");
    let other = TempTree::new().expect("a second tree");
    let git = Git::new(&tree).expect("git");
    let outside = other.workspace_dir().join("held");
    std::fs::create_dir(&outside).expect("a directory in the second tree");
    std::fs::write(outside.join("kept.txt"), "kept").expect("a file in the second tree");
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "main").expect("a repository");
    let links = [
        ("to-directory", outside.clone()),
        ("to-file", outside.join("kept.txt")),
        ("to-nothing", outside.join("absent.txt")),
        ("to-configuration", tree.root().join("gitconfig")),
        ("to-root", tree.root().to_path_buf()),
    ];
    for (name, target) in &links {
        std::os::unix::fs::symlink(target, repo.join(name)).expect("a symbolic link");
    }
    git.commit(&repo, &change("the links", "a.txt", "a\n"))
        .expect("a commit of the links");
    let before = own_entries(&tree);

    git.commit(
        &repo,
        &CommitSpec {
            message: "without the links".to_owned(),
            delete: links.iter().map(|(name, _)| (*name).to_owned()).collect(),
            ..CommitSpec::default()
        },
    )
    .expect("a commit that removes the links");

    assert_eq!(entries(&repo).len(), 2, "{:?}", entries(&repo));
    assert_eq!(
        git.run(&repo, &["ls-tree", "--name-only", "HEAD"])
            .expect("the tree"),
        "a.txt\n"
    );
    assert_eq!(entries(&outside), [outside.join("kept.txt")]);
    assert_eq!(
        std::fs::read(outside.join("kept.txt")).expect("the file"),
        b"kept"
    );
    assert_eq!(own_entries(&tree), before);
    // What lies behind a link is still out of reach.
    std::os::unix::fs::symlink(&outside, repo.join("again")).expect("a symbolic link");
    all_unconfined(&[git.commit(
        &repo,
        &CommitSpec {
            delete: vec!["again/kept.txt".to_owned()],
            ..CommitSpec::default()
        },
    )]);
    assert_eq!(entries(&outside), [outside.join("kept.txt")]);
}

#[test]
fn a_remote_is_refused_whole_when_a_file_of_a_commit_leads_out_of_its_repository() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    git.bare_remote("alpha", "main", &three_commits())
        .expect("a remote");
    let before = history(&git, &tree.root().join("remotes/alpha.git"), "main");
    let leading_out = [
        change("first", "a.txt", "a\n"),
        change("escapes", "../escaped.txt", "x"),
    ];
    let deleting_out = CommitSpec {
        delete: vec!["../../gitconfig".to_owned()],
        ..CommitSpec::default()
    };
    // Scratch directories are made in `tmp`: one that appears while a helper runs shows the helper began.
    let made_in_tmp = |attempt: &dyn Fn() -> Result<(), GitError>| {
        std::fs::remove_dir(tree.root().join("tmp")).expect("no tmp directory");
        let result = attempt();
        std::fs::create_dir(tree.root().join("tmp")).expect("the tmp directory");
        result
    };

    all_unconfined(&[
        made_in_tmp(&|| git.bare_remote("beta", "main", &leading_out).map(drop)),
        made_in_tmp(&|| git.push_to_remote("alpha", &leading_out[1]).map(drop)),
        made_in_tmp(&|| git.push_to_remote("alpha", &deleting_out).map(drop)),
    ]);

    assert!(!tree.root().join("remotes/beta.git").exists());
    assert_eq!(
        history(&git, &tree.root().join("remotes/alpha.git"), "main"),
        before
    );
}

#[test]
fn a_remote_that_is_a_link_out_of_the_tree_is_neither_cloned_nor_pushed_to() {
    let tree = TempTree::new().expect("an isolated tree");
    let other = TempTree::new().expect("a second tree");
    let git = Git::new(&tree).expect("git");
    let git_of_other = Git::new(&other).expect("git");
    let theirs = git_of_other
        .bare_remote("theirs", "main", &three_commits())
        .expect("a remote in the second tree");
    let refs = || {
        git_of_other
            .run(&theirs, &["for-each-ref"])
            .expect("the refs")
    };
    let before = refs();
    std::os::unix::fs::symlink(&theirs, tree.root().join("remotes/linked.git"))
        .expect("a symbolic link");

    let cloned = git.clone_remote("linked", &tree.workspace_dir().join("clone"));
    // Scratch directories are made in `tmp`: without it, a push that began fails on making one.
    std::fs::remove_dir(tree.root().join("tmp")).expect("no tmp directory");
    let pushed = git
        .push_to_remote("linked", &change("pushed", "pushed.txt", "x"))
        .map(drop);
    std::fs::create_dir(tree.root().join("tmp")).expect("the tmp directory");

    all_unconfined(&[cloned, pushed]);
    assert!(!tree.workspace_dir().join("clone").exists());
    assert_eq!(refs(), before);
}

#[test]
fn no_helper_follows_a_link_that_leads_out_of_the_tree() {
    let tree = TempTree::new().expect("an isolated tree");
    let other = TempTree::new().expect("a second tree");
    let git = Git::new(&tree).expect("git");
    git.bare_remote("alpha", "main", &three_commits())
        .expect("a remote");
    let repo = tree.workspace_dir().join("repo");
    git.init(&repo, "main").expect("a repository");
    let outside = other.workspace_dir();
    std::fs::write(outside.join("kept.txt"), "kept").expect("a file outside the tree");
    let link = |from: &Path, to: &Path| {
        std::os::unix::fs::symlink(to, from).expect("a symbolic link");
    };
    // A directory of the workspace, a directory of the repository, and two files of the repository: one that
    // leads to a file outside, one that leads to nothing yet.
    let directory = tree.workspace_dir().join("linked");
    link(&directory, &outside);
    link(&repo.join("linked"), &outside);
    link(&repo.join("leaf.txt"), &outside.join("kept.txt"));
    link(&repo.join("dangling.txt"), &outside.join("made.txt"));

    let refused = [
        git.run(&directory, &["init", "--quiet", "made"]).map(drop),
        git.init(&directory.join("repo"), "main"),
        git.clone_remote("alpha", &directory.join("clone")),
        git.add_worktree(&repo, &directory.join("worktree"), "side", "HEAD"),
        git.commit(&directory, &change("in a linked repository", "a.txt", "x"))
            .map(drop),
        git.commit(&repo, &change("below a link", "linked/written.txt", "x"))
            .map(drop),
        git.commit(&repo, &change("through a link", "leaf.txt", "x"))
            .map(drop),
        git.commit(
            &repo,
            &change("through a link to nothing", "dangling.txt", "x"),
        )
        .map(drop),
        git.commit(
            &repo,
            &CommitSpec {
                delete: vec!["linked/kept.txt".to_owned()],
                ..CommitSpec::default()
            },
        )
        .map(drop),
    ];

    for result in &refused {
        assert!(
            matches!(result, Err(GitError::Unconfined { .. })),
            "{result:?}"
        );
    }
    assert_eq!(
        std::fs::read_dir(&outside)
            .expect("the directory")
            .map(|entry| entry.expect("an entry").path())
            .collect::<Vec<_>>(),
        [outside.join("kept.txt")]
    );
    assert_eq!(
        std::fs::read(outside.join("kept.txt")).expect("the file"),
        b"kept"
    );
    assert_eq!(
        git.run(&repo, &["rev-list", "--all"]).expect("the commits"),
        ""
    );
}

#[test]
fn a_link_that_stays_inside_the_tree_is_followed() {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    let real = tree.workspace_dir().join("real");
    std::fs::create_dir(&real).expect("a directory inside the tree");
    let linked = tree.workspace_dir().join("linked");
    std::os::unix::fs::symlink(&real, &linked).expect("a symbolic link");

    git.init(&linked.join("repo"), "main")
        .expect("a repository");
    git.commit(&linked.join("repo"), &change("first", "a.txt", "a\n"))
        .expect("a commit");

    assert_eq!(history(&git, &real.join("repo"), "main").len(), 1);
}

#[test]
fn a_stub_named_git_in_the_tree_builds_no_fixture() {
    let tree = TempTree::new().expect("an isolated tree");
    let stub = tree.root().join("bin/git");
    std::fs::write(&stub, "#!/bin/sh\nexit 97\n").expect("a stub");
    std::fs::set_permissions(&stub, std::fs::Permissions::from_mode(0o755))
        .expect("an executable stub");

    let git = Git::new(&tree).expect("git");
    let remote = git
        .bare_remote("alpha", "main", &three_commits())
        .expect("a remote");

    assert_eq!(history(&git, &remote, "main").len(), 3);
}
