//! The bounded process runner: what a child is given, what is read of it, and how it is ended.

use std::collections::BTreeMap;
use std::fs::Permissions;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use winter_workspace_test_support::{
    Exit, FailureKind, Invocation, InvocationFailure, Observation, TempTree, invoke,
};

/// An invocation of `/bin/sh -c <script>` in `tree`; `arguments` are the script's `$1`, `$2`, ….
fn shell(tree: &TempTree, script: &str, arguments: &[&Path]) -> Invocation {
    let mut invocation = tree.invocation(Path::new("/bin/sh"));
    invocation.args = vec!["-c".into(), script.into(), "sh".into()];
    invocation
        .args
        .extend(arguments.iter().map(|argument| argument.into()));
    invocation
}

fn run(tree: &TempTree, script: &str) -> Result<Observation, InvocationFailure> {
    invoke(shell(tree, script, &[]))
}

fn text(bytes: &[u8]) -> &str {
    std::str::from_utf8(bytes).expect("UTF-8 from the child")
}

/// The directory of the first `git` on the `PATH` of this test process.
fn directory_of_git() -> PathBuf {
    let path = std::env::var_os("PATH").expect("a PATH");
    std::env::split_paths(&path)
        .find(|directory| directory.is_absolute() && directory.join("git").is_file())
        .expect("git on the PATH of the test process")
}

/// The process ids a script wrote to `file`, one per line.
fn recorded_processes(file: &Path) -> Vec<libc::pid_t> {
    std::fs::read_to_string(file)
        .expect("the file the script records its processes in")
        .lines()
        .map(|line| line.trim().parse().expect("a process id"))
        .collect()
}

/// Whether `process` is running. A process that has ended and waits to be reaped is not.
fn is_alive(process: libc::pid_t) -> bool {
    // SAFETY: `kill` takes two integers and touches no memory; signal 0 delivers nothing.
    if unsafe { libc::kill(process, 0) } != 0 {
        return false;
    }
    let ended_unreaped = std::fs::read_to_string(format!("/proc/{process}/stat"))
        .ok()
        .and_then(|stat| {
            stat.rsplit_once(") ")
                .map(|(_, state)| state.starts_with('Z'))
        });
    ended_unreaped != Some(true)
}

