//! What a pin flow reports for one standalone, and how a repo error reads as the text of a pin reason.

use crate::termination::RepoError;

/// The result of one standalone in a pin run: the `result` of a `repo_synced` event.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PinResult {
    /// `up_to_date`
    UpToDate,
    /// `re_pinned`: `pin_ref` is the new commit, its first 8 characters.
    RePinned,
    /// `held_pin`: `pin_ref` is the declared ref.
    HeldPin,
    /// `pinned`: `pin_ref` is the frozen commit, its first 8 characters.
    Pinned,
    /// `already_pinned`: `pin_ref` is the declared ref.
    AlreadyPinned,
    /// `refused`: `pin_ref` is the reason.
    Refused,
    /// `pin_error`: `pin_ref` is the reason.
    PinError,
}

/// One standalone's outcome: its name, what happened, and the text the result carries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PinOutcome {
    pub repo: String,
    pub result: PinResult,
    pub pin_ref: String,
}

/// `error` as Python's `str()` of the reference's repo error renders it: the message, then each detail that
/// is present on a line of its own indented by two spaces: `$ <program> <subcommand> <args>`, `cwd: <cwd>`,
/// `exit <n>` (with `(killed by signal <s>)` for a negative exit), and `stderr: <stderr, trimmed>`. The lines
/// are joined by `\n`, with none after the last.
pub fn rendered(error: &RepoError) -> String {
    let mut lines = vec![error.message.to_string()];
    if let Some(program) = &error.program {
        let command: Vec<&str> = std::iter::once(program)
            .chain(&error.subcommand)
            .chain(&error.args)
            .map(String::as_str)
            .collect();
        lines.push(format!("  $ {}", command.join(" ")));
    }
    if let Some(cwd) = &error.cwd {
        lines.push(format!("  cwd: {}", cwd.display()));
    }
    match error.exit {
        Some(exit) if exit < 0 => lines.push(format!(
            "  exit {exit} (killed by signal {})",
            exit.unsigned_abs()
        )),
        Some(exit) => lines.push(format!("  exit {exit}")),
        None => {}
    }
    if let Some(stderr) = &error.stderr {
        lines.push(format!("  stderr: {}", stderr.trim()));
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::*;
    use crate::termination::{GitOperation, RepoMessage};

    fn error() -> RepoError {
        RepoError {
            message: RepoMessage::GitFailure {
                operation: GitOperation::Checkout,
                repo: "api".to_owned(),
            },
            program: Some("git".to_owned()),
            subcommand: Some("checkout".to_owned()),
            args: vec!["--detach".to_owned(), "abc".to_owned()],
            cwd: Some(PathBuf::from("/w/api")),
            exit: Some(1),
            stderr: Some("  boom\n".to_owned()),
        }
    }

    #[test]
    fn every_field_present_gives_a_line_each() {
        assert_eq!(
            rendered(&error()),
            "checkout failed for api\n  $ git checkout --detach abc\n  cwd: /w/api\n  exit 1\n  stderr: boom"
        );
    }

    #[test]
    fn a_negative_exit_names_the_signal() {
        let error = RepoError {
            exit: Some(-9),
            ..error()
        };

        assert!(rendered(&error).contains("\n  exit -9 (killed by signal 9)\n"));
    }

    #[test]
    fn absent_fields_give_no_line_and_no_trailing_newline() {
        let error = RepoError {
            program: None,
            subcommand: None,
            args: Vec::new(),
            cwd: None,
            exit: None,
            stderr: None,
            ..error()
        };

        assert_eq!(rendered(&error), error.message.to_string());
    }

    #[test]
    fn a_blank_stderr_trims_to_an_empty_line_value() {
        let error = RepoError {
            stderr: Some("\n".to_owned()),
            ..error()
        };

        assert!(rendered(&error).ends_with("\n  stderr: "));
    }
}
