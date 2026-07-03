"""Tests for the workspace-level agent→model override map (issue #133).

Covers the six acceptance-criterion scenarios:
  1. Tier override wins over frontmatter (``model: sonnet`` → workspace sets ``haiku``).
  2. Concrete-model override resolves (non-tier string passes through as the model id).
  3. Per-vendor scoping (``{claude = "..."}`` only applies to claude; others use tier table).
  4. Local-over-shared precedence (``config.local.toml`` entry wins over ``config.toml``).
  5. Unknown-target error (override key has no matching agent → ``AgentProbeService`` WARN).
  6. ``AgentProbeService`` flags stale when the override changes (byte-level staleness).

Plus parse-time validation: invalid vendor label → ``ConfigError``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from textwrap import dedent
from typing import cast

import pytest
import yaml

from tests.conftest import FakeConfigFileReader, FakeFilesystem, FakeInitReporter
from winter_cli.config.models import AgentModelOverridesConfig, CodeAgentVendor, WorkspaceConfig
from winter_cli.config.workspace import CONFIG_FILE, LOCAL_CONFIG_FILE, WINTER_DIR, WorkspaceConfigService
from winter_cli.core.config_file import ConfigError
from winter_cli.core.filesystem import IFilesystemReader
from winter_cli.modules.doctor.agent_probe_service import AgentProbeService
from winter_cli.modules.doctor.models import ProbeStatus
from winter_cli.modules.workspace.agent_install import ExtensionAgentService
from winter_cli.modules.workspace.agent_transform.agent_enumerator import CanonicalAgentEnumerator
from winter_cli.modules.workspace.agent_transform.canonical_parser import CanonicalAgentParser
from winter_cli.modules.workspace.agent_transform.model_tiers import MODEL_TIER_IDS, ModelTier
from winter_cli.modules.workspace.agent_transform.models import WorkspaceModelOverride
from winter_cli.modules.workspace.agent_transform.renderers import (
    ClaudeAgentRenderer,
    CodexAgentRenderer,
    OpenCodeAgentRenderer,
    resolve_workspace_model_override,
)
from winter_cli.modules.workspace.extension_manifest import ExtensionManifestLoader
from winter_cli.modules.workspace.models import StandaloneRepository

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

WORKSPACE_ROOT = Path("/ws")
CLAUDE_AGENTS = WORKSPACE_ROOT / ".claude" / "agents"
CODEX_AGENTS = WORKSPACE_ROOT / ".codex" / "agents"
OPENCODE_AGENTS = WORKSPACE_ROOT / ".opencode" / "agent"
EXT_ROOT = WORKSPACE_ROOT / "wf"
EXT_AGENTS = EXT_ROOT / "agents"

_PARSER = CanonicalAgentParser()


def _tier(value: str) -> WorkspaceModelOverride:
    """Build a bare-string-form workspace override (tier label, resolved via tier table)."""
    return WorkspaceModelOverride(value=value, is_concrete=False)


def _concrete(value: str) -> WorkspaceModelOverride:
    """Build a per-vendor-form workspace override (concrete model id, passed through)."""
    return WorkspaceModelOverride(value=value, is_concrete=True)


def _warn_sink():
    calls: list[tuple[str, str, str]] = []

    def warn(field: str, agent_name: str, vendor: str) -> None:
        calls.append((field, agent_name, vendor))

    return calls, warn


# A canonical agent that uses the sonnet tier by default.
_SONNET_AGENT_MD = dedent("""\
    ---
    name: reviewer
    description: Reviews code changes
    model: sonnet
    ---
    You are a code reviewer.
    """)

# An agent that also has a per-harness claude: model override in frontmatter.
_FRONTMATTER_OVERRIDE_MD = dedent("""\
    ---
    name: reviewer
    description: Reviews code changes
    model: sonnet
    claude:
      model: claude-opus-4-20250514
    ---
    You are a code reviewer.
    """)


def _config(overrides: dict[str, str | dict[str, str]] | None = None) -> WorkspaceConfig:
    """Build a minimal WorkspaceConfig with an optional override map."""
    cfg = WorkspaceConfig(
        workspace_root=WORKSPACE_ROOT,
        main_branch="main",
    )
    if overrides is not None:
        cfg = cfg.model_copy(update={"agent_model_overrides": AgentModelOverridesConfig(overrides=overrides)})
    return cfg


def _manifest_loader(config_files: dict[Path, dict] | None = None) -> ExtensionManifestLoader:
    return ExtensionManifestLoader(config_file_reader=FakeConfigFileReader(config_files or {}))


def _probe_svc(
    config: WorkspaceConfig,
    fs: FakeFilesystem,
    config_files: dict[Path, dict] | None = None,
) -> AgentProbeService:
    loader = _manifest_loader(config_files)
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
    loader = _manifest_loader(config_files)
    return ExtensionAgentService(
        config=config,
        fs=fs,
        manifest_loader=loader,
        agent_enumerator=CanonicalAgentEnumerator(fs=fs, manifest_loader=loader),
    )


def _seed_extension(
    fs: FakeFilesystem,
    config_files: dict[Path, dict],
    agent_content: str = _SONNET_AGENT_MD,
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


def _extract_frontmatter(text: str) -> str:
    assert text.startswith("---")
    lines = text.split("\n")
    closing = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    return "\n".join(lines[1:closing])


# ===========================================================================
# 1. Tier override wins over frontmatter
# ===========================================================================


class TestTierOverride:
    def test_haiku_tier_override_beats_sonnet_frontmatter_claude(self) -> None:
        """A workspace haiku-tier override replaces the frontmatter sonnet tier for claude."""
        agent = _PARSER.parse(_SONNET_AGENT_MD)
        assert agent.model_tier == "sonnet"

        _, warn = _warn_sink()
        r = ClaudeAgentRenderer().render(agent, warn=warn, workspace_model_override=_tier("haiku"))

        fm = yaml.safe_load(_extract_frontmatter(r.text))
        assert fm["model"] == "haiku", f"expected haiku, got {fm['model']!r}"

    def test_haiku_tier_override_resolves_via_tier_table_for_codex(self) -> None:
        """A workspace haiku-tier override resolves through MODEL_TIER_IDS for codex."""
        agent = _PARSER.parse(_SONNET_AGENT_MD)

        _, warn = _warn_sink()
        r = CodexAgentRenderer().render(agent, warn=warn, workspace_model_override=_tier("haiku"))

        doc = tomllib.loads(r.text)
        expected = MODEL_TIER_IDS[(ModelTier.haiku, "codex")]
        assert doc["model"] == expected, f"expected {expected!r}, got {doc['model']!r}"

    def test_haiku_tier_override_resolves_via_tier_table_for_opencode(self) -> None:
        """A workspace haiku-tier override resolves through MODEL_TIER_IDS for opencode."""
        agent = _PARSER.parse(_SONNET_AGENT_MD)

        _, warn = _warn_sink()
        r = OpenCodeAgentRenderer().render(agent, warn=warn, workspace_model_override=_tier("haiku"))

        fm = yaml.safe_load(_extract_frontmatter(r.text))
        expected = MODEL_TIER_IDS[(ModelTier.haiku, "opencode")]
        assert fm["model"] == expected

    def test_workspace_tier_override_beats_frontmatter_per_harness_block(self) -> None:
        """Workspace override wins over the agent's own per-harness claude: model: block."""
        agent = _PARSER.parse(_FRONTMATTER_OVERRIDE_MD)
        # The claude: block sets claude-opus-4-20250514, but workspace override is haiku.
        assert agent.overrides.get("claude", {}).get("model") == "claude-opus-4-20250514"

        _, warn = _warn_sink()
        r = ClaudeAgentRenderer().render(agent, warn=warn, workspace_model_override=_tier("haiku"))

        fm = yaml.safe_load(_extract_frontmatter(r.text))
        assert fm["model"] == "haiku"

    def test_opus_tier_override_all_three_vendors(self) -> None:
        """Opus-tier workspace override resolves correctly for all three vendors."""
        agent = _PARSER.parse(_SONNET_AGENT_MD)

        for vendor in CodeAgentVendor:
            _, warn = _warn_sink()
            renderer = {
                "claude": ClaudeAgentRenderer(),
                "codex": CodexAgentRenderer(),
                "opencode": OpenCodeAgentRenderer(),
            }[vendor.vendor_label]
            r = renderer.render(agent, warn=warn, workspace_model_override=_tier("opus"))
            expected = MODEL_TIER_IDS[(ModelTier.opus, vendor.vendor_label)]

            if vendor.vendor_label == "codex":
                doc = tomllib.loads(r.text)
                assert doc["model"] == expected
            else:
                fm = yaml.safe_load(_extract_frontmatter(r.text))
                assert fm["model"] == expected


