from __future__ import annotations

import enum
from dataclasses import dataclass, field

from winter_cli.core.config_file import ConfigError

PROVISION_SUBTARGETS = ("dependency", "resource", "data")

_ENTRY_ALLOWED_KEYS = frozenset({"scope", "apply", "destroy", "reset", "clean", "required_services", "project", "name"})
_SUBTARGETS_WITH_REQUIRED_SERVICES = frozenset({"resource", "data"})


class ProvisionScope(enum.Enum):
    workspace = "workspace"
    feature_environment = "feature-environment"
    feature_worktree = "feature-worktree"


class ProvisionAction(enum.Enum):
    """The action vocabulary a provision handler runs.

    Members are named after the ``ProvisionHandler`` command-tuple fields they
    resolve to (``apply``, ``destroy``, ``reset``, ``clean``) — the manifest
    keys and the action names are the same vocabulary by construction.
    """

    apply = "apply"
    destroy = "destroy"
    reset = "reset"
    clean = "clean"


# Scopes on which ``project`` is permitted (defined after the enum so the enum members are available).
_PROJECT_ALLOWED_SCOPES = frozenset({ProvisionScope.feature_environment})

# Scope-qualified selector token grammar for `winter provision --name <scope>.<name>`.
# Short, hyphen-free tokens — distinct from the (hyphenated) `scope` field spelling and
# from arbitrary env names, so a selector can never be confused with either. Mirrors the
# brevity of the `required_services` `workspace/<service>` token while avoiding that
# grammar's per-env token (a provision entry is declared once and is not itself bound to
# a specific env instance, so there is no env name to reuse here).
SELECTOR_SCOPE_TOKENS: dict[str, ProvisionScope] = {
    "workspace": ProvisionScope.workspace,
    "feature": ProvisionScope.feature_environment,
    "worktree": ProvisionScope.feature_worktree,
}


@dataclass(frozen=True)
class ProvisionHandler:
    subtarget: str
    scope: ProvisionScope
    apply: tuple[str, ...]
    source: str
    destroy: tuple[str, ...] | None = None
    reset: tuple[str, ...] | None = None
    clean: tuple[str, ...] | None = None
    required_services: tuple[str, ...] = field(default_factory=tuple)
    project: str | None = None
    name: str | None = None


def _parse_commands(
    raw: object,
    location: str,
    field: str,
    source: str,
    *,
    required: bool,
) -> tuple[str, ...] | None:
    """Normalize a raw field value to a command tuple.

    Accepts a non-empty string or a non-empty list of non-empty strings.
    A bare string becomes a single-element tuple; a list becomes a tuple in
    declaration order.  Returns ``None`` when the field is absent and not
    required.  Raises ``ConfigError`` for any invalid value.
    """
    if raw is None:
        if required:
            raise ConfigError(
                f"{location} in {source!r} is missing required field {field!r} "
                f"(must be a non-empty string or non-empty list of strings)."
            )
        return None

    if isinstance(raw, str):
        if not raw:
            raise ConfigError(
                f"{location} in {source!r} field {field!r} must be a non-empty string (got empty string)."
            )
        return (raw,)

    if isinstance(raw, list):
        if not raw:
            raise ConfigError(f"{location} in {source!r} field {field!r} must be a non-empty list (got empty list).")
        for idx, item in enumerate(raw):
            if not isinstance(item, str) or not item:
                raise ConfigError(
                    f"{location} in {source!r} field {field!r}[{idx}] must be a non-empty string "
                    f"(got {type(item).__name__!r})."
                )
        return tuple(raw)

    raise ConfigError(
        f"{location} in {source!r} field {field!r} must be a string or list of strings (got {type(raw).__name__!r})."
    )


