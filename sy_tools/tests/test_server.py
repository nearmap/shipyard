"""The tool surface, driven through the SDK's own client rather than a hand-rolled stdio harness.

`mcp.Client(server.mcp)` is the SDK's in-memory client: a real client session over real
`initialize` / `tools/list` / `tools/call` exchanges, with the streams short-circuited instead of
crossing a pipe. That is the right level for these tests — they assert what this package decides
(which tools exist, what they are shaped like, what the gate does), and nothing about framing,
which is the SDK's to get right. `test_handshake.py` covers the wire.

The gate tests call the tool functions directly. `@mcp.tool` registers and returns the function
unchanged, so `server.attach_artifact` is still the plain coroutine function it looks like, and a
direct call keeps a gate assertion about *not doing work* readable.
"""
from __future__ import annotations

import json
from typing import Any

import anyio
import mcp
import pytest

from sy_tools import SERVER_NAME, server

TOOL_NAMES = {
    "add-dependency",
    "add-label",
    "assign",
    "attach-artifact",
    "create-issue",
    "find-issues",
    "get-issue",
    "link-parent",
    "post-comment",
    "preflight",
    "reload_config",
    "set-status",
    "update-issue",
    "validate_config",
}

WIRING = [
    ("create-issue", {"issue_type": "task", "title": "T", "body": "B", "parent": "PROJ-1"},
     "create_issue", (), {"issue_type": "task", "title": "T", "body": "B", "parent": "PROJ-1"}),
    ("get-issue", {"issue": "PROJ-2"}, "get_issue", ("PROJ-2",), {}),
    ("update-issue", {"issue": "PROJ-3", "body": "replacement"}, "update_issue", ("PROJ-3", "replacement"), {}),
    ("find-issues", {"status": "ready", "issue_type": "bug", "parent": "PROJ-4", "text": "seam", "limit": 5},
     "find_issues", (), {"status": "ready", "issue_type": "bug", "parent": "PROJ-4", "text": "seam", "limit": 5}),
    ("find-issues", {}, "find_issues", (),
     {"status": None, "issue_type": None, "parent": None, "text": None, "limit": 50}),
    ("set-status", {"issue": "PROJ-5", "status": "in-review"}, "set_status", ("PROJ-5", "in-review"), {}),
    ("assign", {"issue": "PROJ-6"}, "assign", ("PROJ-6", "@me"), {}),
    ("link-parent", {"issue": "PROJ-7", "parent": "PROJ-8"}, "link_parent", ("PROJ-7", "PROJ-8"), {}),
    ("add-dependency", {"issue": "PROJ-9", "blocked_by": "PROJ-10"},
     "add_dependency", ("PROJ-9", "PROJ-10"), {}),
    ("add-label", {"issue": "PROJ-11", "label": "needs-spec"}, "add_label", ("PROJ-11", "needs-spec"), {}),
    ("post-comment", {"issue": "PROJ-12", "body": "TL;DR: done"}, "post_comment", ("PROJ-12", "TL;DR: done"), {}),
    ("preflight", {}, "preflight", (), {}),
]
"""Every canonical-verb tool, the adapter method it must reach, and the arguments it must pass.

The whole slice is wiring, so wiring is what gets pinned: positional-versus-keyword is part of the
expectation because a verb reached with its arguments transposed would otherwise still look green.
"""


class _Recorder:
    """An adapter that records which verb was asked for instead of performing it.

    `__getattr__` rather than eleven stubs: the cost is that a tool wired to a verb no real adapter
    has would still pass here, so `test_every_adapter_implements_the_whole_protocol` in
    `sy_tools/tests/tracker/test_canonical.py` is what closes that gap.
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
        assert set(schemas["find-issues"]["properties"]) == {"status", "issue_type", "parent", "text", "limit"}

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

    A caller migrating from the shell verbs will look for all three by name, so their absence is
    part of the surface's contract rather than an omission to rediscover. `create-child` is what is
    checked positively here, because it is the only one with a distinguishable argument.
    """
    recorder = _Recorder()
    monkeypatch.setattr(server.tracker, "adapter", lambda: recorder)
    async with mcp.Client(server.mcp) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert names.isdisjoint({"create-child", "post-log", "link-pr"}), names

        child = await client.call_tool("create-issue", {"issue_type": "task", "title": "T", "parent": "PROJ-1"})
        assert child.is_error is False, child.content
        assert recorder.calls[0][2]["parent"] == "PROJ-1", "create-issue with a parent is what serves create-child"


@pytest.mark.anyio
async def test_a_tracker_failure_comes_back_as_a_tool_result(monkeypatch):
    """No per-tool try/except: the SDK already turns a raising tool into an `isError` result.

    Asserted once because the mechanism is shared by all eleven, and asserted at all because a
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
async def test_an_empty_issue_id_is_refused_before_the_tracker_is_touched(monkeypatch):
    """An empty string passes schema validation, so the guard has to be in the tool."""
    monkeypatch.setattr(server.tracker, "adapter", pytest.fail)
    async with mcp.Client(server.mcp) as client:
        result = await client.call_tool("get-issue", {"issue": ""})
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

    A stalled upload used to wedge every call queued behind it. This drives a real upload that
    blocks on an event and proves an unrelated `validate_config` completes *while it is still in
    flight* — an ordering claim, so it is asserted against a recorded sequence rather than a
    duration, which would only ever be flaky.

    The deadline is not decoration: a server that serialised calls would never reach the release,
    so without it a regression hangs the suite instead of failing it.
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
    """AM-1220 coupling: with `transcript.attach` off, nothing is read, scrubbed, or uploaded."""
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

    Ordering alone is not observable from `sanitize`'s own tests, so record both calls against one
    list: a swap of the two lines in `attach_artifact` reverses it, and a scrub that raises must
    leave the upload unreached entirely rather than merely unreported.
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
