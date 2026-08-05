"""The tool surface, driven through the SDK's own client rather than a hand-rolled stdio harness.

`mcp.Client(server.mcp)` is the SDK's in-memory client: a real client session over real `initialize`
/ `tools/list` / `tools/call` exchanges, with the streams short-circuited instead of crossing a pipe.
That is the right level here — framing is the SDK's to get right, and `test_handshake.py` covers the
wire.

The gate tests call the tool functions directly. `@mcp.tool` registers and returns the function
unchanged, so a direct call keeps a gate assertion about *not doing work* readable.
"""
from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

import anyio
import mcp
import pytest

from sy_tools import SERVER_NAME, server
from sy_tools.ship_metrics import SCHEMA_ID

TOOL_NAMES = {
    "add-dependency",
    "add-label",
    "agent_model",
    "assign",
    "attach-artifact",
    "attachment-download",
    "attachment-update",
    "check_env",
    "create-issue",
    "export_transcript",
    "find-issues",
    "fingerprint_config",
    "get-issue",
    "get_config",
    "link-parent",
    "memory_add",
    "memory_list",
    "memory_refute",
    "memory_search",
    "post-comment",
    "preflight",
    "reload_config",
    "scratch_dir",
    "set-status",
    "show_config",
    "type-convert",
    "update-issue",
    "usage_summarize",
    "validate_config",
}

WIRING = [
    ("create-issue", {"issue_type": "task", "title": "T", "body": "B", "parent": "PROJ-1"},
     "create_issue", (), {"issue_type": "task", "title": "T", "body": "B", "parent": "PROJ-1"}),
    ("get-issue", {"issue": "PROJ-2"}, "get_issue", ("PROJ-2",), {}),
    ("update-issue", {"issue": "PROJ-3", "body": "replacement"}, "update_issue", ("PROJ-3", "replacement"), {}),
    ("find-issues",
     {"status": "ready", "issue_type": "bug", "parent": "PROJ-4", "text": "seam", "limit": 5, "page_token": "abc"},
     "find_issues", (),
     {"status": "ready", "issue_type": "bug", "parent": "PROJ-4", "text": "seam", "limit": 5, "page_token": "abc"}),
    ("find-issues", {}, "find_issues", (),
     {"status": None, "issue_type": None, "parent": None, "text": None, "limit": 50, "page_token": None}),
    ("set-status", {"issue": "PROJ-5", "status": "in-review"}, "set_status", ("PROJ-5", "in-review"), {}),
    ("assign", {"issue": "PROJ-6"}, "assign", ("PROJ-6", "@me"), {}),
    ("link-parent", {"issue": "PROJ-7", "parent": "PROJ-8"}, "link_parent", ("PROJ-7", "PROJ-8"), {}),
    ("add-dependency", {"issue": "PROJ-9", "blocked_by": "PROJ-10"},
     "add_dependency", ("PROJ-9", "PROJ-10"), {}),
    ("add-label", {"issue": "PROJ-11", "label": "needs-spec"}, "add_label", ("PROJ-11", "needs-spec"), {}),
    ("post-comment", {"issue": "PROJ-12", "body": "TL;DR: done"}, "post_comment", ("PROJ-12", "TL;DR: done"), {}),
    ("preflight", {}, "preflight", (), {}),
    ("type-convert", {"issue": "PROJ-13", "issue_type": "epic"}, "type_convert", ("PROJ-13", "epic"), {}),
    ("attachment-download", {"issue": "PROJ-14", "filename_or_id": "log.txt", "output_path": "/tmp/log.txt"},
     "attachment_download", ("PROJ-14", "log.txt", Path("/tmp/log.txt")), {}),
]
"""Every canonical-verb tool, the adapter method it must reach, and the arguments it must pass.

Positional-versus-keyword is part of the expectation: a verb reached with its arguments transposed
would otherwise still look green.
"""


class _Recorder:
    """An adapter that records which verb was asked for instead of performing it.

    `__getattr__` rather than a stub per verb: the cost is that a tool wired to a verb no real adapter
    has still passes here, which `sy_tools/tests/tracker/test_canonical.py` is what closes.
    """

    name = "recorder"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, verb: str):
        async def record(*args: Any, **kwargs: Any) -> dict:
            self.calls.append((verb, args, kwargs))
            return {"verb": verb}

        return record


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def throwaway_preflight_cache(tmp_path, monkeypatch) -> None:
    """Keep the `preflight` tool off the operator's real cache, for every test in this file.

    The tool short-circuits on a fresh cache entry, so without this the wiring assertion below would
    pass or fail according to whether the machine running the suite had run a preflight in the last
    day — and on a miss the suite would write its own verdict into the real cache.
    """
    monkeypatch.setattr(server.preflight_cache, "cache_path", lambda: tmp_path / "preflight-cache.json")


def _payload(result: Any) -> dict:
    """The tool's own JSON result, parsed out of the text content block the SDK wraps it in."""
    return json.loads(result.content[0].text)


def _text(result: Any) -> str:
    """The result's text content, for a failure whose message is prose rather than JSON."""
    return str(result.content[0].text)


@pytest.mark.anyio
async def test_initialize_list_and_call_roundtrip():
    async with mcp.Client(server.mcp) as client:
        info = client.server_info
        assert info is not None, "the handshake produced no serverInfo at all"
        assert info.name == SERVER_NAME
        assert client.protocol_version, "the SDK must have negotiated a protocol version"

        listed = await client.list_tools()
        assert {t.name for t in listed.tools} == TOOL_NAMES
        for tool in listed.tools:
            assert tool.input_schema["type"] == "object", tool.name
            assert tool.description, f"{tool.name} must document itself to the model"

        attach = next(t for t in listed.tools if t.name == "attach-artifact")
        assert set(attach.input_schema["required"]) == {"issue", "path"}, attach.input_schema
        assert attach.input_schema["properties"]["kind"]["default"] == "transcript"
        assert attach.input_schema["properties"]["process_tier"]["anyOf"][0]["enum"] == ["full", "light"]

        schemas = {t.name: t.input_schema for t in listed.tools}
        assert set(schemas["create-issue"]["required"]) == {"issue_type", "title"}, schemas["create-issue"]
        assert "required" not in schemas["find-issues"] or not schemas["find-issues"]["required"], (
            "every find-issues filter is optional; a required one makes the tool uncallable as a plain list"
        )
        assert set(schemas["find-issues"]["properties"]) == {
            "status", "issue_type", "parent", "text", "limit", "page_token"
        }, "the cursor a page returns has to be sendable back, or the paging it advertises is unusable"

        result = await client.call_tool("validate_config", {})
        assert result.is_error is False
        assert set(_payload(result)) == {"valid", "errors", "tracker", "fingerprint"}


@pytest.mark.anyio
@pytest.mark.parametrize(("tool", "args", "verb", "expected_args", "expected_kwargs"), WIRING)
async def test_each_canonical_tool_reaches_its_verb_with_the_arguments_it_was_given(
    monkeypatch, tool, args, verb, expected_args, expected_kwargs
):
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool(tool, args)
    assert result.is_error is False, result.content
    assert recorder.calls == [(verb, expected_args, expected_kwargs)], (
        f"{tool} is mis-wired: it called {recorder.calls}"
    )


