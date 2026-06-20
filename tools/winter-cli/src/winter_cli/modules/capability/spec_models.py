from __future__ import annotations

import enum
from dataclasses import dataclass, field


class ArityKind(enum.Enum):
    """How the orchestrator entrypoint receives its target selection.

    `no_positionals` — no positionals after the action word (`describe`).
    `single_env` — exactly one positional: the env name (`up`, `down`).
    `patterns_optional` — zero or more `<env>/<service>` glob patterns (`status`).
    `patterns_required` — one or more `<env>/<service>` glob patterns (`restart`, `logs`).
    """

    no_positionals = "no-positionals"
    single_env = "single-env"
    patterns_optional = "patterns-optional"
    patterns_required = "patterns-required"


class CheckKind(enum.Enum):
    """The kind of conformance check a `SpecCheck` entry describes.

    `accepts_action` — the entrypoint exits with any code other than 2 for the
        named action word (exit 2 = unknown-action signal; exit 0 or 3 pass).
    `refuses_unknown` — the entrypoint exits non-zero for an unrecognised action
        (exit 2 or exit 3 are both accepted).
    `forwards_params` — the entrypoint echoes its argv back on stdout or stderr.
        WINTER_* env vars are set on every dispatch but are not asserted by this
        check.
    `emits_describe_json` — the entrypoint's stdout for the `describe` action is
        parseable as the required ``{"services": [...]}`` JSON object. A provider
        that passes ``accepts-action`` for ``describe`` but emits malformed JSON
        would otherwise only fail at runtime via ``DescribeParseError``.
    """

    accepts_action = "accepts-action"
    refuses_unknown = "refuses-unknown"
    forwards_params = "forwards-params"
    emits_describe_json = "emits-describe-json"


@dataclass(frozen=True)
class SpecEnvVar:
    """One environment variable that winter sets for an action.

    `name` is the environment-variable key.
    `description` is a human-readable explanation for docs / scaffolding.
    """

    name: str
    description: str


@dataclass(frozen=True)
class SpecAction:
    """One action that the orchestrator entrypoint must implement.

    `name` is the action word passed as the first positional argument.
    `arity` describes how the remaining positionals are structured.
    `summary` is a short human-readable description for docs / scaffolding.
    `env_vars` is the tuple of per-action environment variables winter sets
    for this action (in addition to the always-present set on `CapabilitySpec`).
    """

    name: str
    arity: ArityKind
    summary: str
    env_vars: tuple[SpecEnvVar, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SpecCheck:
    """One conformance check entry in a capability spec.

    `kind` is the category of check (what property is being verified).
    `description` is a human-readable explanation of what the check validates.
    """

    kind: CheckKind
    description: str


@dataclass(frozen=True)
class ExitCodeSpec:
    """Documents one exit-code convention for a capability spec.

    `code` is the numeric exit code.
    `meaning` is a short human-readable description.
    """

    code: int
    meaning: str


@dataclass(frozen=True)
class CapabilitySpec:
    """The machine-readable contract for one capability slot at one version.

    `slot` is the capability slot name (e.g. `"service"`).
    `version` is the spec version string (e.g. `"v1"`).
    `title` is a short human-readable name for docs / scaffolding.
    `actions` is the ordered tuple of actions the entrypoint must implement.
    `exit_codes` documents the exit-code conventions (orchestrator + winter-owned).
    `env_vars` is the tuple of environment variables winter sets on every dispatch
    regardless of action (the always-present set).
    `checks` is the ordered tuple of conformance check entries.
    """

    slot: str
    version: str
    title: str
    actions: tuple[SpecAction, ...]
    exit_codes: tuple[ExitCodeSpec, ...]
    env_vars: tuple[SpecEnvVar, ...]
    checks: tuple[SpecCheck, ...]
