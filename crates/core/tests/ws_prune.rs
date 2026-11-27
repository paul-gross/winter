//! `ws prune` through its command, against the host's git in fixture workspaces: what it removes, what it
//! retains and why, what it asks, and what it leaves of the exclude file.

use std::collections::BTreeMap;
use std::fs;
use std::io::{self, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use serde_json::{Value, json};
use winter_workspace_core::{BootstrapError, CommandError, Termination};
use winter_workspace_test_support::{CommitSpec, Found, Git, TempTree, WorkspaceBuilder, snapshot};

#[path = "support/in_process.rs"]
mod in_process;
use in_process::{Ended, InProcess};

/// One declared project, `api`. Every other entry of `projects/` is undeclared.
const CONFIG: &str = "\
[[project_repository]]
name = 'api'
url = 'https://fixture.invalid/api.git'
";

fn seed() -> Vec<CommitSpec> {
    vec![CommitSpec {
        message: "seed".into(),
        files: vec![("README".into(), b"fixture\n".to_vec())],
        ..CommitSpec::default()
    }]
}

/// A workspace that declares `api` and holds a clone of it and of each of `undeclared` under `projects/`.
fn fixture(undeclared: &[&str], git_root: bool) -> TempTree {
    let tree = TempTree::new().expect("an isolated tree");
    let mut builder = WorkspaceBuilder::new(&tree)
        .config(CONFIG)
        .remote("api", seed())
        .project("api");
    for name in undeclared {
        builder = builder.remote(name, seed()).project(name);
    }
    if git_root {
        builder = builder.root_repository("");
    }
    builder.build().expect("a workspace");
    tree
}

fn root(tree: &TempTree) -> PathBuf {
    tree.workspace_dir()
}

fn prune(tree: &TempTree, options: &[&str], stdin: &str) -> Ended {
    InProcess::new(&[&["ws", "prune"], options].concat())
        .cwd(&root(tree))
        .pinned(&tree.env())
        .stdin(stdin.as_bytes())
        .run()
}

fn git(tree: &TempTree, path: &Path, args: &[&str]) -> String {
    Git::new(tree)
        .expect("git")
        .run(path, args)
        .expect("fixture git")
}

/// A clone of a new remote `name` at `relative` of the workspace.
fn clone_at(tree: &TempTree, name: &str, relative: &str) -> PathBuf {
    let git = Git::new(tree).expect("git");
    git.bare_remote(name, "main", &seed()).expect("a remote");
    let path = root(tree).join(relative);
    git.clone_remote(name, &path).expect("a clone");
    path
}

/// Writes `blocks`, each a name and its lines, as managed blocks of the exclude file of the workspace root,
/// after a line of the user's.
fn write_blocks(tree: &TempTree, blocks: &[(&str, &[&str])]) {
    let mut text = String::from("user-line\n");
    for (name, lines) in blocks {
        text.push_str(&format!("# >>> {name} (managed by winter)\n"));
        for line in *lines {
            text.push_str(&format!("{line}\n"));
        }
        text.push_str(&format!("# <<< {name}\n"));
    }
    let info = root(tree).join(".git/info");
    fs::create_dir_all(&info).expect("info/");
    fs::write(info.join("exclude"), text).expect("the exclude file");
}

fn exclude(tree: &TempTree) -> String {
    fs::read_to_string(root(tree).join(".git/info/exclude")).expect("the exclude file")
}

/// The workspace as a test compares it before and after: every entry but the index of a repository, which
/// git may rewrite when it is asked for a status.
fn held(tree: &TempTree) -> BTreeMap<PathBuf, Found> {
    let mut found = snapshot(&root(tree));
    found.retain(|path, _| path.file_name().is_none_or(|name| name != "index"));
    found
}

fn assert_success(ended: &Ended, subject: &str) {
    assert!(
        matches!(ended.termination, Termination::Success),
        "{subject}: {:?}",
        ended.termination
    );
    assert_eq!(ended.stderr, "", "{subject}");
}

/// The lines of stdout under `--json`, each parsed.
fn json_lines(ended: &Ended) -> Vec<Value> {
    assert_success(ended, "a JSON run");
    assert!(ended.stdout.ends_with('\n'), "{:?}", ended.stdout);
    ended
        .stdout
        .lines()
        .map(|line| serde_json::from_str(line).expect("one JSON value per line"))
        .collect()
}

fn preview_line(tree: &TempTree, mark: char, kind: &str, relative: &str, evidence: &str) -> String {
    format!(
        "{mark} {kind:<18} {}   [{evidence}]\n",
        root(tree).join(relative).display()
    )
}

#[test]
fn an_undeclared_clean_clone_is_removed_after_a_yes() {
    let tree = fixture(&["old"], true);
    assert_eq!(
        prune(&tree, &["--dry-run"], "").announced,
        Some(vec!["ws".to_owned(), "prune".to_owned()])
    );

    let ended = prune(&tree, &[], "y\n");

    assert_success(&ended, "a confirmed run");
    assert_eq!(
        ended.stdout,
        format!(
            "{}Remove 1 orphan(s)? [y/N]:   remove projects/old\n",
            preview_line(&tree, ' ', "project_clone", "projects/old", "projects/")
        )
    );
    assert!(!root(&tree).join("projects/old").exists());
    assert!(root(&tree).join("projects/api/README").is_file());

    let again = prune(&tree, &[], "");
    assert_success(&again, "a workspace with nothing left");
    assert_eq!(again.stdout, "Nothing to prune. Workspace is clean.\n");
}

#[test]
fn a_declined_or_unanswered_prompt_exits_1_and_removes_nothing() {
    for answer in ["n\n", "\n", "", "what\nno\n"] {
        let tree = fixture(&["old"], true);
        let before = held(&tree);

        let ended = prune(&tree, &[], answer);

        assert!(
            matches!(
                ended.termination,
                Termination::Aborted { interrupted: false }
            ),
            "{answer:?}: {:?}",
            ended.termination
        );
        assert_eq!(ended.termination.exit_code(), 1, "{answer:?}");
        assert!(
            ended.stdout.ends_with("Remove 1 orphan(s)? [y/N]: "),
            "{answer:?}: {:?}",
            ended.stdout
        );
        assert!(!ended.stdout.contains("remove projects"), "{answer:?}");
        assert_eq!(held(&tree), before, "{answer:?}");
        assert!(root(&tree).join("projects/old/README").is_file());
    }
}

/// A workspace whose undeclared entries of `projects/` each hold something a removal would lose, in the
/// order of their names, with the note each is retained under.
fn retained_fixture() -> (TempTree, Vec<(&'static str, &'static str)>) {
    let tree = fixture(&["ahead", "dirty", "stash", "trees"], true);
    let projects = root(&tree).join("projects");
    let helper = Git::new(&tree).expect("git");

    let commit = CommitSpec {
        message: "local".into(),
        files: vec![("work.txt".into(), b"work\n".to_vec())],
        ..CommitSpec::default()
    };
    helper
        .commit(&projects.join("ahead"), &commit)
        .expect("a local commit");
    fs::write(projects.join("dirty/untracked.txt"), "work\n").expect("an untracked file");
    // The file counts even where the repository is set to hide untracked files from a status.
    git(
        &tree,
        &projects.join("dirty"),
        &["config", "status.showUntrackedFiles", "no"],
    );
    fs::create_dir(projects.join("plain")).expect("a directory");
    fs::write(projects.join("plain/notes.txt"), "work\n").expect("a file");
    fs::write(projects.join("stash/README"), "changed\n").expect("a change");
    git(&tree, &projects.join("stash"), &["stash"]);
    helper
        .add_worktree(
            &projects.join("trees"),
            &root(&tree).join("env/trees"),
            "env",
            "origin/main",
        )
        .expect("a linked worktree");

    let notes = vec![
        ("ahead", "has commits no remote-tracking branch holds"),
        ("dirty", "uncommitted or untracked changes"),
        ("plain", "not a git clone (delete by hand if intentional)"),
        ("stash", "has stashes"),
        ("trees", "has linked worktrees"),
    ];
    (tree, notes)
}

#[test]
fn a_clone_that_could_hold_work_is_retained_with_its_note_and_nothing_is_asked() {
    let (tree, notes) = retained_fixture();
    let before = held(&tree);
    let mut preview = String::new();
    for (name, note) in &notes {
        let relative = format!("projects/{name}");
        preview.push_str(&preview_line(
            &tree,
            '!',
            "project_clone",
            &relative,
            "projects/",
        ));
        preview.push_str(&format!("  ({note})\n"));
    }

    let ended = prune(&tree, &[], "y\n");

    assert_success(&ended, "a run with every orphan blocked");
    assert_eq!(
        ended.stdout,
        format!(
            "{preview}Nothing to remove (all orphans are blocked). Resolve the notes above and re-run.\n"
        )
    );
    assert_eq!(held(&tree), before);
}

#[test]
fn a_clone_that_could_hold_work_is_retained_under_force_too() {
    let (tree, notes) = retained_fixture();
    let before = held(&tree);

    let ended = prune(&tree, &["--force"], "");

    assert_success(&ended, "a forced run");
    for (name, note) in &notes {
        assert!(
            ended
                .stdout
                .contains(&format!("  skip   projects/{name} ({note})\n")),
            "{name}: {}",
            ended.stdout
        );
    }
    assert!(!ended.stdout.contains("[y/N]"), "{}", ended.stdout);
    assert!(!ended.stdout.contains("  remove "), "{}", ended.stdout);
    assert_eq!(held(&tree), before);

    let document = prune(&tree, &["--json", "--force"], "").document("forced JSON");
    assert_eq!(document["excludes_updated"], false);
    let orphans = document["orphans"].as_array().expect("orphans");
    assert_eq!(orphans.len(), notes.len());
    for (orphan, (name, note)) in orphans.iter().zip(&notes) {
        assert_eq!(
            orphan,
            &json!({
                "kind": "project_clone", "path": root(&tree).join("projects").join(name),
                "safe_to_remove": false, "notes": note, "action": "skipped",
            })
        );
    }
    assert_eq!(held(&tree), before);
}

#[test]
fn a_directory_whose_git_directory_is_no_repository_is_retained_in_a_clean_git_root() {
    let tree = fixture(&[], true);
    let broken = root(&tree).join("projects/broken");
    fs::create_dir_all(broken.join(".git")).expect("an empty .git");
    fs::write(broken.join("notes.txt"), "work\n").expect("a file");
    write_blocks(&tree, &[("winter-dir/projects", &["/projects/"])]);
    assert_eq!(git(&tree, &root(&tree), &["status", "--porcelain"]), "");

    let ended = prune(&tree, &["--force"], "");

    assert_success(&ended, "a forced run");
    assert!(
        ended.stdout.contains(
            "  skip   projects/broken (not a git clone (delete by hand if intentional))\n"
        ),
        "{}",
        ended.stdout
    );
    assert!(broken.join("notes.txt").is_file());
}

#[test]
fn a_stale_block_directory_without_git_is_retained_and_its_block_survives_the_refresh() {
    let tree = fixture(&["old"], true);
    fs::create_dir(root(&tree).join("gone")).expect("a directory");
    fs::write(root(&tree).join("gone/notes.txt"), "work\n").expect("a file");
    clone_at(&tree, "dead", "dead");
    write_blocks(&tree, &[("gone", &["/gone/"]), ("dead", &["/dead/"])]);

    let ended = prune(&tree, &["--force"], "");

    assert_success(&ended, "a forced run");
    assert_eq!(
        ended.stdout,
        [
            preview_line(&tree, ' ', "project_clone", "projects/old", "projects/"),
            preview_line(&tree, '!', "standalone_clone", "gone", "block gone"),
            "  (not a git clone (delete by hand if intentional))\n".to_owned(),
            preview_line(&tree, ' ', "standalone_clone", "dead", "block dead"),
            "  remove projects/old\n".to_owned(),
            "  skip   gone (not a git clone (delete by hand if intentional))\n".to_owned(),
            "  remove dead\n".to_owned(),
            "[winter-extensions] workspace excludes updated: cleared\n".to_owned(),
        ]
        .concat()
    );
    assert!(root(&tree).join("gone/notes.txt").is_file());
    assert!(!root(&tree).join("dead").exists());
    assert!(!root(&tree).join("projects/old").exists());
    let exclude = exclude(&tree);
    assert!(
        exclude.contains("# >>> gone (managed by winter)\n/gone/\n# <<< gone\n"),
        "{exclude}"
    );
    assert!(!exclude.contains("dead"), "{exclude}");
    assert!(exclude.starts_with("user-line\n"), "{exclude}");
}

#[test]
fn a_declared_checkout_or_a_directory_that_holds_one_is_no_candidate() {
    let tree = fixture(&[], true);
    let standalone = "[[standalone_repository]]\nname = 'lib'\nurl = 'https://fixture.invalid/lib.git'\npath = 'holder/lib'\n";
    fs::write(
        root(&tree).join(".winter/config.toml"),
        format!("{CONFIG}\n{standalone}"),
    )
    .expect("the configuration");
    let held = clone_at(&tree, "lib", "holder/lib");
    write_blocks(
        &tree,
        &[
            ("api", &["/projects/api/"]),
            ("renamed", &["/holder/lib/", "/holder/"]),
        ],
    );

    let ended = prune(&tree, &["--force"], "");

    assert_success(&ended, "a forced run");
    assert_eq!(ended.stdout, "Nothing to prune. Workspace is clean.\n");
    assert!(root(&tree).join("projects/api/.git").is_dir());
    assert!(held.join(".git").is_dir());
}

#[test]
fn a_block_line_that_resolves_outside_the_root_is_retained() {
    let tree = fixture(&[], true);
    write_blocks(&tree, &[("ext", &["/../remotes/"])]);
    let outside = tree.root().join("remotes");
    let before = snapshot(&outside);
    assert!(!before.is_empty());

    let ended = prune(&tree, &["--force"], "");

    assert_success(&ended, "a forced run");
    assert_eq!(
        ended.stdout,
        format!(
            "! {:<18} {}   [block ext]\n  (resolves outside the workspace)\n  skip   {} (resolves outside the workspace)\n",
            "standalone_clone",
            outside.display(),
            outside.display()
        )
    );
    assert_eq!(snapshot(&outside), before);
    assert!(exclude(&tree).contains("/../remotes/"));
}

#[test]
fn a_removal_in_a_root_that_is_no_repository_writes_no_exclude_file() {
    let tree = fixture(&["old"], false);

    let document = prune(&tree, &["--json", "--force"], "").document("forced JSON");

    assert_eq!(
        document,
        json!({"dry_run": false, "excludes_updated": true, "orphans": [{
            "kind": "project_clone", "path": root(&tree).join("projects/old"),
            "safe_to_remove": true, "notes": "", "action": "removed",
        }]})
    );
    assert!(!root(&tree).join("projects/old").exists());
    assert!(!root(&tree).join(".git").exists());
}

#[test]
fn removing_a_project_clone_alone_from_a_git_root_reports_the_excludes_updated() {
    let tree = fixture(&["old"], true);

    let document = prune(&tree, &["--json", "--force"], "").document("forced JSON");

    assert_eq!(document["excludes_updated"], true);
    assert_eq!(document["orphans"][0]["action"], "removed");
    assert!(!root(&tree).join("projects/old").exists());
}

#[test]
fn json_alone_is_refused_and_removes_nothing() {
    let tree = fixture(&["old"], true);
    let before = held(&tree);

    let termination = prune(&tree, &["--json"], "y\n").wrote_nothing(1, "--json alone");

    let Termination::Command(error) = termination else {
        panic!("{termination:?}");
    };
    assert!(
        matches!(
            *error,
            CommandError::Bootstrap(BootstrapError::JsonNeedsForce)
        ),
        "{error:?}"
    );
    assert_eq!(held(&tree), before);
}

#[test]
fn a_json_dry_run_gives_each_orphan_the_action_it_would_get_and_removes_nothing() {
    let tree = fixture(&["dirty", "old"], true);
    fs::write(root(&tree).join("projects/dirty/untracked.txt"), "work\n").expect("a file");
    let before = held(&tree);

    for options in [
        &["--json", "--dry-run"][..],
        &["--json", "--dry-run", "--force"][..],
    ] {
        let document = prune(&tree, options, "").document("a JSON dry run");

        assert_eq!(
            document,
            json!({"dry_run": true, "orphans": [
                {"kind": "project_clone", "path": root(&tree).join("projects/dirty"),
                 "safe_to_remove": false, "notes": "uncommitted or untracked changes",
                 "action": "skipped"},
                {"kind": "project_clone", "path": root(&tree).join("projects/old"),
                 "safe_to_remove": true, "notes": "", "action": "would_remove"},
            ]})
        );
        assert_eq!(held(&tree), before);
    }

    let clean = fixture(&[], true);
    assert_eq!(
        prune(&clean, &["--json", "--dry-run"], "").document("no orphans"),
        json!({"dry_run": true, "orphans": []})
    );
    assert_eq!(
        prune(&clean, &["--json", "--force"], "").document("no orphans"),
        json!({"dry_run": false, "orphans": [], "excludes_updated": false})
    );
}

#[test]
fn a_forced_json_run_prints_the_refresh_events_and_then_the_document() {
    let tree = fixture(&[], true);
    clone_at(&tree, "dead", "dead");
    write_blocks(&tree, &[("dead", &["/dead/"])]);

    let lines = json_lines(&prune(&tree, &["--json", "--force"], ""));

    assert_eq!(
        lines,
        [
            json!({
                "type": "repo_action", "repo": "winter-extensions",
                "location": root(&tree).join(".git/info/exclude"),
                "action": "workspace_excludes_updated", "detail": "cleared",
            }),
            json!({"dry_run": false, "excludes_updated": true, "orphans": [{
                "kind": "standalone_clone", "path": root(&tree).join("dead"),
                "safe_to_remove": true, "notes": "", "action": "removed",
            }]}),
        ]
    );
    assert!(!root(&tree).join("dead").exists());
    assert!(!exclude(&tree).contains("dead"));
}

/// The directory of the host's git: the first of the pinned `PATH` outside the tree that holds one.
fn host_git(tree: &TempTree) -> PathBuf {
    let env = tree.env();
    let (_, path) = env
        .variables()
        .find(|(name, _)| *name == "PATH")
        .expect("a pinned PATH");
    std::env::split_paths(path)
        .filter(|directory| !directory.starts_with(tree.root()))
        .map(|directory| directory.join("git"))
        .find(|git| git.is_file())
        .expect("the host's git")
}

#[test]
fn a_dry_run_in_a_linked_worktree_root_writes_no_git_configuration_and_removes_nothing() {
    let tree = TempTree::new().expect("an isolated tree");
    let helper = Git::new(&tree).expect("git");
    let base = clone_at(&tree, "base", "base");
    let linked = root(&tree).join("linked");
    helper
        .add_worktree(&base, &linked, "linked", "origin/main")
        .expect("a linked worktree");
    fs::create_dir(linked.join(".winter")).expect(".winter/");
    fs::write(linked.join(".winter/config.toml"), CONFIG).expect("a configuration");
    clone_at(&tree, "old", "linked/projects/old");
    clone_at(&tree, "dead", "linked/dead");
    let git_directory = PathBuf::from(
        git(&tree, &linked, &["rev-parse", "--absolute-git-dir"]).trim_end_matches('\n'),
    );
    fs::create_dir_all(git_directory.join("info")).expect("info/");
    fs::write(
        git_directory.join("info/exclude"),
        "# >>> dead (managed by winter)\n/dead/\n# <<< dead\n",
    )
    .expect("the exclude file");

    let calls = tree.root().join("calls/git");
    let stub = tree.root().join("bin/git");
    fs::write(
        &stub,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{}'\nexec '{}' \"$@\"\n",
            calls.display(),
            host_git(&tree).display()
        ),
    )
    .expect("the stand-in");
    fs::set_permissions(&stub, fs::Permissions::from_mode(0o755)).expect("a mode");
    let workspace = |tree: &TempTree| {
        let mut found = snapshot(&root(tree));
        found.retain(|path, _| path.file_name().is_none_or(|name| name != "index"));
        found
    };
    let before = workspace(&tree);

    let ended = InProcess::new(&["ws", "prune", "--dry-run"])
        .cwd(&linked)
        .pinned(&tree.env())
        .run();

    assert_success(&ended, "a dry run");
    assert_eq!(
        ended.stdout,
        format!(
            "  {:<18} {}   [projects/]\n  {:<18} {}   [block dead]\n",
            "project_clone",
            linked.join("projects/old").display(),
            "standalone_clone",
            linked.join("dead").display()
        )
    );
    let recorded = fs::read_to_string(&calls).expect("the recorded calls");
    assert!(
        recorded.lines().any(|call| call.starts_with("status ")),
        "the stand-in must see the scan's calls: {recorded}"
    );
    let writes: Vec<&str> = recorded
        .lines()
        .filter(|call| call.starts_with("config") && !call.contains("--get"))
        .collect();
    assert_eq!(writes, Vec::<&str>::new(), "{recorded}");
    assert_eq!(workspace(&tree), before);

    // The removal that follows is what gives the worktree an exclude file of its own.
    let forced = InProcess::new(&["ws", "prune", "--force"])
        .cwd(&linked)
        .pinned(&tree.env())
        .run();
    assert_success(&forced, "a forced run");
    assert!(!linked.join("dead").exists());
    let recorded = fs::read_to_string(&calls).expect("the recorded calls");
    assert!(
        recorded
            .lines()
            .any(|call| call.starts_with("config") && !call.contains("--get")),
        "{recorded}"
    );
}

