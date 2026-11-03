"""Helpers for the Armbian-next apt repository.

Armbian flavors pull stable meta-package names (``linux-image-edge-rockchip64``
etc.) whose underlying kernel version changes whenever armbian-next republishes.
The repo's ``Release`` file ETag is a cheap proxy for "the repo changed": folding
it into the flavor hash makes CI rebuild (and ship the newer kernel) instead of
reusing a stale cached build.
"""

from __future__ import annotations

import hashlib
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

# The `armbian` dist (where the linux-image kernel packages live). A single
# repo-wide token from this Release file applies to all armbian flavors.
ARMBIAN_NEXT_RELEASE_URL = "https://apt-test.next.armbian.com/dists/armbian/Release"

_FETCH_TIMEOUT = 30  # seconds


def fetch_release_etag(url: str = ARMBIAN_NEXT_RELEASE_URL, timeout: float = _FETCH_TIMEOUT) -> str:
    """Return a short, stable cache-bust token from *url*'s ETag response header.

    Raises ``ValueError`` (or a network error) when there is no usable ETag.
    """
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        etag = resp.headers.get("ETag", "")

    # Normalise weak-validator prefix and surrounding quotes, then hash to a
    # fixed-width token that is safe to embed in filenames / cache keys.
    etag = etag.strip().removeprefix("W/").strip('"')
    if not etag:
        raise ValueError(f"no ETag header from {url}")

    token = hashlib.sha256(etag.encode()).hexdigest()[:16]
    log.info("Armbian repo Release ETag %r → cache-bust token %s", etag, token)
    return token


def release_etag(url: str = ARMBIAN_NEXT_RELEASE_URL) -> str:
    """Like :func:`fetch_release_etag`, but returns ``""`` on any failure — an empty
    token means "no cache-bust".
    """
    try:
        return fetch_release_etag(url)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("Could not fetch ETag from %s (%s); no armbian cache-bust", url, exc)
        return ""
