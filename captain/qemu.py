"""QEMU boot testing."""

from __future__ import annotations

import argparse
import logging
import sys

from captain.config import Config
from captain.util import run

log = logging.getLogger(__name__)

# Tinkerbell kernel cmdline parameters.
# Maps the argparse dest name → kernel cmdline key.
# Insertion order is preserved.
_TINK_PARAMS: list[tuple[str, str]] = [
    # (namespace_attr,              cmdline_key)
    ("tink_worker_image", "tink_worker_image"),
    ("tink_docker_registry", "docker_registry"),
    ("tink_grpc_authority", "grpc_authority"),
    ("tink_worker_id", "worker_id"),
    ("tink_tls", "tinkerbell_tls"),
    ("tink_insecure_tls", "tinkerbell_insecure_tls"),
    ("tink_insecure_registries", "insecure_registries"),
    ("tink_registry_username", "registry_username"),
    ("tink_registry_password", "registry_password"),
    ("tink_syslog_host", "syslog_host"),
    ("tink_facility", "facility"),
]


def _tink_cmdline(args: argparse.Namespace) -> str:
    """Build tinkerbell kernel cmdline fragment from parsed *args*."""
    parts: list[str] = []
    for attr, cmdline_key in _TINK_PARAMS:
        value = getattr(args, attr, "") or ""
        if not value:
            continue
        # Kernel cmdline is space-delimited; whitespace in values would
        # split them into multiple arguments and silently change meaning.
        if any(ch.isspace() for ch in value):
            log.error(
                "--%s must not contain whitespace; cannot safely add it to the kernel cmdline.",
                attr.replace("_", "-"),
            )
            sys.exit(1)
        parts.append(f"{cmdline_key}={value}")

    # Static networking via ipam= parameter
    ipam = getattr(args, "ipam", "") or ""
    if ipam:
        if any(ch.isspace() for ch in ipam):
            log.error("--ipam must not contain whitespace.")
            sys.exit(1)
        parts.append(f"ipam={ipam}")

    return " ".join(parts)


def run_qemu(cfg: Config, args: argparse.Namespace | None = None) -> None:
    """Boot the built image in QEMU for quick testing.

    *args* is the parsed :class:`argparse.Namespace` produced by
    :mod:`configargparse`.  When provided, Tinkerbell kernel cmdline
    parameters are drawn from it instead of the environment.
    """
    kernel = cfg.output_dir / f"vmlinuz-{cfg.kernel_version}-{cfg.arch_info.output_arch}"
    initrd = cfg.output_dir / f"initramfs-{cfg.kernel_version}-{cfg.arch_info.output_arch}"

    missing: list[str] = []
    if not kernel.is_file():
        missing.append(str(kernel))
    if not initrd.is_file():
        missing.append(str(initrd))
    if missing:
        log.error("Build artifacts not found:")
        for m in missing:
            log.error("  %s", m)
        log.error("Run './build.py --kernel-version %s' first.", cfg.kernel_version)
        sys.exit(1)

    tink = _tink_cmdline(args) if args is not None else ""
    if args is not None and not any(
        getattr(args, v, None) for v in ("tink_worker_image", "tink_docker_registry")
    ):
        log.warning(
            "Neither --tink-worker-image nor --tink-docker-registry is set. "
            "tink-agent services will not start."
        )

    log.info("Booting CaptainOS in QEMU (Ctrl-A X to exit)...")

    qemu_cmd = cfg.arch_info.qemu_binary
    append = f"console=ttyS0 audit=0 {tink} {cfg.qemu_append}".strip()

    log.info("Kernel cmdline: %s", append)
    run(
        [
            qemu_cmd,
            "-kernel",
            str(kernel),
            "-initrd",
            str(initrd),
            "-append",
            append,
            "-nographic",
            "-m",
            cfg.qemu_mem,
            "-smp",
            cfg.qemu_smp,
            "-nic",
            "user,model=virtio-net-pci",
            "-no-reboot",
        ],
    )
