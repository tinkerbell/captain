"""Cache keys: content hashes of each cacheable piece's declared inputs.

A key never includes git revisions, hostnames, paths or timestamps, so a rebuild
with eg. only doc changes resolves to the very same keys (and hits).
See docs/build-caching.md for the full inputs table.
"""

from __future__ import annotations

import functools
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from captain.config import Config

if TYPE_CHECKING:
    from captain.flavor import Flavor

# Bump when the artifact format/layout changes (invalidates every key).
CACHE_SCHEMA = 1

# Emergency knob: bump to force a rebuild of everything, eg. after a CVE fix
# lands in upstream distro repos. Per-distro: see `cache_epoch` on distro traits.
CACHE_EPOCH = 0

_CAPTAIN_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class CacheKey:
    kind: str  # tools, kernel, mkosi, iso: also the registry repository name
    name: str  # tag prefix, eg. "tools" or the flavor id
    arch: str
    inputs: dict[str, str] = field(hash=False)

    @functools.cached_property
    def canonical(self) -> str:
        """Canonical JSON of everything the digest covers."""
        doc = {
            "schema": CACHE_SCHEMA,
            "epoch": CACHE_EPOCH,
            "kind": self.kind,
            "name": self.name,
            "arch": self.arch,
            "inputs": self.inputs,
        }
        return json.dumps(doc, sort_keys=True, separators=(",", ":"))

    @functools.cached_property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical.encode()).hexdigest()

    @property
    def tag(self) -> str:
        return f"{self.name}-{self.arch}-{self.digest[:24]}"

    def __str__(self) -> str:
        return f"{self.kind}:{self.tag}"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def project_file_sha(cfg: Config, relative: str) -> str:
    return _sha((cfg.project_dir / relative).read_bytes())


def modules_sha(*modules: str) -> str:
    """Combined hash of captain modules (paths relative to the captain package)."""
    return _sha("".join(f"{m}={_sha((_CAPTAIN_DIR / m).read_bytes())}" for m in modules).encode())


def tools_key(cfg: Config) -> CacheKey:
    # tools.py holds the TOOLS manifest (versions, URLs, members) and the download logic.
    return CacheKey("tools", "tools", cfg.arch, {"modules": modules_sha("tools.py")})


def kernel_key(cfg: Config) -> CacheKey:
    from captain.kernel import find_defconfig

    return CacheKey(
        "kernel",
        f"linux-{cfg.kernel_version}",
        cfg.arch,
        {
            "kernel_version": cfg.kernel_version,
            "defconfig": _sha(find_defconfig(cfg).read_bytes()),
            "modules": modules_sha("kernel.py"),
            # The builder image is the toolchain.
            "dockerfile": project_file_sha(cfg, "Dockerfile"),
        },
    )


def mkosi_key(cfg: Config, flavor: Flavor) -> CacheKey:
    if cfg.flavor_hash is None:
        raise ValueError("flavor_hash is not set; generate the flavor first")
    inputs = {
        # The whole rendered mkosi tree: traits, cache-bust tokens, custom/ etc.
        "flavor_hash": cfg.flavor_hash,
        # Merged via --extra-tree.
        "tools": tools_key(cfg).digest,
        # mkosi itself (MKOSI_VERSION) and the apt/dnf it drives live in the builder image.
        "dockerfile": project_file_sha(cfg, "Dockerfile"),
        # How mkosi is invoked.
        "modules": modules_sha("cli/stages.py", "docker.py"),
    }
    for trait in flavor.traits:
        inputs |= trait.cache_inputs(flavor)
    return CacheKey(flavor.output.output_kind, flavor.id, cfg.arch, inputs)


def iso_key(cfg: Config, flavor: Flavor) -> CacheKey:
    return CacheKey(
        "iso",
        flavor.id,
        cfg.arch,
        {
            "mkosi": mkosi_key(cfg, flavor).digest,
            # grub-mkrescue and the grub platform binaries come from the builder image.
            "dockerfile": project_file_sha(cfg, "Dockerfile"),
            "modules": modules_sha("iso.py"),
        },
    )


def release_key(cfg: Config, flavor: Flavor) -> CacheKey:
    """The published release image of one arch: its outputs plus how they are packaged."""
    inputs = {
        "mkosi": mkosi_key(cfg, flavor).digest,
        # buildah, which assembles the images, comes from the builder image.
        "dockerfile": project_file_sha(cfg, "Dockerfile"),
        # Which files become layers (and extra ones, like the RPi firmware), and how.
        "modules": modules_sha(
            "artifacts.py",
            "buildah.py",
            "oci/_build.py",
            "oci/_publish.py",
            "traits/output/__init__.py",
            "traits/platform/rpi.py",
        ),
    }
    if flavor.has_iso():
        inputs["iso"] = iso_key(cfg, flavor).digest
    return CacheKey("release", flavor.id, cfg.arch, inputs)


def combined_release_key(flavor_id: str, arch_keys: dict[str, CacheKey]) -> CacheKey:
    inputs = {arch: key.digest for arch, key in sorted(arch_keys.items())}
    return CacheKey("release", flavor_id, "combined", inputs)
