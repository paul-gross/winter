from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FakeFilesystem
from winter_cli.config.workspace import (
    CONFIG_FILE,
    WINTER_DIR,
    WorkspaceConfigService,
    parse_provision,
)
from winter_cli.core.config_file import ConfigError
from winter_cli.modules.provision.manifest import (
    ProvisionManifestParser,
    ProvisionScope,
)

WORKSPACE_ROOT = Path("/ws/demo")
SOURCE = "project"


class _StubLocator:
    def __init__(self, root: Path) -> None:
        self._root = root

    def find_workspace_root(self) -> Path:
        return self._root


class _DictConfigFileReader:
    def __init__(self, contents: dict[Path, dict]) -> None:
        self._contents = contents

    def load(self, path: Path) -> dict:
        if path not in self._contents:
            raise FileNotFoundError(path)
        return self._contents[path]


def _config_service(configs: dict[Path, dict]) -> WorkspaceConfigService:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    fs = FakeFilesystem(files={config_path: ""})
    return WorkspaceConfigService(
        workspace_locator=_StubLocator(WORKSPACE_ROOT),
        fs=fs,
        config_file_reader=_DictConfigFileReader(configs),
    )


# ── ProvisionManifestParser unit tests ───────────────────────────────────────


def test_parse_returns_empty_for_none() -> None:
    parser = ProvisionManifestParser()
    assert parser.parse(None, SOURCE) == []


def test_parse_returns_empty_for_empty_dict() -> None:
    parser = ProvisionManifestParser()
    assert parser.parse({}, SOURCE) == []


def test_parse_valid_workspace_scope() -> None:
    parser = ProvisionManifestParser()
    raw = {
        "resource": [
            {
                "scope": "workspace",
                "apply": "scripts/create-db.sh",
                "destroy": "scripts/drop-db.sh",
                "required_services": ["workspace/postgres"],
            }
        ]
    }
    handlers = parser.parse(raw, SOURCE)
    assert len(handlers) == 1
    h = handlers[0]
    assert h.subtarget == "resource"
    assert h.scope == ProvisionScope.workspace
    assert h.apply == ("scripts/create-db.sh",)
    assert h.destroy == ("scripts/drop-db.sh",)
    assert h.reset is None
    assert h.required_services == ("workspace/postgres",)
    assert h.source == SOURCE


def test_parse_valid_feature_environment_scope() -> None:
    parser = ProvisionManifestParser()
    raw = {
        "data": [
            {
                "scope": "feature-environment",
                "apply": "scripts/seed.sh",
                "reset": "scripts/reseed.sh",
                "required_services": ["workspace/postgres"],
            }
        ]
    }
    handlers = parser.parse(raw, SOURCE)
    assert len(handlers) == 1
    h = handlers[0]
    assert h.subtarget == "data"
    assert h.scope == ProvisionScope.feature_environment
    assert h.apply == ("scripts/seed.sh",)
    assert h.reset == ("scripts/reseed.sh",)
    assert h.destroy is None
    assert h.required_services == ("workspace/postgres",)


def test_parse_valid_feature_worktree_scope() -> None:
    parser = ProvisionManifestParser()
    raw = {
        "dependency": [
            {
                "scope": "feature-worktree",
                "apply": "scripts/install.sh",
            }
        ]
    }
    handlers = parser.parse(raw, SOURCE)
    assert len(handlers) == 1
    h = handlers[0]
    assert h.subtarget == "dependency"
    assert h.scope == ProvisionScope.feature_worktree
    assert h.apply == ("scripts/install.sh",)
    assert h.destroy is None
    assert h.reset is None
    assert h.required_services == ()


def test_parse_all_three_subtargets() -> None:
    parser = ProvisionManifestParser()
    raw = {
        "dependency": [{"scope": "feature-worktree", "apply": "scripts/install.sh"}],
        "resource": [{"scope": "workspace", "apply": "scripts/create.sh"}],
        "data": [{"scope": "feature-environment", "apply": "scripts/seed.sh"}],
    }
    handlers = parser.parse(raw, SOURCE)
    assert len(handlers) == 3
    subtargets = [h.subtarget for h in handlers]
    assert "dependency" in subtargets
    assert "resource" in subtargets
    assert "data" in subtargets


