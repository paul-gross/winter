//! The root of the command tree, as the built binary answers it: streams and exit codes of a real process.

use std::ffi::OsString;
use std::os::unix::ffi::OsStringExt;
use std::path::Path;

use winter_workspace_test_support::{Exit, Observation, TempTree, WorkspaceBuilder, invoke};

/// Runs the built binary with `args` in an isolated tree and returns its two streams as text with its exit.
fn run(args: &[&str]) -> (String, String, Exit) {
    run_with(args.iter().map(Into::into).collect(), &[])
}

/// Runs the built binary with `args` in an isolated tree, with `variables` added to the pinned environment,
/// and returns its two streams as text with its exit.
fn run_with(args: Vec<OsString>, variables: &[(&str, &str)]) -> (String, String, Exit) {
    let tree = TempTree::new().expect("an isolated tree");
    let mut invocation = tree.invocation(Path::new(env!("CARGO_BIN_EXE_winter-workspace")));
    invocation.args = args;
    for (key, value) in variables {
        invocation.env.set(key, value);
    }
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

#[test]
fn an_argument_that_is_not_utf_8_exits_2_with_its_diagnostic_on_stderr_alone() {
    for args in [
        vec![OsString::from_vec(b"na\xffme".to_vec())],
        vec![
            "ws".into(),
            "index".into(),
            OsString::from_vec(b"na\xffme".to_vec()),
        ],
        vec!["--help".into(), OsString::from_vec(b"na\xffme".to_vec())],
    ] {
        let line = format!("{args:?}");

        let (stdout, stderr, exit) = run_with(args, &[]);

        assert_eq!(stdout, "", "{line}");
        assert!(stderr.contains("na\u{fffd}me"), "{line}: {stderr}");
        assert_eq!(exit, Exit::Code(2), "{line}");
    }
}

#[test]
fn help_is_the_same_bytes_at_every_terminal_width_and_no_line_is_longer_than_100_columns() {
    let (narrow, _, narrow_exit) = run_with(vec!["--help".into()], &[("COLUMNS", "40")]);
    let (wide, _, wide_exit) = run_with(vec!["--help".into()], &[("COLUMNS", "200")]);

    assert_eq!(narrow_exit, Exit::Code(0));
    assert_eq!(wide_exit, Exit::Code(0));
    assert_eq!(narrow, wide);
    for line in narrow.lines() {
        assert!(line.chars().count() <= 100, "{line:?}");
    }
}

#[test]
fn the_root_help_alone_ends_with_the_line_that_names_the_tracing_variable_and_the_readme() {
    let names_both = |line: &str| {
        line.contains("WINTER_OTEL_EXPORTER_OTLP_ENDPOINT") && line.contains("README.md")
    };

    let (help, _, _) = run(&["--help"]);
    let (_, bare_root, bare_root_exit) = run(&[]);

    let notices: Vec<&str> = help.lines().filter(|line| names_both(line)).collect();
    let [notice] = notices.as_slice() else {
        panic!("expected one line that names both: {help}");
    };
    assert_eq!(help.lines().last(), Some(*notice), "{help}");
    let options = help
        .lines()
        .position(|line| line == "Options:")
        .expect("the options of the root");
    let noticed = help.lines().position(|line| line == *notice);
    assert!(noticed > Some(options), "{help}");
    assert_eq!(bare_root_exit, Exit::Code(2));
    assert!(bare_root.lines().any(|line| line == *notice), "{bare_root}");

    for args in [&["ws", "--help"][..], &["ws", "index", "--help"]] {
        let (stdout, stderr, exit) = run(args);

        assert_eq!(exit, Exit::Code(0), "{args:?}");
        assert_eq!(stderr, "", "{args:?}");
        assert!(stdout.contains("ws"), "{args:?}: {stdout}");
        for word in ["WINTER_OTEL_EXPORTER_OTLP_ENDPOINT", "README.md"] {
            assert!(!stdout.contains(word), "{args:?}: {word}: {stdout}");
        }
    }
}

/// Runs the built binary with `options` before `ws index feature-x --json`, and `variables` added to the pinned
/// environment, in a workspace of an isolated tree that holds the name.
fn index_run(options: &[&str], variables: &[(&str, &str)]) -> (String, String, Exit) {
    let tree = TempTree::new().expect("an isolated tree");
    let workspace = WorkspaceBuilder::new(&tree)
        .config("# workspace\n")
        .state("[env_index]\nfeature-x = 15\n")
        .build()
        .expect("a workspace");
    let mut invocation = tree.invocation(Path::new(env!("CARGO_BIN_EXE_winter-workspace")));
    invocation.cwd = workspace.root;
    invocation.args = options
        .iter()
        .chain(&["ws", "index", "feature-x", "--json"])
        .map(Into::into)
        .collect();
    for (key, value) in variables {
        invocation.env.set(key, value);
    }
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
fn a_logging_switch_writes_its_record_on_stderr_and_leaves_the_document_on_stdout_as_it_was() {
    let (plain, plain_stderr, plain_exit) = index_run(&[], &[]);

    assert_eq!(plain_exit, Exit::Code(0));
    assert_eq!(plain_stderr, "");
    assert_eq!(plain.lines().count(), 1, "{plain:?}");
    assert!(
        plain.starts_with('{') && plain.ends_with("}\n"),
        "{plain:?}"
    );
    assert!(plain.contains("feature-x"), "{plain:?}");

    for (options, variables) in [
        (&["-v"][..], &[][..]),
        (&["--verbose"], &[]),
        (&[], &[("WINTER_LOG_LEVEL", "DEBUG")]),
        (&["-v"], &[("WINTER_LOG_LEVEL", "ERROR")]),
    ] {
        let line = format!("{options:?} under {variables:?}");

        let (stdout, stderr, exit) = index_run(options, variables);

        assert_eq!(exit, Exit::Code(0), "{line}");
        assert_eq!(stdout, plain, "{line}");
        assert_eq!(stderr.lines().count(), 1, "{line}: {stderr:?}");
        assert!(stderr.ends_with('\n'), "{line}: {stderr:?}");
        assert!(stderr.contains("ws index"), "{line}: {stderr:?}");
    }
}

#[test]
fn a_command_that_needs_a_workspace_exits_1_with_nothing_on_stdout_outside_any_workspace() {
    let (stdout, stderr, exit) = run(&["ws", "index", "feature-x"]);

    assert_eq!(stdout, "");
    assert!(stderr.starts_with("error: "), "{stderr}");
    assert_eq!(stderr.lines().count(), 1, "{stderr}");
    assert_eq!(exit, Exit::Code(1));
}
