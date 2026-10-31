# Gotchas

Stumbling blocks hit while building the trait-based flavors (Rocky, Ubuntu, k8s role, disk images).

## Flavor generation / templating

- The old generator walked `rglob("*")` + `is_file()`, silently **dropping symlinks**: unit masks (`-> /dev/null`) and
  `default.target` never reached the image. Symlinks are now copied as symlinks.
- Jinja strips one trailing newline by default; static `*.j2` files are rendered with `keep_trailing_newline=True`.
- Bash `${#array[@]}` contains `{#`, which Jinja reads as a comment start: wrap such code in `{% raw %}`.
- Use `{% if … -%}` / `{% endif -%}` in line-oriented templates, or you get stray blank lines.
- A `*.j2` that renders empty is skipped: used for family-specific files (apt list vs yum repo) in one trait.
- ruff RUF012: mutable class attributes (eg. `package_map` dicts) need `ClassVar[...]` annotations.

## mkosi

- `mkosi.conf.d/<dir>/` drop-in dirs are full config dirs: own `mkosi.conf`, `mkosi.extra/`, `mkosi.sandbox/`,
  `mkosi.postinst`, `mkosi.finalize`, `mkosi.repart/`. Lists append, later scalars win, trees overlay, all scripts run.
- **Relative paths** in a drop-in *dir* resolve against that drop-in; in a drop-in *file* or top-level config, against
  `--directory`. Hence `../../../<path>` (`flavor.project_relative()`).
- `CacheDirectory=` relative to a disposable generated dir gets wiped on every regeneration: point it outside.
- Repo files in `mkosi.sandbox/` are additive: mkosi only writes its own `mkosi.sources` / `mkosi.repo` next to them.
  They are build-time only; ship a copy in `mkosi.extra/` for apt/dnf to work in the image.
- Building RPM distros on a Debian builder needs `dnf`, `rpm` **and** `distribution-gpg-keys` (else: "GPG key not found").
- `Format=disk` needs `systemd-repart`, a separate package on Debian (not in `systemd`).
- "Could not rename … located on different devices" in mkosi output is harmless.
- `Repositories=universe` (Ubuntu) / `Repositories=epel` (Rocky) add components/repos to mkosi's defaults.

## systemd-repart / disk images

- repart may format the ESP with **4K sectors**; EDK2 then sees no filesystem on a 512-byte-sector disk (falls to the
  EFI shell). Pin `SectorSize=512`.
- With an ESP defined, the root partition's `CopyFiles=/` ends up with an **empty `/boot`**. Fine for UEFI (kernel +
  initrd live on the ESP) and RPi (they live on the vfat `boot` partition); extlinux layouts (no ESP) keep `/boot`.