@pytest.mark.anyio
async def test_the_three_folded_verbs_have_no_tool_of_their_own(monkeypatch):
    """`create-child`, `post-log` and `link-pr` are content, not tools — pin that they are absent.

    A caller will look for all three by name, so their absence is part of the surface's contract
    rather than an omission to rediscover.
    """
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    async with mcp.Client(server.mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert names.isdisjoint({"create-child", "post-log", "link-pr"}), names

        # Checked positively because it is the only one of the three with a distinguishable argument.
        child = await client.call_tool("create-issue", {"issue_type": "task", "title": "T", "parent": "PROJ-1"})
        assert child.is_error is False, child.content
        assert recorder.calls[0][2]["parent"] == "PROJ-1", "create-issue with a parent is what serves create-child"


@pytest.mark.anyio
async def test_a_tracker_failure_comes_back_as_a_tool_result(monkeypatch):
    """No per-tool try/except: the SDK already turns a raising tool into an `isError` result.

    Asserted once because the mechanism is shared by every tool, and asserted at all because a
    swallowed TrackerError would look like a successful no-op to the model.
    """
    class _Refusing:
        async def get_issue(self, issue: str) -> dict:
            raise server.tracker.TrackerError(f"no such issue {issue}")

    monkeypatch.setattr(server.tracker, "adapter", lambda: _Refusing())
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("get-issue", {"issue": "PROJ-404"})
    assert result.is_error is True
    assert "PROJ-404" in _text(result)


@pytest.mark.anyio
@pytest.mark.parametrize("blank", ["", " ", "\n", "\t"])
async def test_a_blank_issue_id_is_refused_before_the_tracker_is_touched(monkeypatch, blank):
    """A blank string passes schema validation, so the guard has to be in the tool.

    Whitespace-only is included because it is indistinguishable from empty to a reader of the
    result: `create-issue title="\\n"` would create an issue with no findable title at all.
    """
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("get-issue", {"issue": blank})
    assert result.is_error is True
    assert "'issue' is required" in _text(result)


def test_validate_config_reports_an_unresolvable_config_rather_than_crashing(monkeypatch):
    """The tool whose whole contract is reporting a broken config must not crash on one.

    `config.validate()` maps a ConfigError from `resolve()` into the errors list, so the report
    must come back `valid: false` — not escalate into a tool error because the trailing
    tracker/fingerprint lookups re-ran `resolve()` and hit the same failure.
    """
    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise server.config.ConfigError("repo layer is not valid JSON")

    monkeypatch.setattr(server.config, "resolve", broken)
    report = server.validate_config()
    assert report["valid"] is False
    assert report["errors"] == ["repo layer is not valid JSON"]
    assert "tracker" not in report, "an unresolvable config has no resolved values to report"


def test_validate_config_reports_two_columns_configured_under_one_name(monkeypatch):
    """The collision refusal lives in `column_names()`, which nothing asked during validation.

    A config mapping two lifecycle statuses to one column passed `validate_config` clean and then broke
    on the first `canonical_status`/`native_status` call — the diagnosis tool silently disagreeing with
    the vocabulary. The tool has to ask.
    """
    colliding = {
        "columns.backlog": "Created",
        "columns.ready": "In Progress",
        "columns.in_progress": "in progress",
        "columns.in_review": "In Review",
        "columns.done": "Closed",
    }
    monkeypatch.setattr(
        server.tracker.config, "get", lambda key, *, default=None: colliding.get(key, default)
    )
    report = server.validate_config()
    assert report["valid"] is False, report
    assert any("columns.ready" in e and "columns.in_progress" in e for e in report["errors"]), report


def test_validate_config_reports_a_column_read_that_refuses_rather_than_answering_valid(monkeypatch):
    """A `ConfigError` raised while reading the column keys is a reason no tracker verb can run.

    `config.validate()` does not reach every one of them — a credential-shaped key, an adapter map that
    parses for the selected tracker and not for another — so swallowing this one answers
    `valid: true, errors: []` for a config the very next tool call would refuse.
    """
    def refuses(key: str, *, default: Any = None) -> None:
        raise server.config.ConfigError(f"config key {key!r} is credential-shaped and is never read")

    monkeypatch.setattr(server.tracker.config, "get", refuses)
    report = server.validate_config()
    assert report["valid"] is False, report
    assert any("credential-shaped" in e for e in report["errors"]), report


@pytest.mark.anyio
async def test_a_failing_tool_is_a_tool_result_not_a_protocol_error():
    """The distinction the MCP spec draws: the call reached the server and produced an answer.

    Both shapes of failure are checked, because the SDK routes them differently internally and
    only one of them is this package's own code raising: arguments that fail schema validation,
    and a tool name that does not exist.
    """
    async with mcp.Client(server.mcp) as client:
        missing_args = await client.call_tool("attach-artifact", {})
        assert missing_args.is_error is True, "a tool that could not run must report isError"

        unknown = await client.call_tool("ghost", {})
        assert unknown.is_error is True, "an unknown tool is a tool error, not a protocol error"

        assert (await client.call_tool("validate_config", {})).is_error is False, (
            "the session must still serve calls after two failures"
        )


@pytest.mark.anyio
async def test_a_slow_tool_does_not_block_an_unrelated_call(monkeypatch, tmp_path):
    """No head-of-line blocking: the whole reason the adapters went async.

    A stalled upload wedges every call queued behind it. An unrelated `validate_config` must complete
    *while the upload is still in flight* — an ordering claim, so it is asserted against a recorded
    sequence rather than a duration, which would only ever be flaky.
    """
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("already sanitised\n", encoding="utf-8")
    release = anyio.Event()
    order: list[str] = []

    class _StalledBackend:
        async def attach_artifact(self, issue: str, path) -> dict:
            order.append("upload-start")
            await release.wait()
            order.append("upload-end")
            return {"id": "1", "filename": path.name}

    monkeypatch.setattr(server.config, "get", lambda *_a, **_k: True)
    monkeypatch.setattr(server.config, "adapter_map", lambda: {})
    monkeypatch.setattr(server.config, "extra_secret_words", lambda: ())
    monkeypatch.setattr(server.secrets, "sanitize", lambda *_a, **_k: {"redactions": 0})
    monkeypatch.setattr(server.tracker, "adapter", lambda: _StalledBackend())

    # A server that serialised calls would never reach the release, so without the deadline a
    # regression hangs the suite instead of failing it.
    with anyio.fail_after(10, shield=False):
        async with mcp.Client(server.mcp) as client:
            async with anyio.create_task_group() as tasks:

                async def upload() -> None:
                    result = await client.call_tool(
                        "attach-artifact", {"issue": "PROJ-1", "path": str(artifact)}
                    )
                    assert result.is_error is False, result.content
                    order.append("upload-result")

                tasks.start_soon(upload)
                while "upload-start" not in order:
                    await anyio.sleep(0.01)

                fast = await client.call_tool("validate_config", {})
                assert fast.is_error is False, fast.content
                order.append("fast-result")

                release.set()

    assert order.index("fast-result") < order.index("upload-end"), (
        f"the fast call was blocked behind the stalled upload: {order}"
    )


@pytest.mark.anyio
async def test_gate_off_is_a_no_op_skip(monkeypatch):
    """With `transcript.attach` off, nothing is read, scrubbed, scanned, or uploaded."""
    def explode(*_args, **_kwargs):
        raise AssertionError("gate-off must skip before any render/scrub/scan/upload work")

    monkeypatch.setattr(server.config, "get", lambda *_a, **_k: False)
    monkeypatch.setattr(server.secrets, "sanitize", explode)
    monkeypatch.setattr(server.tracker, "adapter", explode)

    result = await server.attach_artifact(issue="PROJ-1", path="/nonexistent/never-opened.txt")
    assert result == {"attached": False, "skipped": True, "reason": "transcript.attach is false", "issue": "PROJ-1"}


@pytest.mark.anyio
async def test_ship_caller_needs_the_full_process_tier(monkeypatch):
    monkeypatch.setattr(server.config, "get", lambda *_a, **_k: True)
    monkeypatch.setattr(server.secrets, "sanitize", pytest.fail)
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)

    result = await server.attach_artifact(
        issue="PROJ-1", path="/nonexistent/never-opened.txt", caller="ship", process_tier="light"
    )
    assert result["skipped"] is True
    assert "full process tier" in result["reason"]


