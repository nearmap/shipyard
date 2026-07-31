"""The `sy` MCP server, built on the official `mcp` SDK's `MCPServer`.

The SDK owns the protocol: framing, `initialize`, protocol-version negotiation, `tools/list`
schema generation from these functions' type hints, `tools/call` dispatch, and the tool-failure
`isError` result. **No code in this package implements or overrides any of that** — a protocol
concern that appears here is a bug, and the version string this server speaks is deliberately not
findable in this repo.

Tools are `async` wherever they do I/O, so a slow attachment upload cannot block an unrelated tool
call. `reload_config` and `validate_config` stay synchronous: they read small local files.
`secrets.sanitize` also stays synchronous inside the async tool — it is local disk work bounded by
the artifact size, and making it awaitable would buy nothing while adding a way to interleave a
scrub with the upload it must strictly precede.

**stdout carries protocol frames and nothing else.** Anything a helper prints to stdout corrupts
the stream and desynchronises the client, which is why the ported adapter code raises instead of
printing and why every diagnostic goes to stderr.

Run it with `pixi run sy-server` (which is `python -m sy_tools.server`); `.mcp.json` registers
exactly that, and passes `--manifest-path ${CLAUDE_PLUGIN_ROOT}/pyproject.toml` because it has to.
Claude Code launches a plugin-provided stdio server with the *consumer project's* directory as its
working directory, not the plugin's, and `pixi` finds its manifest by walking up from there — so a
bare `pixi run` resolves only in this repo's own dev loop and fails in every real install. It does
interpolate `${CLAUDE_PLUGIN_ROOT}` inside the manifest's JSON strings, which is what makes the
absolute form work; `"cwd"` is not an option, because Claude Code ignores that key silently.

The manifest carries no `env` block, which is deliberate and was settled empirically rather than
from documentation: a stdio server inherits the launching process's environment, so the one real
secret (the tracker credential) arrives without ever being named in a committed file. Verified with
a discriminating control — the same `validate_config` call reports the credential present when it
is exported and missing when it is not.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from pydantic import Field

from . import SERVER_NAME, SERVER_VERSION, config, secrets, tracker

mcp = MCPServer(name=SERVER_NAME, version=SERVER_VERSION)


class ToolError(RuntimeError):
    """A tool failed in a way the caller should see as a tool result, not a protocol error."""


@mcp.tool(name="attach-artifact")
async def attach_artifact(
    issue: Annotated[str, Field(description="Canonical issue id, e.g. PROJ-123.")],
    path: Annotated[str, Field(description="Path to the artifact to attach.")],
    kind: Annotated[
        str, Field(description="Artifact kind. `transcript` is gated; anything else is ungated.")
    ] = "transcript",
    process_tier: Annotated[
        Literal["full", "light"] | None,
        Field(description="The calling workflow's process tier. `ship` requires `full`."),
    ] = None,
    caller: Annotated[
        str, Field(description="Workflow asking for the attachment, e.g. ship, spec, plan.")
    ] = "",
) -> dict[str, Any]:
    """Sanitise a local file and attach it to a tracker issue as a durable artifact.

    Canonical verb `attach-artifact`. Runs the known-value scrub and the pattern scanner, in that
    order, before anything leaves the machine. Gated by the `transcript.attach` config key; for
    `ship` callers the `full` process tier is required on top of it. When the gate is off the call
    is a no-op skip: nothing is read, scrubbed, scanned, or uploaded.
    """
    if not issue:
        raise ToolError("'issue' is required and must be a non-empty string")

    skip = _gate_skip_reason(kind, caller, process_tier)
    if skip is not None:
        return {"attached": False, "skipped": True, "reason": skip, "issue": issue}

    if not path:
        raise ToolError("'path' is required and must be a non-empty string")
    artifact = Path(path)
    if not artifact.is_file():
        raise ToolError(f"artifact not found: {artifact}")
    backend = tracker.adapter()
    required = tuple(config.adapter_map().get("secret_env", []))
    report = secrets.sanitize(artifact, require=required, extra_words=config.extra_secret_words())
    evidence = await backend.attach_artifact(issue, artifact)
    return {"attached": True, "skipped": False, "issue": issue, "sanitize": report, "evidence": evidence}


def _gate_skip_reason(kind: str, caller: str, tier: object) -> str | None:
    """Why this attachment must not happen, or None to proceed.

    Mirrors the adapter attachments reference under `skills/tracker/`: `transcript.attach` gates
    transcript attachment everywhere, and a `ship` caller additionally requires the `full`
    process tier on top of it.
    """
    if kind != "transcript":
        return None
    if not config.get("transcript.attach"):
        return "transcript.attach is false"
    if caller == "ship" and tier != "full":
        return f"ship requires the full process tier; got {tier!r}"
    return None


@mcp.tool(name="reload_config")
def reload_config() -> dict[str, Any]:
    """Re-read the Shipyard configuration layer chain from disk and replace the server's hot copy.

    Reports whether the resolved values changed; never reports a value.
    """
    return config.reload()


@mcp.tool(name="validate_config")
def validate_config() -> dict[str, Any]:
    """Report every reason the resolved configuration would be rejected.

    Covers schema violations, missing required keys, an unknown tracker, a required credential
    absent from the environment, and model-floor breaches. Side-effect-free, and never prints a
    secret value.
    """
    errors = config.validate()
    return {
        "valid": not errors,
        "errors": errors,
        "tracker": config.get("tracker"),
        "fingerprint": config.fingerprint(),
    }


if __name__ == "__main__":
    mcp.run("stdio")
