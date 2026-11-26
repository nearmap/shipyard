---
id: SY-2
severity: MED
---
# SY-2 Agent-facing text justifies its growth

Each net-new addition to agent-facing text carries a justification line, with the scope, carrier and keep/cut test in `skills/ship/references/immutable-gate.md` § Net-new agent-facing text. The words a phase loads (its skill, the references it reads, the agents it dispatches) do not grow without one, and moving text between files does not hide growth.

Applies to every change to that text.

Enforced by: the gate's net-new pass (`agents/gate.md` § Review). The standards review does not check it, because it cannot see the PR body.
