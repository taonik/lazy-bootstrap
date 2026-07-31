# The development environment this was validated in

Recorded because it shaped several design decisions (docs/SPECS.md D-20), and
because "it works here but not there" is much easier to diagnose against a
written baseline. Regenerate the network half at any time with:

```console
$ ./lazy-bootstrap doctor
$ ./lazy-bootstrap doctor --json > env.json
```

## Machine

| | |
|---|---|
| OS | Ubuntu 24.04.4 LTS (noble), kernel 6.18.5 |
| arch | x86_64 |
| privileges | root, full capability set except `cap_sys_resource` |
| namespaces | user namespaces available, `chroot(8)` available, overlayfs available |
| cgroups | v1, `cgroupManager: cgroupfs`, no systemd |
| engines | podman 4.9.3 + runc; **docker CLI present but no daemon** |
| sandboxes | bubblewrap 0.9.0, firejail 0.9.72 |
| helpers | skopeo, umoci, patchelf, curl, tar, xz, git |
| disk | a fixed per-session writable allowance — `df` reports the whole device and therefore misleads |

## Network: default-deny allowlist

Egress is filtered by an HTTP proxy that answers `403 Host not in allowlist`
for anything not permitted. This is a deliberate control of the environment,
not a fault, and lazy-bootstrap does not try to work around it — it reports it
and offers configuration (`--registry-mirror`, `--source-mirror`) that a
permitted host can satisfy.

### Reachable

| host | used for |
|---|---|
| `github.com`, `objects.githubusercontent.com`, `codeload.github.com`, `raw.githubusercontent.com` | LLVM and Fil-C release tarballs, the Alpine `aports` recipes |
| `archive.ubuntu.com`, `security.ubuntu.com` | an apt archive with `deb-src`, binaries and sources |
| `mirror.gcr.io` | Google's Docker Hub mirror: base images |
| `registry-1.docker.io`, `auth.docker.io` | the Docker Hub **API** (manifests) |
| `pypi.org`, `registry.npmjs.org` | not used by this project |

### Blocked

| host | consequence |
|---|---|
| `deb.debian.org` and every Debian mirror tried | **no Debian source packages, no Debian build-dependencies** |
| `dl-cdn.alpinelinux.org` and every Alpine mirror tried | **no Alpine packages: `apk` cannot install anything** |
| `salsa.debian.org`, `sources.debian.org`, `snapshot.debian.org` | no alternative route to Debian sources |
| `git.alpinelinux.org`, `gitlab.alpinelinux.org` | (mitigated: `aports` is mirrored on GitHub) |
| `production.cloudflare.docker.com`, `production.cloudfront.docker.com` | Docker Hub **blobs**: `podman pull docker.io/...` fails after resolving the manifest |
| `ftp.gnu.org`, `kernel.org`, `sourceware.org`, upstream tarball hosts generally | Alpine's `abuild` cannot fetch upstream sources |
| `ppa.launchpadcontent.net` | pre-existing PPAs on this machine make `apt-get update` return errors (harmless: index refresh is advisory, see D-24) |

### What that means in practice

| target | inventory | rebuild |
|---|---|---|
| `debian:13-slim` | ✅ (reads the image's own dpkg database) | ⛔ `blocked` — `deb.debian.org` unreachable |
| `alpine` (± `libc6-compat`) | ✅ (reads `/lib/apk/db/installed`) | ⛔ `blocked` — `dl-cdn.alpinelinux.org` unreachable |
| `ubuntu:24.04` | ✅ | ✅ end to end, including the toolchain matrix |

`blocked` is a first-class status precisely so this shows up as an environment
limit rather than as a package that "fails to compile".

### To lift the limitation

Add to the environment's network allowlist:

```
deb.debian.org                       # Debian binaries and sources
dl-cdn.alpinelinux.org               # Alpine packages
production.cloudflare.docker.com     # Docker Hub blobs (or keep using mirror.gcr.io)
production.cloudfront.docker.com
```

Alpine's `abuild` additionally fetches each package's upstream tarball, so a
full Alpine rebuild needs the upstream hosts named in the APKBUILDs
(`ftp.gnu.org`, `www.kernel.org`, …) or a local source cache.

Nothing in the tool needs changing: `--registry-mirror` and `--source-mirror`
already exist for exactly this, and the profiles in `profiles/` set the mirror
this environment needs.

## Quirks worth knowing

* **`df` lies.** The writable allowance is per session; "no space left on
  device" can arrive while `df` still shows tens of gigabytes free. Deletes keep
  working, so freeing space recovers immediately.
* **podman without systemd.** `podman rm -f` on `sleep infinity` waits ten
  seconds before `SIGKILL` and can leave a container in `Stopping`. The OCI
  backend therefore uses `-t 0` and per-process container names.
* **firejail's `--chroot` is disabled by default** on Debian/Ubuntu. Add
  `chroot yes` to `/etc/firejail/firejail.config`. `lazy-bootstrap doctor` says
  so explicitly rather than failing later.
* **GitHub's REST API is rate-limited** from shared egress IPs. Release
  discovery deliberately uses the unauthenticated HTML/atom endpoints and plain
  downloads, which are not.
