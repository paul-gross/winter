//! `ws index`: the index of a feature env name, looked up in a fixture workspace and never recorded.

use std::collections::BTreeMap;
use std::ffi::OsString;
use std::os::unix::ffi::OsStringExt;
use std::path::{Path, PathBuf};

use serde_json::{Value, json};
use winter_workspace_core::{ConfigError, FileCause, ParseFault, Termination, Usage, UsageError};
use winter_workspace_test_support::{TempTree, WorkspaceBuilder};

#[path = "support/in_process.rs"]
mod in_process;

use in_process::{Ended, InProcess};

/// A configuration file that declares nothing, so both keys take their defaults.
const DEFAULTS: &str = "# workspace\n";

/// The document a successful `ws index NAME --json` in `cwd` writes, which is all it writes.
fn document(cwd: &Path, name: &str) -> Value {
    let ended = InProcess::new(&["ws", "index", name, "--json"])
        .cwd(cwd)
        .run();
    assert!(
        matches!(ended.termination, Termination::Success),
        "{name}: {:?}",
        ended.termination
    );
    assert_eq!(ended.stderr, "", "{name}");
    serde_json::from_str(&ended.stdout)
        .unwrap_or_else(|error| panic!("{name}: {error}: {:?}", ended.stdout))
}

/// The configuration error `ws index feature-x --json` in `cwd` ends with, having written nothing.
fn config_error(cwd: &Path) -> ConfigError {
    let ended = InProcess::new(&["ws", "index", "feature-x", "--json"])
        .cwd(cwd)
        .run();
    assert_eq!(ended.stdout, "");
    assert_eq!(ended.stderr, "");
    assert_eq!(ended.termination.exit_code(), 1);
    match ended.termination {
        Termination::Config(error) => *error,
        other => panic!("expected a configuration error, got {other:?}"),
    }
}

/// What an entry below a directory is, with the content of a file and the target of a link.
#[derive(Debug, PartialEq, Eq)]
enum Found {
    Directory,
    File(Vec<u8>),
    Link(PathBuf),
}

/// Every entry below `directory`, by its path from there.
fn snapshot(directory: &Path) -> BTreeMap<PathBuf, Found> {
    fn walk(root: &Path, directory: &Path, found: &mut BTreeMap<PathBuf, Found>) {
        for entry in std::fs::read_dir(directory).expect("a readable directory") {
            let path = entry.expect("a directory entry").path();
            let relative = path
                .strip_prefix(root)
                .expect("a path below the root")
                .to_path_buf();
            let kind = std::fs::symlink_metadata(&path)
                .expect("an entry")
                .file_type();
            if kind.is_symlink() {
                let target = std::fs::read_link(&path).expect("a link");
                found.insert(relative, Found::Link(target));
            } else if kind.is_dir() {
                found.insert(relative, Found::Directory);
                walk(root, &path, found);
            } else {
                let content = std::fs::read(&path).expect("a readable file");
                found.insert(relative, Found::File(content));
            }
        }
    }

    let mut found = BTreeMap::new();
    walk(directory, directory, &mut found);
    found
}

fn tree() -> TempTree {
    TempTree::new().expect("an isolated tree")
}

#[test]
fn a_recorded_name_gives_its_recorded_index_from_the_registry_and_changes_nothing() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config(DEFAULTS)
        .state("[env_index]\nfeature-x = 15\n")
        .build()
        .expect("a workspace");
    let before = snapshot(&workspace.root);

    let document = document(&workspace.root, "feature-x");

    assert_eq!(
        document,
        json!({"name": "feature-x", "index": 15, "source": "registry"})
    );
    assert_eq!(snapshot(&workspace.root), before);
}

#[test]
fn an_unrecorded_name_gives_its_hash_slot_as_suggested_and_changes_nothing() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config(DEFAULTS)
        .build()
        .expect("a workspace");
    let before = snapshot(&workspace.root);

    let document = document(&workspace.root, "feature-x");

    assert_eq!(
        document,
        json!({"name": "feature-x", "index": 40, "source": "suggested"})
    );
    assert_eq!(snapshot(&workspace.root), before);
    assert!(!workspace.root.join(".winter/state.toml").exists());
}

