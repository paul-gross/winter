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
    }

    assert_none("library rules", &violations);
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
