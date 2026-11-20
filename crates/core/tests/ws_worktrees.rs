//! `ws worktrees`: the locations of a fixture workspace, listed without a change to its tree, and with
//! `--status` the counts git gives for each, from the host's git or from a stand-in that records its calls.

use std::collections::BTreeMap;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;

use serde_json::{Value, json};
use winter_workspace_core::Termination;
use winter_workspace_test_support::{CommitSpec, Git, TempTree, WorkspaceBuilder, snapshot};

#[path = "support/in_process.rs"]
mod in_process;

use in_process::{Ended, InProcess};

const CONFIG: &str = "\
[[project_repository]]
name = \"api\"

[[standalone_repository]]
name = \"ctx\"
path = \".winter/ext/context\"
";

fn worktrees(cwd: &Path, args: &[&str]) -> Ended {
    InProcess::new(&[&["ws", "worktrees"], args].concat())
        .cwd(cwd)
        .run()
}

fn text(path: &Path) -> &str {
    path.to_str().expect("a fixture path of plain characters")
}

#[test]
fn the_document_lists_worktrees_then_standalones_then_the_workspace_without_counts() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = WorkspaceBuilder::new(&tree)
        .config(CONFIG)
        .file("alpha/api/README.md", b"api\n")
        .file(".winter/ext/context/README.md", b"ctx\n")
        .build()
        .expect("a workspace")
        .root;
    let before = snapshot(&root);
    let at = |relative: &str| Value::from(text(&root.join(relative)));
    let base = root.file_name().and_then(|n| n.to_str()).expect("a name");

    let document = worktrees(&root, &["--json"]).document("worktrees");

    assert_eq!(
        document,
        json!([
            {"kind": "worktree", "env": "alpha", "repo": "api", "name": null,
             "label": "alpha/api", "path": at("alpha/api")},
            {"kind": "standalone", "env": null, "repo": null, "name": "ctx",
             "label": "ctx", "path": at(".winter/ext/context")},
            {"kind": "workspace", "env": null, "repo": null, "name": base,
             "label": "<workspace>", "path": text(&root)},
        ])
    );
    assert_eq!(snapshot(&root), before);
}

#[test]
fn an_alias_whose_project_entry_is_a_symbolic_link_to_a_directory_is_listed() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = WorkspaceBuilder::new(&tree)
        .config(CONFIG)
        .file("elsewhere/README.md", b"x\n")
        .build()
        .expect("a workspace")
        .root;
    std::fs::create_dir(root.join("alpha")).expect("a directory");
    std::os::unix::fs::symlink(root.join("elsewhere"), root.join("alpha/api")).expect("a link");

    let document = worktrees(&root, &["--json"]).document("worktrees");

    assert_eq!(document[0]["label"], "alpha/api");
}

#[test]
fn the_human_rows_carry_the_labels_in_the_order_of_the_document() {
    let tree = TempTree::new().expect("an isolated tree");
    let root = WorkspaceBuilder::new(&tree)
        .config(CONFIG)
        .file("alpha/api/README.md", b"api\n")
        .build()
        .expect("a workspace")
        .root;

    let ended = worktrees(&root, &[]);

    let labels: Vec<&str> = ended
        .stdout
        .lines()
        .map(|line| line.split_whitespace().next().expect("a label"))
        .collect();
    assert_eq!(labels, ["alpha/api", "<workspace>"]);
}

/// What the stand-in writes for a call it answers as a git whose working tree is clean: two commits behind
/// and one ahead for the count of commits, and nothing for every other call.
const ANSWERS: &str = "case \"$1\" in rev-list) printf '2\\t1\\n' ;; esac";

/// What the stand-in does for a call it fails as git fails outside a repository.
const FAILS: &str = "echo 'fatal: the stand-in fails' >&2\nexit 128";

/// The value `GIT_SSH_COMMAND` gets when the invocation's environment does not hold the variable.
const SSH_DEFAULT: &str =
    "ssh -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3";

/// A fixture workspace whose worktree `alpha/api` is a repository by its `.git` file alone, and a stand-in
/// `git` first on the pinned `PATH` of its tree.
///
/// The stand-in writes its environment, its arguments, its working directory, and what it reads from stdin
/// to a directory of its own under `calls/`, numbered from 1 in the order of the calls, and then runs
/// `ending`.
struct Stubbed {
    tree: TempTree,
    root: std::path::PathBuf,
}

/// One call the stand-in recorded.
struct Call {
    env: BTreeMap<String, String>,
    args: Vec<String>,
    cwd: String,
    stdin: Vec<u8>,
}

