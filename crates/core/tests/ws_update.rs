//! `ws update`: the re-pin of standalone clones of fixture remotes, run through the host's git, asserting the
//! events, the rows of both streams, each checkout's `HEAD`, and the bytes of the lock.

use std::io::{self, Write};
use std::path::PathBuf;

use serde_json::{Value, json};
use winter_workspace_core::{
    CommandError, LockEntry, OsFiles, RefKind, RepoMessage, SelectionError, Termination, read_lock,
};
use winter_workspace_test_support::{CommitSpec, Git, TempTree, WorkspaceBuilder};

#[path = "support/in_process.rs"]
mod in_process;

use in_process::{Ended, InProcess};

const LOCK: &str = ".winter/config.lock";
const CONFIG: &str = ".winter/config.toml";
const DIRTY_REFUSAL: &str =
    "refusing to re-pin 'lib': uncommitted changes; commit/stash or pass --autostash";

/// The shared configuration that declares each standalone of `standalones` by name, with its ref when it has
/// one.
fn config(standalones: &[(&str, Option<&str>)]) -> String {
    standalones
        .iter()
        .map(|(name, reference)| {
            let reference = reference.map_or_else(String::new, |r| format!("ref = \"{r}\"\n"));
            format!("[[standalone_repository]]\nname = \"{name}\"\n{reference}\n")
        })
        .collect()
}

/// The two commits of every fixture remote: the first, tagged `v1`, and the second, tagged `v2`, which
/// changes `a.txt`, adds `new.txt`, and leaves `keep.txt` alone.
fn history() -> Vec<CommitSpec> {
    let file = |name: &str, content: &str| (name.to_owned(), content.as_bytes().to_vec());
    vec![
        CommitSpec {
            message: "one".to_owned(),
            files: vec![file("a.txt", "one\n"), file("keep.txt", "keep\n")],
            tag: Some("v1".to_owned()),
            ..CommitSpec::default()
        },
        CommitSpec {
            message: "two".to_owned(),
            files: vec![file("a.txt", "two\n"), file("new.txt", "new\n")],
            tag: Some("v2".to_owned()),
            ..CommitSpec::default()
        },
    ]
}

/// A workspace whose standalones are clones of local bare remotes, each on `main` at `v2`.
struct Fixture {
    tree: TempTree,
    root: PathBuf,
}

impl Fixture {
    /// A workspace with the shared configuration `config` and a clone of a remote of its own for each name
    /// of `clones`.
    fn new(config: &str, clones: &[&str]) -> Self {
        let tree = TempTree::new().expect("an isolated tree");
        let mut builder = WorkspaceBuilder::new(&tree).config(config);
        for name in clones {
            builder = builder.remote(name, history());
        }
        let root = builder.build().expect("a workspace").root;
        let git = Git::new(&tree).expect("git");
        for name in clones {
            git.clone_remote(name, &root.join(name)).expect("a clone");
        }
        drop(git);
        Self { tree, root }
    }

    /// One standalone `lib`, cloned, that declares `reference`.
    fn lib(reference: &str) -> Self {
        Self::new(&config(&[("lib", Some(reference))]), &["lib"])
    }

    /// One standalone `lib` that declares `v1` and was re-pinned to it: its `HEAD` is `v1` and the lock
    /// records it.
    fn pinned_at_v1() -> Self {
        let fixture = Self::lib("v1");
        let ended = fixture.update(&[]);
        assert!(
            matches!(ended.termination, Termination::Success),
            "{:?}: {}",
            ended.termination,
            ended.stderr
        );
        fixture
    }

    fn git(&self, repository: &str, args: &[&str]) -> String {
        Git::new(&self.tree)
            .expect("git")
            .run(&self.root.join(repository), args)
            .expect("git output")
            .trim()
            .to_owned()
    }

    /// Runs git in the bare remote `name`.
    fn remote(&self, name: &str, args: &[&str]) -> String {
        Git::new(&self.tree)
            .expect("git")
            .run(
                &self.tree.root().join("remotes").join(format!("{name}.git")),
                args,
            )
            .expect("git output")
            .trim()
            .to_owned()
    }

    fn head(&self, repository: &str) -> String {
        self.git(repository, &["rev-parse", "HEAD"])
    }

    /// The commit `rev` names in the clone `repository`.
    fn commit(&self, repository: &str, rev: &str) -> String {
        self.git(
            repository,
            &["rev-parse", "--verify", &format!("{rev}^{{commit}}")],
        )
    }

    fn stashes(&self, repository: &str) -> String {
        self.git(repository, &["stash", "list"])
    }

    fn write(&self, relative: &str, content: &str) {
        std::fs::write(self.root.join(relative), content).expect("a file");
    }

