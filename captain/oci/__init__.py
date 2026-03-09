"""High-level OCI artifact operations for publishing and retrieving releases.

Each artifact file is pushed as its own layer so that OCI registries
can deduplicate blobs between per-arch and combined images.

Combined image (``target="combined"``, no tag suffix):
  A multi-arch index where each platform manifest has the native
  arch's layers first, then the other arch's layers (8 layers total).
  ``linux/amd64`` → ``[A1‥A4, B1‥B4]``,
  ``linux/arm64`` → ``[B1‥B4, A1‥A4]``.

Per-arch image (``target="amd64"`` or ``"arm64"``, tag suffix ``-{arch}``):
  A multi-arch index where both platform entries contain the same
  4 layers (only that arch's artifacts).

Docker layer caching: pulling the combined image first and then the
native per-arch image gives cache hits, because the per-arch layers
form a prefix of the combined chain.  Cross-arch caching is not
possible due to Docker's chain-ID Merkle structure.

Images are built locally via ``buildah`` and pushed as finished
manifests — no intermediate untagged manifests are created on the
registry.  Read operations (digest, tag, export) use ``skopeo``.

* **containerd** can pull it (valid ``rootfs.diff_ids`` in the config) —
  Kubernetes image-volume mounts work.
* ``skopeo`` extracts individual files for release workflows.
"""

from captain.oci._common import compute_version_tag
from captain.oci._publish import publish
from captain.oci._pull import pull, tag_all, tag_image

__all__ = [
    "compute_version_tag",
    "publish",
    "pull",
    "tag_all",
    "tag_image",
]
