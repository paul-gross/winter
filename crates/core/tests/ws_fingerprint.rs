//! `ws fingerprint`: the digest of fixture workspaces built from the host's git, what it changes under and
//! what it does not, that it writes nothing of the workspace and makes no network call, and the git calls it
//! makes, from a stand-in that records them.

use std::collections::BTreeMap;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use serde_json::Value;
use winter_workspace_core::{GitOperation, RepoMessage, Termination};
use winter_workspace_test_support::{CommitSpec, Git, TempTree, WorkspaceBuilder, snapshot};

#[path = "support/in_process.rs"]
mod in_process;

use in_process::{Ended, InProcess};

const SHARED: &str = "";
const LOCAL: &str = "\
[[standalone_repository]]
name = \"tools\"

[[standalone_repository]]
name = \"docs\"
";
const LOCAL_REORDERED: &str = "\
[[standalone_repository]]
name = \"docs\"

[[standalone_repository]]
name = \"tools\"
";

fn commit(file: &str, content: &str) -> CommitSpec {
    CommitSpec {
        message: format!("add {file}"),
        files: vec![(file.to_owned(), content.as_bytes().to_vec())],
        ..CommitSpec::default()
    }
}

/// A workspace root repository whose standalones `tools` and `docs`, declared in the ignored local
/// configuration, are clones of local bare remotes.
struct Fixture {
    tree: TempTree,
    root: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let tree = TempTree::new().expect("an isolated tree");
        let root = WorkspaceBuilder::new(&tree)
            .config(SHARED)
            .local_config(LOCAL)
            .file("notes.txt", b"notes\n")
            .remote("tools", vec![commit("t.txt", "tools\n")])
            .remote("docs", vec![commit("d.txt", "docs\n")])
            .root_repository(".winter/config.local.toml\n")
            .build()
            .expect("a workspace")
            .root;
        let git = Git::new(&tree).expect("git");
        for name in ["tools", "docs"] {
            git.clone_remote(name, &root.join(name)).expect("a clone");
        }
        drop(git);
        Self { tree, root }
    }

    fn git(&self, repository: &str, args: &[&str]) -> String {
        let cwd = if repository.is_empty() {
            self.root.clone()
        } else {
            self.root.join(repository)
        };
        Git::new(&self.tree)
            .expect("git")
            .run(&cwd, args)
            .expect("git output")
    }

    fn write(&self, relative: &str, content: &str) {
        std::fs::write(self.root.join(relative), content).expect("a file");
    }

    fn fingerprint(&self, args: &[&str]) -> Ended {
        InProcess::new(&[&["ws", "fingerprint"], args].concat())
            .cwd(&self.root)
            .pinned(&self.tree.env())
            .run()
    }

    fn digest(&self) -> String {
        let ended = self.fingerprint(&[]);
        assert!(
            matches!(ended.termination, Termination::Success),
            "{:?}",
            ended.termination
        );
        assert_eq!(ended.stderr, "");
        let line = ended.stdout.strip_suffix('\n').expect("a line");
        assert_eq!(line.len(), 64, "{line}");
        assert!(
            line.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        );
        line.to_owned()
    }

    fn json(&self) -> Value {
        self.fingerprint(&["--json"]).document("fingerprint")
    }
}

#[test]
fn two_clones_of_one_definition_with_different_histories_have_one_digest() {
    let plain = Fixture::new();
    let longer = Fixture::new();
    longer.git("", &["commit", "--allow-empty", "-m", "extra"]);
    longer.git("tools", &["commit", "--allow-empty", "-m", "extra"]);

    assert_eq!(plain.digest(), longer.digest());
    assert_ne!(
        plain.json()["workspace"]["commit"],
        longer.json()["workspace"]["commit"]
    );
}

/// A change to a fixture.
type Change = fn(&Fixture);

#[test]
fn the_digest_changes_under_a_staged_addition_rename_removal_and_a_racy_same_size_edit() {
    let changes: [(&str, Change); 4] = [
        ("a staged addition", |f| {
            f.write("added.txt", "x\n");
            f.git("", &["add", "added.txt"]);
        }),
        ("a staged rename", |f| {
            f.git("", &["mv", "notes.txt", "moved.txt"]);
        }),
        ("a staged removal", |f| {
            f.git("", &["rm", "--cached", "notes.txt"]);
        }),
        ("a racy same-size edit", |f| {
            f.git("", &["add", "notes.txt"]);
            f.write("notes.txt", "other\n");
            let index = std::fs::metadata(f.root.join(".git/index"))
                .and_then(|metadata| metadata.modified())
                .expect("the index time");
            std::fs::File::options()
                .write(true)
                .open(f.root.join("notes.txt"))
                .and_then(|file| file.set_modified(index))
                .expect("a set time");
        }),
    ];
    for (name, change) in changes {
        let f = Fixture::new();
        let before = f.digest();

        change(&f);

        assert_ne!(f.digest(), before, "{name}");
    }
}