# ===========================================================================
# 2. Concrete-model override resolves
# ===========================================================================


class TestConcreteModelOverride:
    def test_concrete_id_passes_through_unchanged_claude(self) -> None:
        """A concrete model id (non-tier string) passes through as-is for claude."""
        agent = _PARSER.parse(_SONNET_AGENT_MD)
        _, warn = _warn_sink()
        r = ClaudeAgentRenderer().render(
            agent, warn=warn, workspace_model_override=_concrete("claude-sonnet-4-5-20251201")
        )

        fm = yaml.safe_load(_extract_frontmatter(r.text))
        assert fm["model"] == "claude-sonnet-4-5-20251201"

    def test_concrete_id_passes_through_unchanged_codex(self) -> None:
        """A concrete model id (non-tier string) passes through as-is for codex."""
        agent = _PARSER.parse(_SONNET_AGENT_MD)
        _, warn = _warn_sink()
        r = CodexAgentRenderer().render(agent, warn=warn, workspace_model_override=_concrete("gpt-5.4-experimental"))

        doc = tomllib.loads(r.text)
        assert doc["model"] == "gpt-5.4-experimental"

    def test_concrete_id_passes_through_unchanged_opencode(self) -> None:
        """A concrete model id (non-tier string) passes through as-is for opencode."""
        agent = _PARSER.parse(_SONNET_AGENT_MD)
        _, warn = _warn_sink()
        r = OpenCodeAgentRenderer().render(
            agent, warn=warn, workspace_model_override=_concrete("anthropic/claude-opus-5-20260101")
        )

        fm = yaml.safe_load(_extract_frontmatter(r.text))
        assert fm["model"] == "anthropic/claude-opus-5-20260101"

    def test_none_workspace_override_falls_through_to_tier_table(self) -> None:
        """When workspace_model_override is None, tier table resolution is used."""
        agent = _PARSER.parse(_SONNET_AGENT_MD)
        _, warn = _warn_sink()
        r = ClaudeAgentRenderer().render(agent, warn=warn, workspace_model_override=None)

        fm = yaml.safe_load(_extract_frontmatter(r.text))
        assert fm["model"] == "sonnet"


