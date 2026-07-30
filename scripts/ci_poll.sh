#!/usr/bin/env bash
# Shared token-free CI poller for ship phases. Launch in the background (run_in_background);
# it sleeps between checks and exits when nothing is pending, so waiting costs no reasoning turns.
# jq-free by design and talks only to the code host. Every ship phase invokes this instead of
# hand-writing its own poller.
#
# Usage:
#   ci_poll.sh poll <pr-number-or-branch> [interval_s] [timeout_s]
#   ci_poll.sh self-test
#
# Omitted interval_s/timeout_s come from resolved config (ci.poll_interval, ci.poll_timeout) —
# raise ci.poll_timeout for repos/matrices whose CI routinely outlasts 30 minutes so a single
# poll call spans the wait.
#
# Exit codes for poll: 0 = checks green or none reported (nothing pending);
# 1 = checks terminal with failures; 2 = timed out while still pending.
set -euo pipefail

# Single reader for every setting; never re-derive a default here.
_config() {
    python "${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/scripts/sy_config.py" get "$1"
}

main() {
    local cmd="${1:-}"
    case "$cmd" in
        poll) shift; poll "$@" ;;
        self-test) self_test ;;
        *) sed -n '2,12p' "$0" >&2; exit 2 ;;
    esac
}

poll() {
    local pr="${1:?usage: ci_poll.sh poll <pr-number-or-branch> [interval_s] [timeout_s]}"
    local interval="${2:-$(_config ci.poll_interval)}" timeout="${3:-$(_config ci.poll_timeout)}"
    local start="$SECONDS" failed_once=0
    while true; do
        local rc=0 out
        out="$(gh pr checks "$pr" 2>&1)" || rc=$?
        case "$(_classify "$rc" "$out")" in
            pass) echo "ci_poll: checks green for $pr"; return 0 ;;
            none) echo "ci_poll: no checks reported for $pr; nothing pending"; return 0 ;;
            fail)
                # retry once so a transient gh/network blip cannot end a long wait early
                if (( failed_once )); then
                    echo "ci_poll: checks terminal with failures for $pr (gh exit $rc)" >&2
                    return 1
                fi
                failed_once=1 ;;
            pending) failed_once=0 ;;
        esac
        if (( SECONDS - start >= timeout )); then
            echo "ci_poll: timed out after ${timeout}s with checks still pending for $pr" >&2
            return 2
        fi
        sleep "$interval"
    done
}

self_test() {
    [[ "$(_classify 0 'all checks were successful')" == pass ]]
    [[ "$(_classify 8 'some checks are still pending')" == pending ]]
    [[ "$(_classify 1 'some checks failed')" == fail ]]
    [[ "$(_classify 1 "no checks reported on the 'branch' branch")" == none ]]

    local tmp
    tmp="$(mktemp -d)"
    cat > "$tmp/gh" <<'FAKE'
#!/usr/bin/env bash
n="$(cat "$CI_POLL_FAKE_STATE")"
echo $((n + 1)) > "$CI_POLL_FAKE_STATE"
if (( n < 2 )); then echo "some checks are still pending"; exit 8; fi
echo "all checks were successful"
FAKE
    chmod +x "$tmp/gh"

    echo 0 > "$tmp/state"
    CI_POLL_FAKE_STATE="$tmp/state" PATH="$tmp:$PATH" poll 99 0 60 > /dev/null

    echo 0 > "$tmp/state"
    local rc=0
    CI_POLL_FAKE_STATE="$tmp/state" PATH="$tmp:$PATH" poll 99 0 0 > /dev/null 2>&1 || rc=$?
    [[ "$rc" == 2 ]]

    cat > "$tmp/gh" <<'FAKE'
#!/usr/bin/env bash
n="$(cat "$CI_POLL_FAKE_STATE")"
echo $((n + 1)) > "$CI_POLL_FAKE_STATE"
if (( n < 1 )); then echo "transient network blip"; exit 6; fi
echo "all checks were successful"
FAKE
    echo 0 > "$tmp/state"
    CI_POLL_FAKE_STATE="$tmp/state" PATH="$tmp:$PATH" poll 99 0 60 > /dev/null

    cat > "$tmp/gh" <<'FAKE'
#!/usr/bin/env bash
echo "some checks failed"; exit 1
FAKE
    rc=0
    CI_POLL_FAKE_STATE="$tmp/state" PATH="$tmp:$PATH" poll 99 0 60 > /dev/null 2>&1 || rc=$?
    [[ "$rc" == 1 ]]

    cat > "$tmp/gh" <<'FAKE'
#!/usr/bin/env bash
echo "some checks are still pending"; exit 8
FAKE
    # an omitted timeout must come from resolved config, not a re-derived local default
    rc=0
    local original_config; original_config="$(declare -f _config)"
    _config() { echo 0; }
    CI_POLL_FAKE_STATE="$tmp/state" PATH="$tmp:$PATH" poll 99 0 > /dev/null 2>&1 || rc=$?
    eval "$original_config"
    [[ "$rc" == 2 ]]

    rm -rf "$tmp"
    echo "ci_poll self-test passed"
}

_classify() {
    local rc="$1" out="$2"
    if [[ "$out" == *"no checks reported"* ]]; then echo none
    elif [[ "$rc" == 0 ]]; then echo pass
    elif [[ "$rc" == 8 ]]; then echo pending
    else echo fail
    fi
}

main "$@"
