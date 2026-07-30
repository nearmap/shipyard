# Jira attachments and transcript handling

Use for large durable artifacts, especially session transcripts. Attachments belong on the work item, not in comment bodies or PR comments.

## Render a session transcript

Render the whole transcript tree (main plus every nested subagent) from the on-disk JSONL rather than running `/export` by hand:

```bash
python ${CLAUDE_PLUGIN_ROOT}/scripts/session_usage.py export \
  --session-id "$SESSION_ID" \
  --task "$KEY" \
  --output .scratch/$KEY-$KIND-transcript-$(date -u +%Y%m%d%H%M).txt
```

`$SESSION_ID` is the current session id; `$KIND` is `ship`, `spec`, or `plan`. The renderer truncates bulky tool output and strips raw-JSONL noise, so the file stays audit-readable. Prefer running the render, scan, and upload from a delegate so the rendered text never enters the caller's context; when that delegation is denied under auto-mode, running them inline via direct Bash is a permitted fallback, provided the rendered transcript is still never read back into the caller's context. Run it as late as the session allows so the captured tail is maximal. Whether this runs at all is gated by `transcript.attach` (resolve via `python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" get transcript.attach`; see `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/config-values.md` and `docs/configuration.md`), checked by each calling skill: `/ship` additionally requires the `full` process tier on top of `transcript.attach`; `/spec` and `/plan` gate on `transcript.attach` alone, attaching to the Task and the Epic respectively.

## Secret scan before upload

1. Run deterministic scanning first — two passes, in this order, both required:

   a. Known-secret scrub. Pattern/entropy scanners only catch a secret that matches a known rule shape; a value that reached the transcript verbatim (a diagnostic `env | grep` dump, a scanner's own `-v` output echoed back into a later tool-call result) leaks regardless of shape, and reappears identically on every future re-render. Strip those first, by value rather than by pattern:

      ```bash
      python ${CLAUDE_PLUGIN_ROOT}/scripts/scrub_known_secrets.py scrub \
        .scratch/PROJ-123-ship-session.txt --require ACLI_TOKEN --report .scratch/scrub-report.json \
        --extra-words "$(python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" get redaction.extra_words)"
      ```

      This rewrites the file in place, replacing every literal occurrence of a credential-shaped env var's current value with `<REDACTED:VAR_NAME>`. It never prints or reports a value, only variable names and counts. `--require ACLI_TOKEN` turns "this process's environment doesn't actually have the token" into a loud failure instead of a silent zero-redaction success — auto-discovery alone only scrubs what's present, so a missing/rotated/unpropagated token would otherwise pass with nothing to show for it. `--extra-words` widens auto-discovery to org-specific credential-name fragments (`redaction.extra_words`, empty by default) — always pass it, even empty, so a repo that configures one is covered without a second call site to remember.

   b. gitleaks, on the now-prescrubbed file:

      ```bash
      gitleaks dir .scratch/PROJ-123-ship-session.txt --redact --report-format json --report-path .scratch/gitleaks-report.json
      ```

      Never pass `-v`/`--verbose` here — it prints matched secret values into gitleaks' own stdout, which is itself a Bash tool-call result and gets logged into the session JSONL exactly like any other secret leak, only now self-inflicted.

2. Inspect the report and the transcript for organisation-specific secrets, bearer tokens, API keys, `.env` values, credentials, private signed URLs, and contextual secrets scanners may miss.
3. Redact and rescan.
4. If safe redaction is uncertain, stop rather than publishing.

A zero-result scanner run is evidence, not proof of safety. Neither pass above is a substitute for the other: the scrub catches known values verbatim regardless of shape; gitleaks catches shapes it recognizes regardless of whether this process ever held the value.

When diagnosing tracker credentials, never dump them to inspect: `env | grep -i token`, `echo $ACLI_TOKEN`, and similar print the raw value into that command's own tool-call result, which is permanent session history from that point on — it resurfaces in every future transcript render whether or not it started life as a leak. Use a presence-only check instead, e.g. `[ -n "$ACLI_TOKEN" ]`, or the tracker's own `preflight` command, which names what's missing without ever printing a value. `scripts/secret_guard.py` (a `PreToolUse` hook on every `Bash` call) denies the dump/echo patterns outright rather than relying on this being remembered.

## Upload

Use the helper so credentials never appear in argv:

```bash
python ${CLAUDE_PLUGIN_ROOT}/skills/tracker/jira/jira_rest.py attach \
  PROJ-123 .scratch/PROJ-123-ship-session.txt
```

The helper reads `tracker_config.email` and `tracker_config.site` from resolved config and the `ACLI_TOKEN` secret from the environment. It prints created attachment metadata. Treat HTTP errors, empty results, or filename mismatch as failure.

Rules:

- name artifacts `<task>-<kind>.<ext>`;
- if site size limits reject a transcript, split into numbered parts and verify every part;
- reference attachment filename from the accounting comment;
- never claim an attachment exists without response evidence.
