//! The development driver of the repository. Its one entry is [`main_with`]; the binary target only calls it.

pub mod architecture;
mod fmt;
mod gate;
mod step;

use std::ffi::OsString;
use std::io::Write;
use std::path::{Path, PathBuf};

pub use fmt::format_files;
pub use gate::{Gate, plan};
pub use step::{STEP_DEADLINE, Step, run_steps};

const USAGE: &str = "usage: cargo dev gate slice | cargo dev gate full | cargo dev fmt";

/// Runs the driver command that `args` names and returns the process exit code.
///
/// `env` looks up one variable of the driver's environment and `root` is the repository root. The commands
/// are `gate slice`, `gate full`, and `fmt`; each answers 0 when every step it ran passed and 1 otherwise.
/// Arguments that name no command the driver has are unknown and answer 2.
pub fn main_with(args: &[OsString], env: &dyn Fn(&str) -> Option<OsString>, root: &Path) -> u8 {
    main_to(args, env, root, &mut std::io::stderr())
}

/// [`main_with`], writing what the driver itself reports to `err`.
fn main_to(
    args: &[OsString],
    env: &dyn Fn(&str) -> Option<OsString>,
    root: &Path,
    err: &mut dyn Write,
) -> u8 {
    let words: Vec<Option<&str>> = args.iter().map(|arg| arg.to_str()).collect();
    let command = match words.as_slice() {
        [Some("gate"), Some("slice")] => Command::Gate(Gate::Slice),
        [Some("gate"), Some("full")] => Command::Gate(Gate::Full),
        [Some("fmt")] => Command::Format,
        _ => {
            let _ = writeln!(err, "{USAGE}");
            return 2;
        }
    };

    let Some(cargo) = env("CARGO").map(PathBuf::from) else {
        let _ = writeln!(
            err,
            "the CARGO environment variable is not set; run the driver through `cargo dev`"
        );
        return 1;
    };

    step::watch_interrupts();
    match command {
        Command::Gate(gate) => {
            run_steps(gate.name(), &plan(gate, &cargo, root), STEP_DEADLINE, err)
        }
        Command::Format => {
            let format = fmt::step(&cargo, root, false);
            match step::run_step(&format, STEP_DEADLINE, err) {
                step::Outcome::Passed => 0,
                step::Outcome::Failed | step::Outcome::Interrupted => 1,
            }
        }
    }
}

enum Command {
    Gate(Gate),
    Format,
}

/// Scratch directories for the driver's unit tests.
#[cfg(test)]
mod scratch {
    use super::*;

    /// A directory of its own under the system's temporary directory, removed when dropped.
    pub(crate) struct Scratch(PathBuf);

    impl Scratch {
        /// `label` tells the directories of one test run apart; each test passes its own.
        pub(crate) fn new(label: &str) -> Self {
            let name = format!("winter-workspace-dev-{}-{label}", std::process::id());
            let path = std::env::temp_dir().join(name);
            std::fs::create_dir_all(&path).unwrap();
            Self(path)
        }

        pub(crate) fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(words: &[&str]) -> Vec<OsString> {
        words.iter().map(OsString::from).collect()
    }

    fn no_environment(_: &str) -> Option<OsString> {
        None
    }

    #[test]
    fn arguments_that_name_no_command_are_unknown() {
        let root = Path::new("/nowhere");

        for unknown in [
            &[][..],
            &["gate"],
            &["gate", "nope"],
            &["gate", "slice", "more"],
            &["fmt", "--check"],
            &["record"],
        ] {
            let mut err = Vec::new();

            let code = main_to(&args(unknown), &no_environment, root, &mut err);

            assert_eq!(code, 2, "{unknown:?}");
            assert_eq!(String::from_utf8(err).unwrap(), format!("{USAGE}\n"));
        }
    }

    #[test]
    fn a_command_without_the_cargo_variable_fails_and_names_it() {
        let root = Path::new("/nowhere");

        for command in [&["gate", "slice"][..], &["gate", "full"], &["fmt"]] {
            let mut err = Vec::new();

            let code = main_to(&args(command), &no_environment, root, &mut err);

            assert_eq!(code, 1, "{command:?}");
            assert!(String::from_utf8(err).unwrap().contains("CARGO"));
        }
    }

    #[test]
    fn main_with_reads_cargo_through_the_lookup_it_is_given() {
        let root = Path::new("/nowhere");

        assert_eq!(
            main_with(&args(&["gate", "slice"]), &no_environment, root),
            1
        );
        assert_eq!(
            main_with(&args(&["gate", "nope"]), &no_environment, root),
            2
        );
    }
}
