from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from pathlib import Path

from winter_cli.core.filesystem import IFilesystemWriter


class LocalFilesystem:
    """Local-disk adapter for IFilesystemReader and IFilesystemWriter.

    All direct `pathlib`, `shutil`, and `os` filesystem usage is confined here
    so services depend on the Protocol rather than reaching the standard
    library. Methods are thin pass-throughs — orchestration stays in services.
    """

    @staticmethod
    def exists(path: Path) -> bool:
        return path.exists()

    @staticmethod
    def is_file(path: Path) -> bool:
        return path.is_file()

    @staticmethod
    def is_dir(path: Path) -> bool:
        return path.is_dir()

    @staticmethod
    def is_symlink(path: Path) -> bool:
        return path.is_symlink()

    @staticmethod
    def iterdir(path: Path) -> list[Path]:
        return list(path.iterdir())

    @staticmethod
    def read_text(path: Path) -> str:
        return path.read_text()

    @staticmethod
    def read_bytes(path: Path) -> bytes:
        return path.read_bytes()

    @staticmethod
    def readlink(path: Path) -> Path:
        return path.readlink()

    @staticmethod
    def access_x_ok(path: Path) -> bool:
        return os.access(path, os.X_OK)

    @staticmethod
    def mkdir(path: Path, parents: bool = False, exist_ok: bool = False) -> None:
        path.mkdir(parents=parents, exist_ok=exist_ok)

    @staticmethod
    def write_text(path: Path, data: str) -> None:
        path.write_text(data)

    @staticmethod
    def append_lines(path: Path, lines: Iterable[str]) -> None:
        with path.open("a") as f:
            for line in lines:
                f.write(line if line.endswith("\n") else line + "\n")

    @staticmethod
    def symlink_to(link_path: Path, target: Path) -> None:
        link_path.symlink_to(target)

    @staticmethod
    def unlink(path: Path) -> None:
        path.unlink()

    @staticmethod
    def rmtree(path: Path) -> None:
        shutil.rmtree(path)


# Returning IFilesystemWriter pins both seams: IFilesystemReader is a supertype,
# so a Writer-conforming adapter trivially conforms to Reader too.
def _conforms_local_filesystem(x: LocalFilesystem) -> IFilesystemWriter:
    return x
