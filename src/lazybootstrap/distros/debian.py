"""Debian family driver (Debian, Ubuntu and derivatives): dpkg + apt + dpkg-buildpackage.

The archive URL is data, not code (D-08): pointing this driver at a different
apt archive is a configuration change, which is what makes it usable behind a
restricted network or against a snapshot.
"""

from __future__ import annotations

import re

from ..orchestration.executors.base import CommandResult, Executor
from ..orchestration.logs import get
from ..model import PackageRef, StepLog
from .base import BuildContext, Distro, SourceTree

log = get("debian")

# Fallback list, used only when ci/system-deps/debian/common.txt is unreadable
# (e.g. lazybootstrap installed as a wheel without the repo alongside it).
BUILD_ESSENTIAL = ["build-essential", "fakeroot", "devscripts", "dpkg-dev",
                   "debhelper", "ca-certificates"]


def _common_packages(ctx: BuildContext) -> list[str]:
    from ..sysdeps import load

    declared = ctx.sysdeps.packages("common") if ctx.sysdeps is not None else load("debian", "common")
    return declared or BUILD_ESSENTIAL

# dpkg-query gives us the binary -> source mapping for free (D-09/D-10).
_QUERY_FORMAT = (
    r"${binary:Package}\t${Version}\t${Architecture}\t"
    r"${source:Package}\t${source:Version}\t${Priority}\n"
)


