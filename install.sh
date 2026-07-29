#!/usr/bin/env bash
# Validate the Shipyard plugin and print how to load it. Shipyard is a Claude Code plugin, so it is
# not symlinked into ~/.claude; it is loaded with --plugin-dir (dev) or via a marketplace (install).
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v python >/dev/null 2>&1 || { echo "ERROR: python not found on PATH" >&2; exit 1; }

MIN_CLAUDE_VERSION="2.1.218"
version_ge() { [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]; }

if command -v claude >/dev/null 2>&1; then
  CLAUDE_VERSION="$(claude --version 2>/dev/null | awk '{print $1}')" || CLAUDE_VERSION=""
  if [[ ! "$CLAUDE_VERSION" =~ ^[0-9]+(\.[0-9]+)*$ ]] || ! version_ge "$CLAUDE_VERSION" "$MIN_CLAUDE_VERSION"; then
    echo "NOTE: Claude Code ${CLAUDE_VERSION:-(version unreadable)} does not meet the $MIN_CLAUDE_VERSION floor; it predates the agent-name namespacing rules the sy: agents rely on. Upgrade Claude Code; see docs/installation.md." >&2
  fi
fi

echo "Validating Shipyard plugin..."
python "$PLUGIN_ROOT/scripts/validate.py"

TRACKER="${SY_TRACKER:-jira}"
case "$TRACKER" in
  jira)
    command -v acli >/dev/null 2>&1 || echo "NOTE: SY_TRACKER=jira but 'acli' (Atlassian CLI) is not on PATH." >&2
    ;;
  github)
    command -v gh >/dev/null 2>&1 || { echo "ERROR: SY_TRACKER=github requires 'gh' >= 2.94.0 on PATH" >&2; exit 1; }
    ;;
  *)
    echo "ERROR: SY_TRACKER must be 'jira' or 'github' (got '$TRACKER')" >&2; exit 1
    ;;
esac

command -v gitleaks >/dev/null 2>&1 || \
  echo "NOTE: gitleaks not installed; transcript attachment stops before publish until a deterministic scanner is available." >&2

if [[ -n "${CLAUDE_CODE_SUBAGENT_MODEL:-}" ]]; then
  echo "WARNING: CLAUDE_CODE_SUBAGENT_MODEL is set; it overrides model routing and would reroute the reviewer, the image inspector, and the debate. Unset it." >&2
fi
if [[ -z "${SY_FRONTIER_MODEL:-}" ]]; then
  echo "NOTE: set SY_FRONTIER_MODEL in settings.json env; gate defaults to fable when unset." >&2
fi
if [[ -z "${SY_IMAGE_MODEL:-}" ]]; then
  echo "NOTE: set SY_IMAGE_MODEL in settings.json env; sy:img-inspector defaults to sonnet when unset." >&2
fi
if [[ -z "${SY_DEBATE_MODEL:-}" ]]; then
  echo "NOTE: set SY_DEBATE_MODEL in settings.json env; sy:debate defaults to opus when unset." >&2
fi
if [[ -z "${SY_WORKTREE_ROOT:-}" ]]; then
  echo "NOTE: SY_WORKTREE_ROOT unset; ship worktrees default to the sibling <repo>-worktrees/ directory." >&2
fi

cat <<EOF

Shipyard validated (tracker: $TRACKER). To load it:

  Local development (this session only):
    claude --plugin-dir "$PLUGIN_ROOT"

  Persistent install (this repo ships a marketplace manifest; add it, then install):
    claude plugin marketplace add "$PLUGIN_ROOT"
    claude plugin install sy@shipyard      # or run /plugin and enable "sy"

Commands are namespaced:  /sy:plan  /sy:spec  /sy:ship  /sy:spike  /sy:pr  /sy:ci  /sy:explain
EOF
