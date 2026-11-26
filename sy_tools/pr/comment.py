"""Post a pull-request comment whose last line names the Claude Code session that wrote it.

The marker is `<!-- shipyard:agent session=<uuid> -->`, a hidden HTML comment appended here after a
blank line, so it renders as nothing and a reader anchors on the body's final line with `MARKER_RE`.
This module writes it and a caller never does: a body already carrying one is refused, so a marker
on a posted comment was always stamped by this code from the session it was handed.

The session is an argument, never this process's environment: the server this runs in outlives a
`/clear`, so its own environment can name a session the caller has already left. A caller passes the
value its own Bash tool prints for `$CLAUDE_CODE_SESSION_ID`, and the `pr_session_guard` hook checks it
against the live session before the call arrives here.

Every post is read back: the body the code host stored must equal the body sent, or this raises.
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from .. import config, secrets

MARKER_RE = re.compile(
    r"^<!-- shipyard:agent session=(?P<session>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}) -->\Z",
    re.M,
)
"""Matches the marker only as the body's last line; `MARKER_RE.search(body)['session']` is the writer's session."""

TIMEOUT_SECONDS = 30

_PR_URL = re.compile(r"^https://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<n>\d+)/?$")
_SESSION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ANY_MARKER = re.compile(r"<!--\s*shipyard:agent\b", re.I)


class CommentError(Exception):
    """A pull-request comment was refused before posting, or the post itself failed or did not verify."""


def post(pr_url: str, body: str, session: str, reply_to: int | None = None) -> dict[str, Any]:
    """Post `body` plus the session marker on the pull request at `pr_url`, and verify what was stored.

    `pr_url` is `https://<host>/<owner>/<repo>/pull/<n>`; anything else is refused. `session` must be a
    lowercase UUID. With `reply_to`, the comment is a reply to that review comment id; without it, a
    top-level comment. Returns `{url, id, session}` for the comment created. Raises `CommentError` on a
    refused argument, a missing or failing `gh`, or a stored body that differs from the one sent.
    """
    match = _PR_URL.match(pr_url)
    if match is None:
        raise CommentError(f"{pr_url!r} is not a pull request URL: https://<host>/<owner>/<repo>/pull/<n>")
    if not _SESSION.fullmatch(session):
        raise CommentError(
            f"session {session!r} is not a lowercase UUID; pass what `echo \"$CLAUDE_CODE_SESSION_ID\"` prints"
        )
    if _ANY_MARKER.search(body):
        raise CommentError("the body already carries a shipyard:agent marker; this tool writes it, never the caller")
    if not body.strip():
        raise CommentError("the body is empty")
    if reply_to is not None and reply_to < 1:
        raise CommentError(f"reply_to {reply_to!r} is not a review comment id")
    full = f"{body.rstrip()}\n\n<!-- shipyard:agent session={session} -->"
    owner, repo, number = match["owner"], match["repo"], match["n"]
    endpoint = (
        f"repos/{owner}/{repo}/issues/{number}/comments"
        if reply_to is None
        else f"repos/{owner}/{repo}/pulls/{number}/comments/{reply_to}/replies"
    )
    created = _gh_post(match["host"], endpoint, {"body": full})
    if created.get("body") != full:
        raise CommentError(
            f"the comment at {created.get('html_url')!r} was stored with a body that differs from the one sent; "
            "correct it with a new comment rather than trusting it"
        )
    return {"url": created["html_url"], "id": created["id"], "session": session}


def _gh_post(host: str, endpoint: str, payload: dict[str, str]) -> dict[str, Any]:
    root = config.resolved_root()
    if not root.is_dir():
        raise CommentError(f"the repository the configuration resolved against, {root}, is not an existing directory")
    args = ["gh", "api", "--hostname", host, "--method", "POST", endpoint, "--input", "-"]
    try:
        # `input=` gives the child its own stdin pipe, so it never reads the server's JSON-RPC frames.
        proc = subprocess.run(
            args, input=json.dumps(payload), capture_output=True, text=True, check=False,
            timeout=TIMEOUT_SECONDS, cwd=root,
        )
    except FileNotFoundError:
        raise CommentError("gh is not installed or not on PATH; install the code host's CLI") from None
    except subprocess.TimeoutExpired:
        raise CommentError(f"gh api {endpoint} did not finish within {TIMEOUT_SECONDS}s and was killed") from None
    if proc.returncode != 0:
        raise CommentError(f"gh api {endpoint} failed (exit {proc.returncode}): {_scrubbed(proc.stderr.strip())}")
    try:
        created = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise CommentError(f"gh api {endpoint} returned non-JSON output: {_scrubbed(proc.stdout[:200])}") from None
    if not isinstance(created, dict) or "id" not in created or "html_url" not in created:
        raise CommentError(f"gh api {endpoint} returned no created comment: {_scrubbed(str(created)[:200])}")
    return created


def _scrubbed(text: str) -> str:
    known = secrets.discover_secret_vars(extra_words=config.extra_secret_words())
    return secrets.scrub_text(text, known)[0]
