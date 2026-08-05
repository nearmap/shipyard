#!/usr/bin/env python3
"""smoke_mcp.py — end-to-end exercise of the canonical verb surface, over a real MCP connection.

WARNING: THIS CREATES REAL ISSUES (an Epic + 3 Tasks), real comments, a real attachment, and real
         board moves in whatever project `.shipyard/config.json` resolves to. Point it at a SCRATCH
         project, never a production one. It refuses to run until you set SMOKE_LIVE=1.

Tracker-agnostic by construction: it spawns the `sy` MCP server as a real subprocess, connects the
SDK's own stdio client to its pipes, and calls tools by their canonical names. Which tracker those
tools reach is the server's business, resolved from configuration — this file names none, imports no
adapter, and would smoke a tracker added after it was written. `validate_config` runs first and
reports the tracker it resolved, so the transcript still says what was exercised.

The server resolves configuration from `CLAUDE_PROJECT_DIR` when it is set, and otherwise from a `git
rev-parse` of its own working directory. Nothing sets that pointer here — this script is not launched
by Claude Code — so the cwd-derived half is the path it relies on: run it from the consuming project
and that project's board is what gets smoked. The manifest path is absolute for the opposite reason —
`pixi` finds its manifest by walking up from the working directory, which is not where this plugin
lives. The environment is forwarded whole, because the tracker credential arrives in it and the SDK's
stdio client otherwise passes on only a fixed handful of variables.

Verbs exercised: preflight, create-issue, create-child, get-issue, update-issue, find-issues,
  set-status, assign, link-parent, add-dependency, add-label, post-comment, post-log, link-pr,
  attach-artifact, type-convert, attachment-download, attachment-update.

Cleanup: created issues are left in place by default. Set SMOKE_CLEANUP=1 to move them to `done`,
which is as far as the canonical verb surface goes — it has no delete verb, so removing them is a
manual step. Nothing this run did not create is ever touched.

Commands:
  run          # SMOKE_LIVE=1 required; spawns the server and drives every verb live
  self-test    # offline: parser and scenario tool-name structure, no server, no network
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import mcp

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
OPT_IN_ENV = "SMOKE_LIVE"
CLEANUP_ENV = "SMOKE_CLEANUP"
PR_PLACEHOLDER = "https://example.invalid/repo/pull/1"
SMOKE_LABEL = "documentation"
"""A label a scratch project already has, because `add-label` deliberately does not create a missing one.

An adapter that invented one would hide a typo in a real caller, so it refuses instead — which means this
scenario has to name a label that exists rather than one that reads well."""
STATUSES = ("backlog", "ready", "in-progress", "in-review", "done")

VERB_TOOLS: dict[str, str] = {
    "validate_config": "validate_config",
    "preflight": "preflight",
    "create-issue": "create-issue",
    "create-child": "create-issue",
    "get-issue": "get-issue",
    "update-issue": "update-issue",
    "find-issues": "find-issues",
    "set-status": "set-status",
    "assign": "assign",
    "link-parent": "link-parent",
    "add-dependency": "add-dependency",
    "add-label": "add-label",
    "post-comment": "post-comment",
    "post-log": "post-comment",
    "link-pr": "post-comment",
    "attach-artifact": "attach-artifact",
    "type-convert": "type-convert",
    "attachment-download": "attachment-download",
    "attachment-update": "attachment-update",
}
"""Canonical verb -> the tool that serves it. Three verbs share a tool with another by contract."""

REQUIRED_TOOLS = frozenset(VERB_TOOLS.values())

UNEXERCISED_TOOLS = frozenset({
    "reload_config", "check_env", "get_config", "show_config", "agent_model", "scratch_dir",
    "fingerprint_config", "usage_summarize", "export_transcript",
})
"""Tools the server registers that this scenario deliberately does not call.

Named rather than implied, so the self-test can compare the two sets exactly: a tool added to the
server and smoked by nobody has to be an explicit decision, not an omission that passes.

