#!/usr/bin/env bash
#
# smoke_github.sh — end-to-end exercise of the GitHub tracker adapter's contract verbs.
#
# WARNING: THIS CREATES REAL ISSUES (an Epic + 3 Tasks), a real private gist, and real comments in
#          the repo named by tracker_config.repo, and adds/moves cards on the tracker_config.project board.
#          Point it at a SCRATCH repo/board, never a production one.
#
# Type and Status are Projects v2 single-select fields (works on a personal user-owned project — no
# org). The board must have a "Type" field (Epic/Task/Bug) and a "Status" field with one option per
# lifecycle column (backlog/ready/in-progress/in-review/done), named via the `columns.*` config keys;
# see docs/github-setup.md.
#
# Verbs exercised: create-issue, create-child, update-issue, find-issues, link-parent,
#   add-dependency, add-label, assign, set-type, set-status, attach-artifact, post-comment,
#   post-log, link-pr, get-issue.
#
# Cleanup: created issues/gist are left in place by default. Set SMOKE_CLEANUP=1 to delete them.
#
# Requires: gh >= 2.94.0 (auth: project + read:project), a resolvable .shipyard/config.json whose
#           tracker_config.project and tracker_config.repo are set.
#           gitleaks optional (scans the transcript before the gist upload, as the real adapter does).
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GH_PROJECT_HELPER="${CLAUDE_PLUGIN_ROOT:-}/skills/tracker/github/gh_project.py"
[[ -f "$GH_PROJECT_HELPER" ]] || GH_PROJECT_HELPER="$SCRIPT_DIR/../skills/tracker/github/gh_project.py"

SY_CONFIG="${CLAUDE_PLUGIN_ROOT:-$SCRIPT_DIR/..}/scripts/sy_config.py"
# Single reader for every setting; this script re-derives no defaults of its own.
config() { python "$SY_CONFIG" get "$1"; }
GH_PROJECT=""
GH_REPO=""

RUN_TAG="sy-smoke-$(date +%s)-$$"
REPO_ARGS=()
TMP=""
GIST_ID=""
CREATED_ISSUES=()

