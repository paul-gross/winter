//! Writing a command's result.
//!
//! The one owner of how a result reaches the stdout handle. A command's `run` and the parser's help and
//! version path write through these functions and return what they return; neither inspects a write error.
//! A result of more than one piece is written through [`Results`]. A write or a flush that fails ends as
//! [`Termination::from_write_error`] classifies its error.

use std::io::Write;

use crate::termination::{InternalError, Termination};

/// Writes `line` and one newline to `out`, and returns how the invocation ends on that write.
///
/// The line and its newline are handed to `out` whole, in one `write_all`: a line-buffered handle passes a whole
/// line straight on, so a write that fails leaves nothing behind for a later flush to fail on again.
pub(crate) fn line(out: &mut dyn Write, line: &str) -> Termination {
    let mut whole = String::with_capacity(line.len() + 1);
    whole.push_str(line);
    whole.push('\n');
    ending(out.write_all(whole.as_bytes()))
}

/// Writes `text` to `out` as it stands, in one `write_all`, and returns how the invocation ends on that write.
pub(crate) fn text(out: &mut dyn Write, text: &str) -> Termination {
    ending(out.write_all(text.as_bytes()))
}

/// Writes `document` to `out` as one line of JSON, and returns how the invocation ends on that write.
///
/// The line is encoded into a buffer and handed to `out` whole, in one `write_all`, so a reader never sees
/// half a document from this call.
pub(crate) fn document(out: &mut dyn Write, document: &serde_json::Value) -> Termination {
    match json_line(document) {
        Ok(encoded) => ending(out.write_all(&encoded)),
        Err(termination) => termination,
    }
}

/// The writer of a result of more than one piece: several lines, or a stream of events.
///
/// Each call hands its piece to the stdout handle whole, in one `write_all`. The first call that fails is the
/// ending of the invocation: the command returns the `Err` it is handed and writes nothing more to stdout.
/// That `Err` is what [`Termination::from_write_error`] makes of the failed write or flush.
pub(crate) struct Results<'o> {
    out: &'o mut dyn Write,
}

impl<'o> Results<'o> {
    pub(crate) fn new(out: &'o mut dyn Write) -> Self {
        Self { out }
    }

    /// Writes `line` and one newline.
    pub(crate) fn line(&mut self, line: &str) -> Result<(), Termination> {
        let mut whole = String::with_capacity(line.len() + 1);
        whole.push_str(line);
        whole.push('\n');
        self.out
            .write_all(whole.as_bytes())
            .map_err(Termination::from_write_error)
    }

    /// Writes `event` as one line of JSON and then flushes the handle, so a reader sees the event when it
    /// happens and not when a buffer fills.
    pub(crate) fn event(&mut self, event: &serde_json::Value) -> Result<(), Termination> {
        let encoded = json_line(event)?;
        self.out
            .write_all(&encoded)
            .and_then(|()| self.out.flush())
            .map_err(Termination::from_write_error)
    }
}

/// `value` as one line of JSON with its newline, or the internal failure of a value that cannot be encoded.
fn json_line(value: &serde_json::Value) -> Result<Vec<u8>, Termination> {
    let mut encoded = Vec::new();
    serde_json::to_writer(&mut encoded, value)
        .map_err(|error| Termination::Internal(Box::new(InternalError::unexpected(error))))?;
    encoded.push(b'\n');
    Ok(encoded)
}

/// How far apart two columns of a table are.
const GAP: &str = "  ";

/// `rows` as aligned text: one line per row, each ended by a newline, the cells of a column padded to the
/// widest cell of that column counted in characters, two spaces between columns, and no line ending in a
/// space. No rows is an empty text.
pub(crate) fn table(rows: &[Vec<String>]) -> String {
    let columns = rows.iter().map(Vec::len).max().unwrap_or(0);
    let widths: Vec<usize> = (0..columns)
        .map(|column| {
            rows.iter()
                .filter_map(|row| row.get(column))
                .map(|cell| cell.chars().count())
                .max()
                .unwrap_or(0)
        })
        .collect();
    let mut table = String::new();
    for row in rows {
        let mut line = String::new();
        for (cell, width) in row.iter().zip(&widths) {
            if !line.is_empty() {
                line.push_str(GAP);
            }
            line.push_str(cell);
            line.extend(std::iter::repeat_n(
                ' ',
                width.saturating_sub(cell.chars().count()),
            ));
        }
        table.push_str(line.trim_end());
        table.push('\n');
    }
    table
}

/// The ending of a write of a result: success, or what [`Termination::from_write_error`] makes of its error.
fn ending(written: std::io::Result<()>) -> Termination {
    written.map_or_else(Termination::from_write_error, |()| Termination::Success)
}

#[cfg(test)]
mod tests {
    use std::io;

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

    /// A handle that accepts everything and keeps each write apart, with the flushes among them.
    #[derive(Default)]
    struct Counting {
        writes: Vec<Vec<u8>>,
        calls: Vec<&'static str>,
    }

