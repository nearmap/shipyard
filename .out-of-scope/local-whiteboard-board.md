# A local or offline board for `/sy:whiteboard`

`/sy:whiteboard` works on one Claude whiteboard. A local file, a local editor or an offline board is declined.

## Why

- One shared surface: the user, colleagues and Claude see and edit the same board, with no second copy to keep in step.
- Colleagues co-edit and comment on the board itself, with no port forward, token or file hand-off.
- Less code: no editor, page server, scene format or layout engine to keep in this repository.
- The board needs a claude.ai login with the Artifact tools and Artifact types, so a session without them cannot run `/sy:whiteboard` and the skill stops. AM-1808 declined a hosted board because it needed an account. That cost is now accepted for one shared surface.

## Prior requests

- AM-1808: chose a local Excalidraw file with a loopback browser editor and an opt-in hosted page for comments.
- AM-1859: reversed it. The local board, page and their `sy` tools were deleted for the Claude whiteboard.
