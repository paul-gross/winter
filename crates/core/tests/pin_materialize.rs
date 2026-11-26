//! `materialize` against the host's git in fixture repositories, asserting the lock's bytes and `HEAD`.

use std::path::PathBuf;

use winter_workspace_core::{
    LockEntry, OsFiles, OsPrograms, PinError, PinPorts, ProcessEnv, RefKind, RepoMessage,
    RepositoryCommon, StandaloneRepository, materialize, upsert_lock,
};
use winter_workspace_test_support::{CommitSpec, Git, TempTree};

#[path = "support/spans.rs"]
#[allow(dead_code)]
mod spans;
use spans::Spans;

const LOCK: &str = ".winter/config.lock";

struct Fixture {
    tree: TempTree,
    programs: OsPrograms,
    clone: PathBuf,
    first: String,
    second: String,
}

fn commit(content: &str, tag: Option<&str>) -> CommitSpec {
    CommitSpec {
        message: content.to_owned(),
        files: vec![("a.txt".to_owned(), content.as_bytes().to_vec())],
        tag: tag.map(str::to_owned),
        ..CommitSpec::default()
    }
}

/// A clone of a remote at its first commit, tagged `v1`, with a second commit on the remote's `main` fetched
/// into `origin/main` but not checked out.
fn fixture() -> Fixture {
    let tree = TempTree::new().expect("an isolated tree");
    let git = Git::new(&tree).expect("git");
    git.bare_remote("origin", "main", &[commit("one\n", Some("v1"))])
        .expect("a remote");
    let clone = tree.root().join("clone");
    git.clone_remote("origin", &clone).expect("a clone");
    let first = git
        .run(&clone, &["rev-parse", "HEAD"])
        .expect("head")
        .trim()
        .to_owned();
    let second = git
        .push_to_remote("origin", &commit("two\n", None))
        .expect("a second commit");
    git.run(&clone, &["fetch", "--quiet", "origin"])
        .expect("a fetch");
    let programs = OsPrograms::new(
        &ProcessEnv::from_vars(
            tree.env()
                .variables()
                .map(|(name, value)| (name.to_owned(), value.to_owned())),
        ),
        false,
    );
    Fixture {
        tree,
        programs,
        clone,
        first,
        second,
    }
}

fn standalone(reference: Option<&str>) -> StandaloneRepository {
    StandaloneRepository {
        common: RepositoryCommon {
            name: "clone".to_owned(),
            url: None,
            main_branch: "main".to_owned(),
            git_excludes: Vec::new(),
            cmd: Vec::new(),
            extension: false,
            load: None,
            entry: None,
        },
        reference: reference.map(str::to_owned),
        path: String::new(),
        prefix: None,
        config_dir: String::new(),
    }
}

impl Fixture {
    fn out(&self, args: &[&str]) -> String {
        let git = Git::new(&self.tree).expect("git");
        git.run(&self.clone, args)
            .expect("git output")
            .trim()
            .to_owned()
    }

    fn head(&self) -> String {
        self.out(&["rev-parse", "HEAD"])
    }

    fn lock(&self) -> Option<String> {
        std::fs::read_to_string(self.tree.root().join(LOCK)).ok()
    }

    fn record(&self, name: &str, r#ref: &str, kind: RefKind, commit: &str) {
        upsert_lock(
            &OsFiles,
            self.tree.root(),
            LockEntry {
                name: name.to_owned(),
                r#ref: r#ref.to_owned(),
                kind,
                commit: commit.to_owned(),
            },
        )
        .expect("a lock entry");
    }

    fn materialize(&self, reference: Option<&str>) -> Result<Option<(RefKind, String)>, PinError> {
        let ports = PinPorts {
            files: &OsFiles,
            writer: &OsFiles,
            programs: &self.programs,
            tracer: &Spans::default(),
        };
        materialize(&ports, self.tree.root(), &standalone(reference))
    }
}

fn repo_message(answer: Result<Option<(RefKind, String)>, PinError>) -> RepoMessage {
    match answer {
        Err(PinError::Repo(error)) => error.message,
        other => panic!("{other:?}"),
    }
}

#[test]
fn no_ref_pins_nothing_and_touches_nothing() {
    let f = fixture();

    assert!(f.materialize(None).expect("an answer").is_none());

    assert_eq!(f.head(), f.first);
    assert_eq!(f.lock(), None);
}

