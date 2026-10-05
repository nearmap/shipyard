# Flagging tests that recompute their expected value

`sy:gate` does not treat a test whose expected value is computed rather than literal as a finding on that ground alone.

## Why

A test that checks the code against an independent implementation of the same logic guards against bugs a refactor introduces, which is a reason to keep it. Such a test is worth flagging only when it costs a complex harness or duplicates production code wholesale, and gate's review already reaches that as a simplification finding without a dedicated lens.

## Prior requests

- AM-1607: proposed from the mattpocock/skills `tdd` skill's tautological-test anti-pattern.
