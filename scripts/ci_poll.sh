#!/usr/bin/env bash
# Shared token-free CI poller for ship phases: launch it in the background (run_in_background) and it
# sleeps between checks, so waiting costs no reasoning turns. Every ship phase invokes this rather than
# hand-writing a poller; jq-free by design, and it talks only to the code host. Run it with no arguments
# for the full interface.
#
# Exit codes for poll: 0 = checks green, or no checks reported when --head was omitted or --allow-no-checks
# was declared and the expected head matched; 1 = checks terminal with failures; 2 = timed out, including an
# expected head that never registered checks; 64 = usage error.
set -euo pipefail

# Single reader for every setting; never re-derive a default here. In-process rather than a `python -m`
# entry point on that module: a bash script cannot make an MCP call, and an argv-shaped second path would
# duplicate the tool surface.
_config() {
    local root="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
    # PYTHONPATH, as in `hooks/hooks.json`: the plugin root has to reach `sys.path`, and neither a hook
    # nor this poller may depend on `pixi run`.
    PYTHONPATH="$root" python -c 'import sys; from sy_tools.config import get; print(get(sys.argv[1]))' "$1"
}

usage() {
    cat <<'USAGE'
ci_poll.sh — token-free CI poller.

  ci_poll.sh poll <pr-number-or-branch> [interval_s] [timeout_s] [--repo OWNER/REPO] [--head SHA] [--allow-no-checks]
  ci_poll.sh self-test

The parser accepts a flag before <pr-number-or-branch>, but callers must put it first anyway: the
end-of-run hygiene check matches a live poller by argv shape (`pgrep -f "ci_poll.sh poll <pr>"`), so a
flag ahead of the selector leaves it undetectable. Omitted interval_s/timeout_s come from resolved config
(ci.poll_interval, ci.poll_timeout); raise ci.poll_timeout where CI routinely outlasts it, so one poll
call spans the whole wait.

  --repo OWNER/REPO   the PR's base repository, not the current checkout's origin — from a fork these
                      differ and the PR will not resolve (`gh repo view --json parent` reports the base).
  --head SHA          expected head: no terminal verdict unless the PR's headRefOid equals it at both
                      reads of one iteration, so a run that has not registered yet cannot report green.
  --allow-no-checks   declare this PR legitimately has no CI; only then is an empty check set on a
                      matching head terminal, and only on a second consecutive empty observation.

Exit codes for poll:
  0   checks green; or no checks reported with --head omitted, or --allow-no-checks declared and the head matched
  1   checks terminal with failures
  2   timed out, including an expected head that never registered checks
  64  usage error
USAGE
}

main() {
    local cmd="${1:-}"
    case "$cmd" in
        poll) shift; poll "$@" ;;
        self-test) self_test ;;
        *) usage >&2; return 64 ;;
    esac
}

