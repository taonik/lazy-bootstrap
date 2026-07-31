"""Alpine driver: apk + abuild, with APKBUILD recipes taken from aports.

Alpine differs from Debian in one structural way: the build recipes do not live
in the archive, they live in the `aports` git repository. So `fetch_source` is a
sparse checkout of one directory, and the branch is derived from the image's own
`/etc/alpine-release` so recipes match the installed versions.
"""

from __future__ import annotations

import re

from ..executors.base import CommandResult, Executor
from ..logs import get
from ..model import PackageRef, StepLog
from .base import BuildContext, Distro, SourceTree

log = get("alpine")

APORTS_URL = "https://github.com/alpinelinux/aports.git"
# Fallback list; ci/system-deps/alpine/common.txt is the real source (D-24).
BUILD_ESSENTIAL = ["alpine-sdk", "build-base", "git", "ca-certificates"]


# aports repositories, searched in this order for a package directory.
APORTS_REPOS = ("main", "community", "testing")


def _common_packages(ctx) -> list[str]:
    from ..sysdeps import load

    declared = ctx.sysdeps.packages("common") if ctx.sysdeps is not None else load("alpine", "common")
    return declared or BUILD_ESSENTIAL


class AlpineDistro(Distro):
    id = "alpine"
    package_manager = "apk"

    @staticmethod
    def detect(executor: Executor) -> bool:
        return executor.exists("/sbin/apk") or executor.exists("/etc/alpine-release")

    def describe(self, executor: Executor) -> dict[str, str]:
        release = executor.read_text("/etc/alpine-release").strip()
        arch = executor.run("apk --print-arch 2>/dev/null || uname -m",
                            title="apk arch", step_prefix="probe").stdout.strip()
        return {
            "id": "alpine",
            "version": release,
            "codename": aports_branch(release),
            "pretty": f"Alpine Linux {release}",
            "arch": arch,
            "libc": detect_libc(executor),
        }

    # -- inventory ----------------------------------------------------------

    def inventory(self, executor: Executor) -> list[PackageRef]:
        """Read /lib/apk/db/installed directly: it carries the origin (`o:`)
        field, i.e. the source package, which `apk info` does not print."""
        result = executor.run(
            r"""awk '
                /^P:/ {name=substr($0,3)}
                /^V:/ {ver=substr($0,3)}
                /^A:/ {arch=substr($0,3)}
                /^o:/ {origin=substr($0,3)}
                /^$/  {if (name != "") printf "%s\t%s\t%s\t%s\n", name, ver, arch, origin;
                       name=""; ver=""; arch=""; origin=""}
                END   {if (name != "") printf "%s\t%s\t%s\t%s\n", name, ver, arch, origin}
            ' /lib/apk/db/installed""",
            title="apk inventory", step_prefix="inventory",
        )
        if not result.ok:
            log.error("cannot read apk database: %s", result.output.strip()[:400])
            return []
        packages: list[PackageRef] = []
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) < 3 or not fields[0]:
                continue
            name, version, arch = fields[:3]
            origin = fields[3] if len(fields) > 3 else ""
            packages.append(
                PackageRef(name=name, version=version, arch=arch,
                           source_name=origin or name, source_version=version)
            )
        return packages

    # -- build machine ------------------------------------------------------

    def prepare_builder(self, executor: Executor, ctx: BuildContext) -> list[StepLog]:
        steps: list[StepLog] = []
        branch = ctx.source_mirror or aports_branch(
            executor.read_text("/etc/alpine-release").strip())

        # Step 1: index + build machinery. alpine-sdk pulls abuild, fakeroot, git.
        # Advisory, like apt-get update: the install right after is the real test.
        steps.append(self.step("apk-update", executor.run(
            "apk update", title="apk update", timeout=600,
            unit=ctx.unit, step_prefix="prepare"), fatal=False))
        steps.append(self.step("build-base", executor.run(
            f"apk add --no-cache {' '.join(_common_packages(ctx))}",
            title="install build machinery", timeout=1800,
            unit=ctx.unit, step_prefix="prepare")))

        # Step 2: abuild refuses to run as root without this, and needs a key
        # to sign the packages it produces.
        steps.append(self.step("abuild-key", executor.run(
            """
set -e
adduser -D -G abuild builder 2>/dev/null || true
addgroup builder abuild 2>/dev/null || true
export PACKAGER="lazy-bootstrap <noreply@example.invalid>"
if [ ! -d /root/.abuild ] || ! ls /root/.abuild/*.rsa >/dev/null 2>&1; then
    printf '\\n' | abuild-keygen -a -i -n >/dev/null 2>&1 || abuild-keygen -a -i -n
fi
ls /root/.abuild/
""",
            title="abuild keygen", timeout=300, unit=ctx.unit, step_prefix="prepare")))

        # Step 3: a shallow, sparse aports checkout - the recipes, nothing else.
        steps.append(self.step("aports", executor.run(
            f"""
set -e
if [ -d {_q(ctx.workdir)}/aports/.git ]; then
    cd {_q(ctx.workdir)}/aports && git fetch --depth 1 origin {_q(branch)} && git checkout FETCH_HEAD
    exit 0
fi
mkdir -p {_q(ctx.workdir)}
cd {_q(ctx.workdir)}
git clone --depth 1 --filter=blob:none --sparse --branch {_q(branch)} {APORTS_URL} aports
cd aports
git sparse-checkout init --cone
echo "aports branch: {branch}"
""",
            title=f"clone aports ({branch})", timeout=1200,
            unit=ctx.unit, step_prefix="prepare")))
        return steps

    # -- per package --------------------------------------------------------

    def fetch_source(self, executor: Executor, source: str,
                     ctx: BuildContext) -> tuple[SourceTree | None, CommandResult]:
        """Sparse-checkout the package directory, then let abuild fetch upstream."""
        repos = " ".join(APORTS_REPOS)
        script = f"""
set -e
cd {_q(ctx.workdir)}/aports
found=""
for repo in {repos}; do
    if git cat-file -e "HEAD:$repo/{source}/APKBUILD" 2>/dev/null; then found="$repo"; break; fi
done
[ -n "$found" ] || {{ echo "no APKBUILD for {source} in aports" >&2; exit 3; }}
git sparse-checkout add "$found/{source}"
echo "LB_TREE={ctx.workdir}/aports/$found/{source}"
echo "LB_VERSION=$(sed -n 's/^pkgver=//p' "$found/{source}/APKBUILD" | head -n 1)"
"""
        result = executor.run(script, title=f"aports checkout {source}", cwd=ctx.workdir,
                              timeout=ctx.timeout, unit=ctx.unit, step_prefix="fetch")
        if not result.ok:
            return None, result
        path = _grab(result.stdout, "LB_TREE")
        if not path:
            return None, result
        return SourceTree(name=source, version=_grab(result.stdout, "LB_VERSION"),
                          path=path, kind="aports"), result

    def install_build_deps(self, executor: Executor, source: str,
                           ctx: BuildContext) -> CommandResult:
        # `abuild deps` reads makedepends from the APKBUILD in the current dir.
        return executor.run(
            f"""
set -e
cd {_q(ctx.workdir)}/aports
dir=$(find . -maxdepth 2 -type d -name {_q(source)} | head -n 1)
[ -n "$dir" ] || {{ echo "package dir not checked out" >&2; exit 3; }}
cd "$dir"
abuild -r deps 2>/dev/null || abuild deps
""",
            title=f"abuild deps {source}", cwd=ctx.workdir, timeout=ctx.timeout,
            unit=ctx.unit, step_prefix="deps",
        )

    def build(self, executor: Executor, tree: SourceTree, ctx: BuildContext) -> CommandResult:
        jobs = self.jobs_expr(ctx)
        # -r installs missing deps, -K keeps the build dir for inspection,
        # -d skips the dependency check we already ran.
        script = f"""
set -e
cd {_q(tree.path)}
export JOBS={jobs}
export MAKEFLAGS="-j{jobs}"
export PACKAGER="lazy-bootstrap <noreply@example.invalid>"
export REPODEST={ctx.workdir}/packages
echo "--- compiler in use ---"
"${{CC:-cc}}" --version 2>&1 | head -n 2 || true
echo "--- abuild ---"
abuild -r -K
"""
        return executor.run(script, title=f"abuild {tree.name}", env=ctx.env, cwd=tree.path,
                            timeout=ctx.timeout, unit=ctx.unit, step_prefix="build")

    def collect_artifacts(self, executor: Executor, tree: SourceTree,
                          ctx: BuildContext) -> list[str]:
        result = executor.run(
            f"find {_q(ctx.workdir)}/packages -name '{tree.name}*.apk' 2>/dev/null || true",
            title="collect artifacts", unit=ctx.unit, step_prefix="collect")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# --- helpers ----------------------------------------------------------------


def aports_branch(alpine_release: str) -> str:
    """`3.22.1` -> `3.22-stable`; anything unparsable -> `master` (edge)."""
    match = re.match(r"^(\d+)\.(\d+)", alpine_release or "")
    return f"{match.group(1)}.{match.group(2)}-stable" if match else "master"


def detect_libc(executor: Executor) -> str:
    result = executor.run(
        "if ls /lib/ld-musl-*.so.* >/dev/null 2>&1; then echo musl; "
        "elif ls /lib*/ld-linux*.so.* >/dev/null 2>&1; then echo glibc; "
        "else echo unknown; fi",
        title="detect libc", step_prefix="probe",
    )
    return result.stdout.strip() or "unknown"


def _q(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _grab(text: str, key: str) -> str:
    match = re.search(rf"^{key}=(.*)$", text, re.M)
    return match.group(1).strip() if match else ""