- RPi disk images use an `esp`-typed vfat `boot` partition on GPT: the Pi 4/5 EEPROM reads GPT (Pi 3's `bootcode.bin`
  doesn't, hence Pi 4B and newer only). The RPi kernel ships bcm2712 (Pi 5) DTBs too; all are copied.
- The RPi firmware (`start4.elf`) prepends its own kernel args, including `cgroup_disable=memory`: kubeadm then fails
  preflight (`missing required cgroups: memory`). The disk image's `cmdline.txt` appends `cgroup_enable=memory
  cgroup_memory=1`; netbooted (`mem-k8s-capt-*-rpi`) nodes need the same in the `cmdline.txt` Tinkerbell serves.
- `out/firmware-rpi/` has the same name for every in-memory RPi flavor: locally, the last build wins (CI builds each
  flavor in its own job, so its artifacts don't collide).
- systemd-repart 257 **ignores `Flags=`** (eg. 0x4, legacy BIOS bootable). U-Boot distro boot falls back to partition 1.
- `qemu-img resize` leaves the backup GPT mid-disk: harmless, firmware and growpart cope.
- On Rocky, systemd's gpt-auto generator mounts the ESP at `/boot` (empty `/boot`).
- `dracut` defaults to host-only on Rocky; in a build container that's the container's "host": use `--no-hostonly`.
- `openssh-server` generates host keys at install: delete them in finalize, or every machine shares them.
- Minimal images have no `sudo`; cloud-init's `sudo:` user rules are useless without it.
- Debian arm64 `vmlinuz` is already a PE/EFI image; gzip-compressed arm64 kernels must be decompressed for
  systemd-boot / U-Boot `booti`.
- Distro package upgrades of the kernel don't refresh the ESP copy or `extlinux.conf`: images are rebuilt, not upgraded.

## Distros

- **Rocky's bash** (as `/bin/sh` for `/init`) has no sbin in its default `PATH` → `switch_root: not found` → panic.
  `/init` now exports `PATH` itself (Debian's dash default includes sbin).
- **Ubuntu 26.04** moved `switch_root` to `util-linux-extra`.
- Ubuntu `linux-image-generic` depends on `linux-firmware` (~770M of firmware: 855M initramfs). The empty
  `linux-firmware-minimal` provides it (206M).
- DTB locations differ per **kernel provider**, not per distro: Debian/Armbian `/usr/lib/linux-image-<kver>`, Ubuntu
  `/usr/lib/firmware/<kver>/device-tree`, Rocky `/usr/lib/modules/<kver>/dtb` (+ `/boot/dtb-<kver>`). Armbian kernels
  keep the Debian layout on Ubuntu too.
- armbian-next has no `armbian-resolute` suite (only bookworm/trixie/forky); kernels live in the shared `armbian` suite.
- Rocky's `kernel` hard-requires `dracut`; there is no tiny-initramfs equivalent.
- EL10: `systemd-networkd`, `systemd-timesyncd`, `fastfetch`, `pv` are EPEL-only; `ebtables` is gone; `login` is in
  `util-linux`; `systemd-sysv` doesn't exist; `systemd-boot-unsigned` is in AppStream.
- Rocky 10 amd64 requires x86-64-v3 (Haswell+).
- Debian's `linux-image-*` needs *an* initramfs generator: `tiny-initramfs` satisfies it cheaply for in-memory images.
- CaptainOS' `/etc/os-release` hardcoded `ID_LIKE=debian`; now per distro.

## Kubernetes / cloud-init

- kubeadm preflight (`SystemVerification`) needs the kernel config: the mem output deletes `/boot`, and disk images
  with an ESP lose it too. Keep it as `/usr/lib/modules/<kver>/config` (Rocky ships it there natively).
- pkgs.k8s.io uses the same signing key for every minor and for deb/rpm; apt accepts an armored `.asc` in `signed-by`.
- cloud-init's Ec2 datasource waits **240s** for metadata by default: boot stalls where there's no Tootles. Cap
  `max_wait`/`timeout`.
- ds-identify may disable cloud-init on non-AWS hardware: `policy: enabled` in `/etc/cloud/ds-identify.cfg`.
- kubelet shows `activating` (crash-looping) until `kubeadm init/join` writes its config: expected.
- After `kubeadm init` without a CNI: node `NotReady`, CoreDNS `Pending`: expected.
- `kubeadm init` needs ≥2 CPUs and ~1.7G free; an in-memory rootfs eats RAM too (test with `--mem 6G`).
- containerd needs `SystemdCgroup = true` to match kubelet's systemd cgroup driver; the tink config disables CRI entirely.

## custom/

- The example postinst's registry mirrors set `server =` to the mirror: **no fallback** to upstream, so pulls hang
  wherever that Harbor is unreachable. Keep the examples disabled in committed code.
- The example postinst assumed Debian's CA store (`update-ca-certificates`); scripts now get `CAPTAIN_DISTRO_FAMILY`
  (and `CAPTAIN_DISTRO`, `CAPTAIN_DISTRO_RELEASE`, `CAPTAIN_FLAVOR_ID`, `CAPTAIN_ARCH`, `CAPTAIN_ROLE`,
  `CAPTAIN_OUTPUT`, `CAPTAIN_PLATFORM`, `CAPTAIN_KERNEL`) to branch on.
- `custom/by-<axis>/<value>/` dirs only apply when the name matches exactly (eg. `custom/by-distro/debian-trixie`, not
  `trixie`); unmatched ones get a warning, never an error. Same-named scripts/files in a more specific layer *replace*
  the general one, they don't add to it.

## Build tooling / CI

- `custom/builds.txt` lists every flavor commented out, but new flavors aren't added to it automatically (globs still
  match them). `.github/mergify.yml` requires specific flavor jobs: a fork that deselects them must adjust it, if it
  uses Mergify.
- `--flavor-id` (alias `--flavor`) and `--arch` are **global** options: `captain --flavor-id X build`, not
  `captain build --flavor-id X`.
- The cache keys hash whole modules (eg. `tools.py`, `kernel.py`, `docker.py`; see `cache/_keys.py`): even a log-only
  edit there rebuilds everything downstream.
- Values folded into the flavor hash (`ARMBIAN_VERSION`, `KUBERNETES_VERSION`) must be passed into the builder
  container, or in-container stages (`captain iso`, release) compute a different hash and miss the outputs.
- `docker run -v` on a non-existent host path creates it as root: create generated dirs first.
- Builder containers run as root; on Linux hosts their outputs are chown'ed back to the host user by a wrapper in the
  same container (`docker._OWNED_WRAPPER`, the stage's `owned_paths`), even after a failure. A stage writing somewhere
  new must add that path, or later host-side steps (eg. cache restore's `rmtree`) hit root-owned files.
- The `captain kernel` / `captain iso` stages import `captain/` from the live bind mount: editing code mid-build can
  change what runs (or the computed hash).
- Builder image is tagged by Dockerfile hash: any Dockerfile change rebuilds it (minutes).
- `kernel.configs/.config.resolved.*` is generated by kernel builds and isn't gitignored.
- Combined (multi-arch) publish applies to img flavors too, which have no ISO: ISO downloads are conditional.
- `while read` loops in workflows: give inner commands `< /dev/null` so they can't eat the loop's input.
- Published release tag suffixes are irregular (`""` for trixie-full, `-slim`, `-rpi`, …): kept via
  `release_tag_suffix`; new flavors default to `-<flavor-id>`.

## Local testing (macOS)

- Docker Desktop can't see `/private/tmp` / `/private/var`: keep files shared with containers inside the project
  (`.local-test/`, excluded via `.git/info/exclude`).
- No `zstandard` Python dep: the qemu disk boot shells out to `zstd`.
- NoCloud seed without ISO tools: SMBIOS `serial=ds=nocloud;s=http://10.0.2.2:<port>/` + a local HTTP server.
- `qemu -nographic` serial prompts differ (`root@host:~#` vs `[root@host ~]#`); autologin prints
  `login: root (automatic login)` and later `Last login:`: easy to mis-match in expect scripts.
- The EFI shell wants `\r`, not `\n`.
- Debian's builder has no `fdisk`/`sfdisk` by default (install it to inspect images).
