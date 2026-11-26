"""Post a pull-request comment whose last line names the Claude Code session that wrote it.

The marker is `<!-- shipyard:agent session=<uuid> -->`, a hidden HTML comment appended here after a
blank line, so it renders as nothing and a reader anchors on the body's final line with `MARKER_RE`.
This module writes it and a caller never does: a body already carrying one is refused.

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
_MAY_BE_POSTED = (
    "; the comment may already be posted, so look for this session's shipyard:agent marker on the pull request "
    "before posting again"
)


class CommentError(Exception):
    """A pull-request comment was refused before posting, or the post itself failed or did not verify."""


def post(pr_url: str, body: str, session: str, reply_to: int | None = None) -> dict[str, Any]:
    """Post `body` plus the session marker on the pull request at `pr_url`, and verify what was stored.

    `pr_url` is `https://<host>/<owner>/<repo>/pull/<n>`; anything else is refused. `session` must be a
    lowercase UUID. With `reply_to`, the comment is a reply in the thread of that review comment id, a
    reply's id included; without it, a top-level comment. Returns `{url, id, session}` for the comment
    created. Raises `CommentError` on a refused argument, a `pr_url` whose number is not a pull request, a
    missing or failing `gh`, or a stored body that differs from the one sent.
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
    host, owner, repo, number = match["host"], match["owner"], match["repo"], match["n"]
    if reply_to is None:
        _gh(host, f"repos/{owner}/{repo}/pulls/{number}", None)
        endpoint = f"repos/{owner}/{repo}/issues/{number}/comments"
    else:
        replied = _gh(host, f"repos/{owner}/{repo}/pulls/comments/{reply_to}", None)
        if not str(replied.get("pull_request_url", "")).endswith(f"/pulls/{number}"):
            raise CommentError(
                f"review comment {reply_to} is not on pull request {number}; take reply_to from this pull request's"
                " review comments"
            )
        thread = replied.get("in_reply_to_id") or reply_to
        endpoint = f"repos/{owner}/{repo}/pulls/{number}/comments/{thread}/replies"
    created = _gh(host, endpoint, {"body": full})
    if created.get("body") != full:
        raise CommentError(
            f"the comment at {created.get('html_url')!r} was stored with a body that differs from the one sent; "
            "correct it with a new comment rather than trusting it"
        )
    return {"url": created["html_url"], "id": created["id"], "session": session}


def _gh(host: str, endpoint: str, payload: dict[str, str] | None) -> dict[str, Any]:
    root = config.resolved_root()
    if not root.is_dir():
        raise CommentError(f"the repository the configuration resolved against, {root}, is not an existing directory")
    if payload is None:
        args, sent, stdin, may_be_posted = ["gh", "api", "--hostname", host, endpoint], None, subprocess.DEVNULL, ""
    else:
        args = ["gh", "api", "--hostname", host, "--method", "POST", endpoint, "--input", "-"]
        sent, stdin, may_be_posted = json.dumps(payload), None, _MAY_BE_POSTED
    try:
        # `input=` or `stdin=DEVNULL` keeps the child off the server's stdin, so it never reads JSON-RPC frames.
        proc = subprocess.run(
            args, input=sent, stdin=stdin, capture_output=True, text=True, check=False, timeout=TIMEOUT_SECONDS,
            cwd=root,
        )
    except FileNotFoundError:
        raise CommentError("gh is not installed or not on PATH; install the code host's CLI") from None
    except subprocess.TimeoutExpired:
        raise CommentError(
            f"gh api {endpoint} did not finish within {TIMEOUT_SECONDS}s and was killed{may_be_posted}"
        ) from None
    if proc.returncode != 0:
        raise CommentError(
            f"gh api {endpoint} failed (exit {proc.returncode}): {_scrubbed(proc.stderr.strip())}{may_be_posted}"
        )
    try:
        got = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise CommentError(
            f"gh api {endpoint} returned non-JSON output: {_scrubbed(proc.stdout)[:200]}{may_be_posted}"
        ) from None
    if not isinstance(got, dict) or "id" not in got or "html_url" not in got:
        raise CommentError(
            f"gh api {endpoint} returned no object with id and html_url: {_scrubbed(str(got))[:200]}"
        )
    return got


def _scrubbed(text: str) -> str:
    known = secrets.discover_secret_vars(extra_words=config.extra_secret_words())
    return secrets.scrub_text(text, known)[0]
