//! The root of the command tree: what the binary answers before any command exists.

use std::ffi::OsString;
use std::os::unix::ffi::OsStringExt;

use winter_workspace_core::{ParseFault, Termination, Usage, UsageError};

#[path = "support/in_process.rs"]
mod in_process;

use in_process::InProcess;

/// The fault of a usage error at the root that wrote nothing to either handle, with the usage it carries.
fn usage_error(args: &[&str]) -> (ParseFault, Usage) {
    let ended = InProcess::new(args).run();
    assert_eq!(ended.stdout, "", "{args:?}");
    assert_eq!(ended.stderr, "", "{args:?}");
    assert_eq!(ended.termination.exit_code(), 2, "{args:?}");
    match ended.termination {
        Termination::Usage(error) => {
            let UsageError::Parse {
                command_path,
                usage,
                fault,
            } = *error;
            assert!(command_path.is_empty(), "{args:?}: {command_path:?}");
            (fault, usage)
        }
        other => panic!("{args:?}: expected a usage termination, got {other:?}"),
    }
}

fn usage_fault(args: &[&str]) -> ParseFault {
    usage_error(args).0
}

#[test]
fn help_is_written_to_stdout_and_succeeds() {
    let ended = InProcess::new(&["--help"]).run();

    assert!(matches!(ended.termination, Termination::Success));
    assert!(
        ended.stdout.contains("winter-workspace"),
        "{}",
        ended.stdout
    );
    assert!(ended.stdout.contains("--help"), "{}", ended.stdout);
    assert!(ended.stdout.contains("--version"), "{}", ended.stdout);
    assert_eq!(ended.stderr, "");
}

#[test]
fn no_argument_writes_nothing_and_returns_the_root_help_as_a_bare_group() {
    let help = InProcess::new(&["--help"]).run().stdout;

    let ended = InProcess::new(&[]).run();

    assert_eq!(ended.stdout, "");
    assert_eq!(ended.stderr, "");
    assert_eq!(ended.termination.exit_code(), 2);
    assert_eq!(
        ended.termination.diagnostic().as_deref(),
        Some(help.as_str())
    );
    let Termination::BareGroup { help: carried } = ended.termination else {
        panic!("expected a bare group, got {:?}", ended.termination);
    };
    assert_eq!(carried, help);
}

#[test]
fn an_unknown_option_is_a_usage_error() {
    let fault = usage_fault(&["--nope"]);

    assert!(
        matches!(&fault, ParseFault::UnknownOption { name } if name == "--nope"),
        "{fault:?}"
    );
}

#[test]
fn a_word_that_names_no_command_is_an_unknown_command() {
    for word in ["nope", "help"] {
        let fault = usage_fault(&[word]);

        assert!(
            matches!(&fault, ParseFault::UnknownCommand { name } if name == word),
            "{fault:?}"
        );
    }
}

#[test]
fn the_short_help_and_version_flags_are_usage_errors() {
    for flag in ["-h", "-V"] {
        let fault = usage_fault(&[flag]);

        assert!(
            matches!(&fault, ParseFault::UnknownOption { name } if name == flag),
            "{fault:?}"
        );
    }
}

#[test]
fn an_abbreviated_long_option_is_a_usage_error() {
    for abbreviated in ["--vers", "--hel"] {
        let fault = usage_fault(&[abbreviated]);

        assert!(
            matches!(&fault, ParseFault::UnknownOption { name } if name == abbreviated),
            "{fault:?}"
        );
    }
}

#[test]
fn version_is_written_to_stdout_as_one_line() {
    let ended = InProcess::new(&["--version"]).run();

    assert!(matches!(ended.termination, Termination::Success));
    assert_eq!(ended.stdout, "winter-workspace 0.1.0\n");
    assert_eq!(ended.stderr, "");
}

#[test]
fn a_fault_at_the_root_carries_the_usage_of_the_root() {
    let help = InProcess::new(&["--help"]).run().stdout;

    for args in [&["nope"][..], &["--nope"], &["--nope", "ws", "index"]] {
        let (_, usage) = usage_error(args);

        assert_eq!(usage.help_command, "winter-workspace --help", "{args:?}");
        assert!(
            help.lines().any(|line| line == usage.line),
            "{args:?}: {:?} is no line of\n{help}",
            usage.line
        );
    }
}

