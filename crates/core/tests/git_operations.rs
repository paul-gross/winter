//! The git operations over the program port, run against the host's git in fixture repositories.

use std::io;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use winter_workspace_core::{
    Call, Diagnostics, Finished, GitFailure, Log, OsPrograms, Pacing, ProcessEnv, RunPrograms,
    checkout_branch, checkout_detached, clean, fetch, head, stash_pop, stash_push, verify,
};
use winter_workspace_test_support::{CommitSpec, Git, TempTree};

#[path = "support/spans.rs"]
#[allow(dead_code)]
mod spans;
use spans::Spans;

struct Fixture {
    tree: TempTree,
    programs: OsPrograms,
    clone: PathBuf,
    first: String,
}

fn commit(message: &str, file: &str, content: &str) -> CommitSpec {
    CommitSpec {
        message: message.to_owned(),
        files: vec![(file.to_owned(), content.as_bytes().to_vec())],
        ..CommitSpec::default()
    }
}

fn fixture() -> Fixture {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    git.bare_remote("origin", "main", &[commit("one", "a.txt", "a\n")])
        .expect("a remote");
    let clone = tree.root().join("clone");
    git.clone_remote("origin", &clone).expect("a clone");
    let first = git
        .run(&clone, &["rev-parse", "HEAD"])
        .expect("a head")
        .trim()
        .to_owned();
    let programs = OsPrograms::new(&ProcessEnv::from_vars(
        tree.env()
            .variables()
            .map(|(name, value)| (name.to_owned(), value.to_owned())),
    ));
    Fixture {
        tree,
        programs,
        clone,
        first,
    }
}

impl Fixture {
    fn git(&self) -> Git<'_> {
        Git::new(&self.tree).expect("git")
    }

    fn out(&self, args: &[&str]) -> String {
        self.git().run(&self.clone, args).expect("git output")
    }

    fn write(&self, file: &str, content: &str) {
        std::fs::write(self.clone.join(file), content).expect("a written file");
    }
}

#[test]
fn head_of_a_fresh_clone_is_its_commit() {
    let f = fixture();

    let sha = head(&f.programs, &Spans::default(), "api", &f.clone).expect("a head");

    assert_eq!(sha, f.first);
}

#[test]
fn head_of_a_directory_that_is_no_repository_fails() {
    let f = fixture();
    let plain = f.tree.root().join("plain");
    std::fs::create_dir(&plain).expect("a directory");

    let answer = head(&f.programs, &Spans::default(), "api", &plain);

    assert!(matches!(answer, Err(GitFailure::Repo(_))), "{answer:?}");
}

#[test]
fn clean_is_true_for_a_fresh_clone_and_false_for_a_modified_tracked_file() {
    let f = fixture();
    let spans = Spans::default();
    assert!(clean(&f.programs, &spans, "api", &f.clone).expect("an answer"));

    f.write("a.txt", "changed\n");

    assert!(!clean(&f.programs, &spans, "api", &f.clone).expect("an answer"));
}

#[test]
fn clean_is_false_for_an_untracked_file_alone() {
    let f = fixture();
    f.write("new.txt", "x\n");

    assert!(!clean(&f.programs, &Spans::default(), "api", &f.clone).expect("an answer"));
}

#[test]
fn clean_is_false_for_a_path_that_is_no_repository() {
    let f = fixture();
    let plain = f.tree.root().join("plain");
    std::fs::create_dir(&plain).expect("a directory");

    assert!(!clean(&f.programs, &Spans::default(), "api", &plain).expect("an answer"));
}

#[test]
fn verify_of_a_missing_ref_is_no_match_and_of_head_is_a_match() {
    let f = fixture();

    assert!(!verify(&f.programs, &f.clone, "refs/heads/missing").expect("an answer"));
    assert!(verify(&f.programs, &f.clone, "HEAD").expect("an answer"));
}

#[test]
fn checkout_detached_leaves_head_detached_at_the_commit() {
    let f = fixture();
    f.git()
        .commit(&f.clone, &commit("two", "b.txt", "b\n"))
        .expect("a commit");

    checkout_detached(&f.programs, &Spans::default(), "api", &f.clone, &f.first)
        .expect("a checkout");

    assert_eq!(f.out(&["rev-parse", "HEAD"]).trim(), f.first);
    assert!(
        f.git()
            .run(&f.clone, &["symbolic-ref", "-q", "HEAD"])
            .is_err()
    );
}