#[test]
fn a_hash_slot_the_registry_records_for_another_name_is_still_suggested() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config(DEFAULTS)
        .state("[env_index]\nother = 40\n")
        .build()
        .expect("a workspace");
    let before = snapshot(&workspace.root);

    let document = document(&workspace.root, "feature-x");

    assert_eq!(
        document,
        json!({"name": "feature-x", "index": 40, "source": "suggested"})
    );
    assert_eq!(snapshot(&workspace.root), before);
}

#[test]
fn a_recorded_index_outside_the_layout_comes_back_as_recorded() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config(DEFAULTS)
        .state("[env_index]\nbelow = -3\nfar = 5000000000\n")
        .build()
        .expect("a workspace");

    assert_eq!(
        document(&workspace.root, "below"),
        json!({"name": "below", "index": -3, "source": "registry"})
    );
    assert_eq!(
        document(&workspace.root, "far"),
        json!({"name": "far", "index": 5_000_000_000_i64, "source": "registry"})
    );
}

#[test]
fn a_name_with_a_double_quote_and_a_backslash_comes_back_equal() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config(DEFAULTS)
        .build()
        .expect("a workspace");
    let name = "fe\"at\\ure";

    let document = document(&workspace.root, name);

    assert_eq!(document["name"], json!(name));
    assert_eq!(document["source"], json!("suggested"));
}

#[test]
fn the_aliases_and_the_slot_count_of_the_configuration_are_read() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config("env_aliases = [\"one\", \"two\"]\nenvs_per_workspace = 12\n")
        .build()
        .expect("a workspace");

    assert_eq!(
        document(&workspace.root, "two"),
        json!({"name": "two", "index": 2, "source": "suggested"})
    );
    // The first two bytes of the SHA-1 of `feature-x` are 0xcbc1, 52161; the band 4..=12 has 9 slots.
    assert_eq!(
        document(&workspace.root, "feature-x"),
        json!({"name": "feature-x", "index": 4 + 52161 % 9, "source": "suggested"})
    );
}

#[test]
fn a_recorded_alias_gives_the_recorded_index_whatever_its_position() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config(DEFAULTS)
        .state("[env_index]\ngamma = 31\n")
        .build()
        .expect("a workspace");

    assert_eq!(
        document(&workspace.root, "gamma"),
        json!({"name": "gamma", "index": 31, "source": "registry"})
    );
}

#[test]
fn an_alias_listed_more_than_once_gives_its_last_position() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config("env_aliases = [\"a\", \"b\", \"a\"]\n")
        .build()
        .expect("a workspace");

    assert_eq!(
        document(&workspace.root, "a"),
        json!({"name": "a", "index": 3, "source": "suggested"})
    );
    assert_eq!(
        document(&workspace.root, "b"),
        json!({"name": "b", "index": 2, "source": "suggested"})
    );
}

#[test]
fn without_the_json_flag_stdout_starts_with_the_integer() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config(DEFAULTS)
        .state("[env_index]\nrecorded = 15\n")
        .build()
        .expect("a workspace");

    // The name, its index, and whether anything follows the integer on the line.
    for (name, index, marked) in [
        ("recorded", "15", false),
        ("beta", "2", false),
        ("feature-x", "40", true),
    ] {
        let ended = InProcess::new(&["ws", "index", name])
            .cwd(&workspace.root)
            .run();

        assert!(
            matches!(ended.termination, Termination::Success),
            "{name}: {:?}",
            ended.termination
        );
        assert_eq!(ended.stderr, "", "{name}");
        assert_eq!(
            ended.stdout.lines().count(),
            1,
            "{name}: {:?}",
            ended.stdout
        );
        let line = ended.stdout.trim_end_matches('\n');
        let (first, rest) = line.split_once(' ').unwrap_or((line, ""));
        assert_eq!(first, index, "{name}: {line:?}");
        assert_eq!(!rest.is_empty(), marked, "{name}: {line:?}");
    }
}

