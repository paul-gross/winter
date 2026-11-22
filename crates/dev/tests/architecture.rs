//! The architecture rules, applied to this checkout.

use std::path::{Path, PathBuf};
use std::process::Command;

use winter_workspace_dev::architecture::{self, Violation};

/// The directory whose files are the library's sources, from the repository root.
const LIBRARY_SOURCES: &str = "crates/core/src";

fn repository_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap()
}

fn read(file: &str) -> String {
    let path = repository_root().join(file);
    std::fs::read_to_string(&path).unwrap_or_else(|error| panic!("{}: {error}", path.display()))
}

/// Every file below `directory`, sorted.
fn files_below(directory: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    let mut pending = vec![directory.to_path_buf()];
    while let Some(directory) = pending.pop() {
        for entry in std::fs::read_dir(&directory).unwrap() {
            let entry = entry.unwrap();
            if entry.file_type().unwrap().is_dir() {
                pending.push(entry.path());
            } else {
                files.push(entry.path());
            }
        }
    }
    files.sort();
    files
}

fn assert_none(rule: &str, violations: &[Violation]) {
    let lines: Vec<String> = violations
        .iter()
        .map(|violation| format!("  {violation}"))
        .collect();
    assert!(violations.is_empty(), "{rule}:\n{}", lines.join("\n"));
}

#[test]
fn the_package_graph_is_the_one_the_layout_lists() {
    let cargo = std::env::var_os("CARGO").expect("cargo sets CARGO for the tests it runs");
    let output = Command::new(cargo)
        .args([
            "metadata",
            "--no-deps",
            "--locked",
            "--offline",
            "--format-version",
            "1",
        ])
        .current_dir(repository_root())
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "cargo metadata failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let metadata =
        cargo_metadata::MetadataCommand::parse(String::from_utf8(output.stdout).unwrap()).unwrap();

    let packages = architecture::workspace_packages(&metadata);

    let mut violations = architecture::member_set(&packages);
    violations.extend(architecture::edges(&packages));
    violations.extend(architecture::unpublished(&packages));
    assert_none("package graph", &violations);
}

#[test]
fn the_library_sources_name_nothing_forbidden_and_hold_no_mod_file() {
    let root = repository_root();
    let files = files_below(&root.join(LIBRARY_SOURCES));
    assert!(!files.is_empty(), "{LIBRARY_SOURCES} holds no file");

    let mut violations = Vec::new();
    for path in files {
        let file = path.strip_prefix(&root).unwrap().to_str().unwrap();
        let source = String::from_utf8_lossy(&std::fs::read(&path).unwrap()).into_owned();
        violations.extend(architecture::module_file(file));
        violations.extend(architecture::forbidden_tokens(file, &source));
        violations.extend(architecture::command_file_names(file, &source));
    }

    assert_none("library rules", &violations);
}

/// The directories whose files may not name the reference tool, from the repository root.
const KEPT_FROM_THE_REFERENCE: [&str; 3] = ["crates/cli", "crates/core", "crates/test-support"];

#[test]
fn no_file_of_the_product_or_the_support_package_names_the_reference_tool() {
    let root = repository_root();

    let mut violations = Vec::new();
    for directory in KEPT_FROM_THE_REFERENCE {
        let files = files_below(&root.join(directory));
        assert!(!files.is_empty(), "{directory} holds no file");
        for path in files {
            let file = path.strip_prefix(&root).unwrap().to_str().unwrap();
            let text = String::from_utf8_lossy(&std::fs::read(&path).unwrap()).into_owned();
            violations.extend(architecture::reference_names(file, &text));
        }
    }

    assert_none("reference names", &violations);
}

#[test]
fn the_workflow_runs_the_bootstrap_and_the_full_gate() {
    let workflow = read(".github/workflows/check.yml");

    assert_none("workflow", &architecture::workflow(&workflow));
}

#[test]
fn the_cargo_configuration_defines_the_two_aliases() {
    let names = architecture::alias_names(&read(".cargo/config.toml")).unwrap();

    assert_none("aliases", &architecture::aliases(&names));
}

/// The document of the context tree whose fenced blocks hold commands.
const DEVELOPMENT: &str = "context/tooling/development.md";

/// The verifiability matrix.
const MATRIX: &str = "context/verification/matrix.md";

