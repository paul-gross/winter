from __future__ import annotations

import contextlib
import logging
import os
import random
import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TypeVar

import git

from winter_cli.core.context_thread_pool import ContextThreadPoolExecutor
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory, unwrap_gitpython_stream

logger = logging.getLogger(__name__)


# Codeberg.org (and most SSH-based git hosts) throttle simultaneous SSH
# connections per source IP. Empirically the cap is around 5; staying at 4
# keeps a comfortable margin while still parallelizing 4x over serial git ops.
PARALLELISM: int = 4

# Retry policy for transient network errors. Gentle defaults — these absorb
# transient SSH-cap collisions, not sustained outages.
MAX_ATTEMPTS: int = 3
BASE_DELAY_S: float = 1.0
DELAY_CAP_S: float = 8.0
JITTER_RATIO: float = 0.5  # ±50% jitter

# Per-call timeout for any single remote git invocation. Sized as a middle
# ground between snapping shut on small Codeberg fetches and tolerating a
# sizeable push over a slow link — a healthy operation completes in well
# under a second, while the bug in #30 ran for hours when wedged. Override
# via WINTER_GIT_TIMEOUT_S for unusually large pushes / slow networks.
OPERATION_TIMEOUT_S: float = 40.0
TIMEOUT_ENV_VAR: str = "WINTER_GIT_TIMEOUT_S"

_TRANSIENT_STDERR_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"Connection closed by .* port 22",
        r"fatal: the remote end hung up unexpectedly",
        r"kex_exchange_identification",
        r"Connection timed out",
        # Wording set by GitPython when `kill_after_timeout` fires
        # ('Timeout: the command "..." did not complete in N secs.', see
        # `git/cmd.py`). Combined with the signal-based fallback in
        # `is_transient_git_error` so a future GitPython rewording doesn't
        # silently drop the retry path.
        r"Timeout: the command .* did not complete",
    )
)


T = TypeVar("T")


def is_transient_git_error(exc: git.GitCommandError) -> bool:
    """Whether `exc` represents a known transient SSH/network failure.

    Signals (high confidence first):
      1. Negative `exc.status` — POSIX `Popen` reports a signal-induced exit
         as `-signum`. In our use of GitPython the only thing sending
         SIGKILL to a `git` subprocess is `kill_after_timeout`, so a
         negative status is a structural signal that the per-call timeout
         fired. This catches the case where GitPython's stderr wording
         drifts across versions.
      2. A known transient stderr pattern (SSH connection drops,
         GitPython's kill_after_timeout watchdog message).

    Auth failures, ref refusals, and divergence are deliberately excluded
    so they fail fast on the first attempt.
    """
    status = getattr(exc, "status", None)
    if isinstance(status, int) and status < 0:
        return True
    stderr = exc.stderr if isinstance(exc.stderr, str) else ""
    return any(pat.search(stderr) for pat in _TRANSIENT_STDERR_PATTERNS)


