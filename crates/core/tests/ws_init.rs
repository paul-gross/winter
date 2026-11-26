//! Direct reconciliation against isolated local repositories.
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::os::unix::fs::symlink;

use winter_workspace_core::{
    Action, Diagnostics, InitError, InitEvent, Log, NoTrace, OsFiles, OsPrograms, PinPorts,
    ProcessEnv, RunEnd, reconcile,
};
use winter_workspace_test_support::{CommitSpec, Git, TempTree};

fn config(tree: &TempTree, content: &str) {
    fs::create_dir_all(tree.workspace_dir().join(".winter")).expect("config directory");
    fs::write(tree.workspace_dir().join(".winter/config.toml"), content).expect("config");
}

fn run(tree: &TempTree) -> (Result<RunEnd, InitError>, Vec<InitEvent>) {
    let env = ProcessEnv::from_vars(
        tree.env()
            .variables()
            .map(|(k, v)| (k.to_owned(), v.to_owned())),
    );
    let programs = OsPrograms::new(&env, false);
    let ports = PinPorts {
        files: &OsFiles,
        writer: &OsFiles,
        programs: &programs,
        tracer: &NoTrace,
    };
    let mut stderr = Vec::new();
    let mut diagnostics = Diagnostics {
        err: &mut stderr,
        log: Log::DEFAULT,
    };
    let mut events = Vec::new();
    let result = reconcile(
        &ports,
        &env,
        &mut diagnostics,
        &tree.workspace_dir(),
        &mut |event, _| {
            events.push(event.clone());
            Ok(())
        },
    );
    (result, events)
}

fn success(result: Result<RunEnd, InitError>) {
    assert!(
        matches!(result, Ok(RunEnd::Completed { success: true })),
        "{result:?}"
    );
}

fn remote(tree: &TempTree, name: &str) {
    Git::new(tree)
        .expect("git")
        .bare_remote(
            name,
            "main",
            &[CommitSpec {
                message: "seed".into(),
                files: vec![("README".into(), b"fixture".to_vec())],
                ..CommitSpec::default()
            }],
        )
        .expect("seed remote");
}

fn root(tree: &TempTree) {
    Git::new(tree)
        .expect("git")
        .init(&tree.workspace_dir(), "main")
        .expect("root repository");
    fs::create_dir_all(tree.workspace_dir().join(".git/info")).expect("info directory");
}

fn exclude(tree: &TempTree) -> String {
    fs::read_to_string(tree.workspace_dir().join(".git/info/exclude")).expect("exclude")
}

#[test]
fn first_run_and_repeat_maintain_blocks_without_replacing_unchanged_excludes() {
    let tree = TempTree::new().expect("tree");
    root(&tree);
    remote(&tree, "api");
    remote(&tree, "tools");
    config(
        &tree,
        r#"
prefix = "custom"
[[project_repository]]
name = "api"
url = "https://fixture.invalid/api.git"
cmd = ["printf 'setup\\n'"]
[[standalone_repository]]
name = "tools"
url = "https://fixture.invalid/tools.git"
"#,
    );
    let (result, events) = run(&tree);
    success(result);
    let targets: Vec<_> = events
        .iter()
        .filter_map(|event| match event {
            InitEvent::TargetStarted { target } => Some(target.as_str()),
            _ => None,
        })
        .collect();
    assert_eq!(targets, ["projects/", "standalone/"]);
    assert_eq!(
        events
            .iter()
            .filter(|event| matches!(
                event,
                InitEvent::RepoAction {
                    action: Action::Cloned,
                    ..
                }
            ))
            .count(),
        2
    );
    let before = exclude(&tree);
    for line in [
        "/projects/",
        "/.winter/logs/",
        ".claude/skills/custom-*",
        ".codex/skills/custom",
        ".opencode/skill/custom-*",
        ".winter/config/**/*.local.*",
        "/tools/",
    ] {
        assert!(before.contains(line), "{line}: {before}");
    }
    let path = tree.workspace_dir().join(".git/info/exclude");
    let inode = fs::metadata(&path).expect("metadata").ino();
    let (result, events) = run(&tree);
    success(result);
    assert_eq!(exclude(&tree), before);
    assert_eq!(fs::metadata(path).expect("metadata").ino(), inode);
    assert!(!events.iter().any(|event| matches!(
        event,
        InitEvent::RepoAction {
            action: Action::Cloned | Action::WorkspaceExcludesUpdated,
            ..
        }
    )));
    assert!(
        events
            .iter()
            .any(|event| matches!(event, InitEvent::CmdOutputLine { line, .. } if line == "setup"))
    );
}

