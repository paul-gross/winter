//! The program port's wait and its deadline, against stub programs in an isolated tree.

use std::ffi::OsStr;
use std::os::unix::fs::PermissionsExt;
use std::time::{Duration, Instant};

use winter_workspace_core::{Call, Exit, OsPrograms, ProcessEnv, Program, RunPrograms, Stdin};
use winter_workspace_test_support::TempTree;

fn shell<'a>(tree: &'a TempTree, args: &'a [&'a OsStr]) -> Call<'a> {
    Call {
        program: Program::Shell,
        args,
        cwd: tree.root(),
        timeout: Some(Duration::from_secs(5)),
        stdin: Stdin::Null,
        remove: &[],
        env: &[],
    }
}

#[test]
fn shell_lines_are_live_ordered_bytes_with_a_final_unterminated_line() {
    let (tree, programs) = with_stub_git("exit 99");
    let args = [OsStr::new(
        "printf 'first\\n'; while [ ! -f acknowledged ]; do sleep 0.01; done; \
         printf '\\377\\n' >&2; printf '\\nlast'; exit 3",
    )];
    let mut lines = Vec::new();
    let exit = programs
        .stream(&shell(&tree, &args), &mut |line| {
            if line == b"first" {
                std::fs::write(tree.root().join("acknowledged"), b"yes")
                    .expect("acknowledge live output");
            }
            lines.push(line.to_vec());
        })
        .expect("stream");
    assert_eq!(exit, Exit::Code(3));
    assert_eq!(
        lines,
        [b"first".to_vec(), vec![255], vec![], b"last".to_vec()]
    );
}

#[test]
fn streaming_reports_a_self_signal_and_deadlines_even_after_eof() {
    let (tree, programs) = with_stub_git("exit 99");
    for (script, expected) in [
        ("kill -TERM $$", Exit::Signal(15)),
        ("sleep 60 & wait", Exit::Signal(9)),
        ("exec >&- 2>&-; sleep 60", Exit::Signal(9)),
    ] {
        let args = [OsStr::new(script)];
        let mut call = shell(&tree, &args);
        call.timeout = Some(Duration::from_millis(300));
        let started = Instant::now();
        assert_eq!(
            programs.stream(&call, &mut |_| {}).expect("stream"),
            expected
        );
        assert!(started.elapsed() < Duration::from_secs(3), "{script}");
    }
}

#[test]
fn shell_environment_removals_precede_overlays_and_do_not_change_the_snapshot() {
    let tree = TempTree::new().expect("tree");
    let mut env = tree.env().clone();
    env.set("LANGUAGE", "custom");
    env.set("LC_ALL", "C.UTF-8");
    env.set("WINTER_LAUNCH_TIME", "123");
    env.set("TO_REMOVE", "original");
    let programs = OsPrograms::new(
        &ProcessEnv::from_vars(env.variables().map(|(k, v)| (k.to_owned(), v.to_owned()))),
        false,
    );
    let args = [OsStr::new(
        "printf '%s|%s|%s|%s|%s' \"$LANGUAGE\" \"$LC_ALL\" \
         \"${WINTER_LAUNCH_TIME-absent}\" \"${TO_REMOVE-absent}\" \"$GIT_SSH_COMMAND\"",
    )];
    let remove = [OsStr::new("TO_REMOVE")];
    let overlay = [(OsStr::new("TO_REMOVE"), OsStr::new("overlay"))];
    let mut call = shell(&tree, &args);
    call.remove = &remove;
    let removed = programs.run(&call).expect("run");
    assert_eq!(removed.stdout, b"custom|C.UTF-8|absent|absent|ssh -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3");
    call.env = &overlay;
    assert!(
        programs
            .run(&call)
            .expect("run")
            .stdout
            .windows(9)
            .any(|s| s == b"|overlay|")
    );
    call.env = &[];
    call.remove = &[];
    assert!(
        programs
            .run(&call)
            .expect("run")
            .stdout
            .windows(10)
            .any(|s| s == b"|original|")
    );
}

