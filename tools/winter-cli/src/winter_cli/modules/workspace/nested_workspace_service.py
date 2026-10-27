from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from winter_cli.config.workspace import CONFIG_FILE, WINTER_DIR
from winter_cli.core.config_file import ConfigFileReadError
from winter_cli.modules.workspace.models import RepoError
from winter_cli.modules.workspace.nested_env import nested_chain, nested_child_env
from winter_cli.modules.workspace.nested_overlay import (
    delegated_keys,
    effective_ports_per_env,
    footprint,
    inherited_keys,
    slice_base,
    slice_size,
)
from winter_cli.modules.workspace.nested_status import NestedStatusShapeError, parse_state, reported_root
from winter_cli.util import deep_merge

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from winter_cli.config.local_overlay_repository import ILocalOverlayRepository
    from winter_cli.core.filesystem import IFilesystemReader
    from winter_cli.modules.workspace.env_index import EnvPortBaseResolver
    from winter_cli.modules.workspace.init_reporter import IInitReporter
    from winter_cli.modules.workspace.models import NestedWorkspaceState, ProjectRepository
    from winter_cli.modules.workspace.nested_workspace_runner import INestedWorkspaceRunner

INIT_COMMAND = "winter ws init"
SERVICE_DOWN_COMMAND = "winter service down workspace"
_STATUS_ARGS = ("ws", "status", "--json")