#[test]
fn failures_do_not_suppress_healthy_checkouts_finalization_or_safety_refresh() {
    let tree = TempTree::new().expect("tree");
    root(&tree);
    remote(&tree, "healthy");
    config(
        &tree,
        r#"
[[project_repository]]
name = "broken"
[[standalone_repository]]
name = "healthy"
url = "https://fixture.invalid/healthy.git"
"#,
    );
    fs::write(tree.workspace_dir().join("AGENTS.md"), "<!-- >>> winter-workspace/safety (managed by winter) -->\nold\n<!-- <<< winter-workspace/safety -->\n").expect("owned safety");
    let (result, events) = run(&tree);
    assert!(matches!(result, Ok(RunEnd::Completed { success: false })));
    assert!(events.iter().any(|event| matches!(event, InitEvent::RepoError { repo, error } if repo == "broken" && error.contains("failed at location; not attempted: identity, excludes, cmd"))));
    assert!(events.iter().any(|event| matches!(event, InitEvent::RepoAction { repo, action: Action::Cloned, .. } if repo == "healthy")));
    assert!(matches!(
        events.last(),
        Some(InitEvent::RepoAction {
            action: Action::AgentsMdUpdated,
            ..
        })
    ));
    assert!(exclude(&tree).contains("/healthy/"));
}

#[test]
fn finalize_runs_without_a_standalone_target_and_respects_adoption_disabled() {
    for mode in ["winter", "none"] {
        let tree = TempTree::new().expect("tree");
        root(&tree);
        let path = tree.workspace_dir().join(".git/info/exclude");
        fs::write(
            &path,
            "# >>> obsolete (managed by winter)\n/obsolete/\n# <<< obsolete\n",
        )
        .expect("old block");
        config(&tree, &format!("adopt_extensions = \"{mode}\"\n"));
        fs::write(tree.workspace_dir().join("AGENTS.md"), "user owned\n").expect("unowned safety");
        let (result, events) = run(&tree);
        success(result);
        assert!(!events.iter().any(
            |event| matches!(event, InitEvent::TargetStarted { target } if target == "standalone/")
        ));
        let content = exclude(&tree);
        assert!(content.contains("winter-config"));
        assert_eq!(content.contains("/obsolete/"), mode == "none");
        assert_eq!(events.iter().any(|event| matches!(event, InitEvent::RepoAction { repo, detail, .. } if repo == "winter-extensions" && detail == "cleared")), mode == "winter");
        assert_eq!(
            fs::read_to_string(tree.workspace_dir().join("AGENTS.md")).expect("safety"),
            "user owned\n"
        );
    }
}

#[test]
fn initial_configuration_failure_emits_nothing_and_creates_no_projects() {
    let tree = TempTree::new().expect("tree");
    config(&tree, "invalid = [");
    let (result, events) = run(&tree);
    assert!(matches!(result, Err(InitError::Config(_))));
    assert!(events.is_empty());
    assert!(!tree.workspace_dir().join("projects").exists());
}

#[test]
fn a_non_git_root_clones_without_creating_an_exclude_file() {
    let tree = TempTree::new().expect("tree");
    remote(&tree, "api");
    config(
        &tree,
        "[[project_repository]]\nname = 'api'\nurl = 'https://fixture.invalid/api.git'\n",
    );
    let (result, _) = run(&tree);
    success(result);
    assert!(tree.workspace_dir().join("projects/api/.git").exists());
    assert!(!tree.workspace_dir().join(".git").exists());
}

