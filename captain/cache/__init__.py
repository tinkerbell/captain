"""Content-hashed build cache, stored as ORAS-style artifacts in OCI registries.

Each cacheable piece (tools, kernel, mkosi output, ISO) lives in an output
directory and is identified by a :class:`CacheKey`. A marker file in the
directory records the key it was built/restored for, so local re-runs are
content-addressed too. See docs/build-caching.md.
"""

from __future__ import annotations

import logging
import os
import shutil
import urllib.error
from collections.abc import Iterable
from pathlib import Path

from captain.config import Config

from . import _artifact
from ._keys import (
    CacheKey,
    combined_release_key,
    iso_key,
    kernel_key,
    mkosi_key,
    release_key,
    tools_key,
)
from ._registry import Registry, RegistryError
from ._scope import CacheScope, Location, resolve_scope

log = logging.getLogger(__name__)

MARKER = ".captain-cache-key"

__all__ = [
    "CacheKey",
    "CacheScope",
    "Location",
    "announce",
    "combined_release_key",
    "iso_key",
    "kernel_key",
    "lookup",
    "mkosi_key",
    "release_key",
    "require_build",
    "restore",
    "save",
    "scope",
    "tools_key",
]

_resolved_scope: CacheScope | None = None
_registries: dict[str, Registry] = {}
_hit_explained = False


def _in_docker() -> bool:
    # The host process does all remote cache work; the relaunched one only builds.
    return bool(os.environ.get("CAPTAIN_IN_DOCKER"))


def scope(cfg: Config) -> CacheScope:
    global _resolved_scope
    if _resolved_scope is None:
        resolved = _resolved_scope = resolve_scope(cfg)
        if not resolved.read:
            log.info("Cache: %s (%s)", resolved.context, resolved.no_write_reason)
            return resolved
        log.info("Cache: %s; reading from %s", resolved.context, ", ".join(map(str, resolved.read)))
        if resolved.write:
            log.info("Cache: pushing to %s", resolved.write)
        else:
            log.info("Cache: not pushing (%s)", resolved.no_write_reason)
        return resolved
    return _resolved_scope


def announce(cfg: Config) -> None:
    """Log the cache scope up front, rather than at the first remote lookup."""
    if not _in_docker():
        scope(cfg)


def _registry(host: str) -> Registry:
    if host not in _registries:
        _registries[host] = Registry(host)
    return _registries[host]


def _work_dir(cfg: Config) -> Path:
    work_dir = cfg.project_dir / "mkosi.output" / ".cache-tmp"
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


def _marker_matches(key: CacheKey, directory: Path) -> bool:
    marker = directory / MARKER
    return marker.is_file() and marker.read_text().strip() == key.digest


def lookup(cfg: Config, key: CacheKey) -> Location | None:
    """The first readable location holding *key*, without downloading it."""
    for location in scope(cfg).read:
        try:
            if _registry(location.host).manifest_exists(location.repository(key.kind), key.tag):
                return location
        except (RegistryError, urllib.error.URLError, OSError) as exc:
            log.warning("Cache: %s unavailable: %s", location, exc)
    return None


def restore(cfg: Config, key: CacheKey, directory: Path, *, force: bool = False) -> bool:
    """Make *directory* hold *key*'s output: True if it already did or was pulled.

    On a miss, *directory* is left removed so the stage builds from scratch.
    """
    if not force and _marker_matches(key, directory):
        log.info("Cache: %s is up to date in %s", key, directory)
        return True

    if directory.exists():
        shutil.rmtree(directory)
    if force:
        log.info("Cache: forced rebuild of %s", key)
        return False
    if _in_docker():
        return False

    for location in scope(cfg).read:
        repo = location.repository(key.kind)
        try:
            if _artifact.pull(_registry(location.host), repo, key, directory, _work_dir(cfg)):
                (directory / MARKER).write_text(key.digest + "\n")
                log.info("Cache: hit %s from %s", key, location)
                _explain_hit()
                return True
        except (RegistryError, urllib.error.URLError, OSError) as exc:
            log.warning("Cache: could not restore %s from %s: %s", key, location, exc)
            if directory.exists():
                shutil.rmtree(directory)
    log.info("Cache: miss %s", key)
    return False


def _explain_hit() -> None:
    global _hit_explained
    if not _hit_explained:
        _hit_explained = True
        log.info(
            "Cache: identical inputs were already built and pushed there, so it was downloaded "
            "instead of built; use --cache off to build everything locally."
        )


def require_build(cfg: Config, key: CacheKey) -> None:
    """Called on a miss, before building: refuses to build under --cache-only."""
    if cfg.cache_only:
        log.error("Cache: %s is not cached, and --cache-only forbids building it", key)
        raise SystemExit(1)


def save(cfg: Config, key: CacheKey, directory: Path, *, exclude: Iterable[str] = ()) -> None:
    """Record *key* as built into *directory*, and push it if this build may write."""
    (directory / MARKER).write_text(key.digest + "\n")
    if _in_docker():
        return
    location = scope(cfg).write
    if location is None:
        return
    registry, repo = _registry(location.host), location.repository(key.kind)
    try:
        if registry.manifest_exists(repo, key.tag):
            log.info("Cache: %s already in %s", key, location)
            return
        _artifact.push(
            registry,
            repo,
            key,
            directory,
            _work_dir(cfg),
            source_url=location.source_url,
            exclude=[MARKER, *exclude],
        )
        log.info("Cache: pushed %s to %s", key, location)
    except (RegistryError, urllib.error.URLError, OSError) as exc:
        log.warning("Cache: could not push %s to %s: %s", key, location, exc)
