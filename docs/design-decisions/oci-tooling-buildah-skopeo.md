# OCI Tooling: Why Buildah + Skopeo

## Context

CaptainOS build artifacts (vmlinuz, initramfs, ISO, checksums) are published as OCI images to GHCR so that:

- Kubernetes can mount them via **image volumes** (requires containerd-compatible images).
- Release workflows can pull artifacts without relying on GitHub Actions artifact expiration windows.
- Multi-arch (amd64/arm64) consumers can pull from a single tag via OCI indexes.

We iterated through three OCI client tools before arriving at the current approach.

## Tool #1: ORAS

The initial implementation (`b34c119`..`90e6f75`) used [ORAS](https://oras.land/) directly in CI workflows to push
artifact files to GHCR:

```bash
oras push \
  --artifact-platform linux/$ARCH \
  ghcr.io/.../artifacts:$TAG \
  vmlinuz-x86_64 initramfs-x86_64 captainos-x86_64.iso ...
```

**Problem:** `oras push` creates **OCI artifacts** (with an artifact manifest), not **OCI images**. Containerd cannot
pull OCI artifacts because the layer count and `rootfs.diff_ids` in the config don't match — the image simply isn't a
valid container image. This meant **Kubernetes image volumes didn't work**, which was a hard requirement.

The fix wasn't a flag or config change — ORAS is designed for arbitrary artifact storage, not for producing container
images that a runtime can unpack.

## Tool #2: Crane

We replaced ORAS with [crane](https://github.com/google/go-containerregistry/tree/main/cmd/crane) (`58f25cf`), wrapping
it in a Python module (`captain/crane.py`). Crane produces valid OCI images with correct `rootfs.diff_ids`, so
containerd could pull them and Kubernetes image volumes worked.

The crane-based approach used `crane append` to add layers, `crane mutate` to set platform metadata and annotations,
`crane index append` for multi-arch indexes, and `crane export` to extract files during release.

**Problem:** `crane append` is a **remote-first** operation — each call pushes a manifest to the registry. When building
an image with four artifact layers, each `crane append` overwrites the tag with a new manifest, leaving the previous
manifests as **untagged ("orphaned") images** in the registry. For every publish cycle we created several untagged
manifests per architecture.

We tried multiple mitigations:

- **WIP tags** (`6cca281`): Push layers to a temporary `-wip` tag, then retag to the final ref. But GHCR requires
  `packages:delete` permission to remove tags, so WIP tag cleanup silently failed, leaving orphaned `-wip` tags instead.
- **Single-layer bundling** (`2cb2206`): Bundle all artifact files into one tar so `crane append` only runs once per
  platform manifest. This reduced but didn't eliminate the problem — `crane mutate` (for setting platform, annotations,
  labels) and `crane set_created` each rewrite the manifest, still producing untagged intermediates.
- **Digest refs** (`f1c8e8a`): Capture digests after each platform push and pass digest refs to `crane index append`
  instead of tags. This avoided some tag churn but didn't solve the fundamental issue.

The root cause is architectural: crane operates directly on the registry for every mutation. There is no local staging
area — every `append`, `mutate`, or `edit config` call creates a new manifest on the remote, and the old one becomes
untagged. Over time this accumulated significant registry garbage.

## Tool #3: Buildah + Skopeo (current)

The current approach splits responsibilities:

- **Buildah** handles image *construction*: creating containers from scratch, adding layers, setting metadata (OS, arch,
  annotations, labels), committing images, and managing manifest lists. All operations happen **locally** — nothing
  touches the registry until the final `buildah manifest push --all`.
- **Skopeo** handles image *read operations*: inspecting digests, checking if images exist, copying/retagging between
  refs, and downloading images for artifact extraction.

Current CLI entry points are grouped under:

```bash
uv run captain release --help
uv run captain release publish --help
uv run captain release pull --help
uv run captain release tag --help
```

### Why this solves the problems

1. **No untagged manifests.** Buildah builds the complete image locally (all layers, all metadata, correct timestamps)
   and pushes the finished manifest in one shot. There are no intermediate remote manifests to orphan.

2. **Containerd compatibility.** Buildah produces standard OCI images with valid `rootfs.diff_ids` — Kubernetes image
   volumes work correctly.

3. **Multi-arch indexes.** `buildah manifest create/add/push` manages OCI indexes natively, pushing the index and all
   referenced platform manifests in a single `push --all` operation.

4. **No external binary downloads.** Both buildah and skopeo are available as standard distro packages (
   `apt-get install buildah skopeo`), eliminating the need to download and verify release tarballs from GitHub as we did
   with crane.

5. **Rootless operation.** Both tools work without a container runtime or root privileges when using `fuse-overlayfs`.

### Trade-offs

- Buildah's local storage model uses more disk than crane's append-directly-to-registry approach, though this is
  negligible for CI runners with ephemeral storage.
- Two tools instead of one, but the separation of concerns (write vs. read) maps cleanly to the codebase:
  `captain/buildah.py` for construction, `captain/skopeo.py` for inspection/extraction.

### Operational notes

- Release commands are exposed as a click command group (`captain release ...`) with shared options (`--registry`,
  `--repository`, `--target`, `--src-tag`, and `--release-mode`).
- Insecure registries can be supported via `REGISTRY_INSECURE=1` (translated to `--tls-verify=false` in wrapper
  commands).
- Per-arch and combined images still use deterministic layers to preserve blob deduplication behavior.

## Addendum: existence checks and re-tags in Python

Building and pulling release images still use buildah and skopeo. Checking whether an image already
exists (`captain release status`, publish's content-tag check), adding a commit's source tag to an
existing image, and `captain release tag` are manifest operations done by captain's own OCI client
(`captain/cache/_registry.py`): they need no Docker, so CI doesn't pull the builder image just to
find there's nothing to do, or to add tags. See `docs/build-caching.md`.
