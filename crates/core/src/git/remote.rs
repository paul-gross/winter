//! The policy of a remote git call: its timeout, which failures are transient, how long to wait before another
//! attempt, and the loop that applies them.
//!
//! The loop takes the sleep and the random draw as arguments and reads no clock: the program port supplies
//! them, and a test fixes both.

use std::time::Duration;

use super::GitFailure;
use crate::diagnostics::{self, Diagnostics, Record};
use crate::logging::Level;
use crate::process::ProcessEnv;
use crate::programs::Finished;
use crate::termination::repr;

/// The variable of the snapshot that holds the timeout of a remote call, in seconds.
const TIMEOUT_VARIABLE: &str = "WINTER_GIT_TIMEOUT_S";

/// The timeout, in seconds, when the variable is unset or holds no usable value.
const DEFAULT_TIMEOUT_S: u64 = 40;

/// The most a remote call is attempted.
pub(super) const ATTEMPTS: u32 = 3;

/// The longest a delay between two attempts is, in seconds.
const MAX_DELAY_S: f64 = 8.0;

/// The largest timeout taken as given, in seconds, so that the deadline of a call is always representable.
const MAX_TIMEOUT_S: f64 = 1e9;

/// The module a record of this policy names as its origin.
const ORIGIN: &str = "git";

/// The timeout of a remote call, from the variable of `env`, read on every call. A value that cannot be used is
/// reported as a warning through `diagnostics` and the default is the answer.
pub(super) fn timeout(env: &ProcessEnv, diagnostics: &mut Diagnostics<'_>) -> Duration {
    let default = Duration::from_secs(DEFAULT_TIMEOUT_S);
    let Some(raw) = env.get(TIMEOUT_VARIABLE) else {
        return default;
    };
    let raw = raw.to_string_lossy();
    match parse_float(&raw) {
        Some(seconds) if seconds.is_finite() => {
            if seconds > 0.0 {
                Duration::from_secs_f64(seconds.min(MAX_TIMEOUT_S))
            } else {
                warn(
                    diagnostics,
                    &format!(
                        "ignoring non-positive timeout {} (would kill every git subprocess immediately); using default {DEFAULT_TIMEOUT_S}s",
                        float_repr(seconds)
                    ),
                );
                default
            }
        }
        Some(seconds) if seconds == f64::NEG_INFINITY => {
            warn(
                diagnostics,
                &format!(
                    "ignoring non-positive timeout -inf (would kill every git subprocess immediately); using default {DEFAULT_TIMEOUT_S}s"
                ),
            );
            default
        }
        _ => {
            warn(
                diagnostics,
                &format!(
                    "ignoring invalid {TIMEOUT_VARIABLE}={} (expected float seconds); using default {DEFAULT_TIMEOUT_S}s",
                    repr(&raw)
                ),
            );
            default
        }
    }
}

/// Reports `message` as a warning, written whatever the format of the log.
fn warn(diagnostics: &mut Diagnostics<'_>, message: &str) {
    diagnostics::record(
        &mut *diagnostics.err,
        diagnostics.log,
        &Record {
            level: Level::Warning,
            origin: ORIGIN,
            message,
            only_when_switched: false,
        },
    );
}

/// `text` as Python's `float()` parses it: surrounding blanks allowed, and an underscore allowed between two
/// digits. The infinities and `nan` parse too.
fn parse_float(text: &str) -> Option<f64> {
    let text = text.trim();
    let bytes = text.as_bytes();
    for (index, byte) in bytes.iter().enumerate() {
        if *byte == b'_' {
            let digit_before = index > 0 && bytes[index - 1].is_ascii_digit();
            let digit_after = bytes.get(index + 1).is_some_and(u8::is_ascii_digit);
            if !(digit_before && digit_after) {
                return None;
            }
        }
    }
    text.replace('_', "").parse().ok()
}

/// A float as Python's `repr` writes it, for the values a non-positive timeout can be: `-1.0`, `0.0`, `-inf`,
/// and an exponent with a sign and two digits at least.
fn float_repr(value: f64) -> String {
    let text = format!("{value:?}");
    match text.split_once('e') {
        Some((mantissa, exponent)) => {
            let exponent: i32 = exponent.parse().unwrap_or(0);
            let sign = if exponent < 0 { '-' } else { '+' };
            format!("{mantissa}e{sign}{:02}", exponent.abs())
        }
        None => text,
    }
}

/// Whether a finished call failed in a way that another attempt may mend: it ended by a signal, or its stderr
/// matches, case-insensitively, one of the five patterns of a dropped connection or an expired timeout.
pub(super) fn transient(finished: &Finished) -> bool {
    if matches!(finished.exit, crate::programs::Exit::Signal(_)) {
        return true;
    }
    String::from_utf8_lossy(&finished.stderr)
        .to_lowercase()
        .lines()
        .any(transient_line)
}