#[test]
fn checkout_branch_resets_the_branch_to_origin_and_leaves_the_local_commit_off_every_branch() {
    let f = fixture();
    let local = f
        .git()
        .commit(&f.clone, &commit("local", "b.txt", "b\n"))
        .expect("a commit");

    checkout_branch(&f.programs, &Spans::default(), "api", &f.clone, "main").expect("a checkout");

    assert_eq!(f.out(&["rev-parse", "main"]).trim(), f.first);
    assert_eq!(f.out(&["rev-parse", "origin/main"]).trim(), f.first);
    assert_eq!(f.out(&["branch", "--all", "--contains", &local]), "");
}

#[test]
fn stash_pop_restores_a_stashed_modification() {
    let f = fixture();
    let spans = Spans::default();
    f.write("a.txt", "changed\n");

    stash_push(&f.programs, &spans, "api", &f.clone).expect("a stash");
    assert_eq!(read(&f.clone, "a.txt"), "a\n");
    stash_pop(&f.programs, &spans, "api", &f.clone).expect("a pop");

    assert_eq!(read(&f.clone, "a.txt"), "changed\n");
}

#[test]
fn stash_push_of_a_tree_dirty_only_by_an_untracked_file_creates_no_entry() {
    let f = fixture();
    f.write("new.txt", "x\n");

    stash_push(&f.programs, &Spans::default(), "api", &f.clone).expect("a stash");

    assert_eq!(f.out(&["stash", "list"]), "");
}

#[test]
fn each_checkout_is_one_git_checkout_span_attributed_with_the_repo_alone() {
    let f = fixture();
    let spans = Spans::default();

    checkout_branch(&f.programs, &spans, "api", &f.clone, "main").expect("a checkout");
    checkout_detached(&f.programs, &spans, "api", &f.clone, &f.first).expect("a checkout");

    let attributes = vec![("winter.repo".to_owned(), "api".to_owned())];
    assert_eq!(
        *spans.0.borrow(),
        vec![
            ("git checkout".to_owned(), attributes.clone()),
            ("git checkout".to_owned(), attributes),
        ]
    );
}

fn read(clone: &Path, file: &str) -> String {
    std::fs::read_to_string(clone.join(file)).expect("a file")
}

/// The programs of the fixture, with a sleep that waits for nothing and a draw fixed at zero.
struct Instant(OsPrograms);

impl RunPrograms for Instant {
    fn run(&self, call: &Call<'_>) -> io::Result<Finished> {
        self.0.run(call)
    }

    fn pacing(&self) -> Pacing {
        Pacing {
            sleep: |_| {},
            draw: || 0.0,
        }
    }
}

fn env_of(f: &Fixture, timeout: Option<&str>) -> ProcessEnv {
    ProcessEnv::from_vars(
        f.tree
            .env()
            .variables()
            .map(|(name, value)| (name.to_owned(), value.to_owned()))
            .chain(timeout.map(|value| ("WINTER_GIT_TIMEOUT_S".into(), value.into()))),
    )
}

/// Runs `fetch` with the stub `bin/git` `body` of the tree first on its `PATH`; answers the outcome, what
/// the diagnostics held, and how often the stub was started.
fn fetch_with_stub(
    f: &Fixture,
    body: &str,
    timeout: &str,
    spans: &Spans,
) -> (Result<(), GitFailure>, String, usize) {
    let bin = f.tree.root().join("bin");
    std::fs::create_dir_all(&bin).expect("a bin directory");
    let count = f.tree.root().join("count");
    let stub = bin.join("git");
    std::fs::write(
        &stub,
        format!("#!/bin/sh\necho x >> '{}'\n{body}\n", count.display()),
    )
    .expect("a stub");
    std::fs::set_permissions(&stub, std::fs::Permissions::from_mode(0o755)).expect("executable");
    let programs = Instant(OsPrograms::new(&env_of(f, Some(timeout))));
    let mut err = Vec::new();
    let answer = fetch(
        &programs,
        spans,
        &env_of(f, Some(timeout)),
        &mut Diagnostics {
            err: &mut err,
            log: Log::DEFAULT,
        },
        "api",
        &f.clone,
    );
    let started = std::fs::read_to_string(count).map_or(0, |text| text.lines().count());
    (answer, String::from_utf8(err).expect("utf-8"), started)
}

