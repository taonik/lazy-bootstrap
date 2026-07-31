#!/bin/sh
# Install a toolchain into an Alpine build image. See the Debian twin for the
# rationale: only the `distro` strategy is baked in.
set -eu

kind="${1:-gcc}"
version="${2:-}"
provision="${3:-distro}"

case ",$provision," in
    *,distro,*) ;;
    *) echo "provision=$provision: nothing to bake in, skipping"; exit 0 ;;
esac

case "$kind" in
    gcc)
        apk add --no-cache gcc g++ musl-dev make
        ;;
    llvm|clang)
        major="${version%%.*}"
        # Alpine names its versioned packages llvm<major>/clang<major>.
        if [ -n "$major" ] && apk add --no-cache "clang$major" "llvm$major" lld 2>/dev/null; then
            :
        else
            apk add --no-cache clang llvm lld compiler-rt
        fi
        ;;
    filc)
        echo "fil-c is provisioned at run time from its GitHub release (musl/pizfix build)"
        ;;
    *)
        echo "unknown toolchain kind: $kind" >&2
        exit 1
        ;;
esac