/// Whether one line of lowercase stderr holds a transient pattern. A `.*` of a pattern stays within the line.
fn transient_line(line: &str) -> bool {
    let after = |start: &str, then: &str| {
        line.find(start)
            .is_some_and(|at| line[at + start.len()..].contains(then))
    };
    after("connection closed by ", " port 22")
        || line.contains("fatal: the remote end hung up unexpectedly")
        || line.contains("kex_exchange_identification")
        || line.contains("connection timed out")
        || after("timeout: the command ", " did not complete")
}

/// The seconds to wait after the failed attempt number `attempt`, counted from 1: the base, which doubles from
/// one second to a limit of eight, plus or minus half of it by `draw`, a number in `[-1, 1]`, within `[0, 8]`.
pub(super) fn delay(attempt: u32, draw: f64) -> f64 {
    let base = (2.0_f64.powi(attempt.saturating_sub(1).min(16) as i32)).min(MAX_DELAY_S);
    (base + base * 0.5 * draw).clamp(0.0, MAX_DELAY_S)
}

/// Makes the call `attempt` until it succeeds, fails in a way that is not transient, or has been made
/// [`ATTEMPTS`] times, and answers what the last one finished with. A transient failure that is retried is
/// reported as a warning through `diagnostics`, and `sleep` waits out its delay. A call that cannot be started is
/// an internal failure at once.
pub(super) fn attempts(
    attempt: &mut dyn FnMut() -> Result<Finished, GitFailure>,
    sleep: &mut dyn FnMut(Duration),
    draw: &mut dyn FnMut() -> f64,
    diagnostics: &mut Diagnostics<'_>,
) -> Result<Finished, GitFailure> {
    let mut number = 1;
    loop {
        let finished = attempt()?;
        let failed = finished.exit != crate::programs::Exit::Code(0);
        if !failed || number >= ATTEMPTS || !transient(&finished) {
            return Ok(finished);
        }
        let seconds = delay(number, draw());
        warn(
            diagnostics,
            &format!(
                "transient git error (attempt {number}/{ATTEMPTS}): {} — retrying in {seconds:.2}s",
                String::from_utf8_lossy(&finished.stderr).trim()
            ),
        );
        sleep(Duration::from_secs_f64(seconds));
        number += 1;
    }
}

#[cfg(test)]
mod tests {
    use std::ffi::OsString;

    use super::*;
    use crate::logging::Log;
    use crate::programs::Exit;

    fn env(value: Option<&str>) -> ProcessEnv {
        ProcessEnv::from_vars(
            value.map(|value| (OsString::from(TIMEOUT_VARIABLE), OsString::from(value))),
        )
    }

    fn read(value: Option<&str>) -> (Duration, String) {
        let mut err = Vec::new();
        let timeout = timeout(
            &env(value),
            &mut Diagnostics {
                err: &mut err,
                log: Log::DEFAULT,
            },
        );
        (timeout, String::from_utf8(err).expect("utf-8"))
    }

    #[test]
    fn an_unset_timeout_is_forty_seconds_with_no_warning() {
        assert_eq!(read(None), (Duration::from_secs(40), String::new()));
    }

    #[test]
    fn a_value_float_cannot_parse_is_invalid() {
        for raw in ["abc", "inf", "nan", "", "1__0", "_1", "1_"] {
            let (timeout, err) = read(Some(raw));
            assert_eq!(timeout, Duration::from_secs(40), "{raw:?}");
            assert!(
                err.starts_with("ignoring invalid WINTER_GIT_TIMEOUT_S="),
                "{raw:?}: {err}"
            );
        }
        assert_eq!(
            read(Some("abc")).1,
            "ignoring invalid WINTER_GIT_TIMEOUT_S='abc' (expected float seconds); using default 40s\n"
        );
    }

    #[test]
    fn a_value_that_is_not_positive_warns_with_its_float_repr() {
        for (raw, shown) in [
            ("-1", "-1.0"),
            ("0", "0.0"),
            ("-inf", "-inf"),
            ("-1e-7", "-1e-07"),
        ] {
            let (timeout, err) = read(Some(raw));
            assert_eq!(timeout, Duration::from_secs(40), "{raw}");
            assert_eq!(
                err,
                format!(
                    "ignoring non-positive timeout {shown} (would kill every git subprocess immediately); using default 40s\n"
                )
            );
        }
    }

    #[test]
    fn a_positive_value_is_its_seconds() {
        assert_eq!(
            read(Some("2.5")),
            (Duration::from_millis(2500), String::new())
        );
        assert_eq!(read(Some("1_0")).0, Duration::from_secs(10));
        assert_eq!(read(Some(" 3 ")).0, Duration::from_secs(3));
        assert_eq!(read(Some("1e400")).0, Duration::from_secs(40));
    }

