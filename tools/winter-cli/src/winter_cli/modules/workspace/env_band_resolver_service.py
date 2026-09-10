"""Fixpoint resolution of env-var band entries into a flat ``{KEY: VALUE}`` scope.

``EnvBandResolverService.resolve()`` is the single place a band entry — a plain
``${...}`` template string or an inline-table command entry (``EnvCommandEntry``)
— turns into a concrete string value.  ``EnvProvisionerService`` keeps the
scope → base-vars → band-selection duty (deciding *which* of the workspace,
feature, and named bands apply to a given scope); this service owns everything
downstream of that selection: precedence, resolution order, execution, and
diagnostics.

Precedence is resolved before iteration
----------------------------------------
A key declared in a higher band shadows the lower band's entry entirely: the
shadowed entry is never rendered and, if it is a command entry, never run.
Precedence, low to high, is workspace < feature < named (the scope's own
``[env.<name>.vars]`` override).  This keeps the documented band layering
intact under a fixpoint — only ever one *effective* entry per key survives to
be resolved.

Resolution is a fixpoint, not an ordered pass
----------------------------------------------
Every effective entry may reference any other effective entry's key via
``${NAME}`` / ``${NAME+N}`` tokens, regardless of which band declared it or in
which order they were declared.  Resolution repeatedly renders every pure
entry, and runs every command entry, whose references are already resolved,
accumulating into the scope, until a full round resolves nothing further. An
entry whose references are not yet resolved is deferred rather than raising
immediately, and is retried next round — so a feature-band entry may observe
a value the per-env band overrides, and a pure entry may reference a key a
command entry contributes.

A command entry's own "references" are only the ``${...}`` tokens inside its
``command`` field — the tokens are rendered into the actual command line
before it runs, using the exact same substitution as a pure entry.  What the
command's *output* eventually contributes is not itself a dependency of
anything until after the command has actually run.

One restriction survives the fixpoint
--------------------------------------
Workspace-band entries — those whose *effective* (post-precedence) origin is
``[env.workspace.vars]`` — see a restricted scope: the base vars minus
``WINTER_PORT_BASE``, plus other workspace-band keys only. They never see
feature- or named-band keys, so a workspace-band entry resolves identically
regardless of whether the scope being computed is ``"workspace"`` or a feature
env. Every other entry sees the full accumulated scope. This is the one
ordering-adjacent constraint the fixpoint keeps; declaration order (within a
band, and across bands) is otherwise irrelevant. A command entry's process
also runs with this same restricted-or-full scope as its environment.

"Other workspace-band keys" is decided from a key's *effective declaring
entry* — whether ``_effective_entries`` resolved that key to a workspace-band
entry — never from whichever source most recently wrote its current value in
``resolved``. A feature- or named-band command's incidental output can
outrank and overwrite a workspace-band literal's value for the same key (see
"declared key vs. command-output key" below); that overwrite changes who
currently holds the key, not which band *declared* it, so the key stays
visible to other workspace-band entries exactly as if the overwrite had never
happened.

"Other" also excludes a name that is itself a base var: a workspace-band
entry may declare a literal sharing a base var's name, but that literal is
not an *other* workspace-band key, it *is* a base var, so every reader —
including a sibling entry in the same band — still sees the base var's
original value, never the colliding literal's own claim (see
``_ResolutionState.visible_scope``). The colliding literal's own final value
in ``resolved`` is unaffected; only what an *other* entry observes when it
reads the name by reference is pinned to the base var.

Whether a name "is a base var" for this exclusion is decided
scope-invariantly (``_ResolutionState._is_base_var_name``), never from
whether *this call's own* ``base_scope`` happens to carry the name.
``WINTER_PORT_BASE`` is the case that forces the distinction: it is a base
var everywhere, but ``EnvProvisionerService.compute`` only ever puts it in
``base_scope`` for a feature scope, never for the workspace scope. Deciding
the exclusion from ``base_scope`` membership alone would therefore answer
differently for the identical config at the two scopes — treating a
workspace-band literal declared under the name ``WINTER_PORT_BASE`` as an
"other workspace-band key" (and so visible to a sibling) at workspace scope,
while correctly excluding it at feature scope — which is exactly the
scope-dependence this restriction exists to rule out. Since
``WINTER_PORT_BASE`` is also the one base var a workspace-band entry may
never observe at all (see above), the correct, scope-invariant answer is
that no workspace-band entry — including one declared under that very name
reading itself — ever observes it by reference, at either scope.

Unresolvable entries
---------------------
When a round makes no progress and entries remain unresolved, resolution
raises ``ValueError`` naming every such entry and the specific reason it is
still waiting, in one of five shapes: a reference no band or command entry
could ever supply (``"undefined variable"`` — including one blocked forever
by the workspace-band restriction above); a reference that genuinely
participates in a reference cycle; a reference that is merely blocked
downstream of one of the other two, whose own line carries the real cause; a
reference that *is* fully resolved but excluded from a restricted entry's
visible scope (resolved elsewhere, but not itself a workspace-band key); and
a pending entry with no missing references at all — a ``shell`` entry the
multi-value ordering below withheld for the whole run, which would otherwise
contribute no line even though it never resolved. Only a reference reachable
from itself is called a cycle, so a reader is never sent hunting for one that
does not exist.

Command entries: the gate, execution, and merge policy
--------------------------------------------------------
``resolve_commands`` gates whether a command entry's output is used at all.
When it is ``False``, every command entry's own **declared** key resolves to
``COMMAND_PLACEHOLDER`` — a module-level constant that is the single source
for both the value injected into a scope's env and the value ``winter env``
prints for a command-derived key it has not run — assigned *before* the
fixpoint loop runs, so no pure entry referencing a *declared* command-entry
key ever blocks waiting on it. For that narrower case — a reference to the
key a command entry is itself declared under — gating can never turn a
computation that would otherwise succeed into a failure.

A ``_``-prefixed declared key is the one exception to that guarantee, in the
opposite direction from the gap below: merge policy (see
``_filtered_command_output``) drops a ``_``-prefixed key unconditionally,
gated or not, so it is never itself claimed into ``resolved`` — a reference
to one can therefore never resolve *even with the gate open*. Masking such a
reference to the placeholder only while gated would make the gate hide a
permanent misconfiguration in exactly the states (``down``, ``status``) where
it should surface; a reference to a ``_``-prefixed key is instead refused —
the same ``ValueError`` naming it as never suppliable — identically whether
``resolve_commands`` is ``False`` or ``True``.

That the *declared-key* guarantee does **not** extend to a key a ``dotenv`` /
``json`` command entry's output would *import* is the gap in the other
direction: such a key is not known until the command actually runs, so it
never appears in ``resolved`` and is never placeholder'd while gated off. A
pure entry referencing one of those (rather than the command entry's own
declared key) sees it as a plain undefined reference and raises under
``resolve_commands=False`` even though the identical config would succeed
under ``True`` — a real, documented gap between the two gate states, not a
hypothetical one.

When ``resolve_commands`` is ``True``, a command entry instead joins the same
fixpoint loop as pure entries. Once its own references resolve, its rendered
``command`` line runs via the injected ``ICommandEntryRunner`` (the sole
constructor argument), with the accumulated scope in its environment. Its
captured stdout is then parsed per its declared ``format`` (``raw`` —
trimmed stdout becomes the *declared* key's own value; ``dotenv`` / ``json`` —
every parsed key is a candidate for merging, independent of the declared key,
which is then just a diagnostic label) and filtered before merging into the
scope: a key that is itself ``_``-prefixed, or that starts with ``WINTER_``,
or that an ``exports`` allow-list excludes, is dropped rather than merged —
silently, the same way a command entry gated off never fails a computation
that would otherwise succeed. Merge policy — the ``WINTER_*`` refusal, the
``exports`` allow-list, and the ``_``-prefix rule — is this service's job;
running the process is the runner's (see ``command_entry_runner.py``).

A non-zero exit, a timeout, or output that fails to parse as the declared
``format`` all raise ``RepoError`` (not ``ValueError``) — command failures do
not follow the best-effort degradation the fixpoint's own diagnostics give
pure entries.

This service tracks which keys were actually merged in from an executed
command's output (``command_derived_keys``). A ``shell = true`` entry may not
reference one: the tokens in a shell command string must be text the operator
wrote, never a value another command produced
(`winter-context:/architecture/subprocess.md`). Referencing one is refused at
resolution time — a ``ValueError`` naming the entry and the reference, raised
before the command ever runs.

That refusal has to be decided the same way regardless of resolution order —
whether a key is command-derived is knowable purely from the band set's
*declared* shape (which keys are the output of a ``format = "dotenv"`` /
``"json"`` entry, not which round any one of them happened to run in). Every
round therefore resolves in two passes: every ``shell = false`` entry first
(pure entries and command entries alike), then every ``shell = true`` entry.
A ``shell`` entry with a ``${...}`` reference of its own is only attempted
once no ``format = "dotenv"`` / ``"json"`` command entry remains pending
*anywhere in the round* — whether or not that pending entry is itself
``shell = true``, and regardless of whether it was actually attempted this
round and merely stayed pending (its own reference not yet ready is not the
same as it being unable to still claim a key later) — since any one of those
could still overwrite a key a ``shell`` entry reads. A multi-value ``shell``
entry excludes only itself from that check, so it is not forced to wait
forever on its own resolution; it still defers behind every *other*
multi-value entry. Until that bar is cleared, a ``shell`` entry with a
reference of its own stays deferred to a later round no matter how ready
that reference already is. A ``shell`` entry with no reference of its own is
exempt from the bar entirely and is always attempted immediately: with no
``${...}`` token in its ``command`` string, there is nothing for the refusal
above to check, so its answer for this entry is already fixed — "never
refused" — no matter which round or position a colliding multi-value entry
resolves in; waiting could not change it. This is narrower than "nothing it
reads is affected by another entry": the entry's process still runs with the
accumulated scope as its own environment (see ``_run_command_entry``), so a
command-derived value already sitting in that scope is visible to it as an
ordinary env var (``$NAME`` under ``shell = true``) — that environment-content
channel is real and load-bearing (`winter-context:/architecture/subprocess.md`),
just outside what *this* refusal polices, which is only the ``${...}``
substitution grammar in the declared ``command`` string. This keeps the
refusal's answer independent of which round or which position within a round
any one multi-value command entry resolves in — see
``EnvBandResolverService._resolve_round``.

A declared key's final value is provenance-based; a mid-fixpoint reader is not
--------------------------------------------------------------------------------
A ``format = "dotenv"`` / ``"json"`` command entry's output keys are not
generally known until the command has actually run, so they cannot be folded
into ``_effective_entries``'s declared-key precedence pass up front. Left
alone, a collision between a key some entry *declares* and a key a *different*
entry's command output *also* emits would resolve by which fixpoint round each
side happened to land in — emergent behavior an unrelated entry could flip by
delaying the command a round, with nothing in the config changing.

Instead, every effective entry carries a **position**: ``(band_rank,
declaration_index)``, band_rank following the same workspace(0) < feature(1)
< named(2) precedence ``_effective_entries`` already applies, and
declaration_index its index within that band's TOML table. Every write to a
key — whether the direct render of a pure entry, a command's own declared key,
or one of the incidental keys a multi-value command's output emits — is
compared against the position of whatever previously claimed that key
(``EnvBandResolverService._ResolutionState.claim``), and the higher position
wins; the loser's write is simply dropped, whichever round it arrives in.
Comparing tuples this way means a higher band always wins outright, and
*within* one band, the entry declared further down wins — exactly the rule
for two entries that declare the same key outright, now extended to a key
only one side declares and the other merely emits. Two different commands'
output keys colliding with each other resolve by the same comparison, using
each command's own declared position: neither side is privileged as "the
declaration", so treating every contributor — literal, a command's own key,
or an incidental output key — as one uniform kind of candidate is the only
rule that stays coherent across all three cases. **This part of the guarantee
is unconditional**: a key's *final* value in ``resolved`` never depends on
resolution order, for any collision shape.

Comparing at the moment of the write settles the key's *final* value, but not
necessarily what an entry resolving *before* that write observes — and here
the guarantee has a real direction to it:

- When the higher-position side is a **pure declared entry** (a literal
  outranking the colliding command), the outcome is fully pre-empted: that
  entry is known from ``_effective_entries`` up front, always resolves, and
  always outranks the command, so the command's losing write for that key is
  dropped before it can ever be seen. ``resolved`` is set for that key
  exactly once. A third entry referencing the key is therefore deferred until
  the declared value lands, never rendering against a value about to be
  replaced.

- When the higher-position side is a **command's incidental output**
  overwriting an *earlier-resolving, lower-position* literal, there is no
  equivalent pre-emption. The literal has no unresolved references of its
  own, so it resolves — and becomes visible to any entry ready to read it —
  as soon as its round comes up, independent of whether the higher-position
  command has run yet. A third entry that reads the key in that window
  renders against the literal's value; the command's later write still wins
  the key's *final* value in ``resolved``, but does not retroactively correct
  a render that already happened. **This is a known, accepted limitation**: a
  config that relies on a command's output overriding an earlier plain
  literal, where a third entry also reads that key before the command runs,
  gets a round-dependent result for that third entry. The mirror-image
  config — the literal declared *after* the command slot, so the literal is
  the pre-empting side — does not have this problem. Closing it in the
  general case would mean not treating any entry as resolved until every
  higher-position candidate for every key it might ever read has also
  resolved, which is a restructuring of the fixpoint itself, not a bug in the
  position comparison.

Only a pure entry pre-empts this way; a command entry that declares the key is
not assumed to produce it, since merge policy may drop its output, and
dropping the other side's write on that assumption could fail a computation
that would otherwise succeed.

A base-scope seed is the same shape of collision, pre-empted the same way
------------------------------------------------------------------------
``base_scope`` — the ``WINTER_*`` vars ``EnvProvisionerService`` computes
before any band entry runs — is itself an occupant of a key, exactly like a
command's incidental output, whenever some effective entry also declares that
same (necessarily ``WINTER_*``) name: a command entry can never be that
declaring entry (``_parse_env_command_entry`` refuses one declared under a
``WINTER_*`` key at load time, and ``_filtered_command_output`` drops any
``WINTER_*`` key a command's output would otherwise import), so the declaring
entry is always a pure literal — the same "**pure declared entry** fully
pre-empts" case above, not the accepted-limitation one. ``resolve()`` gives
every ``base_scope`` key a provenance floor (``_BASE_POSITION``) below every
real position, and withholds a base-scope key any effective entry also
declares from the initial ``resolved`` seed entirely, so a third entry
referencing it is deferred — exactly as it would be for a colliding command's
declared key — until the declaring literal's own claim lands, never rendering
against the stale seed. This is the same "resolved is set for that key
exactly once" guarantee above, extended to cover the one source of a key's
initial value that isn't itself an effective entry.

The withholding above protects a *third* entry from observing the stale seed;
it is not meant to make the declaring entry itself unable to read the value
it is extending. A declared literal's own template may reference the very
name it declares — extending a managed var rather than replacing it outright
(e.g. ``WINTER_SERVICE_PREFIX = "${WINTER_SERVICE_PREFIX}-x"``) — and
``_try_resolve_entry`` exempts exactly that self-read from the withholding: it
sees ``base_scope``'s original value under its own name while resolving
itself, even though a third entry referencing that same name is still
deferred until this entry's own claim lands. Without the exemption, such a
self-reference has no way to ever resolve — its one reference is its own,
still-withheld key — and used to be misdiagnosed as a resolution cycle rather
than named for what it actually was. The one name this exemption never
applies to is ``WINTER_PORT_BASE`` read by a *restricted* (workspace-band)
entry: the workspace-band restriction excludes that name from every
workspace-band entry's view unconditionally (see "one restriction survives
the fixpoint" above), including a workspace-band entry's read of its own
declared name — granting the exemption there would also answer differently
by scope, since ``WINTER_PORT_BASE`` is only ever present in ``base_scope``
(and so in ``base_scope_keys``) at a feature scope.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from winter_cli.config.models import EnvCommandEntry, EnvCommandFormat
from winter_cli.modules.workspace.models import RepoError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from winter_cli.config.models import EnvBandValue
    from winter_cli.modules.workspace.command_entry_runner import ICommandEntryRunner

# Matches ${NAME} or ${NAME+N}: a reference to an in-scope variable, optionally
# plus a non-negative integer offset.  NAME is an env-var-style identifier.
_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:\+(\d+))?\}")
# Matches any ${...} token the reference form did not consume — malformed/unsupported.
_UNKNOWN_TOKEN_RE = re.compile(r"\$\{[^}]*\}")
# A valid env-var key — same identifier shape as a ${...} reference name.
# Shared by both dotenv and json output parsing: an env-var name winter will
# export must be shell/`source`-safe in either format.
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# The one non-scoped var a workspace-band entry may never reference — see the
# module docstring's "one restriction survives the fixpoint" section.
_WINTER_PORT_BASE = "WINTER_PORT_BASE"

COMMAND_PLACEHOLDER: Final[str] = "<unresolved:command>"
"""Value substituted for a command entry's key when ``resolve_commands`` is False.

