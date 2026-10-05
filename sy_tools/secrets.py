"""Known-value credential scrubbing for text the MCP server writes to an external tracker.

Nothing here returns, logs, or embeds a credential value — only variable names and occurrence counts.
"""
from __future__ import annotations

import os
import re

DEFAULT_MIN_LENGTH = 6

# The one home of the credential-name word set: every consumer reaches it through
# `looks_like_secret_name`, never through a copy of its own.
SECRET_WORDS = frozenset({
    "TOKEN", "SECRET", "SECRETS", "KEY", "KEYS", "APIKEY", "PASSWORD", "PASSWD",
    "CREDENTIAL", "CREDENTIALS", "PAT", "AUTH",
})


def looks_like_secret_name(name: str, extra: frozenset[str] = frozenset()) -> bool:
    """True when a variable or config key name is credential-shaped, by word rather than substring.

    `A_TOKEN` matches, `TOKENIZER_PATH` does not. `extra` merges org-specific words
    (`redaction.extra_words`) on top of the built-in set.
    """
    words = re.split(r"[^A-Za-z0-9]+", name.upper())
    all_words = SECRET_WORDS if not extra else SECRET_WORDS | extra
    return any(word in all_words for word in words if word)


def discover_secret_vars(
    min_length: int = DEFAULT_MIN_LENGTH, extra_words: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Every environment variable whose *name* is credential-shaped, with a value long enough to matter."""
    # Name-based, not value-based: value shape alone would redact ordinary long paths, URLs and ids.
    return {
        name: value
        for name, value in os.environ.items()
        if value and len(value) >= min_length and looks_like_secret_name(name, extra=extra_words)
    }


def scrub_text(text: str, secrets: dict[str, str]) -> tuple[str, dict[str, int]]:
    """Replace every literal occurrence of each secret value with its redaction marker."""
    counts: dict[str, int] = {}
    # Longest value first: a secret that is a substring of another is consumed whole, not fragmented.
    for name, value in sorted(secrets.items(), key=lambda kv: len(kv[1]), reverse=True):
        occurrences = text.count(value)
        if occurrences:
            text = text.replace(value, f"<REDACTED:{name}>")
            counts[name] = occurrences
    return text, counts


