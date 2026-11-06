"""The severity overlay shape check against the rules in `docs/severity-overlay.md` § What START checks."""
from __future__ import annotations

from pathlib import Path

from sy_tools import overlay

SPEC = Path(__file__).resolve().parents[2] / "docs" / "severity-overlay.md"

EMPTY = 'scale_mapping: []\npinned_patterns: []\nnotes: ""\n'
PIN = "pinned_patterns:\n  - grade: {grade}\n    pattern: Swallowed error\n    recognise_by: {tell}\n"


def test_the_worked_example_passes():
    """The doc's example is the reference overlay, so the doc and the check cannot drift apart."""
    example = SPEC.read_text(encoding="utf-8").split("## Worked example", 1)[1]
    block = example.split("```yaml\n", 1)[1].split("\n```", 1)[0]
    assert "pinned_patterns:" in block, "the worked example's fenced block was not found"
    assert overlay.check(block) == [], overlay.check(block)


def test_rule_1_the_cli_exits_2_on_a_missing_file_0_on_empty_lists_and_1_on_a_malformed_one(tmp_path: Path):
    path = tmp_path / "severity-overlay.yaml"
    assert overlay.main(["check", str(path)]) == 2
    path.write_text(EMPTY, encoding="utf-8")
    assert overlay.main(["check", str(path)]) == 0
    path.write_text("notes: ''\n", encoding="utf-8")
    assert overlay.main(["check", str(path)]) == 1


def test_rule_2_a_misspelt_top_level_key_fails():
    failures = overlay.check(EMPTY.replace("pinned_patterns", "pinned_pattern"))
    assert len(failures) == 1 and failures[0].startswith("rule 2:") and "pinned_pattern" in failures[0], failures


def test_rule_3_an_entry_with_an_empty_value_fails():
    text = EMPTY.replace("pinned_patterns: []\n", PIN.format(grade="HIGH", tell="''"))
    failures = overlay.check(text)
    assert len(failures) == 1 and failures[0].startswith("rule 3: pinned_patterns entry 1"), failures


def test_rule_4_an_unknown_grade_fails():
    text = EMPTY.replace("pinned_patterns: []\n", PIN.format(grade="CRITICAL", tell="except pass"))
    failures = overlay.check(text)
    assert len(failures) == 1 and failures[0].startswith("rule 4:") and "'CRITICAL'" in failures[0], failures