# ===========================================================================
# 3. Per-vendor scoping
# ===========================================================================


class TestPerVendorScoping:
    def test_resolve_workspace_model_override_all_vendors_string(self) -> None:
        """A string override value applies to every vendor as a tier label."""
        overrides: dict[str, str | dict[str, str]] = {"reviewer": "haiku"}
        for vendor in CodeAgentVendor:
            result = resolve_workspace_model_override(overrides, "reviewer", vendor.vendor_label)
            assert result is not None
            assert result.value == "haiku", f"{vendor.vendor_label}: expected 'haiku', got {result.value!r}"
            assert result.is_concrete is False

    def test_resolve_workspace_model_override_per_vendor_dict_hit(self) -> None:
        """A dict override value returns the concrete entry for the matching vendor."""
        overrides: dict[str, str | dict[str, str]] = {
            "reviewer": {"claude": "claude-opus-4-20250514", "codex": "gpt-5.4"}
        }
        claude_result = resolve_workspace_model_override(overrides, "reviewer", "claude")
        codex_result = resolve_workspace_model_override(overrides, "reviewer", "codex")
        assert claude_result is not None and claude_result.value == "claude-opus-4-20250514"
        assert claude_result.is_concrete is True
        assert codex_result is not None and codex_result.value == "gpt-5.4"
        assert codex_result.is_concrete is True

    def test_resolve_workspace_model_override_per_vendor_dict_value_colliding_with_tier_label(self) -> None:
        """A per-vendor concrete id that collides with a tier label name is not tier-resolved."""
        overrides: dict[str, str | dict[str, str]] = {"coder": {"opencode": "haiku"}}
        result = resolve_workspace_model_override(overrides, "coder", "opencode")
        assert result is not None
        assert result.value == "haiku"
        assert result.is_concrete is True

        agent = _PARSER.parse(_SONNET_AGENT_MD)
        _, warn = _warn_sink()
        r = OpenCodeAgentRenderer().render(agent, warn=warn, workspace_model_override=result)
        fm = yaml.safe_load(_extract_frontmatter(r.text))
        # Passed through literally as "haiku" — NOT resolved to the haiku tier's
        # opencode model id (anthropic/claude-haiku-4-20250514).
        assert fm["model"] == "haiku"

    def test_resolve_workspace_model_override_per_vendor_dict_miss(self) -> None:
        """A dict override returns None for vendors not listed."""
        overrides: dict[str, str | dict[str, str]] = {"reviewer": {"claude": "haiku"}}
        assert resolve_workspace_model_override(overrides, "reviewer", "codex") is None
        assert resolve_workspace_model_override(overrides, "reviewer", "opencode") is None

    def test_per_vendor_override_only_affects_named_vendor(self) -> None:
        """A claude-scoped override changes claude output but not codex."""
        agent = _PARSER.parse(_SONNET_AGENT_MD)
        overrides: dict[str, str | dict[str, str]] = {"reviewer": {"claude": "haiku"}}

        claude_override = resolve_workspace_model_override(overrides, "reviewer", "claude")
        codex_override = resolve_workspace_model_override(overrides, "reviewer", "codex")

        _, warn_c = _warn_sink()
        _, warn_x = _warn_sink()
        claude_r = ClaudeAgentRenderer().render(agent, warn=warn_c, workspace_model_override=claude_override)
        codex_r = CodexAgentRenderer().render(agent, warn=warn_x, workspace_model_override=codex_override)

        claude_fm = yaml.safe_load(_extract_frontmatter(claude_r.text))
        codex_doc = tomllib.loads(codex_r.text)

        assert claude_fm["model"] == "haiku"
        # codex_override is None → falls through to tier table for sonnet
        assert codex_doc["model"] == MODEL_TIER_IDS[(ModelTier.sonnet, "codex")]

    def test_unknown_agent_in_overrides_returns_none(self) -> None:
        """An agent name not in the override map returns None for all vendors."""
        overrides: dict[str, str | dict[str, str]] = {"other-agent": "haiku"}
        for vendor in CodeAgentVendor:
            result = resolve_workspace_model_override(overrides, "reviewer", vendor.vendor_label)
            assert result is None


