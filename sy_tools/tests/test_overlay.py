"""The severity overlay structure check against the rules in `docs/severity-overlay.md` § What START checks."""
from __future__ import annotations

from pathlib import Path

from sy_tools import overlay

SPEC = Path(__file__).resolve().parents[2] / "docs" / "severity-overlay.md"

VALID = """# Severity overlay

## Scale mapping

| Source | Label | Grade |
|---|---|---|

## Pinned patterns

| Grade | Pattern | Recognise by |
|:--|---|--:|
| HIGH | Swallowed error | `except Exception: pass` |

```markdown
## Notes
| Grade | Pattern | Recognise by |
```

## Notes
"""


def test_the_worked_example_passes():
    """The doc's example is the reference overlay, so the doc and the check cannot drift apart."""
    example = SPEC.read_text(encoding="utf-8").split("## Worked example", 1)[1]
    block = example.split("```markdown\n", 1)[1].split("\n```", 1)[0]
    assert "## Notes" in block, "the worked example's fenced block was not found"
    assert overlay.check(block) == [], overlay.check(block)


def test_empty_tables_and_fenced_headings_are_valid():
    assert overlay.check(VALID) == [], overlay.check(VALID)


def test_the_cli_exits_2_on_a_missing_file_and_1_on_a_malformed_one(tmp_path: Path):
    path = tmp_path / "severity-overlay.md"
    assert overlay.main(["check", str(path)]) == 2
    path.write_text("## Notes\n", encoding="utf-8")
    assert overlay.main(["check", str(path)]) == 1


def test_missing_duplicated_and_misordered_sections_fail_rule_2():
    missing = VALID.replace("## Pinned patterns", "# Pinned patterns")
    duplicated = VALID + "\n## Notes\n"
    misordered = VALID.removesuffix("## Notes\n").replace("## Pinned patterns", "## Notes\n\n## Pinned patterns")
    for text, detail in [
        (missing, "## Pinned patterns is missing"),
        (duplicated, "## Notes appears 2 times"),
        (misordered, "out of order"),
    ]:
        failures = overlay.check(text)
        assert len(failures) == 1 and failures[0].startswith("rule 2:") and detail in failures[0], failures


def test_wrong_header_columns_fail_rule_3():
    failures = overlay.check(VALID.replace("| Source | Label | Grade |", "| Source | Grade |"))
    assert failures == ["rule 3: ## Scale mapping header is 'Source | Grade' (expected 'Source | Label | Grade')"], (
        failures
    )


def test_a_decorated_grade_fails_rule_4():
    failures = overlay.check(VALID.replace("| HIGH |", "| **HIGH** |"))
    assert len(failures) == 1 and failures[0].startswith("rule 4:") and "'**HIGH**'" in failures[0], failures
