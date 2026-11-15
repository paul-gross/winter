//! Writing a command's result, and how a failed write ends.
//!
//! The one owner of how a result reaches the stdout handle. A command's `run` and the parser's help and
//! version path write through these functions and return what they return; neither inspects a write error.

use std::backtrace::Backtrace;
use std::error::Error;
use std::io::{self, Write};

use crate::termination::{InternalError, Termination};

/// Writes `line` and one newline to `out`, and returns how the invocation ends on that write.
///
/// The line and its newline are handed to `out` whole, in one `write_all`: a line-buffered handle passes a whole
/// line straight on, so a write that fails leaves nothing behind for a later flush to fail on again.
pub(crate) fn line(out: &mut dyn Write, line: &str) -> Termination {
    let mut whole = String::with_capacity(line.len() + 1);
    whole.push_str(line);
    whole.push('\n');
    ended(out.write_all(whole.as_bytes()))
}

/// Writes `text` to `out` as it stands, in one `write_all`, and returns how the invocation ends on that write.
pub(crate) fn text(out: &mut dyn Write, text: &str) -> Termination {
    ended(out.write_all(text.as_bytes()))
}

/// Writes `document` to `out` as one line of JSON, and returns how the invocation ends on that write.
///
/// The line is encoded into a buffer and handed to `out` whole, in one `write_all`, so a reader never sees
/// half a document from this call.
pub(crate) fn document(out: &mut dyn Write, document: &serde_json::Value) -> Termination {
    let mut encoded = Vec::new();
    if let Err(error) = serde_json::to_writer(&mut encoded, document) {
        return internal(error);
    }
    encoded.push(b'\n');
    ended(out.write_all(&encoded))
}

/// How an invocation ends on a write of its result.
///
/// A reader that closed the handle is no failure of the program: that ending has no diagnostic. Every other
/// error is an internal failure.
fn ended(written: io::Result<()>) -> Termination {
    match written {
        Ok(()) => Termination::Success,
        Err(error) if error.kind() == io::ErrorKind::BrokenPipe => Termination::BrokenPipe,
        Err(error) => internal(error),
    }
}

fn internal(error: impl Error + Send + Sync + 'static) -> Termination {
    Termination::Internal(InternalError::Unexpected {
        source: Box::new(error),
        backtrace: Backtrace::capture(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A handle whose every write fails with an error of one kind.
    struct Failing(io::ErrorKind);

    impl Write for Failing {
        fn write(&mut self, _: &[u8]) -> io::Result<usize> {
            Err(io::Error::new(self.0, "the handle refuses the write"))
        }

        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    /// A handle that accepts everything and keeps each write apart.
    #[derive(Default)]
    struct Counting {
        writes: Vec<Vec<u8>>,
    }

    impl Write for Counting {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.writes.push(bytes.to_vec());
            Ok(bytes.len())
        }

        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    fn value() -> serde_json::Value {
        serde_json::json!({"name": "quo\"te\\slash", "index": -3})
    }

    /// The ending of each of the three functions on `out`, by the function's name.
    fn endings(out: &mut dyn Write) -> [(&'static str, Termination); 3] {
        [
            ("line", line(out, "one line")),
            ("text", text(out, "some text\n")),
            ("document", document(out, &value())),
        ]
    }

    #[test]
    fn a_line_is_written_with_one_newline() {
        let mut out = Vec::new();

        let termination = line(&mut out, "40 (suggested)");

        assert!(matches!(termination, Termination::Success));
        assert_eq!(out, b"40 (suggested)\n");
    }

    #[test]
    fn a_text_is_written_unchanged() {
        for given in ["", "no newline", "two\nlines\n", "ends twice\n\n"] {
            let mut out = Vec::new();

            let termination = text(&mut out, given);

            assert!(matches!(termination, Termination::Success), "{given:?}");
            assert_eq!(out, given.as_bytes(), "{given:?}");
        }
    }

    #[test]
    fn a_document_is_one_line_that_parses_back() {
        let mut out = Vec::new();

        let termination = document(&mut out, &value());

        assert!(matches!(termination, Termination::Success));
        let written = String::from_utf8(out).expect("UTF-8");
        assert!(written.ends_with('\n'), "{written:?}");
        assert_eq!(written.lines().count(), 1, "{written:?}");
        let parsed: serde_json::Value = serde_json::from_str(&written).expect("a JSON document");
        assert_eq!(parsed, value());
    }

    #[test]
    fn a_line_reaches_the_handle_in_one_write() {
        let mut out = Counting::default();

        let termination = line(&mut out, "40 (suggested)");

        assert!(matches!(termination, Termination::Success));
        assert_eq!(out.writes, [b"40 (suggested)\n".to_vec()]);
    }

    #[test]
    fn a_text_reaches_the_handle_in_one_write() {
        let mut out = Counting::default();

        let termination = text(&mut out, "two\nlines\n");

        assert!(matches!(termination, Termination::Success));
        assert_eq!(out.writes, [b"two\nlines\n".to_vec()]);
    }

    #[test]
    fn a_document_reaches_the_handle_in_one_write() {
        let mut out = Counting::default();

        let termination = document(&mut out, &value());

        assert!(matches!(termination, Termination::Success));
        let [whole] = out.writes.as_slice() else {
            panic!("expected one write, got {}", out.writes.len());
        };
        assert!(whole.ends_with(b"\n"), "{whole:?}");
        let parsed: serde_json::Value = serde_json::from_slice(whole).expect("a JSON document");
        assert_eq!(parsed, value());
    }

    #[test]
    fn a_handle_the_reader_closed_ends_each_write_as_a_broken_pipe() {
        for (name, termination) in endings(&mut Failing(io::ErrorKind::BrokenPipe)) {
            assert!(
                matches!(termination, Termination::BrokenPipe),
                "{name}: {termination:?}"
            );
        }
    }

    #[test]
    fn any_other_write_error_ends_each_write_as_an_internal_failure_that_carries_it() {
        for kind in [io::ErrorKind::PermissionDenied, io::ErrorKind::Other] {
            for (name, termination) in endings(&mut Failing(kind)) {
                assert!(
                    matches!(
                        termination,
                        Termination::Internal(InternalError::Unexpected { .. })
                    ),
                    "{name}, {kind:?}: {termination:?}"
                );
                let diagnostic = termination.diagnostic().expect("a diagnostic");
                assert!(
                    diagnostic.contains("the handle refuses the write"),
                    "{name}, {kind:?}: {diagnostic}"
                );
            }
        }
    }
}
