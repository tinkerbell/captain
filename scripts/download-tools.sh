#!/bin/bash
# download-tools.sh — Download binary tools (nerdctl, containerd, etc.) for the initramfs.
#
# These are placed into mkosi.output/kernel/usr/local/bin/ so they are
# picked up by ExtraTrees alongside the kernel and modules.
#
# This script is idempotent — it only downloads if the binary is missing
# or FORCE_TOOLS=1 is set.
#
# Environment:
#   ARCH          Target architecture: amd64 (default) or arm64
#   FORCE_TOOLS   Set to 1 to re-download even if binaries exist

set -euo pipefail

ARCH="${ARCH:-amd64}"
WORK_DIR="/work"
TOOLS_DIR="${WORK_DIR}/mkosi.output/kernel/usr/local/bin"

case "$ARCH" in
    amd64|x86_64)  DL_ARCH="amd64" ;;
    arm64|aarch64) DL_ARCH="arm64" ;;
    *)             echo "ERROR: Unsupported ARCH=$ARCH"; exit 1 ;;
esac

mkdir -p "$TOOLS_DIR"

# --- Install containerd 2.x ---
CONTAINERD_VERSION="2.2.1"
CONTAINERD_DIR="${WORK_DIR}/mkosi.output/kernel/usr/local"
if [[ ! -x "$CONTAINERD_DIR/bin/containerd" ]] || [[ "${FORCE_TOOLS:-}" == "1" ]]; then
    echo "==> Installing containerd ${CONTAINERD_VERSION} (${DL_ARCH})..."
    mkdir -p "$CONTAINERD_DIR/bin"
    # Extract only needed binaries (exclude ctr and containerd-stress)
    curl -fsSL "https://github.com/containerd/containerd/releases/download/v${CONTAINERD_VERSION}/containerd-${CONTAINERD_VERSION}-linux-${DL_ARCH}.tar.gz" \
        | tar -xzf - -C "$CONTAINERD_DIR" \
            bin/containerd bin/containerd-shim-runc-v2
    # Remove unnecessary binaries if they exist from previous builds
    rm -f "$CONTAINERD_DIR/bin/ctr" "$CONTAINERD_DIR/bin/containerd-stress"
    echo "    containerd: $CONTAINERD_DIR/bin/containerd"
    echo "    containerd-shim-runc-v2: $CONTAINERD_DIR/bin/containerd-shim-runc-v2"
else
    echo "==> containerd already present (set FORCE_TOOLS=1 to re-download)"
fi

# --- Install runc ---
RUNC_VERSION="1.4.0"
RUNC_DIR="${WORK_DIR}/mkosi.output/kernel/usr/local/bin"
mkdir -p "$RUNC_DIR"
if [[ ! -x "$RUNC_DIR/runc" ]] || [[ "${FORCE_TOOLS:-}" == "1" ]]; then
    echo "==> Installing runc ${RUNC_VERSION} (${DL_ARCH})..."
    curl -fsSL "https://github.com/opencontainers/runc/releases/download/v${RUNC_VERSION}/runc.${DL_ARCH}" \
        -o "$RUNC_DIR/runc"
    chmod +x "$RUNC_DIR/runc"
    echo "    runc: $RUNC_DIR/runc"
else
    echo "==> runc already present (set FORCE_TOOLS=1 to re-download)"
fi

# --- Install nerdctl ---
NERDCTL_VERSION="2.2.1"
if [[ ! -x "$TOOLS_DIR/nerdctl" ]] || [[ "${FORCE_TOOLS:-}" == "1" ]]; then
    echo "==> Installing nerdctl ${NERDCTL_VERSION} (${DL_ARCH})..."
    curl -fsSL "https://github.com/containerd/nerdctl/releases/download/v${NERDCTL_VERSION}/nerdctl-${NERDCTL_VERSION}-linux-${DL_ARCH}.tar.gz" \
        | tar -xzf - -C "$TOOLS_DIR" nerdctl
    chmod +x "$TOOLS_DIR/nerdctl"
    # Remove Docker binaries if present from previous builds
    rm -f "$TOOLS_DIR/dockerd" "$TOOLS_DIR/docker" "$TOOLS_DIR/docker-init" "$TOOLS_DIR/docker-proxy"
    echo "    nerdctl: $TOOLS_DIR/nerdctl"
else
    echo "==> nerdctl already present (set FORCE_TOOLS=1 to re-download)"
fi

# --- Install CNI plugins (required for container networking) ---
CNI_VERSION="1.6.0"
CNI_DIR="${WORK_DIR}/mkosi.output/kernel/opt/cni/bin"

mkdir -p "$CNI_DIR"
if [[ ! -x "$CNI_DIR/bridge" ]] || [[ "${FORCE_TOOLS:-}" == "1" ]]; then
    echo "==> Installing CNI plugins ${CNI_VERSION} (${DL_ARCH})..."
    rm -rf "$CNI_DIR"
    mkdir -p "$CNI_DIR"
    CNI_TARBALL="/tmp/cni-plugins.tgz"
    curl -fsSL "https://github.com/containernetworking/plugins/releases/download/v${CNI_VERSION}/cni-plugins-linux-${DL_ARCH}-v${CNI_VERSION}.tgz" \
        -o "$CNI_TARBALL"
    # Extract only the core plugins needed for bridge networking:
    #   bridge, host-local (IPAM), loopback, portmap, firewall, tuning
    tar -xzf "$CNI_TARBALL" -C "$CNI_DIR" \
        ./bridge ./host-local ./loopback ./portmap ./firewall ./tuning
    chmod +x "$CNI_DIR"/*
    ls -1 "$CNI_DIR" | sed 's/^/    /'
    rm -f "$CNI_TARBALL"
else
    echo "==> CNI plugins already present (set FORCE_TOOLS=1 to re-download)"
fi

echo "==> Tool download complete."

# NOTE: UPX compression is not used. The final image is cpio.zst, and zstd
# compresses raw ELF binaries better than UPX-packed ones (UPX output looks
# like random data to zstd, defeating its compression).

echo "==> All tools ready."