# ===========================================================================
# 4. Local-over-shared precedence
# ===========================================================================


class _StubLocator:
    def __init__(self, root: Path) -> None:
        self._root = root

    def find_workspace_root(self) -> Path:
        return self._root


class TestLocalOverSharedPrecedence:
    """Tests that config.local.toml entries win over config.toml entries per-agent."""

    def _service(self, fs: FakeFilesystem, configs: dict[Path, dict]) -> WorkspaceConfigService:
        return WorkspaceConfigService(
            workspace_locator=_StubLocator(WORKSPACE_ROOT),
            fs=fs,
            config_file_reader=FakeConfigFileReader(configs),
        )

    def test_local_override_wins_over_shared(self) -> None:
        """An entry in config.local.toml replaces the same entry from config.toml."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        local_path = WORKSPACE_ROOT / WINTER_DIR / LOCAL_CONFIG_FILE

        fs = FakeFilesystem(files={shared_path: "", local_path: ""})
        svc = self._service(
            fs,
            {
                shared_path: {
                    "main_branch": "main",
                    "service_prefix": "ws",
                    "agent_model_overrides": {"reviewer": "haiku"},
                },
                local_path: {
                    "agent_model_overrides": {"reviewer": "opus"},
                },
            },
        )

        config = svc.load()

        assert config.agent_model_overrides.overrides.get("reviewer") == "opus"

    def test_local_override_preserves_non_conflicting_shared_entries(self) -> None:
        """Shared entries not mentioned in local are kept after merge."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        local_path = WORKSPACE_ROOT / WINTER_DIR / LOCAL_CONFIG_FILE

        fs = FakeFilesystem(files={shared_path: "", local_path: ""})
        svc = self._service(
            fs,
            {
                shared_path: {
                    "main_branch": "main",
                    "service_prefix": "ws",
                    "agent_model_overrides": {
                        "reviewer": "haiku",
                        "developer": "sonnet",
                    },
                },
                local_path: {
                    "agent_model_overrides": {"reviewer": "opus"},
                },
            },
        )

        config = svc.load()

        # Local wins for reviewer; shared value preserved for developer.
        assert config.agent_model_overrides.overrides["reviewer"] == "opus"
        assert config.agent_model_overrides.overrides["developer"] == "sonnet"

    def test_no_local_override_uses_shared_value(self) -> None:
        """When local doesn't override, the shared value is used unchanged."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE

        fs = FakeFilesystem(files={shared_path: ""})
        svc = self._service(
            fs,
            {
                shared_path: {
                    "main_branch": "main",
                    "service_prefix": "ws",
                    "agent_model_overrides": {"reviewer": "haiku"},
                },
            },
        )

        config = svc.load()
        assert config.agent_model_overrides.overrides["reviewer"] == "haiku"

    def test_absent_overrides_table_defaults_to_empty(self) -> None:
        """When [agent_model_overrides] is absent, the config loads cleanly with no overrides."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = self._service(
            fs,
            {shared_path: {"main_branch": "main", "service_prefix": "ws"}},
        )

        config = svc.load()
        assert config.agent_model_overrides.overrides == {}


