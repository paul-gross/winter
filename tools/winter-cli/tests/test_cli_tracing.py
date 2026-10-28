"""The CLI boundary's command-span wiring, driven through a fake `ICommandTracer`.

The fake is bound in a `Container` subclass, so these tests exercise the real
`cli()` -> `LazyGroup` -> group-callback path without OpenTelemetry. Test commands
are reached through the real `_cli_group` by swapping its lazy-subcommand map.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from contextlib import AbstractContextManager

import click
import pytest
from click.testing import CliRunner
from dependency_injector import providers

from winter_cli import cli as cli_module
from winter_cli.cli import LazySubcommand, _cli_group, _exit_error_type, _tracing_settings_from_environment
from winter_cli.cli_context import CliContext, cli_ctx
from winter_cli.container import Container
from winter_cli.core.internal.noop_command_tracer import NoopCommandTracer
from winter_cli.core.tracing import AttributeValue, ICommandTracer, IOperationHandle, TracingSettings
from winter_cli.modules.workspace.models import RepoError


class FakeCommandTracer:
    """Records every call; implements the whole `ICommandTracer` surface."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def inject(self, env: dict[str, str]) -> None:
        self.events.append(("inject", None))

    def annotate_env(self, env_name: str) -> None:
        self.events.append(("annotate_env", env_name))

    def start_command(self, command_path: str) -> None:
        self.events.append(("start", command_path))

    def end_command(self, error_type: str | None) -> None:
        self.events.append(("end", error_type))

    def start_background_export(self) -> None:
        self.events.append(("start_background_export", None))

    def session_root(self, name: str) -> AbstractContextManager[IOperationHandle]:
        self.events.append(("session_root", name))
        return NoopCommandTracer().operation(name)

    def operation(
        self, name: str, attributes: Mapping[str, AttributeValue] | None = None
    ) -> AbstractContextManager[IOperationHandle]:
        self.events.append(("operation", name))
        return NoopCommandTracer().operation(name)

    def export(self) -> None:
        self.events.append(("export", None))

    def named(self, kind: str) -> list[object]:
        return [value for event, value in self.events if event == kind]


def _conforms_fake_command_tracer(x: FakeCommandTracer) -> ICommandTracer:
    return x


def _container_with(tracer: FakeCommandTracer) -> Container:
    class FakeTracerContainer(Container):
        command_tracer = providers.Object(tracer)

    return FakeTracerContainer()


# ── Test commands, reached through the real `_cli_group` ─────────────────────

seen: dict[str, object] = {}


@click.command("ok")
@click.pass_context
def ok_cmd(ctx: click.Context) -> None:
    seen["container"] = cli_ctx(ctx).container
    seen["launch_time_env"] = os.environ.get("WINTER_LAUNCH_TIME")


@click.command("sys-exit")
@click.argument("code", type=int)
def sys_exit_cmd(code: int) -> None:
    sys.exit(code)


@click.command("sys-exit-none")
def sys_exit_none_cmd() -> None:
    sys.exit(None)


@click.command("ctx-exit")
@click.argument("code", type=int)
@click.pass_context
def ctx_exit_cmd(ctx: click.Context, code: int) -> None:
    ctx.exit(code)


@click.command("repo-error")
def repo_error_cmd() -> None:
    raise RepoError("clone failed", subcommand="clone", stderr="fatal: boom")


@click.command("boom")
def boom_cmd() -> None:
    raise RuntimeError("secret detail")


@click.command("usage-error")
def usage_error_cmd() -> None:
    raise click.UsageError("bad usage")


@click.group("nested")
def nested_group() -> None:
    pass


@nested_group.command("leaf")
@click.option("--flag", is_flag=True)
def nested_leaf_cmd(flag: bool) -> None:
    pass


