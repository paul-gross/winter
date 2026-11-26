//! `advance` against the host's git in fixture repositories, with a recording integration.

use std::cell::{Cell, RefCell};
use std::path::{Path, PathBuf};

use winter_workspace_core::{
    Advanced, Diagnostics, GitFailure, IntegrateCheckout, Integration, LockEntry, Log, OsFiles,
    OsPrograms, PinError, PinPorts, PinResult, ProcessEnv, PullMode, RefKind, RepositoryCommon,
    StandaloneRepository, advance, upsert_lock,
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
}

fn commit(content: &str, tag: Option<&str>) -> CommitSpec {
    CommitSpec {
        message: content.to_owned(),
        files: vec![("a.txt".to_owned(), content.as_bytes().to_vec())],
        tag: tag.map(str::to_owned),
        ..CommitSpec::default()
    }
}

/// A clone of a remote at its first commit, tagged `v1`, on `main`.
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
    }
}

fn standalone(reference: &str) -> StandaloneRepository {
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
        reference: Some(reference.to_owned()),
        path: String::new(),
        prefix: None,
        config_dir: String::new(),
    }
}

/// An integration that records its calls and answers what it was given, after running `effect` in the checkout.
struct Recording<'f> {
    answer: Integration,
    calls: RefCell<Vec<(String, PullMode, bool)>>,
    effect: Box<dyn Fn(&Path) + 'f>,
    ran: Cell<bool>,
}

impl<'f> Recording<'f> {
    fn answering(answer: Integration) -> Self {
        Self {
            answer,
            calls: RefCell::default(),
            effect: Box::new(|_| {}),
            ran: Cell::new(false),
        }
    }

    fn with(mut self, effect: impl Fn(&Path) + 'f) -> Self {
        self.effect = Box::new(effect);
        self
    }
}

impl IntegrateCheckout for Recording<'_> {
    fn integrate(
        &self,
        path: &Path,
        target: &str,
        mode: PullMode,
        autostash: bool,
    ) -> Result<Integration, GitFailure> {
        self.ran.set(true);
        self.calls
            .borrow_mut()
            .push((target.to_owned(), mode, autostash));
        (self.effect)(path);
        Ok(self.answer)
    }
}

impl Fixture {
    fn git(&self, args: &[&str]) -> String {
        Git::new(&self.tree)
            .expect("git")
            .run(&self.clone, args)
            .expect("git output")
            .trim()
            .to_owned()
    }

    fn lock(&self) -> Option<String> {
        std::fs::read_to_string(self.tree.root().join(LOCK)).ok()
    }

    fn record(&self, r#ref: &str, kind: RefKind, commit: &str) {
        upsert_lock(
            &OsFiles,
            self.tree.root(),
            LockEntry {
                name: "clone".to_owned(),
                r#ref: r#ref.to_owned(),
                kind,
                commit: commit.to_owned(),
            },
        )
        .expect("a lock entry");
    }

    /// The answer and what the diagnostics buffer holds.
    fn advance(
        &self,
        reference: &str,
        mode: PullMode,
        autostash: bool,
        integration: &Recording<'_>,
    ) -> (Result<Advanced, PinError>, String) {
        let ports = PinPorts {
            files: &OsFiles,
            writer: &OsFiles,
            programs: &self.programs,
            tracer: &Spans::default(),
        };
        let mut err = Vec::new();
        let mut diagnostics = Diagnostics {
            err: &mut err,
            log: Log::DEFAULT,
        };
        let answer = advance(
            &ports,
            &mut diagnostics,
            self.tree.root(),
            &standalone(reference),
            mode,
            autostash,
            integration,
        );
        (answer, String::from_utf8(err).expect("UTF-8"))
    }
}

fn pin(answer: Result<Advanced, PinError>) -> (PinResult, String) {
    match answer {
        Ok(Advanced::Pin(outcome)) => (outcome.result, outcome.pin_ref),
        other => panic!("{other:?}"),
    }
}

#[test]
fn a_tag_pin_is_held_in_every_mode_and_is_not_integrated() {
    let f = fixture();
    for mode in [PullMode::FfOnly, PullMode::Merge, PullMode::Rebase] {
        let integration = Recording::answering(Integration::UpToDate);

        let (answer, _) = f.advance("v1", mode, true, &integration);

        assert_eq!(pin(answer), (PinResult::HeldPin, "v1".to_owned()));
        assert!(!integration.ran.get());
    }
    assert_eq!(f.lock(), None);
}

#[test]
fn a_commit_pin_is_held_with_the_declared_ref() {
    let f = fixture();
    let integration = Recording::answering(Integration::UpToDate);

    let (answer, _) = f.advance(&f.first, PullMode::FfOnly, false, &integration);

    assert_eq!(pin(answer), (PinResult::HeldPin, f.first.clone()));
    assert!(!integration.ran.get());
}

