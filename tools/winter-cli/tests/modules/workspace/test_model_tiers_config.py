"""Tests for the workspace-configurable model-tier table ([model_tiers]).

Covers:
  1. Overriding a built-in tier's vendor id via [model_tiers].
  2. Defining a new custom tier label and using it via [agent_model_overrides].
  3. Using a custom tier label in agent frontmatter model: field.
  4. Local-over-shared precedence for [model_tiers].
  5. Unknown tier label in [agent_model_overrides] raises ConfigError (AC#4 typo case).
  6. Custom tier missing a needed vendor mapping raises ConfigError.
  7. AgentProbeService reports stale when [model_tiers] changes alter the resolved model.
  8. A typo'd tier in override map now errors instead of passing through silently.
  9. Per-vendor merging: overriding one vendor in a built-in tier preserves the others.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from textwrap import dedent
from typing import cast

import pytest
import yaml

from tests.conftest import FakeConfigFileReader, FakeFilesystem, FakeInitReporter
from winter_cli.config.models import ModelTiersConfig, WorkspaceConfig
from winter_cli.config.workspace import CONFIG_FILE, LOCAL_CONFIG_FILE, WINTER_DIR, WorkspaceConfigService
from winter_cli.core.config_file import ConfigError
from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.modules.doctor.agent_probe_service import AgentProbeService
from winter_cli.modules.doctor.models import ProbeStatus
from winter_cli.modules.workspace.agent_install import ExtensionAgentService
from winter_cli.modules.workspace.agent_transform.agent_enumerator import CanonicalAgentEnumerator
from winter_cli.modules.workspace.agent_transform.canonical_parser import CanonicalAgentParser
from winter_cli.modules.workspace.agent_transform.model_tiers import (
    MODEL_TIER_IDS,
    ModelTier,
    build_effective_tier_table,
)
from winter_cli.modules.workspace.agent_transform.models import WorkspaceModelOverride
from winter_cli.modules.workspace.agent_transform.renderers import (
    ClaudeAgentRenderer,
    CodexAgentRenderer,
    OpenCodeAgentRenderer,
)
from winter_cli.modules.workspace.extension_manifest import ExtensionManifestLoader
from winter_cli.modules.workspace.models import RepoError, StandaloneRepository

# ---------------------------------------------------------------------------
# Shared constants and helpers
# ---------------------------------------------------------------------------

WORKSPACE_ROOT = Path("/ws")
CLAUDE_AGENTS = WORKSPACE_ROOT / ".claude" / "agents"
CODEX_AGENTS = WORKSPACE_ROOT / ".codex" / "agents"
OPENCODE_AGENTS = WORKSPACE_ROOT / ".opencode" / "agent"
EXT_ROOT = WORKSPACE_ROOT / "wf"
EXT_AGENTS = EXT_ROOT / "agents"

_PARSER = CanonicalAgentParser()


class _StubLocator:
    def __init__(self, root: Path) -> None:
        self._root = root

    def find_workspace_root(self) -> Path:
        return self._root


def _warn_sink():
    calls: list[tuple[str, str, str]] = []

    def warn(field: str, agent_name: str, vendor: str) -> None:
        calls.append((field, agent_name, vendor))

    return calls, warn


def _extract_frontmatter(text: str) -> str:
    assert text.startswith("---")
    lines = text.split("\n")
    closing = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    return "\n".join(lines[1:closing])


def _config_svc(fs: FakeFilesystem, configs: dict[Path, dict]) -> WorkspaceConfigService:
    return WorkspaceConfigService(
        workspace_locator=_StubLocator(WORKSPACE_ROOT),
        fs=fs,
        config_file_reader=FakeConfigFileReader(configs),
    )


def _probe_svc(
    config: WorkspaceConfig,
    fs: FakeFilesystem,
    config_files: dict[Path, dict] | None = None,
) -> AgentProbeService:
    loader = ExtensionManifestLoader(config_file_reader=FakeConfigFileReader(config_files or {}))
    return AgentProbeService(
        config=config,
        fs=cast(IFilesystemReader, fs),
        manifest_loader=loader,
        agent_enumerator=CanonicalAgentEnumerator(fs=cast(IFilesystemReader, fs), manifest_loader=loader),
    )


def _install_svc(
    config: WorkspaceConfig,
    fs: FakeFilesystem,
    config_files: dict[Path, dict] | None = None,
) -> ExtensionAgentService:
    loader = ExtensionManifestLoader(config_file_reader=FakeConfigFileReader(config_files or {}))
    return ExtensionAgentService(
        config=config,
        fs=fs,
        manifest_loader=loader,
        agent_enumerator=CanonicalAgentEnumerator(fs=fs, manifest_loader=loader),
    )


def _bare_config(model_tiers_cfg: ModelTiersConfig | None = None) -> WorkspaceConfig:
    cfg = WorkspaceConfig(workspace_root=WORKSPACE_ROOT, main_branch="main")
    if model_tiers_cfg is not None:
        cfg = cfg.model_copy(update={"model_tiers": model_tiers_cfg})
    return cfg


def _seed_extension(
    fs: FakeFilesystem,
    config_files: dict[Path, dict],
    agent_content: str,
    name: str = "wf",
) -> StandaloneRepository:
    ext_path = WORKSPACE_ROOT / name
    fs.directories.add(ext_path)
    for parent in ext_path.parents:
        fs.directories.add(parent)
    manifest_path = ext_path / "winter-ext.toml"
    fs.files[manifest_path] = ""
    config_files[manifest_path] = {"name": name}
    agents_dir = ext_path / "agents"
    fs.directories.add(agents_dir)
    fs.files[agents_dir / "reviewer.md"] = agent_content
    return StandaloneRepository(name=name, path=ext_path)


_SONNET_AGENT_MD = dedent("""\
    ---
    name: reviewer
    description: Reviews code changes
    model: sonnet
    ---
    You are a code reviewer.
    """)


# ---------------------------------------------------------------------------
# 1. Overriding a built-in tier's vendor id
# ---------------------------------------------------------------------------


class TestBuiltinTierOverride:
    def test_override_haiku_opencode_changes_rendered_model(self) -> None:
        """A [model_tiers.haiku] entry replaces the opencode id for that tier."""
        tier_table = build_effective_tier_table({"haiku": {"opencode": "anthropic/claude-haiku-4-20251201"}})
        agent = _PARSER.parse(_SONNET_AGENT_MD.replace("model: sonnet", "model: haiku"))

        _, warn = _warn_sink()
        r = OpenCodeAgentRenderer().render(agent, warn=warn, effective_tier_table=tier_table)

        fm = yaml.safe_load(_extract_frontmatter(r.text))
        assert fm["model"] == "anthropic/claude-haiku-4-20251201"

    def test_override_haiku_opencode_preserves_claude_and_codex_defaults(self) -> None:
        """Overriding one vendor in a built-in tier leaves other vendors at their built-in values."""
        tier_table = build_effective_tier_table({"haiku": {"opencode": "anthropic/claude-haiku-4-20251201"}})
        agent = _PARSER.parse(_SONNET_AGENT_MD.replace("model: sonnet", "model: haiku"))

        _, warn_c = _warn_sink()
        _, warn_x = _warn_sink()
        claude_r = ClaudeAgentRenderer().render(agent, warn=warn_c, effective_tier_table=tier_table)
        codex_r = CodexAgentRenderer().render(agent, warn=warn_x, effective_tier_table=tier_table)

        claude_fm = yaml.safe_load(_extract_frontmatter(claude_r.text))
        codex_doc = tomllib.loads(codex_r.text)

        assert claude_fm["model"] == MODEL_TIER_IDS[(ModelTier.haiku, "claude")]
        assert codex_doc["model"] == MODEL_TIER_IDS[(ModelTier.haiku, "codex")]

    def test_override_sonnet_claude_id(self) -> None:
        """A [model_tiers.sonnet] claude override changes the claude model id."""
        tier_table = build_effective_tier_table({"sonnet": {"claude": "claude-sonnet-4-5-20251201"}})
        agent = _PARSER.parse(_SONNET_AGENT_MD)

        _, warn = _warn_sink()
        r = ClaudeAgentRenderer().render(agent, warn=warn, effective_tier_table=tier_table)

        fm = yaml.safe_load(_extract_frontmatter(r.text))
        assert fm["model"] == "claude-sonnet-4-5-20251201"

    def test_builtin_tiers_unchanged_when_no_model_tiers_config(self) -> None:
        """With no [model_tiers] config, the effective table matches built-in MODEL_TIER_IDS."""
        tier_table = build_effective_tier_table({})
        for tier in ModelTier:
            for vendor in ("claude", "codex", "opencode"):
                assert tier_table[tier.value][vendor] == MODEL_TIER_IDS[(tier, vendor)]


# ---------------------------------------------------------------------------
# 2. Custom tier label via [agent_model_overrides]
# ---------------------------------------------------------------------------


class TestCustomTierViaOverrideMap:
    def test_custom_tier_in_override_map_resolves_for_claude(self) -> None:
        """A custom tier label in [agent_model_overrides] resolves to the right claude model."""
        tier_table = build_effective_tier_table(
            {
                "big-thinker": {
                    "claude": "claude-opus-4-20250514",
                    "codex": "gpt-5.4",
                    "opencode": "anthropic/claude-opus-4-20250514",
                }
            }
        )
        agent = _PARSER.parse(_SONNET_AGENT_MD)

        _, warn = _warn_sink()
        r = ClaudeAgentRenderer().render(
            agent,
            warn=warn,
            workspace_model_override=WorkspaceModelOverride(value="big-thinker", is_concrete=False),
            effective_tier_table=tier_table,
        )

        fm = yaml.safe_load(_extract_frontmatter(r.text))
        assert fm["model"] == "claude-opus-4-20250514"

    def test_custom_tier_in_override_map_resolves_for_codex(self) -> None:
        """A custom tier label in [agent_model_overrides] resolves for codex."""
        tier_table = build_effective_tier_table(
            {
                "big-thinker": {
                    "claude": "claude-opus-4-20250514",
                    "codex": "gpt-5.4",
                    "opencode": "anthropic/claude-opus-4-20250514",
                }
            }
        )
        agent = _PARSER.parse(_SONNET_AGENT_MD)

        _, warn = _warn_sink()
        r = CodexAgentRenderer().render(
            agent,
            warn=warn,
            workspace_model_override=WorkspaceModelOverride(value="big-thinker", is_concrete=False),
            effective_tier_table=tier_table,
        )

        doc = tomllib.loads(r.text)
        assert doc["model"] == "gpt-5.4"

    def test_custom_tier_parsed_in_overrides_validates_against_effective_table(self) -> None:
        """A known custom tier in [agent_model_overrides] validates successfully at config load."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = _config_svc(
            fs,
            {
                shared_path: {
                    "main_branch": "main",
                    "service_prefix": "ws",
                    "model_tiers": {
                        "big-thinker": {
                            "claude": "opus",
                            "codex": "gpt-5.4",
                            "opencode": "anthropic/claude-opus-4-20250514",
                        },
                    },
                    "agent_model_overrides": {"reviewer": "big-thinker"},
                }
            },
        )
        config = svc.load()
        assert config.agent_model_overrides.overrides["reviewer"] == "big-thinker"
        assert "big-thinker" in config.model_tiers.tiers


