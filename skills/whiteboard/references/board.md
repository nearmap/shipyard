# The board

`<folder>` is the board's folder from SKILL.md § The record.

## Open layout

1. Artifact `list` with `scope: types` and `type_query: "whiteboard"`. Take the Whiteboard type's `type_url`. Check that ArtifactData `batch` takes `if_version`.
2. Create the board from that `type_url`. Record its URL in `notes.md`.
3. Write the title and the one-line aim at the top left.
4. Put the Record column to the right of the drawing area, top to bottom: Aim, a Decisions table (decision / choice / why / agreed), Open questions.
5. Diagrams go left of the Record column. A record never sits in the drawing area.
6. List collection `elements` into `<folder>/last` the way Every turn step 1 lists into `read/`, so `last/` holds the first snapshot. An element the step 3-5 writes did not create, or one listed at a version above what its write returned, is a person's move: add it under `Pending moves` in `notes.md`. When its lines show no `version N`, say so and stop.

## Every turn

1. Empty `<folder>/read/`. ArtifactData `list` of collection `elements`, `out_dir` `<folder>/read`, `query.limit` 1000; follow `next_cursor` until it is empty. When a list or comment read errors, is denied, or reports skipped docs, say which and end the turn, leaving `last/` as it is. Run `mkdir -p` on both `elements/` folders before the diff: a page with no docs writes none. Run `diff -r -x '.*' <folder>/last/elements <folder>/read/elements`. A file only in `last/` is a deleted element. Then ArtifactComments `read` for the board, following its cursor to the end, skipping comments `notes.md` lists as answered, and take the `Pending moves` lines from `notes.md`.
2. Answer every move, edit, add, delete, comment and pending move first, one line each, then clear `Pending moves` and add the answered comment ids to `notes.md`. A section drag shows as the same offset on the section and every element in it: one move.
3. Talk: add one point.
4. Write only the elements that change, through ArtifactData `batch`, each pinned with `if_version`, as the Whiteboard type's instructions say. When a pinned write is refused, list into `read/` again, answer the new diff, and plan the write again.
5. List into `<folder>/next/` as in step 1 and run `diff -r -x '.*' <folder>/read/elements <folder>/next/elements`. A person's move made during the turn is a difference on an element outside the step-4 batch, or a batch element whose version in this `list` result is above the version the batch returned (the saved files hold no version). Append one line for each under `Pending moves` in `notes.md`. Then replace `last/` with `next/`.
6. Close with the question round from SKILL.md § Talking to the user, then print `<folder>` and the board URL.
