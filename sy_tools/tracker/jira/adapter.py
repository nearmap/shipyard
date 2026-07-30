"""Jira REST, spoken by a long-lived server process.

Ported from `skills/tracker/jira/jira_rest.py` rather than imported from it: that script is a
CLI, so it signals with `print()` and `raise SystemExit`. Inside the MCP server stdout carries
JSON-RPC frames — a stray print desynchronises the client — and a `SystemExit` would end a
process that still has other calls to serve. Everything here returns a dict or raises
`TrackerError`, and nothing in this module writes to stdout. The shipped script stays untouched
so the CLI deployment keeps working unchanged.

The transport is async httpx2, and the canonical verbs are `async` because the server serves calls
concurrently: an upload waiting on Jira must not block an unrelated tool call. Only the transport
awaits — the multipart body is assembled synchronously, byte-for-byte, because Jira validates the
hand-built boundary.

Account identifiers come from resolved config; the credential is read from the environment only,
is put into the `Authorization` header and nowhere else, and never appears in a URL, in a
returned dict, or in an exception message.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
import secrets

import httpx2

from ... import config
from .. import TIMEOUT_SECONDS, TrackerError

TOKEN_ENV = "ACLI_TOKEN"
API = "/rest/api/3"


class JiraAdapter:
    """The canonical tracker verbs, implemented against the Jira Cloud REST API."""

    name = "jira"

    async def attach_artifact(self, issue: str, path: Path) -> dict:
        """Upload `path` to `issue` and return the response evidence confirming the write."""
        if not path.is_file():
            raise TrackerError(f"attachment not found: {path}")
        base, auth = _credentials()
        boundary = "----shipyard-" + secrets.token_hex(16)
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload = b"".join([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ])
        _, result = await request(
            "POST",
            f"{base}{API}/issue/{issue}/attachments",
            auth,
            payload,
            {
                "X-Atlassian-Token": "no-check",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        confirmed = _confirmation(result, path.name)
        if confirmed is None:
            raise TrackerError(
                f"upload response did not confirm {path.name!r} on {issue}; treat the attachment as failed"
            )
        return {
            "issue": issue,
            "filename": path.name,
            "id": str(confirmed.get("id", "")),
            "size": confirmed.get("size"),
            "created": confirmed.get("created"),
        }

    async def preflight(self) -> dict:
        """Prove the configured account and its credential authenticate, reporting no secret value.

        A credential can be present and still be dead, so this is a real authenticated read
        rather than a presence check. `myself` is the cheapest one Jira offers.
        """
        base, auth = _credentials()
        _, item = await request("GET", f"{base}{API}/myself", auth)
        account_id = item.get("accountId") if isinstance(item, dict) else None
        if not account_id:
            raise TrackerError(
                f"preflight read of {base}{API}/myself returned no account; the credential in "
                f"{TOKEN_ENV} may be revoked or belong to another site"
            )
        return {"ok": True, "site": base, "account_id": str(account_id)}


async def request(
    method: str,
    url: str,
    auth: str,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> tuple[int, object]:
    """One authenticated REST call, returning `(status, parsed body or None)`.

    The timeout is not optional. Being async no longer wedges the whole server, but an unbounded
    call still leaves its own caller awaiting forever while holding a connection, and the tool that
    was asked to attach a transcript never answers. Handing it to the client bounds every phase —
    connect, write, read and pool — not just the request write.

    The two `except` clauses are ordered, not interchangeable: `TimeoutException` is a subclass of
    `RequestError`, so catching the family first would rename every stall an unreachable host and
    lose the one distinction that decides whether retrying is worth anything.

    `data` is sent as a raw body (`content=`), never form-encoded: the multipart payload carries a
    hand-built boundary that must reach Jira byte-for-byte. `transport` is the seam a test uses to
    drive this mapping without a network; production callers leave it None.
    """
    sent = {"Authorization": auth, "Accept": "application/json"}
    sent.update(headers or {})
    try:
        async with httpx2.AsyncClient(timeout=TIMEOUT_SECONDS, transport=transport) as client:
            resp = await client.request(method, url, content=data, headers=sent)
            if not resp.is_success:
                raise TrackerError(f"HTTP {resp.status_code} from {method} {url}: {resp.text[:2000]}")
            return resp.status_code, json.loads(resp.content) if resp.content else None
    except httpx2.TimeoutException as exc:
        raise TrackerError(f"{method} {url} timed out after {TIMEOUT_SECONDS}s") from exc
    except httpx2.RequestError as exc:
        raise TrackerError(f"could not reach {url}: {exc}") from exc


def _credentials() -> tuple[str, str]:
    """Base URL and the `Authorization` header value: config identifiers plus the env-held secret."""
    email = _identifier("tracker_config.email")
    site = _identifier("tracker_config.site")
    token = os.environ.get(TOKEN_ENV)
    if not email:
        raise TrackerError("tracker_config.email is unset; set it in .shipyard/config.json")
    if not site:
        raise TrackerError(
            "tracker_config.site is unset; set it in .shipyard/config.json, e.g. yourorg.atlassian.net"
        )
    if not token:
        raise TrackerError(
            f"{TOKEN_ENV} is unset in the environment; it is a secret and never lives in a config file"
        )
    base = site if site.startswith("http") else f"https://{site}"
    return base.rstrip("/"), "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


def _identifier(key: str) -> str:
    """One non-secret config identifier, as a string, empty when absent."""
    try:
        value = config.get(key, default="")
    except config.ConfigError as exc:
        raise TrackerError(f"{key} could not be resolved: {exc}") from exc
    return str(value or "")


def _confirmation(result: object, filename: str) -> dict | None:
    """The uploaded attachment Jira echoes back for `filename`, or None when it confirmed nothing."""
    if not isinstance(result, list):
        return None
    return next((x for x in result if isinstance(x, dict) and x.get("filename") == filename), None)