#[test]
fn denied_stdin_inheritance_reads_eof() {
    let (tree, programs) = with_stub_git("exit 99");
    let args = [OsStr::new("cat; printf eof")];
    let mut call = shell(&tree, &args);
    call.stdin = Stdin::Inherit;
    assert_eq!(programs.run(&call).expect("run").stdout, b"eof");
    let mut lines = Vec::new();
    assert_eq!(
        programs
            .stream(&call, &mut |line| lines.push(line.to_vec()))
            .expect("stream"),
        Exit::Code(0)
    );
    assert_eq!(lines, [b"eof"]);
}

#[test]
fn authorized_stdin_inheritance_receives_only_the_fixture_input() {
    let tree = TempTree::new().expect("tree");
    let mut invocation = tree.invocation(&std::env::current_exe().expect("test executable"));
    invocation.args = ["--ignored", "--exact", "stdin_fixture"]
        .map(Into::into)
        .to_vec();
    invocation.env.set("WINTER_TEST_FIXTURE", "stdin_fixture");
    invocation.env.set(
        "WINTER_TEST_TMPDIR",
        tree.root().join("tmp").to_str().expect("tree path"),
    );
    invocation.stdin = b"supplied input\n".to_vec();
    let observed = winter_workspace_test_support::invoke(invocation).expect("fixture");
    assert_eq!(
        observed.exit,
        winter_workspace_test_support::Exit::Code(0),
        "{observed:?}"
    );
}

#[test]
#[ignore]
fn stdin_fixture() {
    if std::env::var("WINTER_TEST_FIXTURE").as_deref() != Ok("stdin_fixture") {
        return;
    }
    let tree = TempTree::new().expect("tree");
    let programs = OsPrograms::new(
        &ProcessEnv::from_vars(
            tree.env()
                .variables()
                .map(|(k, v)| (k.to_owned(), v.to_owned())),
        ),
        true,
    );
    let args = [OsStr::new("cat")];
    let mut call = shell(&tree, &args);
    assert_eq!(programs.run(&call).expect("null input").stdout, b"");
    call.stdin = Stdin::Inherit;
    let denied = OsPrograms::new(
        &ProcessEnv::from_vars(
            tree.env()
                .variables()
                .map(|(k, v)| (k.to_owned(), v.to_owned())),
        ),
        false,
    );
    assert_eq!(denied.run(&call).expect("denied input").stdout, b"");
    let mut lines = Vec::new();
    assert_eq!(
        programs
            .stream(&call, &mut |line| lines.push(line.to_vec()))
            .expect("stream"),
        Exit::Code(0)
    );
    assert_eq!(lines, [b"supplied input"]);
}

#[test]
fn a_stream_deadline_delivers_the_partial_line_already_read() {
    let (tree, programs) = with_stub_git("exit 99");
    let args = [OsStr::new("printf ready; sleep 60")];
    let mut call = shell(&tree, &args);
    call.timeout = Some(Duration::from_millis(300));
    let mut lines = Vec::new();
    assert_eq!(
        programs
            .stream(&call, &mut |line| lines.push(line.to_vec()))
            .expect("stream"),
        Exit::Signal(9)
    );
    assert_eq!(lines, [b"ready"]);
}

#[test]
fn git_keeps_its_c_locale() {
    let (tree, _) = with_stub_git("printf '%s|%s' \"$LANGUAGE\" \"$LC_ALL\"");
    let mut env = tree.env().clone();
    env.set("LANGUAGE", "custom");
    env.set("LC_ALL", "C.UTF-8");
    let programs = OsPrograms::new(
        &ProcessEnv::from_vars(env.variables().map(|(k, v)| (k.to_owned(), v.to_owned()))),
        false,
    );
    assert_eq!(run(&tree, &programs, &[], None).stdout, b"C|C");
}

/// An isolated tree whose `bin/git` is the shell script `body`, and the programs of its pinned environment.
fn with_stub_git(body: &str) -> (TempTree, OsPrograms) {
    let tree = TempTree::new().expect("an isolated tree");
    let bin = tree.root().join("bin");
    std::fs::create_dir_all(&bin).expect("a bin directory");
    let git = bin.join("git");
    std::fs::write(&git, format!("#!/bin/sh\n{body}\n")).expect("a stub");
    std::fs::set_permissions(&git, std::fs::Permissions::from_mode(0o755)).expect("executable");
    let programs = OsPrograms::new(
        &ProcessEnv::from_vars(
            tree.env()
                .variables()
                .map(|(name, value)| (name.to_owned(), value.to_owned())),
        ),
        false,
    );
    (tree, programs)
}

