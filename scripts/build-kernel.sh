#!/bin/bash
# build-kernel.sh — Compile the Linux kernel from source.
#
# This script runs inside the builder Docker container, outside of mkosi.
# The built kernel and modules are placed in mkosi.output/kernel/ so that
# mkosi can pick them up via ExtraTrees.
#
# Environment:
#   KERNEL_SRC       Optional path to pre-existing kernel source tree
#   KERNEL_VERSION   Kernel version to download (set by build.sh)
#   ARCH             Target architecture: amd64 (default) or arm64
set -euo pipefail

if [[ -z "${KERNEL_VERSION:-}" ]]; then
    echo "ERROR: KERNEL_VERSION must be set (normally passed from build.sh)"
    exit 1
fi
KVER="$KERNEL_VERSION"
ARCH="${ARCH:-amd64}"

# Map ARCH to kernel ARCH
case "$ARCH" in
    amd64|x86_64)  KARCH="x86_64"; CROSS_COMPILE="" ;;
    arm64|aarch64) KARCH="arm64";  CROSS_COMPILE="aarch64-linux-gnu-" ;;
    *)             echo "ERROR: Unsupported ARCH=$ARCH"; exit 1 ;;
esac

WORK_DIR="/work"
KERNEL_BUILD_DIR="/var/tmp/kernel-build"
KERNEL_OUTPUT="${WORK_DIR}/mkosi.output/kernel"

# Clean previous output to ensure idempotency
rm -rf "$KERNEL_OUTPUT"
mkdir -p "$KERNEL_BUILD_DIR" "$KERNEL_OUTPUT"

# --- Obtain kernel source ---
if [[ -n "${KERNEL_SRC:-}" ]] && [[ -d "$KERNEL_SRC" ]]; then
    echo "==> Using provided kernel source at $KERNEL_SRC"
    KSRC="$KERNEL_SRC"
else
    KSRC="$KERNEL_BUILD_DIR/linux-${KVER}"
    if [[ ! -d "$KSRC" ]]; then
        echo "==> Downloading kernel ${KVER}..."
        MAJOR_VER="${KVER%%.*}"
        TARBALL="linux-${KVER}.tar.xz"
        curl -fsSL "https://cdn.kernel.org/pub/linux/kernel/v${MAJOR_VER}.x/${TARBALL}" \
            -o "$KERNEL_BUILD_DIR/${TARBALL}"
        echo "==> Extracting kernel source..."
        tar -xf "$KERNEL_BUILD_DIR/${TARBALL}" -C "$KERNEL_BUILD_DIR"
        rm -f "$KERNEL_BUILD_DIR/${TARBALL}"
    else
        echo "==> Using cached kernel source at $KSRC"
    fi
fi

cd "$KSRC"

# --- Apply defconfig ---
DEFCONFIG="${WORK_DIR}/config/defconfig.${ARCH}"
if [[ -f "$DEFCONFIG" ]]; then
    echo "==> Using defconfig: $DEFCONFIG"
    cp "$DEFCONFIG" .config
    make ARCH="$KARCH" ${CROSS_COMPILE:+CROSS_COMPILE=$CROSS_COMPILE} olddefconfig
    # Save the resolved config for debugging (olddefconfig may alter our defconfig)
    cp .config "${WORK_DIR}/config/.config.resolved.${ARCH}"
    echo "==> Resolved config saved to config/.config.resolved.${ARCH}"
else
    echo "==> No defconfig found at $DEFCONFIG, using default"
    make ARCH="$KARCH" ${CROSS_COMPILE:+CROSS_COMPILE=$CROSS_COMPILE} defconfig
fi

# --- Increase kernel command line max size (default 2048 on x86) ---
# Tinkerbell workflows pass many parameters via cmdline; 4096 gives headroom.
if [[ "$KARCH" == "x86_64" ]]; then
    echo "==> Increasing COMMAND_LINE_SIZE to 4096 (x86_64)..."
    sed -i 's/#define COMMAND_LINE_SIZE[[:space:]]*2048/#define COMMAND_LINE_SIZE 4096/' \
        arch/x86/include/asm/setup.h
fi

# --- Build kernel ---
NPROC=$(nproc)
echo "==> Building kernel with ${NPROC} jobs..."
make ARCH="$KARCH" ${CROSS_COMPILE:+CROSS_COMPILE=$CROSS_COMPILE} \
    -j"$NPROC" \
    bzImage modules

# --- Determine actual kernel version from build ---
BUILT_KVER=$(make -s ARCH="$KARCH" kernelrelease)
echo "==> Built kernel version: $BUILT_KVER"

# --- Install modules ---
echo "==> Installing modules..."
make ARCH="$KARCH" ${CROSS_COMPILE:+CROSS_COMPILE=$CROSS_COMPILE} \
    INSTALL_MOD_PATH="$KERNEL_OUTPUT" \
    modules_install

# Strip debug symbols from modules to reduce size
echo "==> Stripping debug symbols from modules..."
find "$KERNEL_OUTPUT" -name '*.ko' -exec strip --strip-unneeded {} \;

# Clean up the build/source symlinks in the modules directory
rm -f "$KERNEL_OUTPUT/lib/modules/$BUILT_KVER/build"
rm -f "$KERNEL_OUTPUT/lib/modules/$BUILT_KVER/source"

# --- Install kernel image ---
# mkosi expects the kernel at /usr/lib/modules/<version>/vmlinuz
MODDIR="$KERNEL_OUTPUT/usr/lib/modules/$BUILT_KVER"
mkdir -p "$MODDIR"

# Move modules from /lib/modules to /usr/lib/modules (merged-usr)
if [[ -d "$KERNEL_OUTPUT/lib/modules/$BUILT_KVER" ]]; then
    cp -a "$KERNEL_OUTPUT/lib/modules/$BUILT_KVER"/* "$MODDIR/"
    rm -rf "$KERNEL_OUTPUT/lib"
fi

# Copy kernel image
case "$KARCH" in
    x86_64)  cp arch/x86/boot/bzImage "$MODDIR/vmlinuz" ;;
    arm64)   cp arch/arm64/boot/Image "$MODDIR/vmlinuz" ;;
esac

# Also place a copy at a well-known location for easy extraction
mkdir -p "$KERNEL_OUTPUT/boot"
cp "$MODDIR/vmlinuz" "$KERNEL_OUTPUT/boot/vmlinuz-${BUILT_KVER}"

echo ""
echo "==> Kernel build complete:"
echo "    Image:   $MODDIR/vmlinuz ($(du -h "$MODDIR/vmlinuz" | cut -f1))"
echo "    Modules: $MODDIR/ ($(du -sh "$MODDIR" | cut -f1) total)"
echo "    Version: $BUILT_KVER"
echo "    Output:  $KERNEL_OUTPUT"

# Note: nerdctl are downloaded separately by scripts/download-tools.sh
# to allow independent updates without requiring a full kernel rebuild.
