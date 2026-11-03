"""The Docker builder image: find it in a registry (ours, then upstream's), pull, or build it."""

from __future__ import annotations

import hashlib
import logging

from captain.config import Config
from captain.util import detect_current_machine_arch, run

log = logging.getLogger(__name__)


def _image_exists(image: str) -> bool:
    """Check if a Docker image exists locally."""
    result = run(
        ["docker", "image", "inspect", image],
        check=False,
        capture=True,
    )
    return result.returncode == 0


def _dockerfile_hash(cfg: Config) -> str:
    """Return the SHA-256 hex digest of the Dockerfile content.

    This is used as an image tag so that Dockerfile changes are detected
    automatically.
    """
    dockerfile = cfg.project_dir / "Dockerfile"
    local_arch = detect_current_machine_arch()
    hex_digest = hashlib.sha256(dockerfile.read_bytes()).hexdigest()
    return f"{local_arch}-{hex_digest}"


def _builder_candidates(cfg: Config, tag: str) -> list[str]:
    """Registry refs the builder may already exist at: our own, then upstream's."""
    from captain.cache._scope import _upstream

    refs = [f"{cfg.builder_registry}/{cfg.builder_repository}/builder:{tag}"]
    upstream = _upstream()
    if upstream is not None:
        refs.append(f"{upstream}/builder:{tag}")
    return list(dict.fromkeys(refs))


def _first_in_registry(refs: list[str]) -> str | None:
    """The first of *refs* that exists in its registry; checked by manifest HEAD, no pull."""
    from captain.cache._registry import Registry, RegistryError

    for ref in refs:
        name, _, tag = ref.rpartition(":")
        host, _, repo = name.partition("/")
        try:
            if Registry(host).manifest_exists(repo, tag):
                return ref
        except (RegistryError, OSError) as exc:
            log.warning("Could not check %s: %s", ref, exc)
    return None


def obtain_builder(cfg: Config, *, ensure_remote: bool = False) -> None:
    """Build the Docker builder image when the Dockerfile has changed.

    The image is tagged with a content hash of the Dockerfile so that
    changes are detected even when the base image name stays the same.

    With *ensure_remote* (CI's builder job), an image already in a registry
    (ours or upstream's) is enough: it is not pulled, as nothing runs here.
    """
    tag = _dockerfile_hash(cfg)
    candidates = _builder_candidates(cfg, tag)
    remote_tagged_image = candidates[0]
    local_tagged_image = f"{cfg.builder_image}:{tag}"

    log.debug(
        "Checking for existing builder image with tag '%s' or remote images %s",
        local_tagged_image,
        candidates,
    )

    if ensure_remote:
        found = _first_in_registry(candidates)
        if found is not None:
            log.info("Builder image %s exists in the registry; nothing to do.", found)
            return

    if _image_exists(local_tagged_image):
        log.info("Docker image '%s' is up to date with %s.", cfg.builder_image, local_tagged_image)
        # Ensure the un-hashed tag exists so later docker-run calls that
        # reference cfg.builder_image (without the hash suffix) succeed.
        run(["docker", "tag", local_tagged_image, cfg.builder_image], check=False)
        return

    # Check if a remote name exists locally... (was pre-pulled somehow)
    for candidate in candidates:
        if _image_exists(candidate):
            log.info(
                "Docker image '%s' already exists locally (pre-pulled). Tagging as '%s'.",
                candidate,
                cfg.builder_image,
            )
            run(["docker", "tag", candidate, cfg.builder_image], check=False)
            return

    # Pull from the first registry that has it (matching our Dockerfile hash).
    found = _first_in_registry(candidates)
    if found is not None and run(["docker", "pull", found], check=False).returncode == 0:
        log.info(
            "Pulled Docker image '%s' from registry. Tagging as '%s'.", found, cfg.builder_image
        )
        run(["docker", "tag", found, cfg.builder_image], check=False)
        return

    # build locally if no existing image was found.
    log.info("Building Docker image '%s'...", cfg.builder_image)
    run(
        [
            "docker",
            "buildx",
            "build",
            "--load",  # for older Docker versions
            "--progress=plain",
            "-t",
            local_tagged_image,
            "-t",
            cfg.builder_image,
            str(cfg.project_dir),
        ]
    )

    # Show the layer size distribution for the built image to help with debugging and optimization.
    log.info("Docker image '%s' built successfully. Layer size distribution:", local_tagged_image)
    layer_sizes_lines = run(
        [
            "docker",
            "history",
            "--no-trunc",
            "--format",
            "-> {{.Size}} :: '{{.CreatedBy}}'",
            local_tagged_image,
        ],
        capture=True,
        check=True,
    )
    layers = []
    for line in layer_sizes_lines.stdout.strip().splitlines():
        line = line.strip()
        if line.startswith("-> "):
            # remove double whitespace chars to make it easier to read
            line = " ".join(line.split())
            layers.append(line)
    # reverse the array to match the order
    layers.reverse()
    for layer in layers:
        log.info("Layer info: %s", layer)

    # Optionally push the image after building it
    if cfg.builder_push:
        log.info(
            "Pushing Docker image '%s' to registry as '%s'...",
            cfg.builder_image,
            remote_tagged_image,
        )
        run(["docker", "tag", local_tagged_image, remote_tagged_image], check=False)
        run(["docker", "push", remote_tagged_image])
