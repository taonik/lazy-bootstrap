# Caching and prefetch

Rebuilding 100 packages means preparing 100 environments, and nearly all of
them install the same `debhelper`, `dh-*` and `build-essential` set. In the
12-package Debian run that produced this document, `gcc` was provisioned
twelve times - identically. That repeated traffic is what this design removes.

There are two independent mechanisms. Either works alone.

| | what it is | when it helps |
|---|---|---|
| **external cacher** | apt-cacher-ng or an nginx cache, shared, long-lived | many runs over time, several machines |
| **prefetch** | this tool downloading a job's dependencies ahead of use | a single run, no cacher available |

The second exists precisely because the first may not be there. A user who
cannot run a service, or a CI job with no sidecar, still gets the benefit.

---

## 1. The dependency registry

Before a job starts, every unit's build-dependencies are resolved and merged
into one registry, keyed by package and version:

```
dep            needed_by   download   installed
debhelper      11 units      1.1 MB      4.2 MB
build-essential 11 units     0.1 MB      0.2 MB
libtool         3 units      0.5 MB      2.1 MB
```

Resolution uses the package manager rather than a hand-rolled solver, because
the package manager is the authority on what a build actually needs:

- **Debian**: `apt-get -s build-dep <src>` for the resolved transitive set,
  `apt-cache show` for `Size` and `Installed-Size`.
- **Alpine**: `apk add --simulate` plus `apk info -s`.

`needed_by` is the whole point of building the registry. Downloading in
descending frequency order means the shared base - exactly the `dh-*` set -
lands first and is reused by every subsequent unit, so the first bytes spent
are the ones with the highest return.

---

## 2. Space is a budget, not a hope

Every number below is an estimate from package metadata, and is labelled as
one. Estimates are used to *decline* work safely, never to promise it.

```
required(unit) = installed(build-deps) + source + build_factor x source
```

`build_factor` defaults to 8 and is configurable. Object files, a second copy
of the tree for `dpkg-buildpackage`, and the resulting artifacts have no
metadata to read, so this term is a guess - a deliberately generous one.

Two checks, both before anything is downloaded:

- **Cache budget** `--cache-budget` (default: 25% of the free space on the
  cache filesystem). Bounds prefetch only.
- **Environment budget** `--env-budget` (default: measured free space in the
  environment). If `required(unit)` exceeds it, the unit is **not attempted**.
  It is reported as `insufficient-space` with the two numbers side by side.

That last point is a category distinction the rest of this codebase already
makes: a package that cannot fit is not a package that fails to compile.
Starting a build that is certain to die with `ENOSPC` and reporting it as a
build failure would blame the software for the disk.

---

## 3. Why this cannot livelock

The dangerous case: units A and B run in parallel, neither working set fits
alongside the other, and eviction keeps throwing out what the other is about
to use. Both keep re-downloading, neither ever assembles a complete set, and
the job makes no progress while appearing busy.

Three rules remove it. They are stated as invariants because they are
enforced, not intended:

**I1 - Admission.** A unit is admitted only when its entire working set can be
reserved: `reserved + W(u) <= budget`. Otherwise it waits in the queue.

**I2 - Pinning.** An admitted unit's working set is immune to eviction until
that unit finishes. Eviction chooses only among unpinned entries.

**I3 - Bypass.** If `W(u) > budget` on its own, the unit can never be
admitted. It runs in bypass mode - fetching directly, populating nothing - and
this is logged. It is never left waiting for a condition that cannot occur.

Together these guarantee progress. At least one unit always holds a
reservation: the first admission always succeeds, by I3 if not by I1. A unit
holding a reservation cannot have its dependencies evicted, by I2. So at any
moment some unit can run to completion, release its reservation, and let the
next in. The queue drains.

Starvation is a separate concern from livelock, and gets a separate rule:
admission is FIFO with aging, so a large unit cannot be indefinitely overtaken
by a stream of small ones.

Eviction, among unpinned entries only, prefers the least valuable: lowest
`needed_by` first, then least recently used. A dependency that eleven units
still want is the last thing to go.

---

## 4. Defensive behaviour

Each of these exists because of a specific way the naive version breaks:

- **Atomic writes.** Download to `.part`, `rename()` on success. A prefetch
  killed mid-write must never leave a truncated file that a later run trusts.
- **Checksums** verified wherever the index supplies one. A cache that serves
  corrupt bytes is worse than no cache, because the resulting build failure
  points at the source.
- **Watermarks with hysteresis.** Stop prefetching at 90% of budget, resume at
  70%. A single threshold flaps: evict one file, immediately refetch it.
- **Bounded concurrency**, default 4. Unbounded parallel downloads exhaust
  file descriptors and starve the builds of bandwidth.
- **Prefetch always yields.** It is best-effort and never holds budget against
  an admitted unit. If a build needs the space, prefetch loses.
- **Stale index refusal.** Index files carry a short TTL (see the cache
  lifetimes in `Dockerfile.http-cache`). A stale `APKINDEX` reports missing
  packages that plainly exist - a failure that looks like a broken mirror and
  costs an hour to trace back to the cache.

---

## 5. Running the cacher

The cachers are ordinary services and can run wherever the rest of this tool
can run: on the host, in podman or docker, or inside `bwrap`/`firejail` via
the same executor interface the build environments use. The service is
long-lived, which the executor interface did not previously cover - it runs
commands to completion - so it is started detached and held by an open handle
until the run ends.

```sh
docker compose -f ci/cache/compose.yml up -d
./lazy-bootstrap rebuild debian:13 --package-cache http://127.0.0.1:3142
```

Both listen on loopback only. A package cache reachable from the network is an
open relay for whatever it has been asked to fetch, and neither service
authenticates.
