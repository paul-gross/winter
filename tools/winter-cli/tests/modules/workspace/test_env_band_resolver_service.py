"""Tests for EnvBandResolverService — fixpoint resolution of env-band entries.

Covers:
- Fixpoint resolution: forward references, references across bands, and
  multi-round chains all resolve regardless of declaration order.
- Band precedence resolved before iteration: a higher band's entry shadows a
  lower band's entry for the same key entirely (the shadowed entry is never
  rendered, even when it would otherwise raise).
- The one restriction that survives the fixpoint: workspace-band entries see
  a restricted scope (base vars minus WINTER_PORT_BASE, plus other
  workspace-band keys only) regardless of what else is being resolved.
- Command entries take the placeholder path when resolve_commands is False,
  and a pure entry may reference a command entry's key without ever being
  deferred.
- Unresolvable-entry diagnostics: distinguishing an undefined reference (one
  nothing could ever supply) from one stuck in a resolution cycle.
- Declared key vs. command-output key collisions: band precedence dominates,
  then declaration order within a band — and the outcome never depends on
  which fixpoint round the command happens to resolve in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeCommandEntryRunner
from winter_cli.config.models import EnvCommandEntry, EnvCommandFormat
from winter_cli.modules.workspace.env_band_resolver_service import (
    COMMAND_PLACEHOLDER,
    EnvBandResolverService,
)
from winter_cli.modules.workspace.internal.repo_error_factory import RepoErrorFactory
from winter_cli.modules.workspace.internal.subprocess_command_entry_runner import SubprocessCommandEntryRunner
from winter_cli.modules.workspace.models import RepoError

FEATURE_BASE_SCOPE = {
    "WINTER_ENV": "alpha",
    "WINTER_ENV_INDEX": "1",
    "WINTER_PORT_BASE": "4020",
    "WINTER_WORKSPACE_PORT_BASE": "4000",
    "WINTER_SERVICE_PREFIX": "proj",
}

WORKSPACE_BASE_SCOPE = {
    "WINTER_ENV": "workspace",
    "WINTER_ENV_INDEX": "0",
    "WINTER_WORKSPACE_PORT_BASE": "4000",
    "WINTER_SERVICE_PREFIX": "proj",
}


def _resolve(
    *,
    workspace_band: dict | None = None,
    feature_band: dict | None = None,
    named_band: dict | None = None,
    named_band_label: str = "env.alpha.vars",
    base_scope: dict | None = None,
    resolve_commands: bool = False,
    runner: FakeCommandEntryRunner | None = None,
) -> dict[str, str]:
    return EnvBandResolverService(runner=runner or FakeCommandEntryRunner()).resolve(
        workspace_band=workspace_band or {},
        feature_band=feature_band or {},
        named_band=named_band or {},
        named_band_label=named_band_label,
        base_scope=base_scope if base_scope is not None else FEATURE_BASE_SCOPE,
        resolve_commands=resolve_commands,
    )


# ---------------------------------------------------------------------------
# Fixpoint resolution
# ---------------------------------------------------------------------------


class TestFixpointResolution:
    def test_forward_reference_within_a_band_resolves(self) -> None:
        """An entry may reference another entry declared later in the same band."""
        result = _resolve(
            feature_band={
                "DATABASE_URL": "postgres://localhost:${WTS_DB_PORT}/db",
                "WTS_DB_PORT": "${WINTER_PORT_BASE+12}",
            }
        )
        assert result["WTS_DB_PORT"] == "4032"
        assert result["DATABASE_URL"] == "postgres://localhost:4032/db"

    def test_named_band_overriding_key_is_observed_by_feature_band(self) -> None:
        """A feature-band entry observes a value the per-env band overrides.

        This is the case single-pass ordered rendering could never satisfy:
        the feature band renders before the named band in the old pipeline, so
        it could never see the override.  Under the fixpoint, band-declaration
        order does not gate visibility.
        """
        result = _resolve(
            feature_band={
                "SECRET_TIER": "dev",
                "TAG": "tier-${SECRET_TIER}",
            },
            named_band={"SECRET_TIER": "qa"},
        )
        # The named band's SECRET_TIER shadows the feature band's entirely
        # (precedence resolved before iteration) — TAG observes "qa", not "dev".
        assert result["SECRET_TIER"] == "qa"
        assert result["TAG"] == "tier-qa"

    def test_multi_round_chain_resolves(self) -> None:
        """A chain of references several entries deep resolves over multiple rounds."""
        result = _resolve(
            feature_band={
                "D": "${C}-d",
                "C": "${B}-c",
                "B": "${A}-b",
                "A": "a",
            }
        )
        assert result["A"] == "a"
        assert result["B"] == "a-b"
        assert result["C"] == "a-b-c"
        assert result["D"] == "a-b-c-d"

    def test_declaration_order_across_bands_is_irrelevant(self) -> None:
        """A workspace-band key is visible to feature/named entries regardless of dict order."""
        result = _resolve(
            workspace_band={"DB_HOST": "db.example.com"},
            feature_band={"DB_URL": "postgres://${DB_HOST}/mydb"},
        )
        assert result["DB_URL"] == "postgres://db.example.com/mydb"


# ---------------------------------------------------------------------------
# Band precedence — resolved before iteration
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_feature_band_shadows_workspace_band_key_entirely(self) -> None:
        """A key in both bands: only the feature-band entry is ever rendered."""
        result = _resolve(
            workspace_band={"COMMON": "from-workspace"},
            feature_band={"COMMON": "from-feature"},
        )
        assert result["COMMON"] == "from-feature"

    def test_shadowed_workspace_entry_is_never_rendered_even_if_it_would_raise(self) -> None:
        """A shadowed lower-band entry is dropped entirely — its own errors never surface."""
        result = _resolve(
            workspace_band={"COMMON": "${NOTHING_SUPPLIES_THIS}"},
            feature_band={"COMMON": "from-feature"},
        )
        assert result["COMMON"] == "from-feature"

    def test_named_band_shadows_feature_and_workspace(self) -> None:
        result = _resolve(
            workspace_band={"COMMON": "from-workspace"},
            feature_band={"COMMON": "from-feature"},
            named_band={"COMMON": "from-named"},
        )
        assert result["COMMON"] == "from-named"

    def test_shadowed_command_entry_never_runs(self) -> None:
        """A literal in a higher band replaces a command entry in a lower one — the command is never run."""
        result = _resolve(
            feature_band={"SECRET_TIER": EnvCommandEntry(command="vals get ref+vault://tier")},
            named_band={"SECRET_TIER": "qa"},
        )
        assert result["SECRET_TIER"] == "qa"


# ---------------------------------------------------------------------------
# Workspace-band invariant — the one restriction that survives the fixpoint
# ---------------------------------------------------------------------------


class TestWorkspaceBandInvariant:
    def test_workspace_entry_cannot_see_winter_port_base_even_at_feature_scope(self) -> None:
        """WINTER_PORT_BASE is in base_scope (feature scope) but never visible to a workspace-band entry."""
        with pytest.raises(ValueError, match=r"undefined variable.*WINTER_PORT_BASE"):
            _resolve(
                workspace_band={"WS_PORT": "${WINTER_PORT_BASE+1}"},
                base_scope=FEATURE_BASE_SCOPE,
            )

    def test_workspace_entry_cannot_see_winter_port_base_at_workspace_scope(self) -> None:
        """Same restriction holds when WINTER_PORT_BASE isn't in base_scope at all."""
        with pytest.raises(ValueError, match=r"undefined variable.*WINTER_PORT_BASE"):
            _resolve(
                workspace_band={"WS_PORT": "${WINTER_PORT_BASE+1}"},
                base_scope=WORKSPACE_BASE_SCOPE,
                feature_band={},
                named_band={},
            )

    def test_workspace_entry_cannot_see_feature_band_key(self) -> None:
        """A workspace-band entry may not reference a feature-band key even though it's in the accumulated scope."""
        with pytest.raises(ValueError, match=r"undefined variable.*DB_HOST"):
            _resolve(
                workspace_band={"WS_URL": "postgres://${DB_HOST}/db"},
                feature_band={"DB_HOST": "db.example.com"},
            )

    def test_workspace_entry_sees_other_workspace_band_keys(self) -> None:
        """Workspace-band entries may still reference each other, in either declaration order."""
        result = _resolve(
            workspace_band={
                "WS_URL": "postgres://${WS_HOST}/db",
                "WS_HOST": "db.example.com",
            },
        )
        assert result["WS_URL"] == "postgres://db.example.com/db"

    def test_workspace_entry_resolves_identically_at_both_scopes(self) -> None:
        """A workspace-band entry gets the same value at workspace and feature scope."""
        ws_result = _resolve(
            workspace_band={"SHARED_PORT": "${WINTER_WORKSPACE_PORT_BASE+1}"},
            base_scope=WORKSPACE_BASE_SCOPE,
        )
        feat_result = _resolve(
            workspace_band={"SHARED_PORT": "${WINTER_WORKSPACE_PORT_BASE+1}"},
            base_scope=FEATURE_BASE_SCOPE,
        )
        assert ws_result["SHARED_PORT"] == feat_result["SHARED_PORT"] == "4001"

    def test_workspace_reader_delayed_across_rounds_resolves_against_its_own_bands_value(self) -> None:
        """A feature-band command's output key outranking a workspace-band literal for the
        same key must not change what a still-pending workspace-band entry resolves
        against: a workspace-band entry resolves identically at workspace scope and at
        feature scope, and at workspace scope no feature band — and so no overwrite —
        exists at all.

        ``WREF`` references both ``K`` (the workspace literal, which ``_f``'s feature-band
        command output later outranks and overwrites *in the returned map*) and ``C`` — a
        chain that only resolves a round after ``K``'s own literal claims it. In that
        window, ``_f`` has already run and taken over ``K``'s write in ``resolved``; ``K``
        is still declared in the workspace band, so ``WREF`` must still resolve against
        the workspace band's own value for it — while the returned map's own ``K`` is
        still the command's value, unaffected.
        """
        workspace_band = {
            "K": "ws-lit",
            "WREF": "${K}-${C}",
            "A": "a",
            "B": "${A}b",
            "C": "${B}c",
        }
        feature_result = _resolve(
            workspace_band=workspace_band,
            feature_band={"_f": EnvCommandEntry(command="vals get k", format=EnvCommandFormat.json)},
            resolve_commands=True,
            runner=FakeCommandEntryRunner({"vals get k": '{"K": "from-command"}'}),
        )
        workspace_result = _resolve(
            workspace_band=workspace_band,
            base_scope=WORKSPACE_BASE_SCOPE,
            resolve_commands=True,
            runner=FakeCommandEntryRunner({}),
        )
        assert feature_result["K"] == "from-command"
        assert feature_result["WREF"] == workspace_result["WREF"] == "ws-lit-abc"

    def test_workspace_chain_delayed_past_the_overwriting_round_still_agrees_across_scopes(self) -> None:
        """The same invariant, exercised through a longer workspace-band chain
        (``W1 -> W2 -> W3``) that only resolves several rounds after a feature-band
        command overwrites one of the keys it references (``SHARED``) in the returned
        map — the delay must not be able to flip which value ``W3`` observes.
        """
        workspace_band = {
            "W3": "${W2}-${SHARED}",
            "W2": "${W1}",
            "W1": "x",
            "SHARED": "ws-literal",
        }
        feature_result = _resolve(
            workspace_band=workspace_band,
            feature_band={"_imp": EnvCommandEntry(command="vals get shared", format=EnvCommandFormat.json)},
            resolve_commands=True,
            runner=FakeCommandEntryRunner({"vals get shared": '{"SHARED": "from-command"}'}),
        )
        workspace_result = _resolve(
            workspace_band=workspace_band,
            base_scope=WORKSPACE_BASE_SCOPE,
            resolve_commands=True,
            runner=FakeCommandEntryRunner({}),
        )
        assert feature_result["SHARED"] == "from-command"
        assert feature_result["W3"] == workspace_result["W3"] == "x-ws-literal"

    def test_workspace_entry_sees_the_base_value_of_a_key_a_feature_band_redeclares_immediate(self) -> None:
        """A feature-band literal sharing a name with a base var (``WINTER_SERVICE_PREFIX``)
        must never leak into a workspace-band entry's restricted view — nothing forbids a
        feature band from declaring a ``WINTER_*`` key; the ``WINTER_*`` refusal in
        ``_filtered_command_output`` covers only command *output*, not a band literal.
        ``WS`` never references anything but the base var itself, so it resolves in the
        very first round, before the feature literal's own claim lands — this shape must
        agree with the delayed-chain shape below, not merely happen to be right by luck
        of resolution order.
        """
        workspace_band = {"WS": "prefix-${WINTER_SERVICE_PREFIX}"}
        feature_result = _resolve(
            workspace_band=workspace_band,
            feature_band={"WINTER_SERVICE_PREFIX": "hijacked"},
        )
        workspace_result = _resolve(
            workspace_band=workspace_band,
            base_scope=WORKSPACE_BASE_SCOPE,
        )
        assert feature_result["WINTER_SERVICE_PREFIX"] == "hijacked"
        assert feature_result["WS"] == workspace_result["WS"] == "prefix-proj"

    def test_workspace_entry_sees_the_base_value_of_a_key_a_feature_band_redeclares_delayed(self) -> None:
        """The same invariant, forced through a workspace-band chain (``A -> B -> C``) that
        delays ``WS`` past the round the feature-band literal claims
        ``WINTER_SERVICE_PREFIX`` in ``resolved`` — the delay must not be able to leak
        that redeclaration into ``WS``'s restricted view of the base var.
        """
        workspace_band = {
            "WS": "prefix-${WINTER_SERVICE_PREFIX}-${C}",
            "A": "a",
            "B": "${A}b",
            "C": "${B}c",
        }
        feature_result = _resolve(
            workspace_band=workspace_band,
            feature_band={"WINTER_SERVICE_PREFIX": "hijacked"},
        )
        workspace_result = _resolve(
            workspace_band=workspace_band,
            base_scope=WORKSPACE_BASE_SCOPE,
        )
        assert feature_result["WINTER_SERVICE_PREFIX"] == "hijacked"
        assert feature_result["WS"] == workspace_result["WS"] == "prefix-proj-abc"

    @pytest.mark.parametrize("literal_declared_first", [False, True])
    def test_workspace_entry_never_observes_its_own_bands_redeclaration_of_a_base_var(
        self, *, literal_declared_first: bool
    ) -> None:
        """A *workspace*-band literal that redeclares a base-var name (``WINTER_PORT_BASE``)
        must never leak into another workspace-band entry's restricted view, in either
        declaration order or scope.

        ``WINTER_PORT_BASE`` is deliberately the collision target, not
        ``WINTER_SERVICE_PREFIX``: it is the one base var present in ``base_scope``
        (and so in ``base_scope_keys``) at a feature scope but absent from it at
        workspace scope, so a fixture built on a base var present at *both* base
        scopes cannot tell a scope-*dependent* exclusion from the scope-*invariant*
        one this restriction requires — it renders identically either way, bug or
        no bug. ``A`` must raise identically at both scopes: a workspace-band entry
        may never observe ``WINTER_PORT_BASE`` by reference at all (see "one
        restriction survives the fixpoint" above), including via a sibling entry's
        redeclaration of that name, regardless of declaration order.
        """
        band = (
            {"WINTER_PORT_BASE": "wsoverride", "A": "${WINTER_PORT_BASE}"}
            if literal_declared_first
            else {"A": "${WINTER_PORT_BASE}", "WINTER_PORT_BASE": "wsoverride"}
        )
        with pytest.raises(ValueError, match=r"undefined variable.*WINTER_PORT_BASE"):
            _resolve(workspace_band=band)
        with pytest.raises(ValueError, match=r"undefined variable.*WINTER_PORT_BASE"):
            _resolve(workspace_band=band, base_scope=WORKSPACE_BASE_SCOPE)


