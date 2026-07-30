# Image inspection fan-out

Rendered figures, screenshots, plots, and marketing visuals carry large image-token payloads. Once an image is `Read` into a long-running agent it stays in that context for the rest of the session, which inflates cost and crowds the window. So the inspection happens in a throwaway delegate rather than in the agent that needs the answer.

**Design invariant (standing; `sy:gate` protects it).** The long-running agents never `Read` or otherwise load a raw image into their own context: the `/sy:spec` and `/sy:ship` main loops, the BUILD worker (`sy:ship-build`), GATE (`sy:gate`), and `sy:slice`. Every eyes-on-pixels check runs in a short-lived `sy:img-inspector` subagent that receives the path(s) and returns text only. No image `Read` may appear in a BUILD or GATE transcript.

## Resolve the image model

Resolve once from the actual process environment:

```text
IMAGE_MODEL=$(python "${CLAUDE_PLUGIN_ROOT}/scripts/sy_config.py" agent img-inspector)
```

Pass `IMAGE_MODEL` as the `sy:img-inspector` Agent invocation's **model override**, not merely as prompt text, per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/model-dispatch.md`. It is a quality floor, not a cost dial, and `config/floors.json` enforces that rather than this sentence alone.

## Fan out

When a step generates, regenerates, selects among, or reviews images:

1. Resolve `IMAGE_MODEL` (above).
2. For each candidate, or the whole set when one verdict suffices, dispatch `sy:img-inspector` with the image path(s), the specific inspection task, and the shape of verdict you need: e.g. "does this figure show `<intended story>`", "does the plot match `<data>`", "is any label clipped or unreadable". Cheap inspections fan out in parallel.
3. Add `sy:img-inspector` to `agents_used`.
4. Read back only the text verdicts. They drive the accept / regenerate / reselect decision; the pixels never enter your context.
5. Record the verdicts (path → verdict + action) in the build brief as the figure's acceptance evidence, exactly as any other verification evidence.

An image whose correctness is load-bearing for the task is a verification obligation like any other: its named evidence is the `sy:img-inspector` verdict, and an obligation with no passing verdict is undischarged.

`sy:slice` has no `Agent` tool and cannot fan out; a slice that produces a figure returns its path to the BUILD worker for inspection and never `Read`s it. `sy:gate`, when a figure's correctness bears on the review, delegates the same way and judges from the returned text verdict. It never opens the image itself.
