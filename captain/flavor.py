"""Flavors: a composition of traits, one per axis, rendered into an mkosi config tree."""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import jinja2
from rich.table import Table

import captain
import captain.custom
from captain.artifacts import OutputArchArtifact
from captain.config import Config
from captain.traits import TRAITS_DIR, Axis, Trait
from captain.traits.distro import Distro
from captain.traits.output import Output
from captain.traits.platform import Platform
from captain.traits.role import Role

log = logging.getLogger(__name__)

# Static dirs copied from each content dir into its drop-in; "*.j2" files in them are rendered.
STATIC_DIRS = ("mkosi.extra", "mkosi.sandbox", "mkosi.skeleton", "mkosi.repart")
# Script templates in each content dir; rendered with the bash header prepended.
SCRIPTS = {"mkosi.postinst.sh.j2": "mkosi.postinst", "mkosi.finalize.sh.j2": "mkosi.finalize"}
BASH_HEADER = TRAITS_DIR / "core" / "bash.header.sh"


@dataclass
class _GeneratedFile:
    content: bytes  # for symlinks, the link target
    mode: int
    symlink: bool = False


@dataclass
class Flavor:
    id: str
    name: str
    description: str
    distro: Distro
    platform: Platform
    role: Role
    output: Output
    extra: tuple[Trait, ...] = ()
    # Suffix for release tags (`<tag><suffix>`); None means "-<id>". Stable for published flavors.
    release_tag_suffix: str | None = None
    cfg: Config = field(init=False, repr=False)

    @property
    def tag_suffix(self) -> str:
        return self.release_tag_suffix if self.release_tag_suffix is not None else f"-{self.id}"

    @property
    def traits(self) -> list[Trait]:
        from captain.traits.core import Core

        return [Core(), self.distro, self.platform, self.role, self.output, *self.extra]

    @property
    def supported_architectures(self) -> frozenset[str]:
        arches = frozenset(["amd64", "arm64"])
        for trait in self.traits:
            if trait.supported_architectures is not None:
                arches &= trait.supported_architectures
        return arches

    def setup(self, cfg: Config) -> None:
        if cfg is None:
            raise ValueError("cfg (Config) cannot be None")
        self.cfg = cfg
        cfg.output_kind = self.output.output_kind

    # --- questions answered by the traits ---------------------------------

    def keep_package_manager(self) -> bool:
        return any(t.keep_package_manager(self) for t in self.traits)

    def keep_hwdb(self) -> bool:
        return any(t.keep_hwdb(self) for t in self.traits)

    def project_relative(self, path: str) -> str:
        """*path* (relative to the project dir) as seen from a generated drop-in directory.

        mkosi resolves relative paths in a drop-in directory's config against that
        drop-in (mkosi.generated/mkosi.conf.d/<dropin>/), and this works both natively
        and in the builder container, where the project dir is mounted elsewhere.
        """
        return f"../../../{path}"

    def has_iso(self) -> bool:
        return self.output.supports_iso and self.platform.iso_capable

    def custom_packages(self, layer: Path) -> set[str]:
        # look into <layer>/packages.txt, if it exists, read each non-comment line
        custom_packages_file = layer / "packages.txt"
        if not custom_packages_file.exists():
            return set()
        log.debug("Reading custom packages from %s", custom_packages_file)
        return {
            line.strip()
            for line in custom_packages_file.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        }

    # --- hooks --------------------------------------------------------------

    def pre_mkosi_stage(self) -> None:
        for trait in self.traits:
            trait.pre_mkosi(self)

    def post_mkosi_stage(self) -> None:
        for trait in self.traits:
            trait.post_mkosi(self)

    def post_artifact_collect(self) -> None:
        for trait in self.traits:
            trait.post_artifact_collect(self)

    def list_arch_artifacts(self, output_arch: str) -> list[OutputArchArtifact]:
        artifacts: list[OutputArchArtifact] = []
        # The output's own artifacts come first, then whatever other traits add.
        self.output.add_arch_artifacts(self, output_arch, artifacts)
        for trait in self.traits:
            if trait is not self.output:
                trait.add_arch_artifacts(self, output_arch, artifacts)
        log.info("Artifacts for flavor %s for arch '%s': %s", self.id, output_arch, artifacts)
        return artifacts

    # --- generation ---------------------------------------------------------

    def generate(self, hash_only: bool = False) -> None:
        """Render the mkosi tree into cfg.generated_dir and compute cfg.flavor_hash."""
        files: dict[str, _GeneratedFile] = {
            "mkosi.conf": _GeneratedFile(
                f"# Generated by captain for flavor '{self.id}'; see mkosi.conf.d/\n".encode(),
                0o644,
            )
        }
        for trait in self.traits:
            self._generate_trait(trait, files)
        self._generate_custom(files)

        hashes = {path: hashlib.sha256(f.content).hexdigest() for path, f in files.items()}
        self.cfg.flavor_hash = hashlib.sha256(
            "".join(f"{k}={v}" for k, v in (sorted(hashes.items()))).encode()
        ).hexdigest()[:16]
        log.info(
            "Generated flavor_hash: %s (based on %s hashes)", self.cfg.flavor_hash, len(hashes)
        )

        # Emit a Rich Table of the generated files and their hashes
        if log.isEnabledFor(logging.DEBUG):
            table = Table(
                title="Generated Files and Hashes", show_header=True, header_style="bold magenta"
            )
            table.add_column("Relative Path", style="dim", width=60)
            table.add_column("SHA256 Hash", style="dim", width=64)
            for relative_path, hash_value in sorted(hashes.items()):
                table.add_row(relative_path, hash_value)
            captain.console.print(table)

        if hash_only:
            return

        out = self.cfg.generated_dir
        log.debug("Writing generated mkosi tree to %s", out)
        shutil.rmtree(out, ignore_errors=True)
        for relative_path, generated in files.items():
            target = out / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            if generated.symlink:
                target.symlink_to(generated.content.decode())
                continue
            target.write_bytes(generated.content)
            target.chmod(generated.mode)

    def _render(self, template_path: Path, keep_trailing_newline: bool = False) -> str:
        template = jinja2.Environment(
            loader=jinja2.FileSystemLoader(template_path.parent),
            undefined=jinja2.StrictUndefined,
            keep_trailing_newline=keep_trailing_newline,
        ).get_template(template_path.name)
        return template.render(cfg=self.cfg, flavor=self)

    def _script(self, parts: list[Path]) -> _GeneratedFile:
        rendered = "".join(self._render(p) + "\n" for p in [BASH_HEADER, *parts])
        return _GeneratedFile(rendered.encode(), 0o755)

    def _add_static_dir(
        self, files: dict[str, _GeneratedFile], src_dir: Path, dest_prefix: str
    ) -> None:
        if not src_dir.is_dir():
            return
        for src in sorted(src_dir.rglob("*")):
            relative = src.relative_to(src_dir)
            if src.is_symlink():
                # eg units masked via /dev/null, or default.target -> multi-user.target
                link = str(src.readlink()).encode()
                files[f"{dest_prefix}/{relative}"] = _GeneratedFile(link, 0o777, symlink=True)
                continue
            if not src.is_file():
                continue
            mode = src.stat().st_mode & 0o7777
            if src.suffix == ".j2":
                rendered = self._render(src, keep_trailing_newline=True)
                if not rendered.strip():
                    continue  # templates can opt out (eg. a repo file for the other distro family)
                content = rendered.encode()
                relative = relative.with_suffix("")
            else:
                content = src.read_bytes()
            files[f"{dest_prefix}/{relative}"] = _GeneratedFile(content, mode)

    def _settings(self, trait: Trait) -> str:
        """mkosi settings computed from a trait's methods (packages etc)."""
        lines: list[str] = []
        token = trait.cache_bust_token(self)
        if token:
            lines += [f"# cache-bust: {token}"]
        packages = self.distro.map_packages(trait.packages(self))
        package_dirs = {self.project_relative(d) for d in trait.package_directories(self)}
        if packages or package_dirs:
            lines += ["[Content]"]
        if packages:
            lines += ["Packages=", *(f"    {p}" for p in sorted(packages))]
        if package_dirs:
            lines += ["PackageDirectories=", *(f"    {d}" for d in sorted(package_dirs))]
        return "\n".join(lines) + "\n" if lines else ""

    def _generate_trait(self, trait: Trait, files: dict[str, _GeneratedFile]) -> None:
        dropins: list[str] = []
        for index, content_dir in enumerate(trait.content_dirs()):
            slug = str(content_dir.relative_to(TRAITS_DIR)).replace("/", "-")
            dropin = f"mkosi.conf.d/{trait.axis.value + index:02d}-{slug}"
            dropins.append(dropin)
            conf = content_dir / "mkosi.conf.j2"
            if conf.is_file():
                files[f"{dropin}/mkosi.conf"] = _GeneratedFile(self._render(conf).encode(), 0o644)
            for template_name, script_name in SCRIPTS.items():
                if (content_dir / template_name).is_file():
                    files[f"{dropin}/{script_name}"] = self._script([content_dir / template_name])
            for static_dir in STATIC_DIRS:
                self._add_static_dir(files, content_dir / static_dir, f"{dropin}/{static_dir}")

        settings = self._settings(trait)
        if not settings:
            return
        if not dropins:
            dropins.append(
                f"mkosi.conf.d/{trait.axis.value:02d}-{trait.axis.name.lower()}-{trait.name}"
            )
        conf_path = f"{dropins[-1]}/mkosi.conf"
        existing = files.get(conf_path)
        content = (existing.content.decode() + "\n" if existing else "") + settings
        files[conf_path] = _GeneratedFile(content.encode(), 0o644)

    def _generate_custom(self, files: dict[str, _GeneratedFile]) -> None:
        """The user's custom/ dir and matching custom/by-<axis>/<value>/ dirs, as the last
        drop-in. Use-cases:
        - add CA root certificate to global trust store (via ca-certificates package)
        - add some package (eg firmware) via custom/packages.txt
        - configure containerd to use specific mirrors
        - run some postinst or finalize script to do some custom configuration

        Layers merge by name, general to specific: same-path files and same-named
        scripts from a later layer replace earlier ones; scripts run sorted by name.
        """
        layers = captain.custom.layers(self)
        dropin = f"mkosi.conf.d/{Axis.CUSTOM.value:02d}-custom"
        for layer in layers:
            self._add_static_dir(files, layer / "files", f"{dropin}/mkosi.extra")
        for in_dir, script_name in (
            ("mkosi-postinst", "mkosi.postinst"),
            ("mkosi-finalize", "mkosi.finalize"),
        ):
            by_name = {
                f.name: f
                for layer in layers
                if (layer / in_dir).is_dir()
                for f in sorted((layer / in_dir).iterdir())
                if f.is_file()
            }
            scripts = [by_name[name] for name in sorted(by_name)]
            log.debug("Adding custom %s scripts: %s", script_name, scripts)
            if scripts:
                files[f"{dropin}/{script_name}"] = self._script(scripts)
        packages = self.distro.map_packages(
            set().union(*(self.custom_packages(layer) for layer in layers))
        )
        if packages:
            settings = "[Content]\nPackages=\n" + "".join(f"    {p}\n" for p in sorted(packages))
            files[f"{dropin}/mkosi.conf"] = _GeneratedFile(settings.encode(), 0o644)


def list_available_flavors() -> list[str]:
    from captain.flavors import FLAVORS

    return sorted(f.id for f in FLAVORS)


def create_and_setup_flavor_for_id(flavor_id: str, cfg: Config) -> Flavor:
    from captain.flavors import FLAVORS

    log.debug("Creating and setting up flavor for id '%s'", flavor_id)
    found = [f for f in FLAVORS if f.id == flavor_id]
    if not found:
        log.error("Flavor '%s' not found. Available: %s", flavor_id, list_available_flavors())
        raise SystemExit(1)

    # Copy, so the registry's instance is never bound to a Config.
    flavor = dataclasses.replace(found[0])
    flavor.setup(cfg)

    # Ensure the current arch is supported by the flavor
    if cfg.arch_info.arch not in flavor.supported_architectures:
        log.error(
            "Flavor '%s' does not support architecture '%s'. Supported architectures: %s",
            flavor.id,
            cfg.arch_info.arch,
            flavor.supported_architectures,
        )
        raise SystemExit(1)

    log.debug(
        "Flavor is setup; description: %s; supported_architectures: %s",
        flavor.description,
        flavor.supported_architectures,
    )
    return flavor