#[test]
fn the_local_alias_list_replaces_the_shared_one() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config("env_aliases = [\"one\", \"two\"]\n")
        .local_config("env_aliases = [\"three\"]\n")
        .build()
        .expect("a workspace");

    assert_eq!(
        document(&workspace.root, "three"),
        json!({"name": "three", "index": 1, "source": "suggested"})
    );
    // With one alias the band is 3..=48, so no shared alias keeps a position.
    for shared in ["one", "two"] {
        let document = document(&workspace.root, shared);

        assert!(document["index"].as_i64() >= Some(3), "{document}");
    }
}

#[test]
fn a_bare_empty_string_of_aliases_leaves_no_alias() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config("env_aliases = \"\"\n")
        .build()
        .expect("a workspace");

    // The band is 2..=48, 47 slots: 52161 % 47 is 38, and the two bytes of `a`, 0x86f7, give 34551 % 47, 6.
    // One alias would make the band 3..=48 and put `feature-x` at 46; the default ten put `a` at 42.
    assert_eq!(document(&workspace.root, "feature-x")["index"], json!(40));
    assert_eq!(document(&workspace.root, "a")["index"], json!(8));
    let ended = InProcess::new(&["ws", "index", ""])
        .cwd(&workspace.root)
        .run();
    assert!(ended.stdout.contains(' '), "{:?}", ended.stdout);
}

#[test]
fn a_slot_count_beyond_32_bits_gives_the_hash_itself_past_the_band_start() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config("envs_per_workspace = 5000000000\n")
        .build()
        .expect("a workspace");

    assert_eq!(
        document(&workspace.root, "feature-x"),
        json!({"name": "feature-x", "index": 12 + 52161, "source": "suggested"})
    );
}

#[test]
fn a_state_file_that_is_not_valid_toml_is_an_empty_registry() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config(DEFAULTS)
        .state("[env_index\nfeature-x = 15\n")
        .build()
        .expect("a workspace");
    let before = snapshot(&workspace.root);

    let ended = InProcess::new(&["ws", "index", "feature-x", "--json"])
        .cwd(&workspace.root)
        .run();

    assert!(matches!(ended.termination, Termination::Success));
    assert_eq!(ended.stderr, "");
    let document: Value = serde_json::from_str(&ended.stdout).expect("a JSON document");
    assert_eq!(
        document,
        json!({"name": "feature-x", "index": 40, "source": "suggested"})
    );
    assert_eq!(snapshot(&workspace.root), before);
}

#[test]
fn a_state_file_that_cannot_be_read_is_an_empty_registry() {
    let tree = tree();
    // A directory in the place of the state file: it exists, and reading it fails.
    let workspace = WorkspaceBuilder::new(&tree)
        .config(DEFAULTS)
        .file(".winter/state.toml/keep", b"")
        .build()
        .expect("a workspace");

    assert_eq!(
        document(&workspace.root, "feature-x"),
        json!({"name": "feature-x", "index": 40, "source": "suggested"})
    );
}

#[test]
fn a_working_directory_two_levels_below_the_root_finds_the_workspace() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config("env_aliases = [\"one\", \"two\"]\nenvs_per_workspace = 12\n")
        .file("alpha/project/keep", b"")
        .build()
        .expect("a workspace");

    let document = document(&workspace.root.join("alpha/project"), "two");

    assert_eq!(
        document,
        json!({"name": "two", "index": 2, "source": "suggested"})
    );
}

#[test]
fn a_directory_with_no_workspace_above_is_a_root_not_found_error() {
    let tree = tree();
    let outside = tree.root().join("home");

    let error = config_error(&outside);

    assert!(
        matches!(&error, ConfigError::RootNotFound { start } if *start == outside),
        "{error:?}"
    );
}

