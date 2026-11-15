//! The process boundary: the one file that names the process streams, the process environment, and the exit.
//!
//! `main` snapshots what the process supplies and hands it over; [`conclude`] turns what the run answers, a
//! panic included, into the last bytes on stderr and the exit code.

use std::any::Any;
use std::backtrace::{Backtrace, BacktraceStatus};
use std::io::{self, IsTerminal, Write};
use std::panic::{self, AssertUnwindSafe, PanicHookInfo};
use std::process::ExitCode;
use std::sync::{Mutex, MutexGuard, PoisonError};
use std::thread::{self, ThreadId};

use winter_workspace_core::{Identity, InternalError, Invocation, ProcessEnv, Termination, run};

fn main() -> ExitCode {
    install_panic_hook();

    let args = std::env::args_os().skip(1).collect();
    let cwd = std::env::current_dir();
    let env = ProcessEnv::from_vars(std::env::vars_os());
    let stdout_is_terminal = io::stdout().is_terminal();
    let stderr_is_terminal = io::stderr().is_terminal();

    let code = conclude(
        || {
            let mut stdin = io::stdin().lock();
            let mut stdout = io::stdout().lock();
            let mut stderr = io::stderr().lock();
            run(Invocation {
                args,
                cwd,
                env,
                identity: Identity {
                    program: "winter-workspace",
                    version: env!("CARGO_PKG_VERSION"),
                },
                stdin: &mut stdin,
                stdout: &mut stdout,
                stderr: &mut stderr,
                stdout_is_terminal,
                stderr_is_terminal,
            })
        },
        || io::stdout().flush(),
        &mut io::stderr(),
    );
    ExitCode::from(code)
}

/// Runs an invocation to its exit code, and writes its final diagnostic to `stderr`.
///
/// The closures are called one after the other, `run` first and inside an unwind catch:
///
/// 1. A panic of `run` becomes an internal failure that carries what the panic hook stored for this thread.
/// 2. `flush` is the final flush of stdout. When it fails after an ending with exit code 0, the ending becomes
///    what [`Termination::from_write_error`] makes of the error; an ending that already failed stands.
/// 3. The diagnostic of the ending, when it has one, is written to `stderr`, with a newline added when the text
///    lacks one.
/// 4. The backtrace of an internal failure is written after it, when one was captured.
///
/// A write to `stderr` that fails is ignored, and nothing further is written: the exit code stands.
fn conclude(
    run: impl FnOnce() -> Termination,
    flush: impl FnOnce() -> io::Result<()>,
    stderr: &mut dyn Write,
) -> u8 {
    let thread = thread::current().id();
    arm(thread);
    let ran = panic::catch_unwind(AssertUnwindSafe(run));
    let stored = disarm(thread);
    let mut termination = ran.unwrap_or_else(|payload| {
        let panicked = stored.unwrap_or_else(|| Panicked {
            message: payload_text(payload.as_ref()).to_owned(),
            backtrace: Backtrace::disabled(),
        });
        Termination::Internal(Box::new(InternalError::Panic {
            message: panicked.message,
            backtrace: panicked.backtrace,
        }))
    });

    if let Err(error) = flush()
        && termination.exit_code() == 0
    {
        termination = Termination::from_write_error(error);
    }

    // What stderr refuses is lost: the exit code stands.
    let _ = write_diagnostic(stderr, &termination);
    termination.exit_code()
}

/// Writes the diagnostic of `termination`, and the backtrace an internal failure captured, each as one
/// write that ends with a newline. The first write that fails ends the function.
fn write_diagnostic(stderr: &mut dyn Write, termination: &Termination) -> io::Result<()> {
    if let Some(diagnostic) = termination.diagnostic() {
        stderr.write_all(line_ended(diagnostic).as_bytes())?;
    }
    if let Termination::Internal(error) = termination {
        let (InternalError::Unexpected { backtrace, .. } | InternalError::Panic { backtrace, .. }) =
            &**error;
        if backtrace.status() == BacktraceStatus::Captured {
            stderr.write_all(line_ended(backtrace.to_string()).as_bytes())?;
        }
    }
    Ok(())
}

/// `text` with a newline added when it does not end with one.
fn line_ended(mut text: String) -> String {
    if !text.ends_with('\n') {
        text.push('\n');
    }
    text
}

/// What the panic hook keeps of one panic.
struct Panicked {
    /// Where the panic was raised, then its message.
    message: String,
    backtrace: Backtrace,
}

