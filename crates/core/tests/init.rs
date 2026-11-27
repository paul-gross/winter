//! Creating a workspace through the command boundary.

use std::ffi::OsStr;
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{PermissionsExt, symlink};
use winter_workspace_core::{BootstrapError, CommandError, Termination};
use winter_workspace_test_support::TempTree;

#[path = "support/in_process.rs"]
mod in_process;
use in_process::InProcess;

#[test]
fn creates_only_the_workspace_files_and_loads_the_configuration() {
    let tree = TempTree::new().unwrap();
    let root = tree.workspace_dir();
    let ended = InProcess::new(&["init", "--json"]).cwd(&root).run();
    let doc = ended.document("init");
    assert_eq!(
        doc,
        serde_json::json!({"root": root, "config":"created", "agents_md":"created", "claude_md":"created"})
    );
    let config = fs::read_to_string(root.join(".winter/config.toml")).unwrap();
    let parsed: toml::Value = toml::from_str(&config).unwrap();
    let expected: toml::Value = toml::from_str("[versions]\nworkspace = \"0.1.0\"\n").unwrap();
    assert_eq!(parsed, expected);
    assert!(
        fs::read_to_string(root.join("AGENTS.md"))
            .unwrap()
            .contains("winter-workspace/safety")
    );
    assert_eq!(fs::read(root.join("CLAUDE.md")).unwrap(), b"@AGENTS.md\n");
    let listing = InProcess::new(&["repo", "list", "--json"]).cwd(&root).run();
    assert_eq!(listing.document("repo list"), serde_json::json!([]));
    assert!(!root.join(".git").exists());
    assert_eq!(fs::read_dir(&root).unwrap().count(), 3);
}

#[test]
fn existing_configuration_refuses_without_touching_agent_files() {
    let tree = TempTree::new().unwrap();
    let root = tree.workspace_dir();
    fs::create_dir(root.join(".winter")).unwrap();
    fs::write(root.join(".winter/config.toml"), "old").unwrap();
    fs::write(root.join("AGENTS.md"), "agents").unwrap();
    fs::write(root.join("CLAUDE.md"), "claude").unwrap();
    let ended = InProcess::new(&["init"]).cwd(&root).run();
    assert!(
        matches!(ended.termination, Termination::Command(ref e) if matches!(**e, CommandError::Bootstrap(BootstrapError::AlreadyInitialized { .. })))
    );
    assert_eq!(ended.stdout, "");
    assert_eq!(fs::read(root.join(".winter/config.toml")).unwrap(), b"old");
    assert_eq!(fs::read(root.join("AGENTS.md")).unwrap(), b"agents");
    assert_eq!(fs::read(root.join("CLAUDE.md")).unwrap(), b"claude");
}

#[test]
fn preserves_user_content_and_warns_on_shim_conflict() {
    let tree = TempTree::new().unwrap();
    let root = tree.workspace_dir();
    let user = b"user rules\r\nlast line\n";
    fs::write(root.join("AGENTS.md"), user).unwrap();
    fs::write(root.join("CLAUDE.md"), b"custom").unwrap();
    let ended = InProcess::new(&["init", "--json"]).cwd(&root).run();
    assert_eq!(ended.termination.exit_code(), 0);
    let doc: serde_json::Value = serde_json::from_str(&ended.stdout).unwrap();
    assert_eq!(doc["agents_md"], "updated");
    assert_eq!(doc["claude_md"], "conflict");
    assert!(ended.stderr.contains("@AGENTS.md"));
    assert!(fs::read(root.join("AGENTS.md")).unwrap().ends_with(user));
    assert_eq!(fs::read(root.join("CLAUDE.md")).unwrap(), b"custom");
}

#[test]
fn an_agents_file_that_cannot_take_the_block_is_a_warned_conflict() {
    let malformed: &[u8] =
        b"<!-- >>> winter-workspace/safety (managed by winter) -->\nunterminated\n";
    for (content, reason) in [
        (malformed, "malformed safety block"),
        (b"\xff\n".as_slice(), "not UTF-8"),
    ] {
        let tree = TempTree::new().unwrap();
        let root = tree.workspace_dir();
        fs::write(root.join("AGENTS.md"), content).unwrap();
        let ended = InProcess::new(&["init", "--json"]).cwd(&root).run();
        assert_eq!(ended.termination.exit_code(), 0, "{reason}");
        let doc: serde_json::Value = serde_json::from_str(&ended.stdout).unwrap();
        assert_eq!(doc["agents_md"], "conflict", "{reason}");
        assert_eq!(
            ended.stderr,
            format!(
                "warning: {}: {reason}; the rules were not placed\n",
                root.join("AGENTS.md").display()
            )
        );
        assert_eq!(
            fs::read(root.join("AGENTS.md")).unwrap(),
            content,
            "{reason}"
        );
    }
}

#[test]
fn the_text_result_names_each_file_and_the_next_commands() {
    let tree = TempTree::new().unwrap();
    let root = tree.workspace_dir();
    let ended = InProcess::new(&["init"]).cwd(&root).run();
    assert_eq!(ended.termination.exit_code(), 0);
    assert_eq!(ended.stderr, "");
    assert_eq!(
        ended.stdout,
        format!(
            "created {}/.winter/config.toml\nAGENTS.md: placed the workspace safety rules\nCLAUDE.md: created\nnext, read `winter-workspace context setup`, or declare repositories with `winter-workspace repo add` and run `winter-workspace ws init`\n",
            root.display()
        )
    );
}

#[test]
fn a_dangling_configuration_link_is_refused() {
    let tree = TempTree::new().unwrap();
    let root = tree.workspace_dir();
    fs::create_dir(root.join(".winter")).unwrap();
    let config = root.join(".winter/config.toml");
    let target = root.join("absent");
    symlink(&target, &config).unwrap();
    let ended = InProcess::new(&["init"]).cwd(&root).run();
    assert!(
        matches!(ended.termination, Termination::Command(ref e) if matches!(**e, CommandError::Bootstrap(BootstrapError::AlreadyInitialized { .. }))),
        "{:?}",
        ended.termination
    );
    assert_eq!(ended.stdout, "");
    assert_eq!(fs::read_link(&config).unwrap(), target);
    assert!(!target.exists());
    assert!(!root.join("AGENTS.md").exists());
    assert!(!root.join("CLAUDE.md").exists());
}

#[test]
fn a_directory_that_is_not_utf8_gets_a_document() {
    let tree = TempTree::new().unwrap();
    let root = tree.workspace_dir().join(OsStr::from_bytes(b"nu\xff"));
    fs::create_dir(&root).unwrap();
    let ended = InProcess::new(&["init", "--json"]).cwd(&root).run();
    let doc = ended.document("init");
    assert_eq!(doc["root"], root.to_string_lossy().as_ref());
    assert!(root.join(".winter/config.toml").is_file());
}

#[test]
fn init_starts_no_git_process() {
    let tree = TempTree::new().unwrap();
    let stub = tree.root().join("bin/git");
    let calls = tree.root().join("calls/git");
    fs::write(
        &stub,
        format!("#!/bin/sh\nprintf called > '{}'\n", calls.display()),
    )
    .unwrap();
    fs::set_permissions(&stub, fs::Permissions::from_mode(0o755)).unwrap();
    let ended = InProcess::new(&["init", "--json"])
        .cwd(&tree.workspace_dir())
        .pinned(&tree.env())
        .run();
    assert_eq!(ended.termination.exit_code(), 0);
    assert!(!calls.exists(), "init must not run git");
}