# ===========================================================================
# 5a. Unknown-target warning at ws init (ExtensionAgentService.check_unknown_overrides)
# ===========================================================================


class TestUnknownOverrideAtInit:
    """check_unknown_overrides surfaces unknown override keys at ws init time."""

    def test_unknown_agent_name_warns_via_reporter(self) -> None:
        """An override for a non-existent agent emits agent_override_warning at init."""
        fs = FakeFilesystem()
        config_files: dict[Path, dict] = {}
        ext = _seed_extension(fs, config_files)  # seeds 'reviewer' agent
        cfg = _config(overrides={"ghost-agent": "haiku"})
        reporter = FakeInitReporter()

        _install_svc(cfg, fs, config_files).check_unknown_overrides([ext], reporter)

        warnings = [a for a in reporter.actions if a[2] == "agent_override_warning"]
        assert warnings, "expected an agent_override_warning action in reporter"
        assert any("ghost-agent" in a[3] for a in warnings)

    def test_known_agent_name_emits_no_warning(self) -> None:
        """An override key that matches an installed agent emits no warning."""
        fs = FakeFilesystem()
        config_files: dict[Path, dict] = {}
        ext = _seed_extension(fs, config_files)  # seeds 'reviewer' agent
        cfg = _config(overrides={"reviewer": "haiku"})
        reporter = FakeInitReporter()

        _install_svc(cfg, fs, config_files).check_unknown_overrides([ext], reporter)

        warnings = [a for a in reporter.actions if a[2] == "agent_override_warning"]
        assert not warnings

    def test_no_overrides_configured_emits_no_warning(self) -> None:
        """When no [agent_model_overrides] are configured, no warning is emitted."""
        fs = FakeFilesystem()
        config_files: dict[Path, dict] = {}
        ext = _seed_extension(fs, config_files)
        cfg = _config(overrides=None)
        reporter = FakeInitReporter()

        _install_svc(cfg, fs, config_files).check_unknown_overrides([ext], reporter)

        warnings = [a for a in reporter.actions if a[2] == "agent_override_warning"]
        assert not warnings

    def test_multiple_unknown_agents_included_in_single_warning(self) -> None:
        """Multiple unknown override keys appear together in one warning action."""
        fs = FakeFilesystem()
        config_files: dict[Path, dict] = {}
        ext = _seed_extension(fs, config_files)  # seeds 'reviewer' agent
        cfg = _config(overrides={"ghost": "haiku", "phantom": "opus"})
        reporter = FakeInitReporter()

        _install_svc(cfg, fs, config_files).check_unknown_overrides([ext], reporter)

        warnings = [a for a in reporter.actions if a[2] == "agent_override_warning"]
        assert warnings
        combined = " ".join(a[3] for a in warnings)
        assert "ghost" in combined
        assert "phantom" in combined

    def test_returns_true_regardless_of_unknown_agents(self) -> None:
        """check_unknown_overrides always returns True — warnings do not fail init."""
        fs = FakeFilesystem()
        config_files: dict[Path, dict] = {}
        ext = _seed_extension(fs, config_files)
        cfg = _config(overrides={"no-such-agent": "haiku"})
        reporter = FakeInitReporter()

        result = _install_svc(cfg, fs, config_files).check_unknown_overrides([ext], reporter)

        assert result is True


