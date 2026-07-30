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
    echo "NOTE: Claude Code ${CLAUDE_VERSION:-(version unreadable)} does not meet the $MIN_CLAUDE_VERSION floor documented in docs/installation.md. Consider upgrading Claude Code." >&2
  fi
fi

echo "Validating Shipyard plugin..."
python "$PLUGIN_ROOT/scripts/validate.py"

# Every setting resolves through one reader; nothing here re-derives a default.
if ! python "$PLUGIN_ROOT/scripts/sy_config.py" validate; then
  echo "ERROR: Shipyard configuration is invalid; fix the errors above before loading the plugin." >&2
  exit 1
fi
TRACKER="$(python "$PLUGIN_ROOT/scripts/sy_config.py" get tracker)"
case "$TRACKER" in
  jira)
    command -v acli >/dev/null 2>&1 || echo "NOTE: tracker is 'jira' but 'acli' (Atlassian CLI) is not on PATH." >&2
    ;;
  github)
    command -v gh >/dev/null 2>&1 || { echo "ERROR: tracker 'github' requires 'gh' >= 2.94.0 on PATH" >&2; exit 1; }
    ;;
  *)
    echo "ERROR: tracker must name an adapter under skills/tracker/ (got '$TRACKER')" >&2; exit 1
    ;;
esac

command -v gitleaks >/dev/null 2>&1 || \
  echo "NOTE: gitleaks not installed; transcript attachment stops before publish until a deterministic scanner is available." >&2

# CLAUDE_CODE_SUBAGENT_MODEL outranks the per-invocation model parameter, so it silently reroutes
# every agent off whatever the resolver decided. `sy_config.py validate` already fails on it; this
# is only a clearer message at install time.
if [[ -n "${CLAUDE_CODE_SUBAGENT_MODEL:-}" ]]; then
  echo "ERROR: CLAUDE_CODE_SUBAGENT_MODEL is set; it outranks resolved per-agent models and would reroute the reviewer, the image inspector, and the debate. Unset it." >&2
  exit 1
fi

echo "Resolved configuration:"
python "$PLUGIN_ROOT/scripts/sy_config.py" show

cat <<EOF

Shipyard validated (tracker: $TRACKER). To load it:

  Local development (this session only):
    claude --plugin-dir "$PLUGIN_ROOT"

  Persistent install (this repo ships a marketplace manifest; add it, then install):
    claude plugin marketplace add "$PLUGIN_ROOT"
    claude plugin install sy@shipyard      # or run /plugin and enable "sy"

Commands are namespaced:  /sy:plan  /sy:spec  /sy:ship  /sy:spike  /sy:pr  /sy:ci  /sy:explain
                          /sy:help  /sy:init-repo  /sy:config
EOF
