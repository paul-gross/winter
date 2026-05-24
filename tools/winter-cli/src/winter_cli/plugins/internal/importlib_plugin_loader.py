from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from winter_cli.plugins.plugin_loader import IPluginLoader


class ImportlibPluginLoader:
    """Adapter that loads a plugin module via `importlib.util.spec_from_file_location`.

    Confines `importlib` and `sys.modules` mutation to this file. The module
    is registered in `sys.modules` under `winter_plugin_<name>` so subsequent
    imports inside the plugin (relative imports, re-imports for screens) hit
    the same instance.
    """

    @staticmethod
    def load(name: str, entry_point: Path) -> ModuleType:
        spec = importlib.util.spec_from_file_location(
            f"winter_plugin_{name}",
            entry_point,
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"could not build module spec for {entry_point}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module


def _conforms_importlib_plugin_loader(x: ImportlibPluginLoader) -> IPluginLoader:
    return x
