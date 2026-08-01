"""Resource limits, per phase, and an honest account of what each backend enforces.

A rebuild has three phases with genuinely different appetites:

    download   network-bound, wants concurrency, wants little CPU
    build      CPU and memory-bound, and where a runaway package hurts
    test       can hang forever, can want a device (GPU, /dev/kvm)

So limits are set per phase, with a global default underneath:

    --limit cpus=4 --limit memory=8G          both phases
    --limit build.memory=16G                  build only
    --limit test.time=600 --limit device=/dev/dri

## What is actually enforced, and where it is not

This is the part worth being blunt about, because a limit that is silently
ignored is worse than no limit: it is a limit the user believes in.

    time      exact, per phase, everywhere - it is our own timeout
    cpus      container backends only, at environment level (see below)
    memory    container backends only, at environment level
    devices   container backends only
    jobs      everywhere - it is just MAKEFLAGS

`podman exec` cannot change cgroup limits for a single command, and this tool
runs one long-lived container per environment with each step as an exec. So
cpu and memory are applied when the environment is created, using the largest
value any phase asks for; a phase asking for less does not get its own smaller
cap. `describe_enforcement()` says so rather than leaving the user to find out.
Where a backend enforces nothing - host, chroot - that is reported once, not
hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .orchestration.logs import get

log = get("limits")

PHASES = ("download", "build", "test")

#: Keys accepted by --limit. Anything else is a typo, and a typo in a resource
#: limit must not be silently discarded.
KEYS = ("cpus", "memory", "time", "jobs", "device")

#: Backends that can enforce cpu/memory/device caps at all.
_ENFORCING = ("oci", "podman", "docker")


def parse_size(text: str) -> int:
    """`8G`, `512M`, `1024` -> bytes. Raises on nonsense rather than guessing."""
    raw = str(text).strip().upper().rstrip("B")
    if not raw:
        raise ValueError("empty size")
    scale = 1
    if raw[-1] in "KMGT":
        scale = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[raw[-1]]
        raw = raw[:-1]
    value = float(raw)
    if value < 0:
        raise ValueError(f"negative size: {text}")
    return int(value * scale)


@dataclass
class PhaseLimits:
    """What one phase may use. None means "no limit from us"."""

    cpus: float | None = None
    memory: int | None = None
    time: int | None = None
    jobs: int | None = None
    devices: list[str] = field(default_factory=list)

    def merged_over(self, base: "PhaseLimits") -> "PhaseLimits":
        """This phase's settings, falling back to the global default."""
        return PhaseLimits(
            cpus=self.cpus if self.cpus is not None else base.cpus,
            memory=self.memory if self.memory is not None else base.memory,
            time=self.time if self.time is not None else base.time,
            jobs=self.jobs if self.jobs is not None else base.jobs,
            devices=list(dict.fromkeys([*base.devices, *self.devices])),
        )


@dataclass
class Limits:
    """Global defaults plus per-phase overrides."""

    default: PhaseLimits = field(default_factory=PhaseLimits)
    phases: dict[str, PhaseLimits] = field(default_factory=dict)

    def for_phase(self, phase: str) -> PhaseLimits:
        return self.phases.get(phase, PhaseLimits()).merged_over(self.default)

    # -- what the environment must be created with --------------------------

    def envelope(self) -> PhaseLimits:
        """The widest limits any phase needs.

        A single long-lived container serves every phase, and `exec` cannot
        narrow a cgroup, so the container is created with the maximum. A phase
        that asked for less is not separately capped - see
        `describe_enforcement`.
        """
        cpus = [p.cpus for p in self._all() if p.cpus is not None]
        memory = [p.memory for p in self._all() if p.memory is not None]
        devices: list[str] = []
        for p in self._all():
            devices.extend(p.devices)
        return PhaseLimits(
            cpus=max(cpus) if cpus else None,
            memory=max(memory) if memory else None,
            devices=list(dict.fromkeys(devices)),
        )

    def _all(self) -> list[PhaseLimits]:
        return [self.for_phase(phase) for phase in PHASES]

    def describe_enforcement(self, backend: str) -> list[str]:
        """Every way the request will not be honoured, in plain words."""
        notes: list[str] = []
        envelope = self.envelope()
        wants_container_caps = (envelope.cpus is not None
                                or envelope.memory is not None
                                or envelope.devices)
        if wants_container_caps and backend not in _ENFORCING:
            notes.append(
                f"backend {backend!r} cannot enforce cpu, memory or device "
                f"limits; only time and job limits will apply")
            return notes
        for phase in PHASES:
            limits = self.for_phase(phase)
            if limits.cpus is not None and envelope.cpus is not None \
                    and limits.cpus < envelope.cpus:
                notes.append(
                    f"{phase}: asked for {limits.cpus} cpus but the environment "
                    f"is created with {envelope.cpus} - exec cannot narrow a "
                    f"cgroup, so this phase is not separately capped")
            if limits.memory is not None and envelope.memory is not None \
                    and limits.memory < envelope.memory:
                notes.append(
                    f"{phase}: asked for {limits.memory} bytes but the "
                    f"environment is created with {envelope.memory}")
        return notes

    def container_args(self, backend: str) -> list[str]:
        """Flags for `podman|docker create`. Empty where nothing is enforceable."""
        if backend not in _ENFORCING:
            return []
        envelope = self.envelope()
        args: list[str] = []
        if envelope.cpus is not None:
            args += [f"--cpus={envelope.cpus:g}"]
        if envelope.memory is not None:
            args += [f"--memory={envelope.memory}"]
        for device in envelope.devices:
            args += ["--device", device]
        return args


def parse(specs: list[str] | None) -> Limits:
    """Turn `["cpus=4", "build.memory=16G", "device=/dev/dri"]` into `Limits`.

    An unknown key or an unknown phase is an error. Resource limits are exactly
    the place where quietly ignoring a typo does damage: the run proceeds
    unbounded while the user believes it is capped.
    """
    limits = Limits()
    for spec in specs or ():
        if "=" not in spec:
            raise ValueError(f"--limit expects key=value, got {spec!r}")
        key, _, value = spec.partition("=")
        key = key.strip()
        phase = ""
        if "." in key:
            phase, _, key = key.partition(".")
            if phase not in PHASES:
                raise ValueError(
                    f"unknown phase {phase!r} in {spec!r}; expected one of "
                    f"{', '.join(PHASES)}")
        if key not in KEYS:
            raise ValueError(
                f"unknown limit {key!r} in {spec!r}; expected one of "
                f"{', '.join(KEYS)}")
        target = (limits.phases.setdefault(phase, PhaseLimits())
                  if phase else limits.default)
        value = value.strip()
        if key == "cpus":
            target.cpus = float(value)
        elif key == "memory":
            target.memory = parse_size(value)
        elif key == "time":
            target.time = int(value)
        elif key == "jobs":
            target.jobs = int(value)
        elif key == "device":
            target.devices.append(value)
    return limits


__all__ = ["Limits", "PhaseLimits", "parse", "parse_size", "PHASES", "KEYS"]