#[test]
fn a_fresh_tag_resolves_to_the_moved_tag_and_is_recorded() {
    let f = fixture();
    f.out(&["tag", "-f", "v1", &f.second]);

    let answer = f.materialize(Some("v1")).expect("an answer");

    assert_eq!(answer, Some((RefKind::Tag, f.second.clone())));
    assert_eq!(f.head(), f.second);
    let lock = f.lock().expect("a lock");
    assert!(lock.contains(&f.second) && lock.contains("tag"), "{lock}");
}

#[test]
fn a_recorded_branch_lands_on_the_fetched_tip_answers_the_record_and_leaves_the_lock() {
    let f = fixture();
    f.record("clone", "main", RefKind::Branch, &f.first);
    let before = f.lock();

    let answer = f.materialize(Some("main")).expect("an answer");

    assert_eq!(answer, Some((RefKind::Branch, f.first.clone())));
    assert_eq!(f.head(), f.second);
    assert_eq!(f.lock(), before);
}

#[test]
fn a_recorded_commit_is_checked_out_detached_and_the_lock_is_unchanged() {
    let f = fixture();
    f.out(&["checkout", "--quiet", "--detach", &f.second]);
    f.record("clone", "v1", RefKind::Commit, &f.first);
    let before = f.lock();

    let answer = f.materialize(Some("v1")).expect("an answer");

    assert_eq!(answer, Some((RefKind::Commit, f.first.clone())));
    assert_eq!(f.head(), f.first);
    assert_eq!(f.lock(), before);
}

#[test]
fn a_stale_entry_with_an_untracked_file_is_refused_and_leaves_head_and_lock() {
    let f = fixture();
    f.record("clone", "old", RefKind::Tag, &f.first);
    let before = f.lock();
    std::fs::write(f.clone.join("new.txt"), "x\n").expect("a file");

    let message = repo_message(f.materialize(Some("main")));

    assert_eq!(
        message,
        RepoMessage::DirtyRePin {
            name: "clone".to_owned()
        }
    );
    assert_eq!(f.head(), f.first);
    assert_eq!(f.lock(), before);
}

#[test]
fn a_stale_clean_entry_is_re_resolved_and_rewritten() {
    let f = fixture();
    f.record("clone", "old", RefKind::Tag, &f.first);

    let answer = f.materialize(Some("main")).expect("an answer");

    assert_eq!(answer, Some((RefKind::Branch, f.second.clone())));
    assert_eq!(f.head(), f.second);
    let lock = f.lock().expect("a lock");
    assert!(lock.contains("main") && lock.contains(&f.second), "{lock}");
    assert!(!lock.contains("old"), "{lock}");
}

#[test]
fn a_missing_entry_is_written_beside_another_standalone_which_is_kept() {
    let f = fixture();
    f.record("other", "v9", RefKind::Tag, "abc123");

    f.materialize(Some("v1")).expect("an answer");

    let lock = f.lock().expect("a lock");
    assert!(lock.contains("other") && lock.contains("abc123"), "{lock}");
    assert!(lock.contains("clone") && lock.contains(&f.first), "{lock}");
}

#[test]
fn an_empty_ref_is_unresolvable_and_writes_nothing() {
    let f = fixture();

    let message = repo_message(f.materialize(Some("")));

    assert!(
        matches!(message, RepoMessage::UnresolvableRef { .. }),
        "{message:?}"
    );
    assert_eq!(f.lock(), None);
    assert_eq!(f.head(), f.first);
}

#[test]
fn a_malformed_lock_is_a_config_error_and_leaves_head() {
    let f = fixture();
    std::fs::create_dir_all(f.tree.root().join(".winter")).expect("a directory");
    std::fs::write(f.tree.root().join(LOCK), "version = [").expect("a lock");

    let answer = f.materialize(Some("main"));

    assert!(matches!(answer, Err(PinError::Config(_))), "{answer:?}");
    assert_eq!(f.head(), f.first);
}

#[test]
fn an_absent_recorded_commit_is_a_failure() {
    let f = fixture();
    f.record("clone", "v1", RefKind::Commit, &"0".repeat(40));

    let answer = f.materialize(Some("v1"));

    assert!(matches!(answer, Err(PinError::Repo(_))), "{answer:?}");
    assert_eq!(f.head(), f.first);
}

#[test]
fn a_checkout_git_refuses_is_a_repo_error() {
    let f = fixture();
    f.record("clone", "v1", RefKind::Commit, &f.second);
    std::fs::write(f.clone.join("a.txt"), "local\n").expect("a change");

    let answer = f.materialize(Some("v1"));

    assert!(matches!(answer, Err(PinError::Repo(_))), "{answer:?}");
    assert_eq!(f.head(), f.first);
}
