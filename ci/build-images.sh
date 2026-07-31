#!/usr/bin/env bash
# Build the flavour images described in ci/flavours.toml.
#
#   ci/build-images.sh                       build every flavour
#   ci/build-images.sh debian-13-slim-gcc    build one
#   ci/build-images.sh --list                show what is defined
#   ci/build-images.sh --distro alpine       build a distro's flavours
#
# Environment
#   ENGINE            podman (default) or docker
#   REGISTRY_MIRROR   e.g. mirror.gcr.io - rewrites docker.io bases
#   IMAGE_PREFIX      tag prefix, default lazy-bootstrap
#   LB_DEBUG          1 = echo every command before running it
#
# Every step is announced, so the output doubles as a transcript you can replay
# by hand (docs/SPECS.md D-17).
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"
ENGINE="${ENGINE:-podman}"
IMAGE_PREFIX="${IMAGE_PREFIX:-lazy-bootstrap}"
REGISTRY_MIRROR="${REGISTRY_MIRROR:-}"

say()   { printf '\n\033[1;35m::\033[0m %s\n' "$*"; }
note()  { printf '   \033[2m%s\033[0m\n' "$*"; }
die()   { printf '\033[0;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
run()   {
    [ "${LB_DEBUG:-0}" != 0 ] && printf '   \033[2m$ %s\033[0m\n' "$*"
    "$@"
}

# --- read flavours.toml with the same parser lazy-bootstrap uses ------------
flavours_json() {
    python3 - "$here/flavours.toml" <<'PY'
import json, sys, tomllib
data = tomllib.loads(open(sys.argv[1], "rb").read().decode())
print(json.dumps(data.get("flavour", [])))
PY
}

field() { python3 -c '
import json,sys
rows=json.loads(sys.stdin.read())
name,key=sys.argv[1],sys.argv[2]
for r in rows:
    if r.get("name")==name:
        v=r.get(key,"")
        if isinstance(v,list): print(",".join(v))
        elif isinstance(v,dict): print(" ".join(f"{k}={x}" for k,x in v.items()))
        else: print(v if v is not None else "")
        break
' "$1" "$2"; }

mirror_base() {
    local base="$1"
    if [ -n "$REGISTRY_MIRROR" ]; then
        printf '%s' "${base/docker.io/$REGISTRY_MIRROR}"
    else
        printf '%s' "$base"
    fi
}

build_one() {
    local name="$1" rows="$2"
    local distro base toolchain provision args
    distro="$(printf '%s' "$rows" | field "$name" distro)"
    base="$(printf '%s' "$rows" | field "$name" base)"
    toolchain="$(printf '%s' "$rows" | field "$name" toolchain)"
    provision="$(printf '%s' "$rows" | field "$name" provision)"
    args="$(printf '%s' "$rows" | field "$name" args)"
    [ -n "$distro" ] || die "unknown flavour: $name"

    local kind="${toolchain%%-*}" version=""
    case "$toolchain" in *-*) version="${toolchain#*-}" ;; esac
    case "$version" in [0-9]*) ;; *) version="" ;; esac

    local tag="$IMAGE_PREFIX/$name:latest"
    say "building $tag"
    note "distro=$distro base=$base toolchain=$toolchain provision=$provision"

    local -a extra=()
    for pair in $args; do extra+=(--build-arg "$pair"); done

    run "$ENGINE" build \
        -f "$here/images/$distro/Containerfile" \
        --build-arg "BASE=$(mirror_base "$base")" \
        --build-arg "TOOLCHAIN_KIND=$kind" \
        --build-arg "TOOLCHAIN_VERSION=$version" \
        --build-arg "PROVISION=$provision" \
        "${extra[@]}" \
        -t "$tag" \
        "$here"
    note "built $tag"
}

main() {
    local rows; rows="$(flavours_json)"
    local names; names="$(printf '%s' "$rows" | python3 -c '
import json,sys
for r in json.loads(sys.stdin.read()): print(r["name"])')"

    case "${1:-}" in
        --list|-l)
            printf '%s\n' "$rows" | python3 -c '
import json, sys
for r in json.loads(sys.stdin.read()):
    mark = " (default)" if r.get("default") else (" (mixed)" if r.get("mixed") else "")
    print("%-42s %-8s %-14s%s" % (r["name"], r["distro"], r["toolchain"], mark))
    print("    " + r.get("description", ""))'
            return 0
            ;;
        --distro)
            [ $# -ge 2 ] || die "--distro needs a value"
            names="$(printf '%s' "$rows" | python3 -c '
import json,sys
want=sys.argv[1]
for r in json.loads(sys.stdin.read()):
    if r["distro"]==want: print(r["name"])' "$2")"
            ;;
        "" ) ;;
        * ) names="$*" ;;
    esac

    command -v "$ENGINE" >/dev/null || die "$ENGINE not found (set ENGINE=docker?)"
    local failed=0
    for name in $names; do
        build_one "$name" "$rows" || { printf '\033[0;31mfailed: %s\033[0m\n' "$name"; failed=$((failed + 1)); }
    done
    say "done: $(printf '%s' "$names" | wc -w) flavour(s), $failed failed"
    [ "$failed" -eq 0 ]
}

main "$@"
