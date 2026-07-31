"""Making the host's egress proxy usable from inside an environment (D-33).

Restricted networks usually allow egress through one sanctioned HTTP proxy.
That proxy is very often bound to the host's loopback, and a container has its
own loopback - so inheriting `HTTPS_PROXY=http://127.0.0.1:PORT` gives the
build a proxy address that resolves to itself and refuses every connection.
Two things have to cross the boundary:

    the address     127.0.0.1 -> an address the environment can actually reach
    the trust       a TLS-terminating proxy presents its own CA, which the
                    environment's trust store has never heard of

None of this is circumvention: it is the difference between using the
sanctioned path and being unable to reach it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .logs import get

log = get("proxy")

PROXY_VARS = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
              "NO_PROXY", "no_proxy")

#: Names podman/docker resolve to the host from inside a container.
CONTAINER_HOST = "host.containers.internal"
DOCKER_HOST = "host.docker.internal"

#: Where the CA bundle is placed inside the environment.
CA_PATH = "/opt/lazy-bootstrap/proxy-ca.crt"

_LOOPBACK = re.compile(r"^(?P<scheme>\w+://)?(?P<host>127(?:\.\d+){3}|localhost|\[::1\]|::1)"
                       r"(?P<rest>:\d+.*)?$")


@dataclass
class ProxySettings:
    """The host's proxy configuration, and how to express it elsewhere."""

    env: dict[str, str] = field(default_factory=dict)
    ca_bundle: str = ""

    @property
    def active(self) -> bool:
        return bool(self.env.get("HTTPS_PROXY") or self.env.get("HTTP_PROXY"))

    def points_at_loopback(self) -> bool:
        for key in ("HTTPS_PROXY", "HTTP_PROXY"):
            value = self.env.get(key, "")
            if value and _LOOPBACK.match(_strip_scheme(value)):
                return True
        return False

    def rewritten(self, host: str) -> dict[str, str]:
        """The same settings with loopback replaced by `host`."""
        out: dict[str, str] = {}
        for key, value in self.env.items():
            if key.upper() in ("NO_PROXY",):
                out[key] = value
                continue
            out[key] = _replace_loopback(value, host)
        return out


def detect(environ: dict[str, str] | None = None) -> ProxySettings:
    """Read the host's proxy configuration and its CA bundle, if any."""
    source = environ if environ is not None else dict(os.environ)
    env = {name: source[name] for name in PROXY_VARS if source.get(name)}
    # Normalise: tools disagree about case, so define both spellings.
    for upper in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        lower = upper.lower()
        if env.get(upper) and not env.get(lower):
            env[lower] = env[upper]
        elif env.get(lower) and not env.get(upper):
            env[upper] = env[lower]

    ca = ""
    for candidate in (source.get("LB_PROXY_CA", ""), source.get("REQUESTS_CA_BUNDLE", ""),
                      source.get("SSL_CERT_FILE", ""), "/root/.ccr/ca-bundle.crt"):
        if candidate and Path(candidate).is_file():
            ca = candidate
            break
    return ProxySettings(env=env, ca_bundle=ca)


def _strip_scheme(url: str) -> str:
    return url.split("://", 1)[1] if "://" in url else url


def _replace_loopback(url: str, host: str) -> str:
    scheme, _, rest = url.partition("://")
    if not rest:
        scheme, rest = "", url
    for needle in ("127.0.0.1", "localhost", "[::1]"):
        if rest.startswith(needle):
            rest = host + rest[len(needle):]
            break
    return f"{scheme}://{rest}" if scheme else rest


def ca_env() -> dict[str, str]:
    """Variables that make OpenSSL-based clients trust the uploaded CA.

    apk-tools 3 does *not* pick up a certificate appended to the system bundle,
    but it does honour SSL_CERT_FILE - the difference between "TLS: server
    certificate not trusted" and 28642 packages. Since no single mechanism
    covers apk, apt, curl, wget and git at once, all of them are set.
    """
    return {
        "SSL_CERT_FILE": CA_PATH,
        "CURL_CA_BUNDLE": CA_PATH,
        "GIT_SSL_CAINFO": CA_PATH,
        "REQUESTS_CA_BUNDLE": CA_PATH,
    }


def install_ca(executor, settings: ProxySettings, unit: str = "") -> bool:
    """Put the proxy's CA where the environment's TLS clients will find it."""
    if not settings.ca_bundle:
        return False
    source = Path(settings.ca_bundle)
    if not source.is_file():
        return False

    executor.mkdir("/opt/lazy-bootstrap")
    # upload(), not write_text(): a system CA bundle is hundreds of kilobytes
    # and write_text puts its content on the command line, which fails with
    # E2BIG ("Argument list too long") long before it fails visibly.
    executor.upload(source, CA_PATH)
    script = f"""
# Three mechanisms, because no one of them reaches every client:
#  1. the system bundle    - apt, wget, most of the distro
#  2. a file plus env vars - apk (see ca_env), curl, git, python
#  3. an apt.conf snippet  - apt's own https method, which ignores the rest
for bundle in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt; do
    [ -e "$bundle" ] || continue
    grep -qF "$(sed -n '2p' {CA_PATH})" "$bundle" 2>/dev/null && continue
    cat {CA_PATH} >> "$bundle" 2>/dev/null && echo "appended to $bundle"
done
if [ -d /etc/apt/apt.conf.d ]; then
    printf 'Acquire::https::CaInfo "%s";\\n' {CA_PATH} \\
        > /etc/apt/apt.conf.d/99-lazy-bootstrap-ca 2>/dev/null || true
fi
command -v update-ca-certificates >/dev/null 2>&1 && update-ca-certificates >/dev/null 2>&1
exit 0
"""
    result = executor.run(script, title="install proxy CA", unit=unit, step_prefix="prepare")
    return result.ok


def prefer_https_sources(executor, settings: ProxySettings, unit: str = "") -> bool:
    """Point the distro's package sources at https when a proxy is in the way.

    An egress proxy typically forwards CONNECT (https) and nothing else, so a
    source list in plain http bypasses it and dies against the network policy
    instead - as a 403, which reads like "this package does not exist" rather
    than "your traffic never went through the proxy". Every Debian and Alpine
    mirror worth using serves https, and the CA is already installed above, so
    switching is safe and is what makes the archive reachable at all.
    """
    if not settings.active:
        return False
    script = """
changed=""
for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list \
         /etc/apt/sources.list.d/*.sources /etc/apk/repositories; do
    [ -f "$f" ] || continue
    grep -q 'http://' "$f" 2>/dev/null || continue
    sed -i 's|http://|https://|g' "$f" && changed="$changed $f"
done
[ -n "$changed" ] && echo "switched to https:$changed"
exit 0
"""
    result = executor.run(script, title="package sources over https",
                          unit=unit, step_prefix="prepare")
    if result.ok and "switched to https:" in result.output:
        log.info("egress proxy in use: package sources switched to https (%s)",
                 result.output.split("switched to https:")[1].strip())
    return result.ok


__all__ = ["ProxySettings", "detect", "install_ca", "prefer_https_sources", "CA_PATH",
           "CONTAINER_HOST", "DOCKER_HOST"]