/// Asserts `process` ends within a few seconds: a kill is delivered after the call that sends it returns.
fn assert_ends(process: libc::pid_t) {
    let patience = Instant::now() + Duration::from_secs(5);
    while is_alive(process) {
        assert!(
            Instant::now() < patience,
            "process {process} is still alive"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn the_child_has_exactly_the_pinned_environment() {
    // SAFETY: the tests of this file run on several threads, and every read or write of the environment they
    // make goes through `std::env`, which serializes them; no thread reads it through the C library.
    unsafe { std::env::set_var("WINTER_TEST_LEAK_PROBE", "leaked") };
    let tree = TempTree::new().expect("an isolated tree");
    let root = tree.root().to_str().expect("a root that is text");
    let git = directory_of_git();
    let git = git.to_str().expect("a directory that is text");

    let observation = invoke(tree.invocation(Path::new("/usr/bin/env"))).expect("an observation");

    assert_eq!(observation.exit, Exit::Code(0), "{observation:?}");
    assert_eq!(text(&observation.stderr), "");
    let seen: BTreeMap<&str, &str> = text(&observation.stdout)
        .lines()
        .map(|line| line.split_once('=').expect("NAME=VALUE"))
        .collect();
    let pinned = [
        ("PATH", format!("{root}/bin:{git}:/usr/bin:/bin")),
        ("HOME", format!("{root}/home")),
        ("XDG_CONFIG_HOME", format!("{root}/home/.config")),
        ("XDG_CACHE_HOME", format!("{root}/cache")),
        ("TMPDIR", format!("{root}/tmp")),
        ("GIT_CONFIG_GLOBAL", format!("{root}/gitconfig")),
        ("GIT_CONFIG_NOSYSTEM", "1".to_owned()),
        ("GIT_TEMPLATE_DIR", format!("{root}/git-template")),
        ("GIT_CEILING_DIRECTORIES", format!("{root}/..")),
        ("GIT_TERMINAL_PROMPT", "0".to_owned()),
        ("GIT_AUTHOR_NAME", "Fixture Author".to_owned()),
        ("GIT_AUTHOR_EMAIL", "author@fixture.invalid".to_owned()),
        ("GIT_COMMITTER_NAME", "Fixture Committer".to_owned()),
        (
            "GIT_COMMITTER_EMAIL",
            "committer@fixture.invalid".to_owned(),
        ),
        ("GIT_AUTHOR_DATE", "2001-01-01T00:00:00+0000".to_owned()),
        ("GIT_COMMITTER_DATE", "2001-01-01T00:00:00+0000".to_owned()),
        ("TZ", "UTC".to_owned()),
        ("LC_ALL", "C.UTF-8".to_owned()),
        ("LANG", "C.UTF-8".to_owned()),
        ("NO_COLOR", "1".to_owned()),
        ("TERM", "dumb".to_owned()),
        ("COLUMNS", "80".to_owned()),
        ("LINES", "24".to_owned()),
        ("OTEL_SDK_DISABLED", "true".to_owned()),
    ];
    let expected: BTreeMap<&str, &str> = pinned
        .iter()
        .map(|(name, value)| (*name, value.as_str()))
        .collect();
    assert_eq!(seen, expected);
    assert!(std::env::var_os("WINTER_TEST_LEAK_PROBE").is_some());
    assert!(!seen.contains_key("WINTER_TEST_LEAK_PROBE"));
}

#[test]
fn a_variable_a_test_sets_reaches_the_child() {
    let tree = TempTree::new().expect("an isolated tree");
    let mut invocation = shell(&tree, r#"printf '%s %s' "$FIXTURE_VARIABLE" "$TERM""#, &[]);
    invocation.env.set("FIXTURE_VARIABLE", "added");
    invocation.env.set("TERM", "replaced");

    let observation = invoke(invocation).expect("an observation");

    assert_eq!(text(&observation.stdout), "added replaced");
}

#[test]
fn the_child_runs_in_the_workspace_directory_with_umask_022() {
    let tree = TempTree::new().expect("an isolated tree");

    let observation = run(&tree, "pwd -P; umask").expect("an observation");

    assert_eq!(
        text(&observation.stdout),
        format!("{}\n0022\n", tree.workspace_dir().display())
    );
}

#[test]
fn the_two_streams_are_captured_apart() {
    let tree = TempTree::new().expect("an isolated tree");

    let observation = run(&tree, "echo out; echo err >&2").expect("an observation");

    assert_eq!(
        observation,
        Observation {
            stdout: b"out\n".to_vec(),
            stderr: b"err\n".to_vec(),
            exit: Exit::Code(0),
        }
    );
}

#[test]
fn two_streams_that_each_fill_a_pipe_many_times_over_are_both_read_whole() {
    let tree = TempTree::new().expect("an isolated tree");

    let observation = run(
        &tree,
        "head -c 2097152 /dev/zero >&2 & head -c 2097152 /dev/zero; wait",
    )
    .expect("an observation");

    assert_eq!(observation.exit, Exit::Code(0));
    assert_eq!(observation.stdout.len(), 2 * 1024 * 1024);
    assert_eq!(observation.stderr.len(), 2 * 1024 * 1024);
}

#[test]
fn a_non_zero_exit_is_an_observation() {
    let tree = TempTree::new().expect("an isolated tree");

    let observation = run(&tree, "exit 7").expect("an observation, not a failure");

    assert_eq!(observation.exit, Exit::Code(7));
}

#[test]
fn a_child_that_a_signal_ends_is_an_observation() {
    let tree = TempTree::new().expect("an isolated tree");

    let observation = run(&tree, "kill -TERM $$").expect("an observation, not a failure");

    assert_eq!(observation.exit, Exit::Signal(libc::SIGTERM));
}

#[test]
fn the_input_is_written_and_then_closed() {
    let tree = TempTree::new().expect("an isolated tree");
    let mut invocation = shell(&tree, "cat", &[]);
    invocation.stdin = b"first\nsecond\n".to_vec();

    let observation = invoke(invocation).expect("an observation");

    assert_eq!(text(&observation.stdout), "first\nsecond\n");
}

#[test]
fn no_input_is_the_end_of_input_at_once() {
    let tree = TempTree::new().expect("an isolated tree");

    let observation = run(&tree, "cat; echo done").expect("an observation");

    assert_eq!(text(&observation.stdout), "done\n");
}

#[test]
fn input_the_child_never_reads_is_no_failure() {
    let tree = TempTree::new().expect("an isolated tree");
    let mut invocation = shell(&tree, "exit 3", &[]);
    invocation.stdin = vec![b'x'; 1024 * 1024];

    let observation = invoke(invocation).expect("an observation");

    assert_eq!(observation.exit, Exit::Code(3));
}

#[test]
fn a_child_that_outruns_its_deadline_is_killed_with_every_process_it_started() {
    let tree = TempTree::new().expect("an isolated tree");
    let processes = tree.root().join("tmp/processes");
    let mut invocation = shell(
        &tree,
        r#"sleep 60 & echo $$ > "$1"; echo $! >> "$1"; echo started; sleep 60"#,
        &[&processes],
    );
    invocation.timeout = Duration::from_secs(1);
    let started = Instant::now();

    let failure = invoke(invocation).expect_err("a failure");

    assert!(started.elapsed() < Duration::from_secs(30));
    assert_eq!(failure.kind, FailureKind::Timeout, "{failure:?}");
    assert_eq!(text(&failure.stdout), "started\n");
    let processes = recorded_processes(&processes);
    assert_eq!(processes.len(), 2, "{processes:?}");
    for process in processes {
        assert_ends(process);
    }
}

#[test]
fn a_process_the_child_leaves_running_is_killed_when_the_child_ends() {
    let tree = TempTree::new().expect("an isolated tree");
    let started = Instant::now();

    let observation = run(&tree, "sleep 60 & echo $!; exit 4").expect("an observation");

    assert!(started.elapsed() < Duration::from_secs(30));
    assert_eq!(observation.exit, Exit::Code(4));
    let left_running = text(&observation.stdout)
        .trim()
        .parse()
        .expect("a process id");
    assert_ends(left_running);
}

#[test]
fn a_process_that_left_the_group_and_holds_a_stream_open_is_a_cleanup_failure() {
    let tree = TempTree::new().expect("an isolated tree");
    let lookup = run(&tree, "command -v setsid").expect("an observation");
    assert_eq!(
        lookup.exit,
        Exit::Code(0),
        "this test needs `setsid` on the pinned PATH to start a process outside the group: {lookup:?}"
    );
    let started = Instant::now();

    let ready = tree.root().join("tmp/ready");

    // The script ends only once the process has a session of its own and has recorded its id.
    let outcome = invoke(shell(
        &tree,
        r#"setsid sh -c 'echo $$ > "$1"; exec sleep 60' sh "$1" &
           until [ -s "$1" ]; do sleep 0.01; done; cat "$1""#,
        &[&ready],
    ));

    // The process is outside the group the runner kills, so this test ends it.
    let escaped: libc::pid_t = match &outcome {
        Ok(observation) => text(&observation.stdout),
        Err(failure) => text(&failure.stdout),
    }
    .trim()
    .parse()
    .expect("a process id");
    // SAFETY: `kill` takes two integers and touches no memory.
    unsafe { libc::kill(escaped, libc::SIGKILL) };
    assert_ends(escaped);
    assert!(started.elapsed() < Duration::from_secs(30));
    let failure = outcome.expect_err("a failure");
    assert_eq!(failure.kind, FailureKind::Cleanup, "{failure:?}");
}

#[test]
fn a_stream_longer_than_the_limit_is_a_failure_that_keeps_the_limit_of_it() {
    const LIMIT: usize = 8 * 1024 * 1024;
    let tree = TempTree::new().expect("an isolated tree");

    let failure = run(&tree, "head -c 8388609 /dev/zero; sleep 60").expect_err("a failure");

    assert_eq!(failure.kind, FailureKind::OutputLimit);
    assert_eq!(failure.stdout.len(), LIMIT);
    assert_eq!(text(&failure.stderr), "");
}

#[test]
fn the_error_stream_is_held_to_the_same_limit() {
    let tree = TempTree::new().expect("an isolated tree");

    let failure = run(&tree, "head -c 9000000 /dev/zero >&2").expect_err("a failure");

    assert_eq!(failure.kind, FailureKind::OutputLimit);
}

#[test]
fn a_stream_of_exactly_the_limit_is_read_whole() {
    let tree = TempTree::new().expect("an isolated tree");

    let observation = run(&tree, "head -c 8388608 /dev/zero").expect("an observation");

    assert_eq!(observation.stdout.len(), 8 * 1024 * 1024);
}

#[test]
fn a_relative_executable_is_refused_and_nothing_starts() {
    let tree = TempTree::new().expect("an isolated tree");
    let script = tree.workspace_dir().join("mark");
    std::fs::write(&script, "#!/bin/sh\necho started > started\n").expect("a script");
    let mut invocation = tree.invocation(Path::new("/bin/sh"));
    invocation.args = vec!["mark".into()];
    assert_eq!(
        invoke(invocation.clone()).map(|observation| observation.exit),
        Ok(Exit::Code(0))
    );
    let mark = tree.workspace_dir().join("started");
    std::fs::remove_file(&mark).expect("the mark the script left when it did run");

    for relative in ["sh", "./mark", "../ws/mark"] {
        invocation.executable = relative.into();
        invocation.args = vec!["mark".into()];

        let failure = invoke(invocation.clone()).expect_err("a failure");

        assert_eq!(failure.kind, FailureKind::InvalidInvocation, "{relative}");
        assert!(!mark.exists(), "{relative}");
    }
}

#[test]
fn a_relative_working_directory_is_refused() {
    let tree = TempTree::new().expect("an isolated tree");
    let mut invocation = shell(&tree, "exit 0", &[]);
    invocation.cwd = "ws".into();

    let failure = invoke(invocation).expect_err("a failure");

    assert_eq!(failure.kind, FailureKind::InvalidInvocation);
}

#[test]
fn an_executable_that_does_not_exist_is_a_spawn_failure() {
    let tree = TempTree::new().expect("an isolated tree");

    let absent = tree.root().join("bin/absent");

    let failure = invoke(tree.invocation(&absent)).expect_err("a failure");

    let cause = format!(
        "{} in {}: No such file or directory (os error 2)",
        absent.display(),
        tree.workspace_dir().display()
    );
    assert_eq!(
        failure,
        InvocationFailure {
            kind: FailureKind::Spawn(cause.clone()),
            stdout: Vec::new(),
            stderr: Vec::new(),
        }
    );
    assert_eq!(
        failure.to_string(),
        format!("the executable could not be started: {cause}")
    );
    let shown = format!("{failure:?}");
    assert!(shown.contains("No such file or directory"), "{shown}");
}

#[test]
fn an_executable_that_may_not_be_executed_is_a_spawn_failure_that_says_so() {
    let tree = TempTree::new().expect("an isolated tree");
    let plain = tree.root().join("bin/plain");
    std::fs::write(&plain, "").expect("a file inside the tree");
    std::fs::set_permissions(&plain, Permissions::from_mode(0o644))
        .expect("a file nobody may execute");

    let failure = invoke(tree.invocation(&plain)).expect_err("a failure");

    assert!(matches!(failure.kind, FailureKind::Spawn(_)), "{failure:?}");
    let said = failure.to_string();
    assert!(
        said.contains(plain.to_str().expect("a path that is text")),
        "{said}"
    );
    assert!(said.contains("Permission denied"), "{said}");
}

#[test]
fn a_working_directory_that_does_not_exist_is_a_spawn_failure_that_names_it() {
    let tree = TempTree::new().expect("an isolated tree");
    let absent = tree.root().join("tmp/absent");
    let mut invocation = shell(&tree, "exit 0", &[]);
    invocation.cwd = absent.clone();

    let failure = invoke(invocation).expect_err("a failure");

    assert!(matches!(failure.kind, FailureKind::Spawn(_)), "{failure:?}");
    let said = failure.to_string();
    assert!(said.contains("/bin/sh in "), "{said}");
    assert!(
        said.contains(absent.to_str().expect("a path that is text")),
        "{said}"
    );
    assert!(said.contains("No such file or directory"), "{said}");
}