#[test]
fn fetch_kills_a_git_past_the_timeout_and_retries_it() {
    let f = fixture();

    let (answer, notices, started) = fetch_with_stub(&f, "sleep 30", "0.2", &Spans::default());

    assert!(matches!(answer, Err(GitFailure::Repo(_))), "{answer:?}");
    assert_eq!(started, 3);
    assert!(notices.contains("transient git error (attempt 1/3): Timeout: the command \"git fetch origin\" did not complete in 0 secs. — retrying in 1.00s"), "{notices}");
    assert!(notices.contains("(attempt 2/3)"), "{notices}");
    assert!(!notices.contains("(attempt 3/3)"), "{notices}");
}

#[test]
fn fetch_that_fails_twice_transiently_and_then_succeeds_is_a_success_with_two_notices() {
    let f = fixture();
    let body = "[ \"$(wc -l < '%COUNT%')\" -ge 3 ] && exit 0\necho 'fatal: the remote end hung up unexpectedly' >&2\nexit 128";
    let body = body.replace(
        "%COUNT%",
        &f.tree.root().join("count").display().to_string(),
    );

    let (answer, notices, started) = fetch_with_stub(&f, &body, "30", &Spans::default());

    answer.expect("a fetch");
    assert_eq!(started, 3);
    assert_eq!(notices.lines().count(), 2, "{notices}");
}

#[test]
fn fetch_that_is_denied_fails_after_one_attempt_with_no_notice() {
    let f = fixture();

    let (answer, notices, started) = fetch_with_stub(
        &f,
        "echo 'git@host: Permission denied (publickey).' >&2\nexit 128",
        "30",
        &Spans::default(),
    );

    assert!(matches!(answer, Err(GitFailure::Repo(_))), "{answer:?}");
    assert_eq!(started, 1);
    assert_eq!(notices, "");
}

#[test]
fn a_fetch_retried_once_is_one_git_fetch_span() {
    let f = fixture();
    let body = "[ \"$(wc -l < '%COUNT%')\" -ge 2 ] && exit 0\necho kex_exchange_identification >&2\nexit 255";
    let body = body.replace(
        "%COUNT%",
        &f.tree.root().join("count").display().to_string(),
    );
    let spans = Spans::default();

    let (answer, _, started) = fetch_with_stub(&f, &body, "30", &spans);

    answer.expect("a fetch");
    assert_eq!(started, 2);
    assert_eq!(
        *spans.0.borrow(),
        vec![(
            "git fetch".to_owned(),
            vec![("winter.repo".to_owned(), "api".to_owned())]
        )]
    );
}

fn quiet_fetch(f: &Fixture) {
    let mut err = Vec::new();
    fetch(
        &f.programs,
        &Spans::default(),
        &env_of(f, None),
        &mut Diagnostics {
            err: &mut err,
            log: Log::DEFAULT,
        },
        "api",
        &f.clone,
    )
    .expect("a fetch");
    assert!(err.is_empty(), "{}", String::from_utf8_lossy(&err));
}

#[test]
fn fetch_brings_in_a_commit_pushed_to_the_remote() {
    let f = fixture();
    let pushed = f
        .git()
        .push_to_remote("origin", &commit("two", "b.txt", "b\n"))
        .expect("a push");

    quiet_fetch(&f);

    assert_eq!(f.out(&["rev-parse", "origin/main"]).trim(), pushed);
}

#[test]
fn fetch_keeps_a_local_tag_the_remote_moved() {
    let f = fixture();
    let mut tagged = commit("two", "b.txt", "b\n");
    tagged.tag = Some("v1".to_owned());
    let at = f.git().push_to_remote("origin", &tagged).expect("a push");
    quiet_fetch(&f);
    assert_eq!(f.out(&["rev-parse", "v1"]).trim(), at);
    let later = f
        .git()
        .push_to_remote("origin", &commit("three", "c.txt", "c\n"))
        .expect("a push");
    let remote = f.tree.root().join("remotes").join("origin.git");
    f.git()
        .run(&remote, &["tag", "-f", "v1", &later])
        .expect("a moved tag");

    quiet_fetch(&f);

    assert_eq!(f.out(&["rev-parse", "v1"]).trim(), at);
    assert_eq!(f.out(&["rev-parse", "origin/main"]).trim(), later);
}
