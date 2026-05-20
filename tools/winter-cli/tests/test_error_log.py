from __future__ import annotations

from winter_cli.modules.tui.error_log import ErrorLogService
from winter_cli.modules.workspace.models import RepoError


def _err(message: str = "boom", *, subcommand: str = "fetch", args: tuple[str, ...] = ("origin",), cwd: str = "/tmp") -> RepoError:
    return RepoError(message, subcommand=subcommand, args=args, cwd=cwd, exit_code=128, stderr="oops")


def test_record_appends_entry():
    svc = ErrorLogService()
    entry, _ = svc.record(location="WorkspaceScreen.refresh", exc=_err())
    assert entry.location == "WorkspaceScreen.refresh"
    assert entry.subcommand == "fetch"
    assert entry.args == ("origin",)
    assert entry.stderr == "oops"
    assert len(svc.entries()) == 1


def test_should_notify_dedupes_same_fingerprint():
    svc = ErrorLogService(notify_ttl_seconds=60)
    _, first = svc.record(location="A", exc=_err())
    _, second = svc.record(location="A", exc=_err())
    assert first is True
    assert second is False
    # Both still recorded — dedup is only on notification, not on logging.
    assert len(svc.entries()) == 2


def test_should_notify_distinct_fingerprints_both_notify():
    svc = ErrorLogService(notify_ttl_seconds=60)
    _, n1 = svc.record(location="A", exc=_err(args=("origin",)))
    _, n2 = svc.record(location="A", exc=_err(args=("upstream",)))
    assert n1 is True
    assert n2 is True


def test_clear_resets_entries_and_dedup():
    svc = ErrorLogService(notify_ttl_seconds=60)
    svc.record(location="A", exc=_err())
    svc.clear()
    assert svc.entries() == []
    # After clear, the same fingerprint should notify again.
    _, n = svc.record(location="A", exc=_err())
    assert n is True
