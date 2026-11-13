# Answering every whiteboard change exactly once

`/sy:whiteboard` does not guarantee that every board change or comment is answered exactly once. A change made during Claude's turn may go unanswered, and a comment may be answered twice.

## Why

- The tools give no reliable way to do it. ArtifactComments `read` shows no comment id and only minute timestamps, and nothing marks which writes were Claude's.
- So it needs a sync protocol run from prose. Four review rounds on AM-1859 each found a new gap in one.
- The board keeps every edit, so only Claude's one-line reply can be missed or doubled.

## Prior requests

- AM-1859: plan v2 tracked pending moves and answered comments in `notes.md`. Plan v3 removed it.
