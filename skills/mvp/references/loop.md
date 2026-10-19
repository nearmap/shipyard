# LOOP: build, deploy on the user's word, keep the ledger current

## The ledger entry

Every entry is a `post-comment` whose `human` half opens `# MVP ledger` and says what changed, in the user's words. Its `agent_detail` half is a full snapshot, never a delta:

```text
change: rule | deploy | hold
rules:
- <every live rule, verbatim, the brief's refused shortcuts included>
deploys:
- target: <name>; sha: <pushed SHA>; version_check: <result>
hold: <target>; since: <date>; side_effect: <from the brief>   # or: hold: none
shortcuts_since_last_deploy:
- <sha> Shortcut: <what was faked> -> <what production needs>
```

A posted entry is never edited; a correction is the next entry.

## Deploys and holds

Deploy only when the user says to, and verify by running the brief's version check until it reports the pushed SHA. A target that answers but reports another SHA is not deployed.

The first deploy to a shared target opens a hold: write a `hold` entry naming the target and the brief's side effect, and tell the user to warn that target's users. The hold stays open until the user releases it, which `/sy:shutdown` records.

## Scope

Scope may grow freely while the goal stands. A change of goal is a `rule` entry, never a body edit.

## Commits

Commit often, so a shortcut is recorded on the commit that takes it rather than reconstructed later. Anything the build depends on outside git — generators, data builders, throwaway scripts — is committed as soon as it is used; a reference that cannot be rebuilt from its tag is no reference.

The user says the MVP is done → route to HARVEST.
