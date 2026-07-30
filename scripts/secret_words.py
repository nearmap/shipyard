#!/usr/bin/env python3
"""The single canonical set of credential-shaped name fragments, shared by every consumer.

`scripts/secret_guard.py` (a `PreToolUse` hook, fires on every `Bash` call) and
`scripts/scrub_known_secrets.py` (transcript redaction) each used to carry their own copy of this
word set and its word-matching function — identical, and drifting the moment one gained a new word
without the other. `scripts/sy_config.py` also uses the matcher, to refuse reading or storing a
secret in a config file. This module has zero dependencies specifically so all three can import it
without risking an import cycle.

Commands:
  self-test
"""
from __future__ import annotations

import re
import sys

SECRET_WORDS = frozenset({
    "TOKEN", "SECRET", "SECRETS", "KEY", "KEYS", "APIKEY", "PASSWORD", "PASSWD",
    "CREDENTIAL", "CREDENTIALS", "PAT", "AUTH",
})


def looks_like_secret_name(name: str, extra: frozenset[str] = frozenset()) -> bool:
    """True when a variable or config key name is credential-shaped, by word rather than substring.

    Word-split so `ACLI_TOKEN` matches while `TOKENIZER_PATH` does not. `extra` merges in
    org-specific fragments (the `redaction.extra_words` config key) on top of the built-in set,
    for a credential name this list was never going to guess.
    """
    words = re.split(r"[^A-Za-z0-9]+", name.upper())
    all_words = SECRET_WORDS if not extra else SECRET_WORDS | extra
    return any(word in all_words for word in words if word)


def _self_test() -> None:
    assert looks_like_secret_name("ACLI_TOKEN")
    assert looks_like_secret_name("GITHUB_TOKEN")
    assert looks_like_secret_name("AWS_SECRET_ACCESS_KEY")
    assert not looks_like_secret_name("ACLI_SITE")
    assert not looks_like_secret_name("PATH")
    assert not looks_like_secret_name("SY_SOME_DIRECTORY")
    assert not looks_like_secret_name("NM_BEARER"), "a fragment outside the built-in set is not a false positive"
    assert looks_like_secret_name("NM_BEARER", extra=frozenset({"BEARER"})), "extra must widen the match"


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "self-test":
        _self_test()
        print("secret_words self-test passed")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
