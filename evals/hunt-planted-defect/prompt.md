---
max_turns: 30
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Agent]
---
Dispatch the `sy:hunt` agent in hunt mode over the change in `CHANGE.diff` (the diff from `HEAD~1` to `HEAD` in this repository). Scope: every file that diff touches. Then report its findings verbatim, each with file and line.