poll() {
    local pr="" interval="" timeout="" repo="" head="" allow_no_checks=0 positionals=0
    while (( $# )); do
        case "$1" in
            --repo|--head)
                # a `--`-prefixed follower is argv exhaustion in disguise: adopting it as the value burns a
                # whole timeout on a flag name instead of failing loudly
                if (( $# < 2 )) || [[ "$2" == --* ]]; then usage >&2; return 64; fi
                if [[ "$1" == --repo ]]; then repo="$2"; else head="$2"; fi
                shift 2 ;;
            --allow-no-checks) allow_no_checks=1; shift ;;
            --*) usage >&2; return 64 ;;
            *)
                case "$positionals" in
                    0) pr="$1" ;;
                    1) [[ "$1" =~ ^[0-9]+$ ]] || { usage >&2; return 64; }; interval="$1" ;;
                    2) [[ "$1" =~ ^[0-9]+$ ]] || { usage >&2; return 64; }; timeout="$1" ;;
                    *) usage >&2; return 64 ;;
                esac
                positionals=$(( positionals + 1 )); shift ;;
        esac
    done
    if [[ -z "$pr" ]]; then usage >&2; return 64; fi
    if [[ -z "$interval" ]]; then interval="$(_config ci.poll_interval)"; fi
    if [[ -z "$timeout" ]]; then timeout="$(_config ci.poll_timeout)"; fi

    local repo_flag=()
    if [[ -n "$repo" ]]; then repo_flag=(--repo "$repo"); fi

    local start="$SECONDS" failed_once=0 none_once=0
    while true; do
        # the head is read before classification exists, so this read can never be conditional on it
        local rc=0 out state downgrade="" head_rc=0 head_first head_second
        head_first="$(_head "$pr" ${repo_flag[@]+"${repo_flag[@]}"} 2>&1)" || head_rc=$?
        out="$(gh pr checks "$pr" ${repo_flag[@]+"${repo_flag[@]}"} 2>&1)" || rc=$?
        state="$(_classify "$rc" "$out")"

        if (( head_rc )); then
            downgrade="head read failed for $pr: $head_first"
        elif [[ -n "$head" && "$head_first" != "$head" ]]; then
            downgrade="head $head_first does not match expected $head for $pr"
        elif [[ "$state" != pending ]]; then
            # the sandwich: both reads are compared only within this iteration, never pinned across
            # iterations, or a legitimately advanced head could never re-match
            local second_rc=0
            head_second="$(_head "$pr" ${repo_flag[@]+"${repo_flag[@]}"} 2>&1)" || second_rc=$?
            if (( second_rc )); then
                downgrade="second head read failed for $pr: $head_second"
            elif [[ "$head_second" != "$head_first" ]]; then
                downgrade="head changed mid-iteration for $pr: $head_first then $head_second"
            elif [[ -n "$head" && "$head_second" != "$head" ]]; then
                downgrade="head $head_second does not match expected $head for $pr"
            fi
        fi
        if [[ -n "$downgrade" ]]; then
            echo "ci_poll: $downgrade; treating as pending" >&2
            state=pending
        fi

        case "$state" in
            pass) echo "ci_poll: checks green for $pr"; return 0 ;;
            none)
                if [[ -z "$head" ]]; then
                    echo "ci_poll: no checks reported for $pr; nothing pending"; return 0
                fi
                if (( allow_no_checks )); then
                    if (( none_once )); then
                        echo "ci_poll: no checks reported for $pr on head $head across two polls; caller declared --allow-no-checks" >&2
                        return 0
                    fi
                    # a caller that just pushed sees the empty-check window, so one empty set proves nothing
                    none_once=1; failed_once=0
                else
                    failed_once=0; none_once=0
                fi ;;
            fail)
                # retry once so a transient gh/network blip cannot end a long wait early
                if (( failed_once )); then
                    echo "ci_poll: checks terminal with failures for $pr (gh exit $rc)" >&2
                    echo "$out" >&2
                    return 1
                fi
                failed_once=1; none_once=0 ;;
            pending) failed_once=0; none_once=0 ;;
        esac
        if (( SECONDS - start >= timeout )); then
            echo "ci_poll: timed out after ${timeout}s for $pr (last observed check state: $state)" >&2
            return 2
        fi
        sleep "$interval"
    done
}

_head() {
    local pr="$1"; shift
    gh pr view "$pr" "$@" --json headRefOid --jq .headRefOid
}

# Writes a fake `gh` that dispatches on argv before it reads or increments the counter, so a head read
# never consumes a state the check case is counting on.
_fake_gh() {
    local path="$1" view_body="$2" checks_body="$3"
    cat > "$path" <<FAKE
#!/usr/bin/env bash
if [[ "\$2" == view ]]; then
$view_body
fi
n="\$(cat "\$CI_POLL_FAKE_STATE")"
echo \$((n + 1)) > "\$CI_POLL_FAKE_STATE"
$checks_body
FAKE
    chmod +x "$path"
}

# Resets the counter, runs `poll` in a subshell, keeps stderr for inspection, and prints the exit status.
_poll_rc() {
    local tmp="$1"; shift
    local rc=0
    echo 0 > "$tmp/state"
    CI_POLL_FAKE_STATE="$tmp/state" PATH="$tmp:$PATH" poll "$@" > /dev/null 2>"$tmp/err" || rc=$?
    printf '%s' "$rc"
}

# `validate.py` surfaces only this script's stderr, so every case has to name itself on failure.
_assert() {
    if [[ "$2" != "$3" ]]; then
        echo "ci_poll self-test: $1: expected '$3', got '$2'" >&2
        return 1
    fi
}

_assert_has() {
    if [[ "$2" != *"$3"* ]]; then
        echo "ci_poll self-test: $1: expected text containing '$3', got: $2" >&2
        return 1
    fi
}