impl Stubbed {
    fn new(ending: &str) -> Self {
        let tree = TempTree::new().expect("an isolated tree");
        let root = WorkspaceBuilder::new(&tree)
            .config(CONFIG)
            .file("alpha/api/.git", b"gitdir: /nowhere\n")
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
             pwd -P >\"$calls/$n/cwd\"\n\
             cat >\"$calls/$n/stdin\"\n\
             {ending}\n",
            text(&calls)
        );
        let stub = tree.root().join("bin/git");
        std::fs::write(&stub, script).expect("the stand-in");
        std::fs::set_permissions(&stub, std::fs::Permissions::from_mode(0o755)).expect("a mode");
        Self { tree, root }
    }

    /// `ws worktrees` with `args` after it and `root_args` before it, in the workspace, under the pinned
    /// environment of the tree with `variables` added, and with a stdin that holds a line.
    fn run(&self, root_args: &[&str], args: &[&str], variables: &[(&str, &str)]) -> Ended {
        let mut invocation = InProcess::new(&[root_args, &["ws", "worktrees"], args].concat())
            .cwd(&self.root)
            .pinned(&self.tree.env())
            .stdin(b"a line the command never reads\n");
        for (key, value) in variables {
            invocation = invocation.env(key, value);
        }
        invocation.run()
    }

    /// The calls the stand-in recorded, in order.
    fn calls(&self) -> Vec<Call> {
        let directory = self.tree.root().join("calls");
        let read = |number: usize, name: &str| {
            std::fs::read(directory.join(number.to_string()).join(name)).expect("a recorded call")
        };
        let lines = |bytes: Vec<u8>| -> Vec<String> {
            let text = String::from_utf8(bytes).expect("UTF-8 from the stand-in");
            text.lines().map(str::to_owned).collect()
        };
        let made = std::fs::read_dir(&directory).expect("calls/").count();
        (1..=made)
            .map(|number| Call {
                env: lines(read(number, "env"))
                    .iter()
                    .filter_map(|line| line.split_once('='))
                    .map(|(name, value)| (name.to_owned(), value.to_owned()))
                    .collect(),
                args: lines(read(number, "args")),
                cwd: lines(read(number, "cwd")).concat(),
                stdin: read(number, "stdin"),
            })
            .collect()
    }
}

#[test]
fn git_is_started_in_the_worktree_with_the_snapshot_less_the_launch_time_and_an_empty_stdin() {
    let stubbed = Stubbed::new(ANSWERS);

    let document = stubbed
        .run(
            &[],
            &["--status", "--json"],
            &[
                ("FIXTURE_MARKER", "as given"),
                ("WINTER_LAUNCH_TIME", "1700000000000000000"),
            ],
        )
        .document("worktrees");

    assert_eq!(
        document[0],
        json!({"kind": "worktree", "env": "alpha", "repo": "api", "name": null,
            "label": "alpha/api", "path": text(&stubbed.root.join("alpha/api")),
            "ahead": 1, "behind": 2, "dirty": 0})
    );
    let calls = stubbed.calls();
    let args: Vec<&[String]> = calls.iter().map(|call| call.args.as_slice()).collect();
    assert_eq!(
        args,
        [
            &[
                "status",
                "--porcelain=v2",
                "--branch",
                "--untracked-files=all",
                "-z"
            ][..],
            &["rev-list", "--left-right", "--count", "origin/main...HEAD"],
        ]
    );
    for call in &calls {
        assert_eq!(call.cwd, text(&stubbed.root.join("alpha/api")));
        assert_eq!(call.stdin, b"", "stdin reads empty");
        let value = |name: &str| call.env.get(name).map(String::as_str);
        assert_eq!(value("FIXTURE_MARKER"), Some("as given"));
        assert_eq!(value("WINTER_LAUNCH_TIME"), None);
        assert_eq!(value("GIT_SSH_COMMAND"), Some(SSH_DEFAULT));
        assert_eq!(value("LANGUAGE"), Some("C"));
        assert_eq!(value("LC_ALL"), Some("C"), "the pinned value is replaced");
        assert_eq!(
            value("HOME"),
            Some(text(&stubbed.tree.root().join("home"))),
            "a pinned variable"
        );
    }
}

#[test]
fn the_ssh_command_of_the_snapshot_reaches_git_as_given_the_empty_one_included() {
    for given in ["ssh -v", ""] {
        let stubbed = Stubbed::new(ANSWERS);

        stubbed
            .run(&[], &["--status", "--json"], &[("GIT_SSH_COMMAND", given)])
            .document(given);

        let calls = stubbed.calls();
        assert_eq!(calls.len(), 2, "{given:?}");
        for call in &calls {
            assert_eq!(
                call.env.get("GIT_SSH_COMMAND").map(String::as_str),
                Some(given)
            );
        }
    }
}