    fn stderr(text: &str) -> Finished {
        Finished {
            exit: Exit::Code(128),
            stdout: Vec::new(),
            stderr: text.as_bytes().to_vec(),
        }
    }

    #[test]
    fn the_five_patterns_and_a_signal_are_transient() {
        for text in [
            "Connection closed by 10.0.0.1 port 22",
            "CONNECTION CLOSED BY remote host port 22\n",
            "fatal: The remote end hung up unexpectedly",
            "kex_exchange_identification: read: Connection reset",
            "ssh: connect to host x port 22: Connection timed out",
            "Timeout: the command \"git fetch origin\" did not complete in 40 secs.",
        ] {
            assert!(transient(&stderr(text)), "{text}");
        }
        assert!(transient(&Finished {
            exit: Exit::Signal(9),
            stdout: Vec::new(),
            stderr: Vec::new(),
        }));
    }

    #[test]
    fn other_failures_and_a_pattern_split_across_lines_are_not_transient() {
        for text in [
            "git@github.com: Permission denied (publickey).",
            "fatal: couldn't find remote ref main",
            "Connection closed by\nport 22",
            "Timeout: the command\n did not complete",
            "",
        ] {
            assert!(!transient(&stderr(text)), "{text}");
        }
    }

    #[test]
    fn the_delay_doubles_to_eight_seconds_and_jitters_by_half() {
        let high: Vec<f64> = (1..=5).map(|attempt| delay(attempt, 1.0)).collect();
        let low: Vec<f64> = (1..=5).map(|attempt| delay(attempt, -1.0)).collect();

        assert_eq!(high, [1.5, 3.0, 6.0, 8.0, 8.0]);
        assert_eq!(low, [0.5, 1.0, 2.0, 4.0, 4.0]);
    }

    struct Scripted {
        answers: Vec<Result<Finished, ()>>,
        sleeps: Vec<Duration>,
        notices: String,
    }

    fn drive(answers: Vec<Result<Finished, ()>>) -> (Result<Finished, GitFailure>, Scripted) {
        let mut state = Scripted {
            answers,
            sleeps: Vec::new(),
            notices: String::new(),
        };
        let mut err = Vec::new();
        let answers = std::cell::RefCell::new(std::mem::take(&mut state.answers).into_iter());
        let sleeps = std::cell::RefCell::new(Vec::new());
        let result = attempts(
            &mut || {
                answers
                    .borrow_mut()
                    .next()
                    .expect("an attempt was scripted")
                    .map_err(|()| {
                        GitFailure::Internal(Box::new(
                            crate::termination::InternalError::unexpected(std::io::Error::other(
                                "no git",
                            )),
                        ))
                    })
            },
            &mut |duration| sleeps.borrow_mut().push(duration),
            &mut || 0.0,
            &mut Diagnostics {
                err: &mut err,
                log: Log::DEFAULT,
            },
        );
        state.sleeps = sleeps.into_inner();
        state.notices = String::from_utf8(err).expect("utf-8");
        (result, state)
    }

    fn ok() -> Finished {
        Finished {
            exit: Exit::Code(0),
            stdout: Vec::new(),
            stderr: Vec::new(),
        }
    }

    #[test]
    fn a_transient_failure_is_retried_after_its_delay_and_noticed() {
        let hung = stderr("fatal: the remote end hung up unexpectedly\n");

        let (result, state) = drive(vec![Ok(hung.clone()), Ok(hung), Ok(ok())]);

        assert_eq!(result.expect("success").exit, Exit::Code(0));
        assert_eq!(
            state.sleeps,
            [Duration::from_secs(1), Duration::from_secs(2)]
        );
        assert_eq!(
            state.notices,
            "transient git error (attempt 1/3): fatal: the remote end hung up unexpectedly — retrying in 1.00s\n\
             transient git error (attempt 2/3): fatal: the remote end hung up unexpectedly — retrying in 2.00s\n"
        );
    }

    #[test]
    fn the_third_transient_failure_is_the_answer() {
        let hung = stderr("kex_exchange_identification");

        let (result, state) = drive(vec![Ok(hung.clone()), Ok(hung.clone()), Ok(hung)]);

        assert_eq!(result.expect("a finished call").exit, Exit::Code(128));
        assert_eq!(state.sleeps.len(), 2);
    }

    #[test]
    fn a_failure_that_is_not_transient_is_not_retried() {
        let (result, state) = drive(vec![Ok(stderr("Permission denied (publickey)"))]);

        assert_eq!(result.expect("a finished call").exit, Exit::Code(128));
        assert!(state.sleeps.is_empty() && state.notices.is_empty());
    }

    #[test]
    fn a_git_that_cannot_start_is_not_retried() {
        let (result, state) = drive(vec![Err(())]);

        assert!(matches!(result, Err(GitFailure::Internal(_))));
        assert!(state.sleeps.is_empty());
    }
}