#[test]
fn a_branch_pin_is_integrated_onto_origin_in_the_mode_given() {
    let f = fixture();
    let integration = Recording::answering(Integration::UpToDate);

    let _ = f.advance("main", PullMode::Rebase, false, &integration);

    assert_eq!(
        *integration.calls.borrow(),
        [("origin/main".to_owned(), PullMode::Rebase, false)]
    );
}

#[test]
fn a_fast_forward_rewrites_the_entry_to_the_new_head_and_is_re_pinned() {
    let f = fixture();
    f.record("main", RefKind::Branch, &f.first);
    let integration = Recording::answering(Integration::FastForwarded { commits: 1 }).with(|_| {
        Git::new(&f.tree)
            .expect("git")
            .commit(&f.clone, &commit("two\n", None))
            .expect("a commit");
    });

    let (answer, _) = f.advance("main", PullMode::FfOnly, false, &integration);

    let head = f.git(&["rev-parse", "HEAD"]);
    assert_ne!(head, f.first);
    assert_eq!(pin(answer), (PinResult::RePinned, head[..8].to_owned()));
    let lock = f.lock().expect("a lock");
    assert!(lock.contains(&head) && !lock.contains(&f.first), "{lock}");
}

#[test]
fn every_other_integration_is_returned_with_the_lock_unchanged() {
    let f = fixture();
    f.record("main", RefKind::Branch, &f.first);
    let before = f.lock();
    for given in [
        Integration::Merged { commits: 2 },
        Integration::Rebased { commits: 3 },
        Integration::UpToDate,
        Integration::Diverged {
            ahead: 1,
            behind: 2,
        },
    ] {
        let integration = Recording::answering(given);

        let (answer, _) = f.advance("main", PullMode::Merge, false, &integration);

        assert_eq!(answer.expect("an answer"), Advanced::Integrated(given));
        assert_eq!(f.lock(), before);
    }
}

#[test]
fn a_dirty_branch_pin_without_autostash_is_a_pin_error_and_is_not_integrated() {
    let f = fixture();
    std::fs::write(f.clone.join("a.txt"), "changed\n").expect("a change");
    let integration = Recording::answering(Integration::UpToDate);

    let (answer, _) = f.advance("main", PullMode::FfOnly, false, &integration);

    let (result, reason) = pin(answer);
    assert_eq!(result, PinResult::PinError);
    assert!(reason.contains("has uncommitted changes"), "{reason}");
    assert!(!integration.ran.get());
}

#[test]
fn the_stash_is_popped_after_a_fast_forward_and_after_a_diverged_integration() {
    for given in [
        Integration::FastForwarded { commits: 1 },
        Integration::Diverged {
            ahead: 1,
            behind: 1,
        },
    ] {
        let f = fixture();
        std::fs::write(f.clone.join("b.txt"), "x\n").expect("a file");
        f.git(&["add", "b.txt"]);
        let integration = Recording::answering(given);

        let (answer, err) = f.advance("main", PullMode::FfOnly, true, &integration);

        assert!(answer.is_ok(), "{answer:?}");
        assert_eq!(err, "");
        assert_eq!(
            std::fs::read_to_string(f.clone.join("b.txt")).expect("restored"),
            "x\n"
        );
        assert_eq!(f.git(&["stash", "list"]), "");
    }
}

#[test]
fn a_failed_pop_is_a_warning_naming_the_checkout_and_leaves_the_outcome() {
    let f = fixture();
    std::fs::write(f.clone.join("a.txt"), "mine\n").expect("a change");
    let given = Integration::Diverged {
        ahead: 1,
        behind: 1,
    };
    let integration = Recording::answering(given).with(|_| {
        Git::new(&f.tree)
            .expect("git")
            .commit(&f.clone, &commit("theirs\n", None))
            .expect("a commit");
    });

    let (answer, err) = f.advance("main", PullMode::FfOnly, true, &integration);

    assert_eq!(answer.expect("an answer"), Advanced::Integrated(given));
    assert!(err.contains("clone") && err.contains("stash"), "{err}");
}

#[test]
fn a_fast_forward_into_a_malformed_lock_is_a_config_error_and_leaves_the_lock() {
    let f = fixture();
    std::fs::create_dir_all(f.tree.root().join(".winter")).expect("a directory");
    std::fs::write(f.tree.root().join(LOCK), "version = [").expect("a lock");
    let integration = Recording::answering(Integration::FastForwarded { commits: 1 });

    let (answer, _) = f.advance("main", PullMode::FfOnly, false, &integration);

    assert!(matches!(answer, Err(PinError::Config(_))), "{answer:?}");
    assert_eq!(f.lock().as_deref(), Some("version = ["));
}
