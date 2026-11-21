//! Writing to the stderr handle: a log record, and a notice that is none.
//!
//! The one owner of what the library writes there. A command, a domain module, and the runtime report a
//! condition through [`record`] and never format a record themselves; a line that is no log record, such as a
//! warning, goes through [`notice`]. A domain entry that reports is handed a [`Diagnostics`] by its command
//! file. A failed write is dropped: stderr never changes how an invocation ends.

use std::io::Write;

use crate::logging::{Level, Log};

/// One log record: a condition a module reports.
pub(crate) struct Record<'r> {
    pub level: Level,
    /// The module that reports, such as `parse`.
    pub origin: &'r str,
    pub message: &'r str,
    /// Whether the record is written only when `-v` or a valid `WINTER_LOG_LEVEL` switched the format, for a
    /// condition that already reaches stderr another way when neither did.
    pub only_when_switched: bool,
}

/// Writes `record` to `err` as one line, when `log` asks for it.
///
/// A record below the threshold of `log` is dropped, and so is one marked `only_when_switched` when the format
/// is not switched. A switched format gives the level name, the origin, and the message; otherwise the line is
/// the message alone. The line and its newline are handed to `err` whole, in one `write_all`, and a write that
/// fails is ignored.
pub(crate) fn record(err: &mut dyn Write, log: Log, record: &Record<'_>) {
    if record.level < log.threshold || (record.only_when_switched && !log.switched) {
        return;
    }
    let line = if log.switched {
        format!(
            "{} {}: {}\n",
            level_name(record.level),
            record.origin,
            record.message
        )
    } else {
        format!("{}\n", record.message)
    };
    // What stderr refuses is lost and nothing else changes.
    let _ = err.write_all(line.as_bytes());
}

/// Writes `line` and one newline to `err`: a line that is no log record, written whatever the threshold and
/// the format of the invocation's log.
///
/// The line and its newline are handed to `err` whole, in one `write_all`, and a write that fails is ignored.
pub(crate) fn notice(err: &mut dyn Write, line: &str) {
    let mut whole = String::with_capacity(line.len() + 1);
    whole.push_str(line);
    whole.push('\n');
    // What stderr refuses is lost and nothing else changes.
    let _ = err.write_all(whole.as_bytes());
}

/// What a domain entry that reports borrows from its command file: the stderr handle and the log of the
/// invocation.
///
/// The command file builds it from its context and hands it to its one call. It is no port: nothing else
/// carries it.
pub struct Diagnostics<'a> {
    pub err: &'a mut dyn Write,
    #[allow(
        dead_code,
        reason = "the reporting contract: the log stays whether or not a present entry writes a record"
    )]
    pub log: Log,
}

/// The name a switched record carries for its level.
fn level_name(level: Level) -> &'static str {
    match level {
        Level::Debug => "DEBUG",
        Level::Info => "INFO",
        Level::Warning => "WARNING",
        Level::Error => "ERROR",
        Level::Critical => "CRITICAL",
    }
}

#[cfg(test)]
mod tests {
    use std::io;

    use super::*;

    const LEVELS: [Level; 5] = [
        Level::Debug,
        Level::Info,
        Level::Warning,
        Level::Error,
        Level::Critical,
    ];

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

    /// A handle whose every write and flush fails.
    struct Failing;

    impl Write for Failing {
        fn write(&mut self, _: &[u8]) -> io::Result<usize> {
            Err(io::Error::other("the handle refuses the write"))
        }

        fn flush(&mut self) -> io::Result<()> {
            Err(io::Error::other("the handle refuses the flush"))
        }
    }

    fn switched(threshold: Level) -> Log {
        Log {
            threshold,
            switched: true,
        }
    }