#[test]
fn a_bare_winter_directory_is_no_workspace() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .file(".winter/state.toml", b"[env_index]\nfeature-x = 15\n")
        .build()
        .expect("a workspace directory");

    let error = config_error(&workspace.root);

    assert!(
        matches!(&error, ConfigError::RootNotFound { start } if *start == workspace.root),
        "{error:?}"
    );
}

#[test]
fn a_configuration_file_that_is_not_valid_toml_is_a_file_read_error() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config("env_aliases = [\n")
        .build()
        .expect("a workspace");

    let error = config_error(&workspace.root);

    assert!(
        matches!(
            &error,
            ConfigError::FileRead { path, cause: FileCause::Toml(_) }
                if *path == workspace.root.join(".winter/config.toml")
        ),
        "{error:?}"
    );
}

#[test]
fn a_local_configuration_file_that_is_not_valid_toml_is_a_file_read_error() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config(DEFAULTS)
        .local_config("envs_per_workspace = = 3\n")
        .build()
        .expect("a workspace");

    let error = config_error(&workspace.root);

    assert!(
        matches!(
            &error,
            ConfigError::FileRead { path, cause: FileCause::Toml(_) }
                if *path == workspace.root.join(".winter/config.local.toml")
        ),
        "{error:?}"
    );
}

#[test]
fn a_configuration_file_that_is_not_utf_8_is_a_file_read_error_with_the_toml_cause() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .file(".winter/config.toml", &[0xff, 0xfe, b'\n'])
        .build()
        .expect("a workspace");

    let error = config_error(&workspace.root);

    assert!(
        matches!(
            &error,
            ConfigError::FileRead {
                cause: FileCause::Toml(_),
                ..
            }
        ),
        "{error:?}"
    );
}

#[test]
fn a_local_configuration_file_that_cannot_be_read_is_a_file_read_error_with_the_io_cause() {
    let tree = tree();
    // A directory in the place of the local file: it exists, and reading it fails.
    let workspace = WorkspaceBuilder::new(&tree)
        .config(DEFAULTS)
        .file(".winter/config.local.toml/keep", b"")
        .build()
        .expect("a workspace");

    let error = config_error(&workspace.root);

    assert!(
        matches!(
            &error,
            ConfigError::FileRead { path, cause: FileCause::Io(_) }
                if *path == workspace.root.join(".winter/config.local.toml")
        ),
        "{error:?}"
    );
}

#[test]
fn a_slot_count_smaller_than_the_aliases_allow_is_refused() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config("env_aliases = [\"one\", \"two\"]\nenvs_per_workspace = 3\n")
        .build()
        .expect("a workspace");

    let error = config_error(&workspace.root);

    assert!(
        matches!(
            error,
            ConfigError::EnvsPerWorkspaceTooSmall {
                envs_per_workspace: 3,
                aliases: 2
            }
        ),
        "{error:?}"
    );
}

#[test]
fn the_ws_help_lists_index() {
    let ended = InProcess::new(&["ws", "--help"]).cwd(Path::new("/")).run();

    assert!(matches!(ended.termination, Termination::Success));
    assert!(ended.stdout.contains("index"), "{}", ended.stdout);
    assert_eq!(ended.stderr, "");

    let root = InProcess::new(&["--help"]).cwd(Path::new("/")).run();
    assert!(root.stdout.contains("ws"), "{}", root.stdout);
}

#[test]
fn ws_alone_writes_nothing_and_ends_as_a_bare_group_with_the_ws_help() {
    let help = InProcess::new(&["ws", "--help"])
        .cwd(Path::new("/"))
        .run()
        .stdout;

    let ended = InProcess::new(&["ws"]).cwd(Path::new("/")).run();

    assert_eq!(ended.stdout, "");
    assert_eq!(ended.stderr, "");
    assert_eq!(ended.termination.exit_code(), 2);
    assert_eq!(
        ended.termination.diagnostic().as_deref(),
        Some(help.as_str())
    );
    assert!(
        matches!(ended.termination, Termination::BareGroup { .. }),
        "{:?}",
        ended.termination
    );
}

