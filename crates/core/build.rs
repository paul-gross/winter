//! Discovers the command files under `src/commands/` and writes the registry that `src/registry.rs` includes.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

#[path = "build/discover.rs"]
mod discover;

fn main() -> ExitCode {
    let manifest_dir = PathBuf::from(env("CARGO_MANIFEST_DIR"));
    let commands_dir = manifest_dir.join("src/commands");

    // The whole source directory is watched so that a commands directory that appears later is seen.
    println!("cargo:rerun-if-changed=src");
    println!("cargo:rerun-if-changed=build/discover.rs");
    if commands_dir.is_dir() {
        println!("cargo:rerun-if-changed=src/commands");
    }
    for group in discover::GROUPS {
        if commands_dir.join(group).is_dir() {
            println!("cargo:rerun-if-changed=src/commands/{group}");
        }
    }

    let mut relative_paths = Vec::new();
    walk(&commands_dir, "", &mut relative_paths);
    relative_paths.sort();

    let contents = |relative: &str| {
        let bytes = fs::read(commands_dir.join(relative)).unwrap_or_default();
        String::from_utf8_lossy(&bytes).into_owned()
    };
    let files = match discover::classify(&relative_paths, &contents) {
        Ok(files) => files,
        Err(error) => {
            eprintln!("{error}");
            return ExitCode::FAILURE;
        }
    };

    let registry = discover::generate(&files, &commands_dir.to_string_lossy());
    let out = Path::new(&env("OUT_DIR")).join("registry.rs");
    fs::write(&out, registry)
        .unwrap_or_else(|error| panic!("cannot write {}: {error}", out.display()));
    ExitCode::SUCCESS
}

fn env(key: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| panic!("cargo sets {key} for a build script"))
}

/// Lists everything below `directory`, depth first: a file as its `/`-separated path relative to the commands
/// directory, a directory as that path with a trailing `/`. An absent directory lists nothing.
fn walk(directory: &Path, prefix: &str, found: &mut Vec<String>) {
    let Ok(entries) = fs::read_dir(directory) else {
        return;
    };
    for entry in entries.flatten() {
        let relative = format!("{prefix}{}", entry.file_name().to_string_lossy());
        let path = entry.path();
        if path.is_dir() {
            found.push(format!("{relative}/"));
            walk(&path, &format!("{relative}/"), found);
        } else {
            found.push(relative);
        }
    }
}
