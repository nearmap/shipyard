# Variant picker: the figure artifact and its keep/omit buttons

The artifact is never the rendered document. It is one page built from the figures and captions pulled out of the document, each variant `references/anticipate.md` built sitting beside its siblings under their primary figure, with a keep/omit button below it. Selections are written to the artifact's own database and read back by the session before the next slice and at CHECK, so the final report contains exactly what was kept and nothing has to be relayed by hand. The buttons exist only on that page: the document and its rendered output carry none.

## In the document

Every variant cell carries a label of the form `fig-<slug>-var-<key>`, where `fig-<slug>` is its primary figure's label and `<key>` names the checklist item that produced it (`obs`, `est`, `rownorm`, `colnorm`, `transposed`, `thr-<value>`, `all-rows`, `median`, `share-pos`, `logit`, `boundary`, `cov-<column>`, `prior`). The variants of one primary sit together, after it, inside one fenced div:

```markdown
::: {.variants data-figure="fig-trust-level"}
<the variant cells>
:::
```

A variant's `fig-cap` is one line: what changed from the primary. The labels and the fenced div are all the document holds for the picker; no button, script or draft profile goes into it.

## Extracting the figures

The document is the source of truth; the page is rebuilt from it after every render and never edited by hand. Walk the document top to bottom and take one entry per cell whose `label` starts with `fig-`:

- **image**: the file the render wrote for that label, at the path the slice returned. The figures must exist as separate files: if the document renders with `embed-resources: true` (one self-contained HTML), the slice writes each `fig-*` cell's image to its own file as well (set `keep-md`/`figure-dir` or save the figure explicitly in the cell), named by label, and returns those paths. Never decode images out of the rendered HTML to get them;
- **caption**: the cell's `fig-cap`, as rendered when it is computed;
- **section**: the nearest heading above the cell;
- **family**: a cell inside a `.variants` div belongs to that div's `data-figure`, and its key is the label after `-var-`; the cell labelled exactly `data-figure` is the family's primary. A figure with no `.variants` div is a family of one.

Write the entries, in document order, as the JSON the page reads:

```json
[{"section": "<heading>", "id": "fig-<slug>", "caption": "<fig-cap>", "src": "<data URI>",
  "variants": [{"id": "fig-<slug>-var-<key>", "key": "<key>", "caption": "<fig-cap>", "src": "<data URI>"}]}]
```

A script in the scratch directory does the extraction and the base64 encoding, so no image enters this context. It writes the page to `scratch_dir(<task>)/artifact/<document>.html`; nothing of the page is committed.

**Size.** The `Artifact` tool refuses a page over 16 MB and base64 adds a third, so keep the page under 12 MB. The script works on copies, never the document's own figure files: cap each image's long side at 1600 px; if the page is still over, re-encode as WebP at quality 80; if still over, lower the cap. An animation keeps its own file type under the same budget.

## The page

The page is this, verbatim, with `__TITLE__` and `__FIGURES__` (the JSON above, with every `<` written as `<`) filled in; it needs the artifact published with `capabilities: {db: {}, user: {}}`. At release set `data-pick="false"`, which leaves the buttons out.

