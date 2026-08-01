"""Distro driver interface.

A driver answers four questions about a running environment:

  1. what is installed here?                 -> inventory()
  2. how do I make this a build machine?     -> prepare_builder()
  3. how do I get the source of a package?   -> fetch_source()
  4. how do I build it?                      -> build()

Everything is expressed as POSIX shell run through an Executor (D-02), so the
same driver works on host, sandbox, container and VM without changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..orchestration.executors.base import CommandResult, Executor
from ..model import PackageRef, StepLog


@dataclass
class BuildContext:
    """Per-unit build settings handed to the driver."""

    workdir: str = "/build"
    env: dict[str, str] = field(default_factory=dict)   # toolchain env (CC, PATH, ...)
    jobs: int = 0                                        # 0 -> nproc
    timeout: int = 1800
    unit: str = "unit"
    extra_configure: list[str] = field(default_factory=list)
    source_mirror: str = ""                              # archive URL override (D-20)
    keep_sources: bool = False
    #: sysdeps.SysDeps for this environment; None disables declared deps (D-24)
    sysdeps: object | None = None
    #: Run each package's own test suite. On by default: a package that builds
    #: but fails its tests has not been shown to rebuild correctly, and hiding
    #: that behind a default would overstate every result this tool produces.
    run_check: bool = True
    #: Seconds allowed for the test phase specifically; 0 -> use `timeout`.
    check_timeout: int = 0


@dataclass
class SourceTree:
    """Where a package's source ended up, and what it is called."""

    name: str
    version: str
    path: str
    kind: str = ""     # dsc | aports | tarball


class Distro(ABC):
    id: str = "abstract"
    #: human name of the package manager, used in reports
    package_manager: str = ""

    # -- detection ----------------------------------------------------------

    @staticmethod
    @abstractmethod
    def detect(executor: Executor) -> bool:
        """True when this driver understands the environment."""

    @abstractmethod
    def describe(self, executor: Executor) -> dict[str, str]:
        """os-release style facts: id, version, libc, arch."""

    # -- inventory ----------------------------------------------------------

    @abstractmethod
    def inventory(self, executor: Executor) -> list[PackageRef]:
        """Installed packages, as reported by the package manager itself (D-09)."""

    def source_units(self, packages: list[PackageRef]) -> dict[str, list[PackageRef]]:
        """Group binary packages by their source package (D-10)."""
        units: dict[str, list[PackageRef]] = {}
        for package in packages:
            units.setdefault(package.source, []).append(package)
        return units

    # -- build machine ------------------------------------------------------

    @abstractmethod
    def prepare_builder(self, executor: Executor, ctx: BuildContext) -> list[StepLog]:
        """Install the toolchain-independent build machinery and enable sources."""

    @abstractmethod
    def install_build_deps(self, executor: Executor, source: str,
                           ctx: BuildContext) -> CommandResult:
        """Install the build dependencies of one source package."""

    @abstractmethod
    def fetch_source(self, executor: Executor, source: str,
                     ctx: BuildContext) -> tuple[SourceTree | None, CommandResult]:
        """Download and unpack one source package under ctx.workdir."""

    @abstractmethod
    def build(self, executor: Executor, tree: SourceTree,
              ctx: BuildContext) -> CommandResult:
        """Build the unpacked source tree with ctx.env applied."""

    @abstractmethod
    def collect_artifacts(self, executor: Executor, tree: SourceTree,
                          ctx: BuildContext) -> list[str]:
        """Paths of the produced packages, relative to the environment."""

    # -- helpers shared by drivers -----------------------------------------

    @staticmethod
    def jobs_expr(ctx: BuildContext) -> str:
        return str(ctx.jobs) if ctx.jobs else "$(nproc 2>/dev/null || echo 2)"

    @staticmethod
    def step(name: str, result: CommandResult, limit: int = 4000,
             fatal: bool = True) -> StepLog:
        from ..orchestration import util

        return StepLog(
            name=name,
            rc=result.rc,
            seconds=round(result.seconds, 2),
            command=result.argv[-1] if result.argv else "",
            output=util.tail(result.output, limit),
            fatal=fatal,
        )
