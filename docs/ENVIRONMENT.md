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
| `deb.debian.org` **and 19 other Debian mirrors** | **no Debian source packages, no Debian build-dependencies** |
| `dl-cdn.alpinelinux.org` **and 10 other Alpine mirrors** | **no Alpine packages: `apk` cannot install anything** |
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
| `ubuntu:24.04` | ✅ | ✅ end to end (used only to exercise the pipeline itself) |

Separately from rebuilds, `lazy-bootstrap toolchain check` answers the other
half of the question — does the toolchain itself work on the target — because
Fil-C and LLVM come from GitHub, which *is* reachable. `llvm-20.1.8` probes
short of compiling only because `debian:13` carries no libc headers, which
again come from the blocked archive:

| target | gcc | llvm | filc-0.681 |
|---|---|---|---|
| `debian:13` | ⛔ archive | ⛔ archive | ✅ provisioned, compiles and runs |
| `debian:13`, `llvm-20.1.8` | — | ✅ provisioned from the upstream 1.9 GiB tarball, exact version | — |
| `alpine` | ⛔ archive | ⛔ archive | ❌ musl-only target cannot run the glibc-linked clang (D-13 corrected) |
| `alpine` + a glibc loader | ⛔ archive | ⛔ archive | ⚠️ clang runs and identifies itself; then needs `ld` from binutils, which needs apk |

`blocked` is a first-class status precisely so this shows up as an environment
limit rather than as a package that "fails to compile".

Mirrors were not assumed unreachable, they were measured. 20 Debian mirrors
(`ftp.{us,de,uk,fr,it,nl}.debian.org`, `mirrors.kernel.org`, `debian.osuosl.org`,
`mirrors.mit.edu`, `mirror.csclub.uwaterloo.ca`, `mirrors.ocf.berkeley.edu`,
`cloudfront.debian.net`, …) and 11 Alpine mirrors (`mirrors.edge.kernel.org`,
`uk.alpinelinux.org`, `mirror.leaseweb.com`, `mirrors.aliyun.com`,
`mirrors.tuna.tsinghua.edu.cn`, …) all return the same thing — the proxy's own
log is unambiguous:

```
connect_rejected: gateway answered 403 to CONNECT (policy denial or upstream failure)
```

This is the environment's network policy, which the account owner chooses when
creating the environment. It is not something the tool can or should work around.

### What changed once the allowlist was widened

`dl-cdn.alpinelinux.org` became reachable, which moved the Alpine target
forward several steps. Each step revealed the next real problem, all of them
now fixed:

| step | was | now |
|---|---|---|
| `apk` inside a container | `Connection refused` (proxy on host loopback) | works — D-33 |
| `apk` TLS | `certificate not trusted` | works — `SSL_CERT_FILE` |
| toolchains | unavailable | gcc 15.2.0, clang 22.1.3, both from the distro |
| `abuild-keygen` | `doas: not found` | key generated and installed directly |
| `abuild deps` | `Do not run abuild as root` | dependencies read from the APKBUILD, installed with apk |
| `abuild` build | `Do not run abuild as root` | runs as `builder` via `su -p` |
| upstream sources | — | **still blocked**, see below |

`deb.debian.org` is still `403`, so Debian remains at the first step.

### To lift the limitation

```
deb.debian.org                       # Debian sources and build-dependencies
distfiles.alpinelinux.org            # Alpine's mirror of every upstream tarball
```

The second line is worth one host: Alpine's `abuild` otherwise fetches each
package's tarball from its own upstream (`musl.libc.org`, `busybox.net`,
`zlib.net`, `www.openssl.org`, `gitlab.alpinelinux.org`, …), all of which are
currently blocked. Pass it with `--distfiles-mirror`, or unblock those hosts
individually. Of the upstream hosts, only `ftp.gnu.org` and `github.com` are
currently reachable — no package in the Alpine base image sources from either.

Nothing in the tool needs changing: `--registry-mirror` and `--source-mirror`
already exist for exactly this, and the profiles in `profiles/` set the mirror
this environment needs.

## Virtualisation

| | |
|---|---|
| `/dev/kvm` | absent |
| nested virtualisation | absent (no `vmx`/`svm` in `/proc/cpuinfo`) |
| qemu | installable from the Ubuntu archive, runs in TCG (emulation) |
| `cloud-images.ubuntu.com` | reachable, so a bootable guest *can* be obtained |

qemu starts and accepts the generated command line here, but sustained emulation
is terminated by the sandbox (the shell returns 144) long before an emulated
Ubuntu guest finishes booting. The VM class is therefore implemented and its
capability/availability paths are verified, while **a full guest boot is not
validated in this environment** (docs/SPECS.md D-28). On a host with KVM:

```console
$ ./lazy-bootstrap env available disk.qcow2 --backend vm
$ ./lazy-bootstrap env ensure disk.qcow2 --backend vm \
      --vm-option SSH_KEY=id_lb --vm-option SEED=seed.img
$ LB_TEST_VM_IMAGE=disk.qcow2 LB_TEST_VM_KEY=id_lb LB_TEST_VM_SEED=seed.img \
      tests/matrix.sh --only vm
```

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
