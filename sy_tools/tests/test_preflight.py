"""The preflight cache's mechanics, and the `preflight` tool's use of them.

The cache path and every variable read are redirected into `tmp_path`, so nothing here reads or writes
the operator's real liveness verdict. The tracker names are placeholders: this module sees a tracker
only as an opaque cache-key string.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sy_tools import preflight, server

VARS = ["SY_TEST_VAR_A", "SY_TEST_VAR_B"]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def cache(tmp_path, monkeypatch) -> Path:
    """A throwaway cache path, and the two secret variables the fingerprint is keyed on."""
    path = tmp_path / "sy" / "preflight-cache.json"
    monkeypatch.setattr(preflight, "cache_path", lambda: path)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setenv("SY_TEST_VAR_A", "a@b.c")
    monkeypatch.setenv("SY_TEST_VAR_B", "tok-1")
    return path


def test_an_empty_cache_misses_and_a_fresh_record_hits(cache):
    assert not preflight.check("trackerA", VARS, 3600), "empty cache must miss"
    preflight.record("trackerA", VARS)
    assert preflight.check("trackerA", VARS, 3600), "fresh record must hit"
    assert not preflight.check("trackerB", VARS, 3600), "wrong tracker must miss"


def test_a_changed_secret_value_misses_and_the_restored_one_hits_again(cache, monkeypatch):
    preflight.record("trackerA", VARS)
    monkeypatch.setenv("SY_TEST_VAR_B", "tok-2")
    assert not preflight.check("trackerA", VARS, 3600), "changed value must miss"
    monkeypatch.setenv("SY_TEST_VAR_B", "tok-1")
    assert preflight.check("trackerA", VARS, 3600), "restored value must hit again"


def test_a_zero_ttl_always_misses(cache):
    preflight.record("trackerA", VARS)
    assert not preflight.check("trackerA", VARS, 0), "zero TTL must always miss"


def test_var_order_does_not_affect_the_fingerprint(cache):
    assert preflight.fingerprint("trackerA", VARS) == preflight.fingerprint("trackerA", list(reversed(VARS))), (
        "var order must not affect the fingerprint"
    )


def test_a_changed_resolved_config_misses(cache, monkeypatch):
    """The one dependency this module has: the config fingerprint folded into its own.

    Asserted because that fold is what lets a caller list nothing but secrets — a switched project or
    a renamed column has to invalidate the cache without anybody naming it.
    """
    preflight.record("trackerA", VARS)
    monkeypatch.setattr(preflight, "config_fingerprint", lambda: "0000000000000000")
    assert not preflight.check("trackerA", VARS, 3600), "a changed resolved config must miss"


class _Preflighting:
    """An adapter that counts live preflight reads instead of performing one."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.reads = 0
        self._failure = failure

    async def preflight(self) -> dict[str, Any]:
        self.reads += 1
        if self._failure is not None:
            raise self._failure
        return {"ok": True, "account": "someone"}


@pytest.fixture
def tool_config(cache, monkeypatch) -> None:
    """Resolve the tool's tracker and secret var names from a fixture rather than the real config."""
    monkeypatch.setattr(server.config, "get", lambda key, **_kw: "trackerA" if key == "tracker" else None)
    monkeypatch.setattr(server.config, "adapter_map", lambda: {"secret_env": VARS})


@pytest.mark.anyio
async def test_the_tool_reads_live_on_a_miss_and_short_circuits_on_the_next_call(tool_config, monkeypatch):
    """The whole point of the cache: the second call must not touch the network."""
    adapter = _Preflighting()
    monkeypatch.setattr(server.tracker, "adapter", lambda: adapter)

    first = await server.preflight()
    assert first["cached"] is False and first["ok"] is True, first
    assert adapter.reads == 1, "a cache miss must run the live read once"

    second = await server.preflight()
    assert second["cached"] is True, second
    # Counted on the adapter, not the `cached` flag: a flag can say "cached" beside a live read.
    assert adapter.reads == 1, "a cache hit must not reach the adapter at all"

    forced = await server.preflight(force=True)
    assert forced["cached"] is False, forced
    assert adapter.reads == 2, "force must read live even with a fresh cache entry"


@pytest.mark.anyio
async def test_the_tool_never_records_a_failed_live_check(tool_config, monkeypatch):
    """A cached failure is a working credential's worst outcome: nothing would re-read for a day."""
    adapter = _Preflighting(failure=server.tracker.TrackerError("the credential is revoked"))
    monkeypatch.setattr(server.tracker, "adapter", lambda: adapter)

    with pytest.raises(server.tracker.TrackerError):
        await server.preflight()
    assert not preflight.cache_path().is_file(), "a failed live check must leave nothing recorded"
    assert not preflight.check("trackerA", VARS, 3600), "a failed live check must not read back as verified"


@pytest.mark.anyio
async def test_an_unresolvable_config_is_a_tool_error_and_no_live_read(tool_config, monkeypatch):
    """The tool resolves the cache key itself, so a broken config is its refusal to report."""
    adapter = _Preflighting()
    monkeypatch.setattr(server.tracker, "adapter", lambda: adapter)

    def refuses(key: str, **_kw: Any) -> None:
        raise server.config.ConfigError(f"config key {key!r} cannot be resolved")

    monkeypatch.setattr(server.config, "get", refuses)
    with pytest.raises(server.ToolError, match="cannot be resolved"):
        await server.preflight()
    assert adapter.reads == 0, "an unresolvable config must be refused before the tracker is touched"