#[test]
fn a_usage_diagnostic_is_the_error_line_the_usage_line_and_the_help_command() {
    for (args, named) in [(&["nope"][..], "nope"), (&["--nope"], "--nope")] {
        let termination = InProcess::new(args).run().termination;

        assert_eq!(termination.exit_code(), 2, "{args:?}");
        let diagnostic = termination.diagnostic().expect("a diagnostic");
        let Termination::Usage(error) = termination else {
            panic!("{args:?}: expected a usage termination, got {termination:?}");
        };
        let UsageError::Parse { usage, .. } = *error;
        let lines: Vec<&str> = diagnostic.lines().collect();
        let [error_line, usage_line, help_line] = lines.as_slice() else {
            panic!("{args:?}: expected three lines, got {diagnostic:?}");
        };
        assert!(error_line.starts_with("error: "), "{args:?}: {diagnostic}");
        assert!(error_line.contains(named), "{args:?}: {diagnostic}");
        assert_eq!(*usage_line, usage.line, "{args:?}");
        assert!(
            help_line.contains(&usage.help_command),
            "{args:?}: {diagnostic}"
        );
    }
}

/// The fault and the usage of a usage error at the root, for arguments that need not be text.
fn usage_error_os(args: Vec<OsString>) -> (ParseFault, Usage) {
    let line = format!("{args:?}");
    let ended = InProcess::os_args(args).run();
    assert_eq!(ended.stdout, "", "{line}");
    assert_eq!(ended.stderr, "", "{line}");
    assert_eq!(ended.termination.exit_code(), 2, "{line}");
    match ended.termination {
        Termination::Usage(error) => {
            let UsageError::Parse {
                command_path,
                usage,
                fault,
            } = *error;
            assert!(command_path.is_empty(), "{line}: {command_path:?}");
            (fault, usage)
        }
        other => panic!("{line}: expected a usage termination, got {other:?}"),
    }
}

/// The names the root help lists as commands.
fn listed_commands(help: &str) -> Vec<&str> {
    help.lines()
        .skip_while(|line| *line != "Commands:")
        .skip(1)
        .take_while(|line| !line.is_empty())
        .filter_map(|line| line.split_whitespace().next())
        .collect()
}

#[test]
fn root_options_and_no_command_write_nothing_and_end_as_the_bare_root_does() {
    let help = InProcess::new(&["--help"]).run().stdout;

    for args in [
        &["-v"][..],
        &["--verbose"],
        &["--service-orchestrator", "X"],
        &["-v", "--service-orchestrator", "X"],
    ] {
        let ended = InProcess::new(args).run();

        assert_eq!(ended.stdout, "", "{args:?}");
        assert_eq!(ended.stderr, "", "{args:?}");
        assert_eq!(ended.termination.exit_code(), 2, "{args:?}");
        let Termination::BareGroup { help: carried } = ended.termination else {
            panic!(
                "{args:?}: expected a bare group, got {:?}",
                ended.termination
            );
        };
        assert_eq!(carried, help, "{args:?}");
    }
}

#[test]
fn the_root_help_names_the_visible_root_options() {
    let help = InProcess::new(&["--help"]).run().stdout;

    for option in ["-v, --verbose", "--service-orchestrator", "--version"] {
        assert!(help.contains(option), "{option}: {help}");
    }
}

#[test]
fn the_source_override_option_is_absent_from_the_help_and_accepted() {
    let help = InProcess::new(&["--help"]).run().stdout;
    assert!(!help.contains("source-override"), "{help}");

    for args in [
        &["--source-override", "T"][..],
        &["--source-override=T"],
        &["--source-override", "-t"],
    ] {
        let ended = InProcess::new(args).run();

        assert!(
            matches!(ended.termination, Termination::BareGroup { .. }),
            "{args:?}: {:?}",
            ended.termination
        );
    }
}

#[test]
fn a_repeated_root_option_is_a_usage_error_that_names_it() {
    for (args, option) in [
        (&["-v", "-v"][..], "--verbose"),
        (&["-vv"], "--verbose"),
        (
            &["--service-orchestrator", "a", "--service-orchestrator", "b"],
            "--service-orchestrator",
        ),
        (
            &["--source-override=a", "--source-override=b"],
            "--source-override",
        ),
    ] {
        let fault = usage_fault(args);

        assert!(
            matches!(&fault, ParseFault::RepeatedOption { option: named } if named == option),
            "{args:?}: {fault:?}"
        );
    }
}