@pytest.mark.anyio
async def test_sanitize_runs_strictly_before_upload(monkeypatch, tmp_path):
    """The security contract: the artifact never leaves the machine before the scrub returns.

    Ordering is not observable from `sanitize`'s own tests, so both calls record against one list: a
    scrub that raises must leave the upload unreached entirely rather than merely unreported.
    """
    path = tmp_path / "artifact.txt"
    path.write_text("nothing secret here\n", encoding="utf-8")
    calls: list[str] = []

    class _Backend:
        async def attach_artifact(self, issue: str, artifact) -> dict:
            calls.append("upload")
            return {"id": f"{issue}-1", "name": artifact.name}

    def _sanitize(*_args, **_kwargs) -> dict:
        calls.append("sanitize")
        return {"redactions": 0}

    monkeypatch.setattr(server.config, "get", lambda *_a, **_k: True)
    monkeypatch.setattr(server.config, "adapter_map", lambda: {})
    monkeypatch.setattr(server.config, "extra_secret_words", lambda: ())
    monkeypatch.setattr(server.tracker, "adapter", lambda *_a, **_k: _Backend())
    monkeypatch.setattr(server.secrets, "sanitize", _sanitize)

    result = await server.attach_artifact(issue="PROJ-1", path=str(path))
    assert result["attached"] is True
    assert calls == ["sanitize", "upload"], "the artifact must not be uploaded before it is scrubbed"

    def _refuse(*_args, **_kwargs) -> dict:
        calls.append("sanitize")
        raise server.secrets.SanitizeError("refusing to upload")

    calls.clear()
    monkeypatch.setattr(server.secrets, "sanitize", _refuse)
    with pytest.raises(server.secrets.SanitizeError, match="refusing to upload"):
        await server.attach_artifact(issue="PROJ-1", path=str(path))
    assert calls == ["sanitize"], "a failed scrub must not be followed by an upload"


@pytest.mark.anyio
async def test_replacing_an_attachment_scrubs_before_it_uploads_and_honours_the_same_gate(monkeypatch, tmp_path):
    """`attachment-update` is an upload, so it owes the same two passes and the same gate as `attach-artifact`.

    A second upload path that skipped either would be exactly the hole that putting gate, scrub and
    upload inside one tool exists to close — and it would be invisible, because the replacement lands
    looking identical to a sanitised one.
    """
    path = tmp_path / "PROJ-1-ship-transcript.txt"
    path.write_text("already sanitised\n", encoding="utf-8")
    calls: list[str] = []

    class _Backend:
        async def attachment_update(self, issue: str, artifact) -> dict:
            calls.append("upload")
            return {"issue": issue, "filename": artifact.name, "replaced": 1}

    monkeypatch.setattr(server.config, "get", lambda *_a, **_k: True)
    monkeypatch.setattr(server.config, "adapter_map", lambda: {})
    monkeypatch.setattr(server.config, "extra_secret_words", lambda: ())
    monkeypatch.setattr(server.secrets, "sanitize", lambda *_a, **_k: calls.append("sanitize") or {"redactions": 0})
    monkeypatch.setattr(server.tracker, "adapter", lambda: _Backend())

    result = await server.attachment_update(issue="PROJ-1", path=str(path), caller="ship", process_tier="full")
    assert result["updated"] is True
    assert calls == ["sanitize", "upload"], "the replacement must not be uploaded before it is scrubbed"

    monkeypatch.setattr(server.config, "get", lambda *_a, **_k: False)
    monkeypatch.setattr(server.secrets, "sanitize", pytest.fail)
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)
    skipped = await server.attachment_update(issue="PROJ-1", path=str(path))
    assert skipped == {
        "updated": False, "skipped": True, "reason": "transcript.attach is false", "issue": "PROJ-1"
    }


@pytest.mark.parametrize(
    ("kind", "caller", "tier", "expected_skip"),
    [
        ("transcript", "spec", None, False),   # spec gates on transcript.attach alone
        ("transcript", "ship", "full", False),
        ("transcript", "ship", "light", True),
        ("report", "ship", "light", False),    # a non-transcript artifact is ungated
    ],
)
def test_gate_matrix(monkeypatch, kind, caller, tier, expected_skip):
    monkeypatch.setattr(server.config, "get", lambda *_a, **_k: True)
    assert (server._gate_skip_reason(kind, caller, tier) is not None) is expected_skip


def _metrics_comment(**fields: Any) -> str:
    """A `# Claude Code ship metrics` comment body, shaped exactly as handoff-accounting.md posts one."""
    body = {"schema": SCHEMA_ID, "task": "PROJ-1", **fields}
    return "# Claude Code ship metrics\n\n```json\n" + json.dumps(body, indent=2) + "\n```\n"


ALL_NULLS = {
    "pr_url": None,
    "plan_divergence_count": None,
    "deviations_declined": None,
    "ci_fix_rounds": None,
    "review_fix_rounds": None,
    "review_findings_accepted": None,
    "review_findings_rejected": None,
    "gate_false_pass": None,
    "gate_false_pass_reason": None,
    "post_merge_defect": None,
    "rollback": None,
    "lead_time_seconds": None,
    "transcript_attachment": None,
}
"""Every optional field explicitly null — the shape a `light`-tier run with nothing yet known posts.

Pinned as a whole rather than field by field because the design invariant is about the set: a field
that stopped accepting `null` would make an honest unknown unrecordable, and the workflow's documented
answer to "we do not know yet" is exactly this body.
"""


@pytest.mark.anyio
async def test_an_all_nulls_ship_metrics_body_is_accepted(monkeypatch):
    """`null` means unknown and must stay postable: converting one to zero is the failure mode."""
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": _metrics_comment(**ALL_NULLS)})
    assert result.is_error is False, result.content
    assert recorder.calls[0][0] == "post_comment", recorder.calls


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("case", "body"),
    [
        ("no task at all", "# Claude Code ship metrics\n\n```json\n{\"schema\": \"" + SCHEMA_ID + "\"}\n```\n"),
        ("misspelled field", _metrics_comment(ci_fix_round=2)),
        ("null where the field is never null", _metrics_comment(human_review_defects=None)),
        ("a count below zero", _metrics_comment(ci_fix_rounds=-1)),
        ("a corrected gate verdict with no reason", _metrics_comment(gate_false_pass=True)),
        ("a string where a count belongs", _metrics_comment(review_fix_rounds="two")),
        ("json the fence holds but nothing parses", _metrics_comment().replace('"PROJ-1"\n', '"PROJ-1",\n')),
        ("no fence at all, because the body is CRLF", _metrics_comment().replace("\n", "\r\n")),
        ("the block pasted as prose with no fence", _metrics_comment().replace("```json\n", "").replace("\n```", "")),
        (
            "two fenced blocks and neither is the log",
            "# Claude Code ship metrics\n\n```bash\ngit status --short\n```\n\n"
            + '```json\n{"schema": "shipyard.claude_usage.v1"}\n```\n'
            + f"The {SCHEMA_ID} block was meant to be here.\n",
        ),
    ],
)
async def test_a_malformed_ship_metrics_body_is_refused_before_anything_is_posted(monkeypatch, case, body):
    """The tool boundary is where this is enforced, so nothing reaches the adapter on a bad shape.

    `pytest.fail` as the adapter factory is the assertion that matters: a validation running *after*
    the write would report an error having already posted the malformed comment.
    """
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": body})
    assert result.is_error is True, f"{case} was accepted: {result.content}"
    assert SCHEMA_ID in _text(result), f"the refusal for {case} must name the schema it checked"


