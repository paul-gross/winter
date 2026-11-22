//! The program port's wait and its deadline, against stub programs in an isolated tree.

use std::ffi::OsStr;
use std::os::unix::fs::PermissionsExt;
use std::time::{Duration, Instant};

use winter_workspace_core::{Call, Exit, OsPrograms, ProcessEnv, Program, RunPrograms};
use winter_workspace_test_support::TempTree;

/// An isolated tree whose `bin/git` is the shell script `body`, and the programs of its pinned environment.
fn with_stub_git(body: &str) -> (TempTree, OsPrograms) {
    let tree = TempTree::new().expect("an isolated tree");
    let bin = tree.root().join("bin");
    std::fs::create_dir_all(&bin).expect("a bin directory");
    let git = bin.join("git");
    std::fs::write(&git, format!("#!/bin/sh\n{body}\n")).expect("a stub");
    std::fs::set_permissions(&git, std::fs::Permissions::from_mode(0o755)).expect("executable");
    let programs = OsPrograms::new(&ProcessEnv::from_vars(
        tree.env()
            .variables()
            .map(|(name, value)| (name.to_owned(), value.to_owned())),
    ));
    (tree, programs)
}

fn run(
    tree: &TempTree,
    programs: &OsPrograms,
    args: &[&str],
    timeout: Option<Duration>,
) -> winter_workspace_core::Finished {
    let args: Vec<&OsStr> = args.iter().map(OsStr::new).collect();
    programs
        .run(&Call {
            program: Program::Git,
            args: &args,
            cwd: tree.root(),
            timeout,
            env: &[],
        })
        .expect("a finished call")
}

#[test]
fn a_megabyte_on_stderr_within_the_timeout_is_captured_whole() {
    let (tree, programs) =
        with_stub_git("head -c 1048576 /dev/zero | tr '\\0' x >&2\nprintf out\nexit 0");

    let finished = run(&tree, &programs, &["fetch"], Some(Duration::from_secs(30)));

    assert_eq!(finished.exit, Exit::Code(0));
    assert_eq!(finished.stdout, b"out");
    assert_eq!(finished.stderr.len(), 1_048_576);
}

#[test]
fn a_call_without_a_timeout_waits_for_the_program() {
    let (tree, programs) = with_stub_git("sleep 0.3\necho done\nexit 3");

    let finished = run(&tree, &programs, &["status"], None);

    assert_eq!(finished.exit, Exit::Code(3));
    assert_eq!(finished.stdout, b"done\n");
}

#[test]
fn a_program_past_its_deadline_is_killed_with_the_child_that_holds_its_stderr() {
    let (tree, programs) = with_stub_git("sleep 60 &\nsleep 60");
    let started = Instant::now();

    let finished = run(
        &tree,
        &programs,
        &["fetch", "origin"],
        Some(Duration::from_millis(1500)),
    );

    assert!(
        started.elapsed() < Duration::from_millis(2500),
        "{:?}",
        started.elapsed()
    );
    assert_eq!(finished.exit, Exit::Signal(9));
    assert_eq!(
        String::from_utf8_lossy(&finished.stderr),
        "Timeout: the command \"git fetch origin\" did not complete in 1 secs."
    );
}

#[test]
fn a_program_that_closes_its_streams_and_runs_on_is_killed_at_the_deadline() {
    let (tree, programs) = with_stub_git("exec >&- 2>&-\nsleep 60");
    let started = Instant::now();

    let finished = run(
        &tree,
        &programs,
        &["fetch"],
        Some(Duration::from_millis(500)),
    );

    assert!(
        started.elapsed() < Duration::from_millis(1500),
        "{:?}",
        started.elapsed()
    );
    assert_eq!(finished.exit, Exit::Signal(9));
}

#[test]
fn a_variable_of_a_call_is_laid_over_that_call_alone() {
    let (tree, programs) = with_stub_git("printf '%s' \"$WINTER_X\"");
    let args = [OsStr::new("status")];
    let call = |env: &'static [(&'static OsStr, &'static OsStr)]| Call {
        program: Program::Git,
        args: &args,
        cwd: tree.root(),
        timeout: None,
        env,
    };

    let given: [(&OsStr, &OsStr); 1] = [(OsStr::new("WINTER_X"), OsStr::new("1"))];
    let laid = programs
        .run(&Call {
            program: Program::Git,
            args: &args,
            cwd: tree.root(),
            timeout: None,
            env: &given,
        })
        .expect("a finished call");
    let after = programs.run(&call(&[])).expect("a finished call");

    assert_eq!(laid.stdout, b"1");
    assert_eq!(after.stdout, b"");
}

#[test]
fn a_variable_of_a_call_wins_over_the_snapshot_one_of_its_name() {
    let (tree, programs) = with_stub_git("printf '%s' \"$LC_ALL\"");
    let args = [OsStr::new("status")];

    let finished = programs
        .run(&Call {
            program: Program::Git,
            args: &args,
            cwd: tree.root(),
            timeout: None,
            env: &[(OsStr::new("LC_ALL"), OsStr::new("given"))],
        })
        .expect("a finished call");

    assert_eq!(finished.stdout, b"given");
}