#[test]
fn the_version_before_a_fault_is_written_and_succeeds() {
    for args in [&["--version", "--bogus"][..], &["--version", "ws", "bogus"]] {
        let ended = InProcess::new(args).run();

        assert!(
            matches!(ended.termination, Termination::Success),
            "{args:?}: {:?}",
            ended.termination
        );
        assert_eq!(ended.stdout, "winter-workspace 0.1.0\n", "{args:?}");
        assert_eq!(ended.stderr, "", "{args:?}");
    }
}

#[test]
fn the_help_before_a_fault_is_written_and_succeeds() {
    let help = InProcess::new(&["--help"]).run().stdout;

    let ended = InProcess::new(&["--help", "--bogus"]).run();

    assert!(
        matches!(ended.termination, Termination::Success),
        "{:?}",
        ended.termination
    );
    assert_eq!(ended.stdout, help);
    assert_eq!(ended.stderr, "");
}

#[test]
fn a_fault_before_the_version_or_the_help_is_a_usage_error() {
    for args in [&["--bogus", "--version"][..], &["--bogus", "--help"]] {
        let fault = usage_fault(args);

        assert!(
            matches!(&fault, ParseFault::UnknownOption { name } if name == "--bogus"),
            "{args:?}: {fault:?}"
        );
    }
}

#[test]
fn an_argument_that_is_not_utf_8_is_a_usage_error_of_the_root_wherever_it_stands_there() {
    let bad = || OsString::from_vec(b"pro\xffvider".to_vec());

    for args in [
        vec![bad()],
        vec!["--service-orchestrator".into(), bad()],
        vec![
            "--service-orchestrator".into(),
            bad(),
            "ws".into(),
            "index".into(),
            "feature-x".into(),
        ],
        vec!["--help".into(), bad()],
        vec!["--version".into(), bad()],
    ] {
        let line = format!("{args:?}");

        let (fault, usage) = usage_error_os(args);

        assert!(
            matches!(&fault, ParseFault::NotUtf8 { argument } if argument == "pro\u{fffd}vider"),
            "{line}: {fault:?}"
        );
        assert_eq!(usage.help_command, "winter-workspace --help", "{line}");
    }
}

#[test]
fn the_diagnostic_of_an_argument_that_is_not_utf_8_shows_it_with_the_invalid_bytes_replaced() {
    let termination = InProcess::os_args(vec![OsString::from_vec(b"a\xffb".to_vec())])
        .run()
        .termination;

    let diagnostic = termination.diagnostic().expect("a diagnostic");
    let lines: Vec<&str> = diagnostic.lines().collect();
    assert_eq!(lines.len(), 3, "{diagnostic}");
    assert!(lines[0].starts_with("error: "), "{diagnostic}");
    assert!(lines[0].ends_with("\"a\u{fffd}b\""), "{diagnostic}");
}

#[test]
fn every_command_the_root_help_lists_answers_its_own_help() {
    let help = InProcess::new(&["--help"]).run().stdout;
    let listed = listed_commands(&help);
    assert!(!listed.is_empty(), "{help}");

    for name in listed {
        let ended = InProcess::new(&[name, "--help"]).run();

        assert!(
            matches!(ended.termination, Termination::Success),
            "{name}: {:?}",
            ended.termination
        );
    }
}

#[test]
fn init_is_no_command_and_the_root_help_does_not_list_it() {
    let help = InProcess::new(&["--help"]).run().stdout;
    assert!(!listed_commands(&help).contains(&"init"), "{help}");

    let fault = usage_fault(&["init"]);

    assert!(
        matches!(&fault, ParseFault::UnknownCommand { name } if name == "init"),
        "{fault:?}"
    );
}

/// The groups and top-level commands this tree registers, in the order the help lists them. A change that adds
/// a group, or a top-level command that is not hidden, adds its name here.
const REGISTERED: [&str; 2] = ["repo", "ws"];

#[test]
fn the_root_help_lists_exactly_the_groups_and_top_level_commands_registered() {
    for (args, on_stdout) in [(&["--help"][..], true), (&[], false)] {
        let ended = InProcess::new(args).run();
        let help = if on_stdout {
            ended.stdout
        } else {
            ended.termination.diagnostic().expect("the root help")
        };

        assert_eq!(listed_commands(&help), REGISTERED, "{args:?}: {help}");
    }
}
