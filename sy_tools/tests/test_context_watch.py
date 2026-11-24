"""`context_watch`: band crossings on the main thread only, and the user warning above the threshold."""
from __future__ import annotations

import json

from sy_tools import context_watch


def _transcript(tmp_path, *contexts: int, sidechain_last: bool = False):
    path = tmp_path / "t.jsonl"
    lines = []
    for i, ctx in enumerate(contexts):
        usage = {"input_tokens": 2, "cache_read_input_tokens": ctx - 1002, "cache_creation_input_tokens": 1000}
        record = {"type": "assistant", "requestId": f"r{i}", "message": {"usage": usage},
                  "isSidechain": sidechain_last and i == len(contexts) - 1}
        lines += [json.dumps(record), json.dumps(record)]  # one request is written as several records
        lines.append(json.dumps({"type": "user", "message": {"content": "x" * 50}}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _post(path: str, **extra) -> dict:
    return {"hook_event_name": "PostToolUse", "transcript_path": path, **extra}


def test_a_crossing_adds_one_context_line_and_staying_inside_a_band_adds_none(tmp_path):
    crossed = context_watch.hook_output(_post(_transcript(tmp_path, 240_000, 260_000)))
    assert crossed, "crossing 250k must tell the model"
    text = crossed["hookSpecificOutput"]["additionalContext"]
    assert "260k" in text and "context-recycle.md" in text, text
    assert context_watch.hook_output(_post(_transcript(tmp_path, 260_000, 300_000))) is None, "same band repeats"
    assert context_watch.hook_output(_post(_transcript(tmp_path, 390_000, 401_000))), "400k is the next band"
    assert context_watch.hook_output(_post(_transcript(tmp_path, 100_000, 200_000))) is None, "below threshold"


def test_a_subagent_call_and_a_sidechain_record_never_count_as_the_main_thread(tmp_path):
    path = _transcript(tmp_path, 240_000, 260_000)
    assert context_watch.hook_output(_post(path, agent_id="a1")) is None
    sidechain = _transcript(tmp_path, 100_000, 240_000, 900_000, sidechain_last=True)
    assert context_watch.hook_output(_post(sidechain)) is None, "the sidechain's 900k is not the main thread's"


def test_stop_warns_the_user_only_at_or_above_the_threshold(tmp_path):
    big = context_watch.hook_output({"hook_event_name": "Stop", "transcript_path": _transcript(tmp_path, 300_000)})
    assert big and "300k" in big["systemMessage"], big
    small = {"hook_event_name": "Stop", "transcript_path": _transcript(tmp_path, 120_000)}
    assert context_watch.hook_output(small) is None


def test_a_missing_transcript_says_nothing(tmp_path):
    assert context_watch.hook_output(_post(str(tmp_path / "absent.jsonl"))) is None
