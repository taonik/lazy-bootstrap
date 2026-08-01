#!/usr/bin/env bash
# The checks, as a script rather than as YAML.
#
# CI systems disagree about everything except how to run a shell script, so the
# checks live here and every CI - GitHub, GitLab, Jenkins, a git hook, a laptop
# - calls this. A check that only exists inside a workflow file cannot be run
# before pushing, which is when it is most useful.
#
#   ci/checks.sh              everything
#   ci/checks.sh unit         one group
#   ci/checks.sh --list       what groups exist
#
# Exit code is the number of failed groups, so a caller can act on it without
# parsing output.

set -uo pipefail
cd "$(dirname "$0")/.."

# Not GROUPS: bash reserves that name for the caller's group ids, so the
# assignment is ignored and the array expands to something like "0".
CHECK_GROUPS=(unit shell cli docs)
failed=0
declare -a FAILURES=()

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
pass() { printf '   \033[32mok\033[0m    %s\n' "$*"; }
fail() { printf '   \033[31mFAIL\033[0m  %s\n' "$*"; FAILURES+=("$*"); failed=$((failed + 1)); }

run_unit() {
    log "unit tests"
    if python3 tests/test_unit.py 2>&1 | tail -3; then
        pass "python3 tests/test_unit.py"
    else
        fail "unit tests"
    fi
}

run_shell() {
    log "shell syntax"
    local bad=0 f
    # A glob that matches nothing must not be handed to bash -n as a literal.
    shopt -s nullglob
    for f in sh/*.sh ci/*.sh tests/*.sh ci/images/*/*.sh; do
        if bash -n "$f" 2>/dev/null; then pass "$f"; else fail "$f"; bad=1; fi
    done
    shopt -u nullglob
    return $bad
}

run_cli() {
    log "cli"
    local cmd
    for cmd in "--help" "toolchains" "build-worker --list"; do
        # shellcheck disable=SC2086
        if ./lazy-bootstrap $cmd >/dev/null 2>&1; then
            pass "lazy-bootstrap $cmd"
        else
            fail "lazy-bootstrap $cmd"
        fi
    done
}

run_docs() {
    log "docs"
    # Every decision record referenced in code should exist in SPECS.md.
    local missing=0 ref
    for ref in $(grep -rhoE '\bD-[0-9]{2}\b' src/ sh/ tests/ 2>/dev/null | sort -u); do
        if grep -q "### $ref" docs/SPECS.md 2>/dev/null; then
            :
        else
            fail "$ref referenced in code but not recorded in docs/SPECS.md"
            missing=1
        fi
    done
    [ "$missing" = 0 ] && pass "every referenced decision record exists"
    return 0
}

if [ "${1:-}" = "--list" ]; then
    printf '%s\n' "${CHECK_GROUPS[@]}"
    exit 0
fi

# Not "${@:-${CHECK_GROUPS[@]}}": after `@`, bash reads `:-` as substring expansion
# with a negative offset rather than as a default value.
[ $# -eq 0 ] && set -- "${CHECK_GROUPS[@]}"

for group in "$@"; do
    case "$group" in
        unit)  run_unit ;;
        shell) run_shell ;;
        cli)   run_cli ;;
        docs)  run_docs ;;
        *) echo "unknown group: $group (try --list)" >&2; exit 2 ;;
    esac
done

echo
if [ "$failed" -eq 0 ]; then
    printf '\033[32mall checks passed\033[0m\n'
else
    printf '\033[31m%d check(s) failed:\033[0m\n' "$failed"
    printf '   - %s\n' "${FAILURES[@]}"
fi
exit "$failed"
