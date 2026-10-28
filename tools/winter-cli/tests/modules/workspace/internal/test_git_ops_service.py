from __future__ import annotations

import contextvars
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import git
import pytest

from winter_cli.core.ssh_keepalives import SSH_ENV_VAR, SSH_KEEPALIVE_OPTIONS, ensure_ssh_keepalives
from winter_cli.modules.workspace.internal.git_ops_service import (
    BASE_DELAY_S,
    DELAY_CAP_S,
    JITTER_RATIO,
    OPERATION_TIMEOUT_S,
    TIMEOUT_ENV_VAR,
    GitOpsService,
    is_transient_git_error,
)
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.models import RepoError

_CWD = Path("/tmp")


def _git_err(stderr: str, *, status: int = 128) -> git.GitCommandError:
    return git.GitCommandError(("git", "fetch", "origin"), status, stderr=stderr)


# ── is_transient_git_error ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stderr",
    [
        "Connection closed by 217.197.84.140 port 22",
        "fatal: the remote end hung up unexpectedly",
        "kex_exchange_identification: Connection closed by remote host",
        "ssh: connect to host codeberg.org port 22: Connection timed out",
        "CONNECTION CLOSED BY 217.197.84.140 PORT 22",
        # GitPython's kill_after_timeout watchdog message — must be transient
        # so the retry loop kicks in on a wedged SSH (the whole point of #30).
        'Timeout: the command "git fetch origin" did not complete in 40 secs.',
    ],
)
def test_is_transient_git_error_matches_documented_stderr(stderr: str) -> None:
    assert is_transient_git_error(_git_err(stderr))


def test_is_transient_git_error_treats_signal_killed_as_transient() -> None:
    """Structural fallback for the GitPython-wording-drift problem: if
    `kill_after_timeout` fires, the git subprocess exits with SIGKILL and
    POSIX Popen reports `returncode == -9`. Treating any negative status as
    transient ensures the retry path stays armed even if GitPython renames
    the watchdog message in a future release."""
    assert is_transient_git_error(_git_err("", status=-9))
    assert is_transient_git_error(_git_err("anything at all", status=-15))


@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: Authentication failed",
        "fatal: repository 'foo' does not exist",
        "! [rejected] main -> main (non-fast-forward)",
        "fatal: Could not read from remote repository.",
        "",
    ],
)
def test_is_transient_git_error_rejects_non_transient(stderr: str) -> None:
    assert not is_transient_git_error(_git_err(stderr))


# ── run_remote ─────────────────────────────────────────────────────────────


def _service(sleeps: list[float], *, timeout_s: float = 30.0) -> GitOpsService:
    return GitOpsService(
        RepoErrorFactory(),
        sleep=lambda d: sleeps.append(d),
        jitter=lambda: 0.0,
        timeout_s=timeout_s,
    )


def test_run_remote_returns_value_on_success() -> None:
    sleeps: list[float] = []
    svc = _service(sleeps)
    assert svc.run_remote(lambda _t: "ok", cwd=_CWD, message="x") == "ok"
    assert sleeps == []


def test_run_remote_threads_resolved_timeout_into_op() -> None:
    """The whole point of the signature change for #30: the op must receive
    the resolved per-call timeout so it can pass it to GitPython's
    `kill_after_timeout`. Without this thread-through, the wedge-protection
    in `r.git.fetch(..., kill_after_timeout=...)` never engages."""
    sleeps: list[float] = []
    svc = _service(sleeps, timeout_s=42.0)
    received: list[float] = []

    def op(timeout_s: float) -> str:
        received.append(timeout_s)
        return "ok"

    assert svc.run_remote(op, cwd=_CWD, message="x") == "ok"
    assert received == [42.0]


def test_run_remote_retries_transient_up_to_max_attempts() -> None:
    sleeps: list[float] = []
    svc = _service(sleeps)
    transient = _git_err("Connection closed by 1.2.3.4 port 22")
    calls = {"n": 0}

    def op(_timeout: float):
        calls["n"] += 1
        raise transient

    with pytest.raises(RepoError) as ei:
        svc.run_remote(op, cwd=_CWD, message="fetch failed")
    assert calls["n"] == svc.MAX_ATTEMPTS
    assert len(sleeps) == svc.MAX_ATTEMPTS - 1
    assert ei.value.subcommand == "fetch"
    assert "Connection closed" in (ei.value.stderr or "")