/// One slot per armed thread, keyed by the thread's id. A slot is empty until that thread panics.
///
/// A thread is armed while [`conclude`] runs its `run` on it. The panic hook stores a panic only in the slot of
/// the thread that panics, so two threads that panic at once each keep their own.
static SLOTS: Mutex<Vec<(ThreadId, Option<Panicked>)>> = Mutex::new(Vec::new());

/// The slots. A lock a panicking holder left poisoned is taken all the same: the hook must not panic.
fn slots() -> MutexGuard<'static, Vec<(ThreadId, Option<Panicked>)>> {
    SLOTS.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Gives `thread` an empty slot.
fn arm(thread: ThreadId) {
    slots().push((thread, None));
}

/// Removes the slot of `thread` and returns what the hook stored in it.
fn disarm(thread: ThreadId) -> Option<Panicked> {
    let mut slots = slots();
    let at = slots.iter().position(|(armed, _)| *armed == thread)?;
    slots.swap_remove(at).1
}

/// Replaces the process's panic hook with one that writes nothing for an armed thread.
///
/// For a panic on an armed thread the hook stores the message with its location, and a backtrace captured as
/// the process's backtrace variables ask, in that thread's slot. A panic on any other thread goes to the hook
/// that was installed before.
fn install_panic_hook() {
    let previous = panic::take_hook();
    panic::set_hook(Box::new(move |info| {
        if !store(info) {
            previous(info);
        }
    }));
}

/// Stores the panic of `info` in the slot of the panicking thread, and answers whether that thread has one.
fn store(info: &PanicHookInfo<'_>) -> bool {
    let thread = thread::current().id();
    let mut slots = slots();
    let Some((_, slot)) = slots.iter_mut().find(|(armed, _)| *armed == thread) else {
        return false;
    };
    let text = payload_text(info.payload());
    let message = match info.location() {
        Some(location) => format!("panicked at {location}: {text}"),
        None => format!("panicked: {text}"),
    };
    *slot = Some(Panicked {
        message,
        backtrace: Backtrace::capture(),
    });
    true
}

