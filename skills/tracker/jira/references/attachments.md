# Jira attachments and transcript handling

Use for large durable artifacts, especially session transcripts. Attachments belong on the work item, not in comment bodies or PR comments.

## Render a session transcript

Render the whole transcript tree (main plus every nested subagent) from the on-disk JSONL rather than running `/export` by hand, with the `export_transcript` tool:

```
export_transcript {"session_id": "<current session id>", "task": "<KEY>",
                   "output": "<resolved scratch dir>/<KEY>-<KIND>-transcript-<UTC yyyymmddHHMM>.txt"}
```

Give `session_id` or an explicit `transcript` path, not both and not neither. `output` is mandatory and the rendered text is never returned — that is the point: the file can be scanned, redacted and attached by path without ever entering a context. The result reports the path, byte count and line count it wrote. `<KIND>` is `ship`, `spec`, or `plan`; resolve `<resolved scratch dir>` with `scratch_dir {"identifier": "<KEY>"}` and use the `path` it reports. The renderer truncates bulky tool output and strips raw-JSONL noise, so the file stays audit-readable. Prefer rendering from a delegate so the rendered text never enters the caller's context; when that delegation is denied under auto-mode, rendering inline via direct Bash is a permitted fallback, provided the rendered transcript is still never read back into the caller's context. Run it as late as the session allows so the captured tail is maximal.

Whether an artifact is attached at all is gated by `transcript.attach` (resolve via `get_config {"key": "transcript.attach"}`; see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md` and `docs/configuration.md`). `/ship` additionally requires the `full` process tier on top of `transcript.attach`; `/spec` and `/plan` gate on `transcript.attach` alone, attaching to the Task and the Epic respectively. Resolve the gate *before* rendering — the render is the expensive part and the attach step cannot un-render a file.

## Attach

Call the `sy` server's tool whose declared name is `attach-artifact`, resolving it from the tools actually available to you rather than typing a literal identifier: the exposed name carries a deployment-dependent prefix, `mcp__plugin_sy_sy__attach-artifact` for a marketplace install and `mcp__sy__attach-artifact` where a project-level `.mcp.json` provides the server instead. Both point at this same tool; hardcoding either one breaks the other deployment.

```
attach-artifact {"issue": "PROJ-123", "path": "<resolved scratch dir>/PROJ-123-ship-transcript.txt",
                 "kind": "transcript", "caller": "ship", "process_tier": "full"}
```

`path` is the absolute path the render above actually wrote — the one `export_transcript` reported: pass that literal string, since the tool expands no substitution of its own.

One call does gate, sanitisation and upload. It re-checks the gate itself and returns a no-op skip when it is off — nothing is read, scrubbed, scanned, or uploaded — so the gate cannot be forgotten at the upload site. Pass `caller` and `process_tier` honestly: a `ship` caller without the `full` tier is skipped, and one that omits `process_tier` is skipped too, which is the safe direction to fail. Only `kind: transcript` is gated; other kinds attach unconditionally.

The tool dispatches to the configured tracker's adapter, so the caller names no tracker: Jira gets a native work-item attachment, GitHub a private gist. Credentials are read from the server's environment — never argv, never stdout. It returns whether it attached or skipped, the sanitisation report — variable names and counts, never a value, or, on a payload the scrub could not decode, the bare declaration that it was skipped (see `../../CONTRACT.md` for what the scanner still does on such a payload) — and the tracker's own response evidence.

There is no standalone scrub script to run by hand any more: both passes live inside the tools, in `sy_tools/secrets.py`, which `attach-artifact` and `attachment-update` each call (as `secrets.sanitize`) before anything is uploaded. That machinery is general-purpose and tracker-neutral rather than tracker mechanics, and nothing here changes it — what it removes is any need for a caller to remember a preparatory step, and any second, unscanned upload path: replacing an already-attached artifact goes through `attachment-update`, which runs the same gate and the same sanitisation in the same order, so there is no way to upload one that skips it undeclared.

## Why sanitisation is part of the attach

Two passes, in this order, over any payload they can read, and the tool exposes no way to run one without the other, because a half-executed recipe is exactly how an unscrubbed transcript reaches a tracker. Nothing but `allow_opaque` — a caller's declaration that the payload is not UTF-8 text, so the known-value scrub cannot act on it — gets past them; what the scanner still does and what the report carries on such a payload is stated once in `../../CONTRACT.md`. Undeclared, that payload is refused.

1. **Known-value scrub first.** Pattern/entropy scanners only catch a secret that matches a known rule shape; a value that reached the transcript verbatim (a diagnostic `env | grep` dump, a scanner's own `-v` output echoed back into a later tool-call result) leaks regardless of shape, and reappears identically on every future re-render. The scrub replaces every literal occurrence of a credential-shaped env var's current value with `<REDACTED:VAR_NAME>`, reporting names and counts and never a value. The adapter's declared secret (`ACLI_TOKEN` here) must actually resolve in the server's environment or the call fails loudly — auto-discovery alone only scrubs what is present, so a missing, rotated, or unpropagated token would otherwise pass as a clean zero-redaction success for precisely the credential it was asked to strip. `redaction.extra_words` widens discovery to org-specific credential-name fragments and is applied without a second call site to remember.
2. **Pattern scan second** (`gitleaks`), over the now-scrubbed file, always `--redact` and never `-v`/`--verbose`: verbose output prints matched secret values into the scanner's own output, which is itself session history, so it self-inflicts the exact leak the scan exists to prevent. Any surviving finding aborts the upload rather than degrading to a warning; `gitleaks` not being installed is a hard failure too, since uploading on the scrub alone silently drops half the defence.

A zero-result scan is evidence, not proof of safety. Neither pass substitutes for the other: the scrub catches known values verbatim regardless of shape; the scanner catches shapes it recognises regardless of whether this process ever held the value. Findings name a rule, not a value — inspect the file, redact contextual secrets the rules miss (organisation-specific identifiers, private signed URLs, `.env` values), and re-attach. If safe redaction is uncertain, stop rather than publishing.

When diagnosing tracker credentials, never dump them to inspect: `env | grep -i token`, `echo $ACLI_TOKEN`, and similar print the raw value into that command's own tool-call result, which is permanent session history from that point on — it resurfaces in every future transcript render whether or not it started life as a leak. Use the `check_env` MCP tool instead (`name: ACLI_TOKEN`): it reports only whether the variable is set and non-empty, never returns a value, and so has nothing to leak — a plain `[ -n "$ACLI_TOKEN" ]` is the shell equivalent if no MCP session is available. The `preflight` tool remains the check for a credential that is present but dead, and it too names what's missing without ever printing a value. `sy_tools/guards/secret_guard.py` (a `PreToolUse` hook on every `Bash` call) denies the dump/echo patterns outright rather than relying on this being remembered.

Rules:

- name artifacts `<task>-<kind>.<ext>`;
- if site size limits reject a transcript, split into numbered parts and attach every part;
- reference attachment filename from the accounting comment;
- never claim an attachment exists without response evidence — a skipped call is not an attachment, and the accounting comment must say which one happened.
