# Review mode

Read `git diff <base>..<head>`, limited to `paths` when given. Check added lines against every part except SY-002, SY-006 and SY-011.

Return the block in `skills/standards/references/review.md` § Compact return contract. Each finding's grade equals its part's `severity` and carries `rule: shipyard-standards <id> [<severity>]`. Set `BEHAVIOURAL_LENSES` to name SY-006 when the range touches text it covers.

Never raise SY-006, and never raise a finding whose only fix is rewording text that is already true.
