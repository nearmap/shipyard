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

The working directory is inherited deliberately: the server resolves configuration relative to where
it was launched, so running this from a consuming project smokes that project's board. The manifest
path is absolute for the opposite reason — `pixi` finds its manifest by walking up from the working
directory, which is not where this plugin lives.

Verbs exercised: preflight, create-issue, create-child, get-issue, update-issue, find-issues,
  set-status, assign, link-parent, add-dependency, add-label, post-comment, post-log, link-pr,
  attach-artifact, type-convert, attachment-download, attachment-update, attachment-delete.

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
import tempfile
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import mcp

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
OPT_IN_ENV = "SMOKE_LIVE"
CLEANUP_ENV = "SMOKE_CLEANUP"
PR_PLACEHOLDER = "https://example.invalid/repo/pull/1"
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
    "attachment-delete": "attachment-delete",
}
"""Canonical verb -> the tool that serves it. Three verbs share a tool with another by contract."""

REQUIRED_TOOLS = frozenset(VERB_TOOLS.values())


def _server_params() -> dict[str, Any]:
    """How to launch the real server: the way `.mcp.json` launches it, with an absolute manifest."""
    return {
        "command": "pixi",
        "args": ["run", "--manifest-path", str(PLUGIN_ROOT / "pyproject.toml"), "sy-server"],
    }


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
        await self.call("preflight", {})

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

        tasks: list[str] = []
        for n in (1, 2, 3):
            task = _issue_id(await self.call("create-child", {
                "issue_type": "task",
                "title": f"[{run_tag}] task {n}",
                "body": f"# {run_tag} task {n}\n\nChild task. Safe to delete.\n",
                "parent": epic,
            }))
            if task:
                tasks.append(task)
                self.created.append(task)
        if len(tasks) != 3:
            self.failures.append(f"create-child: produced {len(tasks)} of 3 children")
            return
        first, second, third = tasks

        await self.call("link-parent", {"issue": third, "parent": epic})

        dependency = await self.call("add-dependency", {"issue": second, "blocked_by": first})
        if dependency is not None:
            self.check(
                "add-dependency",
                bool(dependency.get("verified")),
                f"the tracker did not verify {second} as blocked by {first}",
            )

        await self.call("add-label", {"issue": first, "label": "decomposed"})
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
        """Attach a scrubbed artifact, then round-trip it through download, update and delete."""
        artifact = tmp / f"{run_tag}-transcript.txt"
        artifact.write_text(f"shipyard smoke transcript for {run_tag}\nno secrets here.\n", encoding="utf-8")
        attached = await self.call("attach-artifact", {
            "issue": issue, "path": str(artifact), "kind": "report", "caller": "smoke",
        })
        if attached is not None:
            self.check("attach-artifact", bool(attached.get("attached")), f"not attached: {attached}")

        roundtrip = tmp / "roundtrip.txt"
        await self.call("attachment-download", {
            "issue": issue, "filename_or_id": artifact.name, "output_path": str(roundtrip),
        })
        self.check("attachment-download", roundtrip.is_file(), f"nothing landed at {roundtrip}")

        artifact.write_text(f"shipyard smoke transcript for {run_tag}\nrevised, still no secrets.\n", encoding="utf-8")
        await self.call("attachment-update", {"issue": issue, "path": str(artifact)})
        await self.call("attachment-delete", {"issue": issue, "filename_or_id": artifact.name})

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

    args = _server_params()["args"]
    assert args[1:3] == ["--manifest-path", str(PLUGIN_ROOT / "pyproject.toml")], args
    assert Path(args[2]).is_absolute(), "the manifest must be resolved from this file, never from cwd"

    assert len(REQUIRED_TOOLS) == 17, sorted(REQUIRED_TOOLS)
    assert all(tool and tool == tool.strip() for tool in REQUIRED_TOOLS), sorted(REQUIRED_TOOLS)
    assert VERB_TOOLS["create-child"] == VERB_TOOLS["create-issue"], "a child is the create-issue write"
    assert VERB_TOOLS["post-log"] == VERB_TOOLS["post-comment"], "a machine log is a comment"
    assert VERB_TOOLS["link-pr"] == VERB_TOOLS["post-comment"], "a PR link's durable half is a comment"
    for verb in ("type-convert", "attachment-download", "attachment-update", "attachment-delete"):
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
