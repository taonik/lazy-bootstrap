#!/usr/bin/env bash
# End-to-end matrix: every execution class, for both jobs a class must support
# (docs/SPECS.md D-26).
#
#   tests/matrix.sh                    run everything that this machine can
#   tests/matrix.sh --image ubuntu:24.04
#   tests/matrix.sh --only chroot,bwrap
#   tests/matrix.sh --list
#
# Each case is skipped rather than failed when the machine cannot run it (no
# podman, no root, firejail --chroot disabled), so the same script is useful on
# a laptop and in CI. LB_DEBUG is honoured throughout.
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"
LB="$root/lazy-bootstrap"
SH="$root/sh/lazy-bootstrap.sh"

IMAGE="${LB_TEST_IMAGE:-ubuntu:24.04}"
PACKAGE="${LB_TEST_PACKAGE:-hostname}"
MIRROR="${LB_TEST_REGISTRY_MIRROR:-}"
OUT="${LB_TEST_OUT:-/tmp/lb-matrix}"
ONLY=""

pass=0; fail=0; skip=0
declare -a FAILED=()

say()  { printf '\n\033[1;35m::\033[0m \033[1m%s\033[0m\n' "$*"; }
ok()   { printf '   \033[0;32mPASS\033[0m %s\n' "$*"; pass=$((pass + 1)); }
bad()  { printf '   \033[0;31mFAIL\033[0m %s\n' "$*"; fail=$((fail + 1)); FAILED+=("$*"); }
meh()  { printf '   \033[0;33mSKIP\033[0m %s (%s)\n' "$1" "$2"; skip=$((skip + 1)); }

mirror_args() { [ -n "$MIRROR" ] && printf -- '--registry-mirror\ndocker.io=%s\n' "$MIRROR"; }
readarray -t MIRROR_ARGS < <(mirror_args)

selected() {
    [ -z "$ONLY" ] && return 0
    case ",$ONLY," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}

# --- capability probes ------------------------------------------------------
have_root()     { [ "$(id -u)" = 0 ]; }
have_podman()   { command -v podman >/dev/null && podman info >/dev/null 2>&1; }
have_docker()   { command -v docker >/dev/null && docker info >/dev/null 2>&1; }
have_bwrap()    { command -v bwrap >/dev/null && bwrap --ro-bind / / --dev /dev true >/dev/null 2>&1; }
# firejail always refuses --chroot=/, so probe with a bogus path and look at
# *which* complaint comes back: only "feature is disabled" means unavailable.
have_firejail() {
    command -v firejail >/dev/null || return 1
    ! firejail --quiet --noprofile --chroot=/nonexistent-lb-probe true 2>&1 |
        grep -q "chroot feature is disabled"
}

# --- one case ---------------------------------------------------------------
# run_case <name> <command...>
run_case() {
    local name="$1"; shift
    printf '   ... %s\n' "$name"
    if "$@" > "$OUT/$name.log" 2>&1; then
        ok "$name"
    else
        bad "$name  (see $OUT/$name.log)"
        tail -n 6 "$OUT/$name.log" | sed 's/^/        /'
    fi
}