# ---------------------------------------------------------------------------
# Command entries — the placeholder path (no runner exists yet)
# ---------------------------------------------------------------------------


class TestCommandEntryPlaceholder:
    def test_command_entry_resolves_to_placeholder_when_gated(self) -> None:
        result = _resolve(feature_band={"DB_PASSWORD": EnvCommandEntry(command="vals get ref+vault://db")})
        assert result["DB_PASSWORD"] == COMMAND_PLACEHOLDER

    def test_underscore_prefixed_command_entry_is_never_exported_even_when_gated(self) -> None:
        """A ``_``-prefixed key is an anonymous import slot that is never exported itself —
        that rule holds whether the command actually ran (see
        ``test_underscore_prefixed_output_key_is_never_exported``) or the gate masked it
        off entirely. Only the keys its output would import are ever placeholder'd for a
        reader; the anonymous slot's own key never appears in the returned map.
        """
        result = _resolve(
            feature_band={"_aws": EnvCommandEntry(command="chamber export", format=EnvCommandFormat.json)}
        )
        assert "_aws" not in result

    def test_pure_entry_referencing_a_command_entry_is_never_deferred(self) -> None:
        """A pure entry referencing a gated command entry's key resolves immediately — no failure.

        This is the paradigm example: DATABASE_URL depends on both a command
        entry (DB_PASSWORD) and a pure entry (WTS_DB_PORT); gating never turns
        a computation that would otherwise succeed into a failure.
        """
        result = _resolve(
            feature_band={
                "WTS_DB_PORT": "${WINTER_PORT_BASE+12}",
                "SECRET_TIER": "dev",
                "DB_PASSWORD": EnvCommandEntry(command="vals get ref+vault://${SECRET_TIER}/db"),
                "DATABASE_URL": "postgresql://wts:${DB_PASSWORD}@localhost:${WTS_DB_PORT}/wts",
            },
            named_band={"SECRET_TIER": "qa"},
        )
        assert result["DB_PASSWORD"] == COMMAND_PLACEHOLDER
        assert result["DATABASE_URL"] == f"postgresql://wts:{COMMAND_PLACEHOLDER}@localhost:4032/wts"
        # The named-band override is visible even though DB_PASSWORD's own
        # command string references it — its command is never rendered or run.
        assert result["SECRET_TIER"] == "qa"

    def test_offset_on_a_gated_command_key_masks_instead_of_failing(self) -> None:
        """``${CMD+N}`` under the gate masks to the placeholder rather than raising.

        ``CMD`` is an ordinary (non ``_``-prefixed) declared command-entry key,
        for which the stated rationale actually holds: the gated placeholder
        is not an integer, so applying an offset to it cannot be computed —
        but it must not be judged either, since the same template resolves
        fine once the gate is open (``CMD``'s own declared key is never
        dropped by merge policy), and gating must never turn a computation
        that would otherwise succeed into a failure. A raise here would break
        ``down`` and ``status`` (both gated off) for a config that ``up``
        resolves without complaint. A ``_``-prefixed key does not qualify for
        this masking at all — see
        ``test_reference_to_an_underscore_prefixed_key_is_refused_under_both_gates``.
        """
        result = _resolve(feature_band={"CMD": EnvCommandEntry(command="echo 5000"), "PORT": "${CMD+1}"})
        assert result["PORT"] == COMMAND_PLACEHOLDER

    def test_reference_to_an_underscore_prefixed_key_is_refused_under_both_gates(self) -> None:
        """A ``_``-prefixed key is an anonymous import slot ``_filtered_command_output``
        always drops, gated or not — so a reference to one can never resolve even once
        the gate opens, unlike an ordinary command entry's own declared key (see the test
        above). Masking it only while gated would let ``winter env``, ``service down``,
        and ``service status`` (all gated off) report success for a config that
        ``service up`` and ``winter env --resolve`` (gated on) reject — so the refusal
        must fire identically in both gate states, naming the same permanent cause.
        """
        band = {"_p": EnvCommandEntry(command="echo 5000"), "PORT": "${_p+1}"}
        with pytest.raises(ValueError, match=r"reference to '_p' can never be supplied — '_p' is `_`-prefixed"):
            _resolve(feature_band=band, resolve_commands=False)
        with pytest.raises(ValueError, match=r"reference to '_p' can never be supplied — '_p' is `_`-prefixed"):
            _resolve(
                feature_band=band,
                resolve_commands=True,
                runner=FakeCommandEntryRunner({"echo 5000": "5000"}),
            )

    def test_gating_can_fail_a_reference_to_an_imported_key_that_would_otherwise_succeed(self) -> None:
        """The gate's "never fails a would-succeed computation" guarantee covers only a
        command entry's own *declared* key — not a key its ``dotenv``/``json`` output would
        import. ``AWS_KEY`` is never known while gated off, so a reference to it raises under
        ``resolve_commands=False`` even though the identical config succeeds under ``True``.
        This asymmetry is documented, not fixed — this test pins it so the
        module docstring's narrowed claim stays honest.
        """
        with pytest.raises(ValueError, match="undefined variable 'AWS_KEY'"):
            _resolve(
                feature_band={
                    "_aws": EnvCommandEntry(command="aws-fetch", format=EnvCommandFormat.json),
                    "DSN": "pg://${AWS_KEY}",
                },
                resolve_commands=False,
            )
        runner = FakeCommandEntryRunner({"aws-fetch": '{"AWS_KEY": "shh"}'})
        result = _resolve(
            feature_band={
                "_aws": EnvCommandEntry(command="aws-fetch", format=EnvCommandFormat.json),
                "DSN": "pg://${AWS_KEY}",
            },
            resolve_commands=True,
            runner=runner,
        )
        assert result["DSN"] == "pg://shh"

    def test_a_typo_alongside_a_command_entry_still_raises(self) -> None:
        """Masking is narrow: a command entry's presence does not excuse an unrelated bad reference."""
        with pytest.raises(ValueError, match=r"undefined variable 'NOPE'"):
            _resolve(feature_band={"SECRET": EnvCommandEntry(command="echo hi"), "BAD": "${NOPE}"})

    def test_a_pure_cycle_alongside_a_command_entry_still_raises(self) -> None:
        """Masking is narrow: a cycle among pure entries still fails even when a command entry exists."""
        with pytest.raises(ValueError, match="cycle"):
            _resolve(feature_band={"SECRET": EnvCommandEntry(command="echo hi"), "A": "${B}", "B": "${A}"})


