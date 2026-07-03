from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path


class LintScopeError(Exception):
    """A scope argument couldn't be resolved (unknown name, bad flags, no git repo).

    Raised by `LintScopeResolver`; the command layer translates it into a
    `click.ClickException` so the user sees a clean message and a non-zero exit.
    """


class LintStatus(enum.Enum):
    """Outcome of a single lint check finding."""

    pass_ = "pass"
    warn = "warn"
    fail = "fail"


@dataclass(frozen=True)
class LintFinding:
    """One result emitted by a contributed lint check.

    Parallel to `doctor`'s `ProbeResult`, with `file`/`line` added so a check
    can point at the exact source location of a violation. `source` identifies
    the contributing group — the workspace (`"project"`) or an extension's
    symlink prefix. `check` names the individual check within that source.
    `remediation` is an optional one-line fix hint shown under failures.
    """

    source: str
    check: str
    status: LintStatus
    message: str = ""
    file: str | None = None
    line: int | None = None
    remediation: str | None = None


@dataclass(frozen=True)
class LintCheckOutcome:
    """Everything one contributing lint script produced in a single run.

    Tracked per-source (not flattened) so the dispatcher can tell "no checks
    were contributed" apart from "checks ran and found nothing" — a script that
    exits clean with no findings still appears here with an empty `findings`.
    """

    source: str
    findings: list[LintFinding]


class LintScopeKind(enum.Enum):
    """Which slice of workspace content a lint run targets.

    `all` is every feature env's project worktrees; `env` is one env's project
    worktrees (named, or the one containing the invocation dir by default);
    `repo` is one project repo's source checkout; `changed` is the dirty /
    un-pushed file set of the repo at the invocation dir.
    """

    all = "all"
    repo = "repo"
    env = "env"
    changed = "changed"


@dataclass(frozen=True)
class LintScopeRequest:
    """The raw scope selection parsed from the CLI, before resolution.

    At most one of `names` (non-empty) / `all` / `changed` is honored; the
    resolver rejects combinations and, when none is set, resolves the default
    scope (the env containing `cwd`, or every env). Each entry in `names` may
    be a literal project-repo/env name or a bare glob (no `<env>/<repo>`
    segment) — a glob is expanded against both repo and env names, and every
    resolved name (literal or matched) becomes its own `LintScope` in
    `resolve()`'s returned list. `cwd` is the caller's real invocation
    directory (from `WINTER_INVOCATION_CWD`) — used to detect the current env
    for the default scope and to locate the git repo for the `--changed` set.
    """

    names: list[str] = field(default_factory=list)
    all: bool = False
    changed: bool = False
    cwd: Path | None = None


@dataclass(frozen=True)
class LintScope:
    """A resolved scope — the concrete content a lint run will cover.

    `paths` are absolute roots (a repo dir, an env's worktree dirs, the
    workspace root) or, for the changed set, the individual changed files.
    Checks receive these paths and decide which ones they recognize.
    """

    kind: LintScopeKind
    label: str
    paths: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class LintSummary:
    """Aggregated counts for a completed lint run.

    `contributors` is the number of lint scripts that ran — zero means the
    workspace contributed no checks, which the reporter surfaces explicitly.
    """

    contributors: int
    total: int
    fails: int
    warns: int

    @property
    def exit_code(self) -> int:
        return 1 if self.fails else 0
