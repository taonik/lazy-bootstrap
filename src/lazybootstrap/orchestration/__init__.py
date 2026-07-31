"""Execution-environment orchestration.

**This package is deliberately independent** (docs/SPECS.md D-27). It knows how
to obtain and run an environment - host, chroot, bubblewrap, firejail, an OCI
container, a VM - and knows nothing whatsoever about rebuilding packages. It is
written to be lifted into a repository of its own and used as the interface by
other tools and scripts.

The contract is small on purpose:

    from lazybootstrap.orchestration import Orchestrator, EnvironmentRequest

    orch = Orchestrator(cache_dir="/var/cache/lazy-bootstrap")
    request = EnvironmentRequest(backend="chroot", rootfs="hostfs:overlay",
                                 rootfs_path="temp", acquire="auto")

    print(orch.available(request).summary())   # no side effects
    handle = orch.open(request)                # acquires if the policy allows
    handle.executor.run("gcc --version")
    orch.close(handle)

A caller says *what it needs* and *what it is willing to let the orchestrator do
to get there* (`acquire`), never *how*. There is no separate "build the images
first" phase: `open()` performs whatever `available()` reported, within the
policy. Pre-warming is possible (`lazy-bootstrap env ensure`) but it is an
optimisation, not a required step.

Rule for contributors: nothing under `orchestration/` may import from the rest
of `lazybootstrap`. If something here needs domain knowledge, the design is
wrong - pass it in through EnvironmentRequest instead.
"""

from __future__ import annotations

from . import executors, images, logs, rootfs, trace, util
from .executors import Executor, ExecutorError, ExecutorSpec, create
from .executors import probe as probe_backends
from .images import ImageStore, apply_mirrors, normalise
from .orchestrator import Orchestrator
from .rootfs import RootfsProvider, RootfsSpec, resolve_workdir
from .spec import (AUTO, BUILD, DOWNLOAD, POLICIES, REQUIRE, Availability,
                   EnvironmentHandle, EnvironmentRequest, OrchestrationError)
from .trace import Tracer

__all__ = [
    # the contract
    "Orchestrator",
    "EnvironmentRequest",
    "EnvironmentHandle",
    "Availability",
    "OrchestrationError",
    # acquisition policies
    "AUTO", "BUILD", "DOWNLOAD", "REQUIRE", "POLICIES",
    # lower-level pieces, for callers that want them
    "Executor", "ExecutorError", "ExecutorSpec", "create", "probe_backends",
    "ImageStore", "apply_mirrors", "normalise",
    "RootfsProvider", "RootfsSpec", "resolve_workdir",
    "Tracer",
    # submodules
    "executors", "images", "logs", "rootfs", "trace", "util",
]
