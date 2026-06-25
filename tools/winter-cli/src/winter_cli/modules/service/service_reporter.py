from __future__ import annotations

import json
from typing import Any, Protocol

from winter_cli.core.cli_output_service import Cell, ICliOutputService
from winter_cli.modules.service.status_models import StatusDocument
from winter_cli.modules.service.status_parser import StatusDocumentParser

_STATE_STYLE: dict[str, str] = {
    "running": "green",
    "stopped": "red",
    "unknown": "dim",
}
_HEALTH_STYLE: dict[str, str] = {
    "healthy": "green",
    "unhealthy": "red",
    "unknown": "dim",
}


class IServiceReporter(Protocol):
    """Sink for service output events.

    Covers status rendering, log line emission, and diagnostic messages for the
    service dispatch, status, and logs services. Implementations own all
    formatting — services emit semantic events only.
    """

    def status_document(self, doc: StatusDocument, parser: StatusDocumentParser) -> None: ...
    def log_line(self, rendered: str) -> None: ...
    def no_services(self) -> None: ...
    def no_service_matched(self, token_list: str) -> None: ...
    def follow_multi_provider_error(self, provider_names: str) -> None: ...
    def status_parse_error(self, entrypoint: str, prefix: str, detail: str) -> None: ...
    def describe_parse_error(self, provider_name: str, detail: str) -> None: ...
    def timestamps_warning(self) -> None: ...
    def time_filter_warning(self) -> None: ...
    def no_match_diagnostic(self, token_list: str) -> None: ...
    def readiness_timeout(self, env: str, timeout_s: float, unhealthy: tuple[str, ...]) -> None: ...


class StreamServiceReporter:
    """Renders service output as human-readable lines."""

    def __init__(self, click: Any, cli_output: ICliOutputService) -> None:
        self._click = click
        self._cli_output = cli_output

    def status_document(self, doc: StatusDocument, parser: StatusDocumentParser) -> None:
        self._render_human(doc)

    def _render_human(self, doc: StatusDocument) -> None:
        if not doc.envs:
            self.no_services()
            return

        headers = ["SERVICE", "STATE", "HEALTH", "PORTS", "SINCE", "HANDLE"]
        first = True
        for env in doc.envs:
            if not first:
                self._click.echo("")
            first = False

            session_str = env.session if env.session is not None else "-"
            port_base_str = str(env.port_base) if env.port_base is not None else "-"
            header_text = f"{env.env}  session={session_str}  port_base={port_base_str}"
            self._click.echo(self._cli_output.style(header_text, "bold"))

            rows: list[list[str | Cell]] = []
            for svc in env.services:
                ports_str = ", ".join(str(p) for p in svc.ports) if svc.ports else "-"
                state_cell = Cell.of(svc.state, _STATE_STYLE.get(svc.state, "dim"))
                health_cell = Cell.of(svc.health, _HEALTH_STYLE.get(svc.health, "dim"))
                rows.append(
                    [
                        svc.name,
                        state_cell,
                        health_cell,
                        ports_str,
                        svc.since or "-",
                        svc.handle or "-",
                    ]
                )

            for line in self._cli_output.render_table(rows, headers=headers):
                self._click.echo(line)

    def log_line(self, rendered: str) -> None:
        self._click.echo(rendered)

    def no_services(self) -> None:
        self._click.echo("no services")

    def no_service_matched(self, token_list: str) -> None:
        self._click.echo(f"no service matched {token_list}", err=True)

    def follow_multi_provider_error(self, provider_names: str) -> None:
        self._click.echo(
            f"error: --follow (-f) cannot span multiple service providers "
            f"({provider_names}). "
            f"Narrow your selection to services owned by a single provider, "
            f"or omit -f to merge non-follow logs across providers.",
            err=True,
        )

    def status_parse_error(self, entrypoint: str, prefix: str, detail: str) -> None:
        self._click.echo(
            f"error: orchestrator at {entrypoint} (prefix: {prefix!r}) "
            f"does not emit the structured status document required by the `winter service` contract "
            f"— ensure the extension is up to date. "
            f"Schema: ai/winter-cli/usage/service.md#status-wire-contract\n"
            f"Parse detail: {detail}",
            err=True,
        )

    def describe_parse_error(self, provider_name: str, detail: str) -> None:
        self._click.echo(
            f"warning: provider {provider_name!r} did not emit a valid describe document "
            f"and will be skipped for service ownership resolution. "
            f"Ensure the extension implements the describe action. "
            f"Detail: {detail}",
            err=True,
        )

    def timestamps_warning(self) -> None:
        self._click.echo(
            "warning: the orchestrator supplies no per-line timestamps — timestamp prefixes omitted for affected lines",
            err=True,
        )

    def time_filter_warning(self) -> None:
        self._click.echo(
            "warning: the orchestrator supplies no per-line timestamps — "
            "--since/--until filter is partial (applied only to lines with a ts field)",
            err=True,
        )

    def no_match_diagnostic(self, token_list: str) -> None:
        self._click.echo(f"no service matched {token_list}", err=True)

    def readiness_timeout(self, env: str, timeout_s: float, unhealthy: tuple[str, ...]) -> None:
        names = ", ".join(unhealthy)
        self._click.echo(
            f"error: timed out after {timeout_s:g}s waiting for {env} services to become healthy; "
            f"still unhealthy: {names}",
            err=True,
        )


