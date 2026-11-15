//! The interrupt: the SIGINT handler and the one interrupt state of the process.
//!
//! Beside the process boundary this is the one file of the binary that touches the process: it reads and may
//! change the disposition of SIGINT, and its handler writes to file descriptor 2 and ends the process.

use std::ptr;
use std::sync::atomic::{AtomicPtr, Ordering};

use winter_workspace_core::Interrupts;

/// The interrupt state of the process: the handler reads it, and every invocation is handed it.
pub(crate) static INTERRUPTS: Interrupts = Interrupts::new();

/// What the handler writes to stderr before it ends the process.
const ABORTED_NOTICE: &[u8] = b"aborted\n";

/// The exit code of a process the handler ends.
const ABORTED_EXIT: libc::c_int = 1;

/// The state the installed handler reads: null until [`install`] stores the address of its `&'static Interrupts`,
/// which it does before it installs the handler, and never changed after.
static HANDLED: AtomicPtr<Interrupts> = AtomicPtr::new(ptr::null_mut());

/// Installs the SIGINT handler over `interrupts`, unless the process started with SIGINT ignored.
///
/// The disposition the process has is read first, without changing it. When it is ignore, whatever started the
/// process asked for that: nothing is installed, SIGINT stays ignored, and an interrupt does nothing for the whole
/// run. Any other disposition is replaced by the handler.
///
/// The handler notes every interrupt in `interrupts`, first. When a command defers the interrupt, it then
/// returns. Otherwise it writes the aborted notice to stderr and ends the process with exit code 1, at once:
/// nothing is flushed and no destructor runs. The first call that installs names the state for the life of the
/// process; a later call installs the same handler over that first state.
pub(crate) fn install(interrupts: &'static Interrupts) {
    // SAFETY: `sigaction` is a C structure of integers, a signal set, and addresses, for each of which the
    // all-zero pattern is a value; the call below overwrites it before any field is read.
    let mut inherited: libc::sigaction = unsafe { std::mem::zeroed() };
    // SAFETY: no new action is passed, so the call changes nothing; the second pointer is to `inherited`, which
    // is live and exclusively borrowed for the call, and the call writes that structure and nothing else.
    let asked = unsafe { libc::sigaction(libc::SIGINT, ptr::null(), &raw mut inherited) };
    if asked == 0 && inherited.sa_sigaction == libc::SIG_IGN {
        return;
    }

    // An `Err` is a state already stored, which stays.
    let _ = HANDLED.compare_exchange(
        ptr::null_mut(),
        ptr::from_ref(interrupts).cast_mut(),
        Ordering::SeqCst,
        Ordering::SeqCst,
    );

    // SAFETY: as for `inherited`; the fields that matter are set below before the structure is used.
    let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
    action.sa_sigaction = on_interrupt as extern "C" fn(libc::c_int) as libc::sighandler_t;
    // The system restarts a call it can restart when the handler returns into it: a `read`, a `write`, or a
    // `wait` is resumed. A `poll`, a `select`, an `epoll_wait`, and a sleep are never restarted and fail as
    // interrupted, so a command that defers still handles that error from those.
    action.sa_flags = libc::SA_RESTART;
    // SAFETY: the pointer is to the mask of `action`, which is live and exclusively borrowed for the call;
    // `sigemptyset` writes that set and nothing else.
    unsafe { libc::sigemptyset(&raw mut action.sa_mask) };
    // SAFETY: `action` is live and fully set for the call, and no previous action is asked for. Without
    // `SA_SIGINFO` the kernel calls the handler with the signal number alone, which is the type of
    // `on_interrupt`, and that function calls nothing that is not async-signal-safe. The call fails only for a
    // signal number or an address that is not valid, and both are.
    unsafe { libc::sigaction(libc::SIGINT, &raw const action, ptr::null_mut()) };
}

