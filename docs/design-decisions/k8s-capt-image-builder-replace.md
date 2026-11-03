# Replacing CAPT's image-builder with Captain's `img-k8s-capt-*` flavors

## Context

[CAPT](https://github.com/tinkerbell/cluster-api-provider-tinkerbell) ships its own mkosi-based node image builder in
`image-builder/` (Ubuntu 24.04, ~25 files). Captain now has `img-k8s-capt-<base>` flavors (disk images) and
`mem-k8s-capt-<base>` flavors (in-memory), built from the same traits as CaptainOS. This note compares the two, to see
what's missing before Captain's images could replace CAPT's, and what is merely different.

Compared against: CAPT `image-builder/` at `a8b0891`, plus the workflow templates that consume its images
(`controller/machine/template.go` default template, and the one in `docs/QUICK-START.md`).

## What already matches

- kubelet / kubeadm / kubectl / crictl, containerd + runc with `SystemdCgroup = true`, CNI plugins in `/opt/cni/bin`
- `overlay` + `br_netfilter` modules; `bridge-nf-call-ip{,6}tables` and `ip_forward` sysctls; no swap
- kubeadm preflight packages (`conntrack`, `socat`, `ethtool`, `iptables`; `ebtables` except on EL10)
- cloud-init with an EC2 datasource for Tootles (`strict_id: false`), `ds-identify` not disabling it
- `openssh-server`, `sudo`, growpart (`cloud-guest-utils`); host keys removed at build; fresh machine-id on first boot
- containerd, kubelet, cloud-init and ssh enabled; kubelet crash-loops until `kubeadm init/join` (expected)
- Built with mkosi in a Debian trixie container, per arch on native runners

## Differences and concerns

Ordered by impact on actually replacing CAPT's builder.

### 1. Distribution format (blocking)

- **CAPT**: the gzip'd raw disk is pushed with `oras` as a **single-blob OCI artifact**
  (`artifactType: application/vnd.tinkerbell.image.raw.gz`), one tag per arch, eg.
  `ghcr.io/tinkerbell/cluster-api-provider-tinkerbell/ubuntu:2404-v1.35.2-amd64.gz`. `oci2disk` (`COMPRESSED: true`)
  streams that blob to the disk.
- **Captain**: `captainos-<flavor>-<arch>.img.zst` is one file inside a container-style image (tar layers, plus
  checksum files), and multi-arch flavors are published as a combined index.
- `oci2disk` very likely can't consume Captain's images as they are. Options: also push an oras-style single-blob
  artifact per arch for `img-*` flavors, or teach `oci2disk` the layered format.
- Compression: CAPT uses gzip; Captain uses zstd. `oci2disk`/`image2disk` zstd support needs verifying.

### 2. Kubernetes version and image naming (blocking)

- **CAPT** selects the image from the cluster's **exact** Kubernetes version through `imageLookupFormat`
  (`{{.BaseRegistry}}`, `{{.OSDistro}}`, `{{.OSVersion}}` without dots, `{{.KubernetesVersion}}`). The builder installs
  exactly that version (eg. `v1.35.2`) from `dl.k8s.io`, verifying sha256s, and writes `/etc/kubernetes-version`.
- **Captain** installs whatever patch of one minor (`KUBERNETES_VERSION`, default `1.36`) pkgs.k8s.io has at build
  time; tags are `<release-tag>-<flavor-id>`, with no Kubernetes or OS version.
- Needed: pin exact patch versions (pkgs.k8s.io packages allow `=<version>`), probably build several versions in CI,
  and publish under names `imageLookupFormat` can address (distro, OS version, k8s version, arch).

### 3. Partition layout vs. templates (blocking for existing templates)

| Image              | p1                               | p2           | p3              |
|--------------------|----------------------------------|--------------|-----------------|
| CAPT amd64         | ESP 1G (kernels, initrd, GRUB)   | BIOS boot 1M | root ext4 (≥4G) |
| CAPT arm64         | ESP 1G                           | root ext4    | –               |
| Captain UEFI       | ESP 256M (systemd-boot, BLS)     | root ext4    | –               |
| Captain extlinux   | root ext4 (`/boot/extlinux`)     | –            | –               |
| Captain RPi        | vfat `boot` 256M (RPi firmware)  | root ext4    | –               |

- CAPT's quick-start template writes cloud-init config to **p3** (amd64 only) and kexecs from **p1 (vfat)**.
- CAPT's controller default template writes cloud-init config to **p1 as ext4**, which matches neither CAPT's own
  image (p1 is the vfat ESP) nor Captain's UEFI images. It does match Captain's extlinux images.
- Either match CAPT's layout (ESP first, BIOS boot on amd64, root last, same numbers), or ship a Captain-specific
  template. Root partition numbers differing per arch is fragile either way; `PARTLABEL=root` (Captain) avoids it on
  the kernel side but not in templates.
- Root sizing: CAPT root is ≥4G with `GrowFileSystem=yes`; Captain's is minimized (~1.3–1.6G) and grown by cloud-init's
  growpart on first boot.

### 4. Bootloader and kexec

- **CAPT**: GRUB, with signed `shim` (Secure Boot capable); amd64 BIOS + UEFI, arm64 UEFI; GRUB timeout 0;
  `console=ttyS0,115200 console=tty0` on both arches.
- **Captain**: unsigned systemd-boot (no Secure Boot), UEFI only (no BIOS on amd64), serial console only on amd64
  (arm64 relies on ACPI SPCR); DeviceTree boards use extlinux via U-Boot.
- The `kexec` action is given only `BLOCK_DEVICE` (the ESP) and `FS_TYPE`. As far as I can tell it then finds the
  kernel, initrd and command line by parsing `grub.cfg`. Not verified. If so, it can't boot Captain's systemd-boot ESP
  unless the template passes the kernel and initrd paths (`/captainos/linux`, `/captainos/initrd`) and the command line
  (`root=PARTLABEL=root rw`) explicitly, or Captain switches to GRUB.

### 5. Firmware

- CAPT's `linux-generic` pulls the full `linux-firmware`.
- Captain's images carry **no firmware**: Debian's kernel doesn't pull it, Ubuntu uses the empty
  `linux-firmware-minimal` (to keep in-memory images small), Rocky doesn't add `linux-firmware`.
- Fine in memory; risky for deployed bare-metal nodes (some NICs, HBAs, GPUs). Disk images likely want firmware back.

### 6. Package set and trimming

- In CAPT, not in Captain: `open-iscsi`, `nfs-common`, `lvm2` (needed by most storage/CSI drivers), `jq`, `gnupg`,
  `locales`, `bash-completion`.
- Captain's core trait removes `/usr/share/zoneinfo`, `locale`, `i18n` and `bash-completion` for every flavor. That's
  right for in-memory images but questionable for a general-purpose node (eg. cloud-init's timezone module, apps
  expecting zoneinfo). Disk images probably need a lighter trim.

### 7. Networking ownership

- Captain images include CaptainOS's catch-all `80-dhcp.network` (DHCP on every ethernet) and
  `captainos-static-network` (driven by `ipam=` on the kernel command line).
- On a deployed node, cloud-init renders network config from Tootles metadata (bonds, VLANs, static addresses), and the
  two may conflict. CAPT's Ubuntu leaves networking to cloud-init + netplan.
- Which cloud-init network renderer is chosen on Debian trixie / Rocky (networkd, or none) needs checking.

### 8. CaptainOS extras present in disk images

- rsyslog remote forwarding (`syslog_host=` from the kernel command line), banner/motd, `/etc/resolv.conf` pointing at
  the non-stub resolver file, the tink-oriented sysctls. Harmless, but not what CAPT ships; candidates to move from the
  core trait into the `mem` output or the `tink` role.

### 9. Container runtime details

- Versions: CAPT pins containerd 2.2.2, runc v1.2.6, CNI v1.7.1, crictl v1.35.0, pause 3.10 (all overridable).
  Captain: containerd 2.2.1, runc 1.4.0, CNI 1.6.0 (only six plugins from the tools stage; the full set from
  pkgs.k8s.io's `kubernetes-cni`), crictl from `cri-tools`.
- CAPT's containerd config pins the pause image (`sandbox_image`); Captain's uses containerd's default.
- CAPT ships its own kubelet unit + `10-kubeadm.conf` (binaries in `/usr/local/bin`); Captain uses the packages' units.

### 10. Image hygiene and reproducibility

- CAPT sets a fixed `Seed=` (deterministic partition UUIDs); Captain's are random per build.
- CAPT's sysprep also clears apt lists and caches, `/var/lib/systemd/random-seed`, `/tmp`, `/var/tmp`, cloud-init state
  and shell history. Captain keeps package metadata (so `apt`/`dnf` work on the node) and starts with a fresh image;
  `random-seed` and temp dirs aren't explicitly cleaned.
- CAPT has `validate.sh` (mounts the image and checks binaries, units and configs); Captain verifies by booting in QEMU
  locally (`captain qemu` with a NoCloud seed), but not in CI.

### 11. Distro coverage

- CAPT: Ubuntu 24.04 (noble) only; its README says 25.04+ isn't supported with mkosi v26 because of the
  `systemd-repart` packaging change. That's a limit of building on an Ubuntu host: Captain builds resolute (26.04) fine
  from a Debian trixie builder.
- Captain: Debian trixie, Ubuntu 26.04, Rocky Linux 10, and Armbian DeviceTree boards; **no noble**, which existing
  CAPT users and docs currently reference.

### 12. End to end: CAPT templates assume HookOS

- The `kexec` step runs through `waitdaemon`, which mounts `/var/run/docker.sock`. That's HookOS's Docker; CaptainOS
  runs tink-agent on containerd and has no Docker socket. So using CaptainOS as the provisioning OS for CAPT likely
  needs template or action changes, independently of which node image is used.

## Open questions

- Publish an oras-style single-blob artifact for `img-*` flavors in addition to, or instead of, the layered image?
  gzip or zstd?
- Exact Kubernetes patch pinning, and how many versions to build per release; naming compatible with
  `imageLookupFormat`.
- Match CAPT's partition layout, or ship and document a Captain-specific CAPT template?
- GRUB (Secure Boot via shim, BIOS boot on amd64, kexec action compatibility) vs. systemd-boot?
- Firmware and a lighter trim (zoneinfo, locales) for disk images only?
- Who owns networking on deployed nodes: cloud-init only, or CaptainOS's networkd defaults?
- Add a noble base for parity with CAPT today?
- CI validation of disk images (boot in QEMU, or mount-and-check like `validate.sh`)?
