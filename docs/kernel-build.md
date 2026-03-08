# Kernel Build Process

CaptainOS builds a custom Linux kernel from upstream source with
project-specific defconfigs.  The build is orchestrated by the `captain.kernel`
module and driven through the CLI (`./build.py kernel`).

## Quick Start

```bash
# Build the kernel (defaults to Docker mode, amd64, kernel 6.18.16)
./build.py kernel

# Build for arm64
./build.py kernel --arch=arm64

# Use a local kernel source tree instead of downloading
./build.py kernel --kernel-src=/path/to/linux

# Force a rebuild even if outputs already exist
./build.py kernel --force-kernel

# Build natively (no Docker)
./build.py kernel --kernel-mode=native
```

Every flag also has an environment variable form (e.g. `ARCH`, `KERNEL_VERSION`,
`KERNEL_MODE`, `KERNEL_SRC`, `FORCE_KERNEL`).

## Execution Modes

The `--kernel-mode` flag controls how the kernel is built:

| Mode     | Description                                                            |
|----------|------------------------------------------------------------------------|
| `docker` | (default) Builds inside the `captainos-builder` Docker container.      |
| `native` | Builds directly on the host; requires kernel build tools to be installed. |
| `skip`   | Skips the kernel stage entirely.                                       |

In Docker mode, the builder container is based on Debian Trixie and includes
all kernel build dependencies (gcc, make, flex, bison, bc, libelf, libssl,
dwarves/pahole, etc.) plus cross-compilation toolchains for arm64.  Inside
the container, the mode is forced to `native` so Docker is never invoked
recursively.

## Build Pipeline

The `kernel.build()` function runs four stages sequentially:

### 1. Download

`kernel.download_kernel()` fetches the upstream tarball from
`cdn.kernel.org`:

```
https://cdn.kernel.org/pub/linux/kernel/v{major}.x/linux-{version}.tar.xz
```

The tarball is extracted into `/var/tmp/kernel-build/linux-{version}/`.
If the extracted source directory already exists, it is reused.

When `--kernel-src` is set, this step is skipped and the local source tree
is used as-is.

### 2. Configure

`kernel.configure_kernel()` applies the project defconfig for the target
architecture:

1. Copies `kernel.configs/{major}.{minor}.y.{arch}` into the source tree as `.config`.
2. Runs `make olddefconfig` to resolve any new symbols against defaults.
3. Saves the fully resolved config to `kernel.configs/.config.resolved.{branch}.{arch}` for
   debugging.

If no defconfig file exists for the target kernel version and arch, the build
exits with an error listing the available kernel branches.

For `x86_64` builds, the `COMMAND_LINE_SIZE` is patched from 2048 to 4096
in `arch/x86/include/asm/setup.h` because Tinkerbell passes large kernel
command lines.

Cross-compilation is set up automatically via the `ARCH` and
`CROSS_COMPILE` environment variables (e.g. `CROSS_COMPILE=aarch64-linux-gnu-`
for arm64).

### 3. Compile

`kernel.build_kernel()` runs the parallel make:

```bash
make -j$(nproc) {image_target} modules
```

The image target is architecture-dependent:

| Architecture | `ARCH`    | Image target | Output path                   |
|-------------|-----------|-------------|-------------------------------|
| amd64       | `x86_64`  | `bzImage`   | `arch/x86/boot/bzImage`       |
| arm64       | `arm64`   | `Image`     | `arch/arm64/boot/Image`       |

After compilation, `make -s kernelrelease` is invoked to determine the
exact built kernel version string (e.g. `6.18.16-captainos`).

### 4. Install

`kernel.install_kernel()` places the built artifacts into the output tree:

1. **Module installation** — `make INSTALL_MOD_PATH=... modules_install`
   installs modules to `mkosi.output/kernel/{version}/{arch}/modules/`.
2. **Strip** — Debug symbols are stripped from every `.ko` file with
   `strip --strip-unneeded`.
3. **Compress** — Modules are compressed with `zstd --rm -q -19` producing
   `.ko.zst` files.  The defconfig enables `CONFIG_MODULE_COMPRESS_ZSTD`
   and `CONFIG_MODULE_DECOMPRESS` so the kernel loads compressed modules at
   runtime.
4. **Clean up** — The `build` and `source` symlinks are removed from the
   modules directory.
5. **Merged-usr layout** — Modules are relocated from `lib/modules/` to
   `usr/lib/modules/` to follow the merged-usr filesystem convention.
6. **depmod** — `depmod -a` regenerates module dependency metadata for
   the compressed `.ko.zst` files.
7. **Kernel image** — The kernel image (`vmlinuz-{version}`) is copied to
   `mkosi.output/kernel/{kernel_version}/{arch}/`.  It is kept separate from
   the extra-tree so it is **not** included in the initramfs CPIO — iPXE loads
   the kernel independently.

## Output Layout

After a successful build, the kernel stage produces:

```
mkosi.output/
├── tools/{arch}/                   # tools only (containerd, runc, etc.)
│   ├── usr/local/bin/
│   └── opt/cni/bin/
└── kernel/{kernel_version}/{arch}/
    ├── vmlinuz-{version}           # kernel image (bzImage or Image)
    └── modules/                    # passed as --extra-tree to mkosi
        └── usr/lib/modules/{version}/
            ├── kernel/...          # compressed .ko.zst module files
            ├── modules.dep
            ├── modules.dep.bin
            └── ...
```

The tools and modules subtrees are both passed to mkosi via
separate `--extra-tree=` flags and merged into the initramfs.  The vmlinuz
image is collected into `out/` by `artifacts.collect_kernel()` as
`out/vmlinuz-{kernel_version}-{arch}`.

## Idempotency

The CLI checks for existing outputs before starting:

- If both `mkosi.output/kernel/{kernel_version}/{arch}/modules/usr/lib/modules/` and
  `mkosi.output/kernel/{kernel_version}/{arch}/vmlinuz-*` exist, the build is skipped.
- Use `--force-kernel` (or `FORCE_KERNEL=1`) to force a rebuild.
- If modules exist but the vmlinuz is missing, the kernel is rebuilt
  automatically.

## Defconfigs

Architecture-specific defconfigs live in the `kernel.configs/` directory,
named by stable branch: `{major}.{minor}.y.{arch}`.  This allows
multiple kernel versions to coexist — each stable branch (e.g. 6.18.y,
6.19.y) has its own config per architecture.

- `kernel.configs/6.18.y.amd64` — x86_64 config adapted for kernel 6.18.
- `kernel.configs/6.18.y.arm64` — arm64 config adapted for kernel 6.18.
- `kernel.configs/6.19.y.amd64` — x86_64 config adapted for kernel 6.19.
- `kernel.configs/6.19.y.arm64` — arm64 config adapted for kernel 6.19.

The default kernel version is defined as `DEFAULT_KERNEL_VERSION` in
`captain/config.py` and can be overridden via `--kernel-version` or
the `KERNEL_VERSION` environment variable.

Both configs include support for bare-metal provisioning, container
runtimes (cgroups v2, namespaces, overlayfs), and broad hardware/network
driver coverage.  The local version suffix is set to `-captainos`.
