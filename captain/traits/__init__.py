"""Flavor traits — composable building blocks for flavors.

A flavor is composed of one trait per axis (core, distro, platform, role,
output), plus optional extras and the user's ``custom/`` layer. Each trait
contributes packages, hooks, artifacts and *content*: directories holding any
of ``mkosi.conf.j2``, ``mkosi.postinst.sh.j2``, ``mkosi.finalize.sh.j2``,
``mkosi.extra/``, ``mkosi.sandbox/``, ``mkosi.skeleton/`` and ``mkosi.repart/``.

Each content directory becomes one mkosi drop-in directory under
``mkosi.generated/mkosi.conf.d/``; mkosi merges those natively (list settings
append in order, later scalar settings win, extra/sandbox trees overlay in
order, every drop-in's scripts run in order).

Content is declared per class via the ``content`` class attribute, and is
collected along the MRO (base classes first), so a subclass adds its own
content on top of its parents' — e.g. ``Debian`` gets ``distro`` (generic
distribution settings) + ``distro/deb`` (apt bits) + its own, if any.
"""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from captain.artifacts import OutputArchArtifact
    from captain.flavor import Flavor

TRAITS_DIR = Path(__file__).parent


class Axis(IntEnum):
    """Axis of a trait; the value orders drop-ins and scripts."""

    CORE = 10
    DISTRO = 20
    PLATFORM = 30
    ROLE = 40
    OUTPUT = 50
    EXTRA = 60
    CUSTOM = 90


class Trait:
    axis: ClassVar[Axis]
    # Content dir of this very class, relative to TRAITS_DIR; inherited ones come via the MRO.
    content: ClassVar[str | None] = None
    # None means "any architecture".
    supported_architectures: ClassVar[frozenset[str] | None] = None

    @property
    def name(self) -> str:
        return type(self).__name__.lower()

    def custom_names(self) -> list[str]:
        """Dir names this trait matches under custom/by-<axis>/, general to specific."""
        return [self.name]

    def content_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        for klass in reversed(type(self).__mro__):
            content = klass.__dict__.get("content")
            if content is not None:
                dirs.append(TRAITS_DIR / content)
        return dirs

    def packages(self, flavor: Flavor) -> set[str]:
        """Logical package names (Debian names); the distro family maps them."""
        return set()

    def package_directories(self, flavor: Flavor) -> set[str]:
        """Directories with local packages, relative to the project dir."""
        return set()

    def keep_package_manager(self, flavor: Flavor) -> bool:
        """Keep a working package manager (and its metadata) in the image."""
        return False

    def keep_hwdb(self, flavor: Flavor) -> bool:
        return False

    def cache_bust_token(self, flavor: Flavor) -> str:
        """Token rendered into the generated config so the flavor hash changes on demand."""
        return ""

    def cache_inputs(self, flavor: Flavor) -> dict[str, str]:
        """Build-cache key inputs not visible in the rendered mkosi tree (eg. local packages)."""
        return {}

    def pre_mkosi(self, flavor: Flavor) -> None:
        pass

    def post_mkosi(self, flavor: Flavor) -> None:
        pass

    def post_artifact_collect(self, flavor: Flavor) -> None:
        pass

    def add_arch_artifacts(
        self, flavor: Flavor, output_arch: str, artifacts: list[OutputArchArtifact]
    ) -> None:
        pass