# ===========================================================================
# 5. Unknown-target error via AgentProbeService
# ===========================================================================


class TestUnknownTargetError:
    def test_unknown_agent_name_reports_warn(self) -> None:
        """An override entry whose agent name is not among any known agent → WARN."""
        fs = FakeFilesystem()
        config_files: dict[Path, dict] = {}
        ext = _seed_extension(fs, config_files)
        cfg = _config(overrides={"ghost-agent": "haiku"})

        svc = _probe_svc(cfg, fs, config_files)
        results = svc.run([ext])

        target_result = next((r for r in results if "overrides: targets" in r.name), None)
        assert target_result is not None, "expected an override-target probe result"
        assert target_result.status == ProbeStatus.warn
        assert "ghost-agent" in target_result.message
        assert target_result.remediation is not None

    def test_known_agent_name_reports_pass(self) -> None:
        """An override entry whose agent name matches an existing agent → PASS."""
        fs = FakeFilesystem()
        config_files: dict[Path, dict] = {}
        ext = _seed_extension(fs, config_files)  # seeds reviewer.md → name=reviewer
        cfg = _config(overrides={"reviewer": "haiku"})

        svc = _probe_svc(cfg, fs, config_files)
        results = svc.run([ext])

        target_result = next((r for r in results if "overrides: targets" in r.name), None)
        assert target_result is not None
        assert target_result.status == ProbeStatus.pass_

    def test_no_overrides_emits_no_target_result(self) -> None:
        """When no overrides are configured, the target probe emits no result."""
        fs = FakeFilesystem()
        config_files: dict[Path, dict] = {}
        ext = _seed_extension(fs, config_files)
        cfg = _config(overrides=None)  # empty override map

        svc = _probe_svc(cfg, fs, config_files)
        results = svc.run([ext])

        target_results = [r for r in results if "overrides: targets" in r.name]
        assert target_results == []

    def test_multiple_unknown_agents_reported_together(self) -> None:
        """Multiple unknown agent names are included in one WARN message."""
        fs = FakeFilesystem()
        config_files: dict[Path, dict] = {}
        ext = _seed_extension(fs, config_files)
        cfg = _config(overrides={"ghost": "haiku", "phantom": "opus"})

        svc = _probe_svc(cfg, fs, config_files)
        results = svc.run([ext])

        target_result = next((r for r in results if "overrides: targets" in r.name), None)
        assert target_result is not None
        assert target_result.status == ProbeStatus.warn
        assert "ghost" in target_result.message
        assert "phantom" in target_result.message


# ===========================================================================
# 6. AgentProbeService flags stale on override change
# ===========================================================================


