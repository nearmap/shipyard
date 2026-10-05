"""A credential-shaped variable is discovered by name and value, and its value is scrubbed from text."""
from __future__ import annotations

from sy_tools import secrets


def test_name_heuristic_is_word_based():
    assert secrets.looks_like_secret_name("A_TOKEN")
    assert secrets.looks_like_secret_name("AWS_SECRET_ACCESS_KEY")
    assert not secrets.looks_like_secret_name("TOKENIZER_PATH"), "substring matching would over-redact"
    assert not secrets.looks_like_secret_name("SOME_SITE")
    assert not secrets.looks_like_secret_name("NM_BEARER")
    assert secrets.looks_like_secret_name("NM_BEARER", extra=frozenset({"BEARER"})), "extra must widen the match"


def test_discovery_needs_both_a_secret_shaped_name_and_a_long_enough_value(monkeypatch):
    monkeypatch.setenv("SY_TEST_DISCOVER_TOKEN", "abcdef0123456789secretvalue")
    monkeypatch.setenv("SY_TEST_DISCOVER_SHORT_KEY", "ab")
    monkeypatch.setenv("SY_TEST_DISCOVER_NOTASECRET", "this-name-is-not-secret-shaped-but-is-long-enough")
    monkeypatch.setenv("SY_TEST_DISCOVER_BEARER", "abcdef0123456789bearervalue")

    found = secrets.discover_secret_vars()
    assert found["SY_TEST_DISCOVER_TOKEN"] == "abcdef0123456789secretvalue"
    assert "SY_TEST_DISCOVER_SHORT_KEY" not in found, "a value below the min-length floor must be skipped"
    assert "SY_TEST_DISCOVER_NOTASECRET" not in found, "a non-secret-shaped name must be skipped whatever its length"
    assert "SY_TEST_DISCOVER_BEARER" not in found, "a fragment outside the built-in set is not a false positive"

    widened = secrets.discover_secret_vars(extra_words=frozenset({"BEARER"}))
    assert "SY_TEST_DISCOVER_BEARER" in widened, "extra_words must widen discovery, not just the name heuristic"


def test_longest_value_first_prevents_a_fragmented_redaction():
    found = {"SHORT_TOKEN": "abc123secret", "LONG_TOKEN": "prefix-abc123secret-suffix"}
    scrubbed, counts = secrets.scrub_text("value: prefix-abc123secret-suffix\n", found)
    assert "abc123secret" not in scrubbed
    assert scrubbed.strip() == "value: <REDACTED:LONG_TOKEN>"
    assert counts == {"LONG_TOKEN": 1}
