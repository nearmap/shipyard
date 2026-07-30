"""The one place that knows which concrete tracker is selected.

`skills/tracker/CONTRACT.md` states that tracker selection happens in exactly one place. This is
that place for the MCP deployment: every tool in `mcp/server.py` asks `adapter()` for a
`TrackerAdapter` and speaks only canonical verbs to it. Nothing above this module names a
tracker, and `mcp/tests/test_seam.py` fails the build if anything does.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .. import config

TIMEOUT_SECONDS = 30
"""How long any single tracker transport call may take before it is a failure.

This process is long-lived and serves calls one at a time, so an unbounded network read or a
`gh` invocation waiting on a credential prompt does not stall one tool — it wedges every tool
that follows. Every adapter transport passes this to its own timeout and turns the expiry into
a `TrackerError`.
"""


class TrackerError(RuntimeError):
    """A tracker operation failed. Raised instead of exiting: the process serves other calls."""


@runtime_checkable
class TrackerAdapter(Protocol):
    """The canonical-verb surface every adapter implements. Phase 1 covers `attach-artifact`."""

    name: str

    def attach_artifact(self, issue: str, path: Path) -> dict:
        """Upload an already-sanitised artifact to `issue` and return verified response evidence."""
        ...

    def preflight(self) -> dict:
        """Confirm credentials and account identifiers are usable, naming nothing secret."""
        ...


def adapter() -> TrackerAdapter:
    """The adapter for the configured `tracker` key. The single selection point."""
    name = str(config.get("tracker"))
    if name == "jira":
        from .jira.adapter import JiraAdapter

        return JiraAdapter()
    if name == "github":
        from .github.adapter import GithubAdapter

        return GithubAdapter()
    raise TrackerError(f"no MCP adapter for tracker {name!r}; known adapters: github, jira")


__all__ = ["TIMEOUT_SECONDS", "TrackerAdapter", "TrackerError", "adapter"]
