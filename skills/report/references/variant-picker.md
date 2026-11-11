# Variant picker: keep/omit buttons in the draft artifact

The draft artifact carries every variant `references/anticipate.md` built, grouped under its primary figure, with buttons that record which to keep. Selections are written to the artifact's own database and read back by the session before the next slice and at CHECK, so the final report contains exactly what was kept and nothing has to be relayed by hand.

## In the document

Every variant cell carries a label of the form `fig-<slug>-var-<key>`, where `fig-<slug>` is its primary figure's label and `<key>` names the checklist item that produced it (`obs`, `est`, `rownorm`, `colnorm`, `transposed`, `thr-<value>`, `all-rows`, `median`, `share-pos`, `logit`, `boundary`, `cov-<column>`, `prior`). The variants of one primary sit together, after it, inside one fenced div:

```markdown
::: {.variants data-figure="fig-trust-level"}
<the variant cells>
:::
```

A variant's `fig-cap` is one line: what changed from the primary.

## Draft and final renders

The draft render uses a Quarto profile that appends the picker: `_quarto-draft.yml` in the report directory sets `format.html.include-after-body` to `_draft/variant-picker.html`, and the slice renders the draft with `--profile draft`. The final render (CHECK) uses no profile, so the shipped HTML carries no picker and no variant the selections omitted. Both files are committed with the report so a later session reproduces the draft.

## The picker

`_draft/variant-picker.html` is this, verbatim; it needs the artifact published with `capabilities: {db: {}, user: {}}`:

```html
<style>
.vp-bar{position:sticky;top:0;z-index:5;display:flex;gap:.5rem;flex-wrap:wrap;align-items:center;padding:.4rem .8rem;background:#fff;border-bottom:1px solid #ccc;font:14px system-ui}
.vp-group{display:flex;gap:.4rem;flex-wrap:wrap;margin:.3rem 0 .8rem}
.vp-group button,.vp-bar button{font:inherit;padding:.15rem .5rem;border:1px solid #888;border-radius:3px;background:#f6f6f6;cursor:pointer}
.vp-omit{opacity:.35}
.vp-chip{padding:.1rem .45rem;border-radius:3px;border:1px solid #888;cursor:pointer}
.vp-chip.keep{background:#dff1ef}.vp-chip.omit{background:#f3dede;text-decoration:line-through}
</style>
<div class="vp-bar"><b>Variants</b><span id="vp-status">loading…</span>
<button data-all="keep">keep all</button><button data-all="omit">omit all variants</button></div>
<script>
(async () => {
  const groups = [...document.querySelectorAll('.variants[data-figure]')];
  const status = document.getElementById('vp-status');
  const state = {};  // figure id -> {variant id -> keep}
  for (const g of groups) {
    const fig = g.dataset.figure;
    state[fig] = {};
    const row = document.createElement('div'); row.className = 'vp-group';
    row.innerHTML = `<span>${fig}:</span>`;
    for (const v of g.querySelectorAll('[id^="' + fig + '-var-"]')) {
      state[fig][v.id] = true;
      const chip = document.createElement('span'); chip.className = 'vp-chip keep'; chip.textContent = v.id.slice(fig.length + 5);
      chip.onclick = () => set(fig, v.id, !state[fig][v.id]);
      chip.dataset.v = v.id; row.appendChild(chip);
    }
    for (const [label, fn] of [['keep all', ids => ids.map(() => true)], ['omit all but first', ids => ids.map((_, i) => i === 0)], ['omit all', ids => ids.map(() => false)]]) {
      const b = document.createElement('button'); b.textContent = label;
      b.onclick = () => { const ids = Object.keys(state[fig]); fn(ids).forEach((k, i) => set(fig, ids[i], k, true)); save(fig); };
      row.appendChild(b);
    }
    g.prepend(row);
  }
  document.querySelectorAll('.vp-bar [data-all]').forEach(b => b.onclick = () => {
    for (const fig in state) { for (const id in state[fig]) set(fig, id, b.dataset.all === 'keep', true); save(fig); }
  });
  let db = null;
  try { db = await claude.use('db'); } catch (e) {}
  if (!db) { status.textContent = 'selections are not saved in this view'; return; }
  for (const fig in state) {
    const snap = await db.doc('selections/' + fig).get();
    const kept = snap.exists ? snap.data().kept : null;
    if (Array.isArray(kept)) for (const id in state[fig]) set(fig, id, kept.includes(id), true);
  }
  status.textContent = 'selections saved';
  function render(fig, id) {
    const keep = state[fig][id], el = document.getElementById(id), chip = document.querySelector('.vp-chip[data-v="' + id + '"]');
    if (el) el.classList.toggle('vp-omit', !keep);
    if (chip) { chip.classList.toggle('keep', keep); chip.classList.toggle('omit', !keep); }
  }
  function set(fig, id, keep, quiet) { state[fig][id] = keep; render(fig, id); if (!quiet) save(fig); }
  async function save(fig) {
    if (!db) return;
    const kept = Object.keys(state[fig]).filter(id => state[fig][id]);
    try { await db.doc('selections/' + fig).set({ kept, updatedAt: new Date().toISOString() }); status.textContent = 'selections saved'; }
    catch (e) { status.textContent = 'could not save: ' + (e.code || e.message); }
  }
})();
</script>
```

Every variant starts kept. A chip toggles one variant; the group buttons act on one primary's variants; the bar buttons act on the page. An omitted variant stays visible, dimmed, so the choice can be reversed.

## Reading selections back

Before dispatching the next slice, and at the start of CHECK, the session reads the `selections` collection of the draft artifact with the `ArtifactData` tool (`list` on `selections`); each document's `kept` is the list of variant ids to keep for that primary figure. A primary with no document keeps every variant. Record the read selections in `report-state.yaml` under `selections`.

At CHECK, the slice removes every omitted variant cell from the document before the final render, and the `# Report` comment names the omitted ids and the last draft commit that held them, so an omission is reversible from the record. The first publish of a draft that declares `db` gets the one functional pass the capability requires: one `ArtifactData` `list` of `selections`, reported to you in a line.
