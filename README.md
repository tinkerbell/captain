# CaptainOS

A minimal, systemd-based in-memory OS for [Tinkerbell](https://tinkerbell.org) bare-metal provisioning.

CaptainOS boots via PXE/iPXE, runs entirely from RAM as a compressed CPIO initramfs, and provides a container runtime
environment for the [tink-agent](https://github.com/tinkerbell/tinkerbell) — the component that drives hardware
provisioning workflows.

The same building blocks also produce images for what gets provisioned: Kubernetes nodes for
[CAPT](https://github.com/tinkerbell/cluster-api-provider-tinkerbell), either in-memory or as disk images for final
deployment. Debian, Ubuntu and Rocky Linux bases are available; see [Flavors](#flavors).

## Why does CaptainOS exist?

CaptainOS is the next generation of Tinkerbell's in-memory OS, building on years of experience building, maintaining,
and operating [HookOS](https://github.com/tinkerbell/hook) in production.
It is built with [mkosi](https://github.com/systemd/mkosi), producing a minimal systemd-based Debian OS that runs
entirely from RAM.

- **Significantly smaller initramfs** — small enough to boot comfortably on resource-constrained single-board computers.
- **No Docker-in-Docker** — tink-agent runs directly on the host with containerd, giving it native access to the
  container runtime without any nesting.
- **Familiar operations** — systemd foundation with journalctl, networkd, and standard service management make debugging
  and troubleshooting straightforward.
- **Simpler architecture** — fewer layers between hardware and workload, easier to develop against and extend.

## How it works

1. The machine PXE boots the kernel (`vmlinuz`) and initramfs (`initramfs`) or runs the UEFI-bootable ISO image
2. A custom `/init` script transitions the rootfs to tmpfs, then exec's systemd
3. systemd-networkd configures DHCP on all ethernet interfaces
4. containerd starts, then `tink-agent-setup` pulls the tink-agent container image (configured via kernel cmdline),
   extracts the binary, and runs it as a host process
5. tink-agent connects to the Tinkerbell server and executes provisioning workflows

## Build model

The current CLI is click-based (`captain`) with flavor-driven image generation.

- `captain build` runs: **tools -> mkosi (initramfs or disk image, per flavor) -> iso (if flavor supports it) ->
  collect artifacts**
- `captain kernel` is a separate stage used when building kernel artifacts directly (for example `trixie-slim`
  workflows)
- The flavor's traits are rendered into a disposable `mkosi.generated/` tree, one mkosi drop-in directory per trait
  (`mkosi.conf.d/10-core`, `20-distro`, …, `90-custom`); mkosi runs with `--directory=mkosi.generated`
- `mkosi.output/` keeps hashed per-flavor build outputs; `out/` gets stable publishable filenames

See `docs/design-decisions/flavor-traits.md` for the design (with diagrams), and `docs/gotchas.md` for known pitfalls.

## Build system Usage

**Prerequisites:** `uv` (Python), Docker (with buildx/BuildKit).

- On Debian, the regular Debian-supplied `docker.io` and `docker-cli`/`docker-buildx` packages work fine. You can also
  use Docker CE from Docker Inc if you prefer.
- On MacOS with brew, `colima` is a good alternative to Docker Desktop or similar.

```bash
# Install Astral's `uv` if you don't have it: https://docs.astral.sh/uv/getting-started/installation/
curl -LsSf https://astral.sh/uv/install.sh | sh # then re-log in

# top-level help
uv run captain --help
```

### Core commands

**Important**: please pay attention to the level flags are defined. For example, `--flavor-id` is a global option and
needs to be passed after `uv run captain` but before the subcommand (`build`, `kernel`, etc).
We're investigating ways to make this more flexible using Python's Click library, but for now please follow the
documented usage.

```bash
uv run captain list-flavors
uv run captain builder --help
uv run captain build --help
uv run captain kernel --help
uv run captain tools --help
uv run captain iso --help
uv run captain qemu --help
uv run captain release --help
uv run captain shell --help
```

## Flavors

Flavors are listed in the registry `captain/flavors.py`. Each one is composed of one trait per axis — distro, platform
(firmware interface + kernel), role and output — from `captain/traits/`; each trait contributes packages, hooks and
mkosi drop-in content (see `captain/traits/__init__.py`). Here is a current list:

| Flavor                     | Architectures    | ISO | Description                                                                                                           |
|----------------------------|------------------|-----|-----------------------------------------------------------------------------------------------------------------------|
| `trixie-full`              | `amd64`, `arm64` | yes | Debian Trixie, standard Debian linux-generic kernel, working `apt` package manager                                    |
| `trixie-slim`              | `amd64`, `arm64` | yes | Kernel built from source, slimmed down Debian userspace, ACPI/UEFI only                                               |
| `trixie-armbian-rpi`       | `arm64`          | no  | For RaspberryPi's, using Armbian's bcm2711 kernel; produces `firmware-rpi/` which is ready-to-netboot on RPi's        |
| `trixie-meson64`           | `arm64`          | no  | For Amlogic boards, using Armbian's meson64 kernel                                                                    |
| `trixie-rockchip64`        | `arm64`          | no  | For Rockchip boards (rk35xx, rk33xx, etc) using Armbian's _mainline_ rockchip64 kernel                                |
| `trixie-rockchip64-vendor` | `arm64`          | no  | For Rockchip boards (rk35xx) using the Rockchip vendor kernel from Armbian. Prefer the non-vendor flavor if possible. |
| `trixie-genio`             | `arm64`          | no  | For MediaTek Genioboards, using Armbian's genio kernel                                                                |
| `resolute-full`            | `amd64`, `arm64` | yes | Ubuntu 26.04 Resolute, standard Ubuntu linux-image-generic kernel (no linux-firmware), working `apt`                  |
| `resolute-rockchip64`      | `arm64`          | no  | Ubuntu 26.04 Resolute for Rockchip boards, using Armbian's _mainline_ rockchip64 kernel                               |
| `rockylinux10-full`        | `amd64`, `arm64` | yes | Rocky Linux 10 (+EPEL), standard Rocky kernel, working `dnf`. amd64 requires x86-64-v3 (Haswell or newer) CPUs        |
| `mem-k8s-capt-<base>`      | as `<base>`      | as  | In-memory Kubernetes node for CAPT (kubeadm/kubelet/kubectl, containerd CRI, cloud-init for Tootles)                  |
| `img-k8s-capt-<base>`      | as `<base>`      | no  | Kubernetes node **disk image** for CAPT (as above, plus ssh; root autologin on consoles, password locked; users via cloud-init) |

k8s `<base>`s: `trixie-full`, `resolute-full`, `rockylinux10-full`, `trixie-rockchip64`, `trixie-armbian-rpi`.

The flavors above without a prefix run tink-agent from memory. `mem-k8s-capt-*` flavors install Kubernetes from
pkgs.k8s.io; pick the minor with `--kubernetes-version` (or environment `KUBERNETES_VERSION`, default `1.36`).

`img-*` flavors produce a raw GPT disk image (`out/captainos-<flavor-id>-<arch>.img.zst`) for final deployment
(eg. Tinkerbell's `image2disk`/`oci2disk`), booting with the distro's own initrd and `root=PARTLABEL=root`:

- UEFI/ACPI bases: ESP with systemd-boot (removable-media path) and one BLS entry.
- DeviceTree (Armbian) bases: a single root partition with `/boot/extlinux/extlinux.conf` for U-Boot, which is assumed
  to live on the board's SPI/eMMC boot partitions.
- Raspberry Pi (`trixie-armbian-rpi`), Pi 4B and newer: no U-Boot; a vfat `boot` partition with the RPi firmware,
  `kernel8.img`, initrd, DTBs/overlays and a generic `config.txt`/`cmdline.txt` (provisioning workflows may customize
  them). The in-memory `mem-k8s-capt-trixie-armbian-rpi` netboots like `trixie-armbian-rpi` instead (Tinkerbell
  serves `config.txt`/`cmdline.txt`).

cloud-init grows the root partition on first boot. `captain qemu` boots `img-*` flavors from a scratch copy of the
image grown to `--disk-size`, seeding cloud-init over NoCloud (via SMBIOS) with a `captain` user holding `--ssh-key`;
then `ssh -p 2222 captain@localhost`.

Use `--flavor-id` (alias `--flavor`, or environment `FLAVOR_ID`) to select one. `uv run captain list-flavors` prints the registry; CI
builds every registered flavor, or those `custom/builds.txt` selects (matrices come from
`captain list-flavors --github-output`), and releases tag each one
as `<tag><suffix>` (see `release_tag_suffix` in `captain/flavors.py`).

## Build modes

Most stages support `docker | native | skip`:

- `--tools-mode` / `TOOLS_MODE`
- `--mkosi-mode` / `MKOSI_MODE`
- `--iso-mode` / `ISO_MODE`
- `--kernel-mode` / `KERNEL_MODE`
- `captain release --release-mode` / `RELEASE_MODE`

Defaults are command-specific (for example, `captain build` defaults to `--tools-mode native`, while `captain tools`
defaults to `--tools-mode docker`). Use `--help` on each command for current defaults.

## Build cache

Tools, kernels, mkosi outputs and ISOs are cached as content-hashed OCI artifacts in `ghcr.io`: your fork's cache
first (detected from git remotes), then `tinkerbell/captain`'s. Unchanged inputs are restored instead of rebuilt, locally
and in CI alike.

- `uv run captain cache info`: what would be read/written, and what's cached for the flavor
- `--cache-push`: push local builds to your fork's cache (without it, the log says `Cache: not pushing (…)` and why);
  `--cache off|read`, `--cache-only`; a cache hit is downloaded (with a progress bar on a terminal) instead of built
- The kernel version and armbian token are resolved like CI does (cached 6h in `mkosi.input/.versions.json`)

See [docs/build-caching.md](docs/build-caching.md).

## Customization via `custom/`

The `custom/` directory is mounted automatically in docker-mode runs and applied to every flavor, as the last mkosi drop-in.

- `custom/packages.txt`: one package per line (comments allowed)
- `custom/files/`: copied into the image (mapped under `mkosi.extra/`)
- `custom/mkosi-postinst/`: scripts for the post-install phase (concatenated, run after every flavor's own)
- `custom/mkosi-finalize/`: scripts for the finalize phase (likewise)
- `custom/builds.txt`: which flavors CI builds and releases (ids or globs, one per line; empty: all). Local builds
  ignore it.

Scripts are Jinja-rendered and get `CAPTAIN_FLAVOR_ID`, `CAPTAIN_DISTRO`, `CAPTAIN_DISTRO_RELEASE`,
`CAPTAIN_DISTRO_FAMILY` (`deb` or `rpm`), `CAPTAIN_ARCH`, `CAPTAIN_ROLE`, `CAPTAIN_OUTPUT`, `CAPTAIN_PLATFORM` and
`CAPTAIN_KERNEL` to branch on. Names in `packages.txt` go through the distro's package-name map
(Debian names are translated on Rocky, eg. `iproute2` → `iproute`); distro-native names pass through unchanged.

### Only for some flavors: `custom/by-<axis>/<value>/`

Inside `custom/`, `by-<axis>/<value>/` dirs shaped exactly like `custom/` itself apply only to matching flavors:

| dir                              | values                                                                  |
|----------------------------------|-------------------------------------------------------------------------|
| `custom/by-family/<value>/`      | `deb`, `rpm`                                                            |
| `custom/by-distro/<value>/`      | `debian`, `debian-trixie`, `ubuntu`, `ubuntu-resolute`, `rocky`, `rocky-10` |
| `custom/by-arch/<value>/`        | `amd64`, `arm64`                                                        |
| `custom/by-role/<value>/`        | `tink`, `k8s-capt`                                                      |
| `custom/by-output/<value>/`      | `mem`, `img`                                                            |
| `custom/by-platform/<value>/`    | `acpi`, `armbian`, `rpi`                                                |
| `custom/by-kernel/<value>/`      | `distro`, `captainos`, `armbian-<branch>-<family>` (eg. `armbian-edge-rockchip64`) |
| `custom/by-flavor/<value>/`      | a flavor id, eg. `trixie-slim`                                          |

All matching dirs apply, general to specific: `custom/`, then the table's order top to bottom. They merge by name:
`packages.txt` add up; a file (by path) or script (by filename) in a more specific dir replaces the general one; scripts
from all dirs run together, sorted by filename. To share one flavor's dir with another, symlink it:
`ln -s trixie-meson64 custom/by-flavor/trixie-genio`. A dir matching no flavor gets a warning (typos). For combinations
(eg. arm64 *and* rpm), use a `custom/by-flavor/` dir or branch on the variables above.

See examples under `custom/` and `custom/by-*/` in this repository (disabled by default). In a fork, commit them and push: CI builds and
publishes your images ([docs/fork-custom-builds.md](docs/fork-custom-builds.md)).

## Output artifacts

Final artifacts are copied to `out/` with flavor-aware names:

- `out/vmlinuz-<flavor-id>-<arch>`
- `out/initramfs-<flavor-id>-<arch>`
- `out/captainos-<flavor-id>-<arch>.iso` (only for ISO-capable flavors)
- `out/dtb-<flavor-id>-<arch>/` (directory, arm64 flavors that export DTBs)
- `out/firmware-rpi/` (directory, in-memory Raspberry Pi flavors: ready-to-netboot firmware with `kernel8.img`,
  `initramfs8` and DTBs symlinked; each RPi build overwrites it)
- `out/captainos-<flavor-id>-<arch>.img.zst` (disk image, `img-*` flavors only; instead of the above)

`<arch>` uses Linux output architecture names (`x86_64`, `aarch64`).

## Release (OCI artifacts)

Release operations are grouped under `captain release`:

```bash
# publish
uv run captain release --target amd64 publish

# pull artifacts into out/release/
uv run captain release --target combined pull --pull-output release

# retag existing images
uv run captain release --src-tag v0.0.0-abcdef1-trixie-full tag --new-tag v1.0.0
```

Notes:

- Base source tag defaults to `v0.0.0-<sha7>-<flavor-id>`.
- Per-arch refs append `-amd64` or `-arm64`; combined uses the bare tag; arm64-only flavors get both.
- `release tag --new-tag <version><suffix>` also tags `<version>-<flavor id>[-<arch>]`, the per-commit tags' naming.
- Images are published once per content (content tag `c-…`), then tagged; `captain release status` tells whether
  the current outputs are published.
- Upstream releases from `v*` tags (GitHub Release + OCI tags). Forks: each push to `main` gets OCI tags
  `v0.0.0-ci.<run number>[<suffix>]`; `CAPTAIN_FORK_GH_RELEASE=true` (repo variable) adds a GitHub prerelease.
- `status` and `tag` need no Docker (captain's own OCI client).
- `publish` computes `sha256sums-<flavor-id>-<arch>.txt` and includes checksums as OCI layers.
- `REGISTRY_INSECURE=1` can be used for insecure registries (propagated to buildah/skopeo wrappers).

For the rationale behind the Buildah + Skopeo approach, see `docs/design-decisions/oci-tooling-buildah-skopeo.md`.

## Testing with QEMU

`captain qemu` boots artifacts from `out/`. In-memory flavors boot kernel + initramfs directly and forward additional
arguments to the kernel cmdline; `img-*` flavors boot the disk image under UEFI (see [Flavors](#flavors)).

```bash
# boot with defaults
uv run captain --arch amd64 --flavor-id trixie-full qemu

# pass extra kernel cmdline args after --
uv run captain --arch amd64 --flavor-id trixie-full qemu -- tink_worker_image=ghcr.io/tinkerbell/tink-agent:latest

# more memory/CPUs (eg. for kubeadm init on mem-k8s-capt-* flavors); also QEMU_MEM / QEMU_SMP
uv run captain --arch arm64 --flavor-id mem-k8s-capt-trixie-full qemu --mem 6G --smp 4

# disk image: boots a scratch copy grown to --disk-size, cloud-init user 'captain' gets --ssh-key
uv run captain --arch arm64 --flavor-id img-k8s-capt-trixie-full qemu --ssh-key ~/.ssh/id_ed25519.pub
ssh -p 2222 captain@localhost
```

You will need OVMF firmware for the architecture installed on the host system:

- On MacOS with brew, OVMF builds for both x86 and arm64 is included with the `qemu` package.
- On Linux, you can install `ovmf` from your package manager:
    - Debian: x86: `ovmf`, arm64: `qemu-efi-aarch64`
    - Fedora: `edk2-ovmf` (both architectures)

Booting disk images also needs `zstd` and `qemu-img` (part of `qemu`) on the host.

On native-host architecture runs, acceleration is enabled when available (`hvf` on macOS, `kvm` on Linux).

Exit QEMU with `Ctrl-a x` (that's `Ctrl` + `a`, release, then press `x`).

## Project layout

```text
.
|- build.py
|- captain/
|  |- cache/                  # content-hashed build cache (OCI artifacts in registries)
|  |- cli/                    # click CLI groups/commands
|  |- traits/                 # flavor building blocks (distro/platform/role/output) and their mkosi content
|  |- flavor.py               # flavor composition and mkosi.generated/ rendering
|  |- flavors.py              # the flavor registry
|  |- oci/                    # OCI publish/pull/tag logic
|  |- artifacts.py            # copy outputs into out/
|  |- docker.py               # builder image + docker relaunch helpers
|  |- iso.py                  # ISO assembly
|  |- kernel.py               # kernel build logic
|  |- qemu.py                 # qemu launcher
|  `- tools.py                # pinned tool downloads
|- custom/                    # user customizations (all flavors)
|  `- by-<axis>/<value>/      # user customizations for matching flavors only
|- docs/                      # documentation (see below)
|- kernel.configs/            # kernel configuration (defconfig) for trixie-slim kernel builds
|- mkosi.generated/           # rendered mkosi config tree for the current flavor (disposable)
|- mkosi.input/               # build inputs (generated and cached)
|- mkosi.output/              # per-flavor hashed mkosi outputs
`- out/                       # final artifacts for release/use
```

## Documentation

- [docs/build-caching.md](docs/build-caching.md): content-hashed build cache (OCI registries), CI, release content tags
- [docs/fork-custom-builds.md](docs/fork-custom-builds.md): custom builds and versioned images from a fork
- [docs/gha-uv-python-deps-caching.md](docs/gha-uv-python-deps-caching.md): uv, CPython and dependency caching in CI
- [docs/kernel-build.md](docs/kernel-build.md): the CaptainOS kernel (`trixie-slim`)
- [docs/gotchas.md](docs/gotchas.md): known pitfalls
- Design decisions: [flavor traits](docs/design-decisions/flavor-traits.md),
  [OCI tooling (buildah/skopeo)](docs/design-decisions/oci-tooling-buildah-skopeo.md),
  [replacing CAPT's image-builder](docs/design-decisions/k8s-capt-image-builder-replace.md)

## License

See [Tinkerbell](https://github.com/tinkerbell/captain/blob/main/LICENSE) for license information.
