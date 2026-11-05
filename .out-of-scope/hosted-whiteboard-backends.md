# A hosted whiteboard as `/sy:whiteboard`'s board

The board is a local Excalidraw file, not a Miro, tldraw or other hosted board.

- The board is also editable in a local browser editor on 127.0.0.1, served by `page_serve`. It loads Excalidraw from a pinned CDN and saves to the same local file.
- The decision page is a local file, served on 127.0.0.1.
- When the session has the Artifact tools and the user picks it, the page is also published as a hosted page with a comment store for colleagues. Claude only reads that store.
- A hosted board is still declined.

## Why

- Shipyard is public and runs anywhere Claude Code does. A hosted board needs an account, a network service and per-user setup before the first turn, and puts the board outside the repository's own trust boundary.
- tldraw's SDK needs a license key in production; its free hobby key shows a watermark and is granted case by case, so every user would need their own key.
- Excalidraw is MIT, its scene is plain JSON, and its editors (the VS Code extension, excalidraw.com) keep element ids and frame membership through a save, which is what lets `board_read` turn a drag into a diff.
- JSON Canvas is an equally open format, but Obsidian is its only full editor.
- The local editor and the loopback server add no account and no service: the file stays the record, and `board_read` reads an editor save the same way from every editor.
- A hosted page takes comments, not edits, so a colleague's view never reaches the board or settles a decision.

A hosted board could come back as an optional adapter behind `board_write`/`board_read`, the way trackers sit behind the tracker contract, if a team asks for one; the file stays the default.

## Prior requests

- AM-1808: raised while porting `/sy:whiteboard` into Shipyard; the local file was kept. Plan v4 added the local browser editor and the opt-in hosted page with comments.