def test_run_remote_retries_on_gitpython_timeout_message() -> None:
    """When GitPython's `kill_after_timeout` fires it writes the
    'Timeout: the command "..." did not complete' string into stderr.
    That must trigger the same retry path as a wedged SSH connection —
    a single slow-network blip shouldn't tear down a multi-repo sync."""
    sleeps: list[float] = []
    svc = _service(sleeps)
    timeout_err = _git_err('Timeout: the command "git fetch origin" did not complete in 40 secs.')
    calls = {"n": 0}

    def op(_timeout: float):
        calls["n"] += 1
        raise timeout_err

    with pytest.raises(RepoError):
        svc.run_remote(op, cwd=_CWD, message="fetch failed")
    assert calls["n"] == svc.MAX_ATTEMPTS


def test_run_remote_retries_on_signal_killed_subprocess() -> None:
    """Structural fallback path: a SIGKILL-induced exit (status < 0) is
    treated as transient even if the stderr wording shifts. This is what
    keeps the retry loop alive across GitPython upgrades."""
    sleeps: list[float] = []
    svc = _service(sleeps)
    killed = _git_err("some unfamiliar wording", status=-9)
    calls = {"n": 0}

    def op(_timeout: float):
        calls["n"] += 1
        raise killed

    with pytest.raises(RepoError):
        svc.run_remote(op, cwd=_CWD, message="fetch failed")
    assert calls["n"] == svc.MAX_ATTEMPTS


def test_run_remote_succeeds_after_transient_then_success() -> None:
    sleeps: list[float] = []
    svc = _service(sleeps)
    sequence = [_git_err("Connection closed by 1.2.3.4 port 22"), None]
    calls = {"n": 0}

    def op(_timeout: float):
        i = calls["n"]
        calls["n"] += 1
        item = sequence[i]
        if isinstance(item, BaseException):
            raise item
        return "recovered"

    assert svc.run_remote(op, cwd=_CWD, message="fetch failed") == "recovered"
    assert calls["n"] == 2
    assert len(sleeps) == 1


def test_run_remote_does_not_retry_non_transient() -> None:
    sleeps: list[float] = []
    svc = _service(sleeps)
    non_transient = _git_err("fatal: Authentication failed")
    calls = {"n": 0}

    def op(_timeout: float):
        calls["n"] += 1
        raise non_transient

    with pytest.raises(RepoError):
        svc.run_remote(op, cwd=_CWD, message="push failed")
    assert calls["n"] == 1
    assert sleeps == []


def test_run_remote_backoff_respects_cap_and_jitter() -> None:
    svc = GitOpsService(
        RepoErrorFactory(),
        sleep=lambda _d: None,
        jitter=lambda: 1.0,
    )
    assert svc._backoff_delay(1) == pytest.approx(BASE_DELAY_S * (1 + JITTER_RATIO))
    for attempt in range(1, 10):
        assert 0 <= svc._backoff_delay(attempt) <= DELAY_CAP_S


# ── run_remote_git ─────────────────────────────────────────────────────────


def test_run_remote_git_dispatches_to_repo_subcommand_with_timeout() -> None:
    """The encapsulated entry point that the 6 production call sites use.
    Verifies (a) we look up the named subcommand on `repo.git`, (b) we
    forward positional args verbatim, and (c) we thread `kill_after_timeout`
    so the wedge guard actually engages — none of these are visible from
    the bare `run_remote` API."""
    sleeps: list[float] = []
    svc = _service(sleeps, timeout_s=7.0)
    repo = MagicMock(spec=git.Repo)
    repo.git.fetch.return_value = "ok"

    result = svc.run_remote_git(repo, "fetch", "origin", cwd=_CWD, message="fetch failed")

    assert result == "ok"
    repo.git.fetch.assert_called_once_with("origin", kill_after_timeout=7.0)


