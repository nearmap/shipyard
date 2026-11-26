---
id: SY-10
severity: LOW
---
# SY-10 Docstrings serve callers, comments serve editors

A docstring tells a caller what the code does and its contract. Public functions, tool surfaces and modules get one; a private helper whose name and types already say it does not. A docstring never restates the signature, re-lists typed parameters, or narrates the implementation.

A comment sits at the line it explains, is one line by default, and carries only a non-obvious why the code cannot say: a measured result, a rejected alternative and its reason, a load-bearing ordering, a workaround for behaviour outside this repo. No ticket or PR references, except in a literal `TODO`.

Applies to new and changed code. Enforced by: `shipyard-standards` review mode.