# ---------------------------------------------------------------------------
# 3. Custom tier label in agent frontmatter model:
# ---------------------------------------------------------------------------


class TestCustomTierInFrontmatter:
    def test_custom_tier_in_frontmatter_resolves_via_effective_table(self) -> None:
        """An agent with model: big-thinker resolves correctly when the tier is configured."""
        tier_table = build_effective_tier_table(
            {
                "big-thinker": {
                    "claude": "claude-opus-4-20250514",
                    "codex": "gpt-5.4",
                    "opencode": "anthropic/claude-opus-4-20250514",
                }
            }
        )
        agent_md = dedent("""\
            ---
            name: power-agent
            description: A powerful agent
            model: big-thinker
            ---
            Body.
            """)
        agent = _PARSER.parse(agent_md)
        assert agent.model_tier == "big-thinker"

        _, warn = _warn_sink()
        r = ClaudeAgentRenderer().render(agent, warn=warn, effective_tier_table=tier_table)

        fm = yaml.safe_load(_extract_frontmatter(r.text))
        assert fm["model"] == "claude-opus-4-20250514"

    def test_custom_tier_in_frontmatter_unknown_raises_repo_error(self) -> None:
        """An agent with model: unknown-tier raises RepoError at render time."""
        tier_table = build_effective_tier_table({})
        agent = _PARSER.parse("---\nname: x\ndescription: d\nmodel: unknown-tier\n---\n\nBody.\n")
        _, warn = _warn_sink()
        with pytest.raises(RepoError, match="unknown-tier"):
            ClaudeAgentRenderer().render(agent, warn=warn, effective_tier_table=tier_table)

    def test_custom_tier_missing_vendor_mapping_raises_repo_error(self) -> None:
        """A custom tier without a needed vendor mapping raises RepoError at render time."""
        # big-thinker only has claude — no codex
        tier_table = build_effective_tier_table({"big-thinker": {"claude": "claude-opus-4-20250514"}})
        agent_md = dedent("""\
            ---
            name: x
            description: d
            model: big-thinker
            ---
            Body.
            """)
        agent = _PARSER.parse(agent_md)
        _, warn = _warn_sink()
        with pytest.raises(RepoError, match="codex"):
            CodexAgentRenderer().render(agent, warn=warn, effective_tier_table=tier_table)


