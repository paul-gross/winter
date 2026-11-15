//! The root of the command tree, as the built binary answers it: streams and exit codes of a real process.

use std::path::Path;

use winter_workspace_test_support::{Exit, Observation, TempTree, invoke};

/// Runs the built binary with `args` in an isolated tree and returns its two streams as text with its exit.
fn run(args: &[&str]) -> (String, String, Exit) {
    let tree = TempTree::new().expect("an isolated tree");
    let mut invocation = tree.invocation(Path::new(env!("CARGO_BIN_EXE_winter-workspace")));
    invocation.args = args.iter().map(Into::into).collect();
    let Observation {
        stdout,
        stderr,
        exit,
    } = invoke(invocation).expect("the binary runs to its end");
    (
        String::from_utf8(stdout).expect("UTF-8 on stdout"),
        String::from_utf8(stderr).expect("UTF-8 on stderr"),
        exit,
    )
}

#[test]
fn version_prints_the_program_and_its_version_on_stdout() {
    let (stdout, stderr, exit) = run(&["--version"]);

    assert_eq!(stdout, "winter-workspace 0.1.0\n");
    assert_eq!(stderr, "");
    assert_eq!(exit, Exit::Code(0));
}

#[test]
fn help_prints_on_stdout_alone_and_succeeds() {
    let (stdout, stderr, exit) = run(&["--help"]);

    assert!(stdout.contains("winter-workspace"), "{stdout}");
    assert!(stdout.contains("--version"), "{stdout}");
    assert_eq!(stderr, "");
    assert_eq!(exit, Exit::Code(0));
}

#[test]
fn no_argument_prints_the_help_on_stderr_and_exits_2() {
    let (help, _, _) = run(&["--help"]);

    let (stdout, stderr, exit) = run(&[]);

    assert_eq!(stdout, "");
    assert_eq!(stderr, help);
    assert_eq!(exit, Exit::Code(2));
}

#[test]
fn an_unknown_option_exits_2_and_names_the_option_on_stderr_alone() {
    let (stdout, stderr, exit) = run(&["--nope"]);

    assert_eq!(stdout, "");
    assert!(stderr.contains("--nope"), "{stderr}");
    assert_eq!(exit, Exit::Code(2));
}

#[test]
fn an_unknown_command_exits_2_with_a_diagnostic_of_at_least_three_lines_on_stderr_alone() {
    let (stdout, stderr, exit) = run(&["nope"]);

    assert_eq!(stdout, "");
    assert!(stderr.lines().count() >= 3, "{stderr}");
    assert!(stderr.contains("nope"), "{stderr}");
    assert!(stderr.contains("winter-workspace --help"), "{stderr}");
    assert_eq!(exit, Exit::Code(2));
}
