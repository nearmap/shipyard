"""GitHub tracker adapter, spoken to only through `mcp.tracker.adapter()`.

Ports `skills/tracker/github/gh_project.py`'s `gh` transport rather than importing it: the CLI
deployment stays byte-identical, and this copy differs in two ways the server requires. It never
writes to stdout (that stream carries JSON-RPC frames, so one stray line desynchronises the
client), and a failure raises `TrackerError` instead of `SystemExit`, because this process has
other calls to serve after a bad one.

`attach-artifact` is the deliberate asymmetry `skills/tracker/github/ADAPTER.md` documents: this
tracker has no CLI-scriptable file attachment, so an artifact becomes a secret gist that a
comment on the work item links to. Privacy is verified by reading the created gist back, not
assumed from the flags passed: a public gist would publish a transcript irrevocably.

Credentials are `gh`'s own business. Nothing here reads, passes, or echoes a token, and every
message built from command output is scrubbed of any credential this process holds.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any

from ... import config
from ...secrets import discover_secret_vars, scrub_text
from .. import TrackerError

STDERR_LIMIT = 500


class GithubAdapter:
    """Canonical tracker verbs, mapped onto the `gh` CLI."""

    name = "github"

    def attach_artifact(self, issue: str, path: Path) -> dict:
        """Upload `path` as a secret gist and link it from a comment on `issue`.

        Returns the transport's own evidence: the gist URL it printed, the re-read confirmation
        that the gist is not public, and the URL of the comment that carries the link. Any step
        that produces no output, or exits non-zero, is a failure rather than a warning.
        """
        if not path.is_file():
            raise TrackerError(f"artifact not found: {path}")

        gist_url = _gh(["gist", "create", "--desc", f"shipyard artifact {issue}", str(path)])
        if not gist_url.startswith("https://"):
            raise TrackerError(
                f"gist creation returned no usable URL for {path.name}; nothing was attached to {issue}."
            )
        gist_id = gist_url.rstrip("/").rsplit("/", 1)[-1]
        if _gh_json(["api", f"gists/{gist_id}"]).get("public") is not False:
            raise TrackerError(
                f"{gist_url} is public or its visibility could not be confirmed; refusing to link it "
                f"from {issue}. Delete it: gh gist delete {gist_id}"
            )

        body = f"Shipyard artifact `{path.name}`: {gist_url}\n\nSecret gist — reachable only from this link."
        comment_url = _gh(["issue", "comment", issue, *_repo_args(), "--body", body])
        if not comment_url.startswith("https://"):
            raise TrackerError(
                f"the artifact was uploaded to {gist_url} but commenting on {issue} returned no comment "
                "URL, so the link is not discoverable from the work item."
            )
        return {
            "artifact": path.name,
            "gist_url": gist_url,
            "gist_public": False,
            "comment_url": comment_url,
        }

    def preflight(self) -> dict:
        """Confirm `gh` is installed and authenticated, reporting only non-secret facts."""
        version = _gh(["--version"]).splitlines()
        try:
            status = _gh(["auth", "status"])
        except TrackerError as exc:
            raise TrackerError(f"{exc} Authenticate with `gh auth login` (scopes: project, read:project).") from None
        account = re.search(r"account (\S+)", status)
        scopes = re.search(r"Token scopes:(.*)", status)
        return {
            "tool": "gh",
            "version": version[0] if version else "unknown",
            "authenticated": True,
            "account": account.group(1) if account else None,
            "scopes": sorted(re.findall(r"'([^']+)'", scopes.group(1))) if scopes else [],
        }


def _repo_args() -> list[str]:
    """`--repo` when configured, so a write does not depend on the server's working directory."""
    repo = config.get("tracker_config.repo", default=None)
    return ["--repo", str(repo)] if repo else []


def _gh(args: list[str]) -> str:
    """Run `gh` and return its trimmed stdout. Writes nothing to this process's stdout."""
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise TrackerError("gh is not installed or not on PATH; install the GitHub CLI.") from None
    if proc.returncode != 0:
        raise TrackerError(f"gh {' '.join(args)} failed: {_safe(proc.stderr)}")
    return proc.stdout.strip()


def _gh_json(args: list[str]) -> dict[str, Any]:
    """`_gh`, with the response parsed as a JSON object. Empty output is an empty object."""
    out = _gh(args)
    try:
        parsed = json.loads(out) if out else {}
    except json.JSONDecodeError:
        raise TrackerError(f"gh {' '.join(args)} returned output that is not JSON.") from None
    if not isinstance(parsed, dict):
        raise TrackerError(f"gh {' '.join(args)} returned {type(parsed).__name__}, expected a JSON object.")
    return parsed


def _safe(text: str) -> str:
    """Command output, with any credential this process holds redacted, ready to put in a message."""
    scrubbed, _ = scrub_text(text.strip(), discover_secret_vars())
    return scrubbed[:STDERR_LIMIT]
