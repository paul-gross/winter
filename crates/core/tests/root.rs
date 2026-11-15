//! The root of the command tree: what the binary answers before any command exists.

use std::ffi::OsString;
use std::path::PathBuf;

use winter_workspace_core::{
    Identity, Invocation, ParseFault, ProcessEnv, Termination, UsageError, run,
};

struct Ended {
    termination: Termination,
    stdout: String,
    stderr: String,
}

fn run_with(args: &[&str]) -> Ended {
    let mut stdin: &[u8] = &[];
    let mut stdout = Vec::new();
    let mut stderr = Vec::new();
    let termination = run(Invocation {
        args: args.iter().map(OsString::from).collect(),
        cwd: PathBuf::from("/"),
        env: ProcessEnv::from_vars([]),
        identity: Identity {
            program: "winter-workspace",
            version: "0.1.0",
        },
        stdin: &mut stdin,
        stdout: &mut stdout,
        stderr: &mut stderr,
        stdout_is_terminal: false,
        stderr_is_terminal: false,
    });
    Ended {
        termination,
        stdout: String::from_utf8(stdout).expect("UTF-8 on stdout"),
        stderr: String::from_utf8(stderr).expect("UTF-8 on stderr"),
    }
}

/// The fault of a usage error that wrote nothing to either handle.
fn usage_fault(args: &[&str]) -> ParseFault {
    let ended = run_with(args);
    assert_eq!(ended.stdout, "", "{args:?}");
    assert_eq!(ended.stderr, "", "{args:?}");
    match ended.termination {
        Termination::Usage(UsageError::Parse {
            command_path,
            fault,
        }) => {
            assert!(command_path.is_empty(), "{args:?}: {command_path:?}");
            fault
        }
        other => panic!("{args:?}: expected a usage termination, got {other:?}"),
    }
}

#[test]
fn help_is_written_to_stdout_and_succeeds() {
    let ended = run_with(&["--help"]);

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
    let help = run_with(&["--help"]).stdout;

    let ended = run_with(&[]);

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
    let ended = run_with(&["--version"]);

    assert!(matches!(ended.termination, Termination::Success));
    assert_eq!(ended.stdout, "winter-workspace 0.1.0\n");
    assert_eq!(ended.stderr, "");
}

#[test]
fn a_usage_error_exits_2_and_its_diagnostic_names_the_fault() {
    let termination = run_with(&["--nope"]).termination;

    assert_eq!(termination.exit_code(), 2);
    let diagnostic = termination.diagnostic().expect("a diagnostic");
    assert!(diagnostic.contains("--nope"), "{diagnostic}");
}
