//! The development driver of the repository. Its one entry is [`main_with`]; the binary target only calls it.

use std::ffi::OsString;
use std::path::Path;

/// Runs the driver command that `args` names and returns the process exit code.
///
/// `env` looks up one variable of the driver's environment and `root` is the repository root. Arguments that
/// name no command the driver has are unknown and answer 2; the driver has no command, so every argument vector
/// does.
pub fn main_with(_args: &[OsString], _env: &dyn Fn(&str) -> Option<OsString>, _root: &Path) -> u8 {
    2
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_argument_vector_is_unknown() {
        let env = |_: &str| None;
        let root = Path::new("/nowhere");

        assert_eq!(main_with(&[], &env, root), 2);
        assert_eq!(
            main_with(
                &[OsString::from("gate"), OsString::from("slice")],
                &env,
                root
            ),
            2
        );
    }
}