/// The handler. It calls only what is async-signal-safe: atomic loads and stores, `write`, and `_exit`.
///
/// The note comes before the test of deferral. The signal may arrive on another thread than the command's, and
/// with the test first the command could release and read `pending` between the test and the note, and the
/// interrupt would be lost.
extern "C" fn on_interrupt(_signal: libc::c_int) {
    // SAFETY: the address is null or the one `install` stored, which it took from a `&'static Interrupts`: when
    // it is not null it points to a value that lives as long as the process and is only ever read through
    // shared references. `install` stored it before the handler existed.
    let interrupts = unsafe { HANDLED.load(Ordering::SeqCst).as_ref() };
    if let Some(interrupts) = interrupts {
        interrupts.note();
        if interrupts.is_deferred() {
            return;
        }
    }
    // SAFETY: the pointer and the length are those of one static byte string, and `write` only reads them.
    // What file descriptor 2 refuses is lost: the process ends all the same.
    unsafe {
        libc::write(
            libc::STDERR_FILENO,
            ABORTED_NOTICE.as_ptr().cast(),
            ABORTED_NOTICE.len(),
        )
    };
    // SAFETY: `_exit` takes an integer and does not return. It runs no exit handler, flushes no buffer, and
    // unwinds no frame, so no state of the interrupted thread is observed after it.
    unsafe { libc::_exit(ABORTED_EXIT) };
}

#[cfg(test)]
mod tests {
    use std::io::Write;

    use winter_workspace_test_support::{Exit, Observation, TempTree, invoke};

    use super::*;

    /// Names the fixture a child test process was started to run. A fixture does nothing without its own name
    /// here.
    const FIXTURE_VARIABLE: &str = "WINTER_TEST_FIXTURE";

    /// A test of this module that raises SIGINT on its own process with nothing deferred, which ends the
    /// process, so it runs only in a child process.
    const NOT_DEFERRED_TEST: &str = "interrupt::tests::fixture_an_interrupt_that_is_not_deferred";

    /// As [`NOT_DEFERRED_TEST`], with the interrupt deferred.
    const DEFERRED_TEST: &str = "interrupt::tests::fixture_an_interrupt_that_is_deferred";

    /// As [`NOT_DEFERRED_TEST`], in a process that ignores SIGINT when the installer is called.
    const IGNORED_TEST: &str =
        "interrupt::tests::fixture_an_interrupt_that_was_ignored_at_the_start";

    /// What a fixture writes to stdout once it has called the installer.
    const READY_LINE: &str = "the installer was called\n";

    /// What the deferred fixture writes to stdout once the handler has returned and the interrupt is pending.
    const PENDING_LINE: &str = "the interrupt is pending\n";

    /// What the ignored fixture writes to stdout once the interrupt has done nothing.
    const IGNORED_LINE: &str = "the interrupt was ignored\n";

    /// Whether this process is the child test process a test started to run the fixture `name`.
    fn started_as_fixture(name: &str) -> bool {
        std::env::var_os(FIXTURE_VARIABLE).is_some_and(|fixture| fixture == name)
    }

    /// Runs `fixture` alone in a child process of this test binary, under the pinned environment of a tree of
    /// its own, and returns what the child wrote and how it ended.
    fn fixture(fixture: &str) -> Observation {
        let tree = TempTree::new().expect("an isolated tree");
        let this_test_binary = std::env::current_exe().expect("the path of the test executable");
        let mut invocation = tree.invocation(&this_test_binary);
        invocation.args = [fixture, "--exact", "--ignored", "--nocapture"]
            .iter()
            .map(Into::into)
            .collect();
        let parent = tree.root().join("tmp");
        invocation.env.set(
            "WINTER_TEST_TMPDIR",
            parent.to_str().expect("a path inside a tree is text"),
        );
        invocation.env.set(FIXTURE_VARIABLE, fixture);
        invoke(invocation).expect("the child test process runs to its end")
    }

    fn text(bytes: &[u8]) -> String {
        String::from_utf8_lossy(bytes).into_owned()
    }

    /// Writes `line` to the stdout of the process itself, past the capture of the test harness, and flushes.
    fn say(line: &str) {
        let mut stdout = std::io::stdout().lock();
        stdout.write_all(line.as_bytes()).expect("a stdout");
        stdout.flush().expect("a stdout");
    }

    /// The disposition of SIGINT a launcher starts a process with.
    enum Started {
        /// The default: an interrupt ends the process unless a handler is installed.
        Default,
        /// Ignore: the launcher shields the process from an interrupt.
        Ignoring,
    }