@pytest.mark.anyio
async def test_a_second_block_claiming_the_schema_is_refused_rather_than_validated_off_the_first(monkeypatch):
    """Two candidate blocks are ambiguous, and a first-match check posts the one it never looked at.

    The body quotes a prior valid metrics block for comparison and then carries the log it actually
    means to post, which is malformed. Validating the first match accepts the comment and lands the
    second block unchecked — the same unvalidated-log incident, wearing a valid block as cover.
    """
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)
    body = (
        "TL;DR: compare against last time's numbers.\n\n"
        + _metrics_comment(ci_fix_rounds=1)
        + "\nAnd this run:\n\n"
        + _metrics_comment(ci_fix_rounds=-1)
    )
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": body})
    assert result.is_error is True, f"the first matching block validated and the second posted unread: {body}"
    assert SCHEMA_ID in _text(result) and "2" in _text(result), (
        f"the refusal must name the schema and how many candidate blocks it found: {_text(result)}"
    )


@pytest.mark.anyio
async def test_a_second_block_claiming_the_schema_but_not_parsing_is_refused_not_silently_dropped(monkeypatch):
    """The candidate tally must arm on a block's raw text, or an unparseable block is invisible twice.

    Counting only blocks that *parsed* into a matching object drops a malformed one from both the
    ambiguity count and the schema check, so the valid block beside it validates alone and the comment
    posts carrying an unread machine log — the bypass this validation exists to close, one level down
    from the body-level "names the id" arming that already refuses a lone unparseable block.
    """
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)
    unparseable = _metrics_comment().replace('"PROJ-1"\n', '"PROJ-1",\n')
    assert SCHEMA_ID in unparseable and '"PROJ-1",' in unparseable, "the second block must claim the id and not parse"
    body = _metrics_comment(ci_fix_rounds=1) + "\nAnd this run:\n\n" + unparseable
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": body})
    assert result.is_error is True, f"an unparseable second block claiming the schema was posted unread: {body}"
    assert SCHEMA_ID in _text(result) and "2" in _text(result), (
        f"the refusal must name the schema and count the unparseable block too: {_text(result)}"
    )


def _claiming_json_that_does_not_parse() -> str:
    """JSON naming the schema id that `json.loads` refuses, asserted rather than assumed.

    A spelling that still parsed would quietly turn every test using it into a no-op.
    """
    text = json.dumps({"schema": SCHEMA_ID, "task": "PROJ-1"}, indent=2).replace('"PROJ-1"\n', '"PROJ-1",\n')
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)
    return text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("case", "tail"),
    [
        ("a fence that is never closed", "```json\n{block}\n"),
        ("a closing marker with text after it", "```json\n{block}\n``` end\n"),
        ("a tilde fence that is never closed", "~~~json\n{block}\n"),
    ],
)
async def test_a_fence_invisible_block_claiming_the_schema_is_refused_beside_a_valid_log(monkeypatch, case, tail):
    """A claiming block `FENCE` cannot see at all is the bypass one level below the raw-text tally.

    Counting candidates off `FENCE` matches only sees blocks whose closing marker is a bare fence on
    its own line, so an unclosed fence, or one whose closing marker carries trailing text, is neither
    a candidate nor an ambiguity and the valid block beside it validates alone. It still renders as a
    second code block once the body goes through `markdown_to_adf`, landing exactly as authoritative
    as the block that was checked.
    """
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)
    body = _metrics_comment() + "\nAnd this run:\n\n" + tail.format(block=_claiming_json_that_does_not_parse())
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": body})
    assert result.is_error is True, f"{case} was invisible to the check and posted unread: {body}"
    assert SCHEMA_ID in _text(result), f"the refusal for {case} must name the schema it checked: {_text(result)}"


@pytest.mark.anyio
async def test_a_fence_invisible_block_that_never_names_the_schema_still_posts(monkeypatch):
    """The outside-the-fences check must arm on the id, not on unfenced text being malformed.

    Broken JSON quoted without a closing fence is ordinary postmortem prose, and the narrowing that
    keeps a properly fenced unrelated block postable has to hold for an unfenced one too.
    """
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    body = _metrics_comment() + '\nThe payload that failed:\n\n```json\n{"schema": "other.v1", "a": 1,}\n'
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": body})
    assert result.is_error is False, f"an unfenced block naming no schema must not arm the check: {result.content}"
    assert recorder.calls[0][1] == ("PROJ-1", body), "the body must reach the adapter byte-for-byte"


@pytest.mark.anyio
async def test_a_record_in_the_fence_info_string_is_refused_beside_the_block_it_hides_behind(monkeypatch):
    """A fence's opening line is not its content, so a log parked there was validated by nothing.

    `FENCE`'s content group starts after the first newline: whatever sits after the marker on the
    opening line is the info-string position, conventionally a language tag, and the pattern allows
    anything there. Tallying candidates off the whole match counts the fence once for an id seen in
    that text, then validates only the content, and the info string's own record posts unread —
    rendering as part of the code block's first line, as authoritative as what was checked.
    """
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)
    body = (
        f'```json {{"schema": "{SCHEMA_ID}", "task": "AM-9999", "human_review_defects": 99}}\n'
        f'{{"schema": "{SCHEMA_ID}", "task": "AM-1236"}}\n'
        "```\n"
    )
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": body})
    assert result.is_error is True, f"a record in the info string was invisible to the check: {body}"
    assert SCHEMA_ID in _text(result) and "2" in _text(result), (
        f"the refusal must name the schema and count the info string's record too: {_text(result)}"
    )


@pytest.mark.anyio
async def test_an_info_string_naming_the_schema_over_content_that_does_not_is_refused_as_carrying_no_block(monkeypatch):
    """The degenerate half of the same split: the only mention of the id is in a non-content position.

    Nothing here is a candidate block — the content parses but is not a machine log — so this is the
    "names the id, carries no block" case, not the ambiguity case. Pinning *which* refusal it gets is
    the point: reading it as ambiguous would tell the caller to remove a second block that isn't there.
    """
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)
    body = f'```json {{"schema": "{SCHEMA_ID}"}}\n{{"task": "AM-1236"}}\n```\n'
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": body})
    assert result.is_error is True, f"an info-string-only mention was posted unchecked: {body}"
    assert "carries no fenced block that parses as one" in _text(result), (
        f"one mention in a non-content position is the no-valid-block refusal, not ambiguity: {_text(result)}"
    )


@pytest.mark.anyio
async def test_a_valid_log_whose_info_string_carries_more_than_a_language_tag_still_posts(monkeypatch):
    """Leaving the info string to the stray-mention check must stay a no-op on info strings as such.

    The refusals above turn on the info string *naming this id*, not on it holding anything beyond the
    bare language tag — a fence annotated with a title or a highlight directive is ordinary Markdown.
    """
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    body = _metrics_comment().replace("```json\n", '```json title="ship metrics"\n')
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": body})
    assert result.is_error is False, f"an annotated info string must not arm the check: {result.content}"
    assert recorder.calls[0][1] == ("PROJ-1", body), "the body must reach the adapter byte-for-byte"