class DebianDistro(Distro):
    id = "debian"
    package_manager = "apt"

    @staticmethod
    def detect(executor: Executor) -> bool:
        return executor.exists("/usr/bin/dpkg-query") or executor.exists("/var/lib/dpkg/status")

    def describe(self, executor: Executor) -> dict[str, str]:
        release = _parse_os_release(executor.read_text("/etc/os-release"))
        arch = executor.run("dpkg --print-architecture 2>/dev/null || uname -m",
                            title="dpkg architecture", step_prefix="probe").stdout.strip()
        return {
            "id": release.get("ID", "debian"),
            "version": release.get("VERSION_ID", ""),
            "codename": release.get("VERSION_CODENAME", ""),
            "pretty": release.get("PRETTY_NAME", ""),
            "arch": arch,
            "libc": detect_libc(executor),
        }

    # -- inventory ----------------------------------------------------------

    def inventory(self, executor: Executor) -> list[PackageRef]:
        result = executor.run(
            f"dpkg-query -W -f='{_QUERY_FORMAT}'",
            title="dpkg inventory", step_prefix="inventory",
        )
        if not result.ok:
            log.error("dpkg-query failed: %s", result.output.strip()[:400])
            return []
        packages: list[PackageRef] = []
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) < 5 or not fields[0]:
                continue
            name, version, arch, source, source_version = fields[:5]
            priority = fields[5] if len(fields) > 5 else ""
            packages.append(
                PackageRef(
                    name=name,
                    version=version,
                    arch=arch,
                    # dpkg omits source:Version when it equals the binary version.
                    source_name=source or name,
                    source_version=source_version or version,
                    repo=priority,
                )
            )
        return packages

    # -- build machine ------------------------------------------------------

    def prepare_builder(self, executor: Executor, ctx: BuildContext) -> list[StepLog]:
        steps: list[StepLog] = []

        # Step 1: make source packages visible. Modern images ship a deb822
        # .sources file with only binary entries; a sibling file adds deb-src.
        steps.append(self.step("enable-sources", self._enable_sources(executor, ctx)))

        # Step 2: refresh the index once for the whole unit. Not fatal: one
        # unreachable third-party repository is common and harmless, and if the
        # index really is unusable the install below says so far more clearly.
        steps.append(self.step("apt-update", executor.run(
            "apt-get update -o Acquire::Retries=3",
            title="apt-get update", env=_APT_ENV, timeout=900,
            unit=ctx.unit, step_prefix="prepare"), fatal=False))

        # Step 3: the build machinery itself, from ci/system-deps (D-24).
        packages = _common_packages(ctx)
        steps.append(self.step("build-essential", executor.run(
            f"apt-get install -y --no-install-recommends {' '.join(packages)}",
            title="install build machinery", env=_APT_ENV, timeout=1800,
            unit=ctx.unit, step_prefix="prepare")))

        # Step 4: an unprivileged account, because a buildd does not build as
        # root and some packages refuse to (D-30). Not fatal: the build falls
        # back to root with FORCE_UNSAFE_CONFIGURE and says so in the log.
        steps.append(self.step("build-user", executor.run(
            f"id {BUILD_USER} >/dev/null 2>&1 || "
            f"useradd -m -s /bin/sh {BUILD_USER} >/dev/null 2>&1 || "
            f"adduser -D -s /bin/sh {BUILD_USER} >/dev/null 2>&1 || true\n"
            f"id {BUILD_USER}",
            title="unprivileged build user", timeout=120,
            unit=ctx.unit, step_prefix="prepare"), fatal=False))
        return steps

    def _enable_sources(self, executor: Executor, ctx: BuildContext) -> CommandResult:
        """Write a deb-src entry mirroring the image's own binary sources.

        `ctx.source_mirror` (``URL suite components``) overrides the archive
        entirely; that is the knob used when the image's own archive is not
        reachable (D-20).
        """
        if ctx.source_mirror:
            uri, suite, components = _split_mirror(ctx.source_mirror)
            body = (
                "Types: deb-src\n"
                f"URIs: {uri}\n"
                f"Suites: {suite}\n"
                f"Components: {components}\n"
                "Trusted: yes\n"
            )
            return executor.run(
                f"mkdir -p /etc/apt/sources.list.d && cat > /etc/apt/sources.list.d/lazy-bootstrap-src.sources <<'LB_EOF'\n{body}LB_EOF\n",
                title="configure source mirror", unit=ctx.unit, step_prefix="prepare")

        # Otherwise: clone every deb entry into a deb-src entry, both formats.
        script = r"""
set -e
mkdir -p /etc/apt/sources.list.d
out=/etc/apt/sources.list.d/lazy-bootstrap-src.sources
: > "$out"
# deb822 style (Debian 12+/Ubuntu 24.04+)
for f in /etc/apt/sources.list.d/*.sources; do
    [ -e "$f" ] || continue
    case "$f" in *lazy-bootstrap-src.sources) continue;; esac
    sed -e 's/^Types:.*/Types: deb-src/' "$f" >> "$out"
    printf '\n' >> "$out"
done
# one-line style (older images)
for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
    [ -e "$f" ] || continue
    sed -n 's/^deb \(.*\)$/deb-src \1/p' "$f" >> /etc/apt/sources.list.d/lazy-bootstrap-src.list || true
done
[ -s "$out" ] || rm -f "$out"
echo "--- deb-src configuration ---"
cat /etc/apt/sources.list.d/lazy-bootstrap-src.sources 2>/dev/null || true
cat /etc/apt/sources.list.d/lazy-bootstrap-src.list 2>/dev/null || true
"""
        return executor.run(script, title="enable deb-src", unit=ctx.unit, step_prefix="prepare")

    # -- per package --------------------------------------------------------

    def install_build_deps(self, executor: Executor, source: str,
                           ctx: BuildContext) -> CommandResult:
        return executor.run(
            f"apt-get build-dep -y --no-install-recommends {_q(source)}",
            title=f"build-dep {source}", env=_APT_ENV, cwd=ctx.workdir,
            timeout=ctx.timeout, unit=ctx.unit, step_prefix="deps",
        )

    def fetch_source(self, executor: Executor, source: str,
                     ctx: BuildContext) -> tuple[SourceTree | None, CommandResult]:
        srcdir = f"{ctx.workdir}/src/{source}"
        # `apt-get source` unpacks into <name>-<version>/; find it rather than
        # guessing the version, which may carry an epoch or a binNMU suffix.
        script = f"""
set -e
rm -rf {_q(srcdir)}
mkdir -p {_q(srcdir)}
cd {_q(srcdir)}
apt-get source --only-source {_q(source)}
tree=$(find . -maxdepth 1 -mindepth 1 -type d | head -n 1)
[ -n "$tree" ] || {{ echo "no source tree unpacked" >&2; exit 3; }}
echo "LB_TREE=$(cd "$tree" && pwd)"
echo "LB_VERSION=$(cd "$tree" && dpkg-parsechangelog -S Version 2>/dev/null || echo unknown)"
"""
        result = executor.run(script, title=f"apt-get source {source}", env=_APT_ENV,
                              cwd=ctx.workdir, timeout=ctx.timeout, unit=ctx.unit,
                              step_prefix="fetch")
        if not result.ok:
            return None, result
        tree_path = _grab(result.stdout, "LB_TREE")
        version = _grab(result.stdout, "LB_VERSION")
        if not tree_path:
            return None, result
        return SourceTree(name=source, version=version, path=tree_path, kind="dsc"), result

    def build(self, executor: Executor, tree: SourceTree, ctx: BuildContext) -> CommandResult:
        jobs = self.jobs_expr(ctx)
        # -b: binary only (no source rebuild), -uc -us: no signing, -d: trust the
        # build-dep step above rather than re-checking (some deps are virtual).
        # Build as an unprivileged user with fakeroot, the way a Debian buildd
        # does (D-30). Building as real root changes package behaviour: tar's
        # configure refuses to run at all ("you should not run configure as
        # root"), and other packages silently take different paths. `-rfakeroot`
        # is dpkg-buildpackage's own default for exactly this reason.
        script = f"""
set -e
cd {_q(tree.path)}
DEB_BUILD_OPTIONS="${{DEB_BUILD_OPTIONS:-}} parallel={jobs} nocheck nodoc"
export DEB_BUILD_OPTIONS="$(echo "$DEB_BUILD_OPTIONS" | tr -s ' ' | sed 's/^ //;s/ $//')"
echo "--- compiler in use ---"
command -v cc gcc 2>/dev/null || true
"${{LB_CC:-cc}}" --version 2>&1 | head -n 2 || true
echo "--- dpkg-buildpackage ---"
if [ "$(id -u)" = 0 ] && command -v runuser >/dev/null 2>&1 \\
   && id {BUILD_USER} >/dev/null 2>&1 && command -v fakeroot >/dev/null 2>&1; then
    chown -R {BUILD_USER}:{BUILD_USER} . .. 2>/dev/null || true
{_forward_env_snippet()}
    runuser -u {BUILD_USER} -- env "$@" \\
        dpkg-buildpackage -b -uc -us -d -rfakeroot --jobs-force={jobs}
else
    echo "[lazy-bootstrap] building as root: no {BUILD_USER} user or no fakeroot" >&2
    FORCE_UNSAFE_CONFIGURE=1 dpkg-buildpackage -b -uc -us -d -rfakeroot --jobs-force={jobs}
fi
"""
        return executor.run(script, title=f"build {tree.name}", env=ctx.env,
                            cwd=tree.path, timeout=ctx.timeout, unit=ctx.unit,
                            step_prefix="build")

    def collect_artifacts(self, executor: Executor, tree: SourceTree,
                          ctx: BuildContext) -> list[str]:
        parent = tree.path.rsplit("/", 1)[0]
        result = executor.run(f"ls -1 {_q(parent)}/*.deb 2>/dev/null || true",
                              title="collect artifacts", unit=ctx.unit, step_prefix="collect")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    # -- extras -------------------------------------------------------------

    def essential_filter(self, packages: list[PackageRef]) -> list[PackageRef]:
        """Priority required/important only - a useful smoke-test subset."""
        return [p for p in packages if p.repo in ("required", "important")]