```html
<title>__TITLE__ figures</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--mute:#666;--line:#ccc;--keep:#1f7a6d;--omit:#a83232}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#16181b;--fg:#e8e8e8;--mute:#9aa3ad;--line:#444;--keep:#5cc4b4;--omit:#e08a8a}}
:root[data-theme="dark"]{--bg:#16181b;--fg:#e8e8e8;--mute:#9aa3ad;--line:#444;--keep:#5cc4b4;--omit:#e08a8a}
body{margin:0;padding:0 16px 3rem;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui}
.bar{position:sticky;top:0;z-index:5;display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;padding:.5rem 0;background:var(--bg);border-bottom:1px solid var(--line)}
.bar textarea{flex:1 1 100%;font:12px ui-monospace,monospace}
button{font:inherit;padding:.15rem .7rem;border:1px solid var(--line);border-radius:4px;background:transparent;color:inherit;cursor:pointer}
section{max-width:1400px;margin:2rem auto}
h2{font-size:1.1rem;margin:0 0 .5rem}
figure{margin:0}
img{display:block;max-width:100%;height:auto;background:#fff;border:1px solid var(--line)}
figcaption{color:var(--mute);font-size:.9rem;margin:.35rem 0}
figcaption b{color:var(--fg)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,440px),1fr));gap:1.2rem;margin-top:1.2rem}
.var.omit img,.var.omit figcaption{opacity:.3}
.pick{display:inline-flex}
.pick button{border-radius:0}
.pick button:first-child{border-radius:4px 0 0 4px}.pick button:last-child{border-radius:0 4px 4px 0;border-left:0}
.pick button[aria-pressed="true"]{color:#fff;background:var(--keep);border-color:var(--keep)}
.pick button[data-keep="false"][aria-pressed="true"]{background:var(--omit);border-color:var(--omit)}
</style>
<div class="bar" id="bar"><b>Variants</b><span id="count"></span><span id="status">loading…</span>
<button id="all-keep">keep all</button><button id="all-omit">omit all</button><button id="copy">copy selections</button>
<textarea id="out" readonly hidden rows="3"></textarea></div>
<main id="main"></main>
<script type="application/json" id="figures" data-pick="true">__FIGURES__</script>
<script>
(() => {
  const src = document.getElementById('figures'), figs = JSON.parse(src.textContent), pick = src.dataset.pick !== 'false';
  const $ = id => document.getElementById(id), status = $('status'), main = $('main');
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; };
  const state = {}, cards = {};  // figure id -> {variant id -> keep}; variant id -> its card
  let db = null;
  const figure = (f, cls, name) => {
    const fig = el('figure', cls), img = el('img'), cap = el('figcaption');
    img.src = f.src; img.alt = f.id; img.loading = 'lazy';
    cap.append(el('b', '', name + '. '), f.caption || '');
    fig.append(img, cap); return fig;
  };
  for (const f of figs) {
    const sec = el('section'); sec.append(el('h2', '', f.section || f.id), figure(f, '', f.id));
    const vars = f.variants || [];
    if (vars.length) {
      state[f.id] = {};
      const grid = el('div', 'grid');
      for (const v of vars) {
        const card = figure(v, 'var', v.key);
        state[f.id][v.id] = true; cards[v.id] = card;
        if (pick) {
          const row = el('div', 'pick');
          for (const keep of [true, false]) {
            const b = el('button', '', keep ? 'keep' : 'omit'); b.dataset.keep = keep;
            b.onclick = () => { set(f.id, v.id, keep); save(f.id); };
            row.append(b);
          }
          card.append(row);
        }
        grid.append(card);
      }
      sec.append(grid);
    }
    main.append(sec);
  }
  if (!pick) { $('bar').hidden = true; return; }
  function set(fig, id, keep) {
    state[fig][id] = keep; cards[id].classList.toggle('omit', !keep);
    cards[id].querySelectorAll('.pick button').forEach(b => b.setAttribute('aria-pressed', String((b.dataset.keep === 'true') === keep)));
    const all = Object.values(state).flatMap(s => Object.values(s));
    $('count').textContent = all.filter(Boolean).length + ' of ' + all.length + ' kept';
  }
  const kept = fig => Object.keys(state[fig]).filter(id => state[fig][id]);
  async function save(fig) {
    if (!db) return;
    try { await db.doc('selections/' + fig).set({ kept: kept(fig), updatedAt: new Date().toISOString() }); status.textContent = 'selections saved'; }
    catch (e) { status.textContent = 'could not save (' + (e.code || e.message) + '): copy selections into the chat'; }
  }
  for (const fig in state) for (const id in state[fig]) set(fig, id, true);
  for (const [id, keep] of [['all-keep', true], ['all-omit', false]]) $(id).onclick = () => {
    for (const fig in state) { for (const v in state[fig]) set(fig, v, keep); save(fig); }
  };
  $('copy').onclick = () => {  // copy synchronously in the click: an await first would spend the user activation
    const text = 'variant selections ' + JSON.stringify(Object.fromEntries(Object.keys(state).map(fig => [fig, kept(fig)])));
    const ta = el('textarea'); ta.readOnly = true; ta.value = text; ta.style.cssText = 'position:fixed;left:-9999px;top:0';
    document.body.append(ta); ta.focus(); ta.setSelectionRange(0, text.length);
    let ok = false; try { ok = document.execCommand('copy'); } catch (e) {}
    ta.remove();
    const reveal = () => { const out = $('out'); out.hidden = false; out.value = text; out.focus(); out.setSelectionRange(0, text.length); status.textContent = 'copy the selected text'; };
    if (ok) status.textContent = 'copied';
    else if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(() => { status.textContent = 'copied'; }, reveal);
    else reveal();
  };
  (async () => {
    try { db = await claude.use('db'); } catch (e) {}
    if (!db) { status.textContent = 'not saved in this view: copy selections into the chat'; return; }
    try {
      for (const fig in state) {
        const snap = await db.doc('selections/' + fig).get();
        const k = snap.exists ? snap.data().kept : null;
        if (Array.isArray(k)) for (const id in state[fig]) set(fig, id, k.includes(id));
      }
      status.textContent = 'selections saved';
    } catch (e) { status.textContent = 'could not load (' + (e.code || e.message) + '): copy selections into the chat'; }
  })();
})();
</script>
```

Every variant starts kept. The keep/omit pair below a variant sets that variant; the bar buttons act on the page. An omitted variant stays visible, dimmed, so the choice can be reversed. A primary figure has no button.

## Reading selections back

Before dispatching the next slice, and at the start of CHECK, the session reads the `selections` collection of the artifact with the `ArtifactData` tool (`list` on `selections`); each document's `kept` is the list of variant ids to keep for that primary figure. A primary with no document keeps every variant. Where the page could not save, the user pastes the `copy selections` text into the chat: the same mapping of primary figure to kept ids, and it replaces what the database holds. Record the read selections in `report-state.yaml` under `selections`.

At CHECK, the slice removes every omitted variant cell from the document before the final render, and the `# Report` comment names the omitted ids and the last draft commit that held them, so an omission is reversible from the record. The first publish of a page that declares `db` gets the one functional pass the capability requires: one `ArtifactData` `list` of `selections`, reported to you in a line.
