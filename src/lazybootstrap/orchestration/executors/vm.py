"""VM backend: a generic driver interface, with qemu/kvm implemented.

The Executor contract - run a shell script, move files - says nothing about
processes on this machine, so a VM fits it as well as a container does. What a
VM adds is a *transport*: commands have to reach the guest somehow, and files
have to cross the boundary.

Two transports, in order of preference:

    ssh       needs sshd in the guest; the general answer, and the one that
              works for libvirt, cloud instances and remote hypervisors
    virtiofs  a shared directory plus a small agent loop in the guest; used
              when the guest has no sshd (a rootfs unpacked from an image
              usually has none)

`VmDriver` is the seam other hypervisors plug into (libvirt, VirtualBox, a cloud
API): a driver only has to start a guest, tell us how to reach it, and stop it.
Only `qemu` is implemented here - deliberately, see docs/SPECS.md D-28.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .. import util
from ..logs import get
from .base import Executor, ExecutorError, env_prefix

log = get("vm")

#: Drivers other than qemu are not implemented; they are named here so that
#: `--vm-driver libvirt` fails with "not implemented yet" rather than
#: "unknown option", which is a materially different message.
KNOWN_DRIVERS = ("qemu", "libvirt", "virtualbox", "cloud")


@dataclass
class GuestAddress:
    """How to reach a running guest."""

    kind: str = "ssh"          # ssh | virtiofs
    host: str = "127.0.0.1"
    port: int = 0
    user: str = "root"
    key: str = ""
    share: str = ""            # host directory shared with the guest (virtiofs)


@dataclass
class VmSpec:
    """What a VM driver needs to start a guest."""

    name: str = "lazy-bootstrap"
    image: str = ""            # disk image (qcow2/raw)
    kernel: str = ""           # direct kernel boot
    initrd: str = ""
    append: str = ""
    rootfs: str = ""           # a directory to expose to the guest
    seed: str = ""             # cloud-init seed ISO (cloud-localds), if any
    disk_format: str = "auto"  # auto | qcow2 | raw
    memory: str = "2G"
    cpus: int = 2
    accel: str = "auto"        # auto | kvm | tcg
    ssh_port: int = 0          # 0 = pick a free one
    ssh_user: str = "root"
    ssh_key: str = ""
    extra_args: list[str] = field(default_factory=list)
    boot_timeout: int = 300


class VmDriver(ABC):
    """The seam for hypervisors. Implement three methods and the rest follows."""

    name: str = "abstract"

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """(usable here?, why not / which binary)."""

    @abstractmethod
    def start(self, spec: VmSpec) -> GuestAddress:
        """Boot a guest and return how to reach it."""

    @abstractmethod
    def stop(self) -> None:
        """Shut the guest down and release its resources."""


class QemuDriver(VmDriver):
    """Plain qemu-system-x86_64, KVM when the host offers it, TCG otherwise."""

    name = "qemu"

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.spec: VmSpec | None = None

    # -- capability ---------------------------------------------------------

    def available(self) -> tuple[bool, str]:
        binary = shutil.which("qemu-system-x86_64")
        if not binary:
            return False, "qemu-system-x86_64 is not installed (apt install qemu-system-x86)"
        if Path("/dev/kvm").exists():
            return True, f"{binary} with KVM"
        return True, f"{binary} without KVM (TCG emulation: correct but slow)"

    def accel_flag(self, spec: VmSpec) -> list[str]:
        if spec.accel == "kvm":
            return ["-accel", "kvm"]
        if spec.accel == "tcg":
            return ["-accel", "tcg"]
        return ["-accel", "kvm" if Path("/dev/kvm").exists() else "tcg"]

    # -- lifecycle ----------------------------------------------------------

    def start(self, spec: VmSpec) -> GuestAddress:
        ok, detail = self.available()
        if not ok:
            raise ExecutorError(detail)
        self.spec = spec
        port = spec.ssh_port or _free_port()

        argv = [
            "qemu-system-x86_64",
            "-name", spec.name,
            "-m", spec.memory,
            "-smp", str(spec.cpus),
            *self.accel_flag(spec),
            "-nographic",
            "-serial", "null",
            "-monitor", "none",
            # User-mode networking: no root, no bridge, and a port forward is
            # all we need to reach sshd inside.
            "-netdev", f"user,id=net0,hostfwd=tcp::{port}-:22",
            "-device", "virtio-net-pci,netdev=net0",
        ]
        if spec.image:
            argv += ["-drive", f"file={spec.image},if=virtio,format={_disk_format(spec)}"]
        if spec.seed:
            # cloud-init reads this second disk on first boot: that is how the
            # guest learns the ssh key we are about to connect with.
            argv += ["-drive", f"file={spec.seed},if=virtio,format=raw"]
        if spec.kernel:
            argv += ["-kernel", spec.kernel]
            if spec.initrd:
                argv += ["-initrd", spec.initrd]
            if spec.append:
                argv += ["-append", spec.append]
        if spec.rootfs:
            # virtio-9p is enough to hand a directory to the guest and needs no
            # daemon, unlike virtiofs.
            argv += ["-fsdev", f"local,id=rootfs,path={spec.rootfs},security_model=none",
                     "-device", "virtio-9p-pci,fsdev=rootfs,mount_tag=lbroot"]
        argv += spec.extra_args

        log.info("starting qemu guest %s (%s)", spec.name, detail)
        if "TCG" in detail:
            log.warning("no KVM on this host: the guest is emulated and will boot "
                        "several times slower than usual")
        self.process = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE)
        address = GuestAddress(kind="ssh", port=port, user=spec.ssh_user, key=spec.ssh_key)
        self._wait_for_ssh(address, spec.boot_timeout)
        return address

    def stop(self) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.process = None

    # -- boot ---------------------------------------------------------------

    def _wait_for_ssh(self, address: GuestAddress, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                stderr = (self.process.stderr.read().decode("utf-8", "replace")
                          if self.process.stderr else "")
                raise ExecutorError(f"qemu exited during boot:\n{util.tail(stderr, 1500)}")
            # A bare TCP connect is not a readiness signal: qemu's user-mode
            # networking accepts on the forwarded port from the moment it starts
            # and only then resets. The SSH banner is the first byte that can
            # only come from a live sshd.
            if _ssh_banner(address.host, address.port):
                log.info("guest sshd is up on port %d", address.port)
                return
            time.sleep(3)
        self.stop()
        raise ExecutorError(
            f"the guest did not open ssh within {timeout}s. Its image needs an "
            "sshd that accepts the configured key; a cloud image with cloud-init "
            "is the usual way to get one.")


DRIVERS: dict[str, type[VmDriver]] = {"qemu": QemuDriver}


def resolve_driver_name(engine: str) -> str:
    """`--engine` is shared with the container backends, where it defaults to
    podman. On the vm backend anything that is not a VM driver means "the
    default one"."""
    return engine if engine in KNOWN_DRIVERS else "qemu"