class GitOpsService:
    """Centralized service for network-touching git operations.

    Owns the thread pool for parallel git work (via `executor()`), the retry
    policy for transient SSH errors (via `run_remote()` / `run_remote_git()`),
    and the per-call timeout that prevents wedged subprocesses from hanging
    the CLI forever (see `timeout_s`). Local git ops stay as direct
    `r.git.<verb>` calls in the repositories — they don't fail transiently
    and gain nothing from going through a service.

    The SSH-layer half-dead-connection guard (`GIT_SSH_COMMAND` keepalives)
    is installed at process startup by `ensure_ssh_keepalives()` rather than
    from this constructor, so the service stays free of hidden global side
    effects and DI-friendly.
    """

    PARALLELISM: int = PARALLELISM
    MAX_ATTEMPTS: int = MAX_ATTEMPTS

    def __init__(
        self,
        error_factory: RepoErrorFactory,
        *,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self._error_factory = error_factory
        self._sleep = sleep
        self._jitter = jitter or (lambda: random.uniform(-1.0, 1.0))
        # `None` means "fall back to env var / module default at access
        # time" — see the `timeout_s` property. Resolving lazily keeps a
        # late env-var change (e.g. a test that sets WINTER_GIT_TIMEOUT_S
        # after the DI singleton resolves) effective.
        self._timeout_override = timeout_s

    @property
    def timeout_s(self) -> float:
        """Per-call timeout in seconds. Resolved lazily on every access.

        Precedence: explicit constructor arg → `WINTER_GIT_TIMEOUT_S` env
        var → `OPERATION_TIMEOUT_S` module default. An invalid value (not
        parseable as float, or ≤ 0) falls back to the default with a
        warning rather than crashing the CLI — config noise shouldn't make
        `winter ws fetch` unusable, and a 0 / negative timeout would
        SIGKILL every subprocess before it started.
        """
        if self._timeout_override is not None:
            if self._timeout_override > 0:
                return self._timeout_override
            return self._guarded_default(self._timeout_override)
        raw = os.environ.get(TIMEOUT_ENV_VAR)
        if raw is None:
            return OPERATION_TIMEOUT_S
        try:
            parsed = float(raw)
        except ValueError:
            logger.warning(
                "ignoring invalid %s=%r (expected float seconds); using default %.0fs",
                TIMEOUT_ENV_VAR,
                raw,
                OPERATION_TIMEOUT_S,
            )
            return OPERATION_TIMEOUT_S
        if parsed <= 0:
            return self._guarded_default(parsed)
        return parsed

    @staticmethod
    def _guarded_default(bad_value: float) -> float:
        """Footgun guard: 0 or negative timeouts would SIGKILL every git
        subprocess before it could complete. Warn loudly and fall back."""
        logger.warning(
            "ignoring non-positive timeout %r (would kill every git subprocess immediately); using default %.0fs",
            bad_value,
            OPERATION_TIMEOUT_S,
        )
        return OPERATION_TIMEOUT_S

    @contextlib.contextmanager
    def executor(self) -> Iterator[ContextThreadPoolExecutor]:
        """Thread pool capped at PARALLELISM for fan-out of git operations."""
        with ContextThreadPoolExecutor(max_workers=self.PARALLELISM) as pool:
            yield pool

    def run_remote_git(
        self,
        repo: git.Repo,
        subcommand: str,
        *args: str,
        cwd: Path | str,
        message: str,
    ) -> str:
        """Run `repo.git.<subcommand>(*args)` with bounded retry on transient
        errors and a per-call timeout that SIGKILLs the subprocess on hang.

        This is the standard entry point for the six remote git operations
        in `WriteRepoRepository`. It encapsulates the `kill_after_timeout`
        plumbing inside the service so call sites read as `run_remote_git(r,
        "fetch", "origin", ...)` rather than leaking GitPython's
        kwarg name into every caller. Use `run_remote` directly only when
        the op can't be expressed as a single git subcommand invocation.
        """
        method = getattr(repo.git, subcommand)
        return self.run_remote(
            lambda timeout_s: method(*args, kill_after_timeout=timeout_s),
            cwd=cwd,
            message=message,
        )

    def run_remote(
        self,
        op: Callable[[float], T],
        *,
        cwd: Path | str,
        message: str,
    ) -> T:
        """Run a network-touching git op with bounded retry on transient
        errors and a per-call timeout.

        Prefer `run_remote_git` for the common case of a single git
        subcommand — this method exists for compound ops that can't be
        expressed that way and for white-box retry testing.

        Caller passes the actual git call as a callable that receives the
        resolved timeout —
        `lambda timeout_s: r.git.fetch("origin", kill_after_timeout=timeout_s)`.
        Threading the timeout through `kill_after_timeout` is what makes a
        wedged `git fetch` raise instead of hanging forever; without it,
        `Popen.communicate()` blocks indefinitely and this retry loop is
        unreachable.

        Retries up to `MAX_ATTEMPTS` times when the failure is classified
        transient by `is_transient_git_error`; between attempts, sleeps a
        jittered exponential backoff capped at `DELAY_CAP_S`. Retries are
        silent — the caller observes one logical outcome, not per-attempt
        status. Non-transient failures (auth, missing repo, refused ref)
        raise after the first attempt.
        """
        last_exc: git.GitCommandError | None = None
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            try:
                return op(self.timeout_s)
            except git.GitCommandError as exc:
                last_exc = exc
                if attempt >= self.MAX_ATTEMPTS or not is_transient_git_error(exc):
                    break
                delay = self._backoff_delay(attempt)
                logger.warning(
                    "transient git error (attempt %d/%d): %s — retrying in %.2fs",
                    attempt,
                    self.MAX_ATTEMPTS,
                    unwrap_gitpython_stream(exc.stderr).strip() if isinstance(exc.stderr, str) else "",
                    delay,
                )
                self._sleep(delay)
        assert last_exc is not None  # loop entered ≥ once, only exits on success/break
        raise self._error_factory.from_git(last_exc, message=message, cwd=cwd) from last_exc

    def _backoff_delay(self, attempt: int) -> float:
        """Jittered exponential backoff: base*2^(attempt-1), capped, ±JITTER_RATIO."""
        base = min(BASE_DELAY_S * (2 ** (attempt - 1)), DELAY_CAP_S)
        delay = base + base * JITTER_RATIO * self._jitter()
        return max(0.0, min(delay, DELAY_CAP_S))
