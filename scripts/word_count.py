"""Count the words each Shipyard phase loads: its entry file, the references it cites, and the agents it names.

`python scripts/word_count.py` prints the table; `--root <dir>` counts another checkout; `--write-baseline` records
the counts `validate.py` warns against.
"""

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
BASELINE = "scripts/word_baseline.json"
CORE_GLOB = "skills/shared/references/*.md"

_PLUGIN_REF = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([A-Za-z0-9_./-]+\.md)")
_LOCAL_REF = re.compile(r"`(references/[A-Za-z0-9_./-]+\.md)`")
_AGENT = re.compile(r"`sy:([a-z][a-z0-9-]*)`")
_WORKFLOW = re.compile(r"/sy:([a-z][a-z0-9-]*)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args()
    counts = phase_counts(args.root)
    if args.write_baseline:
        (ROOT / BASELINE).write_text(json.dumps(counts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{'phase':28} {'own':>7} {'refs':>7} {'agents':>8} {'total':>8} {'reach':>8}")
    for phase, row in sorted(counts["phases"].items(), key=lambda kv: -kv[1]["total"]):
        print(f"{phase:28} {row['own']:7d} {row['refs']:7d} {row['agents']:8d} {row['total']:8d} {row['reach']:8d}")
    print(f"workflow names in core files: {counts['core_workflow_names']}")
    return 0


def phase_counts(root: Path) -> dict:
    """Words per phase entry point, and the count of workflow names in the workflow-agnostic core files."""
    agents = {p.stem for p in (root / "agents").glob("*.md")}
    skills = {p.parent.name for p in (root / "skills").glob("*/SKILL.md")}
    entries = {f"skill:{s}": root / "skills" / s / "SKILL.md" for s in skills}
    entries |= {f"agent:{a}": root / "agents" / f"{a}.md" for a in agents}
    phases = {}
    for phase, entry in entries.items():
        loaded = _reach(root, entry, depth=1)
        own = _words(entry)
        refs = sum(_words(f) for f in loaded) - own
        named = {a for f in loaded for a in _AGENT.findall(_text(f)) if a in agents} - {entry.stem}
        agent_files = set().union(set(), *(_reach(root, root / "agents" / f"{a}.md", depth=1) for a in named)) - loaded
        dispatched = sum(_words(f) for f in agent_files)
        reach = sum(_words(f) for f in _reach(root, entry))
        phases[phase] = {
            "own": own,
            "refs": refs,
            "agents": dispatched,
            "total": own + refs + dispatched,
            "reach": reach,
        }
    core = sum(1 for f in root.glob(CORE_GLOB) for name in _WORKFLOW.findall(_text(f)) if name in skills)
    return {"phases": phases, "core_workflow_names": core}


def growth_warnings(root: Path = ROOT) -> list[str]:
    """One warning per phase whose total grew past the recorded baseline, and for more workflow names in core."""
    path = root / BASELINE
    if not path.is_file():
        return [f"{BASELINE} is missing, so loaded words are not compared"]
    base = json.loads(path.read_text(encoding="utf-8"))
    now = phase_counts(root)
    warnings = [
        f"{phase} loads {row['total']} words, up from {base['phases'][phase]['total']}"
        for phase, row in sorted(now["phases"].items())
        if phase in base["phases"] and row["total"] > base["phases"][phase]["total"]
    ]
    if now["core_workflow_names"] > base["core_workflow_names"]:
        warnings.append(
            f"core files name workflows {now['core_workflow_names']} times, up from {base['core_workflow_names']}"
        )
    return warnings


def _reach(root: Path, entry: Path, depth: int = 10_000) -> set[Path]:
    seen: set[Path] = set()
    stack = [(entry, 0)]
    while stack:
        path, level = stack.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        if level == depth:
            continue
        text = _text(path)
        cited = [root / rel for rel in _PLUGIN_REF.findall(text)]
        cited += [base / rel for rel in _LOCAL_REF.findall(text) for base in (path.parent, path.parent.parent)]
        stack += [(c, level + 1) for c in cited]
    return seen


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _words(path: Path) -> int:
    return len(_text(path).split())


if __name__ == "__main__":
    sys.exit(main())