@pytest.mark.anyio
async def test_an_unrelated_malformed_block_beside_a_valid_log_still_posts(monkeypatch):
    """The raw-text tally must stay narrow: a broken block that never names this id is not a candidate.

    Arming on raw text is one step away from rejecting any comment that happens to quote broken JSON
    next to a valid metrics log, which would make the check unusable for exactly the postmortem
    comments it is meant to accompany.
    """
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    body = _metrics_comment() + '\nThe payload that failed:\n\n```json\n{"schema": "other.v1", "a": 1,}\n```\n'
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": body})
    assert result.is_error is False, f"an unrelated malformed block must not be a candidate: {result.content}"
    assert recorder.calls[0][1] == ("PROJ-1", body), "the body must reach the adapter byte-for-byte"


def _escaped_id() -> str:
    """The schema id with its last character written as a `\\u` escape, so no literal id is present.

    Both halves are asserted rather than assumed: an escape that did not decode to the id, or one that
    still held it literally, would turn every test below into a check of the case it exists to defeat.
    """
    escaped = SCHEMA_ID[:-1] + "\\u00" + format(ord(SCHEMA_ID[-1]), "x")
    assert json.loads(f'"{escaped}"') == SCHEMA_ID, f"{escaped} must decode to the schema id"
    assert SCHEMA_ID not in escaped, f"{escaped} must not name the schema id literally"
    return escaped


def _escaped_metrics_block(**fields: Any) -> str:
    """A fenced metrics record whose `schema` value is escaped, hand-built to keep the escape intact."""
    written = "".join(f', "{name}": {json.dumps(value)}' for name, value in fields.items())
    return '```json\n{"schema": "' + _escaped_id() + '"' + written + "}\n```\n"


@pytest.mark.anyio
async def test_a_record_that_escapes_the_schema_id_is_still_validated(monkeypatch):
    """A record claiming the id only through a `\\u` escape is validated, not waved through.

    Two identity rules — a parse for what counts as the record, literal text for what arms the check —
    leave a gap, because JSON can spell one string many ways: a body with no literal occurrence of the
    id arms nothing and posts unread. `json.loads` normalises the escape, so it is a claim by every
    definition except the literal one.
    """
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)
    body = _escaped_metrics_block(task="   ", ci_fix_rounds=-7, ci_fix_round=2)
    assert SCHEMA_ID not in body, f"the escaped body must not arm a literal-text check: {body}"
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": body})
        # `extra="forbid"` fails before the model validators run, so only the same record without the
        # misspelled field shows the escaped content reaching them: the blank `task` raises first.
        rest = await client.call_tool(
            "post-comment", {"issue": "PROJ-1", "body": _escaped_metrics_block(task="   ", ci_fix_rounds=-7)}
        )
    assert result.is_error is True, f"an escaped record was posted unvalidated: {body}"
    text = _text(result)
    assert "does not match the schema" in text, f"the escaped record must be validated, not merely refused: {text}"
    assert "ci_fix_round" in text, f"the refusal must name the misspelled field it rejected: {text}"
    assert rest.is_error is True and "cannot be blank" in _text(rest), (
        f"an escaped record must be checked field by field like any other: {_text(rest)}"
    )


@pytest.mark.anyio
async def test_a_valid_record_that_escapes_the_schema_id_posts(monkeypatch):
    """Deciding identity on the parsed value means *validating* escaped records, not refusing them.

    The counterweight to the test above: a record is judged by what it says, so an escaped spelling of
    the id is a machine log to check against the schema like any other, and one that passes posts.
    """
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    body = _escaped_metrics_block(task="PROJ-1", ci_fix_rounds=1)
    assert SCHEMA_ID not in body, f"the escaped body must not arm a literal-text check: {body}"
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": body})
    assert result.is_error is False, f"a valid escaped record must post: {result.content}"
    assert recorder.calls[0][1] == ("PROJ-1", body), "the body must reach the adapter byte-for-byte"


@pytest.mark.anyio
async def test_an_escaped_second_record_is_counted_beside_a_literal_valid_one(monkeypatch):
    """The tally has to see an escaped claim too, or a valid block is cover for one nobody read.

    A literal valid log beside an escaped record correcting the gate verdict with no reason: counting
    claims by literal text found only the first, validated it alone, and posted a `gate_false_pass`
    the schema would have rejected — the ambiguity case wearing an escape instead of a trailing comma.
    """
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)
    body = _metrics_comment() + "\nAnd the correction:\n\n" + _escaped_metrics_block(
        task="PROJ-1", gate_false_pass=True
    )
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": body})
    assert result.is_error is True, f"an escaped second record was invisible to the tally: {body}"
    assert SCHEMA_ID in _text(result) and "2" in _text(result), (
        f"the refusal must name the schema and count the escaped record too: {_text(result)}"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("case", "wrapper"),
    [("wrapped in an array", "[{inner}]"), ("nested under a key", '{{"log": {inner}}}')],
)
async def test_an_escaped_record_one_level_down_is_still_a_claim(monkeypatch, case, wrapper):
    """Identity is one rule at every depth, or the gap reopens exactly one level lower.

    A literal `[{"schema": "shipyard.ship_metrics.v1", ...}]` is already refused, caught by the
    raw-text fallback on the block's content. Checking the parsed value only at the top level would
    let the *escaped* spelling of that same block post unread — the same two-rules asymmetry, one
    nesting deep. Counted, never validated: what a machine log is stays the top-level object.
    """
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)
    inner = '{"schema": "' + _escaped_id() + '", "task": "   ", "ci_fix_round": 2}'
    body = "```json\n" + wrapper.format(inner=inner) + "\n```\n"
    assert SCHEMA_ID not in body, f"the escaped body must not arm a literal-text check: {body}"
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": body})
    assert result.is_error is True, f"an escaped record {case} was posted unread: {body}"
    assert "carries no fenced block that parses as one" in _text(result), (
        f"a buried claim is the no-block refusal, not a validated record: {_text(result)}"
    )


def _claiming_record(spelling: str) -> str:
    """The counterexample record — blank task, a negative count, a misspelled field — with `spelling`
    as its `schema` value, hand-built so an escaped spelling survives into the body verbatim."""
    return '{"schema": "' + spelling + '", "task": "   ", "ci_fix_rounds": -7, "ci_fix_round": 2}'


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("case", "shape"),
    [
        ("a fence that is never closed", "```json\n{record}\n"),
        ("a closing marker with text after it", "```json\n{record}\n``` end\n"),
        ("CRLF line endings, so nothing closes the fence", "```json\r\n{record}\r\n```\r\n"),
        ("a BOM before the JSON the fence does close around", "```json\n﻿{record}\n```\n"),
        ("no fence at all, the record pasted as prose", "The log:\n\n{record}\n"),
        ("a closed fence whose content parses cleanly", "```json\n{record}\n```\n"),
    ],
)
async def test_an_escaped_id_earns_the_same_answer_as_a_literal_one_in_every_shape(monkeypatch, case, shape):
    """One identity rule means the *same* answer for both spellings of the id, in every body shape.

    Deciding identity on the parsed value covers only the shapes where a parse happens: a properly
    closed fence holding clean JSON. Every other shape here — unclosed and trailing-text fences, a CRLF
    body, a BOM the fence does close around, unfenced prose — falls back to a literal substring search
    for the id, which an escaped spelling never matches.

    Pinned as literal-versus-escaped pairs rather than as expected messages: what goes wrong is the two
    spellings diverging, not the wording of either answer.
    """
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)
    literal = shape.format(record=_claiming_record(SCHEMA_ID))
    escaped = shape.format(record=_claiming_record(_escaped_id()))
    assert SCHEMA_ID not in escaped, f"the escaped body must not arm a literal-text check: {escaped}"
    async with mcp.Client(server.mcp) as client:
        results = [await client.call_tool("post-comment", {"issue": "PROJ-1", "body": b}) for b in (literal, escaped)]
    assert [r.is_error for r in results] == [True, True], f"{case} was accepted for one spelling: {results}"
    assert _text(results[0]) == _text(results[1]), (
        f"{case} answers the two spellings differently: {_text(results[0])!r} vs {_text(results[1])!r}"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("case", "payload"),
    [
        ("an integer past the digit limit", "1" * 5_000),
        ("brackets nested past the decoder's stack", "[" * 100_000),
    ],
)
async def test_content_json_cannot_parse_at_all_is_a_refusal_not_a_crash(monkeypatch, case, payload):
    """`json.loads` fails in three ways, and only one of them is a `JSONDecodeError`.

    A digit string past `sys.int_info.str_digits_check_threshold` raises a bare `ValueError`, and
    nesting past the decoder's recursive descent raises `RecursionError`. Catching only the decode error
    let a body choose which uncaught exception escaped the tool instead of being answered as content
    that does not parse — which is the fallback path, and a refusal.
    """
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)
    body = f"```json\n{{\"schema\": \"{SCHEMA_ID}\", \"n\": {payload}\n```\n"
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": body})
    assert result.is_error is True, f"{case} was posted unread: {result.content}"
    assert "carries no fenced block that parses as one" in _text(result), (
        f"{case} must be answered as unparseable content, not as an uncaught exception: {_text(result)}"
    )


