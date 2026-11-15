//! The recorded-scenario target: replays every scenario under the scenario root with the binary Cargo built,
//! and exits 0 when none failed and 1 otherwise.
//!
//! An argument is a filter, and a scenario is replayed when its id contains one of them. `--list` prints
//! the id and the state of every scenario the filters choose and replays none. `WINTER_SCENARIO_JOBS` is
//! how many scenarios run at once: the available parallelism, at most 8, when it is unset.

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use winter_workspace_test_support::scenario::runner::{ReplayOptions, list, replay_all};

const SCENARIO_ROOT: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../scenarios");

/// The binary Cargo built for this target.
const BINARY: &str = env!("CARGO_BIN_EXE_winter-workspace");

/// The option that lists the scenarios.
const LIST: &str = "--list";

/// Names how many scenarios run at once.
const JOBS_VARIABLE: &str = "WINTER_SCENARIO_JOBS";

/// The most scenarios that run at once when the variable does not say.
const DEFAULT_JOBS: usize = 8;

fn main() -> ExitCode {
    let (options, filters): (Vec<String>, Vec<String>) = std::env::args()
        .skip(1)
        .partition(|argument| argument.starts_with('-'));
    if let Some(unknown) = options.iter().find(|option| *option != LIST) {
        eprintln!("scenarios: unknown option {unknown}; usage: [FILTER]... [{LIST}]");
        return ExitCode::FAILURE;
    }
    let jobs = match jobs(std::env::var_os(JOBS_VARIABLE)) {
        Ok(jobs) => jobs,
        Err(value) => {
            eprintln!("scenarios: {JOBS_VARIABLE}={value} is no count of workers");
            return ExitCode::FAILURE;
        }
    };
    let mut out = std::io::stdout().lock();
    if !options.is_empty() {
        return match list(Path::new(SCENARIO_ROOT), &filters, &mut out) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => {
                eprintln!("scenarios: {SCENARIO_ROOT}: {error}");
                ExitCode::FAILURE
            }
        };
    }
    let options = ReplayOptions {
        executable: PathBuf::from(BINARY),
        scenarios_root: PathBuf::from(SCENARIO_ROOT),
        filters,
        jobs,
    };
    match replay_all(&options, &mut out).failed.is_empty() {
        true => ExitCode::SUCCESS,
        false => ExitCode::FAILURE,
    }
}

/// How many scenarios run at once under `value`, the value of the variable: a count of at least one, or
/// the default when the variable is unset. A value that is no such count is answered as the error.
fn jobs(value: Option<std::ffi::OsString>) -> Result<usize, String> {
    let Some(value) = value else {
        let available = std::thread::available_parallelism().map_or(1, usize::from);
        return Ok(available.min(DEFAULT_JOBS));
    };
    let value = value.to_string_lossy();
    match value.parse() {
        Ok(jobs) if jobs > 0 => Ok(jobs),
        _ => Err(value.into_owned()),
    }
}