None is a canonical contract verb, and none reaches the tracker: `reload_config` mutates the server's
hot config out from under a live scenario, `check_env` reports environment presence, and five read the
resolved configuration — so a live run has nothing to learn from any of them that `preflight` does not
already prove, and `sy_tools/tests/test_config.py` covers what they resolve against a fixture layer
chain rather than whatever the running machine happens to be configured to.

`usage_summarize` and `export_transcript` are here for a different reason: both read the caller's own
on-disk transcript tree, whose contents are whatever session happens to be running the smoke, so a
live call proves nothing repeatable and an export would write a copy of this very session to disk.
`sy_tools/tests/test_usage.py` covers both against a synthetic tree instead."""

SERVER_SOURCE = PLUGIN_ROOT / "sy_tools" / "server.py"
TOOL_REGISTRATION = re.compile(r"""@mcp\.tool\(name=["']([^"']+)["']\)""")


def _server_params() -> dict[str, Any]:
    """How to launch the real server: the way `.mcp.json` launches it, with an absolute manifest.

    The environment is passed explicitly and in full. `stdio_client` does not inherit the caller's
    when `env` is omitted — it forwards a fixed, credential-free allowlist (`DEFAULT_INHERITED_ENV_VARS`:
    HOME, LOGNAME, PATH, SHELL, TERM, USER) — so the tracker credential the caller's shell already holds
    would never reach the spawned server, and a live run would fail authentication rather than smoke
    anything. Which variable that credential is named in is the adapter's business, not this file's,
    which is why the whole environment goes rather than a list this script would have to keep current.
    """
    return {
        "command": "pixi",
        "args": ["run", "--manifest-path", str(PLUGIN_ROOT / "pyproject.toml"), "sy-server"],
        "env": dict(os.environ),
    }


def _registered_tools() -> frozenset[str]:
    """Every tool name the shipped server registers, read out of its source.

    Read, not imported: `scripts/validate.py` runs this self-test on a bare interpreter, where importing
    the server would die on the SDK it is built on before a single name could be compared. Every
    registration is a literal `@mcp.tool(name=...)` decorator, so the source is an exact index of them —
    and comparing against it is what makes a renamed or deleted tool fail here rather than at the first
    live run, which is what this offline check is for.
    """
    return frozenset(TOOL_REGISTRATION.findall(SERVER_SOURCE.read_text(encoding="utf-8")))


def _issue_id(payload: dict[str, Any] | None) -> str:
    """The opaque issue id out of a create or get payload; empty when the call failed."""
    return str((payload or {}).get("id") or "")


class Smoke:
    """One live run: drives verbs through a connected client and tallies a pass/fail line each."""

    def __init__(self, client: mcp.Client) -> None:
        self.client = client
        self.available: set[str] = set()
        self.created: list[str] = []
        self.failures: list[str] = []
        self.passed = 0

    async def discover(self) -> None:
        """List the server's tools and report up front any the scenario needs and cannot find."""
        listed = await self.client.list_tools()
        self.available = {tool.name for tool in listed.tools}
        missing = sorted(REQUIRED_TOOLS - self.available)
        print(f"==> server exposes {len(self.available)} tools")
        if missing:
            print(f"==> MISSING: {', '.join(missing)} — every verb needing one of these FAILs below")

    async def call(self, verb: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """Invoke the tool serving `verb`, print its own error text on failure, return its payload."""
        tool = VERB_TOOLS[verb]
        if tool not in self.available:
            self._fail(verb, f"the server exposes no tool named {tool!r}")
            return None
        result = await self.client.call_tool(tool, args)
        text = "".join(getattr(block, "text", "") for block in result.content)
        if result.is_error:
            self._fail(verb, text or "the tool failed and said nothing")
            return None
        try:
            payload = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            self._fail(verb, f"the tool returned a non-JSON success result: {text[:200]}")
            return None
        self._pass(verb, tool)
        return payload if isinstance(payload, dict) else {}

    def check(self, verb: str, ok: bool, detail: str) -> None:
        """Record a read-back assertion about a verb whose call already succeeded."""
        if ok:
            self._pass(verb, "read-back")
        else:
            self._fail(verb, detail)

    async def scenario(self, run_tag: str, tmp: Path) -> None:
        """Create an Epic and three child Tasks, then exercise every canonical verb against them."""
        config = await self.call("validate_config", {})
        print(f"==> tracker: {(config or {}).get('tracker', 'unresolved')} valid={(config or {}).get('valid')}")
        # `force`, because a cached success from an earlier run proves nothing about this one: a live
        # run's whole claim is that the credential works now.
        await self.call("preflight", {"force": True})

        epic = _issue_id(await self.call("create-issue", {
            "issue_type": "epic",
            "title": f"[{run_tag}] epic",
            "body": f"# {run_tag}\n\nSmoke-test roadmap epic. Safe to delete.\n",
        }))
        if not epic:
            self.failures.append("create-issue: no epic to hang the rest of the scenario on")
            return
        self.created.append(epic)

        await self.call("get-issue", {"issue": epic})
        await self.call("update-issue", {"issue": epic, "body": f"# {run_tag}\n\nUpdated roadmap body.\n"})

        found = await self.call("find-issues", {"issue_type": "epic", "text": run_tag, "limit": 20})
        if found is not None:
            ids = [str(entry.get("id")) for entry in found.get("issues", [])]
            self.check("find-issues", epic in ids, f"epic {epic} absent from {len(ids)} matching epics")

        # Two created already parented and one created unparented: `link-parent` re-parents an existing
        # issue, so aiming it at the parent an issue already has exercises nothing, and a tracker may refuse
        # it outright as a duplicate relation.
        tasks: list[str] = []
        for n in (1, 2, 3):
            task = _issue_id(await self.call("create-child" if n < 3 else "create-issue", {
                "issue_type": "task",
                "title": f"[{run_tag}] task {n}",
                "body": f"# {run_tag} task {n}\n\nChild task. Safe to delete.\n",
                **({"parent": epic} if n < 3 else {}),
            }))
            if task:
                tasks.append(task)
                self.created.append(task)
        if len(tasks) != 3:
            self.failures.append(f"create-child: produced {len(tasks)} of 3 children")
            return
        first, second, third = tasks

        if await self.call("link-parent", {"issue": third, "parent": epic}) is not None:
            read = await self.call("get-issue", {"issue": third})
            self.check(
                "link-parent",
                read is not None and bool(read.get("parent")),
                f"{third} reads back with no parent after being re-parented under {epic}",
            )

        dependency = await self.call("add-dependency", {"issue": second, "blocked_by": first})
        if dependency is not None:
            self.check(
                "add-dependency",
                bool(dependency.get("verified")),
                f"the tracker did not verify {second} as blocked by {first}",
            )

        await self.call("add-label", {"issue": first, "label": SMOKE_LABEL})
        await self.call("assign", {"issue": first, "assignee": "@me"})
        for status in STATUSES:
            await self.call("set-status", {"issue": first, "status": status})
        await self.call("type-convert", {"issue": third, "issue_type": "bug"})

        await self._attachments(run_tag, tmp, first)

        await self.call("post-comment", {"issue": first, "body": f"TL;DR: smoke run {run_tag} drove the verb set.\n"})
        # A real `shipyard.ship_metrics.v1` body, not a stand-in: `post-comment` schema-validates any
        # block claiming that id, so an approximate one exercises the rejection path rather than the verb.
        log = json.dumps({"schema": "shipyard.ship_metrics.v1", "task": first, "pr_url": PR_PLACEHOLDER}, indent=2)
        await self.call("post-log", {"issue": first, "body": f"# Claude Code ship metrics\n\n```json\n{log}\n```\n"})
        await self.call("link-pr", {"issue": epic, "body": f"Delivery PR for {run_tag}: {PR_PLACEHOLDER}\n"})

    async def _attachments(self, run_tag: str, tmp: Path, issue: str) -> None:
        """Attach a scrubbed artifact, then round-trip it through download and update."""
        artifact = tmp / f"{run_tag}-transcript.txt"
        artifact.write_text(f"shipyard smoke transcript for {run_tag}\nno secrets here.\n", encoding="utf-8")
        attached = await self.call("attach-artifact", {
            "issue": issue, "path": str(artifact), "kind": "report", "caller": "smoke",
        })
        if attached is not None:
            self.check("attach-artifact", bool(attached.get("attached")), f"not attached: {attached}")

        # Compared by content, not by existence: a download that wrote the wrong bytes passed an
        # existence check as a success once already, and on disk the two are indistinguishable.
        sent = artifact.read_text(encoding="utf-8")
        roundtrip = tmp / "roundtrip.txt"
        await self.call("attachment-download", {
            "issue": issue, "filename_or_id": artifact.name, "output_path": str(roundtrip),
        })
        landed = roundtrip.read_text(encoding="utf-8") if roundtrip.is_file() else ""
        self.check(
            "attachment-download",
            landed.strip() == sent.strip(),
            f"what came back is not what was attached: sent {sent.strip()!r}, got {landed.strip()!r}",
        )

        # `kind` and `caller` mirror the attach above for the same reason: `attachment-update` defaults
        # to the gated `transcript` kind, and a gated call returns `{"updated": false, "skipped": true}`,
        # which is a successful tool call that replaced nothing. Un-gated and read back, or this verb is
        # counted as exercised by a skip.
        revised = f"shipyard smoke transcript for {run_tag}\nrevised, still no secrets.\n"
        artifact.write_text(revised, encoding="utf-8")
        updated = await self.call("attachment-update", {
            "issue": issue, "path": str(artifact), "kind": "report", "caller": "smoke",
        })
        if updated is not None:
            replaced = await self._read_back(issue, artifact.name, tmp / "replaced.txt")
            self.check(
                "attachment-update",
                bool(updated.get("updated")) and replaced is not None and replaced.strip() == revised.strip(),
                f"the replacement did not land: sent {revised.strip()!r}, the issue holds {replaced!r} ({updated})",
            )

    async def _read_back(self, issue: str, filename: str, destination: Path) -> str | None:
        """Download `filename` off `issue` without tallying a verb: None when the tracker refuses.

        Untallied deliberately: this is `attachment-update`'s own evidence that the replacement landed,
        not a second, independently-scored `attachment-download` — routing it through `call` would
        double-count one download as two verbs exercised.
        """
        result = await self.client.call_tool(VERB_TOOLS["attachment-download"], {
            "issue": issue, "filename_or_id": filename, "output_path": str(destination),
        })
        if result.is_error:
            return None
        return destination.read_text(encoding="utf-8") if destination.is_file() else ""

    async def tidy(self) -> None:
        """Move the issues this run created — and only those — to `done`."""
        print("\n==> cleanup: moving created issues to done")
        for issue in self.created:
            await self.call("set-status", {"issue": issue, "status": "done"})

    def report(self) -> int:
        """Print the summary and return the process exit code."""
        print("\n==> SUMMARY")
        print(f"  created: {', '.join(self.created) or '(nothing)'}")
        print(f"  verbs:   {self.passed} passed, {len(self.failures)} failed")
        for failure in self.failures:
            print(f"  FAIL {failure}")
        if not self.failures:
            print("All verbs exercised.")
        if os.environ.get(CLEANUP_ENV) != "1":
            print(f"  NOTE: created issues left in place. Re-run with {CLEANUP_ENV}=1 to move them to done;")
            print("        the canonical verb surface has no delete verb, so removal stays manual.")
        return 1 if self.failures else 0

    def _pass(self, verb: str, detail: str) -> None:
        """Count and print one passing verb."""
        self.passed += 1
        print(f"PASS  {verb:<22} {detail}")

    def _fail(self, verb: str, detail: str) -> None:
        """Count and print one failing verb, carrying the failure's own text."""
        self.failures.append(f"{verb}: {detail}")
        print(f"FAIL  {verb:<22} {detail}")


async def run_live() -> int:
    """Connect to a freshly spawned server, run the scenario, optionally tidy, and report."""
    # Imported here, not at module scope: `self-test` has to pass on a bare interpreter, which is how
    # scripts/validate.py invokes it, while the SDK exists only inside this repo's pixi environment.
    import mcp
    from mcp import StdioServerParameters, stdio_client

    run_tag = f"sy-smoke-{int(time.time())}-{os.getpid()}"
    with tempfile.TemporaryDirectory() as raw_tmp:
        async with mcp.Client(stdio_client(StdioServerParameters(**_server_params()))) as client:
            smoke = Smoke(client)
            print(f"==> run tag {run_tag}")
            await smoke.discover()
            await smoke.scenario(run_tag, Path(raw_tmp))
            if os.environ.get(CLEANUP_ENV) == "1":
                await smoke.tidy()
    return smoke.report()


def _self_test() -> None:
    """Offline structure check: the parser, and the verb-to-tool map the scenario calls through."""
    parser = _build_parser()
    assert parser.parse_args(["run"]).command == "run"
    assert parser.parse_args(["self-test"]).command == "self-test"

    params = _server_params()
    args = params["args"]
    assert args[1:3] == ["--manifest-path", str(PLUGIN_ROOT / "pyproject.toml")], args
    assert Path(args[2]).is_absolute(), "the manifest must be resolved from this file, never from cwd"
    assert params["env"] == dict(os.environ), "the server must inherit the credentials the caller holds"

    registered = _registered_tools()
    assert registered, f"no @mcp.tool registration was found in {SERVER_SOURCE}; the scan is broken"
    assert registered == REQUIRED_TOOLS | UNEXERCISED_TOOLS, (
        "the scenario and the server disagree about the tool surface. Only the scenario names: "
        f"{sorted(REQUIRED_TOOLS - registered)}; only the server registers: "
        f"{sorted(registered - REQUIRED_TOOLS - UNEXERCISED_TOOLS)}"
    )
    assert len(REQUIRED_TOOLS) == 16, sorted(REQUIRED_TOOLS)
    assert all(tool and tool == tool.strip() for tool in REQUIRED_TOOLS), sorted(REQUIRED_TOOLS)
    assert VERB_TOOLS["create-child"] == VERB_TOOLS["create-issue"], "a child is the create-issue write"
    assert VERB_TOOLS["post-log"] == VERB_TOOLS["post-comment"], "a machine log is a comment"
    assert VERB_TOOLS["link-pr"] == VERB_TOOLS["post-comment"], "a PR link's durable half is a comment"
    for verb in ("type-convert", "attachment-download", "attachment-update"):
        assert VERB_TOOLS[verb] == verb, f"{verb} has a tool of its own"


def _build_parser() -> argparse.ArgumentParser:
    """The two-subcommand CLI: a live run, or an offline self-test."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help=f"drive every canonical verb against the configured project ({OPT_IN_ENV}=1)")
    sub.add_parser("self-test", help="check this script's own structure offline")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch the CLI, refusing a live run without the explicit opt-in."""
    args = _build_parser().parse_args(argv)
    if args.command == "self-test":
        _self_test()
        print("smoke_mcp self-test passed")
        return 0
    if os.environ.get(OPT_IN_ENV) != "1":
        raise SystemExit(
            f"smoke_mcp: refusing to run. This writes REAL issues, comments and attachments to the "
            f"configured project. Set {OPT_IN_ENV}=1 to confirm it is a scratch one."
        )
    return asyncio.run(run_live())


if __name__ == "__main__":
    raise SystemExit(main())
