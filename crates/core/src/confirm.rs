//! The confirmation prompt: the one owner of a question the user answers on stdin.

use std::io::{BufRead, Write};

use crate::termination::Termination;

/// What follows the question: the answers, with the default, no, in upper case.
const DEFAULT_NO: &str = " [y/N]: ";

/// Asks `question` on `out` and reads the answer from `input`. `Ok(())` is a yes; an `Err` is how the
/// invocation ends, which the command returns before it changes anything.
///
/// The question is written without a newline and flushed. `y` and `yes` confirm, in any case and with
/// surrounding blanks ignored. `n`, `no`, an empty line, the end of input, and a read error abort. Any other
/// line asks again, without bound. Whether `input` is a terminal is not looked at.
pub(crate) fn confirm(
    out: &mut dyn Write,
    input: &mut dyn BufRead,
    question: &str,
) -> Result<(), Termination> {
    let prompt = format!("{question}{DEFAULT_NO}");
    loop {
        out.write_all(prompt.as_bytes())
            .and_then(|()| out.flush())
            .map_err(Termination::from_write_error)?;
        let mut line = Vec::new();
        match input.read_until(b'\n', &mut line) {
            Ok(read) if read > 0 => {}
            _ => return Err(Termination::Aborted { interrupted: false }),
        }
        let answer = String::from_utf8_lossy(&line).trim().to_lowercase();
        match answer.as_str() {
            "y" | "yes" => return Ok(()),
            "n" | "no" | "" => return Err(Termination::Aborted { interrupted: false }),
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use std::io::{self, BufReader, Read};

    use super::*;

    /// The answer to `input`, and what was written.
    fn asked(input: &[u8]) -> (Result<(), Termination>, String) {
        let mut out = Vec::new();
        let mut input = input;
        let answer = confirm(&mut out, &mut input, "Remove 2 orphan(s)?");
        (answer, String::from_utf8(out).expect("UTF-8"))
    }

    fn assert_aborted(answer: &Result<(), Termination>, subject: &str) {
        assert!(
            matches!(answer, Err(Termination::Aborted { interrupted: false })),
            "{subject}: {answer:?}"
        );
    }

    #[test]
    fn y_and_yes_confirm_in_any_case_and_with_blanks_around() {
        for input in ["y\n", "Y\n", "yes\n", "YES\n", "  yEs \t\n", "y", " Y \r\n"] {
            let (answer, written) = asked(input.as_bytes());

            assert!(answer.is_ok(), "{input:?}: {answer:?}");
            assert_eq!(written, "Remove 2 orphan(s)? [y/N]: ", "{input:?}");
        }
    }

    #[test]
    fn n_no_an_empty_line_and_the_end_of_input_abort() {
        for input in ["n\n", "N\n", "no\n", " No \n", "\n", "   \n", ""] {
            let (answer, written) = asked(input.as_bytes());

            assert_aborted(&answer, &format!("{input:?}"));
            assert_eq!(written, "Remove 2 orphan(s)? [y/N]: ", "{input:?}");
        }
    }

    struct FailingRead;

    impl Read for FailingRead {
        fn read(&mut self, _: &mut [u8]) -> io::Result<usize> {
            Err(io::Error::other("the terminal is gone"))
        }
    }

    #[test]
    fn a_read_error_aborts() {
        let mut out = Vec::new();

        let answer = confirm(&mut out, &mut BufReader::new(FailingRead), "Remove?");

        assert_aborted(&answer, "a read error");
    }

    #[test]
    fn a_line_that_is_no_answer_asks_again_until_one_is() {
        let (confirmed, written) = asked(b"maybe\nyess\n\xff\ny\nn\n");

        assert!(confirmed.is_ok(), "{confirmed:?}");
        assert_eq!(written, "Remove 2 orphan(s)? [y/N]: ".repeat(4));

        let (ended, written) = asked(b"what\nsure");

        assert_aborted(&ended, "the input ended after no answer");
        assert_eq!(written, "Remove 2 orphan(s)? [y/N]: ".repeat(3));
    }

    /// A writer that fails its write or its flush with `kind`.
    struct Refusing {
        kind: io::ErrorKind,
        on_flush: bool,
    }

    impl Write for Refusing {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            if self.on_flush {
                Ok(bytes.len())
            } else {
                Err(io::Error::from(self.kind))
            }
        }

        fn flush(&mut self) -> io::Result<()> {
            Err(io::Error::from(self.kind))
        }
    }

    #[test]
    fn a_question_that_cannot_be_written_ends_as_the_failed_write_and_reads_no_answer() {
        for on_flush in [false, true] {
            let mut input: &[u8] = b"y\n";
            let mut closed = Refusing {
                kind: io::ErrorKind::BrokenPipe,
                on_flush,
            };
            let answer = confirm(&mut closed, &mut input, "Remove?");
            assert!(matches!(answer, Err(Termination::BrokenPipe)), "{answer:?}");
            assert_eq!(input, b"y\n", "the answer must stay unread");

            let mut full = Refusing {
                kind: io::ErrorKind::StorageFull,
                on_flush,
            };
            let answer = confirm(&mut full, &mut input, "Remove?");
            assert!(
                matches!(answer, Err(Termination::Internal(_))),
                "{answer:?}"
            );
        }
    }
}
