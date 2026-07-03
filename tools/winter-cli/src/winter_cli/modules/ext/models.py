from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class VerifyParams:
    """Parameters for `winter ext verify`.

    `extensions` is one or more names/paths (no glob support — a name/path is
    not a registry enumeration, so each entry is verified literally). At
    least one is required.
    """

    extensions: list[str]
    output_json: bool


@dataclass(frozen=True)
class NewParams:
    """Parameters for `winter ext new`."""

    name: str
    slot: str
    output_dir: Path
    force: bool


@dataclass(frozen=True)
class ScaffoldResult:
    """Result of a successful `winter ext new` scaffolding run."""

    output_dir: Path
    created_files: list[Path]


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one conformance check.

    `check_id` is a short stable identifier for the check (e.g. "accepts-up").
    `passed` is True when the check succeeded.
    `detail` is a human-readable description of what was observed.
    `argv` is the golden invocation argv (the command that was run).
    `observed_exit` is the exit code observed from the subprocess.
    """

    check_id: str
    passed: bool
    detail: str
    argv: list[str]
    observed_exit: int


@dataclass(frozen=True)
class VerifyReport:
    """Aggregated result of all conformance checks for one extension.

    `results` is the ordered list of per-check outcomes.
    `any_failed` is True when at least one check failed.
    `setup_failure` holds a human-readable error string when the extension
    could not be resolved at all (dir missing, no manifest, no entrypoint).
    When `setup_failure` is set no checks are run and `results` is empty.
    """

    results: list[CheckResult] = field(default_factory=list)
    setup_failure: str | None = None

    @property
    def any_failed(self) -> bool:
        if self.setup_failure is not None:
            return True
        return any(not r.passed for r in self.results)
