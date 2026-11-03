"""User customization layers: custom/ for every flavor, plus custom/by-<axis>/<value>/.

Each layer has the shape of custom/ (files/, packages.txt, mkosi-postinst/,
mkosi-finalize/). Layers apply general to specific, in AXES order; see
"Customization via custom/" in README.md.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from captain.flavor import Flavor

log = logging.getLogger(__name__)

PREFIX = "by-"

# General to specific: a later layer's same-named file or script replaces an earlier one's.
AXES: dict[str, Callable[[Flavor, str], list[str]]] = {
    "family": lambda f, arch: [f.distro.family],
    "distro": lambda f, arch: f.distro.custom_names(),
    "arch": lambda f, arch: [arch],
    "role": lambda f, arch: f.role.custom_names(),
    "output": lambda f, arch: f.output.custom_names(),
    "platform": lambda f, arch: f.platform.custom_names(),
    "kernel": lambda f, arch: f.platform.kernel_names(),
    "flavor": lambda f, arch: [f.id],
}

_warned: set[Path] = set()


def axis_dirs(custom_dir: Path) -> list[Path]:
    """The by-<axis>/ dirs present in *custom_dir*."""
    return sorted(p for p in custom_dir.glob(f"{PREFIX}*") if p.is_dir())


def _known_values(axis: str) -> set[str]:
    from captain.flavors import FLAVORS

    return {
        value
        for flavor in FLAVORS
        for arch in flavor.supported_architectures
        for value in AXES[axis](flavor, arch)
    }


def _warn_unknown(custom_dir: Path) -> None:
    for axis_dir in axis_dirs(custom_dir):
        if axis_dir in _warned:
            continue
        _warned.add(axis_dir)
        axis = axis_dir.name.removeprefix(PREFIX)
        if axis not in AXES:
            log.warning(
                "Custom: ignoring custom/%s: unknown axis (one of: %s)",
                axis_dir.name,
                ", ".join(AXES),
            )
            continue
        known = _known_values(axis)
        for value_dir in sorted(p for p in axis_dir.iterdir() if p.is_dir()):
            if value_dir.name not in known:
                log.warning(
                    "Custom: custom/%s/%s matches no flavor (known: %s)",
                    axis_dir.name,
                    value_dir.name,
                    ", ".join(sorted(known)),
                )


def layers(flavor: Flavor) -> list[Path]:
    """The customization dirs applying to *flavor*, general to specific."""
    cfg = flavor.cfg
    _warn_unknown(cfg.custom_dir)
    found = [cfg.custom_dir]
    for axis, values in AXES.items():
        for value in values(flavor, cfg.arch_info.arch):
            layer = cfg.custom_dir / f"{PREFIX}{axis}" / value
            if layer.is_dir():
                found.append(layer)
    if len(found) > 1:
        log.info("Custom: %s", ", ".join(str(p.relative_to(cfg.project_dir)) for p in found))
    return found
