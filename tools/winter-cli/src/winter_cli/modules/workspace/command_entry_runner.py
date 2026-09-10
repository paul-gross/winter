"""The execution seam ``EnvBandResolverService`` depends on to run a command entry.

A single-consumer Protocol, so it lives at this feature's root rather than in
``core/`` (`winter-context:/architecture/module-layout.md`), and is its own
seam rather than a widening of ``core/subprocess_runner.py``'s
``ISubprocessRunner``: that Protocol's ``run`` never raises and carries no
timeout or ``shell`` option — the opposite of what a command-band entry needs
(`winter-context:/architecture/subprocess.md`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping


class ICommandEntryRunner(Protocol):
    """Runs one already-rendered command-band entry and returns its captured stdout.

    Fails loud: raises ``RepoError`` on a non-zero exit, a timeout, or an
    `OSError` (e.g. the program is not found) — it never returns a sentinel
    for failure. It knows nothing of ``${...}`` templates, ``format``, the
    ``WINTER_*`` refusal, ``exports``, or the ``_``-prefixed anonymous-slot
    rule — those all sit above this seam, in ``EnvBandResolverService``.
    """

    def run(
        self,
        command: str,
        *,
        shell: bool,
        env: Mapping[str, str],
        description: str,
        declared_command: str | None = None,
    ) -> str:
        """Execute *command* (its ``${...}`` tokens already rendered) and return captured stdout.

        Runs with cwd at the workspace root. *env* is merged over the
        adapter's own process environment (so the program can still be found
        on `PATH`) and is otherwise exactly the accumulated scope the caller
        resolved. Tokenized via `shlex.split` unless *shell* is true, in which
        case *command* runs through a shell instead. A tokenizing failure
        (e.g. unbalanced quotes) is wrapped through the same ``RepoError``
        path as every other command failure — it never escapes as a raw
        ``ValueError``.

        *description* identifies the entry in a raised ``RepoError``'s
        message (e.g. ``"env.feature.vars key 'DB_PASSWORD'"``) — this seam
        knows nothing about band labels or entry keys itself.

        *declared_command* is the entry's **unrendered** ``command`` field —
        the ``${...}`` tokens as the operator wrote them, before substitution.
        Every operator-facing message and structured error field this seam
        produces names *declared_command*, never *command*: *command* may
        carry another entry's command-derived value substituted into it (e.g.
        a token filled in from a prior entry's output), and echoing that back
        on failure would leak it to stderr and the error log. Defaults to
        *command* itself when omitted — the two coincide whenever *command*
        has no such substitution.
        """
        ...
