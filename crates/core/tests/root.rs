//! The root of the command tree: what the binary answers before any command exists.

use std::ffi::OsString;
use std::os::unix::ffi::OsStringExt;

use winter_workspace_core::{InternalError, ParseFault, Termination, Usage, UsageError};
use winter_workspace_test_support::{TempTree, WorkspaceBuilder};

#[path = "support/in_process.rs"]
mod in_process;

use in_process::InProcess;

/// The fault of a usage error at the root that wrote nothing to either handle, with the usage it carries.
#[track_caller]
fn usage_error(args: &[&str]) -> (ParseFault, Usage) {
    let ended = InProcess::new(args).run();
    match ended.wrote_nothing(2, &format!("{args:?}")) {
        Termination::Usage(error) => {
            let UsageError::Parse {
                command_path,
                usage,
                fault,
            } = *error
            else {
                panic!("expected a parse fault");
            };
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

    let termination = ended.wrote_nothing(2, "no argument");
    assert_eq!(termination.diagnostic().as_deref(), Some(help.as_str()));
    let Termination::BareGroup { help: carried } = termination else {
        panic!("expected a bare group, got {termination:?}");
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
        let UsageError::Parse { usage, .. } = *error else {
            panic!("expected a parse fault");
        };
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
#[track_caller]
fn usage_error_os(args: Vec<OsString>) -> (ParseFault, Usage) {
    let line = format!("{args:?}");
    let ended = InProcess::os_args(args).run();
    match ended.wrote_nothing(2, &line) {
        Termination::Usage(error) => {
            let UsageError::Parse {
                command_path,
                usage,
                fault,
            } = *error
            else {
                panic!("expected a parse fault");
            };
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

        let termination = ended.wrote_nothing(2, &format!("{args:?}"));
        let Termination::BareGroup { help: carried } = termination else {
            panic!("{args:?}: expected a bare group, got {termination:?}");
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
const REGISTERED: [&str; 3] = ["context", "repo", "ws"];

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

/// The record the runtime writes for the command path `ws index` under a switched log at debug.
const INDEX_RECORD: &str = "DEBUG parse: command path: ws index\n";

/// `ws index feature-x --json` after `options`, with `variable` as `WINTER_LOG_LEVEL` when given, in a fixture
/// workspace that holds the name. The run succeeded and left stdout one line of JSON.
fn index_run(options: &[&str], variable: Option<&str>) -> in_process::Ended {
    let tree = TempTree::new().expect("an isolated tree");
    let workspace = WorkspaceBuilder::new(&tree)
        .config("# workspace\n")
        .state("[env_index]\nfeature-x = 15\n")
        .build()
        .expect("a workspace");
    let args: Vec<&str> = options
        .iter()
        .chain(&["ws", "index", "feature-x", "--json"])
        .copied()
        .collect();
    let line = format!("{args:?} under {variable:?}");
    let mut invocation = InProcess::new(&args).cwd(&workspace.root);
    if let Some(value) = variable {
        invocation = invocation.env("WINTER_LOG_LEVEL", value);
    }

    let ended = invocation.run();

    assert!(
        matches!(ended.termination, Termination::Success),
        "{line}: {:?}",
        ended.termination
    );
    assert_eq!(ended.termination.exit_code(), 0, "{line}");
    assert_eq!(
        ended.stdout.lines().count(),
        1,
        "{line}: {:?}",
        ended.stdout
    );
    assert!(ended.stdout.ends_with('\n'), "{line}: {:?}", ended.stdout);
    let document: serde_json::Value = serde_json::from_str(&ended.stdout)
        .unwrap_or_else(|error| panic!("{line}: {error}: {:?}", ended.stdout));
    assert!(document.is_object(), "{line}: {document}");
    ended
}

#[test]
fn the_verbose_flag_writes_the_record_of_the_command_path_to_stderr_and_leaves_stdout_one_document()
{
    for flag in ["-v", "--verbose"] {
        assert_eq!(index_run(&[flag], None).stderr, INDEX_RECORD, "{flag}");
    }
}

#[test]
fn without_a_logging_switch_a_command_writes_nothing_to_stderr() {
    assert_eq!(index_run(&[], None).stderr, "");
}

#[test]
fn the_log_level_variable_at_debug_writes_the_record_without_the_verbose_flag() {
    for value in ["debug", "DEBUG", "Debug"] {
        assert_eq!(index_run(&[], Some(value)).stderr, INDEX_RECORD, "{value}");
    }
}

#[test]
fn the_log_level_variable_above_debug_writes_no_record() {
    for value in [
        "INFO", "info", "WARNING", "WARN", "ERROR", "CRITICAL", "FATAL", "fatal",
    ] {
        assert_eq!(index_run(&[], Some(value)).stderr, "", "{value}");
    }
}

#[test]
fn the_log_level_variable_that_names_no_level_or_is_notset_writes_nothing_and_succeeds() {
    for value in ["bogus", "NOTSET", "notset", "", "10", " DEBUG"] {
        assert_eq!(index_run(&[], Some(value)).stderr, "", "{value:?}");
    }
}

#[test]
fn the_verbose_flag_wins_over_every_value_of_the_log_level_variable() {
    for value in ["ERROR", "CRITICAL", "NOTSET", "bogus", ""] {
        assert_eq!(
            index_run(&["-v"], Some(value)).stderr,
            INDEX_RECORD,
            "{value:?}"
        );
    }
}

#[test]
fn an_invocation_that_ends_at_the_root_writes_no_record_though_logging_is_asked_for() {
    for args in [
        &["-v", "--help"][..],
        &["-v", "--version"],
        &["-v"],
        &["-v", "nope"],
        &["-v", "--bogus", "ws", "index"],
    ] {
        let ended = InProcess::new(args).env("WINTER_LOG_LEVEL", "DEBUG").run();

        assert_eq!(ended.stderr, "", "{args:?}");
    }
    for args in [&["-v", "--help"][..], &["-v", "--version"]] {
        let ended = InProcess::new(args).run();

        assert!(
            matches!(ended.termination, Termination::Success),
            "{args:?}: {:?}",
            ended.termination
        );
        assert_ne!(ended.stdout, "", "{args:?}");
    }
}

#[test]
fn the_help_of_a_group_writes_the_record_that_names_the_group() {
    let ended = InProcess::new(&["-v", "ws", "--help"]).run();

    assert!(
        matches!(ended.termination, Termination::Success),
        "{:?}",
        ended.termination
    );
    assert_ne!(ended.stdout, "");
    assert_eq!(ended.stderr, "DEBUG parse: command path: ws\n");
}

#[test]
fn a_usage_error_of_a_command_writes_the_record_that_names_the_command() {
    let ended = InProcess::new(&["-v", "ws", "index", "--bogus"]).run();

    assert_eq!(ended.termination.exit_code(), 2);
    assert!(
        matches!(ended.termination, Termination::Usage(_)),
        "{:?}",
        ended.termination
    );
    assert_eq!(ended.stdout, "");
    assert_eq!(ended.stderr, INDEX_RECORD);
}

#[test]
fn the_record_is_written_for_a_command_stage_ending_under_the_variable_alone() {
    for (args, record) in [
        (&["ws", "--help"][..], "DEBUG parse: command path: ws\n"),
        (&["ws", "index", "--bogus"], INDEX_RECORD),
        (&["ws", "index"], INDEX_RECORD),
        (&["ws"], "DEBUG parse: command path: ws\n"),
    ] {
        let ended = InProcess::new(args).env("WINTER_LOG_LEVEL", "DEBUG").run();

        assert_eq!(ended.stderr, record, "{args:?}");
    }
}

/// No present command defers the interrupt, so none reads whether one is pending.
#[test]
fn a_pending_interrupt_leaves_the_index_of_a_name_ending_as_it_does_without_one() {
    let tree = TempTree::new().expect("an isolated tree");
    let workspace = WorkspaceBuilder::new(&tree)
        .config("# workspace\n")
        .state("[env_index]\nfeature-x = 15\n")
        .build()
        .expect("a workspace");
    for args in [
        &["ws", "index", "feature-x"][..],
        &["ws", "index", "feature-x", "--json"],
    ] {
        let plain = InProcess::new(args).cwd(&workspace.root).run();
        let pending = InProcess::new(args)
            .cwd(&workspace.root)
            .interrupt_pending()
            .run();

        assert!(
            matches!(plain.termination, Termination::Success),
            "{args:?}: {:?}",
            plain.termination
        );
        assert!(
            matches!(pending.termination, Termination::Success),
            "{args:?}: {:?}",
            pending.termination
        );
        assert!(plain.stdout.contains("15"), "{args:?}: {:?}", plain.stdout);
        assert_eq!(pending.stdout, plain.stdout, "{args:?}");
        assert_eq!(pending.stderr, plain.stderr, "{args:?}");
    }
}

/// The error a process meets when its working directory was removed.
fn no_working_directory() -> std::io::Error {
    std::io::Error::new(
        std::io::ErrorKind::NotFound,
        "the working directory is gone",
    )
}

#[test]
fn a_dispatched_command_without_a_readable_working_directory_is_an_internal_failure_that_carries_the_error()
 {
    for args in [
        &["ws", "index", "feature-x"][..],
        &["ws", "index", "feature-x", "--json"],
    ] {
        let ended = InProcess::new(args).cwd_error(no_working_directory()).run();

        let Termination::Internal(error) = &ended.termination else {
            panic!(
                "{args:?}: expected an internal failure, got {:?}",
                ended.termination
            );
        };
        assert!(
            matches!(**error, InternalError::Unexpected { .. }),
            "{args:?}: {error:?}"
        );
        let source = std::error::Error::source(&**error).expect("a source");
        let source = source
            .downcast_ref::<std::io::Error>()
            .expect("the error of the working directory");
        assert_eq!(source.kind(), std::io::ErrorKind::NotFound, "{args:?}");
        assert_eq!(ended.termination.exit_code(), 1, "{args:?}");
        assert_eq!(
            ended.termination.diagnostic().as_deref(),
            Some("error: the working directory is gone"),
            "{args:?}"
        );
        assert_eq!(ended.stdout, "", "{args:?}");
        assert_eq!(ended.stderr, "", "{args:?}");
    }
}

#[test]
fn an_invocation_that_dispatches_no_command_ends_the_same_without_a_readable_working_directory() {
    for args in [
        &["--help"][..],
        &["--version"],
        &["ws", "--help"],
        &["ws", "index", "--help"],
        &["ws", "index", "--bogus"],
        &["ws", "index"],
        &["-v", "ws", "index", "--bogus"],
        &["ws"],
        &["nope"],
        &[],
    ] {
        let with = InProcess::new(args).run();

        let without = InProcess::new(args).cwd_error(no_working_directory()).run();

        assert_eq!(
            format!("{:?}", without.termination),
            format!("{:?}", with.termination),
            "{args:?}"
        );
        assert_eq!(
            without.termination.exit_code(),
            with.termination.exit_code(),
            "{args:?}"
        );
        assert_eq!(
            without.termination.diagnostic(),
            with.termination.diagnostic(),
            "{args:?}"
        );
        assert_eq!(without.stdout, with.stdout, "{args:?}");
        assert_eq!(without.stderr, with.stderr, "{args:?}");
    }
}

fn path(names: &[&str]) -> Option<Vec<String>> {
    Some(names.iter().map(|name| (*name).to_owned()).collect())
}

#[test]
fn a_command_that_runs_is_announced_to_the_tracer_by_its_command_path() {
    assert_eq!(index_run(&[], None).announced, path(&["ws", "index"]));
}

#[test]
fn a_usage_error_and_the_help_of_a_command_stage_are_announced_by_the_path_resolved_so_far() {
    for (args, announced) in [
        (&["ws", "index", "--bogus"][..], &["ws", "index"][..]),
        (&["ws", "--help"], &["ws"]),
        (&["ws", "nope"], &["ws"]),
    ] {
        assert_eq!(
            InProcess::new(args).run().announced,
            path(announced),
            "{args:?}"
        );
    }
}

#[test]
fn an_invocation_that_ends_at_the_root_announces_no_command() {
    for args in [
        &["--version"][..],
        &["--help"],
        &["--bogus"],
        &["nope"],
        &["-v", "-v", "ws", "index", "feature-x"],
        &[],
    ] {
        assert_eq!(InProcess::new(args).run().announced, None, "{args:?}");
    }
}
