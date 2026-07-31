"""Building the worker environment, on any execution class (docs/SPECS.md D-26).

A *worker* is an environment that already has the build machinery and a
toolchain: the thing a rebuild would otherwise have to construct on every run.

The point of this module is that building one is the *same* work regardless of
where it happens. Preparing a builder and provisioning a toolchain are already
expressed as shell over an Executor, so a worker can be produced by a container
engine, inside a chroot, inside bubblewrap, or on the host - and the result can
be persisted as an OCI image, as a rootfs directory, or as a tarball,
independently of how it was built.

    build class            persist as
    -----------            ----------
    oci (podman/docker)    image  (engine commit) | dir | tar
    chroot/bwrap/firejail  dir    (the rootfs)    | tar | image (engine import)
    host                   dir    (only with an explicit --save dir:PATH)
"""

from __future__ import annotations

import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import toolchains
from .orchestration import util
from .config import RunConfig, ToolchainConfig
from .orchestration.logs import get

log = get("worker")

FLAVOURS = Path(__file__).resolve().parents[2] / "ci" / "flavours.toml"


class WorkerError(RuntimeError):
    pass


@dataclass
class Flavour:
    """One row of ci/flavours.toml: distro x toolchain, plus how to get there."""

    name: str
    distro: str = ""
    base: str = ""
    toolchain: str = "gcc"
    provision: list[str] = field(default_factory=list)
    fallback: str = ""
    args: dict[str, str] = field(default_factory=dict)
    description: str = ""
    default: bool = False
    mixed: bool = False

    def toolchain_config(self) -> ToolchainConfig:
        return ToolchainConfig(id=self.toolchain, provision=list(self.provision))


def load_flavours(path: Path | None = None) -> list[Flavour]:
    source = path or FLAVOURS
    if not source.exists():
        return []
    raw = tomllib.loads(source.read_text(encoding="utf-8"))
    return [Flavour(**row) for row in raw.get("flavour", [])]


def find_flavour(name: str, path: Path | None = None) -> Flavour:
    for flavour in load_flavours(path):
        if flavour.name == name:
            return flavour
    known = ", ".join(f.name for f in load_flavours(path))
    raise WorkerError(f"unknown flavour {name!r}; known: {known}")


@dataclass
class SaveTarget:
    """Where the finished worker goes: image:<tag> | dir:<path> | tar:<file>."""

    kind: str = ""
    value: str = ""

    @classmethod
    def parse(cls, spec: str) -> "SaveTarget":
        if not spec:
            return cls()
        kind, _, value = spec.partition(":")
        if kind not in ("image", "dir", "tar"):
            raise WorkerError(f"unknown save target {kind!r}; use image:, dir: or tar:")
        if not value:
            raise WorkerError(f"{kind}: needs a value")
        return cls(kind=kind, value=value)


@dataclass
class WorkerResult:
    flavour: str
    backend: str
    toolchain: str
    toolchain_version: str
    toolchain_source: str
    saved_as: str = ""
    ok: bool = False
    detail: str = ""