class JsonServiceReporter:
    """Emits service status as JSON; other events go to stderr."""

    def __init__(self, click: Any, cli_output: ICliOutputService) -> None:
        self._click = click
        self._cli_output = cli_output

    def status_document(self, doc: StatusDocument, parser: StatusDocumentParser) -> None:
        self._click.echo(json.dumps(parser.to_json_obj(doc), indent=2))

    def log_line(self, rendered: str) -> None:
        self._click.echo(rendered)

    def no_services(self) -> None:
        # In JSON mode, status_document handles the empty-envs case
        pass

    def readiness_timeout(self, env: str, timeout_s: float, unhealthy: tuple[str, ...]) -> None:
        names = ", ".join(unhealthy)
        self._click.echo(
            f"error: timed out after {timeout_s:g}s waiting for {env} services to become healthy; "
            f"still unhealthy: {names}",
            err=True,
        )

    def no_service_matched(self, token_list: str) -> None:
        self._click.echo(f"no service matched {token_list}", err=True)

    def follow_multi_provider_error(self, provider_names: str) -> None:
        self._click.echo(
            f"error: --follow (-f) cannot span multiple service providers "
            f"({provider_names}). "
            f"Narrow your selection to services owned by a single provider, "
            f"or omit -f to merge non-follow logs across providers.",
            err=True,
        )

    def status_parse_error(self, entrypoint: str, prefix: str, detail: str) -> None:
        self._click.echo(
            f"error: orchestrator at {entrypoint} (prefix: {prefix!r}) "
            f"does not emit the structured status document required by the `winter service` contract "
            f"— ensure the extension is up to date. "
            f"Schema: ai/winter-cli/usage/service.md#status-wire-contract\n"
            f"Parse detail: {detail}",
            err=True,
        )

    def describe_parse_error(self, provider_name: str, detail: str) -> None:
        self._click.echo(
            f"warning: provider {provider_name!r} did not emit a valid describe document "
            f"and will be skipped for service ownership resolution. "
            f"Ensure the extension implements the describe action. "
            f"Detail: {detail}",
            err=True,
        )

    def timestamps_warning(self) -> None:
        self._click.echo(
            "warning: the orchestrator supplies no per-line timestamps — timestamp prefixes omitted for affected lines",
            err=True,
        )

    def time_filter_warning(self) -> None:
        self._click.echo(
            "warning: the orchestrator supplies no per-line timestamps — "
            "--since/--until filter is partial (applied only to lines with a ts field)",
            err=True,
        )

    def no_match_diagnostic(self, token_list: str) -> None:
        self._click.echo(f"no service matched {token_list}", err=True)