# ---------------------------------------------------------------------------
# Command entries — real execution (resolve_commands=True)
# ---------------------------------------------------------------------------


class TestCommandEntryExecution:
    def test_raw_command_entry_becomes_trimmed_stdout(self) -> None:
        """The default format (raw): trimmed stdout becomes the declared key's own value."""
        runner = FakeCommandEntryRunner({"vals get ref+vault://db": "hunter2\n"})
        result = _resolve(
            feature_band={"DB_PASSWORD": EnvCommandEntry(command="vals get ref+vault://db")},
            resolve_commands=True,
            runner=runner,
        )
        assert result["DB_PASSWORD"] == "hunter2"

    def test_command_template_tokens_are_rendered_before_running(self) -> None:
        """${...} tokens in the command string are rendered against the visible scope first."""
        runner = FakeCommandEntryRunner({"vals get ref+vault://qa/db": "secret"})
        result = _resolve(
            feature_band={
                "SECRET_TIER": "dev",
                "DB_PASSWORD": EnvCommandEntry(command="vals get ref+vault://${SECRET_TIER}/db"),
            },
            named_band={"SECRET_TIER": "qa"},
            resolve_commands=True,
            runner=runner,
        )
        assert result["DB_PASSWORD"] == "secret"
        assert result["SECRET_TIER"] == "qa"

    def test_command_entry_waits_for_its_own_references_across_rounds(self) -> None:
        """A command entry deferred on an unresolved reference runs once that reference resolves."""
        runner = FakeCommandEntryRunner({"vals get ref+vault://qa/db": "secret"})
        result = _resolve(
            feature_band={
                "DB_PASSWORD": EnvCommandEntry(command="vals get ref+vault://${SECRET_TIER}/db"),
                "SECRET_TIER": "${TIER_SOURCE}",
                "TIER_SOURCE": "qa",
            },
            resolve_commands=True,
            runner=runner,
        )
        assert result["DB_PASSWORD"] == "secret"

    def test_dotenv_format_merges_every_parsed_key(self) -> None:
        runner = FakeCommandEntryRunner({"chamber export myapp-dev": "AWS_ACCESS_KEY_ID=AKIA123\nAWS_SECRET=shh\n"})
        result = _resolve(
            feature_band={"_aws": EnvCommandEntry(command="chamber export myapp-dev", format=EnvCommandFormat.dotenv)},
            resolve_commands=True,
            runner=runner,
        )
        assert result["AWS_ACCESS_KEY_ID"] == "AKIA123"
        assert result["AWS_SECRET"] == "shh"
        # The declared key is an anonymous label only — never exported itself.
        assert "_aws" not in result

    def test_json_format_merges_every_parsed_key(self) -> None:
        runner = FakeCommandEntryRunner({"chamber export myapp-dev --json": '{"HOST": "db1", "PORT": "5432"}'})
        result = _resolve(
            feature_band={
                "_aws": EnvCommandEntry(command="chamber export myapp-dev --json", format=EnvCommandFormat.json)
            },
            resolve_commands=True,
            runner=runner,
        )
        assert result["HOST"] == "db1"
        assert result["PORT"] == "5432"

    def test_json_format_rejects_a_key_that_is_not_a_valid_env_var_identifier(self) -> None:
        """A json key shaped like ``a-b`` would merge into scope and break `source`."""
        runner = FakeCommandEntryRunner({"chamber export": '{"a-b": "1"}'})
        with pytest.raises(RepoError, match="not a valid env-var identifier"):
            _resolve(
                feature_band={"_aws": EnvCommandEntry(command="chamber export", format=EnvCommandFormat.json)},
                resolve_commands=True,
                runner=runner,
            )

    def test_dotenv_unquoting_preserves_a_value_ending_in_a_stray_quote(self) -> None:
        """Only a *matched* surrounding quote pair is stripped — a password that
        happens to end in an unmatched quote character is not corrupted."""
        runner = FakeCommandEntryRunner({"echo hi": 'PW=hunter2"\n'})
        result = _resolve(
            feature_band={"_x": EnvCommandEntry(command="echo hi", format=EnvCommandFormat.dotenv)},
            resolve_commands=True,
            runner=runner,
        )
        assert result["PW"] == 'hunter2"'

    def test_dotenv_unquoting_preserves_significant_inner_whitespace(self) -> None:
        """A matched quote pair still protects surrounding whitespace inside it."""
        runner = FakeCommandEntryRunner({"echo hi": 'PW=" secret "\n'})
        result = _resolve(
            feature_band={"_x": EnvCommandEntry(command="echo hi", format=EnvCommandFormat.dotenv)},
            resolve_commands=True,
            runner=runner,
        )
        assert result["PW"] == " secret "

    def test_json_format_preserves_a_multiline_value(self) -> None:
        runner = FakeCommandEntryRunner({"echo pem": '{"CERT": "line1\\nline2"}'})
        result = _resolve(
            feature_band={"_cert": EnvCommandEntry(command="echo pem", format=EnvCommandFormat.json)},
            resolve_commands=True,
            runner=runner,
        )
        assert result["CERT"] == "line1\nline2"

    def test_raw_format_preserves_internal_multiline_content(self) -> None:
        """`.strip()` trims outer whitespace only — internal newlines survive."""
        runner = FakeCommandEntryRunner({"cat key.pem": "\n-----BEGIN-----\nabc\n-----END-----\n"})
        result = _resolve(
            feature_band={"CERT": EnvCommandEntry(command="cat key.pem")},
            resolve_commands=True,
            runner=runner,
        )
        assert result["CERT"] == "-----BEGIN-----\nabc\n-----END-----"

    def test_winter_prefixed_output_key_is_dropped_not_overwritten(self) -> None:
        runner = FakeCommandEntryRunner({"echo hi": "WINTER_ENV=hacked\nSAFE=ok\n"})
        result = _resolve(
            base_scope=dict(FEATURE_BASE_SCOPE),
            feature_band={"_x": EnvCommandEntry(command="echo hi", format=EnvCommandFormat.dotenv)},
            resolve_commands=True,
            runner=runner,
        )
        assert result["WINTER_ENV"] == "alpha"
        assert result["SAFE"] == "ok"

    def test_exports_allow_list_drops_keys_outside_it(self) -> None:
        runner = FakeCommandEntryRunner({"echo hi": "A=1\nB=2\n"})
        result = _resolve(
            feature_band={"_x": EnvCommandEntry(command="echo hi", format=EnvCommandFormat.dotenv, exports=("A",))},
            resolve_commands=True,
            runner=runner,
        )
        assert result["A"] == "1"
        assert "B" not in result

    def test_underscore_prefixed_output_key_is_never_exported(self) -> None:
        runner = FakeCommandEntryRunner({"echo hi": "_internal=skip\nREAL=1\n"})
        result = _resolve(
            feature_band={"_x": EnvCommandEntry(command="echo hi", format=EnvCommandFormat.dotenv)},
            resolve_commands=True,
            runner=runner,
        )
        assert "_internal" not in result
        assert result["REAL"] == "1"

    def test_a_pure_entry_may_reference_a_commands_produced_output(self) -> None:
        runner = FakeCommandEntryRunner({"vals get db": "hunter2"})
        result = _resolve(
            feature_band={
                "DB_PASSWORD": EnvCommandEntry(command="vals get db"),
                "DATABASE_URL": "postgresql://wts:${DB_PASSWORD}@localhost/wts",
            },
            resolve_commands=True,
            runner=runner,
        )
        assert result["DATABASE_URL"] == "postgresql://wts:hunter2@localhost/wts"

    def test_non_zero_exit_raises_repo_error_not_value_error(self) -> None:
        err = RepoError("env.feature.vars key 'DB_PASSWORD': command `vals get db` failed", exit_code=1)
        runner = FakeCommandEntryRunner({"vals get db": err})
        with pytest.raises(RepoError):
            _resolve(
                feature_band={"DB_PASSWORD": EnvCommandEntry(command="vals get db")},
                resolve_commands=True,
                runner=runner,
            )

    @pytest.mark.parametrize(
        ("fmt", "output", "detail"),
        [
            (EnvCommandFormat.json, "not json", "not valid JSON"),
            (EnvCommandFormat.json, '["a", "b"]', "flat JSON object"),
            (EnvCommandFormat.json, '{"A": {"nested": 1}}', "not a flat scalar"),
            (EnvCommandFormat.dotenv, "no equals sign here", "dotenv KEY=VALUE"),
            (EnvCommandFormat.dotenv, "1BAD=x", "dotenv KEY=VALUE"),
        ],
    )
    def test_unparseable_output_names_entry_command_and_exit_code(
        self, fmt: EnvCommandFormat, output: str, detail: str
    ) -> None:
        """Every command-failure mode names the entry, command, and
        exit code. A parse failure is raised after the process is gone, so it has to carry
        the entry's own declared command — the exit code is 0, since the command did succeed.
        """
        runner = FakeCommandEntryRunner({"fetch-secrets --tier qa": output})
        with pytest.raises(RepoError) as excinfo:
            _resolve(
                feature_band={
                    "TIER": "qa",
                    "_x": EnvCommandEntry(command="fetch-secrets --tier ${TIER}", format=fmt),
                },
                resolve_commands=True,
                runner=runner,
            )
        message = str(excinfo.value)
        assert "env.feature.vars key '_x'" in message
        # The *declared* (unrendered) command, not the rendered one actually run — a
        # rendered command may carry a value substituted in from another entry's
        # output, and this message must never be able to echo it out.
        assert "fetch-secrets --tier ${TIER}" in message
        assert "fetch-secrets --tier qa" not in message
        assert "exit 0" in message
        assert excinfo.value.exit_code == 0
        assert detail in message

    def test_dotenv_parse_failure_does_not_echo_the_offending_lines_text(self) -> None:
        """A malformed dotenv line's own text can itself be secret material — a secrets
        command's output that fails to parse must not have that text echoed into a
        ``RepoError`` bound for stderr and CI logs. The line number is enough to diagnose
        it: the operator can run the command themselves and look at that line.
        """
        runner = FakeCommandEntryRunner({"fetch-secrets": "GOOD=1\nBEGIN_KEY_MATERIAL_SECRET\nOTHER=2\n"})
        with pytest.raises(RepoError) as excinfo:
            _resolve(
                feature_band={"_x": EnvCommandEntry(command="fetch-secrets", format=EnvCommandFormat.dotenv)},
                resolve_commands=True,
                runner=runner,
            )
        message = str(excinfo.value)
        assert "line 2" in message
        assert "BEGIN_KEY_MATERIAL_SECRET" not in message

    def test_runner_receives_the_declared_unrendered_command_alongside_the_rendered_one(self) -> None:
        """The resolver hands the runner both the rendered command to execute and the
        entry's declared, unrendered command for messages, so a failure can never echo
        a value substituted in from another entry's output. The runner itself is
        what builds failure messages from ``declared_command`` — pinned separately in
        ``test_subprocess_command_entry_runner.py``.
        """
        runner = FakeCommandEntryRunner({"vault-login": "hunter2-token", "vault kv get -field=pw hunter2-token": "pw"})
        _resolve(
            feature_band={
                "TOKEN": EnvCommandEntry(command="vault-login"),
                "PW": EnvCommandEntry(command="vault kv get -field=pw ${TOKEN}"),
            },
            resolve_commands=True,
            runner=runner,
        )
        [call] = [c for c in runner.calls if c[3] == "env.feature.vars key 'PW'"]
        rendered, _shell, _env, _description, declared = call
        assert rendered == "vault kv get -field=pw hunter2-token"
        assert declared == "vault kv get -field=pw ${TOKEN}"

    def test_shell_entry_may_not_reference_a_command_derived_key(self) -> None:
        """A shell = true entry referencing another command's output is refused."""
        runner = FakeCommandEntryRunner({"vals get db": "hunter2"})
        with pytest.raises(ValueError, match=r"shell = true.*DB_PASSWORD"):
            _resolve(
                feature_band={
                    "DB_PASSWORD": EnvCommandEntry(command="vals get db"),
                    "MIGRATE": EnvCommandEntry(command="run-migration --pw=${DB_PASSWORD}", shell=True),
                },
                resolve_commands=True,
                runner=runner,
            )

    def test_shell_entry_may_reference_an_ordinary_pure_entry(self) -> None:
        """The shell refusal only blocks command-derived keys — operator-authored templates are fine."""
        runner = FakeCommandEntryRunner({"run-migration --tier=qa": "done"})
        result = _resolve(
            feature_band={
                "SECRET_TIER": "qa",
                "MIGRATE": EnvCommandEntry(command="run-migration --tier=${SECRET_TIER}", shell=True),
            },
            resolve_commands=True,
            runner=runner,
        )
        assert result["MIGRATE"] == "done"

    def test_shell_refusal_fires_even_when_the_shell_entry_is_declared_before_the_command_slot(self) -> None:
        """The refusal must not depend on which round or position a colliding
        command entry resolves in relative to the shell entry.

        ``TOKEN`` starts as a literal and ``MIGRATE`` (shell) is declared
        *between* it and ``_a`` — the command entry that ultimately wins the
        ``TOKEN`` key. Under naive single-pass-per-round processing, ``MIGRATE``
        would see ``TOKEN`` as already resolved to the literal and run before
        ``_a`` ever executes, escaping the refusal entirely even though
        ``TOKEN``'s final, provenance-determined value is command-derived. The
        refusal must fire regardless of this declaration order — it fires for
        the mirror declaration order in
        ``test_shell_refusal_still_fires_for_a_key_the_command_actually_wins``.
        """
        runner = FakeCommandEntryRunner(
            {"vals get db": '{"TOKEN": "from-command"}', "run-migration --pw=literal": "done"}
        )
        with pytest.raises(ValueError, match=r"shell = true.*TOKEN"):
            _resolve(
                feature_band={
                    "TOKEN": "literal",
                    "MIGRATE": EnvCommandEntry(command="run-migration --pw=${TOKEN}", shell=True),
                    "_a": EnvCommandEntry(command="vals get db", format=EnvCommandFormat.json),
                },
                resolve_commands=True,
                runner=runner,
            )

    def test_shell_refusal_ignores_a_key_a_later_literal_reclaims(self) -> None:
        """A key a later-declared literal wins is not command-derived, so the shell refusal is silent.

        ``_a`` emits ``TOKEN`` but ``TOKEN`` is declared below it, so the
        literal owns the key.  ``MIGRATE`` interpolates an operator-authored
        value, not a command's output, and must be allowed — and must be
        allowed identically whether or not ``_a`` happened to run first.
        """
        runner = FakeCommandEntryRunner(
            {"vals get db": '{"TOKEN": "from-command"}', "run-migration --pw=literal": "done"}
        )
        result = _resolve(
            feature_band={
                "_a": EnvCommandEntry(command="vals get db", format=EnvCommandFormat.json),
                "MIGRATE": EnvCommandEntry(command="run-migration --pw=${TOKEN}", shell=True),
                "TOKEN": "literal",
            },
            resolve_commands=True,
            runner=runner,
        )
        assert result["TOKEN"] == "literal"
        assert result["MIGRATE"] == "done"

    def test_shell_refusal_still_fires_for_a_key_the_command_actually_wins(self) -> None:
        """The mirror of the case above: the literal is declared *before* the slot, so the
        command owns ``TOKEN`` and ``command_derived_keys`` must still hold it."""
        runner = FakeCommandEntryRunner({"vals get db": '{"TOKEN": "from-command"}'})
        with pytest.raises(ValueError, match=r"shell = true.*TOKEN"):
            _resolve(
                feature_band={
                    "TOKEN": "literal",
                    "_a": EnvCommandEntry(command="vals get db", format=EnvCommandFormat.json),
                    "MIGRATE": EnvCommandEntry(command="run-migration --pw=${TOKEN}", shell=True),
                },
                resolve_commands=True,
                runner=runner,
            )

    def test_shell_refusal_fires_when_the_colliding_multivalue_entry_stays_pending_through_a_round(self) -> None:
        """The colliding ``format = "json"`` entry (``_a``) carries an unresolved reference of
        its own (``${DEP}``), so it stays pending after its first attempt and only resolves a
        round later, once ``DEP`` (a plain command entry) has itself run. ``MIGRATE`` never
        references ``DEP``, so nothing about ``_a``'s own reference should matter to the
        refusal — but a ``_a`` that merely *had an attempt* this round, without resolving, must
        still count as a threat to ``TOKEN``: it can still claim the key a later round. Every
        other shell-refusal test uses a multi-value entry whose command has no unresolved
        reference, so it always resolves in the very first pass and this path — a multi-value
        entry that stays pending *through* a round — was never exercised.
        """
        runner = FakeCommandEntryRunner(
            {
                "getdep": "dep-value",
                "vals get dep-value": '{"TOKEN": "from-command"}',
                "run-migration --pw=literal": "done",
            }
        )
        with pytest.raises(ValueError, match=r"shell = true.*TOKEN"):
            _resolve(
                feature_band={
                    "TOKEN": "literal",
                    "MIGRATE": EnvCommandEntry(command="run-migration --pw=${TOKEN}", shell=True),
                    "_a": EnvCommandEntry(command="vals get ${DEP}", format=EnvCommandFormat.json),
                    "DEP": EnvCommandEntry(command="getdep"),
                },
                resolve_commands=True,
                runner=runner,
            )

    def test_shell_refusal_fires_when_the_colliding_multivalue_entry_stays_pending_the_mirror_order(self) -> None:
        """The mirror declaration order of the case above: ``DEP`` declared before ``_a`` lets
        ``DEP`` — and then ``_a`` — resolve within the very same first pass, since nothing
        blocks ``_a``'s own reference by the time its turn comes up in that pass. The refusal
        must fire identically either way: it must not depend on whether the colliding entry
        happened to resolve within the first pass or only after staying pending through it.
        """
        runner = FakeCommandEntryRunner(
            {
                "getdep": "dep-value",
                "vals get dep-value": '{"TOKEN": "from-command"}',
                "run-migration --pw=literal": "done",
            }
        )
        with pytest.raises(ValueError, match=r"shell = true.*TOKEN"):
            _resolve(
                feature_band={
                    "TOKEN": "literal",
                    "MIGRATE": EnvCommandEntry(command="run-migration --pw=${TOKEN}", shell=True),
                    "DEP": EnvCommandEntry(command="getdep"),
                    "_a": EnvCommandEntry(command="vals get ${DEP}", format=EnvCommandFormat.json),
                },
                resolve_commands=True,
                runner=runner,
            )

    def test_shell_refusal_still_fires_when_the_colliding_multivalue_entry_is_itself_shell_true(self) -> None:
        """A ``format = "json"`` command entry that is *also* ``shell = true`` must still
        count as a pending multi-value entry blocking every other ``shell`` entry — it is
        not exempt from the blocking check just because it lives in the same deferred-shell
        pass as ``MIGRATE``. Without that, ``MIGRATE`` could run against ``TOKEN``'s literal
        value in the same round ``_a`` finally overwrites it, escaping the refusal entirely.
        """
        runner = FakeCommandEntryRunner(
            {"vals get db": '{"TOKEN": "from-command"}', "run-migration --pw=literal": "done"}
        )
        with pytest.raises(ValueError, match=r"shell = true.*TOKEN"):
            _resolve(
                feature_band={
                    "TOKEN": "literal",
                    "MIGRATE": EnvCommandEntry(command="run-migration --pw=${TOKEN}", shell=True),
                    "_a": EnvCommandEntry(command="vals get db", format=EnvCommandFormat.json, shell=True),
                },
                resolve_commands=True,
                runner=runner,
            )

    def test_shell_refusal_fires_for_a_multivalue_shell_entry_declared_before_migrate_too(self) -> None:
        """The mirror declaration order for the multi-value-and-shell case above."""
        runner = FakeCommandEntryRunner(
            {"vals get db": '{"TOKEN": "from-command"}', "run-migration --pw=literal": "done"}
        )
        with pytest.raises(ValueError, match=r"shell = true.*TOKEN"):
            _resolve(
                feature_band={
                    "TOKEN": "literal",
                    "_a": EnvCommandEntry(command="vals get db", format=EnvCommandFormat.json, shell=True),
                    "MIGRATE": EnvCommandEntry(command="run-migration --pw=${TOKEN}", shell=True),
                },
                resolve_commands=True,
                runner=runner,
            )

    def test_two_shell_multivalue_entries_do_not_deadlock_each_other(self) -> None:
        """Two ``shell = true, format = "json"`` entries, neither referencing the other,
        must not hold each other pending forever: each is admitted in declaration-position
        order once the other has had its own turn this round (or the next).
        """
        runner = FakeCommandEntryRunner({"vals get a": '{"A_KEY": "a-value"}', "vals get b": '{"B_KEY": "b-value"}'})
        result = _resolve(
            feature_band={
                "_a": EnvCommandEntry(command="vals get a", format=EnvCommandFormat.json, shell=True),
                "_b": EnvCommandEntry(command="vals get b", format=EnvCommandFormat.json, shell=True),
            },
            resolve_commands=True,
            runner=runner,
        )
        assert result["A_KEY"] == "a-value"
        assert result["B_KEY"] == "b-value"
        assert {call[0] for call in runner.calls} == {"vals get a", "vals get b"}

    def test_a_shell_multivalue_producer_is_not_blocked_by_a_non_shell_multivalue_consumer(self) -> None:
        """A legitimate producer/consumer pair — a ``shell = true`` multi-value entry
        supplying a key a *non*-shell multi-value entry's own command references — must
        resolve, not deadlock: the consumer's own reference genuinely cannot be satisfied
        until the producer runs, and the producer has no reference of its own to wait on.
        """
        runner = FakeCommandEntryRunner(
            {"produce": '{"TOKEN": "prod-token"}', "consume prod-token": '{"RESULT": "consumed"}'}
        )
        result = _resolve(
            feature_band={
                "_producer": EnvCommandEntry(command="produce", format=EnvCommandFormat.json, shell=True),
                "_consumer": EnvCommandEntry(command="consume ${TOKEN}", format=EnvCommandFormat.json),
            },
            resolve_commands=True,
            runner=runner,
        )
        assert result["TOKEN"] == "prod-token"
        assert result["RESULT"] == "consumed"

    def test_command_runs_with_the_accumulated_scope_as_env(self) -> None:
        runner = FakeCommandEntryRunner({"echo hi": "ok"})
        _resolve(
            feature_band={
                "WTS_DB_PORT": "${WINTER_PORT_BASE+12}",
                "PROBE": EnvCommandEntry(command="echo hi"),
            },
            resolve_commands=True,
            runner=runner,
        )
        [(command, shell, env, description, declared_command)] = runner.calls
        assert command == "echo hi"
        assert shell is False
        assert env["WTS_DB_PORT"] == "4032"
        assert description == "env.feature.vars key 'PROBE'"
        assert declared_command == "echo hi"


