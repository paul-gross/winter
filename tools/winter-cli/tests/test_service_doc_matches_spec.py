from __future__ import annotations

# Anti-drift guard: service.md contract section must agree with service-v1.toml.
#
# This test parses the "Orchestrator contract" section of
# ai/winter-cli/usage/service.md and asserts that every fact the spec declares
# (actions, exit codes, always-present env vars) is present and consistent in
# the doc.  The spec is authoritative; the doc must agree with it.
#
# If someone edits service-v1.toml or service.md out of agreement this test
# fails, making drift visible at CI time rather than at the reader's desk.
import re
from pathlib import Path

import pytest

from winter_cli.core.internal.tomllib_config_file_reader import TomllibConfigFileReader
from winter_cli.modules.capability.spec_loader import SpecLoader

# Resolve the doc relative to this file: tests/ → tools/winter-cli/ → winter/ → ai/
_REPO_ROOT = Path(__file__).parent.parent.parent.parent  # gamma/winter/
_SERVICE_DOC = _REPO_ROOT / "ai" / "winter-cli" / "usage" / "service.md"

_SPEC_SLOT = "service"
_SPEC_VERSION = "v1"


def _real_spec():
    loader = SpecLoader(config_file_reader=TomllibConfigFileReader())
    return loader.load(_SPEC_SLOT, _SPEC_VERSION)


def _doc_text() -> str:
    if not _SERVICE_DOC.exists():
        pytest.fail(f"service.md not found at {_SERVICE_DOC}")
    return _SERVICE_DOC.read_text()


def _contract_section(doc: str) -> str:
    """Extract the text from '## Orchestrator contract' to the next '## ' heading."""
    match = re.search(r"^## Orchestrator contract\b", doc, re.MULTILINE)
    if not match:
        pytest.fail("service.md has no '## Orchestrator contract' section")
    start = match.start()
    # Find the next ## heading after the contract section.
    next_heading = re.search(r"^## ", doc[match.end():], re.MULTILINE)
    end = (match.end() + next_heading.start()) if next_heading else len(doc)
    return doc[start:end]


# ── action names ─────────────────────────────────────────────────────────────


def test_service_doc_mentions_all_spec_actions() -> None:
    """Every action word in the spec appears in the Orchestrator contract section."""
    spec = _real_spec()
    doc = _contract_section(_doc_text())
    for action in spec.actions:
        assert action.name in doc, (
            f"service.md Orchestrator contract section does not mention action "
            f"{action.name!r}, which is declared in service-v1.toml."
        )


# ── exit codes ───────────────────────────────────────────────────────────────


def test_service_doc_mentions_all_spec_exit_codes() -> None:
    """Every exit code in the spec appears in the contract section of service.md."""
    spec = _real_spec()
    doc = _contract_section(_doc_text())
    for ec in spec.exit_codes:
        # Match the code as a standalone number (not inside a longer number).
        pattern = rf"(?<!\d){re.escape(str(ec.code))}(?!\d)"
        assert re.search(pattern, doc), (
            f"service.md Orchestrator contract section does not mention exit code "
            f"{ec.code} ({ec.meaning!r}), which is declared in service-v1.toml."
        )


# ── always-present environment variables ─────────────────────────────────────


def test_service_doc_mentions_all_always_present_env_vars() -> None:
    """Every always-present env var in the spec appears in the contract section."""
    spec = _real_spec()
    doc = _contract_section(_doc_text())
    for ev in spec.env_vars:
        assert ev.name in doc, (
            f"service.md Orchestrator contract section does not mention always-present "
            f"env var {ev.name!r}, which is declared in service-v1.toml."
        )


# ── per-action env vars for logs ─────────────────────────────────────────────


def test_service_doc_mentions_logs_action_env_vars() -> None:
    """Every per-action env var for the 'logs' action appears in the doc."""
    spec = _real_spec()
    doc = _contract_section(_doc_text())
    logs_action = next((a for a in spec.actions if a.name == "logs"), None)
    assert logs_action is not None, "service-v1.toml must declare a 'logs' action"
    for ev in logs_action.env_vars:
        assert ev.name in doc, (
            f"service.md Orchestrator contract section does not mention logs env var "
            f"{ev.name!r}, which is declared in service-v1.toml [[action.env_var]] for logs."
        )


# ── spec-source-of-truth pointer ─────────────────────────────────────────────


def test_service_doc_mentions_spec_source_of_truth() -> None:
    """service.md must include a pointer to the machine-readable spec file."""
    doc = _doc_text()
    # The pointer must reference the spec file by name.
    assert "service-v1.toml" in doc, (
        "service.md does not reference service-v1.toml as the machine-readable "
        "source of truth. Add a pointer per the Phase 5 requirement."
    )


def test_service_doc_mentions_ext_verify() -> None:
    """service.md must mention 'winter ext verify' as the self-check command."""
    doc = _doc_text()
    assert "ext verify" in doc, (
        "service.md does not mention 'winter ext verify' as the self-check mechanism. "
        "Add a note per the Phase 5 requirement."
    )