self_test() {
    _assert "classify green" "$(_classify 0 'all checks were successful')" pass
    _assert "classify pending" "$(_classify 8 'some checks are still pending')" pending
    _assert "classify fail" "$(_classify 1 'some checks failed')" fail
    _assert "classify none" "$(_classify 1 "no checks reported on the 'branch' branch")" none
    _assert "(a) rc beats text" "$(_classify 0 "no checks reported on the 'branch' branch")" pass
    _assert "(b) resolution error is not none" "$(_classify 1 'Could not resolve to a PullRequest with the number of 99.')" fail

    local tmp
    tmp="$(mktemp -d)"
    local green='printf "build\tpass\t1s\thttps://example.test/1\n"'
    local empty='echo "no checks reported on the '"'"'branch'"'"' branch"; exit 1'
    local head_a='echo aaa111; exit 0'

    _fake_gh "$tmp/gh" "$head_a" 'if (( n < 2 )); then echo "some checks are still pending"; exit 8; fi
'"$green"
    _assert "pending then green" "$(_poll_rc "$tmp" 99 0 60)" 0
    _assert "timeout while pending" "$(_poll_rc "$tmp" 99 0 0)" 2
    # first of the flag cases: a `shift 2` copied onto the valueless flag hangs every later case that ends
    # with it, so this one has to fail the suite before the suite can stall
    _assert "interleaved valueless flag" "$(_poll_rc "$tmp" 99 --allow-no-checks --repo owner/repo 0 0)" 2

    _fake_gh "$tmp/gh" "$head_a" 'if (( n < 1 )); then echo "transient network blip"; exit 6; fi
'"$green"
    _assert "single blip retried" "$(_poll_rc "$tmp" 99 0 60)" 0
    # pins the fakes' argv-first dispatch: a head read that consumed a state would green this on call 1
    _assert "single blip cost exactly two check calls" "$(cat "$tmp/state")" 2

    _fake_gh "$tmp/gh" "$head_a" 'echo "some checks failed: build"; exit 1'
    _assert "terminal failure" "$(_poll_rc "$tmp" 99 0 60)" 1
    _assert_has "(i) gh text reaches stderr on exit 1" "$(cat "$tmp/err")" "some checks failed: build"
    _assert "(o) --allow-no-checks never greens a failure" "$(_poll_rc "$tmp" 99 0 60 --head aaa111 --allow-no-checks)" 1

    _fake_gh "$tmp/gh" "$head_a" 'echo "some checks are still pending"; exit 8'
    _assert "(p) --allow-no-checks never shortens a pending wait" "$(_poll_rc "$tmp" 99 0 0 --head aaa111 --allow-no-checks)" 2
    # an omitted timeout must come from resolved config, not a re-derived local default
    local original_config; original_config="$(declare -f _config)"
    _config() { echo 0; }
    local rc_config; rc_config="$(_poll_rc "$tmp" 99 0)"
    eval "$original_config"
    _assert "omitted timeout resolves from config" "$rc_config" 2

    _fake_gh "$tmp/gh" "$head_a" "$green"
    _assert "(c) green on the wrong head is not terminal" "$(_poll_rc "$tmp" 99 0 0 --head bbb222)" 2
    _assert_has "(c) disagreement is logged" "$(cat "$tmp/err")" "does not match expected bbb222"

    _fake_gh "$tmp/gh" 'echo "gh: could not resolve the head for 99" >&2; exit 1' "$green"
    _assert "(l) failed head read degrades a green" "$(_poll_rc "$tmp" 99 0 0)" 2
    _assert_has "(l) the failed read's own text is logged" "$(cat "$tmp/err")" "gh: could not resolve the head for 99"

    _fake_gh "$tmp/gh" 'v="$(cat "$CI_POLL_FAKE_STATE.view" 2>/dev/null || echo 0)"
echo $((v + 1)) > "$CI_POLL_FAKE_STATE.view"
if (( v % 2 == 0 )); then echo aaa111; else echo bbb222; fi
exit 0' "$green"
    echo 0 > "$tmp/state.view"
    _assert "(f) head changing mid-iteration is not terminal" "$(_poll_rc "$tmp" 99 0 0)" 2
    _assert_has "(f) the change is logged with both SHAs" "$(cat "$tmp/err")" "head changed mid-iteration for 99: aaa111 then bbb222"

    _fake_gh "$tmp/gh" 'if (( $(cat "$CI_POLL_FAKE_STATE") < 2 )); then echo aaa111; else echo bbb222; fi
exit 0' 'if (( n < 2 )); then echo "some checks are still pending"; exit 8; fi
'"$green"
    # a 3s bound rather than 0: a head pinned across iterations can never re-match, and must surface as
    # this assertion failing rather than as the suite spinning to a long timeout
    _assert "(g) an advancing head still reaches a verdict without --head" "$(_poll_rc "$tmp" 99 0 3)" 0

    _fake_gh "$tmp/gh" "$head_a" "$empty"
    _assert "(d) an empty set on an expected head is not green" "$(_poll_rc "$tmp" 99 0 0 --head aaa111)" 2
    _assert_has "(d) the timeout names the observed state" "$(cat "$tmp/err")" "last observed check state: none"
    _assert "(e) an empty set without --head keeps today's contract" "$(_poll_rc "$tmp" 99 0 60)" 0
    _assert "(e) --allow-no-checks without --head is a no-op" "$(_poll_rc "$tmp" 99 0 60 --allow-no-checks)" 0
    _assert "(e) that no-op returns on the first iteration" "$(cat "$tmp/state")" 1
    _assert "(m) --allow-no-checks greens a matching head" "$(_poll_rc "$tmp" 99 0 60 --head aaa111 --allow-no-checks)" 0
    _assert "(m) only on the second observation" "$(cat "$tmp/state")" 2
    _assert_has "(m) the override is announced" "$(cat "$tmp/err")" "no checks reported for 99 on head aaa111 across two polls"
    _assert "(n) never on a mismatched head" "$(_poll_rc "$tmp" 99 0 0 --head bbb222 --allow-no-checks)" 2

    # `1 2` rather than `0 0`: a shared one-shot flag greens iteration 2, so iteration 2 must be reached,
    # and the third state must not be another empty set or a correct implementation would green there
    _fake_gh "$tmp/gh" "$head_a" 'if (( n < 1 )); then echo "some checks failed"; exit 1; fi
if (( n > 1 )); then echo "some checks are still pending"; exit 8; fi
'"$empty"
    _assert "(q) a fail then an empty set is not green" "$(_poll_rc "$tmp" 99 1 2 --head aaa111 --allow-no-checks)" 2

    local usage_text; usage_text="$(usage)"
    local token
    for token in '--repo' '--head' '--allow-no-checks' '0   checks green' '1   checks terminal with failures' '2   timed out' '64  usage error'; do
        _assert_has "(k) usage covers $token" "$usage_text" "$token"
    done

    local shell="${BASH_SOURCE[0]}"
    _assert "(h) unknown subcommand" "$(_status bash "$shell" bogus)" 64
    _assert "(h) missing selector" "$(_status bash "$shell" poll)" 64
    _assert "(h) unknown flag" "$(_status bash "$shell" poll 12 --nope)" 64
    _assert "(h) --head at argv exhaustion" "$(_status bash "$shell" poll 12 --head)" 64
    _assert "(h) --head followed by a flag" "$(_status bash "$shell" poll 12 --head --allow-no-checks)" 64
    _assert "(r) non-numeric interval is a usage error, not a later arithmetic crash" "$(_status bash "$shell" poll 12 abc 60)" 64
    _assert "(r) non-numeric timeout is a usage error" "$(_status bash "$shell" poll 12 5 abc)" 64

    if ! command -v pgrep > /dev/null; then
        echo "ci_poll self-test: (j) needs pgrep, which is not on PATH" >&2
        return 1
    fi
    _fake_gh "$tmp/gh" "$head_a" 'echo "some checks are still pending"; exit 8'
    echo 0 > "$tmp/state"
    CI_POLL_FAKE_STATE="$tmp/state" PATH="$tmp:$PATH" bash "$shell" poll 99 5 60 --repo owner/repo --head aaa111 --allow-no-checks > /dev/null 2>&1 &
    local pid=$! waited=0 matched=0
    while (( waited < 50 )) && ! pgrep -f "ci_poll.sh poll 99" > /dev/null; do sleep 0.1; waited=$(( waited + 1 )); done
    if pgrep -f "ci_poll.sh poll 99" > /dev/null; then matched=1; fi
    # both statuses swallowed: a killed child returns 143 and `kill` itself fails if it already exited
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    _assert "(j) pgrep matches a live poller carrying the new flags" "$matched" 1

    rm -rf "$tmp"
    echo "ci_poll self-test passed"
}

# Runs a command in a child process and prints its exit status, so a usage error is observed as a real
# exit status rather than an in-process `return`.
_status() {
    local rc=0
    "$@" > /dev/null 2>&1 || rc=$?
    printf '%s' "$rc"
}

_classify() {
    local rc="$1" out="$2"
    if [[ "$rc" == 0 ]]; then echo pass
    elif [[ "$rc" == 8 ]]; then echo pending
    elif [[ "$rc" == 1 && "$out" == *"no checks reported"* ]]; then echo none
    else echo fail
    fi
}

main "$@"