    fn read(&self, relative: &str) -> String {
        std::fs::read_to_string(self.root.join(relative)).expect("a file")
    }

    fn declare(&self, standalones: &[(&str, Option<&str>)]) {
        self.write(CONFIG, &config(standalones));
    }

    fn lock(&self) -> Option<String> {
        std::fs::read_to_string(self.root.join(LOCK)).ok()
    }

    fn entry(&self, name: &str) -> Option<LockEntry> {
        read_lock(&OsFiles, &self.root)
            .expect("a lock")
            .remove(name)
    }

    fn invocation(&self, args: &[&str]) -> InProcess {
        InProcess::new(&[&["ws", "update"], args].concat())
            .cwd(&self.root)
            .pinned(&self.tree.env())
    }

    fn update(&self, args: &[&str]) -> Ended {
        self.invocation(args).run()
    }

    /// The events of a `--json` run with `args`, and its ending.
    fn events(&self, args: &[&str]) -> (Vec<Value>, Ended) {
        let ended = self.update(&[&["--json"], args].concat());
        let events = ended
            .stdout
            .lines()
            .map(|line| serde_json::from_str(line).expect("a JSON line"))
            .collect();
        (events, ended)
    }
}

fn started() -> Value {
    json!({"type": "pull_started"})
}

fn completed(success: bool) -> Value {
    json!({"type": "pull_completed", "success": success})
}

fn synced(repo: &str, result: &str, pin_ref: &str) -> Value {
    json!({
        "type": "repo_synced",
        "scope": "standalone",
        "repo": repo,
        "result": result,
        "commits": 0,
        "ahead": 0,
        "behind": 0,
        "pin_ref": pin_ref,
    })
}

/// The `repo` and `result` of each `repo_synced` event of `events`, in order.
fn results(events: &[Value]) -> Vec<(String, String)> {
    events
        .iter()
        .filter(|event| event["type"] == "repo_synced")
        .map(|event| {
            let text = |key: &str| event[key].as_str().expect("a string").to_owned();
            (text("repo"), text("result"))
        })
        .collect()
}

fn pairs(expected: &[(&str, &str)]) -> Vec<(String, String)> {
    expected
        .iter()
        .map(|(repo, result)| ((*repo).to_owned(), (*result).to_owned()))
        .collect()
}

#[track_caller]
fn succeeded(ended: &Ended) {
    assert!(
        matches!(ended.termination, Termination::Success),
        "{:?}: {}",
        ended.termination,
        ended.stderr
    );
}

#[track_caller]
fn reported(ended: &Ended) {
    assert!(
        matches!(ended.termination, Termination::Reported),
        "{:?}: {}",
        ended.termination,
        ended.stderr
    );
    assert_eq!(ended.termination.exit_code(), 1);
}

/// A stdout that accepts `allowed` writes and refuses every later one, counting every write it is asked for.
struct RefusingAfter {
    allowed: usize,
    asked: usize,
}