/// The parts of a usage error that wrote nothing to either handle and exits 2.
fn usage_error(args: &[&str]) -> (Vec<String>, Usage, ParseFault) {
    usage_parts(
        InProcess::new(args).cwd(Path::new("/")).run(),
        &format!("{args:?}"),
    )
}

/// The parts of the usage error `ended` holds, which wrote nothing to either handle and exits 2.
fn usage_parts(ended: Ended, line: &str) -> (Vec<String>, Usage, ParseFault) {
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
            (command_path, usage, fault)
        }
        other => panic!("{line}: expected a usage termination, got {other:?}"),
    }
}

/// Asserts that `usage` is that of the node `help_args` prints the help of.
fn assert_usage_of(usage: &Usage, help_args: &[&str]) {
    let help = InProcess::new(help_args).cwd(Path::new("/")).run().stdout;
    assert_eq!(
        usage.help_command,
        format!("winter-workspace {}", help_args.join(" "))
    );
    assert!(
        help.lines().any(|line| line == usage.line),
        "{:?} is no line of\n{help}",
        usage.line
    );
}

#[test]
fn an_unknown_command_under_ws_carries_the_usage_of_ws() {
    let (command_path, usage, fault) = usage_error(&["ws", "nope"]);

    assert_eq!(command_path, ["ws"]);
    assert!(
        matches!(&fault, ParseFault::UnknownCommand { name } if name == "nope"),
        "{fault:?}"
    );
    assert_usage_of(&usage, &["ws", "--help"]);
}

#[test]
fn an_unknown_option_of_ws_index_carries_the_usage_of_ws_index() {
    let (command_path, usage, fault) = usage_error(&["ws", "index", "--bogus"]);

    assert_eq!(command_path, ["ws", "index"]);
    assert!(
        matches!(&fault, ParseFault::UnknownOption { name } if name == "--bogus"),
        "{fault:?}"
    );
    assert_usage_of(&usage, &["ws", "index", "--help"]);
}

#[test]
fn ws_index_without_a_name_is_a_missing_argument() {
    for args in [&["ws", "index"][..], &["ws", "index", "--json"][..]] {
        let (command_path, usage, fault) = usage_error(args);

        assert_eq!(command_path, ["ws", "index"], "{args:?}");
        assert!(
            matches!(&fault, ParseFault::MissingArgument { metavar } if metavar.contains("NAME")),
            "{args:?}: {fault:?}"
        );
        assert_usage_of(&usage, &["ws", "index", "--help"]);
    }
}

#[test]
fn a_second_name_is_an_unexpected_argument() {
    let (command_path, usage, fault) = usage_error(&["ws", "index", "one", "two"]);

    assert_eq!(command_path, ["ws", "index"]);
    assert!(
        matches!(&fault, ParseFault::UnexpectedArgument { values } if values == &["two"]),
        "{fault:?}"
    );
    assert_usage_of(&usage, &["ws", "index", "--help"]);
}