class TestStalenessOnOverrideChange:
    def test_probe_detects_stale_after_override_added(self) -> None:
        """Adding a workspace override changes the expected bytes → probe flags stale."""
        fs = FakeFilesystem()
        config_files: dict[Path, dict] = {}
        ext = _seed_extension(fs, config_files)

        # Install without any override.
        cfg_no_override = _config(overrides=None)
        reporter = FakeInitReporter()
        _install_svc(cfg_no_override, fs, config_files).process(ext, reporter)

        # Probe without override → should pass (installer and probe agree).
        pre_probe = _probe_svc(cfg_no_override, fs, config_files).run([ext])
        assert all(r.status == ProbeStatus.pass_ for r in pre_probe), (
            f"expected all pass before override: {[(r.name, r.message) for r in pre_probe if r.status != ProbeStatus.pass_]}"
        )

        # Now probe WITH an override active — the on-disk copies were written
        # without the override, so they should now be stale.
        cfg_with_override = _config(overrides={"reviewer": "haiku"})
        post_probe = _probe_svc(cfg_with_override, fs, config_files).run([ext])

        stale = [r for r in post_probe if r.status == ProbeStatus.warn and "stale copy" in r.message]
        assert stale, (
            "expected at least one stale-copy WARN after override was added; "
            f"got: {[(r.name, r.status, r.message) for r in post_probe]}"
        )

    def test_reinstall_with_override_heals_staleness(self) -> None:
        """After re-running ws init with the override active, the probe passes again."""
        fs = FakeFilesystem()
        config_files: dict[Path, dict] = {}
        ext = _seed_extension(fs, config_files)

        cfg_with_override = _config(overrides={"reviewer": "haiku"})
        reporter = FakeInitReporter()

        # Install WITH the override.
        _install_svc(cfg_with_override, fs, config_files).process(ext, reporter)

        # Probe WITH the same override → should pass (installer and probe agree).
        results = _probe_svc(cfg_with_override, fs, config_files).run([ext])
        failures = [r for r in results if r.status != ProbeStatus.pass_ and "overrides: targets" not in r.name]
        assert not failures, (
            f"expected all vendor probes to pass after install with override: "
            f"{[(r.name, r.status, r.message) for r in failures]}"
        )

    def test_probe_detects_stale_after_override_removed(self) -> None:
        """Removing an override that was active during install → probe flags stale."""
        fs = FakeFilesystem()
        config_files: dict[Path, dict] = {}
        ext = _seed_extension(fs, config_files)

        cfg_with_override = _config(overrides={"reviewer": "haiku"})
        reporter = FakeInitReporter()

        # Install WITH the override.
        _install_svc(cfg_with_override, fs, config_files).process(ext, reporter)

        # Probe WITHOUT any override — copies were written with haiku, probe
        # expects sonnet (from frontmatter tier table), so stale.
        cfg_no_override = _config(overrides=None)
        results = _probe_svc(cfg_no_override, fs, config_files).run([ext])

        stale = [r for r in results if r.status == ProbeStatus.warn and "stale copy" in r.message]
        assert stale, (
            "expected stale-copy WARN after override was removed but copies still use old model; "
            f"got: {[(r.name, r.status, r.message) for r in results]}"
        )


# ===========================================================================
# Parse-time validation
# ===========================================================================