fn run(
    tree: &TempTree,
    programs: &OsPrograms,
    args: &[&str],
    timeout: Option<Duration>,
) -> winter_workspace_core::Finished {
    let args: Vec<&OsStr> = args.iter().map(OsStr::new).collect();
    programs
        .run(&Call {
            program: Program::Git,
            stdin: Stdin::Null,
            remove: &[],
            args: &args,
            cwd: tree.root(),
            timeout,
            env: &[],
        })
        .expect("a finished call")
}

#[test]
fn a_megabyte_on_stderr_within_the_timeout_is_captured_whole() {
    let (tree, programs) =
        with_stub_git("head -c 1048576 /dev/zero | tr '\\0' x >&2\nprintf out\nexit 0");

    let finished = run(&tree, &programs, &["fetch"], Some(Duration::from_secs(30)));

    assert_eq!(finished.exit, Exit::Code(0));
    assert_eq!(finished.stdout, b"out");
    assert_eq!(finished.stderr.len(), 1_048_576);
}

#[test]
fn a_call_without_a_timeout_waits_for_the_program() {
    let (tree, programs) = with_stub_git("sleep 0.3\necho done\nexit 3");

    let finished = run(&tree, &programs, &["status"], None);

    assert_eq!(finished.exit, Exit::Code(3));
    assert_eq!(finished.stdout, b"done\n");
}

#[test]
fn a_program_past_its_deadline_is_killed_with_the_child_that_holds_its_stderr() {
    let (tree, programs) = with_stub_git("sleep 60 &\nsleep 60");
    let started = Instant::now();

    let finished = run(
        &tree,
        &programs,
        &["fetch", "origin"],
        Some(Duration::from_millis(1500)),
    );

    assert!(
        started.elapsed() < Duration::from_millis(2500),
        "{:?}",
        started.elapsed()
    );
    assert_eq!(finished.exit, Exit::Signal(9));
    assert_eq!(
        String::from_utf8_lossy(&finished.stderr),
        "Timeout: the command \"git fetch origin\" did not complete in 1 secs."
    );
}

#[test]
fn a_program_that_closes_its_streams_and_runs_on_is_killed_at_the_deadline() {
    let (tree, programs) = with_stub_git("exec >&- 2>&-\nsleep 60");
    let started = Instant::now();

    let finished = run(
        &tree,
        &programs,
        &["fetch"],
        Some(Duration::from_millis(500)),
    );

    assert!(
        started.elapsed() < Duration::from_millis(1500),
        "{:?}",
        started.elapsed()
    );
    assert_eq!(finished.exit, Exit::Signal(9));
}

#[test]
fn a_variable_of_a_call_is_laid_over_that_call_alone() {
    let (tree, programs) = with_stub_git("printf '%s' \"$WINTER_X\"");
    let args = [OsStr::new("status")];
    let call = |env: &'static [(&'static OsStr, &'static OsStr)]| Call {
        program: Program::Git,
        stdin: Stdin::Null,
        remove: &[],
        args: &args,
        cwd: tree.root(),
        timeout: None,
        env,
    };

    let given: [(&OsStr, &OsStr); 1] = [(OsStr::new("WINTER_X"), OsStr::new("1"))];
    let laid = programs
        .run(&Call {
            program: Program::Git,
            stdin: Stdin::Null,
            remove: &[],
            args: &args,
            cwd: tree.root(),
            timeout: None,
            env: &given,
        })
        .expect("a finished call");
    let after = programs.run(&call(&[])).expect("a finished call");

    assert_eq!(laid.stdout, b"1");
    assert_eq!(after.stdout, b"");
}

#[test]
fn a_variable_of_a_call_wins_over_the_snapshot_one_of_its_name() {
    let (tree, programs) = with_stub_git("printf '%s' \"$LC_ALL\"");
    let args = [OsStr::new("status")];

    let finished = programs
        .run(&Call {
            program: Program::Git,
            stdin: Stdin::Null,
            remove: &[],
            args: &args,
            cwd: tree.root(),
            timeout: None,
            env: &[(OsStr::new("LC_ALL"), OsStr::new("given"))],
        })
        .expect("a finished call");

    assert_eq!(finished.stdout, b"given");
}