def test_parse_required_services_parsed_as_tuple_on_resource() -> None:
    parser = ProvisionManifestParser()
    raw = {
        "resource": [
            {
                "scope": "workspace",
                "apply": "scripts/create.sh",
                "required_services": ["workspace/postgres", "workspace/redis"],
            }
        ]
    }
    handlers = parser.parse(raw, SOURCE)
    assert handlers[0].required_services == ("workspace/postgres", "workspace/redis")
    assert isinstance(handlers[0].required_services, tuple)


def test_parse_required_services_parsed_as_tuple_on_data() -> None:
    parser = ProvisionManifestParser()
    raw = {
        "data": [
            {
                "scope": "feature-environment",
                "apply": "scripts/seed.sh",
                "required_services": ["workspace/postgres"],
            }
        ]
    }
    handlers = parser.parse(raw, SOURCE)
    assert handlers[0].required_services == ("workspace/postgres",)
    assert isinstance(handlers[0].required_services, tuple)


def test_parse_unknown_top_level_key_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"deploy": [{"scope": "workspace", "apply": "scripts/deploy.sh"}]}
    with pytest.raises(ConfigError, match="Unknown provision sub-target 'deploy'"):
        parser.parse(raw, SOURCE)


def test_parse_unknown_entry_key_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {
        "dependency": [
            {
                "scope": "feature-worktree",
                "apply": "scripts/install.sh",
                "unknown_key": "bad",
            }
        ]
    }
    with pytest.raises(ConfigError, match="Unknown key"):
        parser.parse(raw, SOURCE)


def test_parse_missing_apply_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"dependency": [{"scope": "feature-worktree"}]}
    with pytest.raises(ConfigError, match="missing required field 'apply'"):
        parser.parse(raw, SOURCE)


def test_parse_empty_apply_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"dependency": [{"scope": "feature-worktree", "apply": ""}]}
    with pytest.raises(ConfigError):
        parser.parse(raw, SOURCE)


def test_parse_bad_scope_value_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"dependency": [{"scope": "global", "apply": "scripts/install.sh"}]}
    with pytest.raises(ConfigError, match="Invalid scope 'global'"):
        parser.parse(raw, SOURCE)


def test_parse_bad_scope_error_lists_valid_values() -> None:
    parser = ProvisionManifestParser()
    raw = {"dependency": [{"scope": "bad", "apply": "scripts/install.sh"}]}
    with pytest.raises(ConfigError, match="'workspace'"):
        parser.parse(raw, SOURCE)


def test_parse_missing_scope_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"dependency": [{"apply": "scripts/install.sh"}]}
    with pytest.raises(ConfigError, match="missing required field 'scope'"):
        parser.parse(raw, SOURCE)


def test_parse_required_services_on_dependency_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {
        "dependency": [
            {
                "scope": "feature-worktree",
                "apply": "scripts/install.sh",
                "required_services": ["workspace/postgres"],
            }
        ]
    }
    with pytest.raises(ConfigError, match=r"'required_services' is not allowed on provision\.dependency"):
        parser.parse(raw, SOURCE)


def test_parse_required_services_must_be_list_of_strings() -> None:
    parser = ProvisionManifestParser()
    raw = {
        "resource": [
            {
                "scope": "workspace",
                "apply": "scripts/create.sh",
                "required_services": "workspace/postgres",  # string, not list
            }
        ]
    }
    with pytest.raises(ConfigError, match="must be a list of strings"):
        parser.parse(raw, SOURCE)


# ── Deferred-parse / workspace config wiring tests ───────────────────────────