#[test]
fn a_dangling_link_and_an_agent_copy_a_stale_block_names_are_removed() {
    let tree = fixture(&[], true);
    let ws = root(&tree);
    for directory in [".claude/skills", ".claude/agents", "kept-skill"] {
        fs::create_dir_all(ws.join(directory)).expect("a directory");
    }
    std::os::unix::fs::symlink(ws.join("missing"), ws.join(".claude/skills/gone")).expect("a link");
    std::os::unix::fs::symlink(ws.join("kept-skill"), ws.join(".claude/skills/live"))
        .expect("a link");
    for name in ["ext-reviewer.md", "ext-writer.md", "extra.md", "mine.md"] {
        fs::write(ws.join(".claude/agents").join(name), "agent\n").expect("an agent file");
    }
    write_blocks(&tree, &[("ext", &[".claude/agents/ext-*"])]);

    let ended = prune(&tree, &[], "yes\n");

    assert_success(&ended, "a confirmed run");
    assert_eq!(
        ended.stdout,
        [
            preview_line(
                &tree,
                ' ',
                "broken_symlink",
                ".claude/skills/gone",
                "dangling link"
            ),
            preview_line(
                &tree,
                ' ',
                "orphan_agent_copy",
                ".claude/agents/ext-reviewer.md",
                "block ext"
            ),
            preview_line(
                &tree,
                ' ',
                "orphan_agent_copy",
                ".claude/agents/ext-writer.md",
                "block ext"
            ),
            "Remove 3 orphan(s)? [y/N]:   remove .claude/skills/gone\n".to_owned(),
            "  remove .claude/agents/ext-reviewer.md\n".to_owned(),
            "  remove .claude/agents/ext-writer.md\n".to_owned(),
            "[winter-extensions] workspace excludes updated: cleared\n".to_owned(),
        ]
        .concat()
    );
    let left = |directory: &str| {
        let mut names: Vec<String> = fs::read_dir(ws.join(directory))
            .expect("a directory")
            .map(|entry| {
                entry
                    .expect("an entry")
                    .file_name()
                    .into_string()
                    .expect("a name")
            })
            .collect();
        names.sort();
        names
    };
    assert_eq!(left(".claude/skills"), ["live"]);
    assert_eq!(left(".claude/agents"), ["extra.md", "mine.md"]);
    assert!(!exclude(&tree).contains("ext"), "{}", exclude(&tree));
}

