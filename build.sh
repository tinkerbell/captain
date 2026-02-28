#!/usr/bin/env bash
# build.sh — Build CaptainOS images using mkosi inside Docker.
#
# Usage:
#   ./build.sh              # Build the image
#   ./build.sh build        # Same as above
#   ./build.sh shell        # Drop into an interactive shell inside the builder
#   ./build.sh clean        # Remove build artifacts
#   ./build.sh summary      # Print mkosi configuration summary
#   ./build.sh qemu-test    # Boot the built image in QEMU for testing
#
# Environment variables:
#   ARCH          Target architecture: amd64 (default) or arm64
#   KERNEL_SRC    Path to a local kernel source tree (optional, avoids git clone)
#   NO_CACHE      Set to 1 to force Docker image rebuild without cache
#   BUILDER_IMAGE Override the builder Docker image name (default: captainos-builder)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Configuration
BUILDER_IMAGE="${BUILDER_IMAGE:-captainos-builder}"
ARCH="${ARCH:-amd64}"
KERNEL_VERSION="${KERNEL_VERSION:-6.12.69}"
OUTPUT_DIR="out"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log()  { echo -e "${GREEN}[captainos]${NC} $*"; }
warn() { echo -e "${YELLOW}[captainos]${NC} $*"; }
err()  { echo -e "${RED}[captainos]${NC} $*" >&2; }

# Build the Docker builder image if it doesn't exist or is outdated
build_builder() {
    local cache_flag=""
    if [[ "${NO_CACHE:-}" == "1" ]]; then
        cache_flag="--no-cache"
    fi

    # Rebuild if Dockerfile is newer than the image
    local needs_build=0
    if ! docker image inspect "$BUILDER_IMAGE" &>/dev/null; then
        needs_build=1
    else
        local image_created
        image_created=$(docker image inspect "$BUILDER_IMAGE" --format '{{.Created}}' 2>/dev/null)
        local dockerfile_modified
        dockerfile_modified=$(stat -c '%Y' Dockerfile 2>/dev/null || echo 0)
        local image_epoch
        image_epoch=$(date -d "$image_created" '+%s' 2>/dev/null || echo 0)
        if [[ "$dockerfile_modified" -gt "$image_epoch" ]]; then
            needs_build=1
        fi
    fi

    if [[ "$needs_build" == "1" ]] || [[ "${NO_CACHE:-}" == "1" ]]; then
        log "Building Docker image '$BUILDER_IMAGE'..."
        docker build $cache_flag -t "$BUILDER_IMAGE" .
    else
        log "Docker image '$BUILDER_IMAGE' is up to date."
    fi
}

# Run a command inside the Docker builder container
run_in_builder() {
    local -a docker_args=(
        --rm
        --privileged
        -v "$SCRIPT_DIR:/work"
        -w /work
        -e "ARCH=$ARCH"
        -e "KERNEL_VERSION=${KERNEL_VERSION}"
        -e "FORCE_TOOLS=${FORCE_TOOLS:-}"
        -e "FORCE_KERNEL=${FORCE_KERNEL:-}"
    )

    # Mount kernel source if provided
    if [[ -n "${KERNEL_SRC:-}" ]]; then
        if [[ ! -d "$KERNEL_SRC" ]]; then
            err "KERNEL_SRC=$KERNEL_SRC does not exist"
            exit 1
        fi
        docker_args+=(-v "$(realpath "$KERNEL_SRC"):/work/kernel-src:ro")
        docker_args+=(-e "KERNEL_SRC=/work/kernel-src")
    fi

    docker run "${docker_args[@]}" "$@"
}

# Ensure binfmt_misc handlers are registered for cross-architecture builds.
# The container runs --privileged so it can access the host's binfmt_misc.
ensure_binfmt() {
    local host_arch
    host_arch=$(uname -m)
    local need_binfmt=0

    case "${host_arch}:${ARCH}" in
        x86_64:arm64|x86_64:aarch64) need_binfmt=1 ;;
        aarch64:amd64|aarch64:x86_64) need_binfmt=1 ;;
    esac

    if [[ "$need_binfmt" == "1" ]]; then
        log "Registering binfmt_misc handlers for cross-architecture build (${host_arch} -> ${ARCH})..."
        if ! docker run --rm --privileged tonistiigi/binfmt --install all >/dev/null 2>&1; then
            warn "Could not auto-register binfmt handlers."
            warn "Run manually: docker run --privileged --rm tonistiigi/binfmt --install all"
        fi
    fi
}

# Map ARCH to mkosi architecture names
mkosi_arch() {
    case "$1" in
        amd64|x86_64)  echo "x86-64" ;;
        arm64|aarch64) echo "arm64" ;;
        *)             echo "$1" ;;
    esac
}

# Run mkosi inside Docker
run_mkosi() {
    ensure_binfmt
    run_in_builder "$BUILDER_IMAGE" --architecture="$(mkosi_arch "$ARCH")" "$@"
}

# Build the kernel (separate step, no mkosi overlay needed)
build_kernel() {
    if [[ -d "mkosi.output/kernel/usr/lib/modules" ]] && [[ "${FORCE_KERNEL:-}" != "1" ]]; then
        log "Kernel already built (set FORCE_KERNEL=1 to rebuild)"
        return 0
    fi
    log "Step 1/4: Building kernel..."
    run_in_builder --entrypoint bash "$BUILDER_IMAGE" /work/scripts/build-kernel.sh
}

# Download binary tools (nerdctl) — always runs, idempotent
download_tools() {
    log "Step 2/3: Downloading tools (nerdctl, remote_syslog)..."
    run_in_builder --entrypoint bash "$BUILDER_IMAGE" /work/scripts/download-tools.sh
}

