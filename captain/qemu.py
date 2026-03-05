"""QEMU boot testing."""

from __future__ import annotations

import argparse
import sys

from captain.config import Config
from captain.log import for_stage
from captain.util import run

_log = for_stage("qemu")

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
            _log.err(
                f"--{attr.replace('_', '-')} must not contain whitespace; "
                "cannot safely add it to the kernel cmdline."
            )
            sys.exit(1)
        parts.append(f"{cmdline_key}={value}")

    # Static networking via ipam= parameter
    ipam = getattr(args, "ipam", "") or ""
    if ipam:
        if any(ch.isspace() for ch in ipam):
            _log.err("--ipam must not contain whitespace.")
            sys.exit(1)
        parts.append(f"ipam={ipam}")

    return " ".join(parts)


def run_qemu(cfg: Config, args: argparse.Namespace | None = None) -> None:
    """Boot the built image in QEMU for quick testing.

    *args* is the parsed :class:`argparse.Namespace` produced by
    :mod:`configargparse`.  When provided, Tinkerbell kernel cmdline
    parameters are drawn from it instead of the environment.
    """
    kernel = cfg.output_dir / f"vmlinuz-{cfg.arch}"
    initrd = cfg.output_dir / f"initramfs-{cfg.arch}.cpio.zst"

    if not kernel.is_file() or not initrd.is_file():
        _log.err("Build artifacts not found. Run './build.py' first.")
        sys.exit(1)

    tink = _tink_cmdline(args) if args is not None else ""
    if args is not None and not any(
        getattr(args, v, None) for v in ("tink_worker_image", "tink_docker_registry")
    ):
        _log.warn(
            "Neither --tink-worker-image nor --tink-docker-registry is set. "
            "tink-agent services will not start."
        )

    _log.log("Booting CaptainOS in QEMU (Ctrl-A X to exit)...")

    qemu_cmd = cfg.arch_info.qemu_binary
    append = f"console=ttyS0 audit=0 {tink} {cfg.qemu_append}".strip()

    _log.log(f"Kernel cmdline: {append}")
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