class ProvisionManifestParser:
    """Parses a raw ``[provision]`` table into a list of ``ProvisionHandler`` objects.

    Raises ``ConfigError`` on any structural or semantic violation.
    """

    def parse(
        self,
        raw: dict | None,
        source: str,
        project_names: frozenset[str] | None = None,
    ) -> list[ProvisionHandler]:
        """Parse raw ``[provision]`` table data.

        ``raw`` is the value of the ``[provision]`` table — a dict mapping
        sub-target names to lists of entry dicts.  Returns ``[]`` for
        ``None`` or empty input.

        ``project_names`` is the set of declared ``[[project_repository]]``
        names.  When provided, a ``project`` field is validated against this
        set; when ``None`` (e.g. for extension manifests), membership is not
        checked.
        """
        if not raw:
            return []

        handlers: list[ProvisionHandler] = []
        names_by_scope: dict[ProvisionScope, set[str]] = {}

        for key, entries in raw.items():
            if key not in PROVISION_SUBTARGETS:
                valid = ", ".join(repr(s) for s in PROVISION_SUBTARGETS)
                raise ConfigError(f"Unknown provision sub-target {key!r} in {source!r}. Must be one of: {valid}.")

            if not isinstance(entries, list):
                raise ConfigError(
                    f"provision.{key} in {source!r} must be a list of tables, got {type(entries).__name__!r}."
                )

            for i, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    raise ConfigError(
                        f"provision.{key}[{i}] in {source!r} must be a table (dict), got {type(entry).__name__!r}."
                    )
                unknown = set(entry.keys()) - _ENTRY_ALLOWED_KEYS
                if unknown:
                    bad = ", ".join(repr(k) for k in sorted(unknown))
                    allowed = ", ".join(repr(k) for k in sorted(_ENTRY_ALLOWED_KEYS))
                    raise ConfigError(
                        f"Unknown key(s) {bad} in provision.{key}[{i}] in {source!r}. Allowed keys: {allowed}."
                    )

                scope_raw = entry.get("scope")
                if scope_raw is None:
                    raise ConfigError(f"provision.{key}[{i}] in {source!r} is missing required field 'scope'.")
                try:
                    scope = ProvisionScope(scope_raw)
                except ValueError as exc:
                    valid_scopes = ", ".join(repr(s.value) for s in ProvisionScope)
                    raise ConfigError(
                        f"Invalid scope {scope_raw!r} in provision.{key}[{i}] in {source!r}. "
                        f"Must be one of: {valid_scopes}."
                    ) from exc

                apply_raw = entry.get("apply")
                apply = _parse_commands(apply_raw, f"provision.{key}[{i}]", "apply", source, required=True)
                assert apply is not None  # required=True guarantees non-None

                destroy_raw = entry.get("destroy")
                destroy = _parse_commands(destroy_raw, f"provision.{key}[{i}]", "destroy", source, required=False)

                reset_raw = entry.get("reset")
                reset = _parse_commands(reset_raw, f"provision.{key}[{i}]", "reset", source, required=False)

                clean_raw = entry.get("clean")
                clean = _parse_commands(clean_raw, f"provision.{key}[{i}]", "clean", source, required=False)

                required_services_raw = entry.get("required_services")
                if required_services_raw is not None:
                    if key not in _SUBTARGETS_WITH_REQUIRED_SERVICES:
                        raise ConfigError(
                            f"'required_services' is not allowed on provision.{key}[{i}] in {source!r}. "
                            f"'required_services' may only be declared on 'resource' or 'data' handlers."
                        )
                    if not isinstance(required_services_raw, list) or not all(
                        isinstance(s, str) for s in required_services_raw
                    ):
                        raise ConfigError(
                            f"provision.{key}[{i}].required_services in {source!r} must be a list of strings."
                        )
                    required_services: tuple[str, ...] = tuple(required_services_raw)
                else:
                    required_services = ()

                project_raw = entry.get("project")
                project: str | None = None
                if project_raw is not None:
                    if scope not in _PROJECT_ALLOWED_SCOPES:
                        raise ConfigError(
                            f"'project' is not allowed on provision.{key}[{i}] in {source!r} "
                            f"with scope {scope.value!r}. "
                            f"'project' may only be declared on 'feature-environment' handlers."
                        )
                    if not isinstance(project_raw, str) or not project_raw:
                        raise ConfigError(f"provision.{key}[{i}].project in {source!r} must be a non-empty string.")
                    if project_names is not None and project_raw not in project_names:
                        declared = ", ".join(repr(n) for n in sorted(project_names)) or "(none)"
                        raise ConfigError(
                            f"provision.{key}[{i}].project {project_raw!r} in {source!r} "
                            f"is not a declared [[project_repository]]. Declared repos: {declared}."
                        )
                    project = project_raw

                name_raw = entry.get("name")
                name: str | None = None
                if name_raw is not None:
                    if not isinstance(name_raw, str) or not name_raw:
                        raise ConfigError(f"provision.{key}[{i}].name in {source!r} must be a non-empty string.")
                    seen = names_by_scope.setdefault(scope, set())
                    if name_raw in seen:
                        raise ConfigError(
                            f"provision.{key}[{i}].name {name_raw!r} in {source!r} duplicates another "
                            f"provision entry's name within scope {scope.value!r}. "
                            f"'name' must be unique within its scope grouping."
                        )
                    seen.add(name_raw)
                    name = name_raw

                handlers.append(
                    ProvisionHandler(
                        subtarget=key,
                        scope=scope,
                        apply=apply,
                        source=source,
                        destroy=destroy,
                        reset=reset,
                        clean=clean,
                        required_services=required_services,
                        project=project,
                        name=name,
                    )
                )

        return handlers