main() {
  preflight

  step "create-issue — Epic (create issue, then set Type + Status on the board)"
  local epic_url epic_num
  printf '# %s\n\nSmoke-test roadmap epic. Safe to delete.\n' "$RUN_TAG" >"$TMP/epic.md"
  epic_url="$(ghissue create --title "[$RUN_TAG] epic" --body-file "$TMP/epic.md")"
  epic_num="${epic_url##*/}"
  CREATED_ISSUES+=("$epic_num")
  ghp set-type   --issue "$epic_url" --type epic
  ghp set-status --issue "$epic_url" --status backlog
  echo "  epic #$epic_num — $epic_url"

  step "update-issue — replace the Epic body"
  printf '# %s\n\nUpdated roadmap body (update-issue verb).\n' "$RUN_TAG" >"$TMP/epic2.md"
  ghissue edit "$epic_num" --body-file "$TMP/epic2.md" >/dev/null

  step "find-issues — list epics on the board and confirm ours is present"
  if ghp list --type epic | python -c "import sys,json;ns=[i.get('number') for i in json.load(sys.stdin)];sys.exit(0 if $epic_num in ns else 1)"; then
    echo "  ok: epic #$epic_num found among board epics"
  else
    echo "  FAIL: epic #$epic_num not found via find-issues --type epic" >&2; exit 1
  fi

  step "create-child — 3 Tasks under the Epic (--parent), each typed Task"
  local tasks=() i url num
  for i in 1 2 3; do
    printf '# %s task %s\n\nChild task (create-child verb).\n' "$RUN_TAG" "$i" >"$TMP/task$i.md"
    url="$(ghissue create --title "[$RUN_TAG] task $i" --body-file "$TMP/task$i.md" --parent "$epic_num")"
    num="${url##*/}"
    tasks+=("$num")
    CREATED_ISSUES+=("$num")
    ghp set-type --issue "$url" --type task
    echo "  task #$num — $url"
  done
  local t1="${tasks[0]}" t2="${tasks[1]}" t3="${tasks[2]}"
  local t1_url t2_url t3_url
  t1_url="$(ghissue view "$t1" --json url --jq '.url')"
  t2_url="$(ghissue view "$t2" --json url --jq '.url')"
  t3_url="$(ghissue view "$t3" --json url --jq '.url')"

  step "link-parent — re-affirm task #$t3 under the Epic (idempotent here)"
  ghissue edit "$t3" --parent "$epic_num" >/dev/null

  step "add-dependency — task #$t2 blocked-by task #$t1"
  ghissue edit "$t2" --add-blocked-by "$t1" >/dev/null

  step "add-label — 'decomposed' on task #$t1"
  gh label create decomposed "${REPO_ARGS[@]}" --color EDEDED --description "shipyard smoke" >/dev/null 2>&1 || true
  ghissue edit "$t1" --add-label decomposed >/dev/null

  step "assign — self-assign task #$t1"
  ghissue edit "$t1" --add-assignee @me >/dev/null

  step "set-status — drive each Task backlog -> ready -> in-progress -> in-review -> done"
  local st tu
  for tu in "$t1_url" "$t2_url" "$t3_url"; do
    for st in backlog ready in-progress in-review done; do
      echo "  $tu -> $st"
      ghp set-status --issue "$tu" --status "$st" >/dev/null
    done
  done

  step "attach-artifact — secret-scan a transcript, upload as a private gist"
  local transcript="$TMP/$RUN_TAG-transcript.txt" gist_url
  printf 'shipyard smoke transcript for %s\nno secrets here.\n' "$RUN_TAG" >"$transcript"
  if command -v gitleaks >/dev/null 2>&1; then
    gitleaks detect --no-git --source "$TMP" >/dev/null
    echo "  gitleaks: clean"
  else
    echo "  WARNING: gitleaks not installed; real adapter must block publication until it is."
  fi
  gist_url="$(gh gist create --desc "shipyard transcript $RUN_TAG" "$transcript")"
  GIST_ID="${gist_url##*/}"
  echo "  gist — $gist_url"

  step "post-comment — TL;DR prose comment on task #$t1"
  printf 'TL;DR: smoke run %s exercised the full verb set.\n' "$RUN_TAG" >"$TMP/comment.md"
  ghissue comment "$t1" --body-file "$TMP/comment.md" >/dev/null

  step "post-log — standalone machine-log comment (fenced JSON) on task #$t1"
  {
    printf '# Claude Code ship metrics\n\n```json\n'
    printf '{"schema": "shipyard.ship_metrics.v1", "task": "%s", "smoke": true, "transcript_attachment": "%s"}\n' "$t1_url" "$gist_url"
    printf '```\n'
  } >"$TMP/metrics.md"
  ghissue comment "$t1" --body-file "$TMP/metrics.md" >/dev/null

  step "link-pr — reference the Tasks from the Epic with plain #N (native cross-links)"
  printf 'Delivery references (plain #N, non-closing): #%s #%s #%s\n' "$t1" "$t2" "$t3" >"$TMP/linkpr.md"
  ghissue comment "$epic_num" --body-file "$TMP/linkpr.md" >/dev/null

  step "get-issue — read back and assert type (board) / parent + blockedBy (native)"
  assert_eq "epic #$epic_num board type is Epic" "Epic" "$(item_field "$epic_url" type)"
  assert_eq "task #$t1 board type is Task"        "Task" "$(item_field "$t1_url" type)"
  assert_eq "task #$t1 parent is #$epic_num" "$epic_num" "$(ghissue view "$t1" --json parent --jq '.parent.number')"
  local blocked_by
  blocked_by="$(ghissue view "$t2" --json blockedBy --jq '.blockedBy[].number' | tr '\n' ' ')"
  if grep -qw "$t1" <<<"$blocked_by"; then
    echo "  ok: task #$t2 blockedBy includes #$t1 ($blocked_by)"
  else
    echo "  FAIL: task #$t2 blockedBy '$blocked_by' does not include #$t1" >&2; exit 1
  fi

  summary "$epic_num" "$t1" "$t2" "$t3" "$gist_url"
}

