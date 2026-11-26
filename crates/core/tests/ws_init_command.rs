//! Bare reconciliation through its public command, with isolated repositories and pinned children.
use std::fs;
use std::io::{self, Write};
use std::os::unix::fs::MetadataExt;

use serde_json::{Value, json};
use winter_workspace_core::Termination;
use winter_workspace_test_support::{CommitSpec, Git, TempTree, WorkspaceBuilder, snapshot};

#[path = "support/in_process.rs"]
mod in_process;
use in_process::{Ended, InProcess};

const CONFIG: &str = ".winter/config.toml";
const EXCLUDE: &str = ".git/info/exclude";
const SAFETY: &str = "<!-- >>> winter-workspace/safety (managed by winter) -->\nold\n<!-- <<< winter-workspace/safety -->\n";
const BOTH: &str = "\
prefix = 'custom'
[[project_repository]]
name = 'api'
url = 'https://fixture.invalid/api.git'
cmd = [\"printf 'setup\\n'; printf 'setup\\n' >> runs\"]
[[standalone_repository]]
name = 'tools'
url = 'https://fixture.invalid/tools.git'
";

fn fixture(config: &str, git_root: bool, extension: bool) -> TempTree {
    let tree = TempTree::new().expect("isolated tree");
    let mut builder = WorkspaceBuilder::new(&tree).config(config);
    for name in ["api", "tools", "healthy"] {
        let mut files = vec![("README".into(), b"fixture\n".to_vec())];
        if extension && name == "api" {
            files.push(("winter-ext.toml".into(), Vec::new()));
        }
        builder = builder.remote(
            name,
            vec![CommitSpec {
                message: "seed".into(),
                files,
                tag: Some("v1".into()),
                ..CommitSpec::default()
            }],
        );
    }
    if git_root {
        builder = builder.root_repository("");
    }
    builder.build().expect("workspace");
    tree
}

fn invocation(tree: &TempTree, args: &[&str]) -> InProcess {
    InProcess::new(args)
        .cwd(&tree.workspace_dir())
        .pinned(&tree.env())
}

fn run(tree: &TempTree, json: bool) -> Ended {
    invocation(
        tree,
        if json {
            &["ws", "init", "--json"]
        } else {
            &["ws", "init"]
        },
    )
    .run()
}

fn read(tree: &TempTree, path: &str) -> String {
    fs::read_to_string(tree.workspace_dir().join(path)).expect("fixture file")
}

fn write(tree: &TempTree, path: &str, content: &str) {
    fs::write(tree.workspace_dir().join(path), content).expect("fixture file");
}

fn events(ended: &Ended, exit: u8) -> Vec<Value> {
    assert_eq!(
        ended.termination.exit_code(),
        exit,
        "{:?}",
        ended.termination
    );
    assert_eq!(ended.stderr, "");
    assert!(ended.stdout.ends_with('\n'));
    ended
        .stdout
        .lines()
        .map(|line| serde_json::from_str(line).expect("one complete JSON event per line"))
        .collect()
}

fn action(events: &[Value], repo: &str, action: &str) -> bool {
    events.iter().any(|event| {
        event["type"] == "repo_action" && event["repo"] == repo && event["action"] == action
    })
}

fn git(tree: &TempTree, path: &str, args: &[&str]) -> String {
    Git::new(tree)
        .expect("git")
        .run(&tree.workspace_dir().join(path), args)
        .expect("fixture git")
}

#[test]
fn first_json_run_and_text_repeat_preserve_excludes_and_rerun_commands() {
    let tree = fixture(BOTH, true, false);
    let first = run(&tree, true);
    let rows = events(&first, 0);
    assert_eq!(first.announced, Some(vec!["ws".into(), "init".into()]));
    let targets: Vec<_> = rows
        .iter()
        .filter(|e| e["type"] == "target_started" || e["type"] == "target_completed")
        .cloned()
        .collect();
    assert_eq!(
        targets,
        [
            json!({"type":"target_started","target":"projects/"}),
            json!({"type":"target_completed","target":"projects/","success":true}),
            json!({"type":"target_started","target":"standalone/"}),
            json!({"type":"target_completed","target":"standalone/","success":true}),
        ]
    );
    for (name, path) in [("api", "projects/api"), ("tools", "tools")] {
        assert!(rows.contains(&json!({
            "type":"repo_action", "repo":name,
            "location":tree.workspace_dir().join(path),
            "action":"cloned", "detail":""
        })));
        assert!(!git(&tree, path, &["rev-parse", "--verify", "HEAD"]).is_empty());
    }
    assert!(rows.contains(&json!({"type":"cmd_output_line","repo":"api","line":"setup"})));
    assert!(
        rows.iter()
            .any(|e| e["type"] == "cmd_completed" && e["returncode"] == 0)
    );
    let before = read(&tree, EXCLUDE);
    for block in [
        "winter-dir/projects",
        "winter-workspace/artifacts",
        "winter-workspace/workspace-skills",
        "winter-config",
        "tools",
    ] {
        assert!(
            before.contains(&format!("# >>> {block} (managed by winter)")),
            "{before}"
        );
    }
    for line in [
        "/projects/",
        "/.winter/logs/",
        ".claude/skills/custom-*",
        ".codex/skills/custom",
        ".opencode/skill/custom-*",
        ".winter/config/**/*.local.*",
        "/tools/",
    ] {
        assert!(
            before.lines().any(|candidate| candidate == line),
            "{before}"
        );
    }
    let metadata = fs::metadata(tree.workspace_dir().join(EXCLUDE)).expect("exclude metadata");
    let second = run(&tree, false);
    assert!(matches!(second.termination, Termination::Success));
    assert_eq!(second.stderr, "");
    for line in [
        "→ projects/",
        "✓ projects/ done",
        "→ standalone/",
        "✓ standalone/ done",
        "[api] setup",
    ] {
        assert!(
            second.stdout.lines().any(|candidate| candidate == line),
            "{}",
            second.stdout
        );
    }
    assert!(second.stdout.contains("[api] exists at "));
    assert!(second.stdout.contains("[tools] exists at "));
    assert!(!second.stdout.contains("cloned"));
    assert!(!second.stdout.contains("excludes updated"));
    assert!(!second.stdout.contains("(exit 0)"));
    assert_eq!(read(&tree, "projects/api/runs"), "setup\nsetup\n");
    assert_eq!(read(&tree, EXCLUDE), before);
    let after = fs::metadata(tree.workspace_dir().join(EXCLUDE)).expect("exclude metadata");
    assert_eq!(
        (after.ino(), after.mtime(), after.mtime_nsec()),
        (metadata.ino(), metadata.mtime(), metadata.mtime_nsec())
    );
    let repeat = events(&run(&tree, true), 0);
    assert!(
        !repeat
            .iter()
            .any(|e| e["action"] == "cloned" || e["action"] == "workspace_excludes_updated")
    );
    assert!(action(&repeat, "api", "exists"));
}

#[test]
fn failed_commands_continue_healthy_projects_standalones_finalization_and_refresh() {
    for json in [false, true] {
        let config = format!(
            "{}\n[[project_repository]]\nname='healthy'\nurl='https://fixture.invalid/healthy.git'\n",
            BOTH.replace("printf 'setup\\n'; printf 'setup\\n' >> runs", "exit 7")
        );
        let tree = fixture(&config, true, false);
        write(&tree, "AGENTS.md", SAFETY);
        let ended = run(&tree, json);
        assert_eq!(ended.termination.exit_code(), 1);
        if json {
            let rows = events(&ended, 1);
            assert!(rows.iter().any(|e| {
                e["type"] == "repo_error"
                    && e["repo"] == "api"
                    && e["error"]
                        .as_str()
                        .expect("error")
                        .contains("failed at cmd")
            }));
            assert!(rows.contains(
                &json!({"type":"cmd_completed","repo":"api","command":"exit 7","returncode":7})
            ));
            assert!(rows.contains(
                &json!({"type":"target_completed","target":"projects/","success":false})
            ));
            assert!(action(&rows, "healthy", "cloned"));
            assert!(action(&rows, "tools", "cloned"));
            assert!(action(
                &rows,
                "winter-extensions",
                "workspace_excludes_updated"
            ));
            assert!(action(&rows, "workspace", "agents_md_updated"));
        } else {
            for line in ["[api] ✗ exit 7 (exit 7)", "✗ projects/ failed"] {
                assert!(
                    ended.stderr.lines().any(|candidate| candidate == line),
                    "{}",
                    ended.stderr
                );
                assert!(!ended.stdout.contains(line));
            }
            assert!(ended.stderr.contains("[api] ERROR: "));
            assert!(ended.stdout.contains("✓ standalone/ done"));
            assert!(ended.stdout.contains("AGENTS.md safety rules updated"));
        }
        assert!(!read(&tree, "AGENTS.md").contains("\nold\n"));
        assert!(read(&tree, EXCLUDE).contains("/tools/"));
    }
}

#[test]
fn removing_standalones_retracts_only_owned_blocks_not_checkouts() {
    let tree = fixture(BOTH, true, true);
    events(&run(&tree, true), 0);
    let projects = BOTH
        .split("[[standalone_repository]]")
        .next()
        .expect("projects");
    write(&tree, CONFIG, projects);
    let rows = events(&run(&tree, true), 0);
    assert!(
        rows.iter()
            .any(|e| e["repo"] == "winter-extensions" && e["detail"] == "api")
    );
    let content = read(&tree, EXCLUDE);
    assert!(!content.contains("# >>> tools "));
    assert!(content.contains("# >>> api (managed by winter)\n/projects/api/\n"));
    assert!(!git(&tree, "tools", &["rev-parse", "HEAD"]).is_empty());
    write(&tree, CONFIG, "");
    let rows = events(&run(&tree, true), 0);
    assert!(
        rows.iter()
            .any(|e| e["repo"] == "winter-extensions" && e["detail"] == "cleared")
    );
    assert!(!read(&tree, EXCLUDE).contains("# >>> api "));
    assert!(read(&tree, EXCLUDE).contains("winter-config"));
    assert!(tree.workspace_dir().join("projects/api/.git").exists());
}

#[test]
fn safety_conflicts_fail_without_replacing_content_and_unowned_files_are_untouched() {
    for (content, exit) in [
        ("user owned\n", 0),
        (
            "<!-- >>> winter-workspace/safety (managed by winter) -->\nmissing end",
            1,
        ),
    ] {
        let tree = fixture("", true, false);
        write(&tree, "AGENTS.md", content);
        let rows = events(&run(&tree, true), exit);
        assert_eq!(read(&tree, "AGENTS.md"), content);
        assert!(!action(&rows, "workspace", "agents_md_updated"));
        assert_eq!(
            rows.iter().any(|e| e["type"] == "repo_error"
                && e["repo"] == "workspace"
                && e["error"].as_str().expect("error").contains("AGENTS.md")),
            exit == 1
        );
    }
}

#[test]
fn finalize_failure_is_reported_even_without_a_standalone_target() {
    let tree = fixture("", true, false);
    fs::create_dir_all(tree.workspace_dir().join(".git/info")).expect("info directory");
    fs::write(tree.workspace_dir().join(EXCLUDE), [0xff]).expect("invalid exclude");
    write(&tree, "AGENTS.md", SAFETY);
    let rows = events(&run(&tree, true), 1);
    assert!(
        rows.iter()
            .any(|e| e["type"] == "repo_error" && e["repo"] == "winter-extensions")
    );
    assert!(action(&rows, "workspace", "agents_md_updated"));
    assert!(
        !rows
            .iter()
            .any(|e| e["type"] == "target_started" && e["target"] == "standalone/")
    );
}

#[test]
fn non_git_root_reconciles_both_kinds_without_creating_root_excludes() {
    let tree = fixture(BOTH, false, false);
    let rows = events(&run(&tree, true), 0);
    assert!(action(&rows, "api", "cloned"));
    assert!(action(&rows, "tools", "cloned"));
    assert!(!tree.workspace_dir().join(".git").exists());
    assert!(
        !rows
            .iter()
            .any(|e| e["action"] == "workspace_excludes_updated")
    );
}

#[test]
fn unpinned_clones_leave_a_clean_root_clean() {
    let tree = fixture(BOTH, true, false);
    assert_eq!(git(&tree, "", &["status", "--porcelain"]), "");
    events(&run(&tree, true), 0);
    assert_eq!(git(&tree, "", &["status", "--porcelain"]), "");
    assert!(!tree.workspace_dir().join(".winter/config.lock").exists());
}

#[test]
fn pinned_standalone_records_a_lock_and_repeats_without_rewriting_it() {
    let tree = fixture(&format!("{BOTH}ref = 'v1'\n"), true, false);
    let rows = events(&run(&tree, true), 0);
    assert!(action(&rows, "tools", "pinned"));
    assert_eq!(
        git(&tree, "tools", &["rev-parse", "HEAD"]),
        git(&tree, "tools", &["rev-parse", "v1^{commit}"])
    );
    let before = read(&tree, ".winter/config.lock");
    let metadata = fs::metadata(tree.workspace_dir().join(".winter/config.lock")).expect("lock");
    assert!(action(&events(&run(&tree, true), 0), "tools", "pinned"));
    assert_eq!(read(&tree, ".winter/config.lock"), before);
    assert_eq!(
        fs::metadata(tree.workspace_dir().join(".winter/config.lock"))
            .expect("lock")
            .ino(),
        metadata.ino()
    );
}

#[test]
fn projects_only_and_adoption_disabled_keep_config_without_extension_actions() {
    for config in [
        "",
        "adopt_extensions = 'none'\n",
        &format!("adopt_extensions = 'none'\n{BOTH}"),
    ] {
        let tree = fixture(config, true, false);
        let rows = events(&run(&tree, true), 0);
        assert!(read(&tree, EXCLUDE).contains("# >>> winter-config "));
        assert!(!action(
            &rows,
            "winter-extensions",
            "workspace_excludes_updated"
        ));
    }
}

#[test]
fn unsupported_arguments_have_no_reconciliation_effects() {
    let tree = fixture(BOTH, true, false);
    let before = snapshot(&tree.workspace_dir());
    for args in [&["ws", "init", "alpha"][..], &["ws", "init", "--all"]] {
        let ended = invocation(&tree, args).run();
        assert_eq!(ended.termination.exit_code(), 2);
        assert_eq!(ended.stdout, "");
        assert_eq!(snapshot(&tree.workspace_dir()), before);
    }
}

struct BrokenOutput {
    flush_only: bool,
}

impl Write for BrokenOutput {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if self.flush_only {
            Ok(bytes.len())
        } else {
            Err(io::ErrorKind::BrokenPipe.into())
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        Err(io::ErrorKind::BrokenPipe.into())
    }
}

#[test]
fn broken_output_preserves_termination_and_stops_before_first_target_effects() {
    for json in [false, true] {
        for flush_only in [false, true] {
            if flush_only && !json {
                continue;
            }
            let tree = fixture(BOTH, true, false);
            let before = snapshot(&tree.workspace_dir());
            let args = if json {
                &["ws", "init", "--json"][..]
            } else {
                &["ws", "init"][..]
            };
            let ended = invocation(&tree, args).run_with_stdout(&mut BrokenOutput { flush_only });
            assert!(
                matches!(ended.termination, Termination::BrokenPipe),
                "{:?}",
                ended.termination
            );
            assert_eq!(ended.stderr, "");
            assert_eq!(snapshot(&tree.workspace_dir()), before);
        }
    }
}

#[test]
fn invalid_configuration_fails_before_any_event_or_mutation() {
    let tree = fixture("invalid = [", true, false);
    let before = snapshot(&tree.workspace_dir());
    let ended = run(&tree, true);
    assert_eq!(ended.termination.exit_code(), 1);
    assert_eq!(ended.stdout, "");
    assert_eq!(snapshot(&tree.workspace_dir()), before);
}
