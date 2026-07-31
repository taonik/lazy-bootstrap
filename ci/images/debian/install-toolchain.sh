#!/bin/sh
# Install a toolchain into a Debian-family build image.
#
#   install-toolchain.sh <kind> <version> <provision>
#
# Only the `distro` strategy is performed here: downloading a release tarball
# is cheap to do at run time and keeps the image reusable across versions.
set -eu

kind="${1:-gcc}"
version="${2:-}"
provision="${3:-distro}"

case ",$provision," in
    *,distro,*) ;;
    *) echo "provision=$provision: nothing to bake in, skipping"; exit 0 ;;
esac

apt_install() {
    apt-get update
    apt-get install -y --no-install-recommends "$@"
    rm -rf /var/lib/apt/lists/*
}

case "$kind" in
    gcc)
        if [ -n "$version" ]; then apt_install "gcc-$version" "g++-$version";
        else echo "gcc comes with build-essential"; fi
        ;;
    llvm|clang)
        major="${version%%.*}"
        if [ -n "$major" ]; then apt_install "clang-$major" "lld-$major" "llvm-$major";
        else apt_install clang lld llvm; fi
        ;;
    filc)
        # No distro ships Fil-C: lazy-bootstrap fetches the release tarball and
        # picks the musl (pizfix) or glibc (/opt/fil) variant per target.
        echo "fil-c is provisioned at run time from its GitHub release"
        ;;
    *)
        echo "unknown toolchain kind: $kind" >&2
        exit 1
        ;;
esac