#[test]
fn project_activation_and_eligibility_are_separate_and_follow_failed_commands() {
    for (declaration, manifest, active, eligible) in [
        ("", true, true, true),
        ("load = 'eager'", false, true, false),
        ("entry = ['guide.md']", false, true, false),
        ("extension = false", true, true, false),
        ("load = 'none'", false, false, false),
        ("", false, false, false),
    ] {
        let tree = TempTree::new().expect("tree");
        root(&tree);
        let mut files = vec![("README".into(), b"fixture".to_vec())];
        if manifest {
            files.push(("winter-ext.toml".into(), Vec::new()));
        }
        Git::new(&tree)
            .expect("git")
            .bare_remote(
                "api",
                "main",
                &[CommitSpec {
                    message: "seed".into(),
                    files,
                    ..CommitSpec::default()
                }],
            )
            .expect("remote");
        config(
            &tree,
            &format!(
                "[[project_repository]]\nname = 'api'\nurl = 'https://fixture.invalid/api.git'\ncmd = ['exit 9']\n{declaration}\n"
            ),
        );
        let (result, events) = run(&tree);
        assert!(matches!(result, Ok(RunEnd::Completed { success: false })));
        assert_eq!(
            events.iter().any(|event| matches!(event,
            InitEvent::TargetStarted { target } if target == "standalone/")),
            active,
            "{declaration}"
        );
        assert_eq!(
            exclude(&tree).contains("# >>> api (managed by winter)\n/projects/api/\n"),
            eligible,
            "{declaration}"
        );
        assert!(events.iter().any(|event| matches!(event,
            InitEvent::RepoError { repo, error } if repo == "api" && error.contains("failed at cmd"))));
    }
}

#[test]
fn a_project_also_declared_as_standalone_has_only_the_standalone_block() {
    let tree = TempTree::new().expect("tree");
    root(&tree);
    Git::new(&tree)
        .expect("git")
        .bare_remote(
            "api",
            "main",
            &[CommitSpec {
                message: "extension".into(),
                files: vec![("winter-ext.toml".into(), Vec::new())],
                ..CommitSpec::default()
            }],
        )
        .expect("remote");
    config(
        &tree,
        "[[project_repository]]\nname = 'api'\nurl = 'https://fixture.invalid/api.git'\n[[standalone_repository]]\nname = 'api'\nurl = 'https://fixture.invalid/api.git'\n",
    );
    let (result, _) = run(&tree);
    success(result);
    let content = exclude(&tree);
    assert!(content.contains("# >>> api (managed by winter)\n/api/\n"));
    assert!(!content.contains("/projects/api/"));
    assert_eq!(content.matches("# >>> api ").count(), 1);
}

#[test]
fn managed_content_failures_accumulate_and_safety_conflicts_remain_untouched() {
    let tree = TempTree::new().expect("tree");
    root(&tree);
    config(&tree, "");
    fs::write(tree.workspace_dir().join(".git/info/exclude"), [0xff]).expect("non-text exclude");
    let malformed = "<!-- >>> winter-workspace/safety (managed by winter) -->\nmissing end";
    fs::write(tree.workspace_dir().join("AGENTS.md"), malformed).expect("malformed safety");
    let (result, events) = run(&tree);
    assert!(matches!(result, Ok(RunEnd::Completed { success: false })));
    let errors: Vec<_> = events
        .iter()
        .filter_map(|event| match event {
            InitEvent::RepoError { repo, error } => Some((repo.as_str(), error.as_str())),
            _ => None,
        })
        .collect();
    assert_eq!(
        errors.iter().map(|(repo, _)| *repo).collect::<Vec<_>>(),
        [
            "winter",
            "winter",
            "workspace",
            "winter-extensions",
            "workspace"
        ]
    );
    assert!(errors[0].1.starts_with("workspace exclude — "));
    assert!(errors[2].1.starts_with("workspace skills — "));
    assert!(errors[4].1.contains("AGENTS.md"));
    assert_eq!(
        fs::read_to_string(tree.workspace_dir().join("AGENTS.md")).expect("safety"),
        malformed
    );
}

#[test]
fn eligibility_follows_links_for_confinement_but_keeps_the_declared_relative_path() {
    for outside in [false, true] {
        let tree = TempTree::new().expect("tree");
        root(&tree);
        remote(&tree, "api");
        let destination = if outside {
            tree.root().join("outside")
        } else {
            tree.workspace_dir().join("actual")
        };
        Git::new(&tree)
            .expect("git")
            .clone_remote("api", &destination)
            .expect("existing checkout");
        symlink(&destination, tree.workspace_dir().join("alias")).expect("checkout alias");
        config(
            &tree,
            "[[standalone_repository]]\nname = 'api'\npath = 'alias'\nurl = 'https://fixture.invalid/api.git'\n",
        );
        let (result, _) = run(&tree);
        success(result);
        let content = exclude(&tree);
        assert_eq!(
            content.contains("# >>> api (managed by winter)\n/alias/\n"),
            !outside
        );
        assert!(!content.contains("/actual/"));
    }
}
