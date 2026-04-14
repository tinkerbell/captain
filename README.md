# CaptainOS

A minimal, systemd-based in-memory OS for [Tinkerbell](https://tinkerbell.org) bare-metal provisioning.

CaptainOS boots via PXE/iPXE, runs entirely from RAM as a compressed CPIO initramfs, and provides a container runtime environment for the [tink-agent](https://github.com/tinkerbell/tinkerbell) — the component that drives hardware provisioning workflows.

## Why does CaptainOS exist?

CaptainOS is the next generation of Tinkerbell's in-memory OS, building on years of experience building, maintaining, and operating [HookOS](https://github.com/tinkerbell/hook) in production.
It is built with [mkosi](https://github.com/systemd/mkosi), producing a minimal systemd-based Debian OS that runs entirely from RAM.

- **Significantly smaller initramfs** — small enough to boot comfortably on resource-constrained single-board computers.
- **No Docker-in-Docker** — tink-agent runs directly on the host with containerd, giving it native access to the container runtime without any nesting.
- **Familiar operations** — systemd foundation with journalctl, networkd, and standard service management make debugging and troubleshooting straightforward.
- **Simpler architecture** — fewer layers between hardware and workload, easier to develop against and extend.

## How it works

1. The machine PXE boots the kernel (`vmlinuz`) and initramfs (`initramfs`) or runs the UEFI-bootable ISO image
2. A custom `/init` script transitions the rootfs to tmpfs, then exec's systemd
3. systemd-networkd configures DHCP on all ethernet interfaces
4. containerd starts, then `tink-agent-setup` pulls the tink-agent container image (configured via kernel cmdline), extracts the binary, and runs it as a host process
5. tink-agent connects to the Tinkerbell server and executes provisioning workflows

## Architecture

The build has four stages:

1. **Kernel compilation** (`./build.py kernel`) — builds a Linux kernel from source using defconfigs from `kernel.configs/`
2. **Tool download** (`./build.py tools`) — fetches pinned binary releases of the container runtime stack
3. **Initramfs build** (`./build.py initramfs`) — assembles a Debian Trixie CPIO initramfs with systemd, injecting the kernel, modules, and tools using `mkosi`
4. **ISO assembly** (`./build.py iso`) — builds a UEFI-bootable ISO with GRUB via `grub-mkrescue`

## Usage

**Prerequisites:** `uv` (Python), Docker.

```bash
# Install Astral's `uv` if you don't have it: https://docs.astral.sh/uv/getting-started/installation/
curl -LsSf https://astral.sh/uv/install.sh | sh # then re-log in

# Build with defaults (amd64, kernel 6.18.16)
uv run ./build.py --help

usage: build.py [flags]

Build CaptainOS images. Stages: kernel → tools → initramfs → iso.

options:
  -h, --help                          show this help message and exit

build configuration:
  --arch {amd64,arm64}                target architecture (default: amd64)
  --builder-image IMAGE               Docker builder image name (default: captainos-builder)
  --no-cache                          rebuild builder image without Docker cache

kernel:
  --kernel-version VER                kernel version to build (default: 6.18.16)
  --kernel-src PATH                   path to local kernel source tree
  --kernel-mode {docker,native,skip}  kernel stage execution mode (default: docker)
  --force-kernel                      force kernel rebuild even if outputs exist

tools:
  --tools-mode {docker,native,skip}   tools stage execution mode (default: docker)
  --force-tools                       re-download tools even if outputs exist

initramfs (mkosi):
  --mkosi-mode {docker,native,skip}   mkosi stage execution mode (default: docker)
  --force                             passed through to mkosi as --force

iso:
  --iso-mode {docker,native,skip}     iso stage execution mode (default: docker)
  --force-iso                         force ISO rebuild even if outputs exist

commands:
  build          Run all build stages: kernel → tools → initramfs → iso (default)
  kernel         Build only the kernel + modules
  tools          Download tools (containerd, runc, nerdctl, CNI)
  initramfs      Build only the initramfs via mkosi
  iso            Build a UEFI-bootable ISO image
  checksums      Compute SHA-256 checksums for specified files
  shell          Interactive shell inside the builder container
  clean          Remove all build artifacts
  summary        Print mkosi configuration summary
  qemu-test      Boot the image in QEMU for testing

```

Output artifacts are placed in `out/`:

- `out/vmlinuz-<kver>-<arch>` — the kernel
- `out/initramfs-<kver>-<arch>` — the initramfs
- `out/captainos-<kver>-<arch>.iso` — UEFI-bootable ISO image
- `out/sha256sums-<kver>-<arch>.txt` — SHA-256 checksums

Here `<kver>` is the kernel version (e.g. `6.18.16`) and `<arch>` is the Linux architecture name (`x86_64` or `aarch64`).

## Release

CI publishes build artifacts as OCI images on every push to `main`. Pushing a version tag (`v*`) creates a GitHub Release with downloadable files and tags the OCI images with the release version.

### OCI artifact images

Three multi-arch OCI indexes are published per build:

| Image | Tag | Contents |
| --- | --- | --- |
| amd64-only | `vX.Y.Z-<sha7>-amd64` | vmlinuz, initramfs, ISO, checksums (x86_64) |
| arm64-only | `vX.Y.Z-<sha7>-arm64` | vmlinuz, initramfs, ISO, checksums (aarch64) |
| combined | `vX.Y.Z-<sha7>` | all artifacts from both architectures |

Each artifact file is pushed as its own OCI layer. Deterministic tar creation (zeroed metadata) ensures identical layer digests across per-arch and combined images, so registries deduplicate shared blobs — the combined image adds zero additional storage.

All three images are multi-arch OCI indexes with `linux/amd64` and `linux/arm64` platform entries pointing to the same content, so any platform can pull them. Images are compatible with:

- **containerd** — valid `rootfs.diff_ids` in the config; Kubernetes image-volume mounts work
- **skopeo** — extracts individual artifact files for release workflows

### GitHub Release

When a `v*` tag is pushed, the release workflow:

1. Pulls the combined OCI image (both architectures)
2. Attaches all artifacts as downloadable files on the GitHub Release page:
   - `vmlinuz-<kver>-x86_64`, `initramfs-<kver>-x86_64`, `captainos-<kver>-x86_64.iso`, `sha256sums-<kver>-x86_64.txt`
   - `vmlinuz-<kver>-aarch64`, `initramfs-<kver>-aarch64`, `captainos-<kver>-aarch64.iso`, `sha256sums-<kver>-aarch64.txt`
3. Tags all three OCI images with the clean release version (`vX.Y.Z`, `vX.Y.Z-amd64`, `vX.Y.Z-arm64`)

### Release subcommands

```bash
# Publish artifacts as a multi-arch OCI image
./build.py release publish --target amd64

# Pull and extract artifacts
./build.py release pull --target combined --pull-output ./out/release/

# Tag all artifact images with a release version
./build.py release tag v1.0.0
```

Run `./build.py release <subcommand> -h` for full flag reference.

## Build modes

Each stage can be executed in one of three modes:

- `docker` (default) — runs the stage inside a Docker container, providing a consistent build environment regardless of host OS.
- `native` — runs the stage directly on the host machine. Requires all build dependencies to be installed and configured correctly.
- `skip` — skips the stage entirely.

### Setting modes

| Mode | CLI flag | Env var | Example |
| --- | --- | --- | --- |
| `docker` | `--{stage}-mode docker` | `{STAGE}_MODE=docker` | `--kernel-mode docker` |
| `native` | `--{stage}-mode native` | `{STAGE}_MODE=native` | `--tools-mode native` |
| `skip` | `--{stage}-mode skip` | `{STAGE}_MODE=skip` | `--iso-mode skip` |

### Included tools

| Component | Version | Purpose |
| --- | --- | --- |
| containerd | 2.2.1 | Container runtime |
| nerdctl | 2.2.1 | Container CLI (Docker-compatible) |
| runc | 1.4.0 | OCI runtime |
| CNI plugins | 1.6.0 | Container networking (bridge, host-local, loopback, portmap, firewall, tuning) |

### Key design decisions

- **Custom `/init` instead of `MakeInitrd`** — systemd's initrd mode expects to switch-root to a real rootfs. CaptainOS runs entirely from RAM, so a custom init transitions rootfs → tmpfs before exec'ing systemd. This makes `pivot_root(2)` work for container runtimes.
- **No UPX compression** — the final image is compressed with zstd level 19. Raw ELF binaries compress better under zstd than UPX-packed ones (UPX output looks like random data to zstd).
- **iptables-nft backend** — uses the nftables-backed iptables for container networking, with the necessary `CONFIG_NF_TABLES_*` kernel options enabled.
- **IP forwarding via sysctl** — enabled at boot for container network traffic.

## Project layout

```bash
.
├── build.py                    # Main build entry point (Python >= 3.13; use `uv run build.py`)
├── captain/                    # Build system package (stdlib only)
│   ├── __init__.py             # Package init incl logging
│   ├── cli.py                  # CLI subcommands (argparse)
│   ├── config.py               # Configuration from environment
│   ├── docker.py               # Docker builder management
│   ├── kernel.py               # Kernel compilation logic
│   ├── tools.py                # Binary tool downloader
│   ├── artifacts.py            # Artifact collection & checksums
│   ├── oci.py                  # OCI artifact publish/pull/tag
│   ├── buildah.py              # buildah CLI wrapper (image construction)
│   ├── skopeo.py               # skopeo CLI wrapper (inspect/copy/export)
│   ├── iso.py                  # ISO image assembly
│   ├── qemu.py                 # QEMU boot testing
│   └── util.py                 # Shared helpers & arch mapping
├── Dockerfile                  # Builder container definition
├── Dockerfile.release          # Lightweight container for OCI release ops
├── mkosi.conf                  # mkosi image configuration
├── mkosi.postinst              # Post-install hooks (symlinks, cleanup)
├── mkosi.finalize              # Final image adjustments
├── config/
│   ├── defconfig.amd64         # Kernel config for x86_64
│   └── defconfig.arm64         # Kernel config for aarch64
└── mkosi.extra/                # Files overlaid into the image
    ├── init                    # Custom PID 1 (rootfs → tmpfs → systemd)
    └── etc/
        ├── containerd/         # containerd configuration
        ├── systemd/system/     # systemd units
        ├── acpi/               # ACPI power button handler
        ├── sysctl.d/           # Kernel tunables
        └── os-release          # OS identification
```

## Testing with QEMU

```bash
./build.py qemu-test -h

usage: build.py qemu-test [flags]

Boot the image in QEMU for testing

options:
  -h, --help                       show this help message and exit

build configuration:
  --arch {amd64,arm64}             target architecture (default: amd64)
  --builder-image IMAGE            Docker builder image name (default: captainos-builder)
  --no-cache                       rebuild builder image without Docker cache

qemu:
  --qemu-append ARGS               extra kernel cmdline args for qemu-test
  --qemu-mem SIZE                  QEMU RAM size (default: 2G)
  --qemu-smp N                     QEMU CPU count (default: 2)

tinkerbell:
  --tink-worker-image IMAGE        tink-agent container image reference (default: ghcr.io/tinkerbell/tink-
                                   agent:latest)
  --tink-docker-registry HOST      registry host (triggers tink-agent services)
  --tink-grpc-authority ADDR       tink-server gRPC endpoint (host:port)
  --tink-worker-id ID              machine / worker ID
  --tink-tls BOOL                  enable TLS to tink-server (default: false)
  --tink-insecure-tls BOOL         allow insecure TLS (default: true)
  --tink-insecure-registries LIST  comma-separated insecure registries
  --tink-registry-username USER    registry auth username
  --tink-registry-password PASS    registry auth password
  --tink-syslog-host HOST          remote syslog host
  --tink-facility CODE             facility code
  --ipam PARAM                     static networking IPAM parameter
```

This boots the image in QEMU with a virtio NIC and serial console. `console=ttyS0 audit=0` is always appended to the kernel cmdline. Press `Ctrl-A X` to exit or run `poweroff` inside the VM.

## License

See [Tinkerbell](https://github.com/tinkerbell/captain/blob/main/LICENSE) for license information.
