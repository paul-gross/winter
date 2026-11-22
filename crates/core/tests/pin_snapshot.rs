//! The pin snapshot, run against the host's git and the real file port in a fixture workspace.

use std::path::PathBuf;

use winter_workspace_core::{
    Diagnostics, Level, Log, NoTrace, OsFiles, OsPrograms, PinPorts, PinSnapshot, ProcessEnv,
    RefKind, RepositoryCommon, StandaloneRepository, snapshot,
};
use winter_workspace_test_support::{CommitSpec, Git, TempTree, WorkspaceBuilder};

struct Fixture {
    tree: TempTree,
    root: PathBuf,
}

fn fixture(lock: Option<&str>) -> Fixture {
    let tree = TempTree::new().expect("a tree");
    let builder = WorkspaceBuilder::new(&tree).config("# workspace\n");
    let root = match lock {
        Some(lock) => builder.lock(lock),
        None => builder,
    }
    .build()
    .expect("a workspace")
    .root;
    Fixture { tree, root }
}

impl Fixture {
    fn git(&self) -> Git<'_> {
        Git::new(&self.tree).expect("git")
    }

    /// A one-commit checkout at `name` under the root; answers its commit.
    fn checkout(&self, name: &str) -> String {
        let path = self.root.join(name);
        let git = self.git();
        git.init(&path, "main").expect("an init");
        git.commit(&path, &commit("one")).expect("a commit")
    }

    fn advance(&self, name: &str) -> String {
        self.git()
            .commit(&self.root.join(name), &commit("two"))
            .expect("a commit")
    }

    /// The snapshot, with the stderr it wrote.
    fn run(&self, standalones: &[StandaloneRepository]) -> (Vec<PinSnapshot>, String) {
        let programs = OsPrograms::new(&ProcessEnv::from_vars(
            self.tree
                .env()
                .variables()
                .map(|(name, value)| (name.to_owned(), value.to_owned())),
        ));
        let ports = PinPorts {
            files: &OsFiles,
            writer: &OsFiles,
            programs: &programs,
            tracer: &NoTrace,
        };
        let mut err = Vec::new();
        let mut diagnostics = Diagnostics {
            err: &mut err,
            log: Log {
                threshold: Level::Debug,
                switched: true,
            },
        };
        let snapshots = snapshot(&ports, &mut diagnostics, &self.root, standalones);
        (snapshots, String::from_utf8(err).expect("UTF-8"))
    }
}

fn commit(message: &str) -> CommitSpec {
    CommitSpec {
        message: message.to_owned(),
        files: vec![(format!("{message}.txt"), message.as_bytes().to_vec())],
        ..CommitSpec::default()
    }
}