# --- helpers ----------------------------------------------------------------

_APT_ENV = {
    "DEBIAN_FRONTEND": "noninteractive",
    "APT_LISTCHANGES_FRONTEND": "none",
    "LC_ALL": "C.UTF-8",
}


#: Unprivileged account the build runs under, created by prepare_builder (D-30).
BUILD_USER = "lbbuild"

#: `runuser -u` resets the environment, so the toolchain's variables have to be
#: forwarded explicitly. PATH carries the shim, which is the whole mechanism.
#: HOME is deliberately absent: it would still point at /root, which the build
#: user cannot read (dpkg warns about it on every invocation).
_FORWARD = ("PATH", "LB_TOOLCHAIN", "LB_CC", "CC", "CXX", "DEB_BUILD_OPTIONS",
            "DEB_CFLAGS_APPEND", "DEB_CXXFLAGS_APPEND", "DEB_LDFLAGS_APPEND",
            "FILC_ROOT", "SOURCE_DATE_EPOCH", "LC_ALL", "LANG")


def _forward_env_snippet() -> str:
    """Shell collecting into "$@" only the variables that are actually set.

    Emphatically not `VAR="${VAR:-}"`. Exporting an *empty* CC is worse than
    leaving it unset: an empty environment variable still overrides make's
    built-in default of `cc`, so the recipe starts with the CFLAGS, `sh -c`
    swallows the leading `-g` as one of its own options, and the build dies
    with `make: g: No such file or directory` - a message that names neither
    the cause nor any real file. Caught by the execution matrix immediately
    after D-29 stopped exporting CC.
    """
    lines = [
        "set --",
        "for _v in " + " ".join(_FORWARD) + "; do",
        '    eval "_val=\\${$_v:-}"',
        '    [ -n "$_val" ] || continue',
        '    set -- "$@" "$_v=$_val"',
        "done",
    ]
    return "\n".join(lines)


def _q(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _grab(text: str, key: str) -> str:
    match = re.search(rf"^{key}=(.*)$", text, re.M)
    return match.group(1).strip() if match else ""


def _split_mirror(spec: str) -> tuple[str, str, str]:
    """"http://host/ubuntu noble main universe" -> (uri, suite, components)."""
    parts = spec.split()
    uri = parts[0] if parts else ""
    suite = parts[1] if len(parts) > 1 else "stable"
    components = " ".join(parts[2:]) if len(parts) > 2 else "main"
    return uri, suite, components


def _parse_os_release(text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key:
            data[key.strip()] = value.strip().strip('"')
    return data


def detect_libc(executor: Executor) -> str:
    """musl vs glibc - decides the Fil-C variant (D-13)."""
    result = executor.run(
        "if ls /lib/ld-musl-*.so.* >/dev/null 2>&1; then echo musl; "
        "elif ls /lib*/ld-linux*.so.* /lib/*/ld-linux*.so.* >/dev/null 2>&1; then echo glibc; "
        "else echo unknown; fi",
        title="detect libc", step_prefix="probe",
    )
    return result.stdout.strip() or "unknown"