#[test]
fn a_git_that_exits_128_ends_the_command_with_the_failure_written_once() {
    let stubbed = Stubbed::new(FAILS);

    for args in [&["--status"][..], &["--status", "--json"]] {
        let termination = stubbed.run(&[], args, &[]).wrote_nothing(1, "a failed git");

        assert!(matches!(termination, Termination::Repo(_)), "{args:?}");
        let diagnostic = termination.diagnostic().expect("a diagnostic");
        assert_eq!(
            diagnostic.matches("status probe failed for api").count(),
            1,
            "{diagnostic}"
        );
        for detail in [
            "  $ git status --porcelain=v2 --branch --untracked-files=all -z\n",
            &format!("  cwd: {}\n", text(&stubbed.root.join("alpha/api"))),
            "  exit 128\n",
            "  stderr: fatal: the stand-in fails",
        ] {
            assert!(diagnostic.contains(detail), "{detail:?} in {diagnostic}");
        }
    }
}

#[test]
fn under_the_verbose_flag_a_failed_git_is_a_log_record_as_well_as_the_ending() {
    let stubbed = Stubbed::new(FAILS);

    let ended = stubbed.run(&["-v"], &["--status", "--json"], &[]);

    assert_eq!(ended.stdout, "");
    let records: Vec<&str> = ended
        .stderr
        .lines()
        .filter(|line| line.contains("status probe failed for api"))
        .collect();
    assert_eq!(
        records,
        ["ERROR locations: status probe failed for api"],
        "{}",
        ended.stderr
    );
    assert!(matches!(ended.termination, Termination::Repo(_)));
    let diagnostic = ended.termination.diagnostic().expect("a diagnostic");
    assert_eq!(
        diagnostic.matches("status probe failed for api").count(),
        1,
        "{diagnostic}"
    );
}

#[test]
fn without_the_status_option_git_is_not_started() {
    let stubbed = Stubbed::new(FAILS);

    let document = stubbed.run(&[], &["--json"], &[]).document("worktrees");

    assert_eq!(document[0]["label"], "alpha/api");
    assert_eq!(document[0].get("ahead"), None);
    assert_eq!(stubbed.calls().len(), 0);
}

#[test]
fn a_path_without_git_ends_as_an_internal_failure_and_writes_nothing() {
    let stubbed = Stubbed::new(ANSWERS);
    let empty = stubbed.tree.root().join("tmp");

    let termination = stubbed
        .run(&[], &["--status", "--json"], &[("PATH", text(&empty))])
        .wrote_nothing(1, "no git");

    assert!(
        matches!(termination, Termination::Internal(_)),
        "{termination:?}"
    );
    assert_eq!(stubbed.calls().len(), 0);
}

#[test]
fn the_counts_of_a_worktree_come_from_the_git_of_the_host() {
    let tree = TempTree::new().expect("an isolated tree");
    let commit = |message: &str, file: &str| CommitSpec {
        message: message.to_owned(),
        files: vec![(file.to_owned(), b"content\n".to_vec())],
        ..CommitSpec::default()
    };
    let root = WorkspaceBuilder::new(&tree)
        .config(CONFIG)
        .remote("api", vec![commit("First", "README.md")])
        .project("api")
        .env_worktree("alpha", "api")
        .build()
        .expect("a workspace")
        .root;
    let worktree = root.join("alpha/api");
    let git = Git::new(&tree).expect("the git of the host");
    git.commit(&worktree, &commit("Ahead", "ahead.txt"))
        .expect("a commit in the worktree");
    git.push_to_remote("api", &commit("Behind", "behind.txt"))
        .expect("a commit on the remote");
    git.run(&worktree, &["fetch", "origin"]).expect("a fetch");
    std::fs::write(worktree.join("README.md"), b"changed\n").expect("a changed file");
    std::fs::create_dir(worktree.join("new")).expect("a directory");
    for name in ["new/one", "new/two"] {
        std::fs::write(worktree.join(name), b"").expect("an untracked file");
    }

    let document = InProcess::new(&["ws", "worktrees", "--status", "--json"])
        .cwd(&root)
        .pinned(&tree.env())
        .run()
        .document("worktrees");

    assert_eq!(
        document,
        json!([
            {"kind": "worktree", "env": "alpha", "repo": "api", "name": null,
             "label": "alpha/api", "path": text(&worktree),
             "ahead": 1, "behind": 1, "dirty": 3},
            {"kind": "workspace", "env": null, "repo": null,
             "name": root.file_name().and_then(|name| name.to_str()),
             "label": "<workspace>", "path": text(&root),
             "ahead": null, "behind": null, "dirty": null},
        ])
    );
}
