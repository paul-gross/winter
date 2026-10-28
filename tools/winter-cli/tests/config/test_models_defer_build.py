from __future__ import annotations

import inspect

from pydantic import BaseModel

from winter_cli.config import models


def test_every_config_model_defers_its_schema_build() -> None:
    """Schemas build on first use, so importing the models costs class creation only."""
    config_models = [
        cls
        for _, cls in inspect.getmembers(models, inspect.isclass)
        if issubclass(cls, BaseModel) and cls.__module__ == models.__name__
    ]
    assert config_models
    assert [cls.__name__ for cls in config_models if not cls.model_config.get("defer_build")] == []
