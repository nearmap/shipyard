# The board

`<folder>` is the board's folder from SKILL.md § The record.

## Open layout

1. Artifact `list` with `scope: types` and `type_query: "whiteboard"`. Take the Whiteboard type's `type_url`.
2. Create the board from that `type_url`. Record its URL in `notes.md`.
3. Write the title and the one-line aim at the top left.
4. Put the Record column to the right of the drawing area, top to bottom: Aim, a Decisions table (decision / choice / why / agreed), Open questions.
5. Diagrams go left of the Record column. A record never sits in the drawing area.
6. Run step 5 of Every turn, so `last/` holds the first snapshot.

## Every turn

1. Empty `<folder>/read/`. ArtifactData `list` of collection `elements`, `out_dir` `<folder>/read`, `query.limit` 1000; follow `next_cursor` until it is empty. Run `diff -r -x '.*' <folder>/last/elements <folder>/read/elements`. A file only in `last/` is a deleted element. Then ArtifactComments `read` for the board.
2. Answer every move, edit, add, delete and comment first, one line each. A section drag shows as the same offset on the section and every element in it: one move.
3. Talk: add one point.
4. Write only the elements that change, through ArtifactData `batch`, each pinned with `if_version`, as the Whiteboard type's instructions say. When a pinned write is refused, list into `read/` again, answer the new diff, and plan the write again.
5. Empty `<folder>/last/`, then list collection `elements` into `<folder>/last` the same way as step 1. An element whose version is above the one the batch returned, or that changed outside the batch, is a person's change: answer it next turn.
6. Close with the question round from SKILL.md § Talking to the user, then print `<folder>` and the board URL.
