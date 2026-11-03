"""Dynamic version tokens for local builds, resolved like CI does, with a TTL cache.

CI's ``prepare`` job resolves the latest kernel.org release of the kernel
branch and the armbian-next repo token once per run (``captain
prepare-versions``) and passes them to every job. Both feed build-cache keys,
so local builds resolve them the same way to hit CI's cache entries; the
answers are kept in a gitignored file for a few hours so that every local
``captain`` invocation doesn't hit kernel.org / armbian.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from captain.config import DEFAULT_KERNEL_VERSION

log = logging.getLogger(__name__)

TTL_SECONDS = 6 * 60 * 60
_LOCAL_TIMEOUT = 5  # seconds; don't hold up local builds when offline
_CI_TIMEOUT = 60  # seconds

# Kept out of kernel.py, whose hash is part of the kernel's build-cache key.
RELEASES_JSON_URL = "https://www.kernel.org/releases.json"


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse a dotted kernel version into an int tuple for comparison."""
    parts: list[int] = []
    for piece in version.split("."):
        # stop at the first non-numeric component (e.g. rc suffixes)
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


def fetch_latest_branch_version(branch: str | None = None, timeout: float = _CI_TIMEOUT) -> str:
    """Return the newest kernel.org point release of *branch* (e.g. ``"6.18"``).

    Consults kernel.org's ``releases.json``, which lists the latest release of
    each active branch. *branch* defaults to the ``<major>.<minor>`` of
    :data:`DEFAULT_KERNEL_VERSION`. Raises ``ValueError`` (or a network error)
    when it can't tell.
    """
    if branch is None:
        branch = ".".join(DEFAULT_KERNEL_VERSION.split(".")[:2])

    req = urllib.request.Request(RELEASES_JSON_URL)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())

    prefix = f"{branch}."
    matches = [
        r["version"]
        for r in data.get("releases", [])
        if isinstance(r.get("version"), str) and r["version"].startswith(prefix)
    ]
    if not matches:
        raise ValueError(f"no kernel.org release for branch {branch}")

    latest = max(matches, key=_version_tuple)
    log.info("Latest kernel for branch %s is %s", branch, latest)
    return latest


def latest_branch_version(branch: str | None = None) -> str:
    """Like :func:`fetch_latest_branch_version`, but falls back to ``DEFAULT_KERNEL_VERSION``
    on any failure — never returns an empty string, so CI always gets a buildable version.
    """
    try:
        return fetch_latest_branch_version(branch)
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError) as exc:
        log.warning(
            "Could not resolve the latest kernel (%s); using default %s",
            exc,
            DEFAULT_KERNEL_VERSION,
        )
        return DEFAULT_KERNEL_VERSION


def _cache_file(project_dir: Path) -> Path:
    return project_dir / "mkosi.input" / ".versions.json"


def _load(path: Path) -> dict[str, dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def resolve(project_dir: Path, name: str, fetch: Callable[[], str], default: str) -> str:
    """The cached value of *name* if fresh, else *fetch*'s (cached); on failure a
    stale cached value, else *default* (whose cache keys won't match CI's)."""
    path = _cache_file(project_dir)
    entries = _load(path)
    entry = entries.get(name)
    age = time.time() - entry["fetched_at"] if entry else None
    if entry is not None and age is not None and age < TTL_SECONDS:
        log.debug("Using cached %s=%r (%.0f min old, %s)", name, entry["value"], age / 60, path)
        return entry["value"]

    try:
        value = fetch()
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        if entry is not None:
            log.warning("Could not resolve %s (%s); using stale %r", name, exc, entry["value"])
            return entry["value"]
        log.warning(
            "Could not resolve %s (%s); using default %r — cache keys won't match CI's",
            name,
            exc,
            default,
        )
        return default

    entries[name] = {"value": value, "fetched_at": time.time()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries, indent=2) + "\n")
    log.info("Resolved %s=%r (cached for %dh in %s)", name, value, TTL_SECONDS // 3600, path)
    return value


def kernel_version(project_dir: Path) -> str:
    return resolve(
        project_dir,
        "kernel_version",
        lambda: fetch_latest_branch_version(timeout=_LOCAL_TIMEOUT),
        DEFAULT_KERNEL_VERSION,
    )


def armbian_version(project_dir: Path) -> str:
    from captain import armbian

    return resolve(
        project_dir,
        "armbian_version",
        lambda: armbian.fetch_release_etag(timeout=_LOCAL_TIMEOUT),
        "",
    )