#[test]
fn two_names_too_many_are_both_rendered_each_quoted() {
    let ended = InProcess::new(&["ws", "index", "one", "two", "three"])
        .cwd(Path::new("/"))
        .run();

    assert_eq!(ended.termination.exit_code(), 2);
    let diagnostic = ended.termination.diagnostic().expect("a diagnostic");
    let lines: Vec<&str> = diagnostic.lines().collect();
    let [error_line, usage_line, help_line] = lines.as_slice() else {
        panic!("expected three lines, got {diagnostic:?}");
    };
    assert!(error_line.starts_with("error: "), "{diagnostic}");
    assert!(error_line.ends_with(r#""two" "three""#), "{diagnostic}");
    assert!(!error_line.contains("one"), "{diagnostic}");
    assert!(
        usage_line.contains("winter-workspace ws index"),
        "{diagnostic}"
    );
    assert!(
        help_line.contains("winter-workspace ws index --help"),
        "{diagnostic}"
    );
}

#[test]
fn a_repeated_option_is_refused_as_a_repeated_option_and_nothing_is_looked_up() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config(DEFAULTS)
        .build()
        .expect("a workspace");

    for (args, path, option) in [
        (
            &["-v", "-v", "ws", "index", "feature-x"][..],
            &[][..],
            "--verbose",
        ),
        (
            &["ws", "index", "feature-x", "--json", "--json"],
            &["ws", "index"],
            "--json",
        ),
        (
            &[
                "--service-orchestrator",
                "a",
                "--service-orchestrator",
                "b",
                "ws",
                "index",
                "feature-x",
            ],
            &[],
            "--service-orchestrator",
        ),
    ] {
        let ended = InProcess::new(args).cwd(&workspace.root).run();

        let (command_path, _, fault) = usage_parts(ended, &format!("{args:?}"));
        assert_eq!(command_path, path, "{args:?}");
        assert!(
            matches!(&fault, ParseFault::RepeatedOption { option: named } if named == option),
            "{args:?}: {fault:?}"
        );
    }
}

#[test]
fn a_root_option_after_the_command_name_is_a_usage_error_of_ws_index() {
    for (args, name) in [
        (&["ws", "index", "-v", "feature-x"][..], "-v"),
        (
            &["ws", "index", "feature-x", "--service-orchestrator", "X"],
            "--service-orchestrator",
        ),
    ] {
        let (command_path, usage, fault) = usage_error(args);

        assert_eq!(command_path, ["ws", "index"], "{args:?}");
        assert!(
            matches!(&fault, ParseFault::UnknownOption { name: named } if named == name),
            "{args:?}: {fault:?}"
        );
        assert_usage_of(&usage, &["ws", "index", "--help"]);
    }
}

#[test]
fn the_root_options_before_the_command_name_are_accepted_and_change_no_result() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config(DEFAULTS)
        .state("[env_index]\nfeature-x = 15\n")
        .build()
        .expect("a workspace");
    let plain = document(&workspace.root, "feature-x");

    for options in [
        &["-v"][..],
        &["--verbose"],
        &["--service-orchestrator", "X"],
        &["--service-orchestrator", "-x"],
        &["--service-orchestrator", ""],
        &["--source-override", "T"],
        &["-v", "--service-orchestrator=X", "--source-override=T"],
    ] {
        let args: Vec<&str> = options
            .iter()
            .chain(&["ws", "index", "feature-x", "--json"])
            .copied()
            .collect();

        let ended = InProcess::new(&args)
            .cwd(&workspace.root)
            .env("WINTER_LOG_LEVEL", "DEBUG")
            .env("WINTER_SERVICE_ORCHESTRATOR", "Y")
            .run();

        assert!(
            matches!(ended.termination, Termination::Success),
            "{args:?}: {:?}",
            ended.termination
        );
        assert_eq!(ended.stderr, "", "{args:?}");
        let document: Value = serde_json::from_str(&ended.stdout).expect("a document");
        assert_eq!(document, plain, "{args:?}");
    }
}

#[test]
fn a_name_after_a_double_dash_is_looked_up_though_it_is_spelled_like_an_option() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config(DEFAULTS)
        .state("[env_index]\n--json = 7\n")
        .build()
        .expect("a workspace");

    let ended = InProcess::new(&["ws", "index", "--json", "--", "--json"])
        .cwd(&workspace.root)
        .run();

    assert!(
        matches!(ended.termination, Termination::Success),
        "{:?}",
        ended.termination
    );
    let document: Value = serde_json::from_str(&ended.stdout).expect("a document");
    assert_eq!(
        document,
        json!({"name": "--json", "index": 7, "source": "registry"})
    );
}

