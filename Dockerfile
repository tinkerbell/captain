# Builder container for CaptainOS using mkosi
# Encapsulates all mkosi dependencies for reproducible builds.
# Includes skopeo and buildah for OCI image manipulation, and uv for Python tool management.
FROM debian:trixie

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive BUILDAH_ISOLATION=chroot FORCE_COLOR=1

# Add foreign architecture for cross-compilation (arm64 on amd64 and vice versa) and apt-update
# Immediately install the cross-arch grub dependencies
RUN <<-FRAGMENT_WITH_VARIABLES
# Determine arch/cross-arch and install grub and other basic packages (around 200mb layer)
NATIVE_ARCH="$(dpkg --print-architecture)"
FOREIGN_ARCH=$([ "$NATIVE_ARCH" = "amd64" ] && echo "arm64" || echo "amd64")
dpkg --add-architecture "$FOREIGN_ARCH"
apt-get -o "Dpkg::Use-Pty=0" update
apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends \
    "grub-efi-${NATIVE_ARCH}-bin" \
    "grub-efi-${FOREIGN_ARCH}-bin:${FOREIGN_ARCH}" \
    "libssl-dev:${FOREIGN_ARCH}" \
    grub-common \
    apt \
    dpkg \
    debian-archive-keyring \
    ubuntu-keyring \
    cpio \
    zstd \
    xz-utils \
    kmod \
    systemd-container \
    systemd \
    udev \
    squashfs-tools \
    mtools \
    erofs-utils \
    dosfstools \
    e2fsprogs \
    btrfs-progs \
    tree
FRAGMENT_WITH_VARIABLES

# Cross-architecture support (arm64 on x86_64 and vice versa) - huge single package
RUN <<-QEMU_USER_FRAGMENT
# Install qemu-user but then delete all un-needed qemu binaries to save space (we only need aarch64 and x86_64)
apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends qemu-user
echo 'All qemus: '
ls -lah /usr/bin/qemu-*
# keep only qemu binary for the arches we're interested in: aarch64 and x86_64
echo 'To be deleted: '
find /usr/bin -name 'qemu-*' -not -name 'qemu-aarch64' -not -name 'qemu-x86_64' -not -name 'qemu-arm*' -not -name 'qemu-amd*' -print0 | xargs -0 ls -lah
echo 'Deleting: '
find /usr/bin -name 'qemu-*' -not -name 'qemu-aarch64' -not -name 'qemu-x86_64' -not -name 'qemu-arm*' -not -name 'qemu-amd*' -print0 | xargs -0 rm -fv
echo 'Remaining: '
ls -lah /usr/bin/qemu-*

QEMU_USER_FRAGMENT

# Extra kernel build tools
RUN apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends \
    make \
    flex \
    bison \
    bc \
    libelf-dev \
    libssl-dev \
    dpkg-dev \
    dwarves \
    pahole

# Those are pulled by build-essential (cross...), but are quite big; pull them ealier to balance layer size
RUN apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends \
    binutils-common \
    libasan8 \
    liblsan0 \
    libubsan1 \
    libhwasan0 \
    binutils-x86-64-linux-gnu \
    libasan8-amd64-cross \
    liblsan0-amd64-cross \
    libtsan2-amd64-cross \
    libc6-amd64-cross \
    linux-libc-dev-amd64-cross \
    libc6-dev-amd64-cross

RUN apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends \
    rsync \
    coreutils \
    git \
    curl \
    ca-certificates \
    qemu-user-static

# Then both, of which one will already be fulfilled
RUN apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends crossbuild-essential-arm64
RUN apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends crossbuild-essential-amd64

# Buildah and Skopeo
# Binary compression
# ISO image creation
# Kernel build deps: build-essential
RUN apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends \
    build-essential \
    containernetworking-plugins \
    bubblewrap \
    skopeo \
    upx-ucl \
    xorriso

# Buildah is pretty huge, gets its own layer.
RUN apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends buildah

# FYI: Rust stuff for kernel build is about 1.3gb
# RUN apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends rustc rust-src bindgen rustfmt rust-clippy

RUN <<-EXTRA_FRAG
# Extra packages for mkosi and kernel builds

# This is just to appease mkosi's later stages, as it re-launches itself using the system Python
apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends python3 python3-pip python3-pefile

# For kernel's bindeb-pkg and menuconfig
apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends debhelper libdw-dev lsb-release libncurses-dev

# For mkosi using Content.PackageDirectories (creating local repo)
apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends apt-utils

# For mkosi building RPM-based distros (Rocky Linux)
apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends dnf rpm distribution-gpg-keys

# For mkosi building disk images (Format=disk)
apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends systemd-repart
EXTRA_FRAG

RUN <<-CONFIG_FRAG
## A few small config fragments to make life easier
# git: Ignore owner mismatches in /work, which will be bind-mounted from the host
git config --global --add safe.directory /work \
# buildah: Configure rootless storage driver and chroot isolation (no user-namespace required — we only assemble scratch images, never RUN anything inside them).
printf '[storage]\ndriver = "vfs"\nrunroot = "/var/tmp/buildah-runroot"\ngraphroot = "/var/tmp/buildah-storage"\n' > /etc/containers/storage.conf
# Buildah 1.39+ on Debian requires netavark but we never need networking
# (all images are FROM scratch with no RUN steps).  A no-op stub satisfies
# the startup check.
mkdir -p /usr/libexec/podman
printf '#!/bin/sh\nexit 0\n' > /usr/libexec/podman/netavark
chmod +x /usr/libexec/podman/netavark
CONFIG_FRAG

# Install astral-sh's uv with a script - install to /usr for global access
RUN echo -n 'System Python: ' && python3 --version && curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="/usr/bin" sh && uv --version

# Install mkosi from GitHub (not on PyPI) via the system pip3
ARG MKOSI_VERSION=v26
RUN pip3 install --break-system-packages --root-user-action=ignore "git+https://github.com/systemd/mkosi.git@${MKOSI_VERSION}" && mkosi --version && command -v mkosi

# Prime uv's cache with our pyproject.toml to speed up runtime
COPY pyproject.toml /work/pyproject.toml
COPY captain /work/captain
COPY build.py /work/build.py
WORKDIR /work
RUN uv --verbose run captain --version
