"""QEMU boot testing."""

from __future__ import annotations

import os
import sys

from captain.config import Config
from captain.log import for_stage
from captain.util import run

_log = for_stage("qemu")

# Tinkerbell kernel cmdline parameters read from environment variables.
# With QEMU user-mode networking the host is reachable at 10.0.2.2, so
# defaults assume a local Tinkerbell stack on the host.
#
#   TINK_GRPC_AUTHORITY     tink-server gRPC endpoint (host:port)
#   TINK_DOCKER_REGISTRY    Registry host (also triggers tink-agent services)
#   TINK_WORKER_IMAGE       Full image ref (overrides TINK_DOCKER_REGISTRY)
#   TINK_WORKER_ID          Machine / worker ID (auto-detected when empty)
#   TINK_TLS                Enable TLS to tink-server        (default: false)
#   TINK_INSECURE_TLS       Allow insecure TLS               (default: true)
#   TINK_INSECURE_REGISTRIES Comma-separated insecure registries
#   TINK_REGISTRY_USERNAME  Registry auth username
#   TINK_REGISTRY_PASSWORD  Registry auth password
#   TINK_SYSLOG_HOST        Remote syslog host
#   TINK_FACILITY           Facility code

# Maps env-var name → kernel cmdline key.  Insertion order is preserved.
_TINK_PARAMS: list[tuple[str, str, str]] = [
    # (env_var,                  cmdline_key,              default)
    ("TINK_WORKER_IMAGE",        "tink_worker_image",      "ghcr.io/tinkerbell/tink-agent:latest"),
    ("TINK_DOCKER_REGISTRY",     "docker_registry",        ""),
    ("TINK_GRPC_AUTHORITY",      "grpc_authority",         ""),
    ("TINK_WORKER_ID",           "worker_id",              ""),
    ("TINK_TLS",                 "tinkerbell_tls",         "false"),
    ("TINK_INSECURE_TLS",        "tinkerbell_insecure_tls", "true"),
    ("TINK_INSECURE_REGISTRIES", "insecure_registries",    ""),
    ("TINK_REGISTRY_USERNAME",   "registry_username",      ""),
    ("TINK_REGISTRY_PASSWORD",   "registry_password",      ""),
    ("TINK_SYSLOG_HOST",         "syslog_host",            ""),
    ("TINK_FACILITY",            "facility",               ""),
]


def _tink_cmdline() -> str:
    """Build tinkerbell kernel cmdline fragment from environment variables."""
    parts: list[str] = []
    for env_var, cmdline_key, default in _TINK_PARAMS:
        value = os.environ.get(env_var, default)
        if not value:
            continue
        # Kernel cmdline is space-delimited; whitespace in values would
        # split them into multiple arguments and silently change meaning.
        if any(ch.isspace() for ch in value):
            _log.err(
                f"Environment variable {env_var} must not contain whitespace; "
                "cannot safely add it to the kernel cmdline."
            )
            sys.exit(1)
        parts.append(f"{cmdline_key}={value}")

    # Static networking via ipam= parameter
    ipam = os.environ.get("IPAM", "")
    if ipam:
        if any(ch.isspace() for ch in ipam):
            _log.err("IPAM must not contain whitespace.")
            sys.exit(1)
        parts.append(f"ipam={ipam}")

    return " ".join(parts)


def run_qemu(cfg: Config) -> None:
    """Boot the built image in QEMU for quick testing."""
    kernel = cfg.output_dir / f"vmlinuz-{cfg.arch}"
    initrd = cfg.output_dir / f"initramfs-{cfg.arch}.cpio.zst"

    if not kernel.is_file() or not initrd.is_file():
        _log.err("Build artifacts not found. Run './build.py' first.")
        sys.exit(1)

    tink = _tink_cmdline()
    if not any(
        os.environ.get(v) for v in ("TINK_WORKER_IMAGE", "TINK_DOCKER_REGISTRY")
    ):
        _log.warn(
            "Neither TINK_WORKER_IMAGE nor TINK_DOCKER_REGISTRY is set. "
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
