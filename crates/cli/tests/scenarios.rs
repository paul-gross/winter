//! The recorded-scenario target. It replays nothing yet: with no scenario directory under the scenario root it
//! reports zero scenarios, and a scenario directory, which it cannot replay, fails it.

use std::path::{Path, PathBuf};
use std::process::ExitCode;

const SCENARIO_ROOT: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../scenarios");

fn main() -> ExitCode {
    match first_scenario(Path::new(SCENARIO_ROOT)) {
        None => {
            println!("scenarios: 0 passed, 0 failed, 0 deferred");
            ExitCode::SUCCESS
        }
        Some(scenario) => {
            eprintln!("scenarios: cannot replay scenarios/{}", scenario.display());
            ExitCode::FAILURE
        }
    }
}

/// The first scenario directory under `root`, relative to it: the first directory of the first command
/// directory, or that command directory itself when it holds none. `None` when `root` is absent or holds no
/// directory.
fn first_scenario(root: &Path) -> Option<PathBuf> {
    let command = first_directory(root)?;
    Some(match first_directory(&root.join(&command)) {
        Some(name) => command.join(name),
        None => command,
    })
}

/// The name of the first directory in `parent`, by name order.
fn first_directory(parent: &Path) -> Option<PathBuf> {
    let mut names: Vec<PathBuf> = std::fs::read_dir(parent)
        .ok()?
        .filter_map(Result::ok)
        .filter(|entry| entry.file_type().is_ok_and(|kind| kind.is_dir()))
        .map(|entry| PathBuf::from(entry.file_name()))
        .collect();
    names.sort();
    names.into_iter().next()
}
