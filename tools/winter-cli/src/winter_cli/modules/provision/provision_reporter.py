from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Protocol seam
# ---------------------------------------------------------------------------


class IProvisionReporter(Protocol):
    """Combined sink for provision execution events and provision-level events.

    Covers the ``IProvisionOutputSink`` contract from Phase 3 (the four
    ``execution_*`` methods) plus the provision-level lifecycle events fired
    by ``ProvisionService``.  Concrete reporters implement all methods; the
    execution service is injected with ``IProvisionOutputSink`` which is a
    strict subset of this interface.
    """

    # ── IProvisionOutputSink methods (Phase 3 contract) ──────────────────

    def execution_started(self, label: str, action: str, cwd: Path) -> None: ...
    def execution_output_line(self, label: str, line: str) -> None: ...
    def execution_completed(self, label: str, action: str, exit_code: int) -> None: ...
    def execution_error(self, label: str, error: str) -> None: ...

    # ── Provision-level lifecycle events ─────────────────────────────────

    def provision_started(self, env: str, subtargets: list[str]) -> None: ...
    def subtarget_started(self, subtarget: str) -> None: ...
    def no_handlers(self, subtarget: str) -> None: ...
    def handler_result(
        self,
        subtarget: str,
        scope: str,
        source: str,
        action: str,
        service_check: str | None,
        runs: list[dict[str, Any]],
        exit_status: int,
    ) -> None: ...
    def handler_warn(self, subtarget: str, scope: str, source: str, message: str) -> None: ...
    def provision_finished(self, status: str, aborted_at: str | None) -> None: ...

    # ── Dry-run plan event ────────────────────────────────────────────────

    def plan_handler(
        self,
        subtarget: str,
        scope: str,
        source: str,
        script: str,
        action: str,
        required_services: list[str],
        service_check_preview: str | None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# StreamProvisionReporter
# ---------------------------------------------------------------------------


class StreamProvisionReporter:
    """Human-readable reporter for ``winter provision``.

    Prints a line-by-line stream suitable for a terminal.  All output goes to
    stdout (``err=False``) so it can be redirected; only banner-level errors
    are directed to stderr.
    """

    def __init__(self, click: Any) -> None:
        self._click = click

    # ── IProvisionOutputSink ──────────────────────────────────────────────

    def execution_started(self, label: str, action: str, cwd: Path) -> None:
        self._click.echo(f"  [{action}] {label} (cwd={cwd})")

    def execution_output_line(self, label: str, line: str) -> None:
        self._click.echo(f"    {line}")

    def execution_completed(self, label: str, action: str, exit_code: int) -> None:
        glyph = "✓" if exit_code == 0 else "✗"
        self._click.echo(f"  {glyph} {label} → exit {exit_code}")

    def execution_error(self, label: str, error: str) -> None:
        self._click.echo(f"  ✗ {label} — error: {error}", err=True)

    # ── Provision-level lifecycle ─────────────────────────────────────────

    def provision_started(self, env: str, subtargets: list[str]) -> None:
        chain = " → ".join(subtargets)
        self._click.echo(f"Provisioning {env!r}: {chain}")

    def subtarget_started(self, subtarget: str) -> None:
        self._click.echo(f"\n[{subtarget}]")

    def no_handlers(self, subtarget: str) -> None:
        self._click.echo(f"  (no handlers declared for {subtarget!r})")

    def handler_result(
        self,
        subtarget: str,
        scope: str,
        source: str,
        action: str,
        service_check: str | None,
        runs: list[dict[str, Any]],
        exit_status: int,
    ) -> None:
        # The per-run output was already streamed via execution_started /
        # execution_output_line / execution_completed — no additional line
        # needed here for the stream reporter.
        pass

    def handler_warn(self, subtarget: str, scope: str, source: str, message: str) -> None:
        self._click.echo(f"  ! {source}/{subtarget}[{scope}] — {message}")

    def provision_finished(self, status: str, aborted_at: str | None) -> None:
        if status == "ok":
            self._click.echo(self._click.style("\nDone.", fg="green"))
        elif status == "aborted":
            self._click.echo(
                self._click.style(
                    f"\nAborted at sub-target {aborted_at!r}.",
                    fg="red",
                ),
                err=True,
            )
        else:
            self._click.echo(
                self._click.style("\nError.", fg="red"),
                err=True,
            )

    def plan_handler(
        self,
        subtarget: str,
        scope: str,
        source: str,
        script: str,
        action: str,
        required_services: list[str],
        service_check_preview: str | None,
    ) -> None:
        svc_info = ""
        if required_services:
            svc_info = f" [requires: {', '.join(required_services)}]"
        self._click.echo(f"  would {action}: {source}/{subtarget}[{scope}] → {script}{svc_info}")


# ---------------------------------------------------------------------------
# JsonProvisionReporter
# ---------------------------------------------------------------------------


class JsonProvisionReporter:
    """NDJSON reporter for ``winter provision --json``.

    Each event is a JSON object on its own line.  Thread-safe (serialisation
    happens under a lock so concurrent execution-output lines don't interleave
    partial JSON).

    --json event schema
    -------------------
    ``{"type":"started", "env":str, "subtargets":[str,...]}``
        Emitted once at the start of the run.

    ``{"type":"subtarget_started", "subtarget":str}``
        Emitted when a sub-target's handlers begin.

    ``{"type":"no_handlers", "subtarget":str}``
        Emitted when a sub-target has no declared handlers.

    ``{"type":"execution_started", "label":str, "action":str, "cwd":str}``
        Emitted immediately before a script subprocess is launched.

    ``{"type":"execution_output_line", "label":str, "line":str}``
        Emitted for each line of stdout/stderr from the script.

    ``{"type":"execution_completed", "label":str, "action":str, "exit_status":int}``
        Emitted after the subprocess exits.

    ``{"type":"execution_error", "label":str, "error":str}``
        Emitted when a script cannot be launched.

    ``{"type":"handler_result", "subtarget":str, "scope":str, "source":str,
       "action":str, "service_check":str|null,
       "runs":[{"cwd":str,"exit_status":int},...], "exit_status":int}``
        Emitted after all runs for one handler complete.
        ``service_check`` is null in Phase 4 (Phase 5 fills it).
        ``exit_status`` is 0 when all runs exited 0, else non-zero.

    ``{"type":"handler_warn", "subtarget":str, "scope":str, "source":str,
       "message":str}``
        Emitted instead of a handler_result when a handler is skipped/degraded
        with a warning (e.g. destroy with no destroy script).

    ``{"type":"finished", "status":"ok"|"aborted"|"error",
       "aborted_at":str|null}``
        Emitted once at the end.  ``aborted_at`` is the sub-target name when
        ``status`` is ``"aborted"``, otherwise null.

    ``{"type":"plan_handler", "would_run":true, "subtarget":str, "scope":str,
       "source":str, "script":str, "action":str,
       "required_services":[str,...], "service_check_preview":str|null}``
        Emitted in dry-run mode instead of ``execution_started`` / ``handler_result``.
        One event per handler in plan order.  ``would_run`` is always ``true``
        so agents can distinguish plan events from real-run events.
        ``service_check_preview`` describes the service-check that WOULD run:
        ``null`` when no ``required_services`` are declared, or a scope string
        such as ``"workspace"`` / the env name indicating which scope would be
        started if needed.
    """

    def __init__(self, click: Any) -> None:
        self._click = click
        self._lock = threading.Lock()

    def _emit(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self._click.echo(json.dumps(payload))

    # ── IProvisionOutputSink ──────────────────────────────────────────────

    def execution_started(self, label: str, action: str, cwd: Path) -> None:
        self._emit({"type": "execution_started", "label": label, "action": action, "cwd": str(cwd)})

    def execution_output_line(self, label: str, line: str) -> None:
        self._emit({"type": "execution_output_line", "label": label, "line": line})

    def execution_completed(self, label: str, action: str, exit_code: int) -> None:
        self._emit({"type": "execution_completed", "label": label, "action": action, "exit_status": exit_code})

    def execution_error(self, label: str, error: str) -> None:
        self._emit({"type": "execution_error", "label": label, "error": error})

    # ── Provision-level lifecycle ─────────────────────────────────────────

    def provision_started(self, env: str, subtargets: list[str]) -> None:
        self._emit({"type": "started", "env": env, "subtargets": subtargets})

    def subtarget_started(self, subtarget: str) -> None:
        self._emit({"type": "subtarget_started", "subtarget": subtarget})

    def no_handlers(self, subtarget: str) -> None:
        self._emit({"type": "no_handlers", "subtarget": subtarget})

    def handler_result(
        self,
        subtarget: str,
        scope: str,
        source: str,
        action: str,
        service_check: str | None,
        runs: list[dict[str, Any]],
        exit_status: int,
    ) -> None:
        self._emit(
            {
                "type": "handler_result",
                "subtarget": subtarget,
                "scope": scope,
                "source": source,
                "action": action,
                "service_check": service_check,
                "runs": runs,
                "exit_status": exit_status,
            }
        )

    def handler_warn(self, subtarget: str, scope: str, source: str, message: str) -> None:
        self._emit(
            {
                "type": "handler_warn",
                "subtarget": subtarget,
                "scope": scope,
                "source": source,
                "message": message,
            }
        )

    def provision_finished(self, status: str, aborted_at: str | None) -> None:
        self._emit({"type": "finished", "status": status, "aborted_at": aborted_at})

    def plan_handler(
        self,
        subtarget: str,
        scope: str,
        source: str,
        script: str,
        action: str,
        required_services: list[str],
        service_check_preview: str | None,
    ) -> None:
        self._emit(
            {
                "type": "plan_handler",
                "would_run": True,
                "subtarget": subtarget,
                "scope": scope,
                "source": source,
                "script": script,
                "action": action,
                "required_services": required_services,
                "service_check_preview": service_check_preview,
            }
        )
