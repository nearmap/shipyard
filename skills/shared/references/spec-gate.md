# Spec-gate: reviewing the drafted plan before sign-off

A plan can survive its own author's judgment and still be the wrong shape, more complicated than the problem, quietly incorrect, or missing an obligation nobody will notice until ship. `sy:spec-gate` is the standing check against that: a fresh reviewer reads the fully drafted plan and reports what it finds, before a human is asked to sign it off.

It is not a second debate. `sy:debate` has already settled the core decision (`${CLAUDE_PLUGIN_ROOT}/skills/shared/references/debate.md`) and the user has already steered it; a spec-gate finding that reduces to "choose the other approach" is out of scope and gets dropped, not triaged. This pass reviews the plan *built on* that decision.

The checklist below is the single copy. Cite this file from the dispatch prompt and from the agent's own instructions; never restate the axes at the call site, because a restated checklist drifts from this one and the drift is invisible from either end.

## The five axes

Three are reviewer judgment — the reviewer has to think, and a pass with nothing found is a real result:

1. **Architecture.** Does the change sit at the right altitude and on the right seam? Look for a new parallel path beside an existing one, a primitive reimplemented instead of reused, a responsibility landing in a layer that should not own it, and coupling the plan introduces but never names.
2. **Simplicity.** Is this the smallest change that delivers the goal? Look for a config toggle where an unconditional behaviour would do, a new abstraction with one caller, ordered steps that collapse into one, and scope the goal does not require.
3. **Correctness.** Do the ordered changes actually produce the stated outcome? Look for a step whose stated effect its cited anchor cannot have, an invariant the plan breaks elsewhere while protecting it here, an ordering that leaves an intermediate state broken, and an acceptance criterion that would pass without the behaviour existing.

Two are required-field completeness — the plan's `/sy:ship` section either carries the field or it does not:

4. **Docs-sync.** `docs requiring updates` must be present and honest: every doc, README, guide, or reference whose content the change makes stale, or the literal `none`. A change to a documented surface with `none` here is a finding; so is a field listing files the change never touches.
5. **Visual-debug flagging.** `visual-debug obligations` must be present and honest: every figure, screenshot, plot, or rendered visual the work produces, regenerates, selects among, or invalidates — each one a verification obligation whose named evidence is an `sy:img-inspector` text verdict per `${CLAUDE_PLUGIN_ROOT}/skills/shared/references/image-inspection.md` — or the literal `none`. Work that touches a visual with `none` here is a finding.

## When it runs

Once, after the plan is fully drafted and after the debate pass, before it is presented for sign-off. The plan the reviewer reads is the one about to be signed off, not an outline of it.

A request-changes round re-dispatches it only on a **material** revision — a changed approach, a changed file set, a changed or dropped obligation, a changed invariant. A wording tweak, a clarified sentence, or a reordering that changes nothing executable does not re-run the pass; a second review of a plan that did not materially move buys nothing and trains the pass to be ignored.