def test_run_remote_git_propagates_retry_on_transient() -> None:
    sleeps: list[float] = []
    svc = _service(sleeps)
    repo = MagicMock(spec=git.Repo)
    repo.git.push.side_effect = _git_err("kex_exchange_identification: ...")

    with pytest.raises(RepoError):
        svc.run_remote_git(repo, "push", "origin", cwd=_CWD, message="push failed")
    assert repo.git.push.call_count == svc.MAX_ATTEMPTS


# ── timeout configuration ──────────────────────────────────────────────────


def test_timeout_s_defaults_to_module_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TIMEOUT_ENV_VAR, raising=False)
    svc = GitOpsService(RepoErrorFactory())
    assert svc.timeout_s == OPERATION_TIMEOUT_S


def test_timeout_s_respects_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TIMEOUT_ENV_VAR, "7.5")
    svc = GitOpsService(RepoErrorFactory())
    assert svc.timeout_s == 7.5


def test_timeout_s_ctor_arg_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit timeout_s wins over the env var so test fixtures stay
    deterministic regardless of the surrounding shell."""
    monkeypatch.setenv(TIMEOUT_ENV_VAR, "7.5")
    svc = GitOpsService(RepoErrorFactory(), timeout_s=3.0)
    assert svc.timeout_s == 3.0


def test_timeout_s_invalid_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TIMEOUT_ENV_VAR, "not-a-number")
    svc = GitOpsService(RepoErrorFactory())
    assert svc.timeout_s == OPERATION_TIMEOUT_S


@pytest.mark.parametrize("bad", ["0", "0.0", "-1", "-3.5"])
def test_timeout_s_guards_against_non_positive_env(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    """Footgun guard: WINTER_GIT_TIMEOUT_S=0 (or negative) would set
    `kill_after_timeout=0`, which SIGKILLs the git subprocess before it can
    even start. The property must reject this and fall back to the default
    so a typo / shell-export-gone-wrong doesn't brick `winter ws fetch`."""
    monkeypatch.setenv(TIMEOUT_ENV_VAR, bad)
    svc = GitOpsService(RepoErrorFactory())
    assert svc.timeout_s == OPERATION_TIMEOUT_S


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_timeout_s_guards_against_non_positive_ctor_arg(bad: float) -> None:
    """Same guard as the env-var path: an explicit `timeout_s=0` ctor arg
    also falls back to the default. Avoids a subtle test-fixture footgun."""
    svc = GitOpsService(RepoErrorFactory(), timeout_s=bad)
    assert svc.timeout_s == OPERATION_TIMEOUT_S


def test_timeout_s_is_resolved_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    """Singleton wiring (`container.py`) means the service is constructed
    long before tests / users tweak the env. Lazy resolution lets a late
    `WINTER_GIT_TIMEOUT_S` change take effect without rebuilding the
    container."""
    monkeypatch.delenv(TIMEOUT_ENV_VAR, raising=False)
    svc = GitOpsService(RepoErrorFactory())
    assert svc.timeout_s == OPERATION_TIMEOUT_S
    monkeypatch.setenv(TIMEOUT_ENV_VAR, "1.25")
    assert svc.timeout_s == 1.25


# ── SSH keepalive bootstrap ────────────────────────────────────────────────


def test_ensure_ssh_keepalives_installs_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without GIT_SSH_COMMAND set, the bootstrap installs SSH keepalives so
    a half-dead TCP connection surfaces at the SSH layer in ~90s rather
    than only at the Python-side timeout."""
    monkeypatch.delenv(SSH_ENV_VAR, raising=False)
    ensure_ssh_keepalives()
    assert os.environ[SSH_ENV_VAR] == SSH_KEEPALIVE_OPTIONS


def test_ensure_ssh_keepalives_respects_existing_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """User-set GIT_SSH_COMMAND (e.g. for an identity file) wins — we only
    pave a default, never stomp on intentional config."""
    monkeypatch.setenv(SSH_ENV_VAR, "ssh -i /custom/id_ed25519")
    ensure_ssh_keepalives()
    assert os.environ[SSH_ENV_VAR] == "ssh -i /custom/id_ed25519"


def test_constructor_does_not_mutate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per project DI conventions, the injected service shouldn't have
    hidden global side effects in its constructor. The SSH bootstrap lives
    in `ensure_ssh_keepalives()`, called once from `cli.py`."""
    monkeypatch.delenv(SSH_ENV_VAR, raising=False)
    GitOpsService(RepoErrorFactory())
    assert SSH_ENV_VAR not in os.environ


