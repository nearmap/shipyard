"""`context_watch`: each band announced once per main-thread session, and the user warning on Stop."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sy_tools import context_watch

DOC = Path(__file__).resolve().parents[2] / "skills/shared/references/context-recycle.md"


@pytest.fixture(autouse=True)
def _markers(tmp_path, monkeypatch):
    monkeypatch.setattr(context_watch, "MARKER_ROOT", tmp_path / "markers")


def _request(i: int, ctx: int, *, sidechain: bool = False, one_hour: bool = False,
             model: str = "claude-opus-5-5") -> list[str]:
    cache = {"ephemeral_1h_input_tokens": 1000} if one_hour else {"ephemeral_5m_input_tokens": 1000}
    usage = {"input_tokens": 2, "cache_read_input_tokens": ctx - 1002, "cache_creation_input_tokens": 1000,
             "cache_creation": cache}
    blocks = [{"type": "text", "text": "x"}, {"type": "tool_use", "id": f"t{i}"}]
    # One request is written as several records, one content block each, every one replaying the usage.
    return [json.dumps({"type": "assistant", "requestId": f"r{i}", "isSidechain": sidechain,
                        "message": {"model": model, "usage": usage, "content": [b]}}) for b in blocks]


def _transcript(tmp_path, *requests: list[str], name: str = "t.jsonl") -> str:
    path = tmp_path / name
    path.write_text("\n".join(line for r in requests for line in r) + "\n", encoding="utf-8")
    return str(path)


def _post(path: str, session: str = "s1", **extra) -> dict:
    return {"hook_event_name": "PostToolUse", "transcript_path": path, "session_id": session, **extra}


def _stop(path: str, **extra) -> dict:
    return {"hook_event_name": "Stop", "transcript_path": path, "session_id": "s1", **extra}


def test_each_band_is_announced_once_per_session(tmp_path):
    below = _transcript(tmp_path, _request(0, 200_000))
    assert context_watch.hook_output(_post(below)) is None
    crossed = context_watch.hook_output(_post(_transcript(tmp_path, _request(0, 200_000), _request(1, 260_000))))
    assert crossed, "crossing 250k must tell the model"
    text = crossed["hookSpecificOutput"]["additionalContext"]
    assert "260k" in text and "context-recycle.md" in text, text
    same = _transcript(tmp_path, _request(0, 260_000), _request(1, 300_000))
    assert context_watch.hook_output(_post(same)) is None, "a later request in the same band repeats nothing"
    assert context_watch.hook_output(_post(same)) is None, "a parallel tool call of the same request repeats nothing"
    assert context_watch.hook_output(_post(_transcript(tmp_path, _request(0, 401_000)))), "400k is the next band"
    assert context_watch.hook_output(_post(same, session="s2")), "another session has its own bands"


def test_a_crossing_the_transcript_has_not_written_yet_is_announced_at_the_next_tool_call(tmp_path):
    lagging = _transcript(tmp_path, _request(0, 240_000))
    assert context_watch.hook_output(_post(lagging)) is None, "only the older request is on disk yet"
    # The crossing request r1 lands only after r2 has gone out, so r2's tool call, with r2 not yet on disk,
    # is the first to see it.
    caught_up = _transcript(tmp_path, _request(0, 240_000), _request(1, 262_000))
    assert context_watch.hook_output(_post(caught_up, tool_use_id="t2")), "the crossing must not be lost to lag"


def test_a_compaction_below_the_threshold_re_arms_the_bands(tmp_path):
    assert context_watch.hook_output(_post(_transcript(tmp_path, _request(0, 260_000))))
    assert context_watch.hook_output(_post(_transcript(tmp_path, _request(1, 60_000)))) is None
    assert context_watch.hook_output(_post(_transcript(tmp_path, _request(2, 255_000)))), "re-crossing after compaction"


def test_subagent_sidechain_and_synthetic_records_never_count_as_the_main_thread(tmp_path):
    path = _transcript(tmp_path, _request(0, 260_000))
    assert context_watch.hook_output(_post(path, agent_id="a1")) is None
    sidechain = _transcript(tmp_path, _request(0, 240_000), _request(1, 900_000, sidechain=True))
    assert context_watch.hook_output(_post(sidechain)) is None, "the sidechain's 900k is not the main thread's"
    zero = {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    synthetic = _transcript(tmp_path, _request(0, 240_000), [json.dumps(
        {"type": "assistant", "requestId": "r1", "message": {"model": "<synthetic>", "usage": zero}})])
    assert context_watch.hook_output(_stop(synthetic)) is None, "a zero-usage synthetic record is not drift"


def test_stop_warns_at_or_above_the_threshold_with_the_newest_write_s_ttl(tmp_path):
    five = context_watch.hook_output(_stop(_transcript(tmp_path, _request(0, 300_000))))
    assert five and "300k" in five["systemMessage"] and "5 minutes" in five["systemMessage"], five
    hour = context_watch.hook_output(_stop(_transcript(tmp_path, _request(0, 300_000, one_hour=True))))
    assert hour and "an hour" in hour["systemMessage"], hour
    dropped = context_watch.hook_output(_stop(_transcript(tmp_path, _request(0, 280_000, one_hour=True),
                                                          _request(1, 300_000))))
    assert dropped and "5 minutes" in dropped["systemMessage"], "a session dropped from 1h to 5m must not read 1h"
    assert context_watch.hook_output(_stop(_transcript(tmp_path, _request(0, 120_000)))) is None


def test_stop_stays_quiet_while_a_background_task_runs(tmp_path):
    path = _transcript(tmp_path, _request(0, 300_000))
    assert context_watch.hook_output(_stop(path, background_tasks=[{"id": "b1"}])) is None


def test_transcript_drift_and_read_errors_show_on_stop_and_stay_quiet_on_post_tool_use(tmp_path):
    no_ids = tmp_path / "no-ids.jsonl"
    no_ids.write_text(json.dumps({"type": "assistant", "message": {"model": "m", "content": []}}) + "\n",
                      encoding="utf-8")
    stop = context_watch.hook_output(_stop(str(no_ids)))
    assert stop and "not working" in stop["systemMessage"], stop
    assert context_watch.hook_output(_post(str(no_ids))) is None
    keyless = _transcript(tmp_path, [json.dumps({"type": "assistant", "requestId": "r0",
                                                  "message": {"model": "m", "usage": {"tokens": 5}}})],
                          name="keyless.jsonl")
    out = context_watch.hook_output(_stop(keyless))
    assert out and "usage keys" in out["systemMessage"], out
    unreadable = context_watch.hook_output(_stop(str(tmp_path)))
    assert unreadable and "cannot read the transcript" in unreadable["systemMessage"], unreadable
    assert context_watch.hook_output(_stop(str(tmp_path / "absent.jsonl"))) is None


def test_a_tail_cut_inside_a_multibyte_character_is_skipped_not_raised(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    # Claude Code writes `type` after `message`, so the cut record still names itself an assistant record.
    record = {"message": {"model": "m", "content": [{"type": "text", "text": "é" * 400}]},
              "requestId": "cut", "type": "assistant"}
    head = json.dumps(record, ensure_ascii=False).encode("utf-8")
    tail = "\n".join(_request(0, 260_000)).encode("utf-8")
    path.write_bytes(head + b"\n" + tail + b"\n")
    cut = head.index("é".encode()) + 1
    monkeypatch.setattr(context_watch, "TAIL_BYTES", len(head) - cut + len(tail) + 2)
    assert 0x80 <= path.read_bytes()[cut] < 0xC0, "the tail must start on a UTF-8 continuation byte"
    assert context_watch.hook_output(_post(str(path))), "the cut first line must be skipped, not fatal"


def test_the_recycle_doc_states_the_hook_s_thresholds():
    text = DOC.read_text(encoding="utf-8")
    for tokens in (context_watch.RECYCLE_TOKENS, context_watch.BAND_TOKENS, context_watch.HARD_RECYCLE_TOKENS):
        assert f"{tokens // 1000}k" in text, f"context-recycle.md must state {tokens // 1000}k"


def test_short_usage_spellings_count_toward_context(tmp_path):
    usage = {"input_tokens": 2, "cache_read_tokens": 299_000, "cache_creation_tokens": 1000}
    path = _transcript(tmp_path, [json.dumps({"type": "assistant", "requestId": "r0",
                                               "message": {"model": "m", "usage": usage}})])
    out = context_watch.hook_output(_stop(path))
    assert out and "300k" in out["systemMessage"], out


def test_stop_reports_an_unwritable_marker_root(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(context_watch, "MARKER_ROOT", blocker / "markers")
    out = context_watch.hook_output(_stop(_transcript(tmp_path, _request(0, 120_000))))
    assert out and "cannot write" in out["systemMessage"], out