impl Write for RefusingAfter {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.asked += 1;
        if self.asked > self.allowed {
            return Err(io::Error::other("the handle refuses the write"));
        }
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[test]
fn a_declaration_changed_to_a_newer_tag_is_re_pinned_and_only_its_entry_is_rewritten() {
    let fixture = Fixture::new(
        &config(&[("lib", Some("v1")), ("other", Some("v1"))]),
        &["lib", "other"],
    );
    succeeded(&fixture.update(&[]));
    let other_before = fixture.entry("other").expect("an entry");
    let v1 = fixture.commit("lib", "v1");
    let v2 = fixture.commit("lib", "v2");
    assert_eq!(fixture.head("lib"), v1);

    fixture.declare(&[("lib", Some("v2")), ("other", Some("v1"))]);
    let (events, ended) = fixture.events(&[]);

    succeeded(&ended);
    assert_eq!(
        events,
        [
            started(),
            synced("lib", "re_pinned", &v2[..8]),
            synced("other", "up_to_date", ""),
            completed(true),
        ]
    );
    assert_eq!(fixture.head("lib"), v2);
    assert_eq!(
        fixture.entry("lib"),
        Some(LockEntry {
            name: "lib".to_owned(),
            r#ref: "v2".to_owned(),
            kind: RefKind::Tag,
            commit: v2,
        })
    );
    assert_eq!(fixture.entry("other"), Some(other_before));
}

#[test]
fn a_second_run_is_up_to_date_and_leaves_the_bytes_of_the_lock() {
    let fixture = Fixture::pinned_at_v1();
    let lock = fixture.lock().expect("a lock");
    let head = fixture.head("lib");

    let (events, ended) = fixture.events(&[]);

    succeeded(&ended);
    assert_eq!(pairs(&[("lib", "up_to_date")]), results(&events));
    assert_eq!(fixture.lock(), Some(lock));
    assert_eq!(fixture.head("lib"), head);
}

#[test]
fn a_tag_moved_on_the_remote_reads_up_to_date() {
    let fixture = Fixture::pinned_at_v1();
    let head = fixture.head("lib");
    let moved = Git::new(&fixture.tree)
        .expect("git")
        .push_to_remote(
            "lib",
            &CommitSpec {
                message: "three".to_owned(),
                files: vec![("a.txt".to_owned(), b"three\n".to_vec())],
                ..CommitSpec::default()
            },
        )
        .expect("a third commit");
    fixture.remote("lib", &["tag", "-f", "v1", &moved]);

    let (events, ended) = fixture.events(&[]);

    succeeded(&ended);
    assert_eq!(pairs(&[("lib", "up_to_date")]), results(&events));
    assert_eq!(fixture.head("lib"), head);
    assert_eq!(
        fixture.commit("lib", "origin/main"),
        moved,
        "the run fetched"
    );
}

#[test]
fn an_annotated_tag_is_re_pinned_on_every_run() {
    let fixture = Fixture::lib("rel");
    let v2 = fixture.commit("lib", "v2");
    fixture.remote("lib", &["tag", "-a", "rel", "-m", "release", &v2]);
    let tag = fixture.remote("lib", &["rev-parse", "refs/tags/rel"]);
    assert_ne!(tag, v2);

    for run in ["first", "second"] {
        let (events, ended) = fixture.events(&[]);

        succeeded(&ended);
        assert_eq!(
            events,
            [
                started(),
                synced("lib", "re_pinned", &tag[..8]),
                completed(true)
            ],
            "{run}"
        );
        assert_eq!(fixture.head("lib"), v2, "{run}");
        assert_eq!(
            fixture.entry("lib").map(|entry| entry.commit),
            Some(tag.clone()),
            "{run}"
        );
    }
}

#[test]
fn a_branch_pin_with_a_local_only_commit_is_re_pinned_to_the_remote_branch() {
    let fixture = Fixture::lib("main");
    succeeded(&fixture.update(&[]));
    let origin = fixture.commit("lib", "origin/main");
    let local = Git::new(&fixture.tree)
        .expect("git")
        .commit(
            &fixture.root.join("lib"),
            &CommitSpec {
                message: "local only".to_owned(),
                files: vec![("local.txt".to_owned(), b"local\n".to_vec())],
                ..CommitSpec::default()
            },
        )
        .expect("a local commit");
    assert_eq!(fixture.head("lib"), local);

    let (events, ended) = fixture.events(&[]);

    succeeded(&ended);
    assert_eq!(
        events,
        [
            started(),
            synced("lib", "re_pinned", &origin[..8]),
            completed(true)
        ]
    );
    assert_eq!(fixture.head("lib"), origin);
    assert_eq!(
        fixture.git("lib", &["rev-parse", "--abbrev-ref", "HEAD"]),
        "main"
    );
    assert_eq!(
        fixture.entry("lib").map(|entry| (entry.kind, entry.commit)),
        Some((RefKind::Branch, origin))
    );
}

#[test]
fn an_autostashed_change_is_restored_after_a_checkout() {
    let fixture = Fixture::lib("v1");
    fixture.write("lib/keep.txt", "edited\n");

    let (events, ended) = fixture.events(&["--autostash"]);

    succeeded(&ended);
    assert_eq!(pairs(&[("lib", "re_pinned")]), results(&events));
    assert_eq!(fixture.head("lib"), fixture.commit("lib", "v1"));
    assert_eq!(fixture.read("lib/keep.txt"), "edited\n");
    assert_eq!(fixture.stashes("lib"), "");
}

#[test]
fn an_autostashed_change_is_restored_after_an_unresolvable_ref() {
    let fixture = Fixture::lib("no-such-ref");
    let head = fixture.head("lib");
    fixture.write("lib/keep.txt", "edited\n");

    let (events, ended) = fixture.events(&["--autostash"]);

    reported(&ended);
    assert_eq!(pairs(&[("lib", "pin_error")]), results(&events));
    assert_eq!(fixture.head("lib"), head);
    assert_eq!(fixture.read("lib/keep.txt"), "edited\n");
    assert_eq!(fixture.stashes("lib"), "");
}

#[test]
fn a_checkout_git_refuses_is_a_pin_error_that_leaves_the_lock_and_restores_the_change() {
    let fixture = Fixture::pinned_at_v1();
    let lock = fixture.lock().expect("a lock");
    let head = fixture.head("lib");
    fixture.declare(&[("lib", Some("v2"))]);
    fixture.write("lib/keep.txt", "edited\n");
    // `v2` adds this file, so git refuses to check it out over the untracked one.
    fixture.write("lib/new.txt", "in the way\n");

    let (events, ended) = fixture.events(&["--autostash"]);

    reported(&ended);
    assert_eq!(pairs(&[("lib", "pin_error")]), results(&events));
    let reason = events[1]["pin_ref"].as_str().expect("a reason");
    assert!(
        reason.starts_with("checkout failed for lib\n  $ git checkout --detach "),
        "{reason}"
    );
    assert_eq!(events.last(), Some(&completed(false)));
    assert_eq!(fixture.lock(), Some(lock));
    assert_eq!(fixture.head("lib"), head);
    assert_eq!(fixture.read("lib/keep.txt"), "edited\n");
    assert_eq!(fixture.read("lib/new.txt"), "in the way\n");
    assert_eq!(fixture.stashes("lib"), "");
}

#[test]
fn a_pop_that_conflicts_keeps_the_outcome_warns_of_the_checkout_and_leaves_the_stash() {
    let fixture = Fixture::pinned_at_v1();
    fixture.declare(&[("lib", Some("v2"))]);
    // `v2` changes this file, so the stashed edit no longer applies.
    fixture.write("lib/a.txt", "edited\n");

    let (events, ended) = fixture.events(&["--autostash"]);

    succeeded(&ended);
    assert_eq!(pairs(&[("lib", "re_pinned")]), results(&events));
    assert_eq!(events.last(), Some(&completed(true)));
    assert_eq!(fixture.head("lib"), fixture.commit("lib", "v2"));
    let warning = format!(
        "stash pop failed for lib; leftover stash at '{}' needs manual resolution: stash pop failed for lib\n",
        fixture.root.join("lib").display()
    );
    assert!(ended.stderr.starts_with(&warning), "{}", ended.stderr);
    assert_eq!(fixture.stashes("lib").lines().count(), 1);
}

#[test]
fn a_dirty_checkout_without_autostash_is_a_pin_error_and_is_left_alone() {
    let fixture = Fixture::lib("v1");
    let head = fixture.head("lib");
    fixture.write("lib/keep.txt", "edited\n");

    let (events, ended) = fixture.events(&[]);

    reported(&ended);
    assert_eq!(
        events,
        [
            started(),
            synced("lib", "pin_error", DIRTY_REFUSAL),
            completed(false)
        ]
    );
    assert_eq!(ended.stderr, "");
    assert_eq!(fixture.head("lib"), head);
    assert_eq!(fixture.read("lib/keep.txt"), "edited\n");
    assert_eq!(fixture.lock(), None);
}

#[test]
fn an_unresolvable_ref_is_a_rendered_pin_error_and_the_next_standalone_still_re_pins() {
    let fixture = Fixture::new(
        &config(&[("bad", Some("no-such-ref")), ("lib", Some("v1"))]),
        &["bad", "lib"],
    );
    let bad = fixture.root.join("bad");
    let reason = format!(
        "unresolvable ref 'no-such-ref' at {path}: not a branch, tag, or commit SHA\n  cwd: {path}",
        path = bad.display()
    );
    let v1 = fixture.commit("lib", "v1");

    let (events, ended) = fixture.events(&[]);

    reported(&ended);
    assert_eq!(
        events,
        [
            started(),
            synced("bad", "pin_error", &reason),
            synced("lib", "re_pinned", &v1[..8]),
            completed(false),
        ]
    );
    assert_eq!(
        ended.stderr,
        format!("resolve_ref failed for standalone bad: {reason}\n")
    );
    assert_eq!(fixture.head("lib"), v1);
    assert!(fixture.entry("bad").is_none());
}

#[test]
fn a_failed_fetch_is_logged_and_the_ref_resolves_against_what_is_on_disk() {
    let fixture = Fixture::lib("v1");
    let gone = fixture.tree.root().join("remotes").join("gone.git");
    fixture.git(
        "lib",
        &["remote", "set-url", "origin", &gone.to_string_lossy()],
    );
    let v1 = fixture.commit("lib", "v1");

    let (events, ended) = fixture.events(&[]);

    succeeded(&ended);
    assert_eq!(
        events,
        [
            started(),
            synced("lib", "re_pinned", &v1[..8]),
            completed(true)
        ]
    );
    assert!(
        ended.stderr.starts_with(
            "Fetch failed for standalone lib: fetch failed for lib\n  $ git fetch origin\n"
        ),
        "{}",
        ended.stderr
    );
    assert_eq!(fixture.head("lib"), v1);
}

#[test]
fn a_malformed_lock_ends_the_run_as_the_configuration_error_and_changes_nothing() {
    let fixture = Fixture::new(
        &config(&[("lib", Some("v1")), ("other", Some("v1"))]),
        &["lib", "other"],
    );
    let malformed = "version = 1\n[[repo]]\nname = \n";
    fixture.write(LOCK, malformed);
    let heads = [fixture.head("lib"), fixture.head("other")];

    let (events, ended) = fixture.events(&[]);

    assert!(
        matches!(ended.termination, Termination::Config(_)),
        "{:?}",
        ended.termination
    );
    assert_eq!(ended.termination.exit_code(), 1);
    assert_eq!(events, [started()]);
    assert_eq!([fixture.head("lib"), fixture.head("other")], heads);
    assert_eq!(fixture.lock().as_deref(), Some(malformed));
}

/// `lib-a`, `lib-b`, and `Lib-a`, each pinned to `v1` and cloned, and `free`, which declares no ref.
fn several() -> Fixture {
    Fixture::new(
        &config(&[
            ("lib-a", Some("v1")),
            ("free", None),
            ("lib-b", Some("v1")),
            ("Lib-a", Some("v1")),
        ]),
        &["lib-a", "lib-b", "Lib-a"],
    )
}

#[test]
fn an_unknown_literal_and_an_unpinned_literal_are_repo_errors_before_any_event() {
    let fixture = several();

    for (literal, expected, text) in [
        (
            "nope",
            RepoMessage::NoPinnedNamed {
                name: "nope".to_owned(),
            },
            "no pinned standalone repo named 'nope'",
        ),
        (
            "free",
            RepoMessage::NotPinned {
                name: "free".to_owned(),
            },
            "standalone repo 'free' has no `ref` configured — nothing to update",
        ),
    ] {
        for mode in [&[][..], &["--json"][..]] {
            let termination = fixture
                .update(&[&[literal, "lib-a"], mode].concat())
                .wrote_nothing(1, literal);

            let Termination::Repo(error) = &termination else {
                panic!("{literal}: {termination:?}");
            };
            assert_eq!(error.message, expected);
            assert_eq!(error.cwd, None, "{literal}");
            let diagnostic = termination.diagnostic().expect("a diagnostic");
            assert!(diagnostic.contains(text), "{diagnostic}");
        }
    }
    assert_eq!(fixture.lock(), None);
}

#[test]
fn a_glob_that_matches_nothing_writes_nothing_to_update_and_succeeds() {
    let fixture = several();

    let ended = fixture.update(&["zzz*"]);

    succeeded(&ended);
    assert_eq!(
        ended.stdout,
        "→ pulling\n\n✓ pull complete\nNothing to update\n"
    );
    assert_eq!(ended.stderr, "");

    let (events, ended) = fixture.events(&["zzz*"]);
    succeeded(&ended);
    assert_eq!(events, [started(), completed(true)]);
}

#[test]
fn a_run_whose_every_selected_checkout_is_missing_warns_and_writes_nothing_to_update() {
    let fixture = Fixture::new(&config(&[("lib", Some("v1")), ("gone", Some("v1"))]), &[]);

    let ended = fixture.update(&[]);

    succeeded(&ended);
    assert_eq!(
        ended.stdout,
        "→ pulling\n\n✓ pull complete\nNothing to update\n"
    );
    assert_eq!(
        ended.stderr,
        "warning: standalone/lib — missing on disk (run `winter ws init`)\n\
         warning: standalone/gone — missing on disk (run `winter ws init`)\n"
    );
    assert_eq!(fixture.lock(), None);
}

#[test]
fn two_literals_select_exactly_those_two() {
    let fixture = several();
    let untouched = fixture.head("lib-b");

    let (events, ended) = fixture.events(&["Lib-a", "lib-a"]);

    succeeded(&ended);
    assert_eq!(
        pairs(&[("lib-a", "re_pinned"), ("Lib-a", "re_pinned")]),
        results(&events)
    );
    assert_eq!(fixture.head("lib-b"), untouched);
    assert!(fixture.entry("lib-b").is_none());
}

#[test]
fn a_bracket_glob_selects_by_case() {
    let fixture = several();
    let untouched = fixture.head("Lib-a");

    let (events, ended) = fixture.events(&["lib-[ab]"]);

    succeeded(&ended);
    assert_eq!(
        pairs(&[("lib-a", "re_pinned"), ("lib-b", "re_pinned")]),
        results(&events)
    );
    assert_eq!(fixture.head("Lib-a"), untouched);
}

#[test]
fn an_empty_name_and_a_name_with_a_slash_are_refused_before_anything_is_read() {
    // A declared project without a directory would be a notice of the run: the refusal comes first.
    let fixture = Fixture::new(
        "[[project_repository]]\nname = \"api\"\n\n[[standalone_repository]]\nname = \"lib\"\nref = \"v1\"\n",
        &["lib"],
    );
    let head = fixture.head("lib");

    let empty = fixture.update(&["lib", ""]).wrote_nothing(1, "empty");
    assert!(
        matches!(
            &empty,
            Termination::Command(error)
                if matches!(**error, CommandError::Selection(SelectionError::EmptyPattern))
        ),
        "{empty:?}"
    );

    let slash = fixture
        .update(&["a/b", "--json"])
        .wrote_nothing(1, "a slash");
    assert!(
        matches!(
            &slash,
            Termination::Command(error) if matches!(
                &**error,
                CommandError::Selection(SelectionError::NotBareName { pattern }) if pattern == "a/b"
            )
        ),
        "{slash:?}"
    );
    let diagnostic = slash.diagnostic().expect("a diagnostic");
    assert!(diagnostic.contains("'a/b'"), "{diagnostic}");
    assert_eq!(fixture.head("lib"), head);
    assert_eq!(fixture.lock(), None);
}

#[test]
fn force_alone_and_freeze_with_autostash_are_usage_errors_that_change_nothing() {
    let fixture = Fixture::lib("v1");
    let head = fixture.head("lib");
    let config = fixture.read(CONFIG);

    for (args, rule) in [
        (&["--force"][..], "--force only applies with --freeze"),
        (
            &["--freeze", "--autostash"][..],
            "--autostash does not apply with --freeze",
        ),
        (
            &["--force", "--autostash"][..],
            "--force only applies with --freeze",
        ),
    ] {
        let ended = fixture.update(args);

        assert!(
            matches!(ended.termination, Termination::Usage(_)),
            "{args:?}: {:?}",
            ended.termination
        );
        assert_eq!(ended.termination.exit_code(), 2, "{args:?}");
        assert!(
            ended
                .termination
                .diagnostic()
                .expect("a diagnostic")
                .contains(rule),
            "{args:?}"
        );
        assert_eq!(ended.stdout, "", "{args:?}");
    }
    assert_eq!(fixture.head("lib"), head);
    assert_eq!(fixture.read(CONFIG), config);
    assert_eq!(fixture.lock(), None);
}

/// A workspace with `lib` pinned to `v1` and `tools` declared without a ref, both cloned, `tools` followed
/// by a commented block.
fn freezable() -> Fixture {
    Fixture::new(
        "[[standalone_repository]]\nname = \"lib\"\nref = \"v1\"\n\n[[standalone_repository]]\nname = \"tools\"\nurl = \"x\"\n\n# next\n[[standalone_repository]]\nname = \"zed\"\nref = \"v1\"\n",
        &["lib", "tools", "zed"],
    )
}

#[test]
fn freeze_writes_the_head_after_the_last_key_and_records_a_commit_entry() {
    let fixture = freezable();
    let before = fixture.read(CONFIG);
    let head = fixture.head("tools");
    let refs = fixture.git("tools", &["show-ref"]);

    let (events, ended) = fixture.events(&["--freeze"]);

    succeeded(&ended);
    assert_eq!(
        events,
        [
            started(),
            synced("lib", "already_pinned", "v1"),
            synced("tools", "pinned", &head[..8]),
            synced("zed", "already_pinned", "v1"),
            completed(true),
        ]
    );
    assert_eq!(
        fixture.read(CONFIG),
        before.replace("url = \"x\"\n", &format!("url = \"x\"\nref = \"{head}\"\n"))
    );
    assert_eq!(
        fixture.entry("tools"),
        Some(LockEntry {
            name: "tools".to_owned(),
            r#ref: head.clone(),
            kind: RefKind::Commit,
            commit: head.clone(),
        })
    );
    assert!(fixture.entry("lib").is_none());
    assert_eq!(fixture.head("tools"), head);
    assert_eq!(fixture.git("tools", &["show-ref"]), refs);

    let again = fixture.update(&["--freeze", "tools"]);
    succeeded(&again);
    assert_eq!(
        again.stdout,
        format!("→ pulling\n[standalone/tools] already pinned @ {head}\n\n✓ pull complete\n")
    );
}

#[test]
fn freeze_writes_a_standalone_declared_only_in_the_local_file_there() {
    let fixture = Fixture::new("", &["tools"]);
    fixture.write(
        ".winter/config.local.toml",
        "[[standalone_repository]]\nurl = \"git@h:o/tools.git\"\n",
    );
    let head = fixture.head("tools");

    let ended = fixture.update(&["--freeze"]);

    succeeded(&ended);
    assert_eq!(fixture.read(CONFIG), "");
    assert_eq!(
        fixture.read(".winter/config.local.toml"),
        format!("[[standalone_repository]]\nurl = \"git@h:o/tools.git\"\nref = \"{head}\"\n")
    );
}

#[test]
fn freeze_refuses_a_dirty_checkout_unless_forced() {
    let fixture = freezable();
    let before = fixture.read(CONFIG);
    fixture.write("tools/a.txt", "edited\n");

    let ended = fixture.update(&["--freeze", "tools"]);

    reported(&ended);
    assert_eq!(
        ended.stderr,
        "[standalone/tools] refused: 'tools' has uncommitted changes, so its commit does not describe it; commit/stash or pass --force\n\n✗ pull had errors\n"
    );
    assert_eq!(fixture.read(CONFIG), before);
    assert_eq!(fixture.lock(), None);

    let forced = fixture.update(&["--freeze", "--force", "tools"]);

    succeeded(&forced);
    assert!(forced.stdout.contains("[standalone/tools] pinned → "));
    assert_ne!(fixture.read(CONFIG), before);
}

#[test]
fn freeze_refuses_a_missing_checkout_and_a_directory_that_is_no_repository() {
    let fixture = Fixture::new(
        &config(&[("gone", None), ("plain", None), ("tools", None)]),
        &["tools"],
    );
    std::fs::create_dir(fixture.root.join("plain")).expect("a directory");

    let (events, ended) = fixture.events(&["--freeze"]);

    reported(&ended);
    let reasons: Vec<&str> = events
        .iter()
        .filter(|event| event["result"] == "refused")
        .map(|event| event["pin_ref"].as_str().expect("a reason"))
        .collect();
    assert_eq!(reasons.len(), 2, "{events:?}");
    assert_eq!(reasons[0], "'gone' is not cloned (run `winter ws init`)");
    assert!(!reasons[1].contains("not cloned"), "{}", reasons[1]);
    assert_eq!(
        results(&events),
        pairs(&[
            ("gone", "refused"),
            ("plain", "refused"),
            ("tools", "pinned")
        ])
    );
}

#[test]
fn freeze_refuses_a_standalone_that_neither_file_declares_by_a_matching_block() {
    let fixture = Fixture::new(
        "standalone_repository = [{ name = \"tools\" }]\n",
        &["tools"],
    );
    let before = fixture.read(CONFIG);

    let ended = fixture.update(&["--freeze"]);

    reported(&ended);
    assert_eq!(
        ended.stderr,
        "[standalone/tools] refused: 'tools' is not declared in config.toml or config.local.toml\n\n✗ pull had errors\n"
    );
    assert_eq!(fixture.read(CONFIG), before);
}

#[test]
fn freeze_names_an_unknown_literal_before_any_event() {
    let fixture = freezable();

    let termination = fixture
        .update(&["--freeze", "nope"])
        .wrote_nothing(1, "nope");

    let Termination::Repo(error) = &termination else {
        panic!("{termination:?}");
    };
    assert_eq!(
        error.message,
        RepoMessage::NoStandaloneNamed {
            name: "nope".to_owned()
        }
    );
}

#[test]
fn freeze_pins_a_local_only_commit_without_the_network() {
    let fixture = freezable();
    let gone = fixture.tree.root().join("remotes").join("gone.git");
    fixture.git(
        "tools",
        &["remote", "set-url", "origin", &gone.to_string_lossy()],
    );
    fixture.git("tools", &["config", "user.email", "a@b.c"]);
    fixture.git("tools", &["config", "user.name", "n"]);
    fixture.git("tools", &["commit", "--allow-empty", "-m", "local only"]);
    let head = fixture.head("tools");

    let ended = fixture.update(&["--freeze", "tools"]);

    succeeded(&ended);
    assert_eq!(fixture.entry("tools").expect("an entry").commit, head);
    assert_eq!(fixture.head("tools"), head);
}

#[test]
fn freeze_with_a_malformed_lock_writes_the_ref_and_ends_as_the_configuration_error() {
    let fixture = freezable();
    let head = fixture.head("tools");
    fixture.write(LOCK, "version = 1\n[[repo]]\nname = \n");

    let (events, ended) = fixture.events(&["--freeze", "tools"]);

    assert!(
        matches!(ended.termination, Termination::Config(_)),
        "{:?}",
        ended.termination
    );
    assert_eq!(ended.termination.exit_code(), 1);
    assert_eq!(events, [started()]);
    assert!(fixture.read(CONFIG).contains(&format!("ref = \"{head}\"")));
}

#[test]
fn freeze_writes_the_drift_notice_before_its_first_row() {
    let fixture = Fixture::new(
        "[[project_repository]]\nname = \"api\"\n\n[[standalone_repository]]\nname = \"lib\"\n",
        &["lib"],
    );
    let mut stdout = RefusingAfter {
        allowed: 0,
        asked: 0,
    };

    let stopped = fixture
        .invocation(&["--freeze"])
        .run_with_stdout(&mut stdout);

    assert_eq!(stdout.asked, 1);
    assert_eq!(
        stopped.stderr,
        "warning: Config and projects/ have drifted — missing from projects/: api\n"
    );
}

#[test]
fn a_stdout_that_fails_after_the_first_event_stops_the_run_and_the_stash_is_popped() {
    let fixture = Fixture::new(
        &config(&[("lib", Some("v1")), ("other", Some("v1"))]),
        &["lib", "other"],
    );
    let other = fixture.head("other");
    fixture.write("lib/keep.txt", "edited\n");
    let mut stdout = RefusingAfter {
        allowed: 1,
        asked: 0,
    };

    let ended = fixture
        .invocation(&["--json", "--autostash"])
        .run_with_stdout(&mut stdout);

    assert!(
        matches!(ended.termination, Termination::Internal(_)),
        "{:?}",
        ended.termination
    );
    assert_eq!(ended.termination.exit_code(), 1);
    assert_eq!(stdout.asked, 2, "no event after the one that failed");
    assert_eq!(fixture.read("lib/keep.txt"), "edited\n");
    assert_eq!(fixture.stashes("lib"), "");
    assert_eq!(fixture.head("other"), other, "the run stopped");
}

#[test]
fn a_closed_stdout_stops_the_run_as_a_broken_pipe() {
    struct Closed;

    impl Write for Closed {
        fn write(&mut self, _: &[u8]) -> io::Result<usize> {
            Err(io::Error::from(io::ErrorKind::BrokenPipe))
        }

        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    let fixture = Fixture::lib("v1");
    let head = fixture.head("lib");

    let ended = fixture.invocation(&[]).run_with_stdout(&mut Closed);

    assert!(
        matches!(ended.termination, Termination::BrokenPipe),
        "{:?}",
        ended.termination
    );
    assert_eq!(fixture.head("lib"), head);
    assert_eq!(fixture.lock(), None);
}

#[test]
fn the_text_rows_of_a_re_pin_and_of_an_up_to_date_run_are_on_stdout() {
    let fixture = Fixture::lib("v1");
    let v1 = fixture.commit("lib", "v1");

    let first = fixture.update(&[]);

    succeeded(&first);
    assert_eq!(
        first.stdout,
        format!(
            "→ pulling\n[standalone/lib] re-pinned → {}\n\n✓ pull complete\n",
            &v1[..8]
        )
    );
    assert_eq!(first.stderr, "");

    let second = fixture.update(&[]);

    succeeded(&second);
    assert_eq!(
        second.stdout,
        "→ pulling\n[standalone/lib] up to date\n\n✓ pull complete\n"
    );
    assert_eq!(second.stderr, "");
}

#[test]
fn the_text_rows_of_a_pin_error_and_of_a_failed_run_are_on_stderr() {
    let fixture = Fixture::lib("v1");
    fixture.write("lib/keep.txt", "edited\n");

    let ended = fixture.update(&[]);

    reported(&ended);
    assert_eq!(ended.stdout, "→ pulling\n");
    assert_eq!(
        ended.stderr,
        format!("[standalone/lib] pin error: {DIRTY_REFUSAL}\n\n✗ pull had errors\n")
    );
}

#[test]
fn the_json_rows_of_a_re_pin_and_of_an_up_to_date_run_are_events() {
    let fixture = Fixture::lib("v1");
    let v1 = fixture.commit("lib", "v1");

    let (first, ended) = fixture.events(&[]);

    succeeded(&ended);
    assert_eq!(ended.stderr, "");
    assert_eq!(
        first,
        [
            started(),
            synced("lib", "re_pinned", &v1[..8]),
            completed(true)
        ]
    );

    let (second, ended) = fixture.events(&[]);

    succeeded(&ended);
    assert_eq!(ended.stderr, "");
    assert_eq!(
        second,
        [started(), synced("lib", "up_to_date", ""), completed(true)]
    );
}

#[test]
fn the_notice_of_a_missing_project_precedes_the_first_row() {
    let fixture = Fixture::new(
        "[[project_repository]]\nname = \"api\"\n\n[[standalone_repository]]\nname = \"lib\"\nref = \"v1\"\n",
        &["lib"],
    );
    let notice = "warning: Config and projects/ have drifted — missing from projects/: api\n";

    for mode in [&[][..], &["--json"][..]] {
        // A stdout that refuses its first write stops the run there, so what stderr holds came before it.
        let mut stdout = RefusingAfter {
            allowed: 0,
            asked: 0,
        };
        let stopped = fixture.invocation(mode).run_with_stdout(&mut stdout);

        assert_eq!(stdout.asked, 1, "{mode:?}");
        assert_eq!(stopped.stderr, notice, "{mode:?}");
    }

    let ended = fixture.update(&[]);
    succeeded(&ended);
    assert_eq!(ended.stderr, notice);
    assert!(ended.stdout.starts_with("→ pulling\n"), "{}", ended.stdout);
}
