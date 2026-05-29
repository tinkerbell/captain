# Builder container for CaptainOS using mkosi
# Encapsulates all mkosi dependencies for reproducible builds.
# Usage: docker build -t captainos-builder . && docker run --rm --privileged -v $(pwd):/work captainos-builder build
FROM debian:trixie

# Pinned post-v26 to pick up systemd/mkosi@1f811f05 ("tools: move grub-pc-bin
# to arch-specific drop-in"), which fixes arm64 builds failing on the default
# tools-tree pulling in grub-pc-bin (BIOS GRUB, x86-only). Bump to a release
# tag once v27 lands.
ARG MKOSI_VERSION=1f811f0524be3096872e79161c8e6ab3e7c2bb1f

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

# Install project dependencies into a persistent venv so that
# `uv run` inside the container reuses it instead of recreating one.
COPY pyproject.toml /opt/captain/pyproject.toml
COPY captain /opt/captain/captain
COPY build.py /opt/captain/build.py
RUN uv venv /opt/captain-venv && \
    VIRTUAL_ENV=/opt/captain-venv uv pip install --project /opt/captain /opt/captain

# Point uv at the pre-built venv for all future runs.
ENV VIRTUAL_ENV=/opt/captain-venv
ENV UV_PROJECT_ENVIRONMENT=/opt/captain-venv

WORKDIR /work
ENTRYPOINT ["mkosi"]
CMD ["build"]
