"""Turn an inventory into build units.

A *unit* is one build environment. Three groupings, all useful for a different
reason (D-19):

    all      one environment for everything - fastest, dependencies accumulate
    group    N sources per environment      - a middle ground for large images
    package  one environment per source     - full isolation, best for bisecting
                                              a toolchain failure

Filtering happens here too, so `rebuild` and `inventory` agree on what "the
package list" means.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

from .orchestration import util
from .config import RunConfig
from .model import PackageRef


@dataclass
class BuildUnit:
    """One environment and the source packages it will build."""

    name: str
    sources: list[str] = field(default_factory=list)
    packages: dict[str, list[PackageRef]] = field(default_factory=dict)

    @property
    def binary_count(self) -> int:
        return sum(len(v) for v in self.packages.values())


@dataclass
class Plan:
    units: list[BuildUnit]
    selected: list[PackageRef]
    skipped: list[PackageRef]
    grouping: str

    @property
    def source_count(self) -> int:
        return sum(len(u.sources) for u in self.units)


def select(packages: list[PackageRef], config: RunConfig) -> tuple[list[PackageRef], list[PackageRef]]:
    """Apply the include/exclude/limit filters. Returns (selected, skipped)."""
    selected: list[PackageRef] = []
    skipped: list[PackageRef] = []

    explicit = set(config.packages)
    for package in packages:
        if explicit and package.name not in explicit and package.source not in explicit:
            skipped.append(package)
            continue
        if config.include and not _matches(package.name, config.include):
            skipped.append(package)
            continue
        if config.exclude and _matches(package.name, config.exclude):
            skipped.append(package)
            continue
        selected.append(package)

    # `limit` counts source packages, not binaries: limiting to 5 should mean
    # five builds, not five binaries that may come from one source.
    if config.limit:
        keep: set[str] = set()
        limited: list[PackageRef] = []
        for package in selected:
            if package.source not in keep and len(keep) >= config.limit:
                skipped.append(package)
                continue
            keep.add(package.source)
            limited.append(package)
        selected = limited
    return selected, skipped


def plan(packages: list[PackageRef], config: RunConfig) -> Plan:
    selected, skipped = select(packages, config)

    by_source: dict[str, list[PackageRef]] = {}
    for package in selected:
        by_source.setdefault(package.source, []).append(package)
    sources = sorted(by_source)

    grouping = config.grouping
    if grouping == "package":
        units = [
            BuildUnit(name=util.slugify(source), sources=[source],
                      packages={source: by_source[source]})
            for source in sources
        ]
    elif grouping == "group":
        units = []
        for index, chunk in enumerate(util.chunked(sources, config.group_size), start=1):
            units.append(BuildUnit(
                name=f"group-{index:02d}",
                sources=list(chunk),
                packages={s: by_source[s] for s in chunk},
            ))
    else:  # "all"
        units = [BuildUnit(name="all", sources=sources,
                           packages={s: by_source[s] for s in sources})]

    return Plan(units=units, selected=selected, skipped=skipped, grouping=grouping)


def _matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)
