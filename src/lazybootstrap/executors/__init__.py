"""Backend registry: name -> Executor class, plus a capability probe."""

from __future__ import annotations

import os
import shutil
import subprocess

from ..trace import Tracer
from .base import CommandResult, Executor, ExecutorError, ExecutorSpec
from .bubblewrap import BubblewrapExecutor
from .chroot import ChrootExecutor
from .firejail import FirejailExecutor
from .host import HostExecutor
from .oci import OciExecutor

BACKENDS: dict[str, type[Executor]] = {
    "host": HostExecutor,
    "bwrap": BubblewrapExecutor,
    "bubblewrap": BubblewrapExecutor,
    "chroot": ChrootExecutor,
    "firejail": FirejailExecutor,
    "oci": OciExecutor,
    "podman": OciExecutor,
    "docker": OciExecutor,
}

# Backends that run inside an image and therefore need a rootfs or an OCI ref.
NEEDS_ROOTFS = {"bwrap", "bubblewrap", "firejail", "chroot"}
NEEDS_IMAGE = {"oci", "podman", "docker"}


def create(spec: ExecutorSpec, tracer: Tracer | None = None) -> Executor:
    try:
        cls = BACKENDS[spec.kind]
    except KeyError:
        raise ExecutorError(
            f"unknown backend {spec.kind!r}; known: {', '.join(sorted(set(BACKENDS)))}"
        ) from None
    # `--backend podman` / `--backend docker` are shorthands for oci+engine.
    if spec.kind in ("podman", "docker"):
        spec.engine = spec.kind
        spec.kind = "oci"
    return cls(spec, tracer)


def probe() -> dict[str, dict[str, str]]:
    """What this machine can actually do. Used by `lazy-bootstrap doctor`."""
    report: dict[str, dict[str, str]] = {}

    report["host"] = {"available": "yes", "detail": f"uid={os.geteuid()}"}
    report["chroot"] = {
        "available": "yes" if (shutil.which("chroot") and os.geteuid() == 0) else "no",
        "detail": (shutil.which("chroot") or "not installed") if os.geteuid() == 0
                  else "needs root",
    }

    for name, hint in (("podman", "oci"), ("docker", "oci")):
        path = shutil.which(name)
        if not path:
            report[name] = {"available": "no", "detail": "not installed"}
            continue
        proc = subprocess.run([name, "info", "--format", "{{.Host.Arch}}"],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            proc = subprocess.run([name, "info"], capture_output=True, text=True)
        report[name] = {
            "available": "yes" if proc.returncode == 0 else "no",
            "detail": path if proc.returncode == 0 else _first_line(proc.stderr),
            "backend": hint,
        }

    bwrap = shutil.which("bwrap")
    if bwrap:
        proc = subprocess.run([bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "true"],
                              capture_output=True, text=True)
        report["bwrap"] = {"available": "yes" if proc.returncode == 0 else "no",
                           "detail": bwrap if proc.returncode == 0 else _first_line(proc.stderr)}
    else:
        report["bwrap"] = {"available": "no", "detail": "not installed"}

    firejail = shutil.which("firejail")
    if not firejail:
        report["firejail"] = {"available": "no", "detail": "not installed"}
    else:
        # Running is not enough: this backend needs --chroot, which many distros
        # disable by default in /etc/firejail/firejail.config.
        proc = subprocess.run([firejail, "--quiet", "--noprofile",
                               "--chroot=/nonexistent-lazy-bootstrap-probe", "true"],
                              capture_output=True, text=True)
        message = proc.stderr + proc.stdout
        if "chroot feature is disabled" not in message:
            # Any other complaint (including "invalid chroot directory") means
            # the feature itself is available; we only probed it with a bad path.
            report["firejail"] = {"available": "yes", "detail": firejail}
        elif True:
            report["firejail"] = {
                "available": "no",
                "detail": "installed, but --chroot is disabled: set 'chroot yes' in "
                          "/etc/firejail/firejail.config",
            }
        else:
            report["firejail"] = {"available": "no", "detail": _first_line(proc.stderr)}

    # overlayfs powers `--rootfs hostfs` without copying the whole machine.
    try:
        overlay = "overlay" in open("/proc/filesystems", encoding="utf-8").read()
    except OSError:
        overlay = False
    report["overlayfs"] = {"available": "yes" if overlay and os.geteuid() == 0 else "no",
                           "detail": "hostfs rootfs can use copy-on-write" if overlay
                                     else "hostfs rootfs falls back to copy mode"}

    for helper in ("skopeo", "umoci", "patchelf", "curl", "tar", "xz", "mount"):
        path = shutil.which(helper)
        report[helper] = {"available": "yes" if path else "no", "detail": path or "not installed"}

    return report


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


__all__ = [
    "BACKENDS",
    "CommandResult",
    "Executor",
    "ExecutorError",
    "ExecutorSpec",
    "create",
    "probe",
]
