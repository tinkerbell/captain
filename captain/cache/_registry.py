"""Minimal OCI distribution-spec client (urllib only) for the build cache.

Just enough of the spec for ORAS-style artifacts: manifest HEAD/GET/PUT, blob
HEAD/GET/upload, with bearer-token (or basic) auth negotiated from the
registry's ``WWW-Authenticate`` challenge. Anonymous pulls work for public
repositories; credentials come from the environment or the Docker config.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from captain.progress import ProgressReader

log = logging.getLogger(__name__)

_TIMEOUT = 120  # seconds, per socket operation
_CHUNK = 1 << 20

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
# Any manifest or index, for re-tagging whatever is there.
ANY_MANIFEST = ", ".join(
    [
        OCI_MANIFEST,
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    ]
)


class RegistryError(Exception):
    pass


class RegistryDenied(RegistryError):
    """No access; ghcr.io also answers this way for repositories that don't exist."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are followed by hand, so credentials never leak to blob CDNs."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _docker_config_credentials(host: str) -> tuple[str, str] | None:
    config_dir = Path(os.environ.get("DOCKER_CONFIG", Path.home() / ".docker"))
    config_file = config_dir / "config.json"
    if not config_file.is_file():
        return None
    try:
        config = json.loads(config_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    helper = config.get("credHelpers", {}).get(host)
    for key in (host, f"https://{host}", f"http://{host}"):
        auth = config.get("auths", {}).get(key, {}).get("auth")
        if auth:
            user, _, password = base64.b64decode(auth).decode().partition(":")
            return user, password
    helper = helper or config.get("credsStore")
    if not helper:
        return None
    binary = shutil.which(f"docker-credential-{helper}")
    if binary is None:
        return None
    result = subprocess.run(
        [binary, "get"], input=host, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return None
    try:
        creds = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return creds.get("Username", ""), creds.get("Secret", "")


def _credentials(host: str) -> tuple[str, str] | None:
    """First of: GitHub token (ghcr.io only), REGISTRY_USERNAME/PASSWORD, Docker config."""
    if host == "ghcr.io":
        token = os.environ.get("CAPTAIN_CACHE_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            return os.environ.get("GITHUB_ACTOR", "captain"), token
    user, password = os.environ.get("REGISTRY_USERNAME"), os.environ.get("REGISTRY_PASSWORD")
    if user and password:
        return user, password
    return _docker_config_credentials(host)


def _parse_challenge(header: str) -> tuple[str, dict[str, str]]:
    scheme, _, params = header.partition(" ")
    return scheme.lower(), dict(re.findall(r'(\w+)="([^"]*)"', params))


class Registry:
    def __init__(self, host: str) -> None:
        self.host = host
        insecure = os.environ.get("REGISTRY_INSECURE") == "1" or host.startswith("localhost")
        self.base = f"{'http' if insecure else 'https'}://{host}"
        self._creds = _credentials(host)
        self._auth: dict[str, str] = {}  # scope -> Authorization header value

    # --- auth ---------------------------------------------------------------

    def _basic(self) -> str | None:
        if self._creds is None:
            return None
        return "Basic " + base64.b64encode(":".join(self._creds).encode()).decode()

    def _authenticate(self, challenge: str, scope: str) -> None:
        scheme, params = _parse_challenge(challenge)
        if scheme == "basic":
            basic = self._basic()
            if basic is None:
                raise RegistryError(f"{self.host} requires credentials")
            self._auth[scope] = basic
            return
        query = {"scope": scope}
        if "service" in params:
            query["service"] = params["service"]
        req = urllib.request.Request(f"{params['realm']}?{urllib.parse.urlencode(query)}")
        basic = self._basic()
        if basic is not None:
            req.add_header("Authorization", basic)
        try:
            with _opener.open(req, timeout=_TIMEOUT) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            error = RegistryDenied if exc.code in (401, 403) else RegistryError
            raise error(f"token request for {scope} failed: HTTP {exc.code}") from None
        token = body.get("token") or body.get("access_token")
        if not token:
            raise RegistryError(f"no token for {scope}")
        self._auth[scope] = f"Bearer {token}"

    # --- plumbing -------------------------------------------------------------

    def _request(
        self,
        method: str,
        repo: str,
        url: str,
        *,
        push: bool = False,
        data: bytes | BinaryIO | ProgressReader | None = None,
        headers: dict[str, str] | None = None,
    ):
        """Send a request, negotiating auth once on 401. Returns the open response.

        Raises urllib.error.HTTPError for non-2xx (other than redirects, which are
        returned as-is for the caller to follow).
        """
        scope = f"repository:{repo}:{'pull,push' if push else 'pull'}"
        if not url.startswith("http"):
            url = urllib.parse.urljoin(self.base, url)
        for attempt in range(2):
            req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
            if scope in self._auth:
                req.add_header("Authorization", self._auth[scope])
            try:
                return _opener.open(req, timeout=_TIMEOUT)
            except urllib.error.HTTPError as exc:
                if exc.code in (301, 302, 303, 307, 308):
                    return exc
                challenge = exc.headers.get("WWW-Authenticate")
                if exc.code != 401 or attempt or not challenge:
                    raise
                self._authenticate(challenge, scope)
                if hasattr(data, "seek"):
                    data.seek(0)  # type: ignore[union-attr]
        raise AssertionError("unreachable")

    # --- manifests ------------------------------------------------------------

    def manifest_exists(self, repo: str, reference: str) -> bool:
        """False also when *repo* is not accessible (or doesn't exist)."""
        try:
            # Any manifest type: release images are OCI indexes, and some registries
            # (eg. registry:2) answer 404 when the Accept header doesn't cover the type.
            with self._request(
                "HEAD", repo, f"/v2/{repo}/manifests/{reference}", headers={"Accept": ANY_MANIFEST}
            ):
                return True
        except RegistryDenied:
            return False
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                return False
            raise RegistryError(f"HEAD manifest {repo}:{reference}: HTTP {exc.code}") from None

    def get_manifest(self, repo: str, reference: str) -> dict | None:
        try:
            with self._request(
                "GET", repo, f"/v2/{repo}/manifests/{reference}", headers={"Accept": OCI_MANIFEST}
            ) as resp:
                return json.loads(resp.read())
        except RegistryDenied:
            return None
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                return None
            raise RegistryError(f"GET manifest {repo}:{reference}: HTTP {exc.code}") from None

    def put_manifest(self, repo: str, reference: str, manifest: dict) -> None:
        body = json.dumps(manifest, separators=(",", ":")).encode()
        try:
            with self._request(
                "PUT",
                repo,
                f"/v2/{repo}/manifests/{reference}",
                push=True,
                data=body,
                headers={"Content-Type": OCI_MANIFEST},
            ):
                pass
        except urllib.error.HTTPError as exc:
            raise RegistryError(f"PUT manifest {repo}:{reference}: HTTP {exc.code}") from None

    def retag(self, repo: str, src: str, dst: str) -> None:
        """Point tag *dst* at whatever *src* is (manifest or index), byte for byte.

        Everything it references is already in *repo*, so no blobs move.
        """
        try:
            with self._request(
                "GET",
                repo,
                f"/v2/{repo}/manifests/{src}",
                push=True,
                headers={"Accept": ANY_MANIFEST},
            ) as resp:
                body, media_type = resp.read(), resp.headers["Content-Type"]
            with self._request(
                "PUT",
                repo,
                f"/v2/{repo}/manifests/{dst}",
                push=True,
                data=body,
                headers={"Content-Type": media_type},
            ):
                pass
        except urllib.error.HTTPError as exc:
            raise RegistryError(f"retag {repo}:{src} → {dst}: HTTP {exc.code}") from None

    # --- blobs ----------------------------------------------------------------

    def blob_exists(self, repo: str, digest: str) -> bool:
        try:
            with self._request("HEAD", repo, f"/v2/{repo}/blobs/{digest}", push=True):
                return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise RegistryError(f"HEAD blob {repo}@{digest}: HTTP {exc.code}") from None

    def get_blob(
        self, repo: str, digest: str, dest: Path, progress: Callable[[int], None] | None = None
    ) -> None:
        """Stream a blob to *dest*, verifying its digest; *progress* gets each chunk's size."""
        try:
            resp = self._request("GET", repo, f"/v2/{repo}/blobs/{digest}")
            if isinstance(resp, urllib.error.HTTPError):  # redirect to storage; no auth there
                resp = _opener.open(resp.headers["Location"], timeout=_TIMEOUT)
        except urllib.error.HTTPError as exc:
            raise RegistryError(f"GET blob {repo}@{digest}: HTTP {exc.code}") from None
        sha = hashlib.sha256()
        with resp, open(dest, "wb") as out:
            while chunk := resp.read(_CHUNK):
                sha.update(chunk)
                out.write(chunk)
                if progress:
                    progress(len(chunk))
        if f"sha256:{sha.hexdigest()}" != digest:
            dest.unlink(missing_ok=True)
            raise RegistryError(f"digest mismatch for {repo}@{digest}")

    def upload_blob(
        self,
        repo: str,
        digest: str,
        size: int,
        source: Path | bytes,
        progress: Callable[[int], None] | None = None,
    ) -> None:
        """Monolithic upload (POST, then a streaming PUT), unless the blob already exists.

        *progress* gets the size of each chunk sent from a *source* file.
        """
        if self.blob_exists(repo, digest):
            log.debug("Blob %s already in %s", digest, repo)
            return
        try:
            with self._request("POST", repo, f"/v2/{repo}/blobs/uploads/", push=True) as resp:
                location = resp.headers["Location"]
            location = urllib.parse.urljoin(self.base, location)
            sep = "&" if "?" in location else "?"
            url = f"{location}{sep}digest={urllib.parse.quote(digest)}"
            headers = {"Content-Type": "application/octet-stream", "Content-Length": str(size)}
            if isinstance(source, bytes):
                with self._request("PUT", repo, url, push=True, data=source, headers=headers):
                    pass
            else:
                with open(source, "rb") as f:
                    data = ProgressReader(f, progress) if progress else f
                    with self._request("PUT", repo, url, push=True, data=data, headers=headers):
                        pass
        except urllib.error.HTTPError as exc:
            raise RegistryError(f"upload blob {repo}@{digest}: HTTP {exc.code}") from None
