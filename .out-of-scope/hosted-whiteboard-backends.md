# A hosted whiteboard as `/sy:whiteboard`'s board

The board is a local Excalidraw file, not a Miro, tldraw or other hosted board. The decision page is a local file too by default; the skill offers a private hosted page only when the session has a tool that publishes one and the user picks it. The board stays a local file either way.

## Why

- Shipyard is public and runs anywhere Claude Code does. A hosted board needs an account, a network service and per-user setup before the first turn, and puts the board outside the repository's own trust boundary.
- tldraw's SDK needs a license key in production; its free hobby key shows a watermark and is granted case by case, so every user would need their own key.
- Excalidraw is MIT, its scene is plain JSON, and its editors (the VS Code extension, excalidraw.com) keep element ids and frame membership through a save, which is what lets `board_read` turn a drag into a diff.
- JSON Canvas is an equally open format, but Obsidian is its only full editor.

A hosted board could come back as an optional adapter behind `board_write`/`board_read`, the way trackers sit behind the tracker contract, if a team asks for one; the file stays the default.

## Prior requests

- AM-1808: raised while porting `/sy:whiteboard` into Shipyard; the local file was kept.