    fn a_record(level: Level) -> Record<'static> {
        Record {
            level,
            origin: "parse",
            message: "the message",
            only_when_switched: false,
        }
    }

    /// What `record` writes to a handle of the test's own.
    fn written(log: Log, given: &Record<'_>) -> String {
        let mut err = Vec::new();
        record(&mut err, log, given);
        String::from_utf8(err).expect("UTF-8")
    }

    #[test]
    fn a_record_below_the_threshold_writes_nothing() {
        for (position, threshold) in LEVELS.into_iter().enumerate() {
            for level in &LEVELS[..position] {
                assert_eq!(
                    written(switched(threshold), &a_record(*level)),
                    "",
                    "{level:?} under {threshold:?}"
                );
            }
        }
    }

    #[test]
    fn a_record_at_or_above_the_threshold_is_written() {
        for (position, threshold) in LEVELS.into_iter().enumerate() {
            for level in &LEVELS[position..] {
                assert_ne!(
                    written(switched(threshold), &a_record(*level)),
                    "",
                    "{level:?} under {threshold:?}"
                );
            }
        }
    }

    #[test]
    fn a_switched_log_writes_one_line_of_the_level_name_the_origin_and_the_message() {
        for (level, name) in [
            (Level::Debug, "DEBUG"),
            (Level::Info, "INFO"),
            (Level::Warning, "WARNING"),
            (Level::Error, "ERROR"),
            (Level::Critical, "CRITICAL"),
        ] {
            assert_eq!(
                written(switched(Level::Debug), &a_record(level)),
                format!("{name} parse: the message\n"),
                "{level:?}"
            );
        }
    }

    #[test]
    fn an_unswitched_log_writes_a_warning_record_as_its_message_alone() {
        for level in [Level::Warning, Level::Error, Level::Critical] {
            assert_eq!(
                written(Log::DEFAULT, &a_record(level)),
                "the message\n",
                "{level:?}"
            );
        }
    }

    #[test]
    fn an_unswitched_log_drops_an_info_and_a_debug_record() {
        for level in [Level::Info, Level::Debug] {
            assert_eq!(written(Log::DEFAULT, &a_record(level)), "", "{level:?}");
        }
    }

    #[test]
    fn a_switched_log_at_warning_drops_a_debug_record_and_writes_a_warning_record_switched() {
        let log = Log {
            threshold: Level::Warning,
            switched: true,
        };

        assert_eq!(written(log, &a_record(Level::Debug)), "");
        assert_eq!(
            written(log, &a_record(Level::Warning)),
            "WARNING parse: the message\n"
        );
    }

    #[test]
    fn a_record_for_a_switched_log_only_writes_nothing_under_an_unswitched_log_at_any_level() {
        for level in LEVELS {
            let given = Record {
                only_when_switched: true,
                ..a_record(level)
            };

            assert_eq!(written(Log::DEFAULT, &given), "", "{level:?}");
        }
    }

    #[test]
    fn a_record_for_a_switched_log_only_is_written_under_a_switched_log_at_its_level() {
        let given = Record {
            only_when_switched: true,
            ..a_record(Level::Error)
        };

        assert_eq!(
            written(switched(Level::Debug), &given),
            "ERROR parse: the message\n"
        );
        assert_eq!(written(switched(Level::Critical), &given), "");
    }

    #[test]
    fn a_record_reaches_the_handle_in_one_write() {
        for log in [switched(Level::Debug), Log::DEFAULT] {
            let mut err = Counting::default();

            record(&mut err, log, &a_record(Level::Warning));

            let [whole] = err.writes.as_slice() else {
                panic!("{log:?}: expected one write, got {}", err.writes.len());
            };
            assert!(whole.ends_with(b"the message\n"), "{log:?}: {whole:?}");
        }
    }

    #[test]
    fn a_notice_is_its_line_and_one_newline_in_one_write() {
        let mut err = Counting::default();

        notice(&mut err, "warning: something drifted");
        notice(&mut err, "");

        assert_eq!(
            err.writes,
            [b"warning: something drifted\n".to_vec(), b"\n".to_vec()]
        );
    }

    #[test]
    fn a_notice_on_a_handle_whose_writes_fail_is_ignored() {
        notice(&mut Failing, "warning: something drifted");
    }

    #[test]
    fn a_notice_through_the_borrow_is_written_whatever_the_log_asks_for() {
        for log in [
            Log::DEFAULT,
            switched(Level::Debug),
            switched(Level::Critical),
        ] {
            let mut err = Vec::new();
            let diagnostics = Diagnostics { err: &mut err, log };

            notice(diagnostics.err, "warning: something drifted");

            assert_eq!(err, b"warning: something drifted\n", "{log:?}");
        }
    }

    #[test]
    fn a_handle_whose_writes_fail_is_ignored() {
        for log in [switched(Level::Debug), Log::DEFAULT] {
            record(&mut Failing, log, &a_record(Level::Critical));
        }
    }
}