# ---------------------------------------------------------------------------
main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --image) IMAGE="$2"; shift 2 ;;
            --package) PACKAGE="$2"; shift 2 ;;
            --registry-mirror) MIRROR="$2"; readarray -t MIRROR_ARGS < <(mirror_args); shift 2 ;;
            --only) ONLY="$2"; shift 2 ;;
            --out) OUT="$2"; shift 2 ;;
            --list)
                printf 'cases: unit host chroot-image chroot-hostfs bwrap firejail podman docker vm shell\n'
                exit 0 ;;
            *) printf 'unknown option: %s\n' "$1" >&2; exit 2 ;;
        esac
    done
    mkdir -p "$OUT"
    printf 'image=%s package=%s out=%s\n' "$IMAGE" "$PACKAGE" "$OUT"

    # -- 0. unit tests -----------------------------------------------------
    if selected unit; then
        say "unit tests"
        run_case unit python3 "$here/test_unit.py"
    fi

    # -- 1. host class -----------------------------------------------------
    if selected host; then
        say "host class (no isolation)"
        run_case host-inventory "$LB" inventory --backend host
        run_case host-workdir-temp "$LB" inventory --backend host --workdir temp
        run_case host-worker "$LB" build-worker --backend host --system-deps host --toolchain gcc
        run_case host-rebuild "$LB" rebuild --backend host --system-deps host \
            --package "$PACKAGE" --toolchain gcc --no-preflight --out-dir "$OUT/runs"
    fi

    # -- 2. chroot class ---------------------------------------------------
    if selected chroot-image; then
        say "chroot class, rootfs from the image pool"
        if have_root; then
            run_case chroot-image-inventory "$LB" inventory "$IMAGE" --backend chroot \
                --rootfs image "${MIRROR_ARGS[@]}"
            run_case chroot-image-rebuild "$LB" rebuild "$IMAGE" --backend chroot \
                --rootfs image --package "$PACKAGE" --toolchain gcc --no-preflight \
                --out-dir "$OUT/runs" "${MIRROR_ARGS[@]}"
        else
            meh chroot-image "needs root"
        fi
    fi

    if selected chroot-hostfs; then
        say "chroot class, rootfs derived from this machine"
        if have_root; then
            run_case chroot-hostfs-inventory "$LB" inventory --backend chroot \
                --rootfs hostfs:overlay --rootfs-path temp
            run_case chroot-hostfs-rebuild "$LB" rebuild --backend chroot \
                --rootfs hostfs:overlay --rootfs-path temp --package "$PACKAGE" \
                --toolchain gcc --no-preflight --out-dir "$OUT/runs"
        else
            meh chroot-hostfs "needs root"
        fi
    fi

    # -- 3. sandbox class --------------------------------------------------
    if selected bwrap; then
        say "sandbox class: bubblewrap"
        if have_bwrap; then
            run_case bwrap-inventory "$LB" inventory "$IMAGE" --backend bwrap \
                --rootfs image "${MIRROR_ARGS[@]}"
            run_case bwrap-worker "$LB" build-worker "$IMAGE" --backend bwrap \
                --rootfs image --rootfs-path temp --toolchain gcc "${MIRROR_ARGS[@]}"
        else
            meh bwrap "bubblewrap unavailable"
        fi
    fi

    if selected firejail; then
        say "sandbox class: firejail"
        if have_firejail; then
            run_case firejail-inventory "$LB" inventory "$IMAGE" --backend firejail \
                --rootfs image "${MIRROR_ARGS[@]}"
        else
            meh firejail "firejail --chroot unavailable (see /etc/firejail/firejail.config)"
        fi
    fi

    # -- 4. container class ------------------------------------------------
    if selected podman; then
        say "container class: podman"
        if have_podman; then
            run_case podman-inventory "$LB" inventory "$IMAGE" --backend podman "${MIRROR_ARGS[@]}"
            run_case podman-worker "$LB" build-worker "$IMAGE" --backend podman \
                --toolchain gcc --save "image:lb-matrix/worker:test" "${MIRROR_ARGS[@]}"
            run_case podman-rebuild "$LB" rebuild "$IMAGE" --backend podman \
                --package "$PACKAGE" --toolchain gcc --out-dir "$OUT/runs" "${MIRROR_ARGS[@]}"
        else
            meh podman "podman unavailable"
        fi
    fi

    if selected docker; then
        say "container class: docker"
        if have_docker; then
            run_case docker-inventory "$LB" inventory "$IMAGE" --backend docker "${MIRROR_ARGS[@]}"
        else
            meh docker "docker daemon unavailable"
        fi
    fi

    # -- 4b. vm class ------------------------------------------------------
    if selected vm; then
        say "vm class: qemu"
        if ! command -v qemu-system-x86_64 >/dev/null; then
            meh vm "qemu-system-x86_64 not installed"
        elif [ -z "${LB_TEST_VM_IMAGE:-}" ]; then
            # A VM boots a disk image, not a container image, so there is
            # nothing sensible to default to. Point at one to enable this case:
            #   LB_TEST_VM_IMAGE=/path/disk.qcow2 LB_TEST_VM_KEY=... tests/matrix.sh
            meh vm "set LB_TEST_VM_IMAGE (and LB_TEST_VM_KEY / LB_TEST_VM_SEED)"
        else
            run_case vm-available "$LB" env available "$LB_TEST_VM_IMAGE" --backend vm
            run_case vm-ensure "$LB" env ensure "$LB_TEST_VM_IMAGE" --backend vm \
                --vm-option "SSH_KEY=${LB_TEST_VM_KEY:-}" \
                --vm-option "SEED=${LB_TEST_VM_SEED:-}" \
                --vm-option "BOOT_TIMEOUT=${LB_TEST_VM_BOOT_TIMEOUT:-600}"
        fi
    fi

    # -- 5. the shell twin -------------------------------------------------
    if selected shell; then
        say "shell edition (must produce the same report schema)"
        if have_podman; then
            run_case shell-inventory "$SH" inventory "$IMAGE" --registry-mirror "${MIRROR:-}"
        else
            meh shell "podman unavailable"
        fi
    fi

    # -- summary -----------------------------------------------------------
    say "summary"
    printf '   %s passed, %s failed, %s skipped\n' "$pass" "$fail" "$skip"
    for case in "${FAILED[@]:-}"; do [ -n "$case" ] && printf '   failed: %s\n' "$case"; done
    [ "$fail" -eq 0 ]
}

main "$@"
