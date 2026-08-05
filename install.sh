#!/usr/bin/env bash
# Validate the Shipyard plugin and print how to load it. Shipyard is a Claude Code plugin, so it is
# not symlinked into ~/.claude; it is loaded with --plugin-dir (dev) or via a marketplace (install).
set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v python >/dev/null 2>&1 || { echo "ERROR: python not found on PATH" >&2; exit 1; }

# Single reader for every setting; nothing here re-derives a default. In-process because a bash script
# cannot make an MCP call, and on bare `python` because this must run before `pixi run` is known to work.
_resolver() {
  local snippet="$1"; shift
  # PYTHONPATH, as in `hooks/hooks.json`: the plugin root has to reach `sys.path`.
  PYTHONPATH="$PLUGIN_ROOT" python -c "$snippet" "$@"
}

command -v pixi >/dev/null 2>&1 || \
  { echo "ERROR: pixi not found on PATH; the sy MCP server runs as 'pixi run sy-server'. Install it: https://pixi.sh/latest/#installation" >&2; exit 1; }

# --locked refuses to re-resolve, so lock/manifest drift stops here rather than installing something untested.
echo "Preparing the sy MCP server environment..."
if ! (cd "$PLUGIN_ROOT" && pixi install --locked); then
  echo "ERROR: 'pixi install --locked' failed. If it reports a lock mismatch, pixi.lock and pyproject.toml disagree in this checkout — that is the plugin's bug, not yours; report it rather than running 'pixi install' without --locked." >&2
  exit 1
fi

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

if ! _resolver '
import sys
from sy_tools.config import validate
errors = validate()
sys.stderr.write("".join(f"  {e}\n" for e in errors))
sys.exit(1 if errors else 0)
'; then
  echo "ERROR: Shipyard configuration is invalid; fix the errors above before loading the plugin." >&2
  exit 1
fi
TRACKER="$(_resolver 'import sys; from sy_tools.config import get; print(get(sys.argv[1]))' tracker)"
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
  echo "NOTE: gitleaks not on PATH; the MCP server resolves its own from pixi.lock, so only the off-tool path (running the two sanitisation passes by hand) stops before publish." >&2

# CLAUDE_CODE_SUBAGENT_MODEL outranks the per-invocation model parameter, so it silently reroutes every
# agent off the resolved one. The validation above already fails on it; this is the clearer message.
if [[ -n "${CLAUDE_CODE_SUBAGENT_MODEL:-}" ]]; then
  echo "ERROR: CLAUDE_CODE_SUBAGENT_MODEL is set; it outranks resolved per-agent models and would reroute the reviewer, the image inspector, and the debate. Unset it." >&2
  exit 1
fi

# The `show_config` report verbatim as JSON: reformatting it here would be a second renderer to keep in step.
echo "Resolved configuration:"
_resolver 'import json; from sy_tools.config import show; print(json.dumps(show(), indent=2))'

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