#[test]
fn a_staged_edit_makes_the_workspace_dirty() {
    let f = Fixture::new();
    assert_eq!(f.json()["workspace"]["dirty"], false);

    f.write("notes.txt", "edited\n");
    f.git("", &["add", "notes.txt"]);

    assert_eq!(f.json()["workspace"]["dirty"], true);
}

#[test]
fn the_digest_does_not_change_under_what_is_not_tracked_content() {
    let f = Fixture::new();
    let before = f.digest();

    f.write("untracked.txt", "x\n");
    assert_eq!(f.digest(), before, "an untracked file");

    std::fs::write(f.root.join(".winter/config.local.toml"), LOCAL_REORDERED).expect("a file");
    assert_eq!(f.digest(), before, "reordered declarations");

    std::fs::create_dir_all(f.root.join("projects/api")).expect("a directory");
    std::fs::write(f.root.join("projects/api/x.txt"), "x\n").expect("a file");
    assert_eq!(f.digest(), before, "a file under projects");

    std::fs::create_dir_all(f.root.join("alpha/api")).expect("a directory");
    assert_eq!(f.digest(), before, "a new environment");
}

#[test]
fn a_run_with_the_remotes_gone_writes_nothing_of_the_workspace_and_leaves_no_scratch() {
    let f = Fixture::new();
    std::fs::rename(
        f.tree.root().join("remotes"),
        f.tree.root().join("remotes-away"),
    )
    .expect("remotes moved");
    let index = f.root.join(".git/index");
    let bytes = std::fs::read(&index).expect("the index");
    let modified = std::fs::metadata(&index)
        .and_then(|m| m.modified())
        .expect("a time");
    let refs = f.git("", &["show-ref"]);
    let before = snapshot(f.tree.root());

    f.digest();

    assert_eq!(std::fs::read(&index).expect("the index"), bytes);
    assert_eq!(
        std::fs::metadata(&index)
            .and_then(|m| m.modified())
            .expect("a time"),
        modified
    );
    assert_eq!(f.git("", &["show-ref"]), refs);
    assert_eq!(snapshot(f.tree.root()), before);
}

#[test]
fn the_document_carries_the_digest_and_each_part_and_the_human_run_the_same_digest() {
    let f = Fixture::new();

    let document = f.json();

    assert_eq!(document["digest"], f.digest());
    let part = |value: &Value, name: &str| {
        assert_eq!(value["name"], name);
        for key in ["commit", "tree"] {
            assert_eq!(value[key].as_str().map(str::len), Some(40), "{key}");
        }
        assert_eq!(value["dirty"], false);
        assert_eq!(value.as_object().map(|o| o.len()), Some(4));
    };
    part(&document["workspace"], "ws");
    let standalones = document["standalones"].as_array().expect("an array");
    assert_eq!(standalones.len(), 2);
    part(&standalones[0], "docs");
    part(&standalones[1], "tools");
    assert_eq!(
        document["workspace"]["tree"],
        f.git("", &["rev-parse", "HEAD^{tree}"]).trim()
    );
    assert_eq!(f.fingerprint(&[]).stdout, format!("{}\n", f.digest()));
}

#[test]
fn a_declared_standalone_without_a_checkout_ends_the_command_naming_it() {
    let f = Fixture::new();
    std::fs::rename(f.root.join("tools"), f.root.join("tools-away")).expect("moved");

    let termination = f.fingerprint(&[]).wrote_nothing(1, "a missing checkout");

    let Termination::Repo(error) = termination else {
        panic!("{termination:?}");
    };
    assert_eq!(error.cwd.as_deref(), Some(f.root.join("tools").as_path()));
    assert_eq!(
        error.message.to_string(),
        format!(
            "cannot fingerprint tools: {} does not exist",
            f.root.join("tools").display()
        )
    );
}

#[test]
fn a_root_that_is_no_repository_ends_with_the_head_read_failing() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = WorkspaceBuilder::new(&tree)
        .config(SHARED)
        .build()
        .expect("a workspace")
        .root;

    let ended = InProcess::new(&["ws", "fingerprint"])
        .cwd(&root)
        .pinned(&tree.env())
        .run();

    let termination = ended.wrote_nothing(1, "no repository");
    let Termination::Repo(error) = termination else {
        panic!("{termination:?}");
    };
    assert!(matches!(
        error.message,
        RepoMessage::GitFailure {
            operation: GitOperation::Head,
            ..
        }
    ));
}