Single source for both the env map injected into provider subprocesses and the
value ``winter env`` prints for a command-derived key — see the module
docstring.
"""


def _render_env_var_value(
    band: str,
    key: str,
    template: str,
    scope: Mapping[str, str],
    placeholder_keys: frozenset[str] = frozenset(),
) -> str:
    """Resolve ``${NAME}`` / ``${NAME+N}`` references in *template* against *scope*.

    *band* is a label for error messages (e.g. ``"env.workspace.vars"``).
    Callers are expected to have already verified every referenced NAME is
    present in *scope* (see ``_missing_refs``); a reference this function still
    finds undefined is treated as a hard, immediate error — it means the
    fixpoint's own bookkeeping and this function's view of *scope* disagree.

    - ``${NAME}``   → NAME's resolved string value.
    - ``${NAME+N}`` → ``int(NAME) + N`` (NAME must parse as an int; N ≥ 0).

    *placeholder_keys* names the keys standing at ``COMMAND_PLACEHOLDER``
    because their command entry was gated off — a command entry's own
    *declared* key only; a key a ``dotenv`` / ``json`` entry's output would
    otherwise import is not in this set (it is not known until the command
    actually runs), so a reference to *that* still raises undefined below,
    even though the same reference would resolve once the gate opens. For a
    *placeholder* key, an offset applied to it cannot be computed and must
    not be *judged*: the token masks to the placeholder rather than raising,
    because the same template resolves fine once the gate is open — gating
    never fails a computation that would otherwise succeed, for this
    narrower, declared-key case.

    Literal values (no ``${...}`` token) pass through unchanged.  A ``+N``
    offset applied to a non-integer value that is *not* a gated placeholder, or
    any other ``${...}`` token, is a fatal substitution error — raises
    ``ValueError`` with a clear message.
    """

    def _replace(m: re.Match[str]) -> str:
        name, offset = m.group(1), m.group(2)
        if name in placeholder_keys:
            return COMMAND_PLACEHOLDER
        if name not in scope:
            raise ValueError(
                f"{band} key {key!r}: reference to undefined variable {name!r} "
                f"— reference a managed base var or another band entry."
            )
        value = scope[name]
        if offset is None:
            return value
        try:
            return str(int(value) + int(offset))
        except ValueError:
            raise ValueError(
                f"{band} key {key!r}: cannot apply +{offset} to non-integer value of {name!r} ({value!r})."
            ) from None

    rendered = _REF_RE.sub(_replace, template)

    # Any ${...} the reference form left behind is an unsupported token.
    unknown = _UNKNOWN_TOKEN_RE.search(rendered)
    if unknown:
        raise ValueError(
            f"{band} key {key!r}: unsupported substitution token {unknown.group()!r}. "
            f"Use ${{NAME}} or ${{NAME+N}} referencing a managed base var or another band entry."
        )
    return rendered


def _referenced_names(template: str) -> set[str]:
    """Return every ``${NAME...}`` reference name in *template* (offsets ignored)."""
    return {m.group(1) for m in _REF_RE.finditer(template)}


def _ref_template(value: EnvBandValue) -> str:
    """The string whose ``${...}`` tokens are one entry's own dependencies.

    For a command entry this is its ``command`` field — the only place its
    tokens live. What its output eventually contributes is a different
    question (whether *other* entries depend on it), decided only once it has
    actually run.
    """
    return value.command if isinstance(value, EnvCommandEntry) else value


def _missing_refs(template: str, scope: Mapping[str, str], gated: frozenset[str] = frozenset()) -> set[str]:
    """Return the subset of *template*'s referenced names not yet present in *scope*.

    *gated* names a gated command entry's own declared key — such a reference
    always masks to ``COMMAND_PLACEHOLDER`` (see ``_render_env_var_value``),
    so it must never count as missing here either. It deliberately excludes a
    ``_``-prefixed key even though that key, too, is gated off and never
    claimed into *scope*: unlike an ordinary declared key, a ``_``-prefixed
    key is dropped by ``_filtered_command_output`` unconditionally (the
    "never exported itself" rule), so a reference to one can never resolve
    even once the gate opens — masking it here would make the gate hide that
    permanent misconfiguration in exactly the states where it should surface.
    """
    return {name for name in _referenced_names(template) if name not in scope and name not in gated}


def _failure_context(label: str, key: str, declared_command: str) -> str:
    """The ``<band> key '<key>': command `<declared>`` prefix every command failure carries.

    Every command failure mode — a non-zero exit, a timeout, and unparseable
    output — names the entry, the command, and the exit code. The runner
    builds the first two failures' messages from the *description* this
    service hands it (and the entry's *declared*, unrendered command — see
    ``ICommandEntryRunner.run``); a parse failure is raised here instead,
    after the process is gone, so it builds the same prefix from the same
    parts. *declared_command* is always the entry's unrendered ``command``
    field, never the rendered line actually executed: the rendered form may
    carry a value substituted in from another entry's output, and this
    message reaches stderr and the CLI's error output, so it must never be
    able to echo one entry's secret through a different entry's failure.
    """
    return f"{label} key {key!r}: command `{declared_command}`"


def _parse_command_output(fmt: EnvCommandFormat, output: str, context: str, key: str) -> dict[str, str]:
    """Parse a command entry's captured stdout per its declared *fmt*.

    A pure transform over stdlib types — no injected collaborator, no I/O —
    the carve-out ``winter-context:/architecture/service-architecture.md``
    permits for format parsing. Raises ``RepoError`` (not ``ValueError``) when
    *output* does not parse as declared — a command failure, not a fixpoint
    one.

    *context* is the ``<band> key '<key>': command `<rendered>`` prefix built
    by ``_failure_context``: unparseable output is one of the three failure
    modes that must name the entry, the command, and the exit code, and the
    command a parse failure came out of is only knowable here because the
    parse itself succeeds or fails after the process is gone. The exit code
    is ``0`` — the command itself succeeded; only its output did not hold up.
    """
    if fmt is EnvCommandFormat.raw:
        return {key: output.strip()}
    if fmt is EnvCommandFormat.dotenv:
        return _parse_dotenv(output, context)
    return _parse_json(output, context)


def _unquote_dotenv_value(raw_value: str) -> str:
    """Trim surrounding whitespace, then strip at most one matched surrounding quote pair.

    Only a value whose first and last character are the *same* quote
    character is considered quoted; anything else — including a value that
    merely ends in a stray quote character, or is quoted with two different
    characters — passes through unchanged past the whitespace trim. The
    naive ``.strip('"').strip("'")`` this replaces stripped *every* leading
    and trailing quote character unconditionally, silently corrupting a
    secret that happens to end in one.
    """
    stripped = raw_value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ('"', "'"):
        return stripped[1:-1]
    return stripped


def _parse_dotenv(output: str, context: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines; blank lines and ``#``-comments are skipped.

    The failure message below names the line number only, never the offending
    line's own text: for a secrets command, that text can itself be secret
    material, and this ``RepoError`` reaches stderr and CI logs the same way
    every other command failure does. Diagnosis stays possible without it —
    the operator can run the command themselves and inspect line *lineno*.
    """
    result: dict[str, str] = {}
    for lineno, raw_line in enumerate(output.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not _ENV_KEY_RE.match(name):
            raise RepoError(
                f"{context} output line {lineno} is not a valid dotenv KEY=VALUE line",
                exit_code=0,
            )
        result[name] = _unquote_dotenv_value(value)
    return result


def _parse_json(output: str, context: str) -> dict[str, str]:
    """Parse a flat JSON object; every value becomes its string form.

    Every key is checked against the same env-var-identifier shape
    ``_parse_dotenv`` enforces: an unchecked key like ``"a-b"`` or
    ``"FOO BAR"`` would otherwise merge into scope and break any consumer
    that expects every scope key to be a valid shell identifier (e.g.
    ``source <(winter env alpha)``).
    """
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RepoError(f"{context} output is not valid JSON — {exc}", exit_code=0) from exc
    if not isinstance(data, dict):
        raise RepoError(
            f"{context} output must be a flat JSON object, got {type(data).__name__}",
            exit_code=0,
        )
    result: dict[str, str] = {}
    for name, value in data.items():
        if not isinstance(name, str):
            raise RepoError(f"{context} output JSON key {name!r} is not a string", exit_code=0)
        if not _ENV_KEY_RE.match(name):
            raise RepoError(f"{context} output JSON key {name!r} is not a valid env-var identifier", exit_code=0)
        if isinstance(value, dict | list):
            raise RepoError(f"{context} output JSON key {name!r} is not a flat scalar", exit_code=0)
        result[name] = str(value)
    return result


def _filtered_command_output(entry: EnvCommandEntry, parsed: Mapping[str, str]) -> dict[str, str]:
    """Apply merge policy to a command entry's parsed output.

    Drops (silently — never raises) any key that is itself ``_``-prefixed
    (the anonymous-slot rule), any ``WINTER_*`` key (winter's managed vars are
    never overwritten by command output), and — when *entry* declares an
    ``exports`` allow-list — any key outside it.
    """
    allow = entry.exports
    return {
        name: value
        for name, value in parsed.items()
        if not name.startswith("_") and not name.startswith("WINTER_") and (allow is None or name in allow)
    }


def _unclaimed_command_key_reason(name: str, target: EnvCommandEntry) -> str:
    """Why *name* — the declared key of a command entry that already ran — is not in scope.

    A ``dotenv``/``json`` entry's declared key is a diagnostic label only and
    is never itself written; a ``raw`` entry's declared key *is* its one
    output key, so its absence here means merge policy dropped it — the
    ``_``-prefix rule or the entry's own ``exports`` allow-list. Distinguishing
    these three is what ``_raise_unresolved`` needs: the entry that would
    otherwise carry the explanation already ran and left ``pending``, so
    nothing else names the reason (see the module docstring's "Unresolvable
    entries" section).
    """
    if name.startswith("_"):
        return "is `_`-prefixed, an anonymous import slot that is never exported itself"
    if target.exports is not None and name not in target.exports:
        return "is excluded by the command entry's `exports` allow-list"
    if target.format is not EnvCommandFormat.raw:
        return (
            f'is a `format = "{target.format.value}"` command entry\'s declared key, used only as a '
            f"diagnostic label — its own key is never written; reference one of the keys its output "
            f"emits instead"
        )
    return "was not present in the command's own output"


#: ``(band_rank, declaration_index)`` — an effective entry's provenance position.
#: Compared as a tuple: band_rank dominates, declaration_index breaks a tie
#: within one band. See the module docstring's "declared key vs. command-output
#: key" section.
_Position = tuple[int, int]

_BASE_POSITION: Final[_Position] = (-1, -1)
"""The provenance floor every base-scope seed carries — lower than any real
``(band_rank, declaration_index)``, since ``band_rank`` is never negative.
Recorded for every ``base_scope`` key up front so a colliding effective
entry's later claim always compares as strictly higher and is unconditionally
accepted, the same guarantee ``declared_literals`` gives a colliding command's
output — see the module docstring's "declared key vs. command-output key"
section and ``resolve()``."""


@dataclass(frozen=True)
class _EffectiveEntry:
    """One band entry that survived precedence resolution for its key."""

    label: str
    """The winning band's label, e.g. ``"env.feature.vars"`` — used in diagnostics."""

    value: EnvBandValue
    """The template string or ``EnvCommandEntry`` declared for this key."""

    restricted: bool
    """True when this entry's origin is the workspace band — the invariant the
    module docstring's "one restriction survives the fixpoint" section owns."""

    position: _Position
    """This entry's provenance position — see the module docstring."""


@dataclass
class _ResolutionState:
    """Every mutable accumulator threaded through one ``resolve()`` call's fixpoint.

    ``_resolve_round`` and ``_try_resolve_entry`` previously took these as
    separate positional parameters, each mutated in place by both methods (and
    by the write path formerly named ``_claim``) — nothing owned the
    invariants they jointly encode. This class is that single owner: each
    decision reads or writes through one of the methods below instead of
    reaching into a bare dict or set directly.

    ``effective``, ``base_scope``, ``base_scope_keys``, ``declared_literals``,
    and ``gated`` are fixed for the lifetime of one ``resolve()`` call
    (computed once, up front); ``resolved``, ``restricted_resolved``,
    ``origin``, ``key_provenance``, ``restricted_provenance``, and
    ``command_derived_keys`` are the accumulators the fixpoint loop actually
    mutates round over round.
    """

    effective: Mapping[str, _EffectiveEntry]
    base_scope: Mapping[str, str]
    base_scope_keys: frozenset[str]
    declared_literals: Mapping[str, _Position]
    gated: frozenset[str]
    resolved: dict[str, str] = field(default_factory=dict)
    restricted_resolved: dict[str, str] = field(default_factory=dict)
    origin: dict[str, str] = field(default_factory=dict)
    key_provenance: dict[str, _Position] = field(default_factory=dict)
    restricted_provenance: dict[str, _Position] = field(default_factory=dict)
    command_derived_keys: set[str] = field(default_factory=set)

    def visible_scope(self, restricted: bool) -> Mapping[str, str]:
        """Return the scope visible to an entry, honoring the workspace-band restriction.

        A restricted (workspace-origin) entry sees the base vars minus
        ``WINTER_PORT_BASE``, plus other workspace-band keys only. Its base-var
        slice is read from ``base_scope`` — the immutable snapshot handed to
        ``resolve()`` — never from ``resolved``: nothing in
        ``WorkspaceConfigService`` stops a feature- or named-band entry from
        declaring a key that shares a name with a base var (the ``WINTER_*``
        refusal in ``_filtered_command_output`` covers only command *output*),
        and such an entry's write still lands in ``resolved`` like any other
        key it claims. Reading the base-var slice from ``resolved`` would let
        that higher-scope entry's value leak into a workspace-band entry's
        view of a var it never declared and can never see — and, since
        whether it leaks would then depend on whether the higher-scope entry
        had rendered yet, would make a workspace-band entry's own resolved
        value depend on resolution order too. ``base_scope`` is never written
        by the fixpoint, so it stays the same base-var view regardless of what
        any band entry claims or when.

        "Other workspace-band keys" is decided from ``effective`` — whether
        the key's own precedence-resolved entry was declared in the workspace
        band — not from ``origin``, which names whoever most recently *wrote*
        the key's current value. A feature- or named-band command's incidental
        output can outrank and overwrite a workspace-band literal's value for
        the same key (see "declared key vs. command-output key" below); that
        overwrite changes who currently holds the key, not which band
        *declared* it, so a workspace-band key's *value* here is served from
        ``restricted_resolved`` — written only by a restricted-position
        entry's own claim (see ``claim``) — rather than from ``resolved``,
        which the higher-position overwrite also updates. Serving from
        ``resolved`` for a workspace-band key would make that overwrite change
        what a still-pending workspace-band entry observes, even though it
        does not change which band declared the key. Every other entry sees
        the full accumulated scope.

        "Other" also excludes any name that is itself a base-var key, even
        when the workspace band's own effective entry for that name is
        restricted — a workspace-band entry may declare a literal that shares
        a base var's name (nothing stops it), but that literal is not an
        *other* workspace-band key, it *is* a base var, so every entry reading
        it by name — including a sibling in the very same band — must still
        see the base var's original value from ``base_scope``, never the
        colliding literal's own claim. Without this exclusion the literal's
        claim would land in ``restricted_resolved`` like any other write and
        get overlaid here, making a reader's answer depend on whether that
        claim had already happened — order-dependent in exactly the way the
        restriction exists to prevent (see
        ``EnvProvisionerService``'s "as originally computed" guarantee). The
        colliding literal's own position in ``resolved`` — the value the
        returned map ultimately reports for that key — is untouched by this:
        only what an *other* entry sees when it reads the name is restricted
        to the base var's original value.
        """
        if not restricted:
            return self.resolved
        scope = {key: value for key, value in self.base_scope.items() if key != _WINTER_PORT_BASE}
        scope.update(
            (key, value)
            for key, value in self.restricted_resolved.items()
            if key in self.effective and self.effective[key].restricted and not self._is_base_var_name(key)
        )
        return scope

    def _is_base_var_name(self, key: str) -> bool:
        """True when *key* names one of winter's own managed base vars, scope-invariantly.

        ``key in self.base_scope_keys`` alone is scope-*dependent*:
        ``WINTER_PORT_BASE`` is a base-var name everywhere, but
        ``EnvProvisionerService.compute`` only ever puts it in ``base_scope`` (and
        so in ``base_scope_keys``) for a feature scope — the workspace scope never
        emits it at all. Testing ``base_scope_keys`` directly for "is this a base
        var" therefore answers differently for the very same key depending on
        which scope happened to be resolved, which is exactly the kind of
        scope-dependence the workspace-band restriction exists to rule out (see
        the module docstring's "one restriction survives the fixpoint" section).
        Every caller that needs to know whether a name is a base var — not merely
        whether *this* call's own ``base_scope`` happens to carry it — goes
        through this method instead of testing ``base_scope_keys`` directly.
        """
        return key in self.base_scope_keys or key == _WINTER_PORT_BASE

    def reachable_keys(self, restricted: bool) -> set[str]:
        """Return every key an entry with this restriction could ever eventually see.

        Used only for the final unresolvable-entry diagnostic: a missing
        reference outside this set can never be supplied, no matter how many
        more rounds run.
        """
        if not restricted:
            return set(self.base_scope_keys) | set(self.effective.keys())
        workspace_keys = {
            key for key, entry in self.effective.items() if entry.restricted and not self._is_base_var_name(key)
        }
        return set(self.base_scope_keys - {_WINTER_PORT_BASE}) | workspace_keys

    def claim(self, key: str, value: str, position: _Position, label: str, *, restricted: bool) -> bool:
        """Write *value* for *key* only if *position* outranks whoever last claimed it.

        The single provenance-aware write path for every source that can
        produce a key: a pure entry's own render, a command's own declared
        key, and an incidental key a multi-value command's output emits. A key
        with no recorded provenance — a base-scope var no band entry has
        claimed yet — always accepts the first write, matching the layering
        base scope already had before this rule existed. A tie is impossible:
        every effective entry has one distinct position, and a command runs at
        most once, so two writes for the same key never carry equal positions.
        The loser is dropped silently, the same way a filtered-out command
        output key or a gated command entry never fails a computation that
        would otherwise succeed — see the module docstring's "declared key vs.
        command-output key" section.

        *restricted* is the *claiming* entry's own restriction — true exactly
        when the write comes from the entry effective_entries resolved to the
        workspace band (a workspace-band literal's own render, or a
        workspace-band command's own declared key or incidental output key).
        Such a write also lands in ``restricted_resolved``, tracked under its
        own, separate provenance (``restricted_provenance``) so a later,
        higher-position write from a *non*-restricted entry — which still
        wins here in ``resolved`` — cannot overwrite the value a restricted
        entry's own claim established there. See ``visible_scope``.

        Returns whether the write was accepted — the caller uses this to keep
        ``command_derived_keys`` in sync with which source currently holds
        each key, since the shell refusal cares about the key's actual
        current value, not every command that ever tried to produce it.
        """
        existing = self.key_provenance.get(key)
        accepted = existing is None or existing < position
        if accepted:
            self.resolved[key] = value
            self.origin[key] = label
            self.key_provenance[key] = position
        if restricted:
            restricted_existing = self.restricted_provenance.get(key)
            if restricted_existing is None or restricted_existing < position:
                self.restricted_resolved[key] = value
                self.restricted_provenance[key] = position
        return accepted

    def command_derived(self, names: set[str]) -> set[str]:
        """Return the subset of *names* currently claimed by a command's output."""
        return names & self.command_derived_keys

    def note_command_derived(self, key: str) -> None:
        self.command_derived_keys.add(key)

    def forget_command_derived(self, key: str) -> None:
        self.command_derived_keys.discard(key)


class EnvBandResolverService:
    """Resolve a scope's effective env-band entries to a fixpoint.

    Takes the bands, the already-computed base scope, and the command-resolution
    gate as method arguments rather than at construction — it consumes no
    ``WorkspaceConfig`` and has no per-scope state to hold between calls
    (`winter-context:/architecture/dependency-injection.md`). The
    ``ICommandEntryRunner`` seam is the sole constructor argument: everything
    else a command entry needs (merge policy, format parsing, the shell
    refusal) is this service's own job — see the module docstring.
    """

    def __init__(self, runner: ICommandEntryRunner) -> None:
        self._runner = runner

    def resolve(
        self,
        *,
        workspace_band: Mapping[str, EnvBandValue],
        feature_band: Mapping[str, EnvBandValue],
        named_band: Mapping[str, EnvBandValue],
        named_band_label: str,
        base_scope: Mapping[str, str],
        resolve_commands: bool,
    ) -> dict[str, str]:
        """Return *base_scope* overlaid with every band entry's resolved value.

        *workspace_band* / *feature_band* / *named_band* are the bands already
        selected by the caller for this scope (an empty dict for a band that
        does not apply — e.g. workspace scope passes empty feature/named
        bands). *named_band_label* is the diagnostic label for *named_band*
        (e.g. ``"env.alpha.vars"``).

        Raises ``ValueError`` when a template has an unsupported token, a
        non-integer ``+N`` offset, a ``shell = true`` entry references a
        command-derived key, or entries remain unresolved after a round makes
        no further progress. Raises ``RepoError`` when a command entry (run
        because *resolve_commands* is true) exits non-zero, times out, or
        produces output that does not parse as its declared ``format``.
        """
        effective = self._effective_entries(workspace_band, feature_band, named_band, named_band_label)
        declared_literals: dict[str, _Position] = {
            key: entry.position for key, entry in effective.items() if not isinstance(entry.value, EnvCommandEntry)
        }
        placeholder_keys = {
            key for key, entry in effective.items() if isinstance(entry.value, EnvCommandEntry) and not resolve_commands
        }

        # A base-scope key an effective entry also declares (necessarily a pure
        # literal — a command entry can never be declared under one, see
        # `_parse_env_command_entry`'s `WINTER_*` refusal, and a command's
        # *output* can never introduce one either, see
        # `_filtered_command_output`) is withheld from the initial `resolved`
        # seed: seeding it immediately would let a third entry read the stale
        # base-scope value before the declaring literal's own claim lands,
        # exactly the round-dependence "declared key vs. command-output key"
        # already rules out for a colliding command — extended here to cover
        # the base-scope seed itself. Every base-scope key still gets a
        # provenance floor (`_BASE_POSITION`) up front, whether withheld or
        # not, so the declaring entry's later claim is unconditionally
        # accepted regardless of its own position.
        withheld_base_keys = frozenset(base_scope.keys()) & effective.keys()
        state = _ResolutionState(
            effective=effective,
            base_scope=dict(base_scope),
            base_scope_keys=frozenset(base_scope.keys()),
            declared_literals=declared_literals,
            # A `_`-prefixed key is excluded here even though it is itself a
            # gated placeholder key: `_filtered_command_output` drops it
            # unconditionally, so a reference to it can never resolve even
            # once the gate opens (see `_missing_refs`) — masking it only
            # while gated would make the answer depend on the gate.
            gated=frozenset(key for key in placeholder_keys if not key.startswith("_")),
            resolved={key: value for key, value in base_scope.items() if key not in withheld_base_keys},
            key_provenance=dict.fromkeys(base_scope, _BASE_POSITION),
        )

        pending: dict[str, EnvBandValue] = {}
        for key, entry in effective.items():
            if key in placeholder_keys:
                # A `_`-prefixed key is an anonymous import slot — it is never
                # exported itself (see ``_filtered_command_output``), gated or
                # not. Unlike an ordinary gated key, ``key`` is *excluded*
                # from ``state.gated`` (see above), so a reference to it never
                # masks to the placeholder; it is simply never claimed into
                # ``resolved`` for its own sake either.
                if not key.startswith("_"):
                    state.claim(key, COMMAND_PLACEHOLDER, entry.position, entry.label, restricted=entry.restricted)
            else:
                pending[key] = entry.value

        made_progress = True
        while pending and made_progress:
            pending, made_progress = self._resolve_round(pending, state)

        if pending:
            self._raise_unresolved(pending, state)

        return state.resolved

    def _resolve_round(
        self,
        pending: Mapping[str, EnvBandValue],
        state: _ResolutionState,
    ) -> tuple[dict[str, EnvBandValue], bool]:
        """Resolve every ready entry in *pending* once, in two passes.

        Every ``shell = false`` entry (pure or command, any format) is
        attempted first. Every ``shell = true`` entry is attempted second, in
        declaration-position order, and a given one — unless it has no
        ``${...}`` reference of its own, in which case there is nothing for
        the shell refusal to check and its answer is already fixed regardless
        of timing, so it is always attempted immediately — is admitted only
        once every *other* multi-value entry still pending in this round
        has had its turn: a ``format = "dotenv"`` / ``"json"`` entry counts as
        a threat whether it is ``shell = true`` or ``shell = false``, and
        whether it was never attempted this round or was attempted and merely
        stayed pending on its own unresolved reference — either way it can
        still claim a key a ``shell`` entry reads once it resolves in a later
        round (see the module docstring's shell-refusal section). A
        still-*deferred* multi-value ``shell`` entry is excluded from that
        blocking set by *itself* the moment its own turn in the
        position-ordered loop below comes up — successfully resolving or not
        — so two ``shell`` entries can never hold each other back forever; a
        ``shell = false`` multi-value entry that stayed pending after its one
        attempt above is never excluded that way; only its own eventual
        resolution, in some later round, removes it from the set. A ``shell``
        entry with a reference of its own that still misses the bar is
        deferred to a later round regardless of how ready that reference is,
        keeping the refusal independent of resolution order rather than
        dependent on which round or position a colliding command happened to
        resolve in.

        Returns the entries still unresolved after this round, and whether
        the round resolved anything at all (the fixpoint's halt condition).
        """
        made_progress = False
        still_pending: dict[str, EnvBandValue] = {}
        deferred_shell: dict[str, EnvBandValue] = {}

        for key, value in pending.items():
            if isinstance(value, EnvCommandEntry) and value.shell:
                deferred_shell[key] = value
                continue
            if self._try_resolve_entry(key, value, state, still_pending):
                made_progress = True

        # Every multi-value entry still pending after the pass above — shell
        # or not — could still claim a key a shell entry reads once it
        # eventually resolves in a later round; a ``shell = false`` one
        # having already had its one attempt this round does not remove that
        # threat, since an attempt that stayed pending changed nothing. Only
        # a still-*deferred* shell entry is excluded by *itself* below, in
        # position order, once it has had its own turn.
        not_yet_attempted = {key for key, value in deferred_shell.items() if _is_multivalue_command(value)}
        not_yet_attempted |= {key for key, value in still_pending.items() if _is_multivalue_command(value)}

        for key, value in sorted(deferred_shell.items(), key=lambda item: state.effective[item[0]].position):
            not_yet_attempted.discard(key)
            # An entry with no references of its own has no `${...}` token for
            # the shell refusal to check, so the refusal's answer for it can
            # never change no matter what another multi-value entry might
            # still claim — it is never made to wait on the blocking set
            # below. Only a shell entry that actually has a reference of its
            # own is at risk of that answer changing under it.
            has_own_references = bool(_referenced_names(_ref_template(value)))
            if not_yet_attempted and has_own_references:
                still_pending[key] = value
                continue
            if self._try_resolve_entry(key, value, state, still_pending):
                made_progress = True

        return still_pending, made_progress

    def _try_resolve_entry(
        self,
        key: str,
        value: EnvBandValue,
        state: _ResolutionState,
        still_pending: dict[str, EnvBandValue],
    ) -> bool:
        """Attempt one entry; record it into *still_pending* and return False if not yet ready.

        Otherwise renders (a pure entry) or runs (a command entry), merges via
        ``_ResolutionState.claim``'s provenance comparison, and returns True.
        Factored out of ``_resolve_round`` so the same per-entry logic runs in
        both of that method's passes.
        """
        entry = state.effective[key]
        visible = state.visible_scope(entry.restricted)
        if key in state.base_scope_keys and key not in visible and not (entry.restricted and key == _WINTER_PORT_BASE):
            # The declaring entry's own read of the very base-var name it
            # declares is exempt from the withholding below — see the module
            # docstring's "base-scope seed" section. A restricted (workspace-
            # band) entry declared under ``WINTER_PORT_BASE`` itself is the one
            # exception: that name is excluded from *every* workspace-band
            # entry's view, including its own declaring entry's self-read (see
            # "one restriction survives the fixpoint" above) — granting it here
            # would also answer differently by scope, since ``WINTER_PORT_BASE``
            # is only ever a ``base_scope_keys`` member at a feature scope.
            visible = {**visible, key: state.base_scope[key]}
        if _missing_refs(_ref_template(value), visible, state.gated):
            still_pending[key] = value
            return False
        if isinstance(value, EnvCommandEntry):
            merged = self._run_command_entry(key, entry, value, visible, state)
            for name, rendered_value in merged.items():
                losing = state.declared_literals.get(name)
                if losing is not None and losing > entry.position:
                    continue
                accepted = state.claim(name, rendered_value, entry.position, entry.label, restricted=entry.restricted)
                if accepted:
                    state.note_command_derived(name)
        else:
            rendered = _render_env_var_value(entry.label, key, value, visible, state.gated)
            accepted = state.claim(key, rendered, entry.position, entry.label, restricted=entry.restricted)
            if accepted:
                state.forget_command_derived(key)
        return True

    def _run_command_entry(
        self,
        key: str,
        entry: _EffectiveEntry,
        value: EnvCommandEntry,
        visible: Mapping[str, str],
        state: _ResolutionState,
    ) -> dict[str, str]:
        """Render, run, parse, and filter one ready-to-run command entry.

        Returns the ``{key: value}`` pairs to merge into ``resolved`` (already
        passed through merge policy — may be empty, e.g. every produced key
        was filtered out). Raises ``ValueError`` for the shell /
        command-derived-key refusal — a resolution-time policy check, not a
        command failure — before the command ever runs.

        Takes no ``gated`` placeholder set (unlike the pure-entry render path):
        a command entry only ever reaches this method when ``resolve_commands``
        is true, and gating only ever populates a placeholder when it is
        false — the two are mutually exclusive, so no command entry's own
        ``${...}`` tokens can ever need masking here.

        Substitution happens before tokenizing, deliberately: ``${...}``
        tokens in ``value.command`` are rendered into the command line as one
        string first, and only then handed to the runner, which
        ``shlex.split``s that *rendered* string when ``shell`` is false. The
        alternative — splitting the *declared* template first and
        substituting each resulting token in place — would guarantee a
        substituted value always fills exactly one argv slot, whatever
        whitespace or quote characters it contains, at the cost of widening
        ``ICommandEntryRunner`` to accept pre-split argv as well as a shell
        string. That is a larger seam change than this fix justifies: the
        risk it would close is a substituted value re-tokenizing the command
        line into more (or fewer) arguments than the operator wrote, or — if
        it happens to contain an unbalanced quote — failing to tokenize at
        all. The latter fails loud as a ``RepoError`` naming the entry rather
        than escaping as a bare exception, and the former is avoidable in the
        config itself: quoting a substitution in the declared command
        (``vals get "${TOKEN}"``) keeps the runner's `shlex.split` treating it
        as one token even when the rendered value contains whitespace, exactly
        as it would for a literal argument.
        """
        if value.shell:
            blocked = state.command_derived(_referenced_names(value.command))
            if blocked:
                raise ValueError(
                    f"{entry.label} key {key!r}: a shell = true entry may not reference "
                    f"{sorted(blocked)[0]!r}, a command-derived key — interpolating one "
                    f"command's output into another's shell command string is not supported."
                )
        # ``${...}`` substitution happens on the whole command *string*, before
        # the runner tokenizes it (see the docstring above): a substituted
        # value containing whitespace or a quote character can change the
        # resulting argv shape rather than filling exactly one slot.
        rendered_command = _render_env_var_value(entry.label, key, value.command, visible, frozenset())
        output = self._runner.run(
            rendered_command,
            shell=value.shell,
            env=visible,
            description=f"{entry.label} key {key!r}",
            declared_command=value.command,
        )
        parsed = _parse_command_output(value.format, output, _failure_context(entry.label, key, value.command), key)
        return _filtered_command_output(value, parsed)

    @staticmethod
    def _effective_entries(
        workspace_band: Mapping[str, EnvBandValue],
        feature_band: Mapping[str, EnvBandValue],
        named_band: Mapping[str, EnvBandValue],
        named_band_label: str,
    ) -> dict[str, _EffectiveEntry]:
        """Merge the three bands by precedence: workspace < feature < named.

        A key declared in more than one band keeps only the highest-precedence
        entry — the lower one is dropped entirely (never rendered, never run).
        """
        effective: dict[str, _EffectiveEntry] = {}
        for band_rank, (label, band, restricted) in enumerate(
            (
                ("env.workspace.vars", workspace_band, True),
                ("env.feature.vars", feature_band, False),
                (named_band_label, named_band, False),
            )
        ):
            for declaration_index, (key, value) in enumerate(band.items()):
                effective[key] = _EffectiveEntry(
                    label=label,
                    value=value,
                    restricted=restricted,
                    position=(band_rank, declaration_index),
                )
        return effective

    @staticmethod
    def _keys_in_cycles(pending: Mapping[str, EnvBandValue]) -> set[str]:
        """Return the pending keys that actually participate in a reference cycle.

        Edges run key → each of its references that is itself still pending; a
        key is in a cycle exactly when it is reachable from itself.  A key that
        is merely *downstream* of an unresolvable one (``A -> B -> UNDEFINED``)
        is reachable from nothing that returns to it and so is not reported as
        cyclic — the distinction the diagnostic owes its reader, who would
        otherwise hunt for a cycle that does not exist.
        """
        edges = {key: _referenced_names(_ref_template(value)) & pending.keys() for key, value in pending.items()}

        def _reaches_itself(start: str) -> bool:
            seen: set[str] = set()
            stack = list(edges[start])
            while stack:
                node = stack.pop()
                if node == start:
                    return True
                if node in seen:
                    continue
                seen.add(node)
                stack.extend(edges[node])
            return False

        return {key for key in edges if _reaches_itself(key)}

    def _raise_unresolved(
        self,
        pending: Mapping[str, EnvBandValue],
        state: _ResolutionState,
    ) -> None:
        """Build and raise the final diagnostic for entries still unresolved at halt.

        Every missing reference is classified as "undefined" (outside the
        entry's reachable-key universe — nothing could ever supply it,
        including one permanently excluded by the workspace-band restriction),
        "unclaimed" (a command entry that already ran but never claimed this
        key — its own declared key is a diagnostic label only, or merge policy
        dropped it; see ``_unclaimed_command_key_reason``), "cycle" (a
        declared key that is reachable from itself), "blocked" (a declared key
        still in ``pending``, unresolved for a reason its own line reports),
        or "resolved but not visible" (fully resolved, but excluded from a
        restricted entry's scope because it was never itself a workspace-band
        key). Reachability is checked *before* "unclaimed": a restricted
        (workspace-band) entry referencing a key only a feature- or named-band
        command entry declares is blocked by the workspace-band restriction
        itself, regardless of whether that command already ran and produced
        the key — reporting it as "unclaimed" there would blame a missing
        output the command in fact produced, when the real cause is that a
        restricted entry can never see it. The "unclaimed" case is otherwise
        checked before "blocked" precisely because such an entry is *not* in
        ``pending`` any more — it already ran — so it never gets a line of its
        own, and reporting it as "blocked ... on its own line" would point the
        reader at a line that does not exist. "Resolved but not visible"
        exists for the same reason: a name that is not pending has no line
        coming, whether it never resolved at all is beside the point.

        A pending entry with no missing references at all — a ``shell`` entry
        the multi-value ordering in ``_resolve_round`` withheld for the whole
        run — gets one line of its own explaining why, rather than silently
        contributing nothing to the message.
        """
        cyclic = self._keys_in_cycles(pending)
        lines: list[str] = []
        for key in sorted(pending):
            entry = state.effective[key]
            visible = state.visible_scope(entry.restricted)
            missing = sorted(_missing_refs(_ref_template(pending[key]), visible, state.gated))
            if not missing:
                lines.append(
                    f"{entry.label} key {key!r}: a shell = true entry withheld for the whole run — its own "
                    f'references are satisfied, but a format = "dotenv"/"json" command entry elsewhere '
                    f"in the band set never resolved, so the shell refusal could never be decided."
                )
                continue
            reachable = state.reachable_keys(entry.restricted)
            for name in missing:
                unclaimed = state.effective.get(name)
                if name not in reachable:
                    lines.append(
                        f"{entry.label} key {key!r}: reference to undefined variable {name!r} "
                        f"— reference a managed base var or another band entry."
                    )
                elif unclaimed is not None and isinstance(unclaimed.value, EnvCommandEntry) and name not in pending:
                    reason = _unclaimed_command_key_reason(name, unclaimed.value)
                    lines.append(
                        f"{entry.label} key {key!r}: reference to {name!r} can never be supplied — {name!r} {reason}."
                    )
                elif name in cyclic:
                    lines.append(
                        f"{entry.label} key {key!r}: reference to {name!r} is part of a resolution "
                        f"cycle — {name!r} is itself never resolved."
                    )
                elif name in pending:
                    lines.append(
                        f"{entry.label} key {key!r}: reference to {name!r} is blocked — {name!r} is "
                        f"itself unresolved for a reason reported on its own line."
                    )
                else:
                    lines.append(
                        f"{entry.label} key {key!r}: reference to {name!r} is resolved but not visible "
                        f"here — {name!r} is resolved elsewhere, but is not itself a workspace-band key, "
                        f"and this entry only sees other workspace-band keys."
                    )
        raise ValueError(
            "Unable to resolve env-band entr" + ("y" if len(pending) == 1 else "ies") + " after "
            "iteration made no further progress:\n" + "\n".join(lines)
        )


def _is_multivalue_command(value: EnvBandValue) -> bool:
    """True for a ``format = "dotenv"`` / ``"json"`` command entry.

    A multi-value entry's output can contribute *any* key, not just its own
    declared one — see the module docstring's shell-refusal section for why
    that makes every other ``shell`` entry wait on it.
    """
    return isinstance(value, EnvCommandEntry) and value.format is not EnvCommandFormat.raw
