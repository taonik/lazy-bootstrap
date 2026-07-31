# lazy-bootstrap

Rebuild every package of a distro image from source, with a toolchain of your
choice, and compare the results.

Point it at an image, and it reads the package list from that image's own
package manager, fetches each source package, rebuilds it, and tells you what
worked. Swap `gcc` for `clang` or [Fil-C](https://fil-c.org/) and the same run
becomes a toolchain comparison.

```console
$ ./lazy-bootstrap rebuild debian:13-slim --toolchain gcc --toolchain filc-0.681
...
package                  gcc           filc-0.681
--------------------------------------------------
hostname                 ok            ok
zlib                     ok            failed

filc-0.681 vs gcc:
  regressions (1): zlib
  fixes       (0): none
```

The design decisions, and the alternatives rejected along the way, are recorded
in [docs/SPECS.md](docs/SPECS.md).

---

## Quick start

No installation, no dependencies beyond Python 3.11:

```console
$ ./lazy-bootstrap doctor                 # what can this machine do?
$ ./lazy-bootstrap inventory debian:13-slim
$ ./lazy-bootstrap rebuild debian:13-slim --package hostname
$ ./lazy-bootstrap report runs/<id> --format html -o report.html
```

Behind a proxy or an allowlist, point the image pull at a mirror:

```console
$ ./lazy-bootstrap rebuild debian:13-slim --registry-mirror docker.io=mirror.gcr.io
```

## Where it runs

*How* a command runs and *where its filesystem comes from* are two separate
choices, and any combination is valid.

| `--backend` | isolation | needs |
|---|---|---|
| `host` | none | nothing |
| `chroot` | filesystem only | root |
| `bwrap` | namespaces | bubblewrap |
| `firejail` | namespaces | firejail with `chroot yes` |
| `oci` / `podman` / `docker` | full | a container engine |
| `vm` / `qemu` | a whole kernel | qemu (KVM optional), ssh, a bootable disk image |

| `--rootfs` | filesystem source |
|---|---|
| `image[:REF]` | unpacked from the local engine pool (default) |
| `hostfs[:overlay\|bind\|copy]` | derived from this machine; `overlay` is copy-on-write |
| `dir:PATH` | a directory somebody else prepared (`--rootfs-prepare CMD` to build it) |
| `none` | no rootfs — the `host` backend |

```console
# rebuild against this machine, in a copy-on-write overlay that discards writes
$ ./lazy-bootstrap rebuild --backend chroot --rootfs hostfs:overlay --rootfs-path temp

# use a rootfs produced by something else entirely
$ ./lazy-bootstrap rebuild --backend bwrap \
    --rootfs dir:/srv/rootfs --rootfs-prepare 'debootstrap trixie "$LB_ROOTFS"'
```

`--rootfs-path` and `--workdir` both accept a path or the literal `temp` for a
throwaway one.

## Toolchains

| id | what it is | how it is obtained |
|---|---|---|
| `gcc` | the distro's own compiler, and the baseline | already installed, else the package manager |
| `llvm` | whatever clang the distro ships | package manager |
| `llvm-20.1.8` | exactly that version | distro package if it really is that version, else the upstream release tarball |
| `filc-0.681` | Fil-C, memory-safe C/C++ on clang 20.1.8 | GitHub release |

A run refuses to substitute a nearby version for a pinned one: a comparison
that silently used 20.1.2 instead of 20.1.8 would be worse than no comparison.

**Fil-C** publishes two artefacts, and they are not interchangeable: `filc-*`
(the "pizfix" build, musl, self-contained) and `optfil-*` (`/opt/fil`, glibc).
`variant = "auto"` picks by the target's libc. Alpine with `libc6-compat` is
still musl — gcompat is a shim, not glibc — so it gets the musl build.

### How a toolchain reaches the build

A directory of wrapper scripts goes first in `PATH`, so even a hand-written
Makefile calling `gcc` ends up in the toolchain you asked for. The same wrappers
are where extra flags are injected — and where flags a toolchain *cannot honour*
are removed. Fil-C ships no LTO plugin, so Debian's default `-flto=auto
-ffat-lto-objects` would fail every link; the shim drops them and says so in the
report.

```console
$ LB_SHIM_TRACE=1 ./lazy-bootstrap rebuild ... --debug
[shim] /opt/lazy-bootstrap/shim/bin/cc -O2 -flto=auto -c foo.c
[shim] dropped -flto=auto
```

## Reporting

Every run writes one canonical `report.json`; every other format is rendered
from it, months later if you like.

```console
$ ./lazy-bootstrap report runs/<id> --format md
$ ./lazy-bootstrap report runs/<id> --format html -o report.html   # self-contained
$ ./lazy-bootstrap compare runs/a runs/b --format html -o compare.html
```

The comparison answers the question that actually matters — *what broke* —
classifying every package as a regression, a fix, or stable.

Statuses are distinguished on purpose: `failed` (the code did not build) is not
the same as `blocked` (the network would not let us fetch it) or `nosource` (no
source package exists). Conflating them would poison a toolchain comparison.

## The orchestration layer

`src/lazybootstrap/orchestration/` is independent of everything else here — it
knows how to obtain and run an environment and nothing about rebuilding
packages. It is written to become a repository of its own, and a test enforces
that it never imports the rest of the project.

```python
from lazybootstrap.orchestration import Orchestrator, EnvironmentRequest

orch = Orchestrator()
req = EnvironmentRequest(backend="chroot", rootfs="hostfs:overlay", acquire="auto")

print(orch.available(req).summary())   # no side effects
handle = orch.open(req)                # acquires it, if the policy allows
handle.executor.run("gcc --version")
orch.close(handle)
```

A caller says *what it needs* and *what the orchestrator may do to get there* —
never *how*. There is no separate "build the images first" phase: `rebuild`
acquires what it needs under the same policy.

| `--acquire` | the orchestrator may |
|---|---|
| `require` (`--no-download-image`) | only use what is already present |
| `download` (`--download-image`) | pull from a registry |
| `build` | build locally from a recipe |
| `auto` (default) | pull if missing; not build |

The same thing from the shell:

```console
$ ./lazy-bootstrap env probe                       # what can this machine do
$ ./lazy-bootstrap env available debian:13-slim --backend podman
request : podman:debian:13-slim
policy  : --acquire auto
status  : missing, would pull: mirror.gcr.io/library/debian:13-slim is not in the local podman store
$ ./lazy-bootstrap env ensure debian:13-slim       # pre-warm (optional)
```

## Worker environments

A *worker* is an environment that already has the build machinery and a
toolchain. Building one is the same work wherever it happens, so it can be done
on any class and saved in any form:

```console
$ ./lazy-bootstrap build-worker --list
$ ./lazy-bootstrap build-worker debian:13-slim -f debian-13-slim-filc-0.681 \
      --backend podman --save image:lazy-bootstrap/debian-filc:0.681
$ ./lazy-bootstrap build-worker debian:13-slim --backend chroot \
      --save dir:/srv/workers/debian-gcc
$ ./lazy-bootstrap rebuild --backend bwrap --rootfs dir:/srv/workers/debian-gcc
```

Each worker carries `/opt/lazy-bootstrap/worker.json` describing itself.

`ci/` holds the same thing as data: [`ci/flavours.toml`](ci/flavours.toml) lists
distro × toolchain combinations, [`ci/system-deps/`](ci/system-deps/) declares
what each needs, and the `Containerfile`s install from those very files, so an
image built in CI and an environment built on the fly cannot drift apart.

```console
$ ci/build-images.sh --list
$ ci/build-images.sh debian-13-slim-gcc
```

## Debugging

Every step can be traced and re-run by hand.

```console
$ ./lazy-bootstrap rebuild ... --debug        # step ids, return codes, timings
$ ./lazy-bootstrap rebuild ... --debug --debug   # plus the shell payload and env
$ ./lazy-bootstrap rebuild ... -ddd              # plus untruncated output
```

Each run also writes `runs/<id>/replay/<unit>.sh`: a real, runnable script with
the exact commands, wrapper included, in order.

```console
$ LB_REPLAY_DRY=1 sh runs/<id>/replay/all.sh    # read it
$ sh runs/<id>/replay/all.sh                    # or just run it
```

## The shell edition

[`sh/lazy-bootstrap.sh`](sh/lazy-bootstrap.sh) is the same pipeline in readable
Bash — inventory, toolchain, prepare, fetch, deps, build, report — and it emits
the *same* `report.json`, so the Python renderers work on its output:

```console
$ sh/lazy-bootstrap.sh rebuild debian:13-slim --package hostname -d
$ ./lazy-bootstrap report runs/<id> --format html -o report.html
```

It exists so the pipeline can be read and reproduced without reading Python. The
comparison and HTML renderers stay on the Python side; those are reporting, not
pipeline.

## Tests

```console
$ python3 tests/test_unit.py          # no network, no root, no engines
$ tests/matrix.sh                     # every execution class this machine allows
$ tests/matrix.sh --only chroot-hostfs,bwrap
```

`matrix.sh` skips what the machine cannot do instead of failing, so it is
equally useful on a laptop and in CI.

## Layout

```
lazy-bootstrap            entry point (works straight from a checkout)
sh/lazy-bootstrap.sh      the same pipeline in Bash
src/lazybootstrap/
  executors/              host, chroot, bwrap, firejail, oci
  rootfs.py               image / hostfs / dir / none
  distros/                debian (apt family), alpine (apk)
  toolchains/             gcc, llvm, filc + the compiler shim
  planner.py engine.py    grouping and orchestration
  report/                 text, markdown, json, html, comparison
  worker.py               build a worker on any class, save it in any form
ci/                       flavours, system-deps, Containerfiles
profiles/                 ready-made runs
docs/SPECS.md             design decisions and why
```

### Virtual machines

The VM class uses the same `Executor` contract; what it adds is a transport
(ssh into the guest). `VmDriver` is the seam other hypervisors plug into —
libvirt, VirtualBox, a cloud API — and only qemu is implemented, on purpose.
Naming an unimplemented one says so, rather than "unknown option".

```console
$ ./lazy-bootstrap env available disk.qcow2 --backend vm
$ ./lazy-bootstrap env ensure disk.qcow2 --backend vm \
      --vm-option SSH_KEY=id_lb --vm-option SEED=seed.img
```

KVM is an optimisation, not a requirement: without `/dev/kvm` the guest runs
under TCG emulation and says so. And nothing is ever pulled implicitly for a VM
— a container image is not a bootable disk, so `--image debian:13-slim` is
reported as such instead of failing obscurely later.

## Status

Working and tested end to end: Debian/Ubuntu and Alpine; gcc, clang and Fil-C;
the host, chroot, bubblewrap, firejail and podman classes, for both worker
construction and rebuilds; text/markdown/json/html reporting and run
comparison; the orchestration interface with its acquisition policy.

Implemented but not fully exercised here: docker (no daemon in the development
environment) and the VM class (no KVM, and emulation is cut short — see
docs/ENVIRONMENT.md). Both are covered by capability probes that skip rather
than pretend.

Next: full bootstrap with optional recompilation.