def test_unescaping_stays_linear_on_a_large_body():
    """Undoing escapes must not reintroduce the cost the linear fence scan removed.

    The substitution is a fixed-width match with nothing to backtrack into, so both a body that is
    almost entirely escapes and one holding none at all cost a single pass. Same loose bound as the
    fence scan's, and for the same reason: these measure in milliseconds.
    """
    for case, body in [
        ("all escapes", f"The {SCHEMA_ID} log:\n" + "\\u0041" * 100_000),
        ("no escapes", f"The {SCHEMA_ID} log:\n" + "a" * 600_000),
    ]:
        start = time.perf_counter()
        with pytest.raises(server.ToolError, match="carries no fenced block"):
            server._validate_machine_log(body)
        assert time.perf_counter() - start < 2.0, f"unescaping {case} is not a single linear pass"


def test_fence_detection_stays_linear_on_a_body_of_unclosed_openers():
    """The scan is linear, and this is the body that proves it: 40,000 openers that never close.

    The backtracking pattern this replaced took 91 seconds here, because a lazy match reaching for the
    next closing marker re-scans the rest of the body once per opener. `_validate_machine_log` runs
    synchronously inside the `post_comment` coroutine, so that is not one slow call but the whole
    server's event loop wedged for every concurrent tool call by one malformed comment.
    """
    body = f"The {SCHEMA_ID} log was meant to go here:\n" + "```json\n" * 40_000
    start = time.perf_counter()
    with pytest.raises(server.ToolError, match="carries no fenced block"):
        server._validate_machine_log(body)
    # Loose on purpose (the scan measures in single-digit milliseconds): a slow runner must not make
    # this flaky, while a quadratic regression still fails it by three orders of magnitude.
    assert time.perf_counter() - start < 2.0, "fence detection is backtracking again, not scanning"

    start = time.perf_counter()
    server._validate_machine_log("```json\n" * 40_000)
    assert time.perf_counter() - start < 2.0, "a body that claims nothing must not be scanned at all"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("case", "body"),
    [
        ("plain prose", "TL;DR: the gate passed."),
        ("a different machine log", "# Claude Code usage\n\n```json\n{\"schema\": \"shipyard.claude_usage.v1\"}\n```"),
        ("prose about metrics that names no schema", "TL;DR: the ship metrics log is on the task."),
        ("a fenced block that is not JSON", "```bash\ngit status --short\n```"),
        ("a valid log beside an unrelated fenced block", _metrics_comment() + "\n```bash\ngit log -1\n```\n"),
    ],
)
async def test_a_body_that_is_not_a_ship_metrics_log_passes_through_unvalidated(monkeypatch, case, body):
    """A body that never names this schema is not this validation's business, whatever else it holds.

    The boundary moved with the bypass fix: naming the schema id now demands a block that validates,
    so what proves the check stays narrow is a body that talks about metrics without claiming the id.
    """
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": body})
    assert result.is_error is False, f"{case} must post unchanged: {result.content}"
    assert recorder.calls[0][1] == ("PROJ-1", body), f"{case} must reach the adapter byte-for-byte"


BODY_WRITES = [
    ("update-issue", lambda body: {"issue": "PROJ-1", "body": body}),
    ("create-issue", lambda body: {"issue_type": "task", "title": "T", "body": body}),
]
"""Every tool that writes a caller-supplied body, and the arguments that carry one.

The machine-log gate is the body's, not the comment's: a log written into an issue body is the same
unvalidated-metrics incident, so each of these writes has to refuse what `post-comment` refuses.
"""


@pytest.mark.anyio
@pytest.mark.parametrize(("tool", "arguments"), BODY_WRITES, ids=[t for t, _ in BODY_WRITES])
@pytest.mark.parametrize(
    ("case", "body"),
    [
        ("a count below zero", _metrics_comment(ci_fix_rounds=-1)),
        ("json the fence holds but nothing parses", _metrics_comment().replace('"PROJ-1"\n', '"PROJ-1",\n')),
        ("two candidate blocks and no way to choose", _metrics_comment() + "\n" + _metrics_comment(ci_fix_rounds=1)),
    ],
)
async def test_a_malformed_ship_metrics_body_is_refused_by_every_write_not_only_comments(
    monkeypatch, tool, arguments, case, body
):
    """The gate belongs to the body, so a body write must refuse a log a comment would have refused.

    Validation reached only from `post-comment` leaves an issue body as an unguarded second route for
    exactly the malformed log the gate exists to stop.
    """
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool(tool, arguments(body))
    assert result.is_error is True, f"{tool} accepted {case}: {result.content}"
    assert SCHEMA_ID in _text(result), f"the refusal for {case} must name the schema it checked"


@pytest.mark.anyio
@pytest.mark.parametrize(("tool", "arguments"), BODY_WRITES, ids=[t for t, _ in BODY_WRITES])
async def test_an_ordinary_body_still_reaches_the_adapter_unchanged(monkeypatch, tool, arguments):
    """The gate is a no-op for a body that claims nothing, which is nearly every body these writes take."""
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    body = "TL;DR: prose about the ship metrics log, claiming no schema.\n\n```bash\ngit log -1\n```\n"
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool(tool, arguments(body))
    assert result.is_error is False, f"{tool} refused an ordinary body: {result.content}"
    _verb, args, kwargs = recorder.calls[0]
    assert body in (*args, *kwargs.values()), f"{tool} must pass the body through byte-for-byte: {recorder.calls}"


@pytest.mark.anyio
async def test_create_issue_still_accepts_no_body_at_all(monkeypatch):
    """`create-issue`'s body defaults to `""`, and the gate must not turn an omitted body into a refusal."""
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("create-issue", {"issue_type": "task", "title": "T"})
    assert result.is_error is False, result.content
    assert recorder.calls[0][2]["body"] == "", recorder.calls


SENTINEL = "sy-check-env-sentinel-9f3a1c"
"""A value distinctive enough that grepping a whole tool result for it is a sufficient leak test."""