# ---------------------------------------------------------------------------
# 4. Local-over-shared precedence for [model_tiers]
# ---------------------------------------------------------------------------


class TestLocalOverSharedPrecedenceForModelTiers:
    def test_local_tier_entry_wins_over_shared(self) -> None:
        """A [model_tiers] entry in config.local.toml replaces the same entry from config.toml."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        local_path = WORKSPACE_ROOT / WINTER_DIR / LOCAL_CONFIG_FILE

        fs = FakeFilesystem(files={shared_path: "", local_path: ""})
        svc = _config_svc(
            fs,
            {
                shared_path: {
                    "main_branch": "main",
                    "service_prefix": "ws",
                    "model_tiers": {
                        "my-tier": {
                            "claude": "opus",
                            "codex": "gpt-5.4",
                            "opencode": "anthropic/claude-opus-4-20250514",
                        },
                    },
                },
                local_path: {
                    "model_tiers": {
                        "my-tier": {
                            "claude": "haiku",
                            "codex": "gpt-5.4-mini",
                            "opencode": "anthropic/claude-haiku-4-20250514",
                        },
                    },
                },
            },
        )
        config = svc.load()
        assert config.model_tiers.tiers["my-tier"]["claude"] == "haiku"

    def test_local_preserves_non_conflicting_shared_tier_entries(self) -> None:
        """Shared tier entries not in local are kept after merge."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        local_path = WORKSPACE_ROOT / WINTER_DIR / LOCAL_CONFIG_FILE

        fs = FakeFilesystem(files={shared_path: "", local_path: ""})
        svc = _config_svc(
            fs,
            {
                shared_path: {
                    "main_branch": "main",
                    "service_prefix": "ws",
                    "model_tiers": {
                        "tier-a": {
                            "claude": "opus",
                            "codex": "gpt-5.4",
                            "opencode": "anthropic/claude-opus-4-20250514",
                        },
                        "tier-b": {
                            "claude": "haiku",
                            "codex": "gpt-5.4-mini",
                            "opencode": "anthropic/claude-haiku-4-20250514",
                        },
                    },
                },
                local_path: {
                    "model_tiers": {
                        "tier-a": {
                            "claude": "sonnet",
                            "codex": "gpt-5.4",
                            "opencode": "anthropic/claude-sonnet-4-20250514",
                        },
                    },
                },
            },
        )
        config = svc.load()
        # Local wins for tier-a
        assert config.model_tiers.tiers["tier-a"]["claude"] == "sonnet"
        # Shared value preserved for tier-b
        assert "tier-b" in config.model_tiers.tiers
        assert config.model_tiers.tiers["tier-b"]["claude"] == "haiku"

    def test_builtin_tier_partial_override_preserves_other_vendors_in_effective_table(self) -> None:
        """Overriding one vendor for a built-in tier in local preserves others via build_effective_tier_table."""
        # Local overrides only opencode for haiku
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        local_path = WORKSPACE_ROOT / WINTER_DIR / LOCAL_CONFIG_FILE

        fs = FakeFilesystem(files={shared_path: "", local_path: ""})
        svc = _config_svc(
            fs,
            {
                shared_path: {"main_branch": "main", "service_prefix": "ws"},
                local_path: {
                    "model_tiers": {
                        "haiku": {"opencode": "anthropic/claude-haiku-4-20251201"},
                    },
                },
            },
        )
        config = svc.load()
        # The raw config only has opencode for haiku
        assert config.model_tiers.tiers.get("haiku") == {"opencode": "anthropic/claude-haiku-4-20251201"}
        # But the effective tier table merges with built-in, so claude and codex still present
        from winter_cli.modules.workspace.agent_transform.model_tiers import build_effective_tier_table

        eff = build_effective_tier_table(config.model_tiers.tiers)
        assert eff["haiku"]["opencode"] == "anthropic/claude-haiku-4-20251201"
        assert eff["haiku"]["claude"] == MODEL_TIER_IDS[(ModelTier.haiku, "claude")]
        assert eff["haiku"]["codex"] == MODEL_TIER_IDS[(ModelTier.haiku, "codex")]