class NestedWorkspaceService:
    """Drives the nested workspace inside one env's worktree of a `nested = true` repo, reporting as that repo.

    Initializes it, reads which feature envs it holds and what work in it
    exists nowhere else, destroys its envs, and stops its workspace-scope
    services. The nested workspace's own CLI does the work; its output streams
    through the caller's reporter under the outer repo's name.

    Every nested call is guarded here, before the runner starts anything:

    - **Chain.** A process whose own *workspace_root* is already on
      `WINTER_NESTED_CHAIN` was started by a nested call yet resolved an
      enclosing workspace, so it refuses every nested call; so does a call
      into a root already on the chain. Each child's chain is this process's
      chain plus *workspace_root*, so a mis-resolved child stops at once
      instead of fanning out to every sibling nested worktree.
    - **Preflight.** The nested root must hold `.winter/config.toml`.
    - **Verify before mutate.** The nested `ws status --json` must be a
      schema-v1 document whose `workspace.root_path` is the nested root;
      otherwise the `winter` there resolved some other workspace — the outer
      one, say — which must never be mutated through this service. A root is
      verified once per process, by its first status read, and every later
      call into it reuses that verification.

    Every child runs with `nested_child_env`: this process's environment
    scrubbed of the outer workspace's variables.
    """

    def __init__(
        self,
        runner: INestedWorkspaceRunner,
        fs: IFilesystemReader,
        workspace_root: Path,
        environ: Mapping[str, str],
        port_bases: EnvPortBaseResolver,
        service_prefix: str,
        ports_per_env: int,
        overlay_repo: ILocalOverlayRepository,
        nested_repo_names: Sequence[str],
    ) -> None:
        self._runner = runner
        self._fs = fs
        self._workspace_root = workspace_root
        self._environ = environ
        self._port_bases = port_bases
        self._service_prefix = service_prefix
        self._ports_per_env = ports_per_env
        self._overlay_repo = overlay_repo
        self._nested_repo_names = tuple(nested_repo_names)
        self._verified: set[Path] = set()

    def reconcile(self, repo: ProjectRepository, root: Path, env: str, reporter: IInitReporter) -> None:
        """Verify the nested *root*, delegate the outer *env*'s ports and prefix to it, then run `winter ws init` there.

        Raises `RepoError` when the root fails verification, a config file the
        delegation reads or writes is malformed, the delegation is refused, or
        the init fails; any of the first three writes nothing and skips the init.
        """
        self._ensure_verified(root)
        self._delegate(repo, root, env, reporter)
        reporter.cmd_started(repo.name, INIT_COMMAND)
        returncode = self._run(root, ["ws", "init"], lambda line: reporter.cmd_output_line(repo.name, line))
        reporter.cmd_completed(repo.name, INIT_COMMAND, returncode)
        if returncode != 0:
            raise RepoError(f"nested `{INIT_COMMAND}` exited with code {returncode}", cwd=str(root))

    def _delegate(self, repo: ProjectRepository, root: Path, env: str, reporter: IInitReporter) -> None:
        """Write the outer env's port base and prefix, plus the inherited keys, into the nested `config.local.toml`.

        The outer env's band is its `WINTER_PORT_BASE`, resolved registry-first
        as every other consumer does. It splits into `K + 1` equal slices for
        the `K` nested repos: slice 0 is the parent's own, and the repo at
        position `i` in configuration order gets slice `i`, whose start is its
        `base_port`. Its prefix is `<service_prefix>-<env>-<repo name>`.
        The keys the repo's `inherit_local` names are copied from the outer
        workspace's raw `config.local.toml`; a delegated key wins over an
        inherited one of the same name. Rewritten on every init so the nested
        workspace tracks the outer env. A config file that is not valid TOML
        raises `RepoError` naming it.
        """
        try:
            self._write_delegation(repo, root, env, reporter)
        except ConfigFileReadError as exc:
            raise RepoError(f"cannot delegate to the nested workspace at {root}: {exc}", cwd=str(root)) from exc

    def _write_delegation(self, repo: ProjectRepository, root: Path, env: str, reporter: IInitReporter) -> None:
        count = len(self._nested_repo_names)
        size = slice_size(self._ports_per_env, count)
        position = self._nested_repo_names.index(repo.name) + 1
        base = slice_base(self._port_bases.port_base(env), size, position)
        delegated = delegated_keys(base, f"{self._service_prefix}-{env}-{repo.name}", repo.envs)
        inherited = inherited_keys(self._outer_local(repo), repo.inherit_local)
        values = {**inherited, **delegated}
        committed, local = self._overlay_repo.read_layers(root)
        local_after = {**local, **values}
        used = footprint(committed, local_after)
        available = size
        if used > available:
            raise RepoError(self._refusal(repo, env, committed, local_after, used, available, count), cwd=str(root))
        if self._overlay_repo.upsert_local(root, values):
            summary = ", ".join(f"{key} = {value!r}" for key, value in delegated.items())
            if inherited:
                summary += f"; inherited {', '.join(inherited)}"
            reporter.repo_action(repo.name, str(root), "nested_overlay_written", summary)

    def _outer_local(self, repo: ProjectRepository) -> dict[str, Any]:
        """The outer workspace's raw `config.local.toml`; unread when the repo inherits nothing."""
        if not repo.inherit_local:
            return {}
        return self._overlay_repo.read_layers(self._workspace_root)[1]

    def _refusal(
        self,
        repo: ProjectRepository,
        env: str,
        committed: dict,
        local_after: dict,
        used: int,
        available: int,
        count: int,
    ) -> str:
        """The refusal naming the footprint, the slice, and a fitting change, all from the layers the check used."""
        per_env = effective_ports_per_env(committed, local_after)
        usable_that_fit = available // per_env - 2
        message = (
            f"nested workspace {repo.name!r} needs {used} ports for env {env!r} "
            f"((envs_per_workspace + 1) x ports_per_env of {per_env}), but its slice of the outer env band holds only "
            f"{available} (outer ports_per_env of {self._ports_per_env} split into {count + 1} equal slices, "
            f"the first for the parent's own services). Raise the outer `ports_per_env` to at least "
            f"{used * (count + 1)}"
        )
        if usable_that_fit >= 1:
            return f"{message}, or set `envs = {usable_that_fit}` or fewer on its [[project_repository]] entry."
        return f"{message}, or lower the nested workspace's `ports_per_env`."

    def state(self, root: Path) -> NestedWorkspaceState:
        """What the nested workspace at *root* holds, read from its `ws status --json`.

        Raises `RepoError` when it cannot be read. The read also verifies
        *root* for every later call in this process.
        """
        self._preflight(root)
        state = self._read_verified(root)
        self._verified.add(root.resolve())
        return state

    def destroy_envs(
        self,
        repo: ProjectRepository,
        root: Path,
        state: NestedWorkspaceState,
        *,
        force: bool,
        strict: bool,
        provision_teardown: bool,
        reporter: IInitReporter,
    ) -> bool:
        """Destroy every env in *state*, one nested `winter ws destroy` per env; return whether all succeeded.

        Every env is attempted even after one fails, so a single stuck nested
        env leaves no sibling running. A *root* `state` already read is not
        verified again. Each success reports
        `nested_env_destroyed`; each failure is reported as a repo error.
        """
        success = True
        for env in state.envs:
            command = f"winter ws destroy {env.name}"
            reporter.cmd_started(repo.name, command)
            args = ["ws", "destroy", env.name]
            if force:
                args.append("--force")
            if strict:
                args.append("--strict")
            if not provision_teardown:
                args.append("--no-provision-teardown")
            try:
                self._ensure_verified(root)
                returncode = self._run(root, args, lambda line: reporter.cmd_output_line(repo.name, line))
            except RepoError as exc:
                reporter.repo_error(repo.name, f"nested env {env.name} — {exc}")
                success = False
                continue
            reporter.cmd_completed(repo.name, command, returncode)
            if returncode != 0:
                reporter.repo_error(repo.name, f"nested `{command}` exited with code {returncode}")
                success = False
                continue
            reporter.repo_action(repo.name, str(root / env.name), "nested_env_destroyed", env.name)
        return success

    def binds_service(self, root: Path) -> bool:
        """Whether the nested workspace at *root* binds a `service` capability in its effective config.

        Reads its committed `config.toml` and its `config.local.toml` layered as
        winter layers them. Raises `RepoError` when either is not valid TOML.
        """
        try:
            committed, local = self._overlay_repo.read_layers(root)
        except ConfigFileReadError as exc:
            raise RepoError(f"cannot read the nested workspace config at {root}: {exc}", cwd=str(root)) from exc
        return _binds_service(deep_merge(committed, local))

    def stop_workspace_services(self, repo: ProjectRepository, root: Path, *, reporter: IInitReporter) -> bool:
        """Run `winter service down workspace` in *root* when it binds a service provider; return whether it succeeded.

        A nested env's destroy stops only that env's services; the nested
        workspace's own workspace-scope services would outlive the worktree
        that holds them. Nothing runs when no provider is bound, since there
        `service down` itself fails. A *root* `state` already read is not
        verified again. Success reports `nested_workspace_services_stopped`; a
        failure is reported as a repo error.
        """
        try:
            if not self.binds_service(root):
                return True
            reporter.cmd_started(repo.name, SERVICE_DOWN_COMMAND)
            self._ensure_verified(root)
            returncode = self._run(
                root, ["service", "down", "workspace"], lambda line: reporter.cmd_output_line(repo.name, line)
            )
        except RepoError as exc:
            reporter.repo_error(repo.name, f"nested workspace services — {exc}")
            return False
        reporter.cmd_completed(repo.name, SERVICE_DOWN_COMMAND, returncode)
        if returncode != 0:
            reporter.repo_error(repo.name, f"nested `{SERVICE_DOWN_COMMAND}` exited with code {returncode}")
            return False
        reporter.repo_action(repo.name, str(root), "nested_workspace_services_stopped")
        return True

    def _ensure_verified(self, root: Path) -> None:
        """Refuse a guarded *root*, and verify it unless an earlier call in this process already did."""
        self._preflight(root)
        if root.resolve() not in self._verified:
            self._read_verified(root)
            self._verified.add(root.resolve())

    def _preflight(self, root: Path) -> None:
        """Refuse a nested call into *root* before anything runs: the chain checks and the config check."""
        chain = nested_chain(self._environ)
        own = str(self._workspace_root.resolve())
        if own in chain:
            raise RepoError(
                f"this winter resolved workspace {own}, which an enclosing nested call already runs in; "
                f"refusing to run the nested workspace at {root} from it",
                cwd=str(root),
            )
        if str(root.resolve()) in chain:
            raise RepoError(
                f"nested workspace at {root} is already being run by an enclosing winter call; refusing to re-enter it",
                cwd=str(root),
            )
        if not self._fs.is_file(root / WINTER_DIR / CONFIG_FILE):
            raise RepoError(
                f"nested workspace at {root} has no {WINTER_DIR}/{CONFIG_FILE}; refusing to run winter there",
                cwd=str(root),
            )

    def _read_verified(self, root: Path) -> NestedWorkspaceState:
        """Run the nested `ws status --json` and parse it, after confirming it describes *root*."""
        stdout = self._runner.status_json(root, self._child_env())
        try:
            doc = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise self._status_error(root, f"nested workspace status at {root} is not JSON — {exc}") from exc
        try:
            reported = reported_root(doc)
        except NestedStatusShapeError as exc:
            raise self._status_error(root, f"nested workspace status at {root} {exc}") from exc
        if reported is None or Path(reported).resolve() != root.resolve():
            raise self._status_error(
                root, f"winter in {root} resolved workspace {reported!r}, not the nested workspace; refusing to run it"
            )
        try:
            return parse_state(doc)
        except NestedStatusShapeError as exc:
            raise self._status_error(root, f"nested workspace status at {root} is malformed: {exc}") from exc

    def _run(self, root: Path, args: Sequence[str], on_line: Callable[[str], None]) -> int:
        return self._runner.run(root, args, self._child_env(), on_line)

    def _child_env(self) -> dict[str, str]:
        return nested_child_env(self._environ, self._workspace_root)

    @staticmethod
    def _status_error(root: Path, message: str) -> RepoError:
        return RepoError(message, program="winter", subcommand="ws", cmd_args=_STATUS_ARGS[1:], cwd=str(root))


def _binds_service(config: Mapping[str, Any]) -> bool:
    """Whether *config*'s `[capabilities] service` names a provider — a non-empty string, or a list holding one."""
    capabilities = config.get("capabilities")
    if not isinstance(capabilities, dict):
        return False
    service = capabilities.get("service")
    names = service if isinstance(service, list) else [service]
    return any(isinstance(name, str) and name for name in names)