class TestParseTimeValidation:
    """Tests for ConfigError raised during WorkspaceConfigService.load()."""

    def _service(self, fs: FakeFilesystem, configs: dict[Path, dict]) -> WorkspaceConfigService:
        return WorkspaceConfigService(
            workspace_locator=_StubLocator(WORKSPACE_ROOT),
            fs=fs,
            config_file_reader=FakeConfigFileReader(configs),
        )

    def _shared_config(self, overrides_raw: object) -> dict:
        return {
            "main_branch": "main",
            "service_prefix": "ws",
            "agent_model_overrides": overrides_raw,
        }

    def test_unknown_vendor_label_raises_config_error(self) -> None:
        """An unknown vendor label in a per-vendor dict value raises ConfigError."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = self._service(
            fs,
            {shared_path: self._shared_config({"reviewer": {"unknown-vendor": "haiku"}})},
        )

        with pytest.raises(ConfigError, match="unknown vendor label"):
            svc.load()

    def test_empty_string_value_raises_config_error(self) -> None:
        """An empty string value raises ConfigError."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = self._service(
            fs,
            {shared_path: self._shared_config({"reviewer": ""})},
        )

        with pytest.raises(ConfigError, match="non-empty"):
            svc.load()

    def test_invalid_value_type_raises_config_error(self) -> None:
        """A non-string, non-dict value (e.g. integer) raises ConfigError."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = self._service(
            fs,
            {shared_path: self._shared_config({"reviewer": 42})},
        )

        with pytest.raises(ConfigError, match=r"string.*per-vendor table|per-vendor table.*string"):
            svc.load()

    def test_valid_tier_names_parse_without_error(self) -> None:
        """All three tier names ('opus', 'sonnet', 'haiku') parse cleanly."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = self._service(
            fs,
            {
                shared_path: self._shared_config(
                    {
                        "agent-a": "opus",
                        "agent-b": "sonnet",
                        "agent-c": "haiku",
                    }
                )
            },
        )

        config = svc.load()
        assert config.agent_model_overrides.overrides == {
            "agent-a": "opus",
            "agent-b": "sonnet",
            "agent-c": "haiku",
        }

    def test_concrete_model_id_as_bare_string_raises_config_error(self) -> None:
        """A bare string value that is not a tier label raises ConfigError.

        Concrete model ids must use the per-vendor inline-table form to avoid
        typo'd tier names silently passing through as unvalidated model ids.
        """
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = self._service(
            fs,
            {shared_path: self._shared_config({"reviewer": "gpt-5.4-experimental"})},
        )

        with pytest.raises(ConfigError, match="not a recognised tier label"):
            svc.load()

    def test_typo_tier_in_overrides_raises_config_error(self) -> None:
        """A bare string value with a typo'd tier label raises ConfigError at load time."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = self._service(
            fs,
            {shared_path: self._shared_config({"reviewer": "snonet"})},
        )

        with pytest.raises(ConfigError, match="snonet"):
            svc.load()

    def test_valid_per_vendor_dict_parses_without_error(self) -> None:
        """A per-vendor dict with valid vendor labels parses cleanly."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = self._service(
            fs,
            {shared_path: self._shared_config({"reviewer": {"claude": "haiku", "codex": "gpt-5.4"}})},
        )

        config = svc.load()
        assert config.agent_model_overrides.overrides["reviewer"] == {
            "claude": "haiku",
            "codex": "gpt-5.4",
        }

    def test_empty_per_vendor_dict_raises_config_error(self) -> None:
        """An empty dict value raises ConfigError."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = self._service(
            fs,
            {shared_path: self._shared_config({"reviewer": {}})},
        )

        with pytest.raises(ConfigError, match="at least one vendor"):
            svc.load()

    def test_invalid_vendor_dict_value_type_raises_config_error(self) -> None:
        """A non-string value inside a per-vendor dict raises ConfigError."""
        shared_path = WORKSPACE_ROOT / WINTER_DIR / CONFIG_FILE
        fs = FakeFilesystem(files={shared_path: ""})
        svc = self._service(
            fs,
            {shared_path: self._shared_config({"reviewer": {"claude": 123}})},
        )

        with pytest.raises(ConfigError, match="non-empty string"):
            svc.load()


# ===========================================================================
# End-to-end: installer + probe agree on override bytes
# ===========================================================================


class TestInstallerProbeConsistency:
    def test_install_with_override_then_probe_passes(self) -> None:
        """After install with override, the probe (using same override) reports PASS."""
        fs = FakeFilesystem()
        config_files: dict[Path, dict] = {}
        ext = _seed_extension(fs, config_files)
        cfg = _config(overrides={"reviewer": "haiku"})
        reporter = FakeInitReporter()

        _install_svc(cfg, fs, config_files).process(ext, reporter)

        results = _probe_svc(cfg, fs, config_files).run([ext])
        vendor_results = [r for r in results if "agent copies:" in r.name]
        assert all(r.status == ProbeStatus.pass_ for r in vendor_results), (
            f"expected all vendor probes PASS: {[(r.name, r.message) for r in vendor_results if r.status != ProbeStatus.pass_]}"
        )

    def test_install_with_concrete_override_then_probe_passes(self) -> None:
        """After install with a concrete-id override, the probe reports PASS."""
        fs = FakeFilesystem()
        config_files: dict[Path, dict] = {}
        ext = _seed_extension(fs, config_files)
        cfg = _config(overrides={"reviewer": "gpt-5.4-experimental"})
        reporter = FakeInitReporter()

        _install_svc(cfg, fs, config_files).process(ext, reporter)

        results = _probe_svc(cfg, fs, config_files).run([ext])
        vendor_results = [r for r in results if "agent copies:" in r.name]
        assert all(r.status == ProbeStatus.pass_ for r in vendor_results), (
            f"expected all vendor probes PASS: {[(r.name, r.message) for r in vendor_results if r.status != ProbeStatus.pass_]}"
        )