def get_driver(name: str) -> VmDriver:
    if name in DRIVERS:
        return DRIVERS[name]()
    if name in KNOWN_DRIVERS:
        raise ExecutorError(
            f"the {name!r} VM driver is not implemented yet; only 'qemu' is. "
            "The VmDriver interface in this module is what it would plug into.")
    raise ExecutorError(f"unknown VM driver {name!r}; known: {', '.join(KNOWN_DRIVERS)}")


class VmExecutor(Executor):
    """Runs commands in a guest over ssh, so the rest of the tool is unchanged."""

    kind = "vm"

    def __init__(self, spec, tracer=None) -> None:  # type: ignore[no-untyped-def]
        super().__init__(spec, tracer)
        self.driver = get_driver(resolve_driver_name(spec.engine))
        self.address: GuestAddress | None = None
        self.label = f"vm:{self.driver.name}:{spec.name or 'guest'}"

    # -- lifecycle ----------------------------------------------------------

    def _start(self) -> None:
        if not shutil.which("ssh"):
            raise ExecutorError("the vm backend needs an ssh client on the host")
        vm = VmSpec(
            name=self.spec.name or "lazy-bootstrap",
            image=self.spec.image,
            rootfs=self.spec.rootfs,
            ssh_key=self.spec.env.get("LB_VM_SSH_KEY", ""),
            ssh_user=self.spec.env.get("LB_VM_SSH_USER", "root"),
            ssh_port=int(self.spec.env.get("LB_VM_SSH_PORT", "0") or 0),
            memory=self.spec.env.get("LB_VM_MEMORY", "2G"),
            cpus=int(self.spec.env.get("LB_VM_CPUS", "2") or 2),
            accel=self.spec.env.get("LB_VM_ACCEL", "auto"),
            seed=self.spec.env.get("LB_VM_SEED", ""),
            disk_format=self.spec.env.get("LB_VM_DISK_FORMAT", "auto"),
            boot_timeout=int(self.spec.env.get("LB_VM_BOOT_TIMEOUT", "300") or 300),
        )
        if not vm.image and not vm.rootfs:
            raise ExecutorError(
                "the vm backend needs a bootable disk image (--image) or a rootfs "
                "to share (--rootfs)")
        self.address = self.driver.start(vm)
        self.run("true", title="guest ready", step_prefix="session").check("guest start")

    def _close(self) -> None:
        self.driver.stop()

    # -- primitives ---------------------------------------------------------

    def _ssh_base(self) -> list[str]:
        assert self.address is not None
        argv = ["ssh", "-p", str(self.address.port),
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "LogLevel=ERROR",
                "-o", "BatchMode=yes"]
        if self.address.key:
            argv += ["-i", self.address.key]
        return argv + [f"{self.address.user}@{self.address.host}"]

    def _wrap(self, script: str, cwd: str | None, env: Mapping[str, str]) -> list[str]:
        prologue = f"cd {util.shell_join([cwd or self.spec.workdir])} 2>/dev/null || true\n"
        # LB_VM_* configure the hypervisor, not the guest: do not leak them in.
        guest_env = {k: v for k, v in env.items() if not k.startswith("LB_VM_")}
        inner = util.shell_join([*env_prefix(guest_env), "/bin/sh", "-c", prologue + script])
        return [*self._ssh_base(), inner]

    def upload(self, host_path: str | Path, target_path: str) -> None:
        self._scp(str(host_path), f"{self.address.user}@{self.address.host}:{target_path}")

    def download(self, target_path: str, host_path: str | Path) -> None:
        util.ensure_dir(Path(host_path).parent)
        self._scp(f"{self.address.user}@{self.address.host}:{target_path}", str(host_path))

    def _scp(self, src: str, dst: str) -> None:
        assert self.address is not None
        argv = ["scp", "-r", "-P", str(self.address.port),
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "LogLevel=ERROR"]
        if self.address.key:
            argv += ["-i", self.address.key]
        argv += [src, dst]
        step_id = self.tracer.next_id("copy")
        self.tracer.step(step_id, f"scp {src} -> {dst}", backend=self.label, wrapper=argv)
        proc = subprocess.run(argv, capture_output=True, text=True)
        self.tracer.result(step_id, proc.returncode, 0.0, proc.stdout + proc.stderr)
        if proc.returncode != 0:
            raise ExecutorError(f"scp failed: {proc.stderr.strip()}")


def _disk_format(spec: VmSpec) -> str:
    if spec.disk_format != "auto":
        return spec.disk_format
    proc = subprocess.run(["qemu-img", "info", "--output=json", spec.image],
                          capture_output=True, text=True)
    if proc.returncode == 0:
        import json

        try:
            return str(json.loads(proc.stdout)["format"])
        except (ValueError, KeyError):
            pass
    return "raw" if spec.image.endswith((".raw", ".img")) else "qcow2"


def _ssh_banner(host: str, port: int, timeout: float = 4.0) -> bool:
    """True when something on `port` greets us with an SSH identification string."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            return sock.recv(4).startswith(b"SSH-")
    except OSError:
        return False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def probe() -> dict[str, dict[str, str]]:
    """Capability report for `doctor`, one entry per known driver."""
    out: dict[str, dict[str, str]] = {}
    for name in KNOWN_DRIVERS:
        if name not in DRIVERS:
            out[name] = {"available": "no", "detail": "not implemented (see VmDriver)"}
            continue
        ok, detail = DRIVERS[name]().available()
        out[name] = {"available": "yes" if ok else "no", "detail": detail}
    return out