/// The message of a panic payload: the payload itself when it is text, otherwise a fixed text.
fn payload_text(payload: &(dyn Any + Send)) -> &str {
    if let Some(text) = payload.downcast_ref::<&str>() {
        text
    } else if let Some(text) = payload.downcast_ref::<String>() {
        text
    } else {
        "a panic whose payload is no text"
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Once;

    use winter_workspace_core::{ParseFault, Usage, UsageError};

    use super::*;

    /// The threads whose panic reached the hook that was installed before the boundary's own.
    static FORWARDED: Mutex<Vec<ThreadId>> = Mutex::new(Vec::new());

    /// Installs the boundary's hook, once for the test process, over a hook that notes the panicking thread
    /// and then reports the panic as the test harness always does.
    fn install() {
        static INSTALLED: Once = Once::new();
        INSTALLED.call_once(|| {
            let harness = panic::take_hook();
            panic::set_hook(Box::new(move |info| {
                FORWARDED
                    .lock()
                    .unwrap_or_else(PoisonError::into_inner)
                    .push(thread::current().id());
                harness(info);
            }));
            install_panic_hook();
        });
    }

    fn was_forwarded(thread: ThreadId) -> bool {
        FORWARDED
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .contains(&thread)
    }

    /// A stderr whose every write fails, and that counts them.
    #[derive(Default)]
    struct Refusing {
        writes: usize,
    }

    impl Write for Refusing {
        fn write(&mut self, _: &[u8]) -> io::Result<usize> {
            self.writes += 1;
            Err(io::Error::other("stderr refuses the write"))
        }

        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    fn text(stderr: Vec<u8>) -> String {
        String::from_utf8(stderr).expect("UTF-8 on stderr")
    }

    fn internal(backtrace: Backtrace) -> Termination {
        Termination::Internal(Box::new(InternalError::Unexpected {
            source: Box::new(io::Error::other("disk on fire")),
            backtrace,
        }))
    }

    fn usage() -> Termination {
        Termination::Usage(Box::new(UsageError::Parse {
            command_path: vec!["ws".to_owned(), "index".to_owned()],
            usage: Usage {
                line: "Usage: winter-workspace ws index [OPTIONS] <NAME>".to_owned(),
                help_command: "winter-workspace ws index --help".to_owned(),
            },
            fault: ParseFault::UnknownOption {
                name: "--nope".to_owned(),
            },
        }))
    }

    fn flush_failing(kind: io::ErrorKind) -> impl FnOnce() -> io::Result<()> {
        move || Err(io::Error::new(kind, "stdout refuses the flush"))
    }

    #[test]
    fn a_run_that_succeeds_and_a_flush_that_succeeds_exit_0_and_write_nothing() {
        install();
        let mut stderr = Vec::new();

        let code = conclude(|| Termination::Success, || Ok(()), &mut stderr);

        assert_eq!(code, 0);
        assert_eq!(text(stderr), "");
    }

    #[test]
    fn the_flush_is_called_after_the_run() {
        install();
        let mut order = Vec::new();
        let mut stderr = Vec::new();
        let events = std::cell::RefCell::new(&mut order);

        conclude(
            || {
                events.borrow_mut().push("run");
                Termination::Success
            },
            || {
                events.borrow_mut().push("flush");
                Ok(())
            },
            &mut stderr,
        );

        assert_eq!(order, ["run", "flush"]);
    }

    #[test]
    fn a_run_that_panics_exits_1_and_its_error_line_holds_the_panic_message() {
        install();
        let mut stderr = Vec::new();

        let code = conclude(
            || panic!("the index is out of range"),
            || Ok(()),
            &mut stderr,
        );

        assert_eq!(code, 1);
        let stderr = text(stderr);
        let first = stderr.lines().next().expect("an error line");
        assert!(first.starts_with("error: "), "{stderr}");
        assert!(first.contains("the index is out of range"), "{stderr}");
        assert!(first.contains(file!()), "{stderr}");
        assert!(!was_forwarded(thread::current().id()));
    }

    #[test]
    fn a_panic_whose_payload_is_no_text_exits_1_with_an_error_line() {
        install();
        let mut stderr = Vec::new();

        let code = conclude(|| panic::panic_any(7_u8), || Ok(()), &mut stderr);

        assert_eq!(code, 1);
        let stderr = text(stderr);
        let first = stderr.lines().next().expect("an error line");
        assert!(first.starts_with("error: "), "{stderr}");
        assert!(first.contains("payload is no text"), "{stderr}");
    }

    /// An unwind that is resumed calls no panic hook, so the slot of the armed thread stays empty.
    #[test]
    fn an_unwind_no_hook_saw_exits_1_with_one_error_line_that_holds_the_payload() {
        install();
        let unwinds: [(Box<dyn Any + Send>, &str); 3] = [
            (Box::new("resumed with a str"), "resumed with a str"),
            (
                Box::new("resumed with a string".to_owned()),
                "resumed with a string",
            ),
            (Box::new(7_u8), "a panic whose payload is no text"),
        ];
        for (payload, message) in unwinds {
            let mut stderr = Vec::new();

            let code = conclude(|| panic::resume_unwind(payload), || Ok(()), &mut stderr);

            assert_eq!(code, 1, "{message}");
            assert_eq!(text(stderr), format!("error: {message}\n"));
        }
        assert!(!was_forwarded(thread::current().id()));
    }

    #[test]
    fn a_panic_leaves_no_slot_behind_and_a_later_run_on_the_thread_starts_empty() {
        install();
        let thread = thread::current().id();

        conclude(|| panic!("the first run"), || Ok(()), &mut Vec::new());

        assert!(slots().iter().all(|(armed, _)| *armed != thread));
        let mut stderr = Vec::new();
        let code = conclude(|| Termination::Success, || Ok(()), &mut stderr);
        assert_eq!(code, 0);
        assert_eq!(text(stderr), "");
    }

    #[test]
    fn an_internal_failure_without_a_backtrace_exits_1_with_one_error_line() {
        install();
        let mut stderr = Vec::new();

        let code = conclude(|| internal(Backtrace::disabled()), || Ok(()), &mut stderr);

        assert_eq!(code, 1);
        assert_eq!(text(stderr), "error: disk on fire\n");
    }

    #[test]
    fn an_internal_failure_with_a_backtrace_writes_the_error_line_and_then_the_backtrace() {
        install();
        let backtrace = Backtrace::force_capture();
        assert_eq!(backtrace.status(), BacktraceStatus::Captured);
        let rendered = line_ended(backtrace.to_string());
        assert!(!rendered.trim().is_empty());
        let mut stderr = Vec::new();

        let code = conclude(|| internal(backtrace), || Ok(()), &mut stderr);

        assert_eq!(code, 1);
        assert_eq!(text(stderr), format!("error: disk on fire\n{rendered}"));
    }

    #[test]
    fn a_panic_on_a_thread_that_is_not_armed_reaches_the_previous_hook_and_leaves_the_armed_slot_empty()
     {
        install();
        let armed = thread::current().id();
        let mut other = None;
        let mut slot_is_empty = false;
        let mut stderr = Vec::new();

        let code = conclude(
            || {
                let joined = thread::spawn(|| -> ThreadId { panic!("a panic of another thread") });
                other = Some(joined.thread().id());
                assert!(joined.join().is_err());
                slot_is_empty = slots()
                    .iter()
                    .any(|(thread, slot)| *thread == armed && slot.is_none());
                Termination::Success
            },
            || Ok(()),
            &mut stderr,
        );

        assert_eq!(text(stderr), "");
        assert_eq!(code, 0);
        assert!(slot_is_empty);
        let other = other.expect("the id of the thread that panicked");
        assert!(was_forwarded(other));
        assert!(slots().iter().all(|(thread, _)| *thread != other));
    }

    #[test]
    fn a_flush_that_fails_as_a_broken_pipe_after_a_success_exits_1_and_writes_nothing() {
        install();
        let mut stderr = Vec::new();

        let code = conclude(
            || Termination::Success,
            flush_failing(io::ErrorKind::BrokenPipe),
            &mut stderr,
        );

        assert_eq!(code, 1);
        assert_eq!(text(stderr), "");
    }

    #[test]
    fn a_flush_that_fails_otherwise_after_a_success_exits_1_with_one_error_line() {
        install();
        let mut stderr = Vec::new();

        let code = conclude(
            || Termination::Success,
            flush_failing(io::ErrorKind::StorageFull),
            &mut stderr,
        );

        assert_eq!(code, 1);
        let stderr = text(stderr);
        let error_line = "error: stdout refuses the flush\n";
        assert!(stderr.starts_with(error_line), "{stderr}");
        // The failure is built here, so it carries a backtrace when the test process asks for them, and that
        // backtrace follows the error line.
        if Backtrace::capture().status() != BacktraceStatus::Captured {
            assert_eq!(stderr, error_line);
        }
    }

    #[test]
    fn a_flush_that_fails_after_a_usage_ending_leaves_its_exit_code_and_its_diagnostic() {
        let diagnostic = usage().diagnostic().expect("a diagnostic");
        for kind in [io::ErrorKind::BrokenPipe, io::ErrorKind::StorageFull] {
            install();
            let mut stderr = Vec::new();

            let code = conclude(usage, flush_failing(kind), &mut stderr);

            assert_eq!(code, 2, "{kind:?}");
            assert_eq!(text(stderr), format!("{diagnostic}\n"), "{kind:?}");
        }
    }

    #[test]
    fn a_diagnostic_that_ends_with_a_newline_gains_none() {
        install();
        let mut stderr = Vec::new();

        let code = conclude(
            || Termination::BareGroup {
                help: "Usage: winter-workspace <COMMAND>\n".to_owned(),
            },
            || Ok(()),
            &mut stderr,
        );

        assert_eq!(code, 2);
        assert_eq!(text(stderr), "Usage: winter-workspace <COMMAND>\n");
    }

    #[test]
    fn a_stderr_that_refuses_its_writes_leaves_the_exit_code_of_each_ending() {
        install();

        let mut stderr = Refusing::default();
        assert_eq!(conclude(usage, || Ok(()), &mut stderr), 2);
        assert_eq!(stderr.writes, 1);

        let mut stderr = Refusing::default();
        let code = conclude(
            || internal(Backtrace::force_capture()),
            || Ok(()),
            &mut stderr,
        );
        assert_eq!(code, 1);
        assert_eq!(
            stderr.writes, 1,
            "nothing is tried after the write that failed"
        );

        let mut stderr = Refusing::default();
        let code = conclude(|| panic!("with no stderr"), || Ok(()), &mut stderr);
        assert_eq!(code, 1);

        let mut stderr = Refusing::default();
        assert_eq!(conclude(|| Termination::Success, || Ok(()), &mut stderr), 0);
        assert_eq!(stderr.writes, 0);
    }
}
