"""Rebuild each ship run's final state from the transcripts and count the runs that reach the GATE round cap,
by `gate_rounds_total` and by the substantive rounds `ship_events.derive` counts. `python scripts/gate_cap_replay.py`."""
from collections import defaultdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT)]
import ship_state_replay as r  # noqa: E402
from sy_tools import ship_events  # noqa: E402

CAP = 5
by_path = defaultdict(list)
for w in r.collect(Path.home() / ".claude" / "projects"):
    if not w.error and isinstance(w.fields, dict):
        by_path[w.path].append(w)
rows = []
for path, seq in by_path.items():
    state = {}
    for w in sorted(seq, key=lambda w: w.ts):
        state.update(w.fields)
    log = state.get("gate_round_log") or []
    if not log or not isinstance(log, list):
        continue
    base = int(state.get("gate_rounds_budget_base") or 0)
    old = max(int(state.get("gate_rounds_total") or 0), len(log)) - base
    new = ship_events.derive(state, CAP)["budget"]["used"]
    rows.append((path, old, new, len(log)))
print(f"runs with a round log: {len(rows)}")
print(f"old count reaches cap {CAP}: {sum(o >= CAP for _, o, _, _ in rows)}")
print(f"logged rounds alone reach cap {CAP}: {sum(l >= CAP for *_, l in rows)}")
print(f"substantive count reaches cap {CAP}: {sum(n >= CAP for _, _, n, _ in rows)}")
print(f"rounds logged: {sum(l for *_, l in rows)}; old-counted: {sum(o for _, o, _, _ in rows)}; substantive: {sum(n for _, _, n, _ in rows)}")
for p, o, n, l in sorted(rows, key=lambda x: -x[1])[:10]:
    print(f"  old {o:2d} new {n:2d} log {l:2d}  {Path(p).parent.name}")

