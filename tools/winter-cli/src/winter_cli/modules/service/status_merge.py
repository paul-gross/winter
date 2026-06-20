"""Merge multiple ``StatusDocument`` objects into one.

Used by ``ServiceStatusService`` when two or more providers are bound for the
service slot: each provider's ``status`` output is parsed into a
``StatusDocument`` and then merged into a single document for rendering.

Merge semantics (per plan §3d / Risk 7):
- Different env names → concatenated in the order they are encountered.
- Same env name across providers → ``services`` lists are concatenated; for
  scalar env fields (``session``, ``port_base``) **first-non-null wins**.
"""

from __future__ import annotations

from winter_cli.modules.service.status_models import EnvStatus, ServiceStatus, StatusDocument


def merge_status_documents(docs: list[StatusDocument]) -> StatusDocument:
    """Merge an ordered list of ``StatusDocument`` into one.

    An empty list returns an empty document ``StatusDocument(envs=())``.

    For the same env name appearing in multiple documents the services are
    concatenated in document order; ``session`` and ``port_base`` are the first
    non-null value encountered across documents.
    """
    if not docs:
        return StatusDocument(envs=())

    # Preserve encounter order of env names while accumulating per-env state.
    env_order: list[str] = []
    env_sessions: dict[str, str | None] = {}
    env_port_bases: dict[str, int | None] = {}
    env_services: dict[str, list[ServiceStatus]] = {}

    for doc in docs:
        for env in doc.envs:
            if env.env not in env_services:
                env_order.append(env.env)
                env_sessions[env.env] = env.session
                env_port_bases[env.env] = env.port_base
                env_services[env.env] = list(env.services)
            else:
                # First-non-null wins for scalars.
                if env_sessions[env.env] is None and env.session is not None:
                    env_sessions[env.env] = env.session
                if env_port_bases[env.env] is None and env.port_base is not None:
                    env_port_bases[env.env] = env.port_base
                # Services always concatenated.
                env_services[env.env].extend(env.services)

    merged_envs = tuple(
        EnvStatus(
            env=name,
            session=env_sessions[name],
            port_base=env_port_bases[name],
            services=tuple(env_services[name]),
        )
        for name in env_order
    )
    return StatusDocument(envs=merged_envs)
