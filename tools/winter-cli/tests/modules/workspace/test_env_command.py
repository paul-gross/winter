"""Tests for ``winter env <scope>`` command.

Covers:
- Feature-env scope: prints export KEY=value lines for WINTER_* vars + [env.vars] entries.
- Workspace scope: prints the workspace env (WINTER_ENV=workspace, index 0).
- Error path: EnvProvisionerService raises ValueError → prints to stderr, exits 1.
- Unknown scope: scope not in registry and not "workspace" → exits 1 with diagnostic.
- Registered scope: scope in registry → exits 0.
- ``--resolve`` gate: defaults to ``resolve_commands=False``; the flag forwards
  ``resolve_commands=True``. A command failure surfacing as ``RepoError`` under
  ``--resolve`` is exercised end to end (real subprocess) in ``test_cli.py``'s
  ``TestEnvResolveGate`` — not re-tested here with a fake.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from click.testing import CliRunner, Result

from winter_cli.cli_context import CliContext
from winter_cli.modules.workspace.env_command import env_cmd

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeProvisioner:
    """Fake IEnvProvisioner — records every ``resolve_commands`` value it is called with."""

    def __init__(
        self,
        responses: dict[str, dict[str, str]] | None = None,
        error_scope: str | None = None,
    ) -> None:
        self._responses = responses or {}
        self._error_scope = error_scope
        self.resolve_commands_calls: list[bool] = []

    def compute(self, scope: str, *, resolve_commands: bool) -> dict[str, str]:
        self.resolve_commands_calls.append(resolve_commands)
        if scope == self._error_scope:
            raise ValueError(f"undefined variable 'BAD' in scope {scope!r}")
        return self._responses.get(
            scope,
            {
                "WINTER_ENV": scope,
                "WINTER_ENV_INDEX": "1",
                "WINTER_PORT_BASE": "4020",
                "WINTER_WORKSPACE_PORT_BASE": "4000",
            },
        )


def _make_ctx(
    provisioner_responses: dict[str, dict[str, str]] | None = None,
    error_scope: str | None = None,
    registered_envs: dict[str, int] | None = None,
    provisioner: _FakeProvisioner | None = None,
) -> CliContext:
    """Build a CliContext whose container returns fake provisioner and registry.

    ``registered_envs`` controls which scopes the fake registry reports as
    assigned.  Defaults to ``{"alpha": 1}`` so existing tests that invoke with
    scope ``"alpha"`` pass the existence check without modification.

    ``provisioner``, when given, is returned verbatim instead of a fresh
    ``_FakeProvisioner`` — used by tests that need to assert on
    ``resolve_commands`` calls after invoking.
    """
    if registered_envs is None:
        registered_envs = {"alpha": 1}

    registry_mock = MagicMock()
    registry_mock.all_assignments.return_value = dict(registered_envs)

    container = MagicMock()
    container.env_provisioner.return_value = provisioner or _FakeProvisioner(provisioner_responses, error_scope)
    container.env_index_registry.return_value = registry_mock
    return CliContext(container=container)


def _invoke(scope: str, ctx: CliContext | None = None, *extra_args: str) -> Result:
    runner = CliRunner()
    if ctx is None:
        ctx = _make_ctx()
    return runner.invoke(env_cmd, [scope, *extra_args], obj=ctx, catch_exceptions=False)


# ---------------------------------------------------------------------------
# Feature-env scope output
# ---------------------------------------------------------------------------


def test_env_prints_winter_env_for_alpha() -> None:
    """winter env alpha includes export WINTER_ENV=alpha on stdout."""
    result = _invoke("alpha")
    assert result.exit_code == 0
    assert "export WINTER_ENV=alpha" in result.output


def test_env_prints_all_four_base_vars() -> None:
    """winter env alpha prints export lines for WINTER_ENV / INDEX / PORT_BASE / WORKSPACE_PORT_BASE."""
    result = _invoke("alpha")
    assert result.exit_code == 0
    assert "export WINTER_ENV=alpha" in result.output
    assert "export WINTER_ENV_INDEX=" in result.output
    assert "export WINTER_PORT_BASE=" in result.output
    assert "export WINTER_WORKSPACE_PORT_BASE=" in result.output


def test_env_prints_custom_env_vars() -> None:
    """Custom [env.vars] entries appear in the output as export lines."""
    ctx = _make_ctx(
        provisioner_responses={
            "alpha": {
                "WINTER_ENV": "alpha",
                "WINTER_ENV_INDEX": "1",
                "WINTER_PORT_BASE": "4020",
                "WINTER_WORKSPACE_PORT_BASE": "4000",
                "MY_APP_PORT": "4031",
            }
        }
    )
    result = _invoke("alpha", ctx=ctx)
    assert result.exit_code == 0
    assert "export MY_APP_PORT=" in result.output
    assert "4031" in result.output


def test_env_output_uses_export_key_equals_value_format() -> None:
    """Each output line is 'export KEY=<shell-quoted-value>' (sourceable)."""
    result = _invoke("alpha")
    assert result.exit_code == 0
    for line in result.output.strip().splitlines():
        assert line.startswith("export "), f"expected export prefix, got: {line!r}"
        rest = line[len("export ") :]
        assert "=" in rest


# ---------------------------------------------------------------------------
# Workspace scope output
# ---------------------------------------------------------------------------


def test_env_workspace_prints_workspace_scope() -> None:
    """winter env workspace prints export WINTER_ENV=workspace."""
    ctx = _make_ctx(
        provisioner_responses={
            "workspace": {
                "WINTER_ENV": "workspace",
                "WINTER_ENV_INDEX": "0",
                "WINTER_PORT_BASE": "4000",
                "WINTER_WORKSPACE_PORT_BASE": "4000",
            }
        }
    )
    result = _invoke("workspace", ctx=ctx)
    assert result.exit_code == 0
    assert "export WINTER_ENV=workspace" in result.output
    assert "export WINTER_ENV_INDEX=0" in result.output
    assert "export WINTER_PORT_BASE=4000" in result.output


# ---------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------


def test_env_error_exits_nonzero() -> None:
    """When the provisioner raises ValueError, the command exits with code 1."""
    ctx = _make_ctx(error_scope="alpha")
    runner = CliRunner()
    result = runner.invoke(env_cmd, ["alpha"], obj=ctx, catch_exceptions=False)
    assert result.exit_code == 1


def test_env_error_no_stdout_on_error() -> None:
    """On error, nothing is printed to stdout (error goes to stderr)."""
    ctx = _make_ctx(error_scope="alpha")
    runner = CliRunner()
    result = runner.invoke(env_cmd, ["alpha"], obj=ctx, catch_exceptions=False)
    assert result.exit_code == 1
    # Output should not contain any export KEY=value lines on error.
    assert not any(line.startswith("export ") for line in result.output.splitlines())


# ---------------------------------------------------------------------------
# Unknown scope → exit 1
# ---------------------------------------------------------------------------


def test_env_unknown_scope_exits_1() -> None:
    """winter env <unregistered> exits 1 when the scope is not in the registry."""
    ctx = _make_ctx(registered_envs={})
    runner = CliRunner()
    result = runner.invoke(env_cmd, ["bogus"], obj=ctx, catch_exceptions=False)
    assert result.exit_code == 1


def test_env_unknown_scope_no_export_lines() -> None:
    """Unknown scope produces no export lines on stdout."""
    ctx = _make_ctx(registered_envs={})
    runner = CliRunner()
    result = runner.invoke(env_cmd, ["bogus"], obj=ctx, catch_exceptions=False)
    assert not any(line.startswith("export ") for line in result.output.splitlines())


def test_env_registered_scope_exits_0() -> None:
    """A registered feature-env scope exits 0."""
    ctx = _make_ctx(registered_envs={"feature-xyz": 5})
    result = _invoke("feature-xyz", ctx=ctx)
    assert result.exit_code == 0


def test_env_workspace_scope_always_exits_0() -> None:
    """The 'workspace' scope is always valid regardless of registry contents."""
    ctx = _make_ctx(
        provisioner_responses={
            "workspace": {
                "WINTER_ENV": "workspace",
                "WINTER_ENV_INDEX": "0",
                "WINTER_PORT_BASE": "4000",
                "WINTER_WORKSPACE_PORT_BASE": "4000",
            }
        },
        registered_envs={},
    )
    result = _invoke("workspace", ctx=ctx)
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# --resolve gate
# ---------------------------------------------------------------------------


def test_env_defaults_to_resolve_commands_false() -> None:
    """Without --resolve, compute() is called with resolve_commands=False."""
    provisioner = _FakeProvisioner()
    ctx = _make_ctx(provisioner=provisioner)
    result = _invoke("alpha", ctx)
    assert result.exit_code == 0
    assert provisioner.resolve_commands_calls == [False]


def test_env_resolve_flag_forwards_resolve_commands_true() -> None:
    """--resolve forwards resolve_commands=True to compute()."""
    provisioner = _FakeProvisioner()
    ctx = _make_ctx(provisioner=provisioner)
    result = _invoke("alpha", ctx, "--resolve")
    assert result.exit_code == 0
    assert provisioner.resolve_commands_calls == [True]