# ---------------------------------------------------------------------------
# Declared key vs. command-output key collisions — band, then declaration order
# ---------------------------------------------------------------------------


class TestDeclaredKeyVsCommandOutputCollision:
    def test_literal_declared_after_a_command_slot_overrides_its_output_key(self) -> None:
        """A later-declared literal beats the earlier slot's output for the same key."""
        runner = FakeCommandEntryRunner({"chamber export": '{"DB_HOST": "dbhost-from-command", "DB_PORT": "5432"}'})
        result = _resolve(
            feature_band={
                "_aws": EnvCommandEntry(command="chamber export", format=EnvCommandFormat.json),
                "DB_HOST": "localhost",
            },
            resolve_commands=True,
            runner=runner,
        )
        assert result["DB_HOST"] == "localhost"
        assert result["DB_PORT"] == "5432"

    def test_literal_declared_before_a_command_slot_loses_to_its_output_key(self) -> None:
        """An earlier-declared literal loses to a later slot's output for the same key."""
        runner = FakeCommandEntryRunner({"chamber export": '{"DB_HOST": "dbhost-from-command"}'})
        result = _resolve(
            feature_band={
                "DB_HOST": "localhost",
                "_aws": EnvCommandEntry(command="chamber export", format=EnvCommandFormat.json),
            },
            resolve_commands=True,
            runner=runner,
        )
        assert result["DB_HOST"] == "dbhost-from-command"

    def test_result_is_independent_of_which_round_the_command_resolves_in(self) -> None:
        """The literal-after-slot outcome holds even when the command is delayed several rounds.

        ``DB_HOST`` (no references) resolves in round 1; ``_aws`` cannot run
        until ``TIER`` resolves, and ``TIER`` cannot resolve until
        ``TIER_SOURCE`` does — on top of that, ``TIER`` is declared *after*
        ``_aws`` in the same band, so ``_aws`` is deferred past the round in
        which ``TIER`` itself resolves too, landing its command run several
        rounds after ``DB_HOST`` already wrote a value. Under the old
        arrival-order behavior this test guards against, whichever entry's
        write physically happened last inside the loop would win regardless
        of declared position — here that would be the delayed command,
        flipping ``DB_HOST`` to ``"dbhost-from-command"`` even though the
        literal is declared after the slot and should win. Resolving by
        provenance position instead of arrival order keeps the outcome
        identical to the single-round case above.
        """
        runner = FakeCommandEntryRunner(
            {"chamber export prod": '{"DB_HOST": "dbhost-from-command", "DB_PORT": "5432"}'}
        )
        result = _resolve(
            feature_band={
                "_aws": EnvCommandEntry(command="chamber export ${TIER}", format=EnvCommandFormat.json),
                "TIER": "${TIER_SOURCE}",
                "TIER_SOURCE": "prod",
                "DB_HOST": "localhost",
            },
            resolve_commands=True,
            runner=runner,
        )
        assert result["DB_HOST"] == "localhost"
        assert result["DB_PORT"] == "5432"

    def test_named_band_literal_beats_feature_band_command_output_key(self) -> None:
        """Band precedence dominates: a per-env literal beats a feature-band command's output."""
        runner = FakeCommandEntryRunner({"chamber export": '{"DB_HOST": "dbhost-from-command"}'})
        result = _resolve(
            feature_band={"_aws": EnvCommandEntry(command="chamber export", format=EnvCommandFormat.json)},
            named_band={"DB_HOST": "override"},
            resolve_commands=True,
            runner=runner,
        )
        assert result["DB_HOST"] == "override"

    def test_feature_band_command_output_key_beats_workspace_band_literal(self) -> None:
        """Band precedence dominates the other way too: a higher band's command output beats
        a lower band's literal, regardless of declaration position within either band."""
        runner = FakeCommandEntryRunner({"chamber export": '{"DB_HOST": "dbhost-from-command"}'})
        result = _resolve(
            workspace_band={"DB_HOST": "localhost"},
            feature_band={"_aws": EnvCommandEntry(command="chamber export", format=EnvCommandFormat.json)},
            resolve_commands=True,
            runner=runner,
        )
        assert result["DB_HOST"] == "dbhost-from-command"

    def test_two_commands_output_keys_collide_by_band_then_declaration_order(self) -> None:
        """Two commands' outputs colliding on the same key resolve by the same band-then-position
        rule as a declared-vs-command collision — the later-declared slot's output wins."""
        runner = FakeCommandEntryRunner(
            {
                "vals get first": '{"DB_HOST": "from-first"}',
                "vals get second": '{"DB_HOST": "from-second"}',
            }
        )
        result = _resolve(
            feature_band={
                "_first": EnvCommandEntry(command="vals get first", format=EnvCommandFormat.json),
                "_second": EnvCommandEntry(command="vals get second", format=EnvCommandFormat.json),
            },
            resolve_commands=True,
            runner=runner,
        )
        assert result["DB_HOST"] == "from-second"

    @pytest.mark.parametrize("delay_command", [False, True])
    def test_a_third_entry_never_observes_a_value_the_declaration_takes_back(self, *, delay_command: bool) -> None:
        """A key's losing command value is never visible, so a referrer cannot render against it.

        ``HOST`` is declared below the slot that also emits it, so the literal
        wins.  ``Q`` references ``HOST``.  Settling the collision only at the
        moment of the write would leave ``Q`` rendering against whichever value
        happened to be in the scope when its own round came up — ``from-cmd-q``
        when the command runs in round 1, ``literal-q`` when an added
        dependency delays it — which is the round-dependence this rule exists
        to remove.
        """
        command = "chamber prod" if delay_command else "chamber"
        band: dict[str, object] = {
            "_a": EnvCommandEntry(
                command="chamber ${TIER}" if delay_command else "chamber", format=EnvCommandFormat.json
            )
        }
        band["Q"] = "${HOST}-q"
        if delay_command:
            band["TIER"] = "prod"
        band["HOST"] = "literal"
        result = _resolve(
            feature_band=band,
            resolve_commands=True,
            runner=FakeCommandEntryRunner({command: '{"HOST": "from-cmd"}'}),
        )
        assert result["HOST"] == "literal"
        assert result["Q"] == "literal-q"

    def test_literal_declared_before_a_slot_loses_even_when_it_lands_in_a_later_round(self) -> None:
        """The losing literal resolves a round *after* the command, so arrival order would
        hand it the key; declared position hands it to the command instead."""
        runner = FakeCommandEntryRunner({"chamber": '{"DB_HOST": "from-cmd"}'})
        result = _resolve(
            feature_band={
                "DB_HOST": "${TIER}",
                "_a": EnvCommandEntry(command="chamber", format=EnvCommandFormat.json),
                "TIER": "localhost",
            },
            resolve_commands=True,
            runner=runner,
        )
        assert result["DB_HOST"] == "from-cmd"

    def test_later_slot_wins_a_command_vs_command_collision_from_an_earlier_round(self) -> None:
        """Command-vs-command by declared position, not arrival: ``_second`` is declared
        below ``_first`` but runs a round earlier, and still wins."""
        runner = FakeCommandEntryRunner(
            {
                "vals first prod": '{"DB_HOST": "from-first"}',
                "vals second": '{"DB_HOST": "from-second"}',
            }
        )
        result = _resolve(
            feature_band={
                "_first": EnvCommandEntry(command="vals first ${TIER}", format=EnvCommandFormat.json),
                "_second": EnvCommandEntry(command="vals second", format=EnvCommandFormat.json),
                "TIER": "prod",
            },
            resolve_commands=True,
            runner=runner,
        )
        assert result["DB_HOST"] == "from-second"

    def test_named_band_literal_beats_command_output_even_when_it_lands_later(self) -> None:
        """Band precedence holds when the per-env literal resolves after the command ran."""
        runner = FakeCommandEntryRunner({"chamber": '{"DB_HOST": "from-cmd"}'})
        result = _resolve(
            feature_band={"_a": EnvCommandEntry(command="chamber", format=EnvCommandFormat.json)},
            named_band={"DB_HOST": "${T}", "T": "override"},
            resolve_commands=True,
            runner=runner,
        )
        assert result["DB_HOST"] == "override"

    def test_command_output_beats_a_workspace_literal_that_lands_in_a_later_round(self) -> None:
        """The mirror: the losing workspace literal writes last and still loses to the
        higher band's command output."""
        runner = FakeCommandEntryRunner({"chamber": '{"DB_HOST": "from-cmd"}'})
        result = _resolve(
            workspace_band={"DB_HOST": "${WS_TIER}", "WS_TIER": "localhost"},
            feature_band={"_a": EnvCommandEntry(command="chamber", format=EnvCommandFormat.json)},
            resolve_commands=True,
            runner=runner,
        )
        assert result["DB_HOST"] == "from-cmd"

    @pytest.mark.parametrize("literal_declared_first", [False, True])
    def test_third_entry_reading_a_feature_band_redeclared_base_var_is_order_independent(
        self, *, literal_declared_first: bool
    ) -> None:
        """A feature-band literal that redeclares a base-var name (``WINTER_SERVICE_PREFIX``)
        pre-empts a third entry's read of that name in either declaration order — the third
        entry must never render against the unclaimed base-scope seed while the declaring
        literal is still pending, only against the literal's own landed value.
        """
        band = (
            {"WINTER_SERVICE_PREFIX": "override", "A": "${WINTER_SERVICE_PREFIX}"}
            if literal_declared_first
            else {"A": "${WINTER_SERVICE_PREFIX}", "WINTER_SERVICE_PREFIX": "override"}
        )
        result = _resolve(feature_band=band)
        assert result["WINTER_SERVICE_PREFIX"] == "override"
        assert result["A"] == "override"

    @pytest.mark.parametrize("band_kind", ["feature", "workspace"])
    def test_a_literal_may_extend_the_base_var_name_it_declares_by_self_reference(self, *, band_kind: str) -> None:
        """A declared literal's own template may reference the very base-var name it
        declares, extending rather than replacing it (``WINTER_SERVICE_PREFIX =
        "${WINTER_SERVICE_PREFIX}-x"``). The base-scope withholding exists to stop a
        *third* entry from observing the stale seed while this literal's own claim is
        still pending (see the test above) — it must not also make the declaring entry
        itself unable to read the value it is extending.

        Before the self-read exemption this raised as a resolution cycle: the entry's
        one reference is its own, still-withheld key, which looks exactly like ``A -> A``
        unless the withholding is excused for the declaring entry's own read. A
        restricted (workspace-band) entry is unaffected by the withholding in the first
        place — its restricted view reads the base var straight from ``base_scope``,
        never the withheld ``resolved`` seed — so this pins both band shapes agreeing.
        """
        band = {"WINTER_SERVICE_PREFIX": "${WINTER_SERVICE_PREFIX}-x"}
        result = _resolve(feature_band=band) if band_kind == "feature" else _resolve(workspace_band=band)
        assert result["WINTER_SERVICE_PREFIX"] == "proj-x"