fn standalone(name: &str, reference: Option<&str>) -> StandaloneRepository {
    StandaloneRepository {
        common: RepositoryCommon {
            name: name.to_owned(),
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

fn lock(entries: &[(&str, &str, &str, &str)]) -> String {
    let mut text = String::from("version = 1\n");
    for (name, reference, kind, commit) in entries {
        text.push_str(&format!(
            "\n[[standalone]]\nname = \"{name}\"\nref = \"{reference}\"\nkind = \"{kind}\"\ncommit = \"{commit}\"\n"
        ));
    }
    text
}

#[test]
fn a_standalone_in_step_with_its_entry_has_no_drift() {
    let f = fixture(None);
    let sha = f.checkout("api");
    std::fs::write(
        f.root.join(".winter/config.lock"),
        lock(&[("api", "main", "branch", &sha)]),
    )
    .expect("a lock");

    let (snapshots, err) = f.run(&[standalone("api", Some("main"))]);

    assert_eq!(
        snapshots,
        vec![PinSnapshot {
            name: "api".to_owned(),
            r#ref: "main".to_owned(),
            kind: Some(RefKind::Branch),
            locked_commit: Some(sha.clone()),
            config_ref_drift: false,
            head_drift: false,
            head_commit: Some(sha),
        }]
    );
    assert_eq!(err, "");
}

#[test]
fn a_declared_ref_that_differs_from_the_entry_sets_config_ref_drift() {
    let f = fixture(None);
    let sha = f.checkout("api");
    std::fs::write(
        f.root.join(".winter/config.lock"),
        lock(&[("api", "v1", "tag", &sha)]),
    )
    .expect("a lock");

    let (snapshots, _) = f.run(&[standalone("api", Some("main"))]);

    assert!(snapshots[0].config_ref_drift);
    assert!(!snapshots[0].head_drift);
}

#[test]
fn a_checkout_moved_off_the_recorded_commit_sets_head_drift() {
    let f = fixture(None);
    let first = f.checkout("api");
    let second = f.advance("api");
    std::fs::write(
        f.root.join(".winter/config.lock"),
        lock(&[("api", "main", "branch", &first)]),
    )
    .expect("a lock");

    let (snapshots, _) = f.run(&[standalone("api", Some("main"))]);

    assert!(snapshots[0].head_drift);
    assert_eq!(snapshots[0].head_commit.as_deref(), Some(second.as_str()));
}

#[test]
fn an_annotated_tag_entry_sets_head_drift_at_its_own_tag() {
    let f = fixture(None);
    let commit_sha = f.checkout("api");
    let path = f.root.join("api");
    f.git()
        .run(&path, &["tag", "-a", "v1", "-m", "release"])
        .expect("a tag");
    let tag_object = f
        .git()
        .run(&path, &["rev-parse", "v1"])
        .expect("a tag object")
        .trim()
        .to_owned();
    assert_ne!(tag_object, commit_sha);
    f.git()
        .run(&path, &["checkout", "--detach", "v1"])
        .expect("a checkout");
    std::fs::write(
        f.root.join(".winter/config.lock"),
        lock(&[("api", "v1", "tag", &tag_object)]),
    )
    .expect("a lock");

    let (snapshots, _) = f.run(&[standalone("api", Some("v1"))]);

    assert_eq!(
        snapshots[0].head_commit.as_deref(),
        Some(commit_sha.as_str())
    );
    assert!(snapshots[0].head_drift);
    assert!(!snapshots[0].config_ref_drift);
}

#[test]
fn a_standalone_without_an_entry_has_no_kind_and_no_drift() {
    let f = fixture(None);
    let sha = f.checkout("api");

    let (snapshots, _) = f.run(&[standalone("api", Some("main"))]);

    assert_eq!(snapshots[0].kind, None);
    assert_eq!(snapshots[0].locked_commit, None);
    assert!(!snapshots[0].config_ref_drift);
    assert!(!snapshots[0].head_drift);
    assert_eq!(snapshots[0].head_commit, Some(sha));
}

#[test]
fn only_standalones_with_a_ref_are_answered_in_the_order_given() {
    let f = fixture(None);
    f.checkout("b");
    f.checkout("a");

    let (snapshots, _) = f.run(&[
        standalone("b", Some("main")),
        standalone("unpinned", None),
        standalone("a", Some("main")),
    ]);

    let names: Vec<_> = snapshots.iter().map(|s| s.name.as_str()).collect();
    assert_eq!(names, ["b", "a"]);
}

#[test]
fn a_malformed_lock_yields_snapshots_without_entries_and_a_warning() {
    let f = fixture(Some("this is = not [ toml"));
    let sha = f.checkout("api");

    let (snapshots, err) = f.run(&[standalone("api", Some("main"))]);

    assert_eq!(snapshots.len(), 1);
    assert_eq!(snapshots[0].kind, None);
    assert!(!snapshots[0].head_drift);
    assert_eq!(snapshots[0].head_commit, Some(sha));
    assert!(err.contains("WARNING"), "{err}");
}

#[test]
fn a_missing_checkout_yields_no_head_commit_and_a_debug_record() {
    let f = fixture(None);
    let entry = lock(&[(
        "api",
        "main",
        "branch",
        "0123456789abcdef0123456789abcdef01234567",
    )]);
    std::fs::write(f.root.join(".winter/config.lock"), entry).expect("a lock");

    let (snapshots, err) = f.run(&[standalone("api", Some("main"))]);

    assert_eq!(snapshots[0].head_commit, None);
    assert!(!snapshots[0].head_drift);
    assert!(err.contains("DEBUG"), "{err}");
}

#[test]
fn no_pinned_standalone_reads_no_lock_and_writes_no_warning() {
    let f = fixture(Some("this is = not [ toml"));

    let (snapshots, err) = f.run(&[standalone("api", None)]);

    assert!(snapshots.is_empty());
    assert_eq!(err, "");
}
