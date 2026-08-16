#!/usr/bin/env python3
"""plan_density_check.py — mechanical anchor check across a plan half's post-approval density rewrite.

An approved plan's implementation half is rewritten for density after sign-off, and nobody reads that
half again. This checks the one property the rewrite must not break: every backticked anchor in the
pre-rewrite text — a path, a symbol, a command, a line reference — still appears in the post-rewrite
text. Whatever this lists is an anchor to restore before the rewrite is accepted.

A backticked token is a span between single backticks on one line. Fence handling: a triple-backtick
fence is not a token, and a fenced block's contents are code rather than anchors, so fenced blocks are
removed from the pre-rewrite text before tokens are extracted. Presence is then searched over the
WHOLE post-rewrite text, fenced blocks included: an anchor the rewrite moved into a code block has
survived, not dropped. Comparison is on the literal span content as a substring, so an anchor that
survives inside a longer post-rewrite span still counts as present. Presence, not count: two
occurrences before and one after is not a drop.

Dropping a whole ordered change fails this check, and that is correct rather than a false failure:
removing a change is a scope edit, not a density edit, and scope does not move after sign-off.

Commands:
  check PRE POST   # exit 1 listing every backticked token present in PRE and absent from POST
  self-test        # offline: the comparison's own cases, in memory, reading no files
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

# `(?P=fence)` rather than a fixed length: a block opened with four backticks closes on four, and a
# `\Z` alternative so an unterminated fence swallows its tail instead of leaking code as anchors.
_FENCE_BLOCK = re.compile(r"^[ \t]*(?P<fence>`{3,})[^\n]*\n.*?(?:^[ \t]*(?P=fence)[ \t]*$|\Z)", re.M | re.S)
_INLINE_TOKEN = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")


def backticked_tokens(text: str) -> list[str]:
    """Every single-backticked span in `text` outside fenced code blocks, in document order, with repeats."""
    return [m.group(1) for m in _INLINE_TOKEN.finditer(_FENCE_BLOCK.sub("\n", text))]


def dropped_tokens(pre: str, post: str) -> list[str]:
    """Backticked tokens present in `pre` and absent from `post`, first-seen order, each reported once."""
    dropped: list[str] = []
    for token in backticked_tokens(pre):
        if token not in post and token not in dropped:
            dropped.append(token)
    return dropped


def check_paths(pre: Path, post: Path) -> int:
    """Compare the two plan halves on disk, printing each dropped anchor; 0 when the rewrite dropped none."""
    dropped = dropped_tokens(pre.read_text(encoding="utf-8"), post.read_text(encoding="utf-8"))
    if not dropped:
        print(f"plan_density_check: no backticked anchor dropped ({pre} -> {post}).")
        return 0
    print(f"plan_density_check: the rewrite dropped {len(dropped)} backticked anchor(s) ({pre} -> {post}):")
    for token in dropped:
        print(f"  DROPPED `{token}`")
    print("Restore each anchor above in the rewritten half. An anchor is not prose, and scope is not density.")
    return 1


def _self_test() -> None:
    parser = _build_parser()
    assert parser.parse_args(["self-test"]).command == "self-test"
    args = parser.parse_args(["check", "pre.md", "post.md"])
    assert (args.command, args.pre, args.post) == ("check", Path("pre.md"), Path("post.md")), args

    pre = "Edit `sy_tools/config.py`, then run `pixi run validate` and read the summary line it prints.\n"
    assert dropped_tokens(pre, "Edit `sy_tools/config.py`; run `pixi run validate`.\n") == [], "prose-only cut"
    assert dropped_tokens(pre, "Edit `sy_tools/config.py`.\n") == ["pixi run validate"], "dropped anchor"

    twice = "Read `a.py`. Then edit `a.py` and `b.py`.\n"
    assert dropped_tokens(twice, "Edit `a.py` and `b.py`.\n") == [], "presence, not count"

    assert dropped_tokens("Edit `a.py`.\n", "Edit `sy_tools/a.py:12` now.\n") == [], "survives in a longer span"

    gone = "Call `f()`. Call `f()` again, then `g()`.\n"
    assert dropped_tokens(gone, "Call `g()`.\n") == ["f()"], "a duplicate drop is reported once"

    fenced = "Run it:\n\n```bash\npixi run `nope` validate\n```\n\nThen check `x.py`.\n"
    assert backticked_tokens(fenced) == ["x.py"], backticked_tokens(fenced)
    assert dropped_tokens(fenced, "Then check `x.py`.\n") == [], "a fence is not a token"
    assert dropped_tokens("Check `x.py`.\n", "```\ncheck x.py\n```\n") == [], "an anchor may move into a fence"
    assert backticked_tokens("Write ``x`` here.\n") == [], "a doubled-backtick delimiter is not a token"


def _build_parser() -> argparse.ArgumentParser:
    """The two-subcommand CLI: a check over two plan halves, or an offline self-test."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="list backticked anchors the density rewrite dropped")
    check.add_argument("pre", type=Path, help="the pre-rewrite plan half")
    check.add_argument("post", type=Path, help="the post-rewrite plan half")
    sub.add_parser("self-test", help="check this script's own comparison offline")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch the CLI."""
    args = _build_parser().parse_args(argv)
    if args.command == "self-test":
        _self_test()
        print("plan_density_check self-test passed")
        return 0
    for path in (args.pre, args.post):
        if not path.is_file():
            print(f"plan_density_check: no such file: {path}", file=sys.stderr)
            return 2
    return check_paths(args.pre, args.post)


if __name__ == "__main__":
    raise SystemExit(main())