def test_malformed_provision_does_not_raise_at_config_load() -> None:
    """A bad scope in [provision] must NOT raise during WorkspaceConfigService.load()."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    svc = _config_service(
        {
            config_path: {
                "provision": {
                    "dependency": [
                        {
                            "scope": "totally-invalid-scope",
                            "apply": "scripts/install.sh",
                        }
                    ]
                }
            }
        }
    )
    # Must not raise — deferred parse
    config = svc.load()
    assert isinstance(config.provision_raw, dict)
    assert "dependency" in config.provision_raw


def test_malformed_provision_raises_when_parse_provision_called() -> None:
    """parse_provision() runs the strict parser and raises ConfigError for a bad scope."""
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    svc = _config_service(
        {
            config_path: {
                "provision": {
                    "dependency": [
                        {
                            "scope": "totally-invalid-scope",
                            "apply": "scripts/install.sh",
                        }
                    ]
                }
            }
        }
    )
    config = svc.load()
    with pytest.raises(ConfigError, match="Invalid scope"):
        parse_provision(config, source="project")


def test_parse_provision_returns_handlers_for_valid_config() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    svc = _config_service(
        {
            config_path: {
                "provision": {
                    "dependency": [
                        {
                            "scope": "feature-worktree",
                            "apply": "scripts/install.sh",
                        }
                    ]
                }
            }
        }
    )
    config = svc.load()
    handlers = parse_provision(config, source="project")
    assert len(handlers) == 1
    assert handlers[0].subtarget == "dependency"
    assert handlers[0].scope == ProvisionScope.feature_worktree
    assert handlers[0].source == "project"


def test_parse_provision_returns_empty_when_no_provision_key() -> None:
    config_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
    svc = _config_service({config_path: {"main_branch": "main"}})
    config = svc.load()
    assert parse_provision(config, source="project") == []


# ── String / list normalization ───────────────────────────────────────────────


def test_parse_apply_as_string_normalizes_to_single_element_tuple() -> None:
    parser = ProvisionManifestParser()
    raw = {"dependency": [{"scope": "workspace", "apply": "echo hello"}]}
    handlers = parser.parse(raw, SOURCE)
    assert handlers[0].apply == ("echo hello",)
    assert isinstance(handlers[0].apply, tuple)


def test_parse_apply_as_list_normalizes_to_tuple() -> None:
    parser = ProvisionManifestParser()
    raw = {"dependency": [{"scope": "workspace", "apply": ["echo step1", "echo step2"]}]}
    handlers = parser.parse(raw, SOURCE)
    assert handlers[0].apply == ("echo step1", "echo step2")
    assert isinstance(handlers[0].apply, tuple)


def test_parse_destroy_as_string_normalizes_to_single_element_tuple() -> None:
    parser = ProvisionManifestParser()
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "destroy": "echo destroy"}]}
    handlers = parser.parse(raw, SOURCE)
    assert handlers[0].destroy == ("echo destroy",)


def test_parse_destroy_as_list_normalizes_to_tuple() -> None:
    parser = ProvisionManifestParser()
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "destroy": ["echo d1", "echo d2"]}]}
    handlers = parser.parse(raw, SOURCE)
    assert handlers[0].destroy == ("echo d1", "echo d2")


def test_parse_reset_as_string_normalizes_to_single_element_tuple() -> None:
    parser = ProvisionManifestParser()
    raw = {"data": [{"scope": "workspace", "apply": "echo apply", "reset": "echo reset"}]}
    handlers = parser.parse(raw, SOURCE)
    assert handlers[0].reset == ("echo reset",)


def test_parse_reset_as_list_normalizes_to_tuple() -> None:
    parser = ProvisionManifestParser()
    raw = {"data": [{"scope": "workspace", "apply": "echo apply", "reset": ["echo r1", "echo r2"]}]}
    handlers = parser.parse(raw, SOURCE)
    assert handlers[0].reset == ("echo r1", "echo r2")


def test_parse_clean_as_string_normalizes_to_single_element_tuple() -> None:
    parser = ProvisionManifestParser()
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "clean": "echo clean"}]}
    handlers = parser.parse(raw, SOURCE)
    assert handlers[0].clean == ("echo clean",)


def test_parse_clean_as_list_normalizes_to_tuple() -> None:
    parser = ProvisionManifestParser()
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "clean": ["echo c1", "echo c2"]}]}
    handlers = parser.parse(raw, SOURCE)
    assert handlers[0].clean == ("echo c1", "echo c2")


def test_parse_apply_empty_list_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"dependency": [{"scope": "workspace", "apply": []}]}
    with pytest.raises(ConfigError):
        parser.parse(raw, SOURCE)


def test_parse_apply_non_string_element_in_list_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"dependency": [{"scope": "workspace", "apply": ["echo ok", 123]}]}
    with pytest.raises(ConfigError):
        parser.parse(raw, SOURCE)


def test_parse_apply_neither_str_nor_list_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"dependency": [{"scope": "workspace", "apply": 42}]}
    with pytest.raises(ConfigError):
        parser.parse(raw, SOURCE)


def test_parse_destroy_empty_list_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "destroy": []}]}
    with pytest.raises(ConfigError):
        parser.parse(raw, SOURCE)


def test_parse_destroy_empty_string_element_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "destroy": ["echo ok", ""]}]}
    with pytest.raises(ConfigError):
        parser.parse(raw, SOURCE)


def test_parse_reset_empty_list_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"data": [{"scope": "workspace", "apply": "echo apply", "reset": []}]}
    with pytest.raises(ConfigError):
        parser.parse(raw, SOURCE)


def test_parse_reset_non_string_element_in_list_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"data": [{"scope": "workspace", "apply": "echo apply", "reset": [None]}]}
    with pytest.raises(ConfigError):
        parser.parse(raw, SOURCE)


def test_parse_clean_empty_list_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "clean": []}]}
    with pytest.raises(ConfigError):
        parser.parse(raw, SOURCE)


def test_parse_clean_empty_string_element_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "clean": ["echo ok", ""]}]}
    with pytest.raises(ConfigError):
        parser.parse(raw, SOURCE)


def test_parse_apply_empty_string_in_list_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"dependency": [{"scope": "workspace", "apply": ["echo ok", ""]}]}
    with pytest.raises(ConfigError):
        parser.parse(raw, SOURCE)


def test_parse_destroy_neither_str_nor_list_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "destroy": 99}]}
    with pytest.raises(ConfigError):
        parser.parse(raw, SOURCE)


def test_parse_reset_neither_str_nor_list_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"data": [{"scope": "workspace", "apply": "echo apply", "reset": {"cmd": "bad"}}]}
    with pytest.raises(ConfigError):
        parser.parse(raw, SOURCE)


def test_parse_clean_neither_str_nor_list_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "clean": 99}]}
    with pytest.raises(ConfigError):
        parser.parse(raw, SOURCE)


def test_parse_absent_destroy_is_none() -> None:
    parser = ProvisionManifestParser()
    raw = {"dependency": [{"scope": "workspace", "apply": "echo apply"}]}
    handlers = parser.parse(raw, SOURCE)
    assert handlers[0].destroy is None


def test_parse_absent_reset_is_none() -> None:
    parser = ProvisionManifestParser()
    raw = {"dependency": [{"scope": "workspace", "apply": "echo apply"}]}
    handlers = parser.parse(raw, SOURCE)
    assert handlers[0].reset is None


def test_parse_absent_clean_is_none() -> None:
    parser = ProvisionManifestParser()
    raw = {"dependency": [{"scope": "workspace", "apply": "echo apply"}]}
    handlers = parser.parse(raw, SOURCE)
    assert handlers[0].clean is None


# ── project field ─────────────────────────────────────────────────────────────


def test_parse_project_absent_is_none() -> None:
    """No project field → handler.project is None."""
    parser = ProvisionManifestParser()
    raw = {"data": [{"scope": "feature-environment", "apply": "echo seed"}]}
    handlers = parser.parse(raw, SOURCE)
    assert handlers[0].project is None


def test_parse_project_valid_on_feature_environment_scope() -> None:
    """project on feature-environment scope with a declared repo name is accepted."""
    parser = ProvisionManifestParser()
    raw = {"data": [{"scope": "feature-environment", "apply": "echo seed", "project": "web"}]}
    project_names: frozenset[str] = frozenset({"web", "api"})
    handlers = parser.parse(raw, SOURCE, project_names=project_names)
    assert len(handlers) == 1
    assert handlers[0].project == "web"


def test_parse_project_no_membership_check_when_project_names_is_none() -> None:
    """When project_names is None (extension case), membership is not checked."""
    parser = ProvisionManifestParser()
    raw = {"data": [{"scope": "feature-environment", "apply": "echo seed", "project": "any-name"}]}
    handlers = parser.parse(raw, SOURCE, project_names=None)
    assert handlers[0].project == "any-name"


def test_parse_project_on_workspace_scope_rejected() -> None:
    """project on workspace scope raises ConfigError."""
    parser = ProvisionManifestParser()
    raw = {"data": [{"scope": "workspace", "apply": "echo apply", "project": "web"}]}
    with pytest.raises(ConfigError, match="'project' is not allowed"):
        parser.parse(raw, SOURCE)


def test_parse_project_on_feature_worktree_scope_rejected() -> None:
    """project on feature-worktree scope raises ConfigError."""
    parser = ProvisionManifestParser()
    raw = {"dependency": [{"scope": "feature-worktree", "apply": "echo install", "project": "web"}]}
    with pytest.raises(ConfigError, match="'project' is not allowed"):
        parser.parse(raw, SOURCE)


def test_parse_project_scope_error_mentions_scope_value() -> None:
    """Wrong-scope error message names the invalid scope."""
    parser = ProvisionManifestParser()
    raw = {"data": [{"scope": "workspace", "apply": "echo apply", "project": "web"}]}
    with pytest.raises(ConfigError, match="'workspace'"):
        parser.parse(raw, SOURCE)


def test_parse_project_undeclared_repo_rejected_when_names_provided() -> None:
    """project naming a repo not in project_names is a ConfigError."""
    parser = ProvisionManifestParser()
    raw = {"data": [{"scope": "feature-environment", "apply": "echo seed", "project": "unknown-repo"}]}
    project_names: frozenset[str] = frozenset({"web", "api"})
    with pytest.raises(ConfigError, match="not a declared"):
        parser.parse(raw, SOURCE, project_names=project_names)


def test_parse_project_undeclared_repo_error_names_the_project() -> None:
    """Error message for undeclared project includes the project name."""
    parser = ProvisionManifestParser()
    raw = {"data": [{"scope": "feature-environment", "apply": "echo seed", "project": "no-such-repo"}]}
    project_names: frozenset[str] = frozenset({"web"})
    with pytest.raises(ConfigError, match="no-such-repo"):
        parser.parse(raw, SOURCE, project_names=project_names)


def test_parse_project_empty_string_rejected() -> None:
    """An empty string for project is rejected."""
    parser = ProvisionManifestParser()
    raw = {"data": [{"scope": "feature-environment", "apply": "echo seed", "project": ""}]}
    with pytest.raises(ConfigError, match="non-empty string"):
        parser.parse(raw, SOURCE)


# ── name field ─────────────────────────────────────────────────────────────


def test_parse_name_absent_is_none() -> None:
    """No name field → handler.name is None."""
    parser = ProvisionManifestParser()
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply"}]}
    handlers = parser.parse(raw, SOURCE)
    assert handlers[0].name is None


def test_parse_name_valid_string_accepted() -> None:
    parser = ProvisionManifestParser()
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "name": "mydb"}]}
    handlers = parser.parse(raw, SOURCE)
    assert handlers[0].name == "mydb"


def test_parse_name_empty_string_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "name": ""}]}
    with pytest.raises(ConfigError, match="non-empty string"):
        parser.parse(raw, SOURCE)


def test_parse_name_non_string_rejected() -> None:
    parser = ProvisionManifestParser()
    raw = {"resource": [{"scope": "workspace", "apply": "echo apply", "name": 42}]}
    with pytest.raises(ConfigError, match="non-empty string"):
        parser.parse(raw, SOURCE)


def test_parse_duplicate_name_within_same_scope_rejected() -> None:
    """Two entries in the same scope with the same name is a parse-time ConfigError."""
    parser = ProvisionManifestParser()
    raw = {
        "resource": [
            {"scope": "workspace", "apply": "echo one", "name": "mydb"},
            {"scope": "workspace", "apply": "echo two", "name": "mydb"},
        ]
    }
    with pytest.raises(ConfigError, match="duplicates another"):
        parser.parse(raw, SOURCE)


def test_parse_duplicate_name_across_subtargets_same_scope_rejected() -> None:
    """Uniqueness spans subtargets too — same scope, same name, different subtargets still collides."""
    parser = ProvisionManifestParser()
    raw = {
        "dependency": [{"scope": "workspace", "apply": "echo dep", "name": "mydb"}],
        "resource": [{"scope": "workspace", "apply": "echo res", "name": "mydb"}],
    }
    with pytest.raises(ConfigError, match="duplicates another"):
        parser.parse(raw, SOURCE)


def test_parse_same_name_different_scope_allowed() -> None:
    """The same short name under different scopes is not a collision."""
    parser = ProvisionManifestParser()
    raw = {
        "resource": [
            {"scope": "workspace", "apply": "echo one", "name": "mydb"},
            {"scope": "feature-environment", "apply": "echo two", "name": "mydb"},
        ]
    }
    handlers = parser.parse(raw, SOURCE)
    assert len(handlers) == 2
    assert handlers[0].name == "mydb"
    assert handlers[1].name == "mydb"
    assert handlers[0].scope != handlers[1].scope


def test_parse_duplicate_name_error_names_scope_and_name() -> None:
    parser = ProvisionManifestParser()
    raw = {
        "resource": [
            {"scope": "workspace", "apply": "echo one", "name": "mydb"},
            {"scope": "workspace", "apply": "echo two", "name": "mydb"},
        ]
    }
    with pytest.raises(ConfigError, match="mydb") as exc_info:
        parser.parse(raw, SOURCE)
    assert "workspace" in str(exc_info.value)
