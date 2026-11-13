# The board

`<folder>` is the board's folder from SKILL.md § The record.

## Open layout

1. Artifact `list` with `scope: types` and `type_query: "whiteboard"`. Take the Whiteboard type's `type_url`. Check that ArtifactData `batch` takes `if_version`.
2. Create the board from that `type_url`. Record its URL in `notes.md`.
3. Write the title and the one-line aim at the top left.
4. Put the Record column to the right of the drawing area, top to bottom: Aim, a Decisions table (decision / choice / why / agreed), Open questions.
5. Diagrams go left of the Record column. A record never sits in the drawing area.
6. Take the first snapshot: list into `<folder>/next` as § Every turn step 1 lists into `read/`. When the listing is complete, replace `last/` with `next/`. When its lines show no `version N`, say so and stop.

## Every turn

1. Empty `<folder>/read/`. ArtifactData `list` of collection `elements`, `out_dir` `<folder>/read`, `query.limit` 1000; follow `next_cursor` until it is empty. When the list or the comment read below errors, is denied, or reports skipped docs, say which and end the turn; `last/` and `Comments seen to` stay as they are. Run `mkdir -p` on both `elements/` folders, then `diff -r -x '.*' <folder>/last/elements <folder>/read/elements`. A file only in `last/` is a deleted element. Then ArtifactComments `read` for the board, following its cursor to the end. For a thread whose comments it elides, `read` that `thread_id`; when text is still elided, say so and end the turn.
2. Answer every move, edit, add and delete in the diff, and every comment whose timestamp is after the `Comments seen to` timestamp in `notes.md`, one line each, before anything else. With no mark yet, answer every comment. Then set `Comments seen to` to the newest comment timestamp this turn's `read` showed, copied as `read` prints it. A section drag shows as the same offset on the section and every element in it: one move.
3. Talk: add one point.
4. Write only the elements that change, through ArtifactData `batch`, each pinned with `if_version`, as the Whiteboard type's instructions say. When a pinned write is refused because a document changed, list into `next/` as in step 1, answer the diff from `read/` to `next/`, replace `read/` with `next/`, and plan the write again. When a pinned batch is refused because the server does not take pinned batch writes, that is the SKILL.md § Bounds stop: say so and stop. Never drop `if_version` to get a write through.
5. List into `<folder>/next/` as in step 1. When the listing is complete, replace `last/` with `next/`. A change a person makes during the turn lands in `last/` unanswered; it stays on the board.
6. Append a dated line to the log in `notes.md`. Close with the question round from SKILL.md § Talking to the user, then print `<folder>` and the board URL.