    /// Gives this process the disposition of SIGINT a launcher would have started it with. A fixture calls it
    /// before the installer, so that what the installer finds is the fixture's choice and not whatever the test
    /// run itself was started with.
    ///
    /// It sets the disposition only, not the signal mask, which a child test process inherits from the test run.
    /// A test run started with SIGINT blocked leaves the raised signal waiting, and the tests of the handler
    /// fail there.
    fn start_with(started: Started) {
        let disposition = match started {
            Started::Default => libc::SIG_DFL,
            Started::Ignoring => libc::SIG_IGN,
        };
        // SAFETY: `signal` takes a signal number and a disposition, and `disposition` is one of the two
        // constants above, neither of which is a function the kernel would call; it touches no memory of the
        // caller.
        let before = unsafe { libc::signal(libc::SIGINT, disposition) };
        assert_ne!(before, libc::SIG_ERR, "the disposition of SIGINT is set");
    }

    /// Sends SIGINT to the calling thread. The handler, when one is installed, has run by the time this
    /// returns, when it returns.
    fn interrupt_this_process() {
        // SAFETY: `raise` takes a signal number and touches no memory of the caller. What it runs, if
        // anything, is the handler the fixture installed just before, which is async-signal-safe.
        let raised = unsafe { libc::raise(libc::SIGINT) };
        assert_eq!(raised, 0, "SIGINT is raised");
    }

    #[test]
    #[ignore = "a fixture: it ends its own process, so it runs only as the child of the test that asserts on it"]
    fn fixture_an_interrupt_that_is_not_deferred() {
        if !started_as_fixture(NOT_DEFERRED_TEST) {
            return;
        }
        static OWN: Interrupts = Interrupts::new();
        start_with(Started::Default);
        install(&OWN);
        say(READY_LINE);

        interrupt_this_process();

        say("the handler returned\n");
    }

    #[test]
    #[ignore = "a fixture: it interrupts its own process, so it runs only as the child of the test that asserts on it"]
    fn fixture_an_interrupt_that_is_deferred() {
        if !started_as_fixture(DEFERRED_TEST) {
            return;
        }
        static OWN: Interrupts = Interrupts::new();
        start_with(Started::Default);
        install(&OWN);
        OWN.defer();
        say(READY_LINE);

        interrupt_this_process();

        assert!(OWN.pending(), "the handler noted the interrupt");
        assert!(OWN.is_deferred());
        say(PENDING_LINE);
    }

    #[test]
    #[ignore = "a fixture: it interrupts its own process, so it runs only as the child of the test that asserts on it"]
    fn fixture_an_interrupt_that_was_ignored_at_the_start() {
        if !started_as_fixture(IGNORED_TEST) {
            return;
        }
        static OWN: Interrupts = Interrupts::new();
        start_with(Started::Ignoring);
        install(&OWN);
        say(READY_LINE);

        interrupt_this_process();

        assert!(!OWN.pending(), "no handler noted the interrupt");
        say(IGNORED_LINE);
    }

    #[test]
    fn an_interrupt_that_is_not_deferred_writes_the_aborted_notice_and_exits_1() {
        let observation = fixture(NOT_DEFERRED_TEST);

        assert_eq!(observation.exit, Exit::Code(1), "{observation:?}");
        let stderr = text(&observation.stderr);
        assert_eq!(stderr, "aborted\n", "{observation:?}");
        let stdout = text(&observation.stdout);
        assert!(stdout.ends_with(READY_LINE), "{observation:?}");
    }

    #[test]
    fn an_interrupt_that_is_deferred_is_noted_and_the_process_runs_on() {
        let observation = fixture(DEFERRED_TEST);

        assert_eq!(observation.exit, Exit::Code(0), "{observation:?}");
        let stdout = text(&observation.stdout);
        let ready = stdout.find(READY_LINE).expect("the ready line");
        let pending = stdout.find(PENDING_LINE).expect("the second line");
        assert!(ready < pending, "{observation:?}");
        assert!(
            stdout.contains("test result: ok. 1 passed"),
            "{observation:?}"
        );
        assert!(
            !text(&observation.stderr).contains("aborted"),
            "{observation:?}"
        );
    }

    #[test]
    fn an_interrupt_ignored_at_the_start_stays_ignored_and_the_process_runs_on() {
        let observation = fixture(IGNORED_TEST);

        assert_eq!(observation.exit, Exit::Code(0), "{observation:?}");
        let stdout = text(&observation.stdout);
        let ready = stdout.find(READY_LINE).expect("the ready line");
        let ignored = stdout.find(IGNORED_LINE).expect("the second line");
        assert!(ready < ignored, "{observation:?}");
        assert!(
            stdout.contains("test result: ok. 1 passed"),
            "{observation:?}"
        );
        assert_eq!(text(&observation.stderr), "", "{observation:?}");
    }
}
