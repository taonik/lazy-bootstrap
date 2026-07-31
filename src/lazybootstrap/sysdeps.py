"""Declared system dependencies of a build environment (docs/SPECS.md D-24).

The packages a flavour needs live in `ci/system-deps/<family>/<component>.txt`,
never in the driver code. The same files are consumed by the CI Containerfiles,
so an image built by `ci/build-images.sh` and an environment prepared on the fly
by `lazy-bootstrap` install exactly the same set.

Installing packages is not always allowed: on the `host` backend it would modify
the developer's machine, so it takes an explicit opt-in (`--system-deps host`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .executors.base import CommandResult, Executor
from .logs import get

log = get("sysdeps")

#: policy values for RunConfig.system_deps
OFF = "off"        # never install; report what is missing
TARGET = "target"  # install inside container/sandbox environments (default)
HOST = "host"      # also allow it when the backend *is* the host (invasive)

POLICIES = (OFF, TARGET, HOST)

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "ci" / "system-deps"

#: map a distro driver id to the system-deps family directory
FAMILIES = {"debian": "debian", "ubuntu": "debian", "alpine": "alpine"}


def load(family: str, component: str, root: Path | None = None) -> list[str]:
    """Read one dependency list. Missing files mean "no extra packages"."""
    path = (root or DEFAULT_ROOT) / FAMILIES.get(family, family) / f"{component}.txt"
    if not path.exists():
        log.debug("no system-deps file at %s", path)
        return []
    packages: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            packages.append(entry)
    return packages


@dataclass
class SysDeps:
    """Installs declared dependencies into one environment, if policy allows."""

    executor: Executor
    family: str
    backend: str
    policy: str = TARGET
    root: Path | None = None
    unit: str = ""
    installed: set[str] = field(default_factory=set)
    skipped: list[str] = field(default_factory=list)

    # -- policy -------------------------------------------------------------

    @property
    def allowed(self) -> bool:
        if self.policy == OFF:
            return False
        if self.backend == "host":
            # Touching the machine you are sitting at is opt-in, always.
            return self.policy == HOST
        return True

    def why_not(self) -> str:
        if self.policy == OFF:
            return "--system-deps off"
        if self.backend == "host" and self.policy != HOST:
            return ("backend is 'host' and --system-deps is not 'host': refusing to "
                    "install packages on this machine")
        return ""

    # -- installation -------------------------------------------------------

    def packages(self, component: str) -> list[str]:
        return load(self.family, component, self.root)

    def ensure(self, component: str) -> tuple[bool, str]:
        """Install `component`'s packages. Returns (ok, detail).

        `ok` is False only when something was genuinely needed and could not be
        installed; a policy that forbids installing is reported but not fatal,
        because the packages may already be present.
        """
        wanted = [p for p in self.packages(component) if p not in self.installed]
        if not wanted:
            return True, ""

        if not self.allowed:
            reason = self.why_not()
            note = f"system-deps for '{component}' not installed ({reason}): {' '.join(wanted)}"
            log.warning("%s", note)
            self.skipped.append(note)
            return True, note

        result = self._install(wanted)
        if result.ok:
            self.installed.update(wanted)
            log.debug("installed system-deps for %s: %s", component, " ".join(wanted))
            return True, ""
        detail = result.output.strip()[-1200:]
        log.warning("could not install system-deps for %s: %s", component, detail[-300:])
        return False, detail

    def _install(self, packages: list[str]) -> CommandResult:
        joined = " ".join(packages)
        if FAMILIES.get(self.family) == "alpine":
            script = f"apk add --no-cache {joined}"
        else:
            script = ("apt-get update -o Acquire::Retries=3 >/dev/null || true\n"
                      f"apt-get install -y --no-install-recommends {joined}")
        return self.executor.run(
            script, title=f"system-deps: {joined}",
            env={"DEBIAN_FRONTEND": "noninteractive"}, timeout=1800,
            unit=self.unit, step_prefix="sysdeps",
        )

    # -- diagnostics --------------------------------------------------------

    def missing(self, component: str) -> list[str]:
        """Which declared commands/packages are still absent - used in reports."""
        return [p for p in self.packages(component) if p not in self.installed]