# ---------------------------------------------------------------------------
# Unresolvable entries — undefined vs. cycle
# ---------------------------------------------------------------------------


class TestUnresolvableEntries:
    def test_undefined_reference_raises_naming_entry_and_reference(self) -> None:
        with pytest.raises(ValueError, match=r"env\.feature\.vars key 'BAD'.*undefined variable.*UNKNOWN_VAR"):
            _resolve(feature_band={"BAD": "${UNKNOWN_VAR}"})

    def test_cycle_raises_distinguishing_it_from_undefined(self) -> None:
        """Two entries that reference each other raise, and the message calls out the cycle."""
        with pytest.raises(ValueError) as excinfo:
            _resolve(feature_band={"A": "${B}", "B": "${A}"})
        message = str(excinfo.value)
        assert "cycle" in message
        assert "undefined variable" not in message

    def test_mixed_cycle_and_undefined_are_both_named_distinctly(self) -> None:
        """An entry blocked on both a cycle and a genuinely undefined name reports both, distinctly."""
        with pytest.raises(ValueError) as excinfo:
            _resolve(
                feature_band={
                    "A": "${B}",
                    "B": "${A}",
                    "C": "${NOWHERE}",
                }
            )
        message = str(excinfo.value)
        assert "cycle" in message
        assert "undefined variable" in message
        assert "NOWHERE" in message

    def test_a_blocked_chain_is_not_reported_as_a_cycle(self) -> None:
        """``A -> B -> C -> UNDEFINED`` is a chain, not a cycle, and must not be called one.

        Every entry in the chain is unresolvable, but only ``C`` has the real
        cause. Labelling ``A`` and ``B`` as cyclic would send a reader hunting
        for a cycle that does not exist, so they are reported as blocked and
        the undefined reference is named exactly once, on the entry that
        declares it.
        """
        with pytest.raises(ValueError) as excinfo:
            _resolve(feature_band={"A": "${B}", "B": "${C}", "C": "${GONE}"})
        message = str(excinfo.value)
        assert "cycle" not in message
        assert "key 'A': reference to 'B' is blocked" in message
        assert "key 'B': reference to 'C' is blocked" in message
        assert "key 'C': reference to undefined variable 'GONE'" in message

    def test_a_chain_feeding_into_a_cycle_names_the_cycle_from_outside_it(self) -> None:
        """Each line classifies the *referenced* key, so an outside entry still points at the cycle.

        ``OUTSIDE`` is not itself part of a loop, but the key it waits on is —
        and saying so is what sends the reader to the actual fault.
        """
        with pytest.raises(ValueError) as excinfo:
            _resolve(feature_band={"OUTSIDE": "${A}", "A": "${B}", "B": "${A}"})
        message = str(excinfo.value)
        assert "key 'A': reference to 'B' is part of a resolution cycle" in message
        assert "key 'B': reference to 'A' is part of a resolution cycle" in message
        assert "key 'OUTSIDE': reference to 'A' is part of a resolution cycle" in message

    def test_unsupported_token_raises_immediately(self) -> None:
        with pytest.raises(ValueError, match="unsupported substitution token"):
            _resolve(feature_band={"BAD": "${not-an-identifier}"})

    def test_non_integer_offset_raises(self) -> None:
        with pytest.raises(ValueError, match="non-integer"):
            _resolve(
                feature_band={
                    "HOSTNAME": "db.example.com",
                    "BAD": "${HOSTNAME+1}",
                }
            )

    def test_reference_to_a_label_only_multivalue_key_names_the_real_reason(self) -> None:
        """A ``dotenv``/``json`` entry's declared key is a diagnostic label, never written.

        The command already ran (it's not "blocked"), and the key is declared
        in the config (it's not "undefined") — the old message pointed at "a
        reason reported on its own line" that does not exist, because the
        command entry that resolved the key already left ``pending``.
        """
        runner = FakeCommandEntryRunner({"aws-fetch": '{"AWS_KEY": "shh"}'})
        with pytest.raises(ValueError) as excinfo:
            _resolve(
                feature_band={
                    "_aws": EnvCommandEntry(command="aws-fetch", format=EnvCommandFormat.json),
                    "X": "${_aws}",
                },
                resolve_commands=True,
                runner=runner,
            )
        message = str(excinfo.value)
        assert "on its own line" not in message
        assert "undefined variable '_aws'" not in message
        assert "'_aws' is `_`-prefixed" in message

    def test_reference_to_a_key_dropped_by_exports_names_the_real_reason(self) -> None:
        """A ``raw`` key an ``exports`` allow-list excludes is likewise never written."""
        runner = FakeCommandEntryRunner({"vals get pw": "hunter2"})
        with pytest.raises(ValueError) as excinfo:
            _resolve(
                feature_band={
                    "PW": EnvCommandEntry(command="vals get pw", exports=("OTHER",)),
                    "U": "${PW}",
                },
                resolve_commands=True,
                runner=runner,
            )
        message = str(excinfo.value)
        assert "on its own line" not in message
        assert "'PW' is excluded by the command entry's `exports` allow-list" in message

    def test_workspace_entry_referencing_a_feature_command_key_is_blocked_by_restriction_not_unclaimed(
        self,
    ) -> None:
        """A workspace-band entry referencing a feature-band command entry's key can never
        see it, no matter how many rounds run — the workspace-band restriction is the real
        cause, not a claim ``DB``'s own command never made. The command genuinely produced
        ``DB`` here, so a diagnostic claiming otherwise would send the reader chasing the
        wrong entry.
        """
        runner = FakeCommandEntryRunner({"vals get db": "hunter2"})
        with pytest.raises(ValueError) as excinfo:
            _resolve(
                workspace_band={"WS_URL": "postgres://${DB}/db"},
                feature_band={"DB": EnvCommandEntry(command="vals get db")},
                resolve_commands=True,
                runner=runner,
            )
        message = str(excinfo.value)
        assert "undefined variable 'DB'" in message
        assert "was not present in the command's own output" not in message

    def test_a_deferred_shell_entry_with_no_missing_references_still_gets_a_line(self) -> None:
        """A ``shell = true`` entry withheld only by the multi-value ordering (never any
        missing reference of its own) must not vanish from the diagnostic just because it
        has no *missing reference* to report — otherwise the message understates how many
        entries are actually unresolved.

        ``MIGRATE``'s own reference (``WINTER_ENV``) is satisfied from the base scope, but
        it can never run: ``_a`` — itself ``shell = true`` and multi-value, so it is one of
        the deferred entries ``MIGRATE`` waits its ordered turn behind — is permanently
        unresolvable (``NOPE`` is undefined), so it never reaches its own turn and the
        multi-value ordering withholds every entry declared before it forever. A
        ``shell = false`` multi-value entry stuck the same way no longer withholds
        ``MIGRATE`` at all once it has had its one attempt for the round — see
        ``EnvBandResolverService._resolve_round``.
        """
        with pytest.raises(ValueError) as excinfo:
            _resolve(
                feature_band={
                    "MIGRATE": EnvCommandEntry(command="migrate --x=${WINTER_ENV}", shell=True),
                    "_a": EnvCommandEntry(command="vals get ${NOPE}", format=EnvCommandFormat.json, shell=True),
                },
                resolve_commands=True,
            )
        message = str(excinfo.value)
        assert "_a" in message
        assert "NOPE" in message
        assert "MIGRATE" in message


