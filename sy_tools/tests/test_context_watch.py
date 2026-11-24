"""`context_watch`: band crossings on the main thread only, once per request, and the user warning."""
from __future__ import annotations

import json
from pathlib import Path

from sy_tools import context_watch

DOC = Path(__file__).resolve().parents[2] / "skills/shared/references/context-recycle.md"


def _request(i: int, ctx: int, *, tools: int = 1, sidechain: bool = False, one_hour: bool = False,
             model: str = "claude-opus-5-5") -> list[str]:
    cache = {"ephemeral_1h_input_tokens": 1000} if one_hour else {"ephemeral_5m_input_tokens": 1000}
    usage = {"input_tokens": 2, "cache_read_input_tokens": ctx - 1002, "cache_creation_input_tokens": 1000,
             "cache_creation": cache}
    blocks = [{"type": "text", "text": "x"}] + [{"type": "tool_use", "id": f"t{i}-{k}"} for k in range(tools)]
    # One request is written as several records, one content block each, every one replaying the usage.
    return [json.dumps({"type": "assistant", "requestId": f"r{i}", "isSidechain": sidechain,
                        "message": {"model": model, "usage": usage, "content": [b]}}) for b in blocks]


def _transcript(tmp_path, *requests: list[str]) -> str:
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(line for r in requests for line in r) + "\n", encoding="utf-8")
    return str(path)


def _post(path: str, tool_use_id: str | None = None, **extra) -> dict:
    return {"hook_event_name": "PostToolUse", "transcript_path": path, "tool_use_id": tool_use_id, **extra}


def test_a_crossing_adds_one_context_line_and_staying_inside_a_band_adds_none(tmp_path):
    crossed = context_watch.hook_output(_post(_transcript(tmp_path, _request(0, 240_000), _request(1, 260_000))))
    assert crossed, "crossing 250k must tell the model"
    text = crossed["hookSpecificOutput"]["additionalContext"]
    assert "260k" in text and "context-recycle.md" in text, text
    same = _transcript(tmp_path, _request(0, 260_000), _request(1, 300_000))
    assert context_watch.hook_output(_post(same)) is None, "a second request in the same band repeats nothing"
    assert context_watch.hook_output(_post(_transcript(tmp_path, _request(0, 390_000), _request(1, 401_000))))
    assert context_watch.hook_output(_post(_transcript(tmp_path, _request(0, 100_000), _request(1, 200_000)))) is None


def test_a_crossing_on_a_text_only_request_reaches_the_next_tool_call(tmp_path):
    path = _transcript(tmp_path, _request(0, 240_000), _request(1, 255_000, tools=0), _request(2, 262_000))
    assert context_watch.hook_output(_post(path)), "the text-only request had no tool call to carry the line"


def test_parallel_tool_calls_add_the_line_once(tmp_path):
    path = _transcript(tmp_path, _request(0, 240_000), _request(1, 260_000, tools=3))
    assert context_watch.hook_output(_post(path, "t1-0")), "the request's first tool call carries the line"
    assert context_watch.hook_output(_post(path, "t1-1")) is None
    assert context_watch.hook_output(_post(path, "t1-2")) is None


def test_subagent_sidechain_and_synthetic_records_never_count_as_the_main_thread(tmp_path):
    path = _transcript(tmp_path, _request(0, 240_000), _request(1, 260_000))
    assert context_watch.hook_output(_post(path, agent_id="a1")) is None
    sidechain = _transcript(tmp_path, _request(0, 240_000), _request(1, 900_000, sidechain=True))
    assert context_watch.hook_output(_post(sidechain)) is None, "the sidechain's 900k is not the main thread's"
    synthetic = _transcript(tmp_path, _request(0, 260_000), _request(1, 1002, model="<synthetic>"),
                            _request(2, 270_000))
    assert context_watch.hook_output(_post(synthetic)) is None, "a zero-usage synthetic record re-armed the band"


def test_stop_warns_at_or_above_the_threshold_and_names_the_session_s_ttl(tmp_path):
    five = context_watch.hook_output({"hook_event_name": "Stop",
                                      "transcript_path": _transcript(tmp_path, _request(0, 300_000))})
    assert five and "300k" in five["systemMessage"] and "5 minutes" in five["systemMessage"], five
    hour = context_watch.hook_output({"hook_event_name": "Stop",
                                      "transcript_path": _transcript(tmp_path, _request(0, 300_000, one_hour=True))})
    assert hour and "an hour" in hour["systemMessage"], hour
    small = {"hook_event_name": "Stop", "transcript_path": _transcript(tmp_path, _request(0, 120_000))}
    assert context_watch.hook_output(small) is None


def test_a_tail_cut_inside_a_multibyte_character_is_skipped_not_raised(tmp_path, monkeypatch):
    path = tmp_path / "t.jsonl"
    record = {"type": "user", "message": {"content": "é" * 400}, "requestId": "u"}
    head = json.dumps(record, ensure_ascii=False).encode("utf-8")
    tail = "\n".join(_request(0, 240_000) + _request(1, 260_000)).encode("utf-8")
    path.write_bytes(head + b"\n" + tail + b"\n")
    tail_bytes = len(tail) + 2 + 302
    data = path.read_bytes()
    assert 0x80 <= data[-tail_bytes] < 0xC0, "the tail must start on a UTF-8 continuation byte"
    monkeypatch.setattr(context_watch, "TAIL_BYTES", tail_bytes)
    assert context_watch.hook_output(_post(str(path))), "the cut first line must be skipped, not fatal"


def test_a_missing_transcript_says_nothing(tmp_path):
    assert context_watch.hook_output(_post(str(tmp_path / "absent.jsonl"))) is None


def test_the_recycle_doc_states_the_hook_s_thresholds():
    text = DOC.read_text(encoding="utf-8")
    for tokens in (context_watch.RECYCLE_TOKENS, context_watch.BAND_TOKENS, context_watch.HARD_RECYCLE_TOKENS):
        assert f"{tokens // 1000}k" in text, f"context-recycle.md must state {tokens // 1000}k"


def test_the_ttl_comes_from_the_newest_cache_write_not_any_in_the_tail(tmp_path):
    dropped = _transcript(tmp_path, _request(0, 280_000, one_hour=True), _request(1, 300_000))
    out = context_watch.hook_output({"hook_event_name": "Stop", "transcript_path": dropped})
    assert out and "5 minutes" in out["systemMessage"], "a session dropped from 1h to 5m must not read 'an hour'"


def test_transcript_format_drift_is_shown_on_stop_and_quiet_on_post_tool_use(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps({"type": "assistant", "message": {"model": "m", "content": []}}) + "\n",
                    encoding="utf-8")
    stop = context_watch.hook_output({"hook_event_name": "Stop", "transcript_path": str(path)})
    assert stop and "not working" in stop["systemMessage"], stop
    assert context_watch.hook_output(_post(str(path))) is None
    keyless = _transcript(tmp_path, [json.dumps({"type": "assistant", "requestId": "r0",
                                                  "message": {"model": "m", "usage": {"tokens": 5}}})])
    out = context_watch.hook_output({"hook_event_name": "Stop", "transcript_path": keyless})
    assert out and "usage keys" in out["systemMessage"], out
