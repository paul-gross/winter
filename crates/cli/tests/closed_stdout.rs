//! The built binary against a stdout that cannot be written: how the process ends, and what it leaves on stderr.

use std::path::Path;

use winter_workspace_test_support::{
    Exit, Observation, StdoutFault, TempTree, WorkspaceBuilder, invoke_with_stdout,
};

const FAULTS: [StdoutFault; 3] = [
    StdoutFault::ClosedPipe,
    StdoutFault::Full,
    StdoutFault::Closed,
];

const SHARED: &str = ".winter/config.toml";

const TWO_REPOSITORIES: &str = "\
[[project_repository]]
url = \"https://fixture.invalid/api.git\"

[[project_repository]]
url = \"https://fixture.invalid/web.git\"
";

/// The text the removal leaves: the blank line before the removed block stays.
const WITHOUT_WEB: &str = "\
[[project_repository]]
url = \"https://fixture.invalid/api.git\"

";

/// Runs the built binary with `args` in `cwd` of `tree`, its stdout the one `fault` names, and returns its
/// stderr as text with its exit.
fn run(tree: &TempTree, cwd: &Path, args: &[&str], fault: StdoutFault) -> (String, Exit) {
    let mut invocation = tree.invocation(Path::new(env!("CARGO_BIN_EXE_winter-workspace")));
    invocation.cwd = cwd.to_path_buf();
    invocation.args = args.iter().map(Into::into).collect();
    let Observation { stderr, exit, .. } =
        invoke_with_stdout(invocation, fault).expect("the binary runs to its end");
    (String::from_utf8(stderr).expect("UTF-8 on stderr"), exit)
}

/// Asserts the ending of a command that would have succeeded, under `fault`: a pipe the reader closed ends it
/// silently at exit code 1, a device that takes nothing ends it at exit code 1 with one error line, and no
/// stdout at all leaves it its own ending.
fn assert_ending(fault: StdoutFault, stderr: &str, exit: Exit, line: &str) {
    match fault {
        StdoutFault::ClosedPipe => {
            assert_eq!(exit, Exit::Code(1), "{line}");
            assert_eq!(stderr, "", "{line}");
        }
        StdoutFault::Full => {
            assert_eq!(exit, Exit::Code(1), "{line}");
            assert!(stderr.starts_with("error: "), "{line}: {stderr:?}");
            assert!(stderr.ends_with('\n'), "{line}: {stderr:?}");
            assert_eq!(stderr.lines().count(), 1, "{line}: {stderr:?}");
        }
        StdoutFault::Closed => {
            assert_eq!(exit, Exit::Code(0), "{line}");
            assert_eq!(stderr, "", "{line}");
        }
    }
}

#[test]
fn the_help_ends_as_its_stdout_decides() {
    for fault in FAULTS {
        let tree = TempTree::new().expect("an isolated tree");

        let (stderr, exit) = run(&tree, &tree.workspace_dir(), &["--help"], fault);

        assert_ending(fault, &stderr, exit, &format!("--help under {fault:?}"));
    }
}

#[test]
fn the_version_ends_as_its_stdout_decides() {
    for fault in FAULTS {
        let tree = TempTree::new().expect("an isolated tree");

        let (stderr, exit) = run(&tree, &tree.workspace_dir(), &["--version"], fault);

        assert_ending(fault, &stderr, exit, &format!("--version under {fault:?}"));
    }
}

#[test]
fn a_command_that_writes_a_document_ends_as_its_stdout_decides() {
    for fault in FAULTS {
        let tree = TempTree::new().expect("an isolated tree");
        let workspace = WorkspaceBuilder::new(&tree)
            .config("# workspace\n")
            .state("[env_index]\nfeature-x = 15\n")
            .build()
            .expect("a workspace");

        let (stderr, exit) = run(
            &tree,
            &workspace.root,
            &["ws", "index", "feature-x", "--json"],
            fault,
        );

        assert_ending(fault, &stderr, exit, &format!("ws index under {fault:?}"));
    }
}

#[test]
fn a_removal_is_kept_whatever_becomes_of_its_result() {
    for fault in FAULTS {
        let tree = TempTree::new().expect("an isolated tree");
        let workspace = WorkspaceBuilder::new(&tree)
            .config(TWO_REPOSITORIES)
            .build()
            .expect("a workspace");

        let (stderr, exit) = run(
            &tree,
            &workspace.root,
            &["repo", "remove", "project/web"],
            fault,
        );

        assert_ending(
            fault,
            &stderr,
            exit,
            &format!("repo remove under {fault:?}"),
        );
        assert_eq!(
            std::fs::read_to_string(workspace.root.join(SHARED)).expect("the configuration file"),
            WITHOUT_WEB,
            "{fault:?}"
        );
    }
}