@pytest.mark.anyio
@pytest.mark.parametrize("present", [True, False], ids=["set", "unset"])
async def test_check_env_reports_presence_and_never_the_value(monkeypatch, present):
    """Presence has to be reported correctly, and the value must appear nowhere in the result.

    Serialising the entire result and searching it for the sentinel is the assertion, rather than
    checking one field: the whole point of the tool is that no field, no error string and no log line
    can carry the value, so what gets pinned is the absence of the value from all of it.
    """
    if present:
        monkeypatch.setenv("SY_CHECK_ENV_PROBE", SENTINEL)
    else:
        monkeypatch.delenv("SY_CHECK_ENV_PROBE", raising=False)
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("check_env", {"name": "SY_CHECK_ENV_PROBE"})
    assert result.is_error is False, result.content
    assert _payload(result) == {"name": "SY_CHECK_ENV_PROBE", "present": present}, _payload(result)
    assert SENTINEL not in str(result), f"the variable's value reached the tool result: {result}"


@pytest.mark.anyio
async def test_check_env_refuses_a_blank_name_without_naming_a_value(monkeypatch):
    """A whitespace-only name is refused like every other required argument, and the error leaks nothing."""
    monkeypatch.setenv("SY_CHECK_ENV_PROBE", SENTINEL)
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("check_env", {"name": " \n"})
    assert result.is_error is True, result.content
    assert SENTINEL not in _text(result), f"the refusal carried a value: {_text(result)}"


@pytest.mark.anyio
async def test_check_env_reads_an_empty_variable_as_unset(monkeypatch):
    """A variable exported empty holds no credential, so reporting it as set would be a false all-clear."""
    monkeypatch.setenv("SY_CHECK_ENV_PROBE", "")
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("check_env", {"name": "SY_CHECK_ENV_PROBE"})
    assert _payload(result)["present"] is False, _payload(result)


SCRUB_WRITES = [("post-comment", lambda body: {"issue": "PROJ-1", "body": body}), *BODY_WRITES]
"""Every tool that takes a caller-supplied body, the comment write included.

`BODY_WRITES` leaves `post-comment` out because the machine-log gate drives it separately, but the
scrub is one shared helper serving all three writes — and a helper wired into two of the three sites
is precisely the half-fixed duplicate this closes, so every write that takes a body is asserted.
"""

FAKE_SECRET_VAR = "SY_TEST_FAKE_TOKEN"
"""A test-only variable name, credential-shaped by the same word heuristic discovery uses.

Never the real declared credential: reading an actual token here would put it one failure message
away from a permanent transcript, which is the leak the code under test exists to prevent.
"""

FAKE_SECRET = "sy-fake-token-3d91f7-not-a-credential"
"""Past the 6-character discovery floor, and distinctive enough that grepping a whole result is sound."""


def _body_sent(recorder: _Recorder) -> str:
    """The body the adapter was actually handed, whether the tool passed it positionally or by name."""
    _verb, args, kwargs = recorder.calls[0]
    return str(kwargs["body"] if "body" in kwargs else args[-1])


@pytest.mark.anyio
@pytest.mark.parametrize(("tool", "arguments"), SCRUB_WRITES, ids=[t for t, _ in SCRUB_WRITES])
async def test_a_known_credential_value_never_reaches_the_adapter_through_a_body(monkeypatch, tool, arguments):
    """The gap this closes: three writes handed a caller's body to the tracker with no scrub at all.

    Bodies are assembled out of command output and transcript text, so a credential landing in one is a
    routine accident rather than an exotic one — and a posted comment is durable, visible to everyone
    with issue access, and not made safe again by deleting it.
    """
    monkeypatch.setenv(FAKE_SECRET_VAR, FAKE_SECRET)
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    body = f"TL;DR: the run exported {FAKE_SECRET} and then logged {FAKE_SECRET} again.\n"
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool(tool, arguments(body))
    assert result.is_error is False, result.content
    sent = _body_sent(recorder)
    # The load-bearing assertion is the value's absence; the marker below only proves the scrub ran.
    assert FAKE_SECRET not in sent, f"{tool} handed the credential straight to the tracker"
    assert sent.count(f"<REDACTED:{FAKE_SECRET_VAR}>") == 2, f"{tool} redacted only part of the body: {sent}"
    report = _payload(result)["scrub"]
    assert report["scrubbed_vars"] == [FAKE_SECRET_VAR], report
    assert report["redactions"] == 2, report
    assert FAKE_SECRET not in str(result), "the result disclosed the value it had just redacted"


@pytest.mark.anyio
@pytest.mark.parametrize(("tool", "arguments"), SCRUB_WRITES, ids=[t for t, _ in SCRUB_WRITES])
async def test_a_body_holding_no_known_secret_is_written_byte_for_byte(monkeypatch, tool, arguments):
    """Nearly every body is this one, so a scrub that finds nothing has to be invisible."""
    monkeypatch.delenv(FAKE_SECRET_VAR, raising=False)
    monkeypatch.setattr(server.config, "adapter_map", lambda: {})
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    body = "TL;DR: ordinary prose.\n\n```bash\ngit log -1\n```\n"
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool(tool, arguments(body))
    assert result.is_error is False, result.content
    assert _body_sent(recorder) == body, f"{tool} rewrote a body holding nothing to redact"
    assert _payload(result)["scrub"] == {
        "scrubbed_vars": [], "redactions": 0,
        "declared_absent_from_env": [], "declared_below_length_floor": [],
    }, _payload(result)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "arguments", "field"),
    [
        ("create-issue", lambda v: {"issue_type": "task", "title": f"TL;DR: {v} leaked", "body": "b"}, "title"),
        ("add-label", lambda v: {"issue": "PROJ-1", "label": v}, "label"),
    ],
    ids=["create-issue-title", "add-label-label"],
)
async def test_every_caller_supplied_field_is_scrubbed_not_only_the_body(monkeypatch, tool, arguments, field):
    """A write's other caller-supplied strings are the same class of value as its body.

    A scrub wired to `body` alone hands a credential pasted into `create-issue`'s `title` to the adapter
    verbatim, while the report beside it says `redactions: 1` and reads as full coverage of the write. A
    title is durable, echoed by every search result and every failed write, and not made safe again by
    editing it; `add-label`'s `label` is the same shape at lower volume.
    """
    monkeypatch.setenv(FAKE_SECRET_VAR, FAKE_SECRET)
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool(tool, arguments(FAKE_SECRET))
    assert result.is_error is False, result.content
    _verb, args, kwargs = recorder.calls[0]
    sent = str(kwargs.get(field) or args[0 if tool == "create-issue" else 1])
    assert FAKE_SECRET not in sent, f"{tool} handed the credential to the tracker through {field}"
    assert f"<REDACTED:{FAKE_SECRET_VAR}>" in sent, f"{tool} did not scrub {field}: {sent}"
    assert FAKE_SECRET not in str(result), "the result disclosed the value it had just redacted"


@pytest.mark.anyio
async def test_one_scrub_report_counts_every_field_of_a_write_not_just_the_body(monkeypatch):
    """A count covering some of a write's fields is worse than none: it is read as covering all of them."""
    monkeypatch.setenv(FAKE_SECRET_VAR, FAKE_SECRET)
    monkeypatch.setattr(server.config, "adapter_map", lambda: {})
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("create-issue", {
            "issue_type": "task",
            "title": f"TL;DR: {FAKE_SECRET}",
            "body": f"it printed {FAKE_SECRET} twice: {FAKE_SECRET}\n",
        })
    assert result.is_error is False, result.content
    report = _payload(result)["scrub"]
    assert report == {
        "scrubbed_vars": [FAKE_SECRET_VAR], "redactions": 3,
        "declared_absent_from_env": [], "declared_below_length_floor": [],
    }, f"the report must total the title's redaction with the body's two: {report}"