/// Every file of the checkout as its path from the repository root, without the build directory and the
/// repository's own git directory.
fn checkout_files() -> Vec<String> {
    let root = repository_root();
    let mut files = Vec::new();
    for entry in std::fs::read_dir(&root).unwrap() {
        let entry = entry.unwrap();
        let name = entry.file_name();
        if name == "target" || name == ".git" {
            continue;
        }
        if entry.file_type().unwrap().is_dir() {
            files.extend(files_below(&entry.path()));
        } else {
            files.push(entry.path());
        }
    }
    let mut files: Vec<String> = files
        .iter()
        .map(|path| {
            path.strip_prefix(&root)
                .unwrap()
                .to_str()
                .unwrap()
                .to_owned()
        })
        .collect();
    files.sort();
    files
}

/// The entry point and the documents of the context tree, each with its text.
fn context_documents() -> Vec<(String, String)> {
    checkout_files()
        .into_iter()
        .filter(|file| architecture::is_context_document(file))
        .map(|file| {
            let text = read(&file);
            (file, text)
        })
        .collect()
}

#[test]
fn the_context_documents_are_reachable_from_the_entry_point_and_their_links_resolve() {
    let files = checkout_files().into_iter().collect();
    let documents = context_documents();
    assert!(documents.len() > 1, "the checkout holds no context tree");

    let mut violations = architecture::reachable(&documents);
    for (file, text) in &documents {
        violations.extend(architecture::links_resolve(file, text, &files));
    }

    assert_none("context tree", &violations);
}

#[test]
fn the_contributor_documents_stay_in_scope() {
    let mut violations = architecture::scope_tokens("README.md", &read("README.md"));
    for (file, text) in context_documents() {
        violations.extend(architecture::scope_tokens(&file, &text));
    }

    assert_none("scope", &violations);
}

#[test]
fn the_documented_commands_are_gate_commands_or_tests_of_a_workspace_package() {
    let development = architecture::fenced_lines(&read(DEVELOPMENT));
    let matrix = architecture::matrix_commands(&read(MATRIX));
    assert!(!development.is_empty(), "{DEVELOPMENT} holds no command");
    assert!(!matrix.is_empty(), "{MATRIX} holds no command");

    let mut violations = architecture::documented_commands(DEVELOPMENT, &development);
    violations.extend(architecture::documented_commands(MATRIX, &matrix));

    assert_none("documented commands", &violations);
}

#[test]
fn the_matrix_declares_its_command_methods_and_its_tools() {
    let matrix = read(MATRIX);

    assert_eq!(
        architecture::matrix_command_ids(&matrix),
        [
            "workspace:unit-test",
            "workspace:component-test",
            "workspace:executable-test",
            "workspace:architecture",
            "workspace:slice-gate",
            "workspace:full-gate",
            "workspace:scenario",
        ]
    );
    assert_eq!(
        architecture::matrix_tool_ids(&matrix),
        ["tool:format", "tool:keep-tree", "tool:record"]
    );
}

#[test]
fn the_matrix_declares_its_manual_methods_each_with_its_setup_its_success_and_where_it_runs() {
    let methods = architecture::matrix_manual_methods(&read(MATRIX));

    let ids: Vec<&str> = methods.iter().map(|(id, _)| id.as_str()).collect();
    assert_eq!(
        ids,
        [
            "workspace:manual-gates",
            "workspace:manual-registration",
            "workspace:manual-interrupt",
            "workspace:manual-repo-add",
            "workspace:manual-repo-remove",
            "workspace:manual-ws-fingerprint",
            "workspace:manual-ws-update"
        ]
    );
    for (id, text) in &methods {
        let text = text.split_whitespace().collect::<Vec<_>>().join(" ");
        for stated in ["**Setup.**", "**Success.**"] {
            assert!(text.contains(stated), "{id} does not state {stated}");
        }
        let location: &[&str] =
            if id.starts_with("workspace:manual-repo-") || id.starts_with("workspace:manual-ws-") {
                &[
                    "fresh temporary directory outside any real workspace",
                    "verify that no ancestor holds `.winter/config.toml`",
                    "Work only in a new `workspace/` beneath it",
                    "with a `.winter/config.toml` made for this run",
                ]
            } else {
                &["scratch copy of the checkout"]
            };
        for stated in location {
            assert!(text.contains(stated), "{id} does not state {stated}");
        }
    }
}
