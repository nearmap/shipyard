# Scanning attachments for credentials in other encodings

The attachment check matches a known credential value's UTF-8 bytes verbatim, and nothing else. It does not look for UTF-16 or other encodings of the value, and it does not look inside compressed content.

## Why

The check is a cheap backstop on values this process actually holds, not a secret scanner. Every encoding or container format added is another rule to maintain that still leaves the next one uncovered: the same whack-a-mole that the removed gitleaks and two-pass sanitiser turned into, which caught nothing real while blocking legitimate work. Committed secrets are caught at the repository layer (pre-commit and CI), where the real leaks have happened.

## Prior requests

- AM-1610 (PR #43): proposed in gate review (UTF-16-LE text without a BOM carrying a known value), declined.