# Collect output artifacts into out/
collect_artifacts() {
    log "Collecting build artifacts..."
    mkdir -p "$OUTPUT_DIR"

    # The initrd CPIO output (flat, single-image layout)
    local initrd_src
    initrd_src=$(find mkosi.output/ -maxdepth 1 -name '*.cpio*' 2>/dev/null | head -1)
    if [[ -n "$initrd_src" ]]; then
        cp "$initrd_src" "$OUTPUT_DIR/initramfs-${ARCH}.cpio.zst"
        log "initramfs: $OUTPUT_DIR/initramfs-${ARCH}.cpio.zst ($(du -h "$OUTPUT_DIR/initramfs-${ARCH}.cpio.zst" | cut -f1))"
    else
        warn "No initramfs CPIO found in mkosi.output/"
    fi

    # The kernel image (built by scripts/build-kernel.sh)
    local vmlinuz
    vmlinuz=$(find mkosi.output/kernel/boot/ -name 'vmlinuz-*' 2>/dev/null | head -1)
    if [[ -n "$vmlinuz" ]]; then
        cp "$vmlinuz" "$OUTPUT_DIR/vmlinuz-${ARCH}"
        log "kernel: $OUTPUT_DIR/vmlinuz-${ARCH} ($(du -h "$OUTPUT_DIR/vmlinuz-${ARCH}" | cut -f1))"
    else
        warn "No kernel image found in mkosi.output/kernel/boot/"
    fi

    # Print checksums
    if ls "$OUTPUT_DIR"/* &>/dev/null; then
        log "Checksums:"
        sha256sum "$OUTPUT_DIR"/* 2>/dev/null | sed 's/^/  /'
    fi
}

# Clean all build artifacts (Docker creates root-owned files)
do_clean() {
    log "Cleaning build artifacts..."
    # Use Docker to remove root-owned files from mkosi
    if [[ -d mkosi.output || -d mkosi.cache ]]; then
        docker run --rm -v "$SCRIPT_DIR:/work" -w /work debian:trixie \
            sh -c 'rm -rf /work/mkosi.output/image* /work/mkosi.output/image.vmlinuz /work/mkosi.cache'
    fi
    rm -rf "$OUTPUT_DIR/"
    log "Clean complete."
}

# Quick QEMU boot test
do_qemu_test() {
    local kernel="$OUTPUT_DIR/vmlinuz-${ARCH}"
    local initrd="$OUTPUT_DIR/initramfs-${ARCH}.cpio.zst"

    if [[ ! -f "$kernel" ]] || [[ ! -f "$initrd" ]]; then
        err "Build artifacts not found. Run './build.sh' first."
        exit 1
    fi

    log "Booting CaptainOS in QEMU (Ctrl-A X to exit)..."
    local qemu_cmd="qemu-system-x86_64"
    if [[ "$ARCH" == "arm64" ]]; then
        qemu_cmd="qemu-system-aarch64"
    fi

    local append="console=ttyS0 audit=0 ${QEMU_APPEND:-}"

    log "Kernel cmdline: $append"
    $qemu_cmd \
        -kernel "$kernel" \
        -initrd "$initrd" \
        -append "$append" \
        -nographic \
        -m "${QEMU_MEM:-2G}" \
        -smp "${QEMU_SMP:-2}" \
        -nic user,model=virtio-net-pci \
        -no-reboot
}

# Print usage
usage() {
    cat <<EOF
Usage: $(basename "$0") [command] [mkosi args...]

Commands:
  build       Build the CaptainOS image (default)
  shell       Interactive shell inside the builder container
  clean       Remove all build artifacts
  summary     Print mkosi configuration summary
  qemu-test   Boot the image in QEMU for testing
  help        Show this help message

Environment:
  ARCH            Target architecture: amd64 (default) or arm64
  KERNEL_SRC      Path to local kernel source tree
  KERNEL_VERSION  Kernel version to build (default: 6.12.69, set at top of build.sh)
  FORCE_KERNEL    Set to 1 to force kernel rebuild
  NO_CACHE        Set to 1 to rebuild builder image without cache
  BUILDER_IMAGE   Override builder Docker image name
  QEMU_APPEND    Extra kernel cmdline args for qemu-test
  QEMU_MEM       QEMU RAM size (default: 2G)
  QEMU_SMP       QEMU CPU count (default: 2)

Examples:
  ./build.sh                     # Build with defaults
  ARCH=arm64 ./build.sh          # Build for ARM64
  KERNEL_SRC=~/linux ./build.sh  # Use local kernel source
  ./build.sh shell               # Debug inside builder
  ./build.sh qemu-test           # Boot test with QEMU
  QEMU_APPEND='tink_worker_image=reg.local/tink-agent:latest' ./build.sh qemu-test
EOF
}

# Parse global flags
MKOSI_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --force) MKOSI_ARGS+=(--force) ;;
        --force-kernel) FORCE_KERNEL=1 ;;
    esac
done

# Main
case "${1:-build}" in
    build|--force)
        shift || true
        build_builder
        build_kernel
        download_tools
        log "Step 3/3: Building initrd with mkosi..."
        run_mkosi build "${MKOSI_ARGS[@]}"
        collect_artifacts
        log "Build complete!"
        ;;
    shell)
        build_builder
        log "Entering builder shell (type 'exit' to leave)..."
        run_in_builder -it --entrypoint /bin/bash "$BUILDER_IMAGE"
        ;;
    clean)
        do_clean
        ;;
    summary)
        build_builder
        run_mkosi summary
        ;;
    qemu-test)
        do_qemu_test
        ;;
    help|--help|-h)
        usage
        ;;
    *)
        # Pass through to mkosi
        build_builder
        run_mkosi "$@"
        ;;
esac
