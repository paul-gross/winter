from __future__ import annotations

import json

import pytest

from winter_cli.modules.service.describe_models import DescribeResult
from winter_cli.modules.service.describe_parser import DescribeParseError, DescribeResultParser

_parser = DescribeResultParser()
_PROVIDER = "my-provider"


# ── helpers ───────────────────────────────────────────────────────────────────


def _raw(**kwargs: object) -> str:
    """Serialise a dict to a JSON string for use as raw_stdout."""
    return json.dumps(kwargs)


# ── well-formed responses ─────────────────────────────────────────────────────


def test_parse_services_list() -> None:
    """A well-formed {"services": [...]} response parses to the named services."""
    raw = _raw(services=["api", "worker", "frontend"])
    result = _parser.parse(raw, provider_name=_PROVIDER)
    assert result == DescribeResult(services=("api", "worker", "frontend"))


def test_parse_empty_services_list() -> None:
    """{"services": []} is valid and yields an empty tuple."""
    raw = _raw(services=[])
    result = _parser.parse(raw, provider_name=_PROVIDER)
    assert result == DescribeResult(services=())


def test_parse_single_service() -> None:
    """A single-element services list parses correctly."""
    raw = _raw(services=["backend"])
    result = _parser.parse(raw, provider_name=_PROVIDER)
    assert result == DescribeResult(services=("backend",))


def test_parse_non_string_elements_are_dropped() -> None:
    """Non-string elements in the services list are silently dropped (shape-stability)."""
    raw = json.dumps({"services": ["api", 42, None, "worker", True]})
    result = _parser.parse(raw, provider_name=_PROVIDER)
    assert result == DescribeResult(services=("api", "worker"))


def test_parse_extra_fields_are_ignored() -> None:
    """Extra keys in the describe document are ignored."""
    raw = json.dumps({"services": ["api"], "extra": "ignored", "version": 1})
    result = _parser.parse(raw, provider_name=_PROVIDER)
    assert result == DescribeResult(services=("api",))


# ── missing / empty services key → () ────────────────────────────────────────


def test_missing_services_key_yields_empty_tuple() -> None:
    """A missing 'services' key yields () (shape-stability)."""
    raw = _raw()
    result = _parser.parse(raw, provider_name=_PROVIDER)
    assert result == DescribeResult(services=())


def test_null_services_key_yields_empty_tuple() -> None:
    """A null 'services' value yields () (shape-stability; non-list → empty)."""
    raw = json.dumps({"services": None})
    result = _parser.parse(raw, provider_name=_PROVIDER)
    assert result == DescribeResult(services=())


def test_non_list_services_key_yields_empty_tuple() -> None:
    """A non-list 'services' value (e.g. a string) yields () (shape-stability)."""
    raw = json.dumps({"services": "not-a-list"})
    result = _parser.parse(raw, provider_name=_PROVIDER)
    assert result == DescribeResult(services=())


# ── malformed JSON → error naming the provider ────────────────────────────────


def test_malformed_json_raises_with_provider_name() -> None:
    """Malformed JSON raises DescribeParseError naming the provider."""
    with pytest.raises(DescribeParseError, match="my-provider"):
        _parser.parse("not valid json {{{", provider_name=_PROVIDER)


def test_malformed_json_raises_with_detail() -> None:
    """Malformed JSON error message includes parse detail."""
    with pytest.raises(DescribeParseError, match="valid JSON"):
        _parser.parse("{bad}", provider_name=_PROVIDER)


def test_non_object_top_level_raises() -> None:
    """A JSON array at the top level raises DescribeParseError."""
    with pytest.raises(DescribeParseError, match="my-provider"):
        _parser.parse(json.dumps(["api", "worker"]), provider_name=_PROVIDER)


def test_non_object_top_level_names_type() -> None:
    """The error for a non-object top level names the received type."""
    with pytest.raises(DescribeParseError, match="list"):
        _parser.parse(json.dumps(["api"]), provider_name=_PROVIDER)


def test_json_null_top_level_raises() -> None:
    """A JSON null at the top level raises DescribeParseError."""
    with pytest.raises(DescribeParseError, match="my-provider"):
        _parser.parse("null", provider_name=_PROVIDER)


# ── provider_name is forwarded ────────────────────────────────────────────────


def test_provider_name_appears_in_json_error() -> None:
    """The provider_name is included in JSON parse error messages."""
    with pytest.raises(DescribeParseError, match="special-provider"):
        _parser.parse("!!!not json!!!", provider_name="special-provider")
