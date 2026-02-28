# Builder container for CaptainOS using mkosi
# Encapsulates all mkosi dependencies for reproducible builds.
# Usage: docker build -t captainos-builder . && docker run --rm --privileged -v $(pwd):/work captainos-builder build
FROM debian:trixie

ARG MKOSI_VERSION=v26

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install mkosi runtime dependencies and kernel build dependencies in one layer
RUN apt-get update && apt-get install -y --no-install-recommends \
    # mkosi runtime deps
    python3 \
    python3-pip \
    python3-venv \
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
    && rm -rf /var/lib/apt/lists/*

# Install mkosi from GitHub (not on PyPI)
RUN pip3 install --break-system-packages \
    "git+https://github.com/systemd/mkosi.git@${MKOSI_VERSION}"

# Verify mkosi is functional
RUN mkosi --version

WORKDIR /work
ENTRYPOINT ["mkosi"]
CMD ["build"]
