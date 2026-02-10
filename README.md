# CaptainOS

A minimal, systemd-based in-memory OS for [Tinkerbell](https://tinkerbell.org) bare-metal provisioning.

CaptainOS boots via PXE/iPXE, runs entirely from RAM as a compressed CPIO initramfs, and provides a container runtime environment for the [tink-agent](https://github.com/tinkerbell/tinkerbell) — the component that drives hardware provisioning workflows.

## Output sizes (amd64)

| Artifact | Size |
| --- | --- |
| `initramfs-amd64.cpio.zst` | ~88 MB |
| `vmlinuz-amd64` | ~9.5 MB |

## How it works

1. The machine PXE boots a kernel (`vmlinuz`) and initramfs (`initramfs.cpio.zst`)
2. A custom `/init` script transitions the rootfs to tmpfs, then exec's systemd
3. systemd-networkd configures DHCP on all ethernet interfaces
4. containerd starts, then `tink-agent-setup` pulls the tink-agent container image (configured via kernel cmdline), extracts the binary, and runs it as a host process
5. tink-agent connects to the Tinkerbell server and executes provisioning workflows

## Building

**Prerequisites:** Docker (or Podman)

```bash
# Build with defaults (amd64, kernel 6.12.69)
./build.sh

# Build for ARM64
ARCH=arm64 ./build.sh

# Use a local kernel source tree
KERNEL_SRC=~/linux ./build.sh

# Force kernel rebuild
FORCE_KERNEL=1 ./build.sh

# Force tool re-download
FORCE_TOOLS=1 ./build.sh

# Rebuild builder image without cache
NO_CACHE=1 ./build.sh
```

Output artifacts are placed in `out/`:

- `out/initramfs-<arch>.cpio.zst` — the initramfs
- `out/vmlinuz-<arch>` — the kernel

### Other commands

```bash
./build.sh shell       # Interactive shell inside the builder container
./build.sh clean       # Remove build artifacts
./build.sh summary     # Print mkosi configuration summary
./build.sh qemu-test   # Boot the image in QEMU for quick testing
```

## Architecture

The build has three stages, all running inside a Docker container:

1. **Kernel compilation** (`scripts/build-kernel.sh`) — builds a Linux kernel from source using minimal defconfigs (`config/defconfig.{amd64,arm64}`)
2. **Tool download** (`scripts/download-tools.sh`) — fetches pinned binary releases of the container runtime stack
3. **mkosi image build** (`mkosi.conf`) — assembles a Debian Trixie CPIO initramfs with systemd, injecting the kernel, modules, and tools

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

## Kernel cmdline parameters

CaptainOS reads provisioning configuration from the kernel command line:

| Parameter | Description |
| - | - |
| `tink_worker_image` | Container image for the tink-agent (e.g. `registry.example.com/tink-agent:latest`) |
| `docker_registry` | Registry to pull from |
| `registry_username` | Registry auth username |
| `registry_password` | Registry auth password |
| `tinkerbell_tls` | Set to `false` to disable TLS for tink-agent |
| `syslog_host` | Remote syslog host (IP or hostname) |
| `syslog_port` | Remote syslog port (default: 514) |
| `insecure_registries` | Comma-separated list of registries to configure as HTTP |

## Project layout

```bash
.
├── build.sh                    # Main build orchestrator
├── Dockerfile                  # Builder container definition
├── mkosi.conf                  # mkosi image configuration
├── mkosi.postinst              # Post-install hooks (symlinks, cleanup)
├── mkosi.finalize              # Final image adjustments
├── config/
│   ├── defconfig.amd64         # Kernel config for x86_64
│   └── defconfig.arm64         # Kernel config for aarch64
├── scripts/
│   ├── build-kernel.sh         # Kernel compilation script
│   └── download-tools.sh       # Binary tool downloader
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
./build.sh qemu-test

# With extra kernel cmdline parameters
QEMU_APPEND='tink_worker_image=reg.local/tink-agent:latest docker_registry=reg.local' ./build.sh qemu-test

# With more resources
QEMU_MEM=4G QEMU_SMP=4 ./build.sh qemu-test
```

This boots the image in QEMU with a virtio NIC and serial console. `console=ttyS0 audit=0` is always appended. Press `Ctrl-A X` to exit.

## License

See [Tinkerbell](https://github.com/tinkerbell/hook) for license information.