#[test]
fn help_before_a_fault_of_ws_index_is_written_and_succeeds() {
    let help = InProcess::new(&["ws", "index", "--help"])
        .cwd(Path::new("/"))
        .run();
    assert!(
        matches!(help.termination, Termination::Success),
        "{:?}",
        help.termination
    );
    assert!(help.stdout.contains("NAME"), "{}", help.stdout);
    assert_eq!(help.stderr, "");

    for args in [
        &["ws", "index", "--help", "--bogus"][..],
        &["ws", "index", "--help", "one", "two"],
        &["ws", "index", "--json", "--help", "--json"],
    ] {
        let ended = InProcess::new(args).cwd(Path::new("/")).run();

        assert!(
            matches!(ended.termination, Termination::Success),
            "{args:?}: {:?}",
            ended.termination
        );
        assert_eq!(ended.stdout, help.stdout, "{args:?}");
        assert_eq!(ended.stderr, "", "{args:?}");
    }
}

#[test]
fn a_fault_before_the_help_of_ws_index_is_a_usage_error() {
    let (command_path, usage, fault) = usage_error(&["ws", "index", "--bogus", "--help"]);

    assert_eq!(command_path, ["ws", "index"]);
    assert!(
        matches!(&fault, ParseFault::UnknownOption { name } if name == "--bogus"),
        "{fault:?}"
    );
    assert_usage_of(&usage, &["ws", "index", "--help"]);
}

#[test]
fn an_argument_of_ws_index_that_is_not_utf_8_is_refused_and_nothing_is_looked_up() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config(DEFAULTS)
        .build()
        .expect("a workspace");
    let name = || OsString::from_vec(b"feat\xffure".to_vec());

    for args in [
        vec!["ws".into(), "index".into(), name()],
        vec!["ws".into(), "index".into(), "--".into(), name()],
        vec!["ws".into(), "index".into(), name(), "--json".into()],
        vec!["ws".into(), "index".into(), "--help".into(), name()],
        vec!["-v".into(), "ws".into(), "index".into(), name()],
    ] {
        let line = format!("{args:?}");

        let ended = InProcess::os_args(args).cwd(&workspace.root).run();

        let (command_path, usage, fault) = usage_parts(ended, &line);
        assert_eq!(command_path, ["ws", "index"], "{line}");
        assert!(
            matches!(&fault, ParseFault::NotUtf8 { argument } if argument == "feat\u{fffd}ure"),
            "{line}: {fault:?}"
        );
        assert_usage_of(&usage, &["ws", "index", "--help"]);
    }
}

#[test]
fn a_repeated_root_option_is_refused_though_a_help_or_a_fault_follows_it() {
    for option in ["--service-orchestrator", "--source-override"] {
        for tail in [
            &["ws", "index", "--help"][..],
            &["ws", "--help"],
            &["ws", "index", "--bogus"],
            &["ws", "nope"],
            &["--bogus"],
            &["nope"],
        ] {
            let args: Vec<&str> = [option, "a", option, "b"]
                .into_iter()
                .chain(tail.iter().copied())
                .collect();

            let (command_path, usage, fault) = usage_error(&args);

            assert!(command_path.is_empty(), "{args:?}: {command_path:?}");
            assert_usage_of(&usage, &["--help"]);
            assert!(
                matches!(&fault, ParseFault::RepeatedOption { option: named } if named == option),
                "{args:?}: {fault:?}"
            );
        }
    }
}

#[test]
fn a_double_dash_before_a_command_name_is_a_usage_error_and_nothing_is_looked_up() {
    let tree = tree();
    let workspace = WorkspaceBuilder::new(&tree)
        .config(DEFAULTS)
        .state("[env_index]\nfeature-x = 15\n")
        .build()
        .expect("a workspace");

    for (args, path, help_args) in [
        (
            &["--", "ws", "index", "feature-x"][..],
            &[][..],
            &["--help"][..],
        ),
        (
            &["ws", "--", "index", "feature-x"],
            &["ws"],
            &["ws", "--help"],
        ),
    ] {
        let ended = InProcess::new(args).cwd(&workspace.root).run();

        let (command_path, usage, fault) = usage_parts(ended, &format!("{args:?}"));
        assert_eq!(command_path, path, "{args:?}");
        assert_usage_of(&usage, help_args);
        assert!(
            matches!(&fault, ParseFault::UnexpectedArgument { values } if values == &["--"]),
            "{args:?}: {fault:?}"
        );
    }
}
