from __future__ import annotations

from winter_cli.modules.service.service_readiness_service import ServiceReadinessService
from winter_cli.modules.service.status_models import EnvStatus, ServiceStatus, StatusDocument


def _svc(name: str, health: str) -> ServiceStatus:
    return ServiceStatus(
        name=name,
        state="running",
        health=health,
        ports=(),
        handle=None,
        log_path=None,
        since=None,
    )


def _doc(env: str, services: list[ServiceStatus]) -> StatusDocument:
    return StatusDocument(envs=(EnvStatus(env=env, session=None, port_base=None, services=tuple(services)),))


class _StubStatusService:
    """Returns the next queued StatusDocument on each ``collect`` call.

    The last queued document repeats once the queue is exhausted, so a single
    document models a status that never changes.
    """

    def __init__(self, docs: list[StatusDocument | None]) -> None:
        self._docs = list(docs)
        self.collect_patterns: list[tuple[str, ...]] = []

    def collect(self, patterns: tuple[str, ...]) -> StatusDocument | None:
        self.collect_patterns.append(patterns)
        if len(self._docs) > 1:
            return self._docs.pop(0)
        return self._docs[0]


def _ticking_clock(step: float = 1.0):
    counter = {"n": -1}

    def _clock() -> float:
        counter["n"] += 1
        return counter["n"] * step

    return _clock


def _readiness(status: _StubStatusService, sleeps: list[float]) -> ServiceReadinessService:
    return ServiceReadinessService(
        status_service=status,  # type: ignore[arg-type]
        sleep=sleeps.append,
        monotonic=_ticking_clock(),
        poll_interval_s=0.25,
    )


def test_ready_on_first_poll_when_all_healthy() -> None:
    status = _StubStatusService([_doc("alpha", [_svc("api", "healthy")])])
    sleeps: list[float] = []
    result = _readiness(status, sleeps).wait("alpha", timeout_s=30.0)
    assert result.ready is True
    assert result.unhealthy == ()
    # One poll, no sleep — the common case returns immediately.
    assert len(status.collect_patterns) == 1
    assert sleeps == []


def test_unknown_health_does_not_block() -> None:
    # A service with no declared probe reports "unknown" — it must not block.
    status = _StubStatusService([_doc("alpha", [_svc("api", "unknown"), _svc("web", "healthy")])])
    sleeps: list[float] = []
    result = _readiness(status, sleeps).wait("alpha", timeout_s=30.0)
    assert result.ready is True
    assert sleeps == []


def test_polls_until_service_becomes_healthy() -> None:
    status = _StubStatusService(
        [
            _doc("alpha", [_svc("api", "unhealthy")]),
            _doc("alpha", [_svc("api", "unhealthy")]),
            _doc("alpha", [_svc("api", "healthy")]),
        ]
    )
    sleeps: list[float] = []
    result = _readiness(status, sleeps).wait("alpha", timeout_s=30.0)
    assert result.ready is True
    assert len(status.collect_patterns) == 3
    assert sleeps == [0.25, 0.25]


def test_timeout_names_unhealthy_services() -> None:
    status = _StubStatusService(
        [_doc("alpha", [_svc("api", "unhealthy"), _svc("web", "healthy"), _svc("worker", "unhealthy")])]
    )
    sleeps: list[float] = []
    # timeout 0.5s: deadline = tick0 (0) + 0.5; second monotonic tick (1.0) >= deadline → give up.
    result = _readiness(status, sleeps).wait("alpha", timeout_s=0.5)
    assert result.ready is False
    assert result.unhealthy == ("alpha/api", "alpha/worker")


def test_none_document_does_not_block() -> None:
    # No provider produced a parseable status — nothing reports unhealthy.
    status = _StubStatusService([None])
    sleeps: list[float] = []
    result = _readiness(status, sleeps).wait("alpha", timeout_s=30.0)
    assert result.ready is True
    assert sleeps == []


def test_poll_is_scoped_to_the_env() -> None:
    status = _StubStatusService([_doc("alpha", [_svc("api", "healthy")])])
    _readiness(status, []).wait("alpha", timeout_s=30.0)
    # The env name is forwarded as a bare status pattern (expands to alpha/*).
    assert status.collect_patterns == [("alpha",)]