# ── executor ───────────────────────────────────────────────────────────────


def test_executor_uses_parallelism_constant() -> None:
    svc = GitOpsService(RepoErrorFactory())
    with svc.executor() as pool:
        assert pool._max_workers == GitOpsService.PARALLELISM


def test_executor_carries_the_submitters_context_into_tasks() -> None:
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("git_ops_executor_marker", default="unset")
    marker.set("from-submitter")
    svc = GitOpsService(RepoErrorFactory())
    with svc.executor() as pool:
        assert pool.submit(marker.get).result() == "from-submitter"


# ── hung-remote integration (#30) ──────────────────────────────────────────


@pytest.mark.skipif(sys.platform != "linux", reason="ps --ppid + /proc semantics required")
@pytest.mark.skipif(shutil.which("pgrep") is None, reason="pgrep required to verify subprocess cleanup")
def test_run_remote_git_times_out_and_cleans_up_subprocess_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end check for the wedge bug in issue #30.

    Spins up a fake `ssh` shim that sleeps indefinitely and points
    `GIT_SSH_COMMAND` at it. A real `git fetch` against an ssh:// remote
    will then hang forever unless `kill_after_timeout` kicks in. Asserts:
    (a) `run_remote_git` raises a typed `RepoError` within a bound derived
    from the configured timeout (not 'forever'), and (b) no orphan shim
    processes are left behind once the call returns — the orphan-ssh
    accumulation the bug report observed.
    """
    repo_path = tmp_path / "test-repo"
    repo_path.mkdir()
    git.Repo.init(repo_path)
    r = git.Repo(str(repo_path))
    r.git.remote("add", "origin", "ssh://fakehost.invalid/repo.git")

    # uuid (not pid+ms) so a pytest-xdist worker run can't collide with
    # itself or a sibling worker on a busy machine.
    marker = f"winter-test-shim-{uuid.uuid4().hex[:12]}"
    shim = tmp_path / f"{marker}.sh"
    shim.write_text(f"#!/bin/sh\n# {marker}\nexec sleep 600\n")
    shim.chmod(0o755)

    monkeypatch.setenv(SSH_ENV_VAR, str(shim))

    per_call_timeout = 2.0
    svc = GitOpsService(
        RepoErrorFactory(),
        sleep=lambda _d: None,  # skip retry backoff to keep the test fast
        jitter=lambda: 0.0,
        timeout_s=per_call_timeout,
    )

    start = time.monotonic()
    with pytest.raises(RepoError) as ei:
        svc.run_remote_git(r, "fetch", "origin", cwd=repo_path, message="fetch failed")
    elapsed = time.monotonic() - start

    # Derived from service constants so a change to MAX_ATTEMPTS or the
    # per-call timeout doesn't require updating a magic number. The
    # generous multiplier covers process-spawn overhead on loaded CI.
    elapsed_ceiling = (svc.MAX_ATTEMPTS * per_call_timeout * 3) + 5
    assert elapsed < elapsed_ceiling, f"run_remote_git took {elapsed:.1f}s (ceiling {elapsed_ceiling:.1f}s)"
    assert ei.value.subcommand == "fetch"
    # The wedge raises *some* transient error — either GitPython's timeout
    # wording or a SIGKILL exit (status < 0). Both routes through
    # is_transient_git_error are valid; we don't pin to the wording.
    assert ei.value.exit_code is None or ei.value.exit_code < 0 or "Timeout" in (ei.value.stderr or "")

    # Verify no shim processes outlived the call. ps's child walk (which
    # GitPython uses internally to reap the SSH descendant) should have
    # killed our shim — this is the assertion that maps directly to the
    # bug report's "14 orphan ssh processes".
    deadline = time.monotonic() + 2.0
    result = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    while result.returncode != 1 and time.monotonic() < deadline:
        time.sleep(0.1)
        result = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    assert result.returncode == 1, f"orphan shim processes still running: {result.stdout!r}"
