from __future__ import annotations

from pathlib import Path

import tomlkit
from tomlkit.items import AoT

from winter_cli.config.models import (
    ProjectRepositoryConfig,
    StandaloneRepositoryConfig,
    WorkspaceConfig,
)
from winter_cli.config.winter_configuration_repository import IWriteWinterConfigurationRepository
from winter_cli.config.workspace import CONFIG_FILE, LOCAL_CONFIG_FILE, WINTER_DIR
from winter_cli.core.filesystem import IFilesystemWriter


class WriteWinterConfigurationRepository:
    """Mutates the workspace's winter configuration files via tomlkit, preserving
    comments and surrounding structure.

    Targets `.winter/config.toml` by default; with `local=True`, targets the
    overlay `.winter/config.local.toml` (auto-created on first write).

    Takes Pydantic config models for appends so only fields the caller explicitly
    set land in the file. Removals match by explicit `name` or URL-derived name.

    Raw file I/O goes through an injected `IFilesystemWriter` so tests can run
    against an in-memory fake; tomlkit is only invoked on the string content
    returned by the seam.
    """

    def __init__(self, workspace_config: WorkspaceConfig, fs: IFilesystemWriter) -> None:
        winter_dir = workspace_config.workspace_root / WINTER_DIR
        self._shared_path = winter_dir / CONFIG_FILE
        self._local_path = winter_dir / LOCAL_CONFIG_FILE
        self._fs = fs

    def append_project_repository(self, config: ProjectRepositoryConfig, local: bool = False) -> None:
        self._append_block("project_repository", config.model_dump(exclude_defaults=True, exclude_none=True), local)

    def append_standalone_repository(self, config: StandaloneRepositoryConfig, local: bool = False) -> None:
        self._append_block(
            "standalone_repository",
            config.model_dump(exclude_defaults=True, exclude_none=True),
            local,
        )

    def remove_project_repository(self, name: str, local: bool = False) -> bool:
        return self._remove_block("project_repository", name, local)

    def remove_standalone_repository(self, name: str, local: bool = False) -> bool:
        return self._remove_block("standalone_repository", name, local)

    def _append_block(self, table_name: str, fields: dict, local: bool) -> None:
        path = self._path_for(local)
        doc = self._load(path, allow_missing=local)
        block = tomlkit.table()
        for key, value in fields.items():
            block[key] = value
        if table_name in doc:
            existing = doc[table_name]
            assert isinstance(existing, AoT)
            existing.append(block)
        else:
            aot = tomlkit.aot()
            aot.append(block)
            doc[table_name] = aot
        self._fs.write_text(path, tomlkit.dumps(doc))

    def _remove_block(self, table_name: str, target_name: str, local: bool) -> bool:
        path = self._path_for(local)
        if local and not self._fs.exists(path):
            return False
        doc = self._load(path, allow_missing=False)
        aot = doc.get(table_name)
        if aot is None:
            return False
        for index, block in enumerate(aot):
            explicit = block.get("name")
            url = block.get("url")
            effective = str(explicit) if explicit is not None else (self._name_from_url(str(url)) if url else None)
            if effective != target_name:
                continue
            del aot[index]
            self._fs.write_text(path, tomlkit.dumps(doc))
            return True
        return False

    def _path_for(self, local: bool) -> Path:
        return self._local_path if local else self._shared_path

    def _load(self, path: Path, allow_missing: bool) -> tomlkit.TOMLDocument:
        if not self._fs.exists(path):
            if allow_missing:
                return tomlkit.document()
            raise FileNotFoundError(f"Config file not found: {path}")
        return tomlkit.parse(self._fs.read_text(path))

    @staticmethod
    def _name_from_url(url: str) -> str:
        stripped = url.rstrip("/")
        cut = max(stripped.rfind("/"), stripped.rfind(":"))
        candidate = stripped[cut + 1 :] if cut != -1 else stripped
        return candidate.removesuffix(".git")


def _conforms_write_winter_configuration_repository(
    x: WriteWinterConfigurationRepository,
) -> IWriteWinterConfigurationRepository:
    return x
