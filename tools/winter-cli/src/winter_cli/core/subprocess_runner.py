from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SubprocessResult:
    """Outcome of a one-shot subprocess call.

    `stdout` and `stderr` are captured as strings; the runner is responsible
    for decoding, and decodes leniently — an undecodable byte becomes U+FFFD
    rather than raising, so a command that emits non-UTF-8 output yields a
    result like any other. `returncode` is the OS exit code (0 = success). The
    runner never raises on non-zero exit — callers inspect `returncode`.
    """

    returncode: int
    stdout: str
    stderr: str


class IStreamingProcess(Protocol):
    """A line-streaming subprocess handle returned by `ISubprocessRunner.popen`.

    Callers iterate `stdout_lines` to consume output as it arrives, then call
    `wait()` to get the exit code. Designed for the init/destroy hook flow
    where we want to surface command output line-by-line through a reporter
    while the subprocess is still running.
    """

    @property
    def stdout_lines(self) -> Iterator[str]: ...

    def wait(self) -> int: ...


class ISubprocessRunner(Protocol):
    """Process-execution seam.

    Three shapes:
      - `run(...)` — fire and forget, get captured output back. Used for short
        git/system probes (`git status --porcelain`, etc.).
      - `popen(...)` — start a long-running process and stream its merged
        stdout line by line. Used for setup commands and extension hooks.
      - `call(...)` — run a process with stdin/stdout/stderr inherited from the
        parent and return only its exit code. Nothing is captured or
        re-rendered, so the child's output reaches the terminal unmodified.
        Used by `winter service` to dispatch to an orchestrator entrypoint and
        pass its output and exit code straight through.
    """

    def run(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> SubprocessResult: ...

    def call(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> int: ...

    def popen(
        self,
        cmd: list[str] | str,
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        shell: bool = False,
        merge_stderr: bool = True,
    ) -> AbstractContextManager[IStreamingProcess]: ...
