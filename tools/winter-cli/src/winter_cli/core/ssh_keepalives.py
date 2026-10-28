"""SSH-side half-dead-connection guard, kept free of gitpython so the CLI entry module can install it cheaply."""

from __future__ import annotations

import os

# Conservative SSH client options paved into GIT_SSH_COMMAND by
# `ensure_ssh_keepalives()`. The SSH layer notices a half-dead TCP socket in
# ~90s (3 * 30s keepalive misses) instead of relying solely on the Python-
# side per-call timeout — belt (this) and suspenders (kill_after_timeout in
# `run_remote`).
SSH_KEEPALIVE_OPTIONS: str = "ssh -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3"
SSH_ENV_VAR: str = "GIT_SSH_COMMAND"


def ensure_ssh_keepalives() -> None:
    """Install the SSH-side half-dead-connection guard into GIT_SSH_COMMAND.

    `setdefault`-style: a user-set GIT_SSH_COMMAND (e.g. for an identity
    file, custom port, or ProxyCommand) wins — we only pave a safe default,
    never stomp on intentional config. Call once at process startup; this
    mutates global env and shouldn't run from inside an injected service
    constructor (would leak across test boundaries and surprise consumers).
    """
    os.environ.setdefault(SSH_ENV_VAR, SSH_KEEPALIVE_OPTIONS)