# ---------------------------------------------------------------------------
# Dead plumbing on the security-sensitive command path
# ---------------------------------------------------------------------------


class TestNoDeadGatedPlumbingInCommandExecution:
    def test_run_command_entry_takes_no_gated_placeholder_set(self) -> None:
        """``_run_command_entry`` only ever runs when ``resolve_commands`` is true,
        and gating only ever populates a placeholder when it is false — the two states
        are mutually exclusive, so a ``gated`` frozenset threaded into this method could
        never be non-empty. Pins that the dead parameter stays removed rather than
        silently reappearing.
        """
        import inspect

        params = inspect.signature(EnvBandResolverService._run_command_entry).parameters
        assert "gated" not in params


# ---------------------------------------------------------------------------
# Substitution-before-tokenizing is a documented decision, not an accident
# ---------------------------------------------------------------------------


class TestSubstitutionBeforeTokenizing:
    """Pins the substitution-before-tokenizing decision documented on ``EnvBandResolverService._run_command_entry``:
    ``${...}`` substitution happens on the whole command string before the runner
    tokenizes it, so a substituted value's own whitespace changes the resulting argv
    shape unless the operator quotes the reference in the declared command. Uses the
    real ``SubprocessCommandEntryRunner`` — a fake never tokenizes at all, so it cannot
    show this either way.
    """

    def test_an_unquoted_substituted_value_with_whitespace_splits_into_extra_argv(self) -> None:
        runner = SubprocessCommandEntryRunner(workspace_root=Path.cwd(), error_factory=RepoErrorFactory())
        result = EnvBandResolverService(runner=runner).resolve(
            workspace_band={},
            feature_band={
                "TOKEN": EnvCommandEntry(command="echo -n a b"),
                "COUNT": EnvCommandEntry(command='python3 -c "import sys; print(len(sys.argv) - 1)" ${TOKEN}'),
            },
            named_band={},
            named_band_label="env.alpha.vars",
            base_scope=FEATURE_BASE_SCOPE,
            resolve_commands=True,
        )
        # "a b" was not quoted in the declared command, so it became two argv items.
        assert result["COUNT"] == "2"

    def test_quoting_the_reference_in_the_declared_command_keeps_it_one_argv_item(self) -> None:
        runner = SubprocessCommandEntryRunner(workspace_root=Path.cwd(), error_factory=RepoErrorFactory())
        result = EnvBandResolverService(runner=runner).resolve(
            workspace_band={},
            feature_band={
                "TOKEN": EnvCommandEntry(command="echo -n a b"),
                "COUNT": EnvCommandEntry(command="""python3 -c 'import sys; print(len(sys.argv) - 1)' '${TOKEN}'"""),
            },
            named_band={},
            named_band_label="env.alpha.vars",
            base_scope=FEATURE_BASE_SCOPE,
            resolve_commands=True,
        )
        # Quoting the reference in the declared command protects it, as documented.
        assert result["COUNT"] == "1"