#[test]
fn an_exclude_file_that_cannot_be_read_is_warned_of_and_leaves_the_blocks_alone() {
    let tree = fixture(&["old"], true);
    clone_at(&tree, "dead", "dead");
    let info = root(&tree).join(".git/info");
    fs::create_dir_all(&info).expect("info/");
    let bytes = b"# >>> dead (managed by winter)\n/dead/\n# <<< dead\n\xff\n";
    fs::write(info.join("exclude"), bytes).expect("the exclude file");

    let ended = prune(&tree, &["--json", "--force"], "");

    assert!(
        matches!(ended.termination, Termination::Success),
        "{:?}",
        ended.termination
    );
    assert!(
        ended
            .stderr
            .starts_with("warning: the workspace exclude file could not be read"),
        "{}",
        ended.stderr
    );
    let document: Value = serde_json::from_str(&ended.stdout).expect("one document");
    assert_eq!(document["excludes_updated"], false);
    assert_eq!(document["orphans"].as_array().expect("orphans").len(), 1);
    assert_eq!(document["orphans"][0]["action"], "removed");
    assert!(root(&tree).join("dead/README").is_file());
    assert_eq!(fs::read(info.join("exclude")).expect("the file"), bytes);
}

/// A stdout that takes everything but the line of a removal.
struct RefusingRemovals(Vec<u8>);

impl Write for RefusingRemovals {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.starts_with(b"  remove ") {
            return Err(io::Error::from(io::ErrorKind::BrokenPipe));
        }
        self.0.extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

#[test]
fn a_removal_line_that_cannot_be_written_leaves_the_orphans_not_yet_reached() {
    let tree = fixture(&["old", "older"], true);
    let mut stdout = RefusingRemovals(Vec::new());

    let ended = InProcess::new(&["ws", "prune", "--force"])
        .cwd(&root(&tree))
        .pinned(&tree.env())
        .run_with_stdout(&mut stdout);

    assert!(
        matches!(ended.termination, Termination::BrokenPipe),
        "{:?}",
        ended.termination
    );
    assert!(!root(&tree).join("projects/old").exists());
    assert!(root(&tree).join("projects/older/README").is_file());
}

#[test]
fn outside_a_workspace_the_run_ends_as_a_configuration_error() {
    let tree = TempTree::new().expect("an isolated tree");

    let termination = prune(&tree, &["--json"], "").wrote_nothing(1, "no workspace");

    assert!(
        matches!(termination, Termination::Config(_)),
        "{termination:?}"
    );
}