class WorkerBuilder:
    """Constructs a worker environment using whichever class the run asked for."""

    def __init__(self, engine) -> None:  # engine: engine.Engine, avoid a cycle
        self.engine = engine

    def build(self, flavour: Flavour, save: SaveTarget, probe: bool = True) -> WorkerResult:
        config: RunConfig = self.engine.config
        name = f"lb-worker-{util.slugify(flavour.name)}"
        log.info("building worker %s on backend %s", flavour.name, config.backend)

        env = self.engine.open_environment(name)
        try:
            # Step 1: the distro's own build machinery, from ci/system-deps.
            ctx = self.engine._context(env, {}, unit=name)
            steps = env.distro.prepare_builder(env.executor, ctx)
            broken = [s for s in steps if s.rc != 0 and s.fatal]
            if broken:
                raise WorkerError(f"preparing the builder failed at {broken[0].name}:\n"
                                  f"{util.tail(broken[0].output, 1500)}")

            # Step 2: the toolchain, exactly as a rebuild would provision it.
            tc_config = flavour.toolchain_config()
            driver = toolchains.get(tc_config)
            install = driver.provision(env.executor, env.distro.id, env.facts,
                                       self.engine.fetcher, unit=name, sysdeps=env.sysdeps)
            detail = ""
            ok = True
            if probe:
                ok, detail = driver.probe(env.executor, install, unit=name, workdir=self.engine.workdir)
                if not ok:
                    raise WorkerError(f"the worker's toolchain does not work:\n{detail}")

            # Step 3: leave a manifest inside, so the image can describe itself.
            env.executor.write_text(
                "/opt/lazy-bootstrap/worker.json",
                _manifest(flavour, config, install, env.facts))

            result = WorkerResult(
                flavour=flavour.name, backend=config.backend, toolchain=tc_config.id,
                toolchain_version=install.version, toolchain_source=install.source,
                ok=ok, detail=detail,
            )
            if save.kind:
                result.saved_as = self._save(env, save, flavour)
            return result
        finally:
            # A saved worker outlives the session; an unsaved one does not.
            env.close()

    # -- persistence --------------------------------------------------------

    def _save(self, env, save: SaveTarget, flavour: Flavour) -> str:
        backend = self.engine.config.backend
        if save.kind == "image":
            return self._save_image(env, save.value, backend)
        if save.kind == "dir":
            return self._save_dir(env, save.value, backend)
        return self._save_tar(env, save.value, backend)

    def _save_image(self, env, tag: str, backend: str) -> str:
        engine_bin = self.engine.config.engine
        if backend in ("oci", "podman", "docker"):
            # Commit the live container: cheapest and keeps the image metadata.
            self._run([engine_bin, "commit", env.executor.container, tag],
                      f"commit worker as {tag}")
            return f"image:{tag}"
        # Rootfs backends: tar the directory and import it as a single layer.
        rootfs = Path(env.executor.spec.rootfs)
        if not rootfs.is_dir():
            raise WorkerError(f"cannot save an image from backend {backend}: no rootfs")
        log.info("importing %s into %s as %s", rootfs, engine_bin, tag)
        tar = subprocess.Popen(["tar", "-C", str(rootfs), "-c", "."], stdout=subprocess.PIPE)
        importer = subprocess.Popen([engine_bin, "import", "-", tag], stdin=tar.stdout)
        if tar.stdout:
            tar.stdout.close()
        rc = importer.wait()
        tar.wait()
        if rc != 0:
            raise WorkerError(f"{engine_bin} import failed (rc={rc})")
        return f"image:{tag}"

    def _save_dir(self, env, path: str, backend: str) -> str:
        destination = util.ensure_dir(path)
        if backend in ("oci", "podman", "docker"):
            engine_bin = self.engine.config.engine
            export = subprocess.Popen([engine_bin, "export", env.executor.container],
                                      stdout=subprocess.PIPE)
            untar = subprocess.Popen(["tar", "-x", "-C", str(destination)], stdin=export.stdout)
            if export.stdout:
                export.stdout.close()
            rc = untar.wait()
            export.wait()
            if rc != 0:
                raise WorkerError(f"exporting the worker failed (rc={rc})")
        else:
            rootfs = Path(env.executor.spec.rootfs)
            if not rootfs.is_dir():
                raise WorkerError(f"backend {backend} has no rootfs to save; "
                                  "use --backend chroot/bwrap/firejail or an OCI engine")
            if rootfs.resolve() != destination.resolve():
                self._run(["cp", "-a", f"{rootfs}/.", str(destination)],
                          f"copy worker rootfs to {destination}")
        return f"dir:{destination}"

    def _save_tar(self, env, path: str, backend: str) -> str:
        target = Path(path)
        util.ensure_dir(target.parent)
        if backend in ("oci", "podman", "docker"):
            engine_bin = self.engine.config.engine
            with open(target, "wb") as fh:
                proc = subprocess.run([engine_bin, "export", env.executor.container], stdout=fh)
            if proc.returncode != 0:
                raise WorkerError(f"exporting the worker failed (rc={proc.returncode})")
        else:
            rootfs = Path(env.executor.spec.rootfs)
            if not rootfs.is_dir():
                raise WorkerError(f"backend {backend} has no rootfs to tar")
            self._run(["tar", "-C", str(rootfs), "-cf", str(target), "."],
                      f"tar worker rootfs to {target}")
        return f"tar:{target}"

    # -- plumbing -----------------------------------------------------------

    def _run(self, argv: list[str], title: str) -> None:
        step_id = self.engine.tracer.next_id("worker")
        self.engine.tracer.step(step_id, title, wrapper=argv)
        proc = subprocess.run(argv, capture_output=True, text=True)
        self.engine.tracer.result(step_id, proc.returncode, 0.0, proc.stdout + proc.stderr)
        if proc.returncode != 0:
            raise WorkerError(f"{title} failed:\n{util.tail(proc.stderr, 1200)}")


def _manifest(flavour: Flavour, config: RunConfig, install, facts: dict[str, str]) -> str:
    import json

    return json.dumps({
        "flavour": flavour.name,
        "built_by": "lazy-bootstrap",
        "built_at": util.now_iso(),
        "built_on": config.backend,
        "distro": facts.get("id", ""),
        "distro_version": facts.get("version", ""),
        "arch": facts.get("arch", ""),
        "libc": facts.get("libc", ""),
        "toolchain": {
            "id": install.toolchain_id,
            "kind": install.kind,
            "version": install.version,
            "source": install.source,
            "cc": install.cc,
            "cxx": install.cxx,
            "notes": install.notes,
        },
    }, indent=2) + "\n"