# ---------------------------------------------------------------------------
# 5. Unknown tier label in [agent_model_overrides] raises ConfigError (AC#4)
# ---------------------------------------------------------------------------


class TestAC4TypoTierError:
    def test_typo_tier_in_override_raises_config_error_at_load_time(self) -> None:
        """A typo'd tier (snonet) in [agent_model_overrides] raises ConfigError at config load."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = _config_svc(
            fs,
            {
                shared_path: {
                    "main_branch": "main",
                    "service_prefix": "ws",
                    "agent_model_overrides": {"reviewer": "snonet"},
                }
            },
        )

        with pytest.raises(ConfigError, match="snonet"):
            svc.load()

    def test_valid_builtin_tier_does_not_raise(self) -> None:
        """Built-in tier labels (opus, sonnet, haiku) pass validation at config load."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = _config_svc(
            fs,
            {
                shared_path: {
                    "main_branch": "main",
                    "service_prefix": "ws",
                    "agent_model_overrides": {
                        "agent-a": "opus",
                        "agent-b": "sonnet",
                        "agent-c": "haiku",
                    },
                }
            },
        )
        config = svc.load()
        assert config.agent_model_overrides.overrides["agent-a"] == "opus"

    def test_custom_tier_in_override_is_valid_when_model_tiers_defines_it(self) -> None:
        """A custom tier label in [agent_model_overrides] is valid when [model_tiers] defines it."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = _config_svc(
            fs,
            {
                shared_path: {
                    "main_branch": "main",
                    "service_prefix": "ws",
                    "model_tiers": {
                        "smol": {
                            "claude": "haiku",
                            "codex": "gpt-5.4-mini",
                            "opencode": "anthropic/claude-haiku-4-20250514",
                        },
                    },
                    "agent_model_overrides": {"reviewer": "smol"},
                }
            },
        )
        config = svc.load()
        assert config.agent_model_overrides.overrides["reviewer"] == "smol"

    def test_concrete_id_as_bare_string_in_override_raises_config_error(self) -> None:
        """A bare concrete model id (not a tier label) raises ConfigError at config load."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = _config_svc(
            fs,
            {
                shared_path: {
                    "main_branch": "main",
                    "service_prefix": "ws",
                    "agent_model_overrides": {"reviewer": "claude-opus-4-20250514"},
                }
            },
        )

        with pytest.raises(ConfigError, match="not a recognised tier label"):
            svc.load()

    def test_concrete_id_in_per_vendor_dict_does_not_raise(self) -> None:
        """A concrete model id in per-vendor inline-table form passes validation."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = _config_svc(
            fs,
            {
                shared_path: {
                    "main_branch": "main",
                    "service_prefix": "ws",
                    "agent_model_overrides": {
                        "reviewer": {"claude": "claude-opus-4-20250514"},
                    },
                }
            },
        )
        config = svc.load()
        assert config.agent_model_overrides.overrides["reviewer"] == {"claude": "claude-opus-4-20250514"}


# ---------------------------------------------------------------------------
# 6. Custom tier missing a needed vendor mapping raises ConfigError
# ---------------------------------------------------------------------------


class TestMissingVendorMappingInCustomTier:
    def test_workspace_override_with_custom_tier_missing_vendor_raises(self) -> None:
        """A workspace override using a custom tier that lacks a vendor mapping raises RepoError."""
        # big-thinker only maps claude — no opencode
        tier_table = build_effective_tier_table(
            {"big-thinker": {"claude": "claude-opus-4-20250514", "codex": "gpt-5.4"}}
        )
        agent = _PARSER.parse(_SONNET_AGENT_MD)
        _, warn = _warn_sink()
        with pytest.raises(RepoError, match="big-thinker"):
            OpenCodeAgentRenderer().render(
                agent,
                warn=warn,
                workspace_model_override=WorkspaceModelOverride(value="big-thinker", is_concrete=False),
                effective_tier_table=tier_table,
            )

    def test_frontmatter_tier_missing_vendor_raises_repo_error(self) -> None:
        """An agent with a frontmatter tier lacking a vendor mapping raises RepoError at render."""
        # smol-tier only has claude
        tier_table = build_effective_tier_table({"smol-tier": {"claude": "haiku"}})
        agent = _PARSER.parse("---\nname: x\ndescription: d\nmodel: smol-tier\n---\n\nBody.\n")
        _, warn = _warn_sink()
        with pytest.raises(RepoError, match="codex"):
            CodexAgentRenderer().render(agent, warn=warn, effective_tier_table=tier_table)

    def test_error_message_names_missing_label_and_vendor(self) -> None:
        """RepoError message names both the tier label and missing vendor."""
        tier_table = build_effective_tier_table({"my-tier": {"claude": "opus"}})
        agent = _PARSER.parse("---\nname: x\ndescription: d\nmodel: my-tier\n---\n\nBody.\n")
        _, warn = _warn_sink()
        try:
            CodexAgentRenderer().render(agent, warn=warn, effective_tier_table=tier_table)
            pytest.fail("expected RepoError")
        except RepoError as exc:
            assert "my-tier" in str(exc)
            assert "codex" in str(exc)


# ---------------------------------------------------------------------------
# 7. AgentProbeService reports stale when [model_tiers] changes
# ---------------------------------------------------------------------------


class TestStalenessOnModelTiersChange:
    def test_probe_detects_stale_after_model_tiers_change(self) -> None:
        """Adding a [model_tiers] override that changes a resolved model makes copies stale."""
        fs = FakeFilesystem()
        config_files: dict[Path, dict] = {}

        # Seed an agent that uses sonnet (built-in haiku opencode = anthropic/claude-haiku-...)
        haiku_agent_md = dedent("""\
            ---
            name: reviewer
            description: Reviews code changes
            model: haiku
            ---
            You are a code reviewer.
            """)
        ext = _seed_extension(fs, config_files, haiku_agent_md)

        # Install WITHOUT any model_tiers config.
        cfg_no_override = WorkspaceConfig(workspace_root=WORKSPACE_ROOT, main_branch="main")
        reporter = FakeInitReporter()
        _install_svc(cfg_no_override, fs, config_files).process(ext, reporter)

        # Probe without model_tiers → should pass (installer and probe agree).
        pre_probe = _probe_svc(cfg_no_override, fs, config_files).run([ext])
        assert all(r.status == ProbeStatus.pass_ for r in pre_probe), (
            f"pre-probe failures: {[(r.name, r.message) for r in pre_probe if r.status != ProbeStatus.pass_]}"
        )

        # Now probe WITH a [model_tiers] change that changes haiku's opencode id.
        cfg_with_tiers = WorkspaceConfig(
            workspace_root=WORKSPACE_ROOT,
            main_branch="main",
            model_tiers=ModelTiersConfig(tiers={"haiku": {"opencode": "anthropic/claude-haiku-4-20251201"}}),
        )
        post_probe = _probe_svc(cfg_with_tiers, fs, config_files).run([ext])

        stale = [r for r in post_probe if r.status == ProbeStatus.warn and "stale copy" in r.message]
        assert stale, (
            "expected at least one stale-copy WARN after model_tiers change; "
            f"got: {[(r.name, r.status, r.message) for r in post_probe]}"
        )

    def test_reinstall_with_model_tiers_heals_staleness(self) -> None:
        """After re-running ws init with model_tiers config active, probe passes."""
        fs = FakeFilesystem()
        config_files: dict[Path, dict] = {}

        haiku_agent_md = dedent("""\
            ---
            name: reviewer
            description: Reviews code changes
            model: haiku
            ---
            You are a code reviewer.
            """)
        ext = _seed_extension(fs, config_files, haiku_agent_md)

        cfg_with_tiers = WorkspaceConfig(
            workspace_root=WORKSPACE_ROOT,
            main_branch="main",
            model_tiers=ModelTiersConfig(tiers={"haiku": {"opencode": "anthropic/claude-haiku-4-20251201"}}),
        )
        reporter = FakeInitReporter()
        _install_svc(cfg_with_tiers, fs, config_files).process(ext, reporter)

        # Probe with same config → should all pass.
        results = _probe_svc(cfg_with_tiers, fs, config_files).run([ext])
        vendor_failures = [r for r in results if "agent copies:" in r.name and r.status != ProbeStatus.pass_]
        assert not vendor_failures, (
            f"expected all vendor probes to pass after install with model_tiers: "
            f"{[(r.name, r.status, r.message) for r in vendor_failures]}"
        )


# ---------------------------------------------------------------------------
# 8. build_effective_tier_table contract
# ---------------------------------------------------------------------------


class TestBuildEffectiveTierTable:
    def test_empty_custom_returns_only_builtins(self) -> None:
        """With no custom tiers, the effective table matches the built-in set."""
        table = build_effective_tier_table({})
        assert set(table) == {"opus", "sonnet", "haiku"}

    def test_new_custom_tier_added_to_table(self) -> None:
        """A new custom tier label is present in the effective table."""
        table = build_effective_tier_table(
            {"big-thinker": {"claude": "opus", "codex": "gpt-5.4", "opencode": "anthropic/claude-opus-4-20250514"}}
        )
        assert "big-thinker" in table
        assert table["big-thinker"]["claude"] == "opus"

    def test_builtin_override_merges_per_vendor(self) -> None:
        """Overriding one vendor for a built-in tier leaves other vendors unchanged."""
        table = build_effective_tier_table({"haiku": {"opencode": "anthropic/claude-haiku-4-20251201"}})
        assert table["haiku"]["opencode"] == "anthropic/claude-haiku-4-20251201"
        # Built-in values preserved for unlisted vendors
        assert table["haiku"]["claude"] == MODEL_TIER_IDS[(ModelTier.haiku, "claude")]
        assert table["haiku"]["codex"] == MODEL_TIER_IDS[(ModelTier.haiku, "codex")]

    def test_complete_builtin_override_replaces_all_vendors(self) -> None:
        """Overriding all three vendors for a built-in tier replaces all of them."""
        table = build_effective_tier_table(
            {"opus": {"claude": "new-opus-claude", "codex": "new-opus-codex", "opencode": "new-opus-opencode"}}
        )
        assert table["opus"]["claude"] == "new-opus-claude"
        assert table["opus"]["codex"] == "new-opus-codex"
        assert table["opus"]["opencode"] == "new-opus-opencode"


# ---------------------------------------------------------------------------
# 9. [model_tiers] config parse validation
# ---------------------------------------------------------------------------


class TestModelTiersConfigParsing:
    def test_valid_model_tiers_parses_cleanly(self) -> None:
        """A valid [model_tiers] config parses without error."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = _config_svc(
            fs,
            {
                shared_path: {
                    "main_branch": "main",
                    "service_prefix": "ws",
                    "model_tiers": {
                        "smol": {
                            "claude": "haiku",
                            "codex": "gpt-5.4-mini",
                            "opencode": "anthropic/claude-haiku-4-20250514",
                        },
                    },
                }
            },
        )
        config = svc.load()
        assert config.model_tiers.tiers["smol"]["claude"] == "haiku"

    def test_unknown_vendor_in_model_tiers_raises_config_error(self) -> None:
        """An unknown vendor label in [model_tiers] raises ConfigError."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = _config_svc(
            fs,
            {
                shared_path: {
                    "main_branch": "main",
                    "service_prefix": "ws",
                    "model_tiers": {
                        "my-tier": {"unknown-vendor": "some-model"},
                    },
                }
            },
        )
        with pytest.raises(ConfigError, match="unknown vendor label"):
            svc.load()

    def test_empty_vendor_dict_raises_config_error(self) -> None:
        """An empty vendor dict for a tier label raises ConfigError."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = _config_svc(
            fs,
            {
                shared_path: {
                    "main_branch": "main",
                    "service_prefix": "ws",
                    "model_tiers": {
                        "my-tier": {},
                    },
                }
            },
        )
        with pytest.raises(ConfigError, match="at least one vendor"):
            svc.load()

    def test_non_string_vendor_value_raises_config_error(self) -> None:
        """A non-string model id in [model_tiers] raises ConfigError."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = _config_svc(
            fs,
            {
                shared_path: {
                    "main_branch": "main",
                    "service_prefix": "ws",
                    "model_tiers": {
                        "my-tier": {"claude": 42},
                    },
                }
            },
        )
        with pytest.raises(ConfigError, match="non-empty string"):
            svc.load()

    def test_absent_model_tiers_table_defaults_to_empty(self) -> None:
        """When [model_tiers] is absent, the config loads cleanly with no custom tiers."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = _config_svc(
            fs,
            {shared_path: {"main_branch": "main", "service_prefix": "ws"}},
        )
        config = svc.load()
        assert config.model_tiers.tiers == {}
