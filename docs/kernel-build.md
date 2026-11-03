# Kernel Build Process

CaptainOS now has two kernel paths, selected by flavor and command:

1. **Flavor-provided distro kernels** (for example `trixie-full` and several Armbian-based arm64 flavors).
2. **Captain-built kernels** via `captain kernel` (notably `trixie-slim` workflows).

## Commands

```bash
uv run captain kernel --help
```

Important options:

- `--kernel-mode {docker,native,skip}`
- `--force-kernel` (rebuild even if cached)
- `--kernel-version` (default: the latest kernel.org release of the branch, cached for 6h; see
  `docs/build-caching.md`)
- `--config` (interactive `menuconfig` flow; implies rebuild)

## Build outputs and handoff

- Kernel build artifacts are staged under `mkosi.input/kernel/<arch>/`.
- They are cached by a hash of kernel version, defconfig, `kernel.py` and the `Dockerfile`; an unchanged kernel is
  restored from the build cache instead of rebuilt (see `docs/build-caching.md`).
- Flavor generation + mkosi consume the staged kernel/modules.
- Final published kernel artifact is copied to `out/vmlinuz-<flavor-id>-<arch>`.

For the end-to-end pipeline using kernels and tools together, see `README.md` (`captain build`).