@pytest.mark.anyio
async def test_a_declared_credential_absent_from_the_environment_is_reported_not_refused(monkeypatch):
    """`secrets.sanitize` raises on this and a body write must not, because the two cases differ.

    `sanitize` scrubs a file another process produced, which can hold a value this process never sees,
    so a clean zero-redaction run there is a false all-clear worth failing on. A body is composed here,
    out of strings this process holds, so a value absent from this environment cannot be in it. Raising
    would hard-block every tracker write for anyone whose credential is not exported — the default
    configuration under CI among them, which exports none.
    """
    monkeypatch.setattr(server.config, "adapter_map", lambda: {"secret_env": ["SY_TEST_UNSET_TOKEN"]})
    monkeypatch.delenv("SY_TEST_UNSET_TOKEN", raising=False)
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": "TL;DR: nothing secret.\n"})
    assert result.is_error is False, result.content
    assert _payload(result)["scrub"]["declared_absent_from_env"] == ["SY_TEST_UNSET_TOKEN"], _payload(result)


@pytest.mark.anyio
async def test_a_declared_credential_is_scrubbed_even_where_discovery_would_skip_it(monkeypatch):
    """A declared name outranks discovery's *name* heuristic, which is the half a declaration replaces.

    This name holds no credential word, so auto-discovery alone would post the value verbatim — while
    the configuration says in as many words that it is the credential.
    """
    monkeypatch.setattr(server.config, "adapter_map", lambda: {"secret_env": ["SY_TEST_DECLARED"]})
    monkeypatch.setenv("SY_TEST_DECLARED", "q7zx4m")
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": "TL;DR: it said q7zx4m.\n"})
    assert result.is_error is False, result.content
    assert _body_sent(recorder) == "TL;DR: it said <REDACTED:SY_TEST_DECLARED>.\n", _body_sent(recorder)
    assert _payload(result)["scrub"] == {
        "scrubbed_vars": ["SY_TEST_DECLARED"], "redactions": 1,
        "declared_absent_from_env": [], "declared_below_length_floor": [],
    }, _payload(result)


@pytest.mark.anyio
async def test_a_declared_value_under_the_length_floor_is_reported_rather_than_redacted(monkeypatch):
    """The length floor is not part of the heuristic a declaration overrides: it stops a corruption.

    Forcing a sub-floor value in replaces every occurrence of it anywhere in the write, so a
    one-character declared credential redacts every space in the prose — mangling the body to protect a
    value too short to be one, and disagreeing with `secrets.sanitize`, which treats a sub-floor value
    as absent and refuses. Dropping it silently is the other half: this path cannot refuse, so a caller
    whose exported credential will not be scrubbed has to be told which one.
    """
    monkeypatch.setattr(server.config, "adapter_map", lambda: {"secret_env": ["SY_TEST_DECLARED"]})
    monkeypatch.setenv("SY_TEST_DECLARED", " ")
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    body = "TL;DR: ordinary prose with spaces in it.\n"
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("post-comment", {"issue": "PROJ-1", "body": body})
    assert result.is_error is False, result.content
    assert _body_sent(recorder) == body, f"a sub-floor value must not rewrite the body: {_body_sent(recorder)}"
    assert _payload(result)["scrub"] == {
        "scrubbed_vars": [], "redactions": 0, "declared_absent_from_env": [],
        "declared_below_length_floor": ["SY_TEST_DECLARED"],
    }, _payload(result)


def _synthetic_session(root: Path) -> Path:
    """One two-turn main transcript for session `t1`, with no subagents. Returns its path."""
    main = root / "t1.jsonl"
    main.write_text(
        "".join(
            json.dumps(record) + "\n"
            for record in (
                {
                    "type": "assistant", "timestamp": "2026-07-09T10:00:00Z",
                    "message": {
                        "id": "m1", "model": "probe-model",
                        "content": [{"type": "text", "text": "rendered body"}],
                        "usage": {"input_tokens": 11, "output_tokens": 3},
                    },
                },
                {
                    "type": "user", "timestamp": "2026-07-09T10:00:01Z",
                    "message": {"content": [{"type": "text", "text": "thanks"}]},
                },
            )
        ),
        encoding="utf-8",
    )
    return main


@pytest.mark.anyio
async def test_usage_summarize_returns_the_roll_up_the_retired_subcommand_printed(tmp_path, monkeypatch):
    """The tool's result is the summary object itself, and `output` also writes it as JSON."""
    monkeypatch.setattr(server.usage, "LEDGER_ROOT", tmp_path / "ledger")
    main = _synthetic_session(tmp_path)
    destination = tmp_path / "usage.json"
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("usage_summarize", {
            "transcript": str(main), "phase": "ship", "task": "PROJ-1", "output": str(destination),
        })
    assert result.is_error is False, result.content
    payload = _payload(result)
    assert payload["schema"] == "shipyard.claude_usage.v1", payload
    assert payload["session_id"] == "t1", payload
    assert payload["task"] == "PROJ-1", payload
    assert payload["totals"]["input_tokens"] == 11, payload
    assert json.loads(destination.read_text(encoding="utf-8")) == payload, "the written file must match the result"


@pytest.mark.anyio
async def test_usage_summarize_refuses_when_a_required_agent_is_absent(tmp_path, monkeypatch):
    """A roll-up missing a dispatched agent's transcript under-reports, so it must fail rather than pass."""
    monkeypatch.setattr(server.usage, "LEDGER_ROOT", tmp_path / "ledger")
    main = _synthetic_session(tmp_path)
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("usage_summarize", {"transcript": str(main), "require_agent": ["slice"]})
    assert result.is_error is True, result.content
    assert "slice" in _text(result), _text(result)


@pytest.mark.anyio
async def test_export_transcript_writes_the_render_and_never_returns_its_text(tmp_path, monkeypatch):
    """The isolation the attachment flow depends on: the rendered text reaches disk and not the caller."""
    monkeypatch.setattr(server.usage, "LEDGER_ROOT", tmp_path / "ledger")
    main = _synthetic_session(tmp_path)
    destination = tmp_path / "transcript.txt"
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("export_transcript", {
            "transcript": str(main), "output": str(destination), "task": "PROJ-1",
        })
    assert result.is_error is False, result.content
    written = destination.read_text(encoding="utf-8")
    assert "MAIN SESSION t1" in written, written
    assert "rendered body" in written, written
    assert _payload(result) == {
        "path": str(destination), "bytes": len(written.encode("utf-8")), "lines": written.count("\n"),
    }, _payload(result)
    assert "rendered body" not in str(result), f"the rendered transcript reached the tool result: {result}"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("usage_summarize", {}),
        ("usage_summarize", {"session_id": "t1", "transcript": "/tmp/t1.jsonl"}),
        ("export_transcript", {"output": "/tmp/out.txt"}),
        ("export_transcript", {"session_id": "t1", "transcript": "/tmp/t1.jsonl", "output": "/tmp/out.txt"}),
    ],
    ids=["summarize-neither", "summarize-both", "export-neither", "export-both"],
)
async def test_the_transcript_tools_take_exactly_one_source(tool, arguments):
    """Neither source means nothing to read; both means the tool would silently pick one."""
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool(tool, arguments)
    assert result.is_error is True, result.content
    assert "not both and not neither" in _text(result), _text(result)
