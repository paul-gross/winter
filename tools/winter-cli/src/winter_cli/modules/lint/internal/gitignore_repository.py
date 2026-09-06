from __future__ import annotations

import logging
from pathlib import Path

from winter_cli.core.subprocess_runner import ISubprocessRunner
from winter_cli.modules.lint.gitignore_repository import IGitIgnoreRepository

logger = logging.getLogger(__name__)

# Batched per scope root rather than one `check-ignore` call per candidate, but
# still chunked: a single call whose argv holds every candidate risks the OS
# `ARG_MAX` ceiling (observed failure around ~21,500 args) on a large enough
# vendored tree, and `LocalSubprocessRunner` turns that `OSError` into
# `returncode=-1` — indistinguishable from "git can't be invoked" once it
# happens. Chunking keeps each call comfortably under the ceiling while still
# costing a handful of calls, not one per file.
_BATCH_SIZE = 500

_ESCAPES = {"a": 0x07, "b": 0x08, "t": 0x09, "n": 0x0A, "v": 0x0B, "f": 0x0C, "r": 0x0D, '"': 0x22, "\\": 0x5C}


def _unquote_c_style(quoted: str) -> str | None:
    """Reverse git's C-quoting of a `check-ignore` output line, or `None` if it can't be parsed.

    With `core.quotePath=false`, git still C-quotes a path containing a
    literal `"`, backslash, or other byte a shell-unsafe context can't carry
    verbatim — each such byte becomes `\\NNN` (octal) or a named escape
    (`\\"`, `\\\\`, `\\t`, ...). A line this can't confidently reverse returns
    `None` rather than a guess, so the caller leaves the corresponding
    candidate unmatched (kept) instead of mismatching it to some other file.
    """
    if len(quoted) < 2 or not quoted.startswith('"') or not quoted.endswith('"'):
        return None
    inner = quoted[1:-1]
    out = bytearray()
    i = 0
    n = len(inner)
    while i < n:
        c = inner[i]
        if c != "\\":
            out.extend(c.encode("utf-8"))
            i += 1
            continue
        if i + 1 >= n:
            return None
        nxt = inner[i + 1]
        if nxt in _ESCAPES:
            out.append(_ESCAPES[nxt])
            i += 2
            continue
        digits = ""
        j = i + 1
        while j < n and len(digits) < 3 and inner[j] in "01234567":
            digits += inner[j]
            j += 1
        if not digits:
            return None
        out.append(int(digits, 8) & 0xFF)
        i = j
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


class GitIgnoreRepository:
    """Resolves gitignore-matched candidates via `git check-ignore`. All git usage for this is confined here.

    `filter_ignored` anchors on the scope root's *own* repo, never an
    enclosing one: `git check-ignore` resolves which repo owns a path by
    walking up from `cwd`, so calling it with `cwd=scope_root` and trusting
    the result would silently pick up an ignore rule from whatever repo
    happens to sit above `scope_root` — including one that ignores
    `scope_root` itself wholesale, which would filter away every candidate.
    `filter_ignored` verifies `scope_root` is that repo's own toplevel before
    trusting anything it says.
    """

    def __init__(self, subprocess_runner: ISubprocessRunner) -> None:
        self._subprocess = subprocess_runner

    def filter_ignored(self, scope_root: Path, candidates: list[Path]) -> list[Path]:
        """Drop candidates the repo rooted at `scope_root` already gitignores.

        Falls back to `candidates` unchanged — no filtering — in every case
        where the result can't be trusted:

        * `scope_root` isn't inside a git repository at all, git refuses it
          on dubious ownership, or git can't be invoked (`ISubprocessRunner`
          reports these as non-zero / `-1` from `rev-parse --show-toplevel`).
        * `scope_root` resolves to a repo, but not to *its own* toplevel — an
          enclosing repo owns it instead, so its ignore rules aren't
          `scope_root`'s to trust.

        Otherwise resolves ignored paths in batches of `_BATCH_SIZE` (never
        one subprocess call per file) with `core.quotePath` disabled so a
        non-ASCII path comes back verbatim rather than C-quoted. A path git
        still quotes (a literal `"`, backslash, or control byte) is decoded
        via `_unquote_c_style`; a line that can't be decoded is dropped from
        consideration rather than guessed at, leaving its candidate in scope.
        A filename that isn't valid UTF-8 at all reaches us with U+FFFD in
        place of the offending bytes (`ISubprocessRunner` decodes leniently
        so a vendored tree's odd filename can't abort the run), which matches
        no candidate — so it too stays in scope.
        A batch whose `check-ignore` call itself fails (non-`0`/`1` exit)
        fails open for that batch only — its candidates stay unfiltered.

        An untracked file that isn't ignored is never in the matched set, so
        it stays in scope; a *tracked* file is never reported by
        `check-ignore` in the first place (git only applies ignore rules to
        untracked paths), so a tracked file always stays in scope regardless
        of whether it matches a pattern.
        """
        if not candidates:
            return candidates
        if not self._owns_root(scope_root):
            logger.debug(
                "%s is not its own git toplevel; leaving %d candidate(s) unfiltered", scope_root, len(candidates)
            )
            return candidates
        ignored = self._resolve_ignored(scope_root, candidates)
        kept = [c for c in candidates if str(c) not in ignored]
        logger.debug(
            "%s: dropped %d of %d candidate(s) as gitignored", scope_root, len(candidates) - len(kept), len(candidates)
        )
        return kept

    def _owns_root(self, scope_root: Path) -> bool:
        result = self._subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=scope_root)
        if result.returncode != 0:
            return False
        toplevel = result.stdout.strip()
        if not toplevel:
            return False
        try:
            return Path(toplevel).resolve() == scope_root.resolve()
        except OSError:
            return False

    def _resolve_ignored(self, scope_root: Path, candidates: list[Path]) -> set[str]:
        ignored: set[str] = set()
        for i in range(0, len(candidates), _BATCH_SIZE):
            batch = candidates[i : i + _BATCH_SIZE]
            result = self._subprocess.run(
                ["git", "-c", "core.quotePath=false", "check-ignore", *(str(c) for c in batch)],
                cwd=scope_root,
            )
            if result.returncode not in (0, 1):
                # This batch's candidates stay unfiltered. WARNING, not DEBUG:
                # the consequence is findings against files the repo told git
                # to disregard, and this is the only place that names why.
                logger.warning(
                    "git check-ignore failed in %s (exit %d): %d candidate(s) left unfiltered: %s",
                    scope_root,
                    result.returncode,
                    len(batch),
                    result.stderr.strip(),
                )
                continue
            for line in result.stdout.splitlines():
                if not line:
                    continue
                if line.startswith('"'):
                    decoded = _unquote_c_style(line)
                    if decoded is None:
                        # Unmappable — leave the corresponding candidate in scope.
                        logger.debug("could not decode check-ignore output line %r in %s", line, scope_root)
                        continue
                    ignored.add(decoded)
                else:
                    ignored.add(line)
        return ignored


def _conforms_git_ignore_repository(x: GitIgnoreRepository) -> IGitIgnoreRepository:
    return x
