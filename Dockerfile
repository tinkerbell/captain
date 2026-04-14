# Builder container for CaptainOS using mkosi
# Encapsulates all mkosi dependencies for reproducible builds.
# Usage: docker build -t captainos-builder . && docker run --rm --privileged -v $(pwd):/work captainos-builder build
FROM debian:trixie

ARG MKOSI_VERSION=v26

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install mkosi runtime dependencies and kernel build dependencies in one layer
RUN apt-get -o "Dpkg::Use-Pty=0" update && apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends \
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
    bubblewrap \
    squashfs-tools \
    mtools \
    erofs-utils \
    dosfstools \
    e2fsprogs \
    btrfs-progs \
    # Kernel build deps
    build-essential \
    gcc \
    gcc-aarch64-linux-gnu \
    make \
    flex \
    bison \
    bc \
    libelf-dev \
    libssl-dev \
    dwarves \
    pahole \
    rsync \
    coreutils \
    # Cross-architecture support (arm64 on x86_64 and vice versa)
    qemu-user-static \
    # Network tools (for fetching kernel source etc.)
    git \
    curl \
    ca-certificates \
    # Binary compression
    upx-ucl \
    # ISO image creation
    xorriso \
    grub-common \
    && NATIVE_ARCH="$(dpkg --print-architecture)" \
    && FOREIGN_ARCH=$([ "$NATIVE_ARCH" = "amd64" ] && echo "arm64" || echo "amd64") \
    && apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends "grub-efi-${NATIVE_ARCH}-bin" \
    && dpkg --add-architecture "$FOREIGN_ARCH" \
    && apt-get -o "Dpkg::Use-Pty=0" update \
    && apt-get -o "Dpkg::Use-Pty=0" install -y --no-install-recommends "grub-efi-${FOREIGN_ARCH}-bin:${FOREIGN_ARCH}" \
    && rm -rf /var/lib/apt/lists/*

# Install astral-sh's uv with a script - install to /usr for global access
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="/usr/bin" sh

# Verify uv is functional
RUN uv --version

# Install mkosi from GitHub (not on PyPI) via uv; symlink to /usr/bin for global access
RUN uv tool install "git+https://github.com/systemd/mkosi.git@${MKOSI_VERSION}"
RUN ln -sf ~/.local/bin/mkosi /usr/bin/mkosi

# Verify mkosi is functional
RUN mkosi --version

# Prime uv's cache with our pyproject.toml to speed up runtime
COPY pyproject.toml /tmp/pyproject.toml
COPY captain /tmp/captain
COPY build.py /tmp/build.py
WORKDIR /tmp
RUN uv --verbose run build.py --help

WORKDIR /work
ENTRYPOINT ["mkosi"]
CMD ["build"]
