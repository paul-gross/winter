//! The git operations over the program port, run against the host's git in fixture repositories.

use std::cell::RefCell;
use std::path::{Path, PathBuf};

use winter_workspace_core::{
    CommandTracer, GitFailure, OperationSpan, OsPrograms, ProcessEnv, TraceLog, checkout_branch,
    checkout_detached, clean, head, stash_pop, stash_push, verify,
};
use winter_workspace_test_support::{CommitSpec, Git, TempTree};

type Span = (String, Vec<(String, String)>);

#[derive(Default)]
struct Spans(RefCell<Vec<Span>>);

struct Open;

impl OperationSpan for Open {
    fn failed(&mut self, _: &'static str) {}
}

impl CommandTracer for Spans {
    fn command_started(&self, _: &[String], _: TraceLog) {}

    fn operation(
        &self,
        name: &'static str,
        attributes: &[(&'static str, &str)],
    ) -> Box<dyn OperationSpan + '_> {
        let attributes = attributes
            .iter()
            .map(|(key, value)| ((*key).to_owned(), (*value).to_owned()))
            .collect();
        self.0.borrow_mut().push((name.to_owned(), attributes));
        Box::new(Open)
    }
}

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