#[test]
fn outside_a_workspace_the_command_ends_as_a_configuration_error() {
    let tree = TempTree::new().expect("an isolated tree");

    let termination = InProcess::new(&["ws", "fingerprint"])
        .cwd(tree.root())
        .pinned(&tree.env())
        .run()
        .wrote_nothing(1, "no workspace");

    assert!(
        matches!(termination, Termination::Config(_)),
        "{termination:?}"
    );
}

/// A workspace root whose `git` is a stand-in that records each call to `calls/` and answers as a repository
/// whose index file does not exist.
struct Stubbed {
    tree: TempTree,
    root: PathBuf,
}

struct Call {
    env: BTreeMap<String, String>,
    args: Vec<String>,
}

impl Stubbed {
    fn new() -> Self {
        let tree = TempTree::new().expect("an isolated tree");
        let root = WorkspaceBuilder::new(&tree)
            .config(SHARED)
            .build()
            .expect("a workspace")
            .root;
        let calls = tree.root().join("calls");
        let script = format!(
            "#!/bin/sh\n\
             calls='{}'\n\
             n=1\n\
             while ! mkdir \"$calls/$n\" 2>/dev/null; do n=$((n + 1)); done\n\
             env >\"$calls/$n/env\"\n\
             printf '%s\\n' \"$@\" >\"$calls/$n/args\"\n\
             case \"$1 $2\" in\n\
               'rev-parse --git-path') echo missing/index ;;\n\
               'rev-parse HEAD') echo 1111111111111111111111111111111111111111 ;;\n\
             esac\n\
             case \"$1\" in write-tree) echo 2222222222222222222222222222222222222222 ;; esac\n",
            calls.to_str().expect("a plain path")
        );
        let stub = tree.root().join("bin/git");
        std::fs::write(&stub, script).expect("the stand-in");
        std::fs::set_permissions(&stub, std::fs::Permissions::from_mode(0o755)).expect("a mode");
        Self { tree, root }
    }

    fn calls(&self) -> Vec<Call> {
        let directory = self.tree.root().join("calls");
        let made = std::fs::read_dir(&directory).expect("calls/").count();
        let lines = |number: usize, name: &str| -> Vec<String> {
            let bytes = std::fs::read(directory.join(number.to_string()).join(name))
                .expect("a recorded call");
            String::from_utf8(bytes)
                .expect("UTF-8")
                .lines()
                .map(str::to_owned)
                .collect()
        };
        (1..=made)
            .map(|number| Call {
                env: lines(number, "env")
                    .iter()
                    .filter_map(|line| line.split_once('='))
                    .map(|(name, value)| (name.to_owned(), value.to_owned()))
                    .collect(),
                args: lines(number, "args"),
            })
            .collect()
    }
}

#[test]
fn the_scratch_index_reaches_the_seeding_the_staging_and_the_tree_write_alone() {
    let stubbed = Stubbed::new();

    let ended = InProcess::new(&["ws", "fingerprint"])
        .cwd(&stubbed.root)
        .pinned(&stubbed.tree.env())
        .run();

    assert!(
        matches!(ended.termination, Termination::Success),
        "{}",
        ended.stderr
    );
    let calls = stubbed.calls();
    let args: Vec<Vec<&str>> = calls
        .iter()
        .map(|call| call.args.iter().map(String::as_str).collect())
        .collect();
    assert_eq!(
        args,
        [
            vec!["rev-parse", "HEAD"],
            vec!["rev-parse", "--git-path", "index"],
            vec!["read-tree", "HEAD"],
            vec!["add", "-u"],
            vec!["write-tree"],
            vec!["status", "--porcelain", "--untracked-files=no"],
        ]
    );
    let temp = stubbed.tree.root().join("tmp");
    for (call, indexed) in calls.iter().zip([false, false, true, true, true, false]) {
        match (call.env.get("GIT_INDEX_FILE"), indexed) {
            (Some(index), true) => assert!(Path::new(index).starts_with(&temp), "{index}"),
            (None, false) => {}
            (found, _) => panic!("{:?}: {found:?}", call.args),
        }
    }
    assert_eq!(
        calls[5].env.get("GIT_OPTIONAL_LOCKS").map(String::as_str),
        Some("0")
    );
}