# ---- helpers -------------------------------------------------------------

preflight() {
  command -v gh >/dev/null 2>&1 || die "gh CLI not found on PATH"
  local have min="2.94.0"
  have="$(gh --version | head -n1 | awk '{print $3}')"
  version_ge "$have" "$min" || die "gh $have is too old; need >= $min"
  gh auth status >/dev/null 2>&1 || die "gh is not authenticated (run: gh auth login)"
  python "$SY_CONFIG" validate >/dev/null || die "Shipyard configuration is invalid (run: python $SY_CONFIG validate)"
  GH_PROJECT="$(config tracker_config.project)"
  GH_REPO="$(python "$SY_CONFIG" get tracker_config.repo --default "")"
  [[ -n "$GH_PROJECT" ]] || die "tracker_config.project is not set (<owner>/<number>, e.g. @me/3)"
  [[ -f "$GH_PROJECT_HELPER" ]] || die "gh_project.py helper not found at $GH_PROJECT_HELPER"
  REPO_ARGS=(); [[ -n "$GH_REPO" ]] && REPO_ARGS=(-R "$GH_REPO")
  TMP="$(mktemp -d)"
  trap cleanup EXIT
  echo "==> preflight ok: gh $have, repo ${GH_REPO:-<current>}, board $GH_PROJECT"
}

ghissue() { gh issue "$1" "${REPO_ARGS[@]}" "${@:2}"; }
ghp() { python "$GH_PROJECT_HELPER" "$1" --project "$GH_PROJECT" "${@:2}"; }

item_field() { # <issue-url> <type|status>  -> the board field value, or empty
  ghp get --issue "$1" | python -c "import sys,json;d=json.load(sys.stdin) or {};print(d.get('$2') or '')"
}

version_ge() { [[ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" == "$2" ]]; }

assert_eq() {
  if [[ "$3" == "$2" ]]; then
    echo "  ok: $1 ($3)"
  else
    echo "  FAIL: $1 — expected '$2', got '$3'" >&2; exit 1
  fi
}

step() { echo; echo "==> $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

summary() {
  local epic="$1" t1="$2" t2="$3" t3="$4" gist="$5" owner num board_url
  owner="${GH_PROJECT%/*}"; num="${GH_PROJECT##*/}"
  board_url="$(gh project view "$num" --owner "$owner" --format json --jq '.url' 2>/dev/null || true)"
  echo
  echo "==> SUMMARY"
  echo "  epic:  #$epic"
  echo "  tasks: #$t1 (assigned, labeled), #$t2 (blocked-by #$t1), #$t3"
  echo "  gist:  $gist"
  echo "  board: ${board_url:-https://github.com/$owner (project $num)}"
  [[ "${SMOKE_CLEANUP:-0}" == "1" ]] || echo "  NOTE: issues/gist left in place. Re-run with SMOKE_CLEANUP=1 to delete them."
  echo
  echo "All verbs exercised."
}

cleanup() {
  [[ -n "$TMP" && -d "$TMP" ]] && rm -rf "$TMP"
  [[ "${SMOKE_CLEANUP:-0}" == "1" ]] || return 0
  echo "==> cleanup: deleting created issues and gist"
  local n
  for n in "${CREATED_ISSUES[@]:-}"; do
    [[ -n "$n" ]] || continue
    gh issue delete "$n" "${REPO_ARGS[@]}" --yes >/dev/null 2>&1 || true
  done
  [[ -n "$GIST_ID" ]] && { gh gist delete "$GIST_ID" --yes >/dev/null 2>&1 || gh gist delete "$GIST_ID" >/dev/null 2>&1 || true; }
}

main "$@"