_TEST_COMMANDS = {
    k: LazySubcommand(v, "")
    for k, v in {
        "ok": "tests.test_cli_tracing:ok_cmd",
        "sys-exit": "tests.test_cli_tracing:sys_exit_cmd",
        "sys-exit-none": "tests.test_cli_tracing:sys_exit_none_cmd",
        "ctx-exit": "tests.test_cli_tracing:ctx_exit_cmd",
        "repo-error": "tests.test_cli_tracing:repo_error_cmd",
        "boom": "tests.test_cli_tracing:boom_cmd",
        "usage-error": "tests.test_cli_tracing:usage_error_cmd",
        "nested": "tests.test_cli_tracing:nested_group",
    }.items()
}


@pytest.fixture(autouse=True)
def _no_ambient_tracing_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own tracing variables out of every test in this module."""
    for name in ("WINTER_OTEL_EXPORTER_OTLP_ENDPOINT", "WINTER_OTEL_EXPORTER_OTLP_HEADERS", "OTEL_SDK_DISABLED"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def real_tree_tracer() -> FakeCommandTracer:
    """A fake tracer for tests that resolve names against the real command tree."""
    return FakeCommandTracer()


@pytest.fixture
def tracer(monkeypatch: pytest.MonkeyPatch) -> FakeCommandTracer:
    """A fake tracer bound into every Container `cli()` builds; the real command map is swapped for test commands."""
    fake = FakeCommandTracer()
    built: list[Container] = []

    def build_container() -> Container:
        container = _container_with(fake)
        built.append(container)
        return container

    monkeypatch.setattr("winter_cli.container.Container", build_container)
    monkeypatch.setattr(
        "winter_cli.cli.ensure_ssh_keepalives",
        lambda: None,
    )
    monkeypatch.setattr(_cli_group, "_lazy_subcommands", _TEST_COMMANDS)
    seen.clear()
    seen["built"] = built
    return fake


def _run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int | BaseException:
    """Run `cli()` as the process entrypoint; return its exit code (0 on a plain return) or the escaping exception."""
    monkeypatch.setattr(sys, "argv", ["winter", *argv])
    try:
        cli_module.cli()
    except SystemExit as exc:
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:
        return exc
    return 0


# ── Span naming ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["provision", "alpha"], "winter provision"),
        (["ws", "init", "alpha"], "winter ws init"),
        (["service", "up", "alpha"], "winter service up"),
        (["service", "start", "alpha"], "winter service start"),
        (["ws", "status", "--json"], "winter ws status"),
        (["ws", "--help"], "winter ws"),
        (["repo", "list"], "winter repo list"),
        (["doctor"], "winter doctor"),
    ],
)
def test_span_is_named_by_the_full_click_path(
    args: list[str], expected: str, real_tree_tracer: FakeCommandTracer
) -> None:
    ctx = click.Context(_cli_group, obj=CliContext.of(_container_with(real_tree_tracer)))

    _cli_group.resolve_command(ctx, args)

    assert real_tree_tracer.named("start") == [expected]


def test_unknown_command_opens_no_span(real_tree_tracer: FakeCommandTracer) -> None:
    ctx = click.Context(_cli_group, obj=CliContext.of(_container_with(real_tree_tracer)))

    with pytest.raises(click.UsageError):
        _cli_group.resolve_command(ctx, ["no-such-command"])

    assert real_tree_tracer.events == []


def test_resilient_resolution_opens_no_span(real_tree_tracer: FakeCommandTracer) -> None:
    """Shell completion resolves commands with `resilient_parsing`; that is not a command run."""
    ctx = click.Context(_cli_group, obj=CliContext.of(_container_with(real_tree_tracer)), resilient_parsing=True)

    _cli_group.resolve_command(ctx, ["ws", "init"])

    assert real_tree_tracer.events == []


def test_nested_path_stops_at_options_and_unknown_names(tracer: FakeCommandTracer) -> None:
    ctx = click.Context(_cli_group, obj=CliContext.of(_container_with(tracer)))

    _cli_group.resolve_command(ctx, ["nested", "leaf", "--flag"])
    ctx.meta.clear()
    _cli_group.resolve_command(ctx, ["nested", "nope"])

    assert tracer.named("start") == ["winter nested leaf", "winter nested"]


# ── export() once per exit path, exit code unchanged ─────────────────────────


@pytest.mark.parametrize(
    ("argv", "expected_exit"),
    [
        (["ok"], 0),
        (["sys-exit", "3"], 3),
        (["sys-exit", "0"], 0),
        (["sys-exit-none"], 0),
        (["ctx-exit", "2"], 2),
        (["ctx-exit", "0"], 0),
        (["repo-error"], 1),
        (["usage-error"], 2),
    ],
)
def test_export_runs_once_and_the_exit_code_is_unchanged(
    argv: list[str], expected_exit: int, tracer: FakeCommandTracer, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run_cli(monkeypatch, *argv) == expected_exit

    assert tracer.named("export") == [None]
    assert tracer.events[-1] == ("export", None)
    assert len(tracer.named("start")) == 1
    assert len(tracer.named("end")) == 1


def test_export_runs_once_for_an_uncaught_exception(tracer: FakeCommandTracer, monkeypatch: pytest.MonkeyPatch) -> None:
    outcome = _run_cli(monkeypatch, "boom")

    assert isinstance(outcome, RuntimeError)
    assert tracer.events == [("start", "winter boom"), ("end", "RuntimeError"), ("export", None)]


def test_no_container_is_built_when_no_command_runs(tracer: FakeCommandTracer, monkeypatch: pytest.MonkeyPatch) -> None:
    """The container is built on first need; a run that never reaches a command has no tracer to export."""
    assert _run_cli(monkeypatch, "no-such-command") == 2

    assert tracer.events == []
    assert seen["built"] == []


# ── Background export belongs to the dashboard alone ─────────────────────────


@pytest.mark.parametrize(
    "argv",
    [["ok"], ["sys-exit", "1"], ["repo-error"], ["boom"], ["nested", "leaf"], ["no-such-command"]],
)
def test_a_one_shot_command_never_starts_background_export(
    argv: list[str], tracer: FakeCommandTracer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_cli(monkeypatch, *argv)

    assert tracer.named("start_background_export") == []


def test_the_dashboard_starts_background_export_before_the_app_runs_and_exports_after_it(
    tracer: FakeCommandTracer, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _App:
        def __init__(self, container: Container, source_override: str | None = None) -> None:
            pass

        def run(self) -> None:
            tracer.events.append(("app_run", None))

    monkeypatch.setattr("winter_cli.modules.tui.app.WinterDashboardApp", _App)
    monkeypatch.setitem(_TEST_COMMANDS, "dashboard", LazySubcommand("winter_cli.modules.tui.command:dashboard", ""))

    assert _run_cli(monkeypatch, "dashboard") == 0

    assert [event for event, _ in tracer.events] == [
        "start",
        "start_background_export",
        "app_run",
        "end",
        "export",
    ]
    assert tracer.named("start") == ["winter dashboard"]


# ── Exit semantics (a zero exit is success; anything else is an error type) ──


@pytest.mark.parametrize(
    ("argv", "expected_error_type"),
    [
        (["ok"], None),
        (["sys-exit", "0"], None),
        (["sys-exit-none"], None),
        (["ctx-exit", "0"], None),
        (["sys-exit", "1"], "SystemExit"),
        (["ctx-exit", "2"], "Exit"),
        (["repo-error"], "RepoError"),
        (["usage-error"], "UsageError"),
        (["boom"], "RuntimeError"),
    ],
)
def test_span_ends_with_the_exit_semantics(
    argv: list[str], expected_error_type: str | None, tracer: FakeCommandTracer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_cli(monkeypatch, *argv)

    assert tracer.named("end") == [expected_error_type]


def test_error_type_is_the_class_name_only() -> None:
    assert _exit_error_type(RuntimeError("secret detail")) == "RuntimeError"
    assert _exit_error_type(SystemExit("message")) == "SystemExit"
    assert _exit_error_type(KeyboardInterrupt()) == "KeyboardInterrupt"
    assert _exit_error_type(SystemExit(0)) is None
    assert _exit_error_type(SystemExit(None)) is None
    assert _exit_error_type(click.exceptions.Exit(0)) is None
    assert _exit_error_type(click.exceptions.Exit(5)) == "Exit"
    assert _exit_error_type(None) is None


# ── Container adoption ───────────────────────────────────────────────────────


def test_the_group_callback_adopts_the_boundarys_container(
    tracer: FakeCommandTracer, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run_cli(monkeypatch, "ok") == 0

    built = seen["built"]
    assert isinstance(built, list)
    assert len(built) == 1
    assert seen["container"] is built[0]


def test_a_click_tree_entered_without_a_container_builds_its_own_and_traces_nothing(
    tracer: FakeCommandTracer,
) -> None:
    result = CliRunner().invoke(_cli_group, ["ok"])

    assert result.exit_code == 0
    assert tracer.events == []
    built = seen["built"]
    assert isinstance(built, list)
    assert len(built) == 1
    assert seen["container"] is built[0]  # the group callback's own, built through the patched `Container`


# ── WINTER_LAUNCH_TIME ───────────────────────────────────────────────────────


def test_launch_time_is_read_once_and_removed_from_the_environment(
    tracer: FakeCommandTracer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WINTER_LAUNCH_TIME", "1.5")

    assert _run_cli(monkeypatch, "ok") == 0

    assert seen["launch_time_env"] is None
    assert "WINTER_LAUNCH_TIME" not in os.environ


def test_boundary_hands_the_parsed_launch_time_to_the_container(
    tracer: FakeCommandTracer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WINTER_LAUNCH_TIME", "1.500000")
    captured: list[TracingSettings] = []
    monkeypatch.setattr(_cli_group, "invoke", lambda ctx: captured.append(cli_ctx(ctx).container.tracing_settings()))

    _run_cli(monkeypatch, "ok")

    assert captured == [TracingSettings(launch_time_ns=1_500_000_000)]


def test_settings_from_environment_without_a_launch_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WINTER_LAUNCH_TIME", raising=False)

    assert _tracing_settings_from_environment() == TracingSettings(launch_time_ns=None)


@pytest.mark.parametrize("raw", ["garbage", "99999999999.0", ""])
def test_settings_from_environment_fall_back_for_garbage_and_future_values(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WINTER_LAUNCH_TIME", raw)

    assert _tracing_settings_from_environment() == TracingSettings(launch_time_ns=None)
    assert "WINTER_LAUNCH_TIME" not in os.environ


def test_settings_from_environment_accept_a_comma_decimal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINTER_LAUNCH_TIME", "1700000000,250000")

    assert _tracing_settings_from_environment() == TracingSettings(launch_time_ns=1_700_000_000_250_000_000)


def test_settings_from_environment_read_the_opt_in_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WINTER_LAUNCH_TIME", raising=False)
    monkeypatch.setenv("WINTER_OTEL_EXPORTER_OTLP_ENDPOINT", " http://localhost:4318 ")
    monkeypatch.setenv("WINTER_OTEL_EXPORTER_OTLP_HEADERS", "x-token=abc")
    monkeypatch.setenv("OTEL_SDK_DISABLED", "TRUE")

    settings = _tracing_settings_from_environment()

    assert settings == TracingSettings(
        otlp_endpoint="http://localhost:4318", otlp_headers="x-token=abc", sdk_disabled=True
    )
    assert not settings.enabled
    # The endpoint and headers describe the whole process tree, so children keep them.
    assert os.environ["WINTER_OTEL_EXPORTER_OTLP_ENDPOINT"] == " http://localhost:4318 "


def test_generic_exporter_endpoint_does_not_switch_tracing_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WINTER_LAUNCH_TIME", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

    settings = _tracing_settings_from_environment()

    assert settings.otlp_endpoint is None
    assert not settings.enabled


def test_a_blank_endpoint_leaves_tracing_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WINTER_LAUNCH_TIME", raising=False)
    monkeypatch.setenv("WINTER_OTEL_EXPORTER_OTLP_ENDPOINT", "  ")

    assert not _tracing_settings_from_environment().enabled