    impl Write for Counting {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            self.writes.push(bytes.to_vec());
            self.calls.push("write");
            Ok(bytes.len())
        }

        fn flush(&mut self) -> io::Result<()> {
            self.calls.push("flush");
            Ok(())
        }
    }

    /// A handle that accepts every write and whose every flush fails with an error of one kind.
    struct FailingFlush(io::ErrorKind);

    impl Write for FailingFlush {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            Ok(bytes.len())
        }

        fn flush(&mut self) -> io::Result<()> {
            Err(io::Error::new(self.0, "the handle refuses the flush"))
        }
    }

    /// The ending a call of `Results` answered, for the assertions on an ending.
    fn ended(answer: Result<(), Termination>) -> Termination {
        answer.err().unwrap_or(Termination::Success)
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
                        &termination,
                        Termination::Internal(error) if matches!(**error, InternalError::Unexpected { .. })
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

    #[test]
    fn a_results_line_reaches_the_handle_in_one_write_and_no_flush() {
        let mut out = Counting::default();
        let mut results = Results::new(&mut out);

        let first = results.line("→ pulling");
        let second = results.line("");

        assert!(first.is_ok(), "{first:?}");
        assert!(second.is_ok(), "{second:?}");
        assert_eq!(
            out.writes,
            ["→ pulling\n".as_bytes().to_vec(), b"\n".to_vec()]
        );
        assert_eq!(out.calls, ["write", "write"]);
    }

    #[test]
    fn a_results_event_is_one_write_of_one_json_line_and_then_one_flush() {
        let mut out = Counting::default();
        let mut results = Results::new(&mut out);

        let answer = results.event(&value());

        assert!(answer.is_ok(), "{answer:?}");
        assert_eq!(out.calls, ["write", "flush"]);
        let [whole] = out.writes.as_slice() else {
            panic!("expected one write, got {}", out.writes.len());
        };
        assert!(whole.ends_with(b"\n"), "{whole:?}");
        assert_eq!(whole.iter().filter(|byte| **byte == b'\n').count(), 1);
        let parsed: serde_json::Value = serde_json::from_slice(whole).expect("a JSON document");
        assert_eq!(parsed, value());
    }

    #[test]
    fn each_results_event_is_flushed_before_the_next_is_written() {
        let mut out = Counting::default();
        let mut results = Results::new(&mut out);

        for _ in 0..2 {
            let answer = results.event(&value());
            assert!(answer.is_ok(), "{answer:?}");
        }

        assert_eq!(out.calls, ["write", "flush", "write", "flush"]);
    }

    #[test]
    fn a_failed_write_ends_each_results_call_as_the_write_error_is_classified() {
        let mut closed = Failing(io::ErrorKind::BrokenPipe);
        let mut results = Results::new(&mut closed);
        for (name, answer) in [
            ("line", results.line("one line")),
            ("event", results.event(&value())),
        ] {
            let termination = ended(answer);
            assert!(
                matches!(termination, Termination::BrokenPipe),
                "{name}: {termination:?}"
            );
        }

        let mut refusing = Failing(io::ErrorKind::PermissionDenied);
        let mut results = Results::new(&mut refusing);
        for (name, answer) in [
            ("line", results.line("one line")),
            ("event", results.event(&value())),
        ] {
            let termination = ended(answer);
            assert!(
                matches!(
                    &termination,
                    Termination::Internal(error) if matches!(**error, InternalError::Unexpected { .. })
                ),
                "{name}: {termination:?}"
            );
            let diagnostic = termination.diagnostic().expect("a diagnostic");
            assert!(
                diagnostic.contains("the handle refuses the write"),
                "{name}: {diagnostic}"
            );
        }
    }

    #[test]
    fn a_failed_flush_ends_a_results_event_as_the_flush_error_is_classified() {
        let mut closed = FailingFlush(io::ErrorKind::BrokenPipe);
        let termination = ended(Results::new(&mut closed).event(&value()));
        assert!(
            matches!(termination, Termination::BrokenPipe),
            "{termination:?}"
        );

        let mut refusing = FailingFlush(io::ErrorKind::Other);
        let termination = ended(Results::new(&mut refusing).event(&value()));
        assert!(
            matches!(
                &termination,
                Termination::Internal(error) if matches!(**error, InternalError::Unexpected { .. })
            ),
            "{termination:?}"
        );
        let diagnostic = termination.diagnostic().expect("a diagnostic");
        assert!(
            diagnostic.contains("the handle refuses the flush"),
            "{diagnostic}"
        );
    }

    #[test]
    fn a_results_line_does_not_flush_so_a_failing_flush_does_not_end_it() {
        let mut out = FailingFlush(io::ErrorKind::Other);

        let answer = Results::new(&mut out).line("one line");

        assert!(answer.is_ok(), "{answer:?}");
    }
}
