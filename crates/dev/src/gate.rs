//! The two verification gates: which steps each one runs.

use std::ffi::OsString;
use std::path::Path;

use crate::fmt;
use crate::step::Step;

/// A verification gate.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Gate {
    /// Formatting, lints, and the workspace's tests.
    Slice,
    /// The slice gate and the recorded scenarios.
    Full,
}

impl Gate {
    /// The name the driver's arguments and the summary line spell the gate with.
    pub fn name(self) -> &'static str {
        match self {
            Self::Slice => "slice",
            Self::Full => "full",
        }
    }
}

/// The steps of `gate`, in the order they run. Every step's program is `cargo`. Pure but for listing the
/// commands directory under `repository_root`, which the format step names file by file.
pub fn plan(gate: Gate, cargo: &Path, repository_root: &Path) -> Vec<Step> {
    let cargo_step = |name, args: &[&str]| Step {
        name,
        program: cargo.to_path_buf(),
        args: args.iter().map(OsString::from).collect(),
    };

    let mut steps = vec![
        fmt::step(cargo, repository_root, true),
        cargo_step(
            "clippy",
            &[
                "clippy",
                "--locked",
                "--offline",
                "--workspace",
                "--all-targets",
                "--all-features",
                "--",
                "-D",
                "warnings",
            ],
        ),
        cargo_step("test", &["test", "--locked", "--offline", "--workspace"]),
    ];
    if gate == Gate::Full {
        steps.push(cargo_step(
            "scenarios",
            &[
                "test",
                "--locked",
                "--offline",
                "-p",
                "winter-workspace",
                "--features",
                "scenarios",
                "--test",
                "scenarios",
            ],
        ));
    }
    steps
}

#[cfg(test)]
mod tests {
    use super::*;

    const CARGO: &str = "/toolchain/bin/cargo";
    const ROOT: &str = "/repository";

    fn step(name: &'static str, args: &[&str]) -> Step {
        Step {
            name,
            program: CARGO.into(),
            args: args.iter().map(OsString::from).collect(),
        }
    }

    fn slice_steps() -> Vec<Step> {
        vec![
            step(
                "fmt",
                &[
                    "fmt",
                    "--all",
                    "--check",
                    "--",
                    "/repository/crates/core/build/discover.rs",
                    "/repository/crates/core/build/topics.rs",
                ],
            ),
            step(
                "clippy",
                &[
                    "clippy",
                    "--locked",
                    "--offline",
                    "--workspace",
                    "--all-targets",
                    "--all-features",
                    "--",
                    "-D",
                    "warnings",
                ],
            ),
            step("test", &["test", "--locked", "--offline", "--workspace"]),
        ]
    }

    #[test]
    fn the_slice_gate_formats_lints_and_tests() {
        assert_eq!(
            plan(Gate::Slice, Path::new(CARGO), Path::new(ROOT)),
            slice_steps()
        );
    }

    #[test]
    fn the_full_gate_adds_the_scenarios() {
        let mut expected = slice_steps();
        expected.push(step(
            "scenarios",
            &[
                "test",
                "--locked",
                "--offline",
                "-p",
                "winter-workspace",
                "--features",
                "scenarios",
                "--test",
                "scenarios",
            ],
        ));

        assert_eq!(
            plan(Gate::Full, Path::new(CARGO), Path::new(ROOT)),
            expected
        );
    }

    #[test]
    fn every_step_runs_the_cargo_it_was_given() {
        let cargo = Path::new("/elsewhere/cargo");

        for gate in [Gate::Slice, Gate::Full] {
            for step in plan(gate, cargo, Path::new(ROOT)) {
                assert_eq!(step.program, cargo, "{}", step.name);
            }
        }
    }
}
