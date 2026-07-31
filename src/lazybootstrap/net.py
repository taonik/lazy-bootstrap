"""Host-side downloads with a content cache.

Toolchain tarballs are large and reused across runs, so they are fetched once on
the host and then pushed into whichever environment needs them. Fetching on the
host (rather than inside every container) is also what makes a run possible in
a network-restricted environment: one place to point at a mirror, one place to
report a blocked host.
"""

from __future__ import annotations

import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .orchestration import util
from .orchestration.logs import get
from .orchestration.trace import Tracer

log = get("net")

USER_AGENT = "lazy-bootstrap/0.1 (+https://github.com/taonik/lazy-bootstrap)"


class DownloadError(RuntimeError):
    """Raised when a URL cannot be fetched. Callers turn this into `blocked`."""


@dataclass
class Download:
    url: str
    path: Path
    size: int
    cached: bool


class Fetcher:
    def __init__(self, cache_dir: str | Path, tracer: Tracer | None = None,
                 timeout: int = 120) -> None:
        self.cache_dir = util.ensure_dir(Path(cache_dir) / "downloads")
        self.tracer = tracer or Tracer()
        self.timeout = timeout

    def fetch(self, url: str, filename: str = "", sha256: str = "") -> Download:
        """Download `url` into the cache, or return the cached copy."""
        name = filename or url.rsplit("/", 1)[-1] or util.slugify(url)
        dest = self.cache_dir / name
        if dest.exists() and dest.stat().st_size > 0:
            if sha256 and util.sha256_file(dest) != sha256:
                log.warning("cached %s has wrong checksum, refetching", name)
                dest.unlink()
            else:
                self.tracer.note(f"cache hit {name} ({dest.stat().st_size} bytes)")
                return Download(url, dest, dest.stat().st_size, cached=True)

        step_id = self.tracer.next_id("fetch")
        self.tracer.step(step_id, f"download {name}",
                         wrapper=["curl", "-fsSL", "-o", str(dest), url])
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            self._download(url, tmp)
        except Exception as exc:  # noqa: BLE001 - reported as DownloadError below
            tmp.unlink(missing_ok=True)
            self.tracer.result(step_id, 1, 0.0, str(exc))
            raise DownloadError(f"cannot fetch {url}: {exc}") from exc
        tmp.replace(dest)
        size = dest.stat().st_size
        self.tracer.result(step_id, 0, 0.0, f"{size} bytes")

        if sha256:
            got = util.sha256_file(dest)
            if got != sha256:
                dest.unlink(missing_ok=True)
                raise DownloadError(f"checksum mismatch for {url}: expected {sha256}, got {got}")
        log.info("downloaded %s (%.1f MiB)", name, size / (1 << 20))
        return Download(url, dest, size, cached=False)

    def _download(self, url: str, dest: Path) -> None:
        """curl when available (resume, retries, proxy support), urllib otherwise."""
        curl = shutil.which("curl")
        if curl:
            proc = subprocess.run(
                [curl, "-fsSL", "--retry", "3", "--retry-delay", "2",
                 "--connect-timeout", "20", "-A", USER_AGENT, "-o", str(dest), url],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or f"curl exited {proc.returncode}")
            return
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            with open(dest, "wb") as fh:
                shutil.copyfileobj(response, fh)

    # -- diagnostics --------------------------------------------------------

    def reachable(self, url: str, timeout: int = 10) -> tuple[bool, str]:
        """HEAD-ish probe used by `doctor` to map a restricted network."""
        curl = shutil.which("curl")
        if curl:
            proc = subprocess.run(
                [curl, "-sS", "-o", "/dev/null", "-w", "%{http_code}", "-I",
                 "--max-time", str(timeout), "-A", USER_AGENT, url],
                capture_output=True, text=True,
            )
            code = proc.stdout.strip()
            ok = code.startswith(("2", "3")) or code in ("401", "404", "405")
            return ok, code or proc.stderr.strip()
        try:
            request = urllib.request.Request(url, method="HEAD",
                                             headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return True, str(response.status)
        except urllib.error.HTTPError as exc:
            return exc.code in (401, 404, 405), str(exc.code)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
