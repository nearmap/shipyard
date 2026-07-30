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


__all__ = ["TrackerAdapter", "TrackerError", "adapter"]
