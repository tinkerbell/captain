"""Flavor-specific configuration."""

from __future__ import annotations

import hashlib
import logging
import shutil
from abc import abstractmethod
from pathlib import Path
from typing import Protocol, runtime_checkable

import jinja2
from rich.table import Table

import captain
from captain.artifacts import OutputArchArtifact, OutputArchArtifactType
from captain.config import Config

log = logging.getLogger(__name__)


@runtime_checkable
class BaseFlavor(Protocol):
    cfg: Config
    id: str
    name: str
    description: str
    flavor_dir: Path
    supported_architectures: frozenset[str]
    template_map: dict[str, list[Path]]
    static_map: dict[str, Path]

    def setup(self, cfg: Config, flavor_dir: Path) -> None:
        if cfg is None:
            raise ValueError("cfg (Config) cannot be None")
        self.cfg = cfg

        if flavor_dir is None:
            raise ValueError("flavor_dir (Path) cannot be None")
        if not flavor_dir.is_dir():
            raise ValueError(f"flavor_dir {flavor_dir} does not exist or is not a directory")
        self.flavor_dir = flavor_dir

        self.template_map = {}
        self.static_map = {}
        log.debug("Called BaseFlavor.setup()...")
        pass

    def generate(self, hash_only: bool = False):
        log.debug("Called BaseFlavor.generate()...")
        # Before generating, cleanup known targets. @TODO make dir disposable instead
        log.debug("Cleaning up old generated files in %s", self.cfg.project_dir)
        shutil.rmtree(self.cfg.project_dir / "mkosi.conf", ignore_errors=True)
        shutil.rmtree(self.cfg.project_dir / "mkosi.postinst", ignore_errors=True)
        shutil.rmtree(self.cfg.project_dir / "mkosi.finalize", ignore_errors=True)
        shutil.rmtree(self.cfg.project_dir / "mkosi.extra", ignore_errors=True)
        shutil.rmtree(self.cfg.project_dir / "mkosi.sandbox", ignore_errors=True)
        shutil.rmtree(self.cfg.project_dir / "mkosi.skeleton", ignore_errors=True)

        hashes: dict[str, str] = {}
        self.copy_static_files(self.cfg.project_dir, hash_only, hashes)
        self.render_templates(self.cfg.project_dir, hash_only, hashes)  # For compatibility

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
            table.add_column("Relative Path", style="dim", width=40)
            table.add_column("SHA256 Hash", style="dim", width=64)
            for relative_path, hash_value in hashes.items():
                table.add_row(relative_path, hash_value)
            captain.console.print(table)

        pass

    def specific_flavor_dir(self, flavor_id: str) -> Path:
        flavor_id_underscore = flavor_id.replace("-", "_")
        flavor_dir = self.cfg.project_dir / "captain" / "flavors" / flavor_id_underscore

        if not flavor_dir.is_dir():
            log.error(
                "Specific Flavor dir '%s' not found. Expected to find directory %s",
                flavor_id,
                flavor_dir,
            )
            raise SystemExit(1)
        return flavor_dir

    def add_static_dir(self, dir_to_include: str, flavor_dir: Path, prefix_with: str | None = None):
        extra_dir = flavor_dir / dir_to_include
        if extra_dir.exists() and extra_dir.is_dir():
            for extra_file in extra_dir.rglob("*"):
                if extra_file.is_file():
                    relative_path = extra_file.relative_to(flavor_dir)
                    if prefix_with is not None:
                        relative_path = Path(prefix_with) / extra_file.relative_to(extra_dir)
                    self.static_map[str(relative_path)] = extra_file

    def render_templates(
        self, output_dir: Path, hash_only: bool = False, hashes: dict[str, str] | None = None
    ):
        log.debug("Called BaseFlavor.render_templates() with output_dir: %s", output_dir)
        # Use jinja2 to render all templates in self.template_map, writing output to output_dir
        # The keys of self.template_map are the relative output paths (e.g. "mkosi.conf"), and the
        # values are lists of Path objects pointing to Jinja2 template files.
        # If more than one template is provided for a given output path, they should be rendered
        # in order and concatenated together to produce the final output file.
        for relative_output_path, template_paths in self.template_map.items():
            log.debug(
                "Rendering templates for output path '%s': %s",
                relative_output_path,
                template_paths,
            )
            rendered_content = ""
            for template_path in template_paths:
                log.debug("Rendering template %s", template_path)
                # Here you would load the template file, render it with the appropriate context
                # (e.g. using Jinja2), and append the rendered content to rendered_content.
                # For example:
                template = jinja2.Environment(
                    loader=jinja2.FileSystemLoader(template_path.parent),
                    undefined=jinja2.StrictUndefined,
                ).get_template(template_path.name)
                rendered_content += template.render(cfg=self.cfg, flavor=self) + "\n"

            if hashes is not None:
                hashes[relative_output_path] = hashlib.sha256(rendered_content.encode()).hexdigest()
            if hash_only:
                continue  # Skip writing if we're only interested in hashes

            output_file_path = output_dir / relative_output_path
            log.debug("Writing rendered content to %s", output_file_path)
            output_file_path.parent.mkdir(parents=True, exist_ok=True)
            output_file_path.write_text(rendered_content)

            # Make output_file executable @TODO: we will need a way to tell
            output_file_path.chmod(output_file_path.stat().st_mode | 0o111)

    def copy_static_files(
        self, project_dir, hash_only: bool = False, hashes: dict[str, str] | None = None
    ):
        # Do a plain copy of all files in self.static_map to project_dir / relative_path, where
        # relative_path is the key in self.static_map
        for relative_path, source_path in self.static_map.items():
            if hashes is not None:
                hashes[relative_path] = hashlib.sha256(source_path.read_bytes()).hexdigest()
            if hash_only:
                continue  # Skip copying if we're only interested in hashes
            destination_path = project_dir / relative_path
            log.debug("Copying static file from '%s' to '%s'", source_path, destination_path)
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)

    def cache_bust_token(self) -> str:
        """Token rendered into mkosi.conf so a flavor's hash changes on demand.

        Base flavors return an empty string (stable hash). Flavors whose upstream
        packages move under a stable name (e.g. armbian meta-packages) override
        this to fold a repo version token in, forcing a rebuild when it changes.
        """
        return ""

    @abstractmethod
    def has_iso(self) -> bool:
        return False

    def pre_mkosi_stage(self):
        pass

    def post_mkosi_stage(self):
        pass

    def post_artifact_collect(self):
        pass

    def list_arch_artifacts(self, output_arch: str) -> list[OutputArchArtifact]:
        artifacts: list[OutputArchArtifact] = [
            OutputArchArtifact(
                type=OutputArchArtifactType.FILE, name=f"vmlinuz-{self.cfg.flavor_id}-{output_arch}"
            ),
            OutputArchArtifact(
                type=OutputArchArtifactType.FILE,
                name=f"initramfs-{self.cfg.flavor_id}-{output_arch}",
            ),
        ]
        # include .iso if the flavor supports it.
        if self.has_iso():
            artifacts += [
                OutputArchArtifact(
                    type=OutputArchArtifactType.FILE,
                    name=f"captainos-{self.cfg.flavor_id}-{output_arch}.iso",
                )
            ]

        self.add_arch_dtb_artifacts(artifacts, output_arch)

        log.info(
            "Artifacts for flavor %s for arch '%s': %s", self.cfg.flavor_id, output_arch, artifacts
        )
        return artifacts

    def add_arch_dtb_artifacts(self, artifacts: list[OutputArchArtifact], output_arch: str):
        # if arm64, include DTBs.
        if output_arch == "aarch64":
            artifacts.append(
                OutputArchArtifact(
                    type=OutputArchArtifactType.DIRECTORY,
                    name=f"dtb-{self.cfg.flavor_id}-{output_arch}",
                )
            )


def list_available_flavors() -> list[str]:
    import importlib
    import pkgutil

    package = importlib.import_module("captain.flavors")
    # iter_modules finds immediate children; walk_packages recurses
    ret = []
    for _finder, module_name, _is_pkg in pkgutil.walk_packages(
        package.__path__, prefix=f"{package.__name__}."
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            log.debug(f"Skipping {module_name}: {exc}")
            continue

        fn = getattr(module, "create_flavor", None)
        if fn is not None and callable(fn):
            flavor_id = module_name.rsplit(".", 1)[-1].replace("_", "-")
            ret.append(flavor_id)
            log.debug("Discovered flavor '%s' via module %s", flavor_id, module_name)

    return sorted(ret)


def create_and_setup_flavor_for_id(flavor_id: str, cfg: Config) -> BaseFlavor:
    log.debug("Creating and setting up flavor for id '%s'", flavor_id)
    flavor_id_underscore = flavor_id.replace("-", "_")
    flavor_dir = cfg.project_dir / "captain" / "flavors" / flavor_id_underscore

    if not flavor_dir.is_dir():
        log.error(
            "Flavor '%s' not found. Expected to find directory %s",
            flavor_id,
            flavor_dir,
        )
        raise SystemExit(1)

    wanted_module = f"captain.flavors.{flavor_id_underscore}"
    log.debug("Attempting to import flavor module %s from directory %s", wanted_module, flavor_dir)

    try:
        module = __import__(wanted_module, fromlist=["create_flavor"])
    except ImportError as e:
        log.error(
            "Failed to import flavor module %s from directory %s: %s",
            wanted_module,
            flavor_dir,
            e,
        )
        raise e

    # Validate API explicitly
    if not hasattr(module, "create_flavor"):
        log.error("Flavor module %s does not define create_flavor()", wanted_module)
        raise SystemExit(1)

    log.debug("Executing %s.create_flavor()", wanted_module)
    flavor: BaseFlavor = module.create_flavor()

    if not isinstance(flavor, BaseFlavor):
        log.error(
            "create_flavor() in %s did not return BaseFlavor (got %r)",
            wanted_module,
            type(flavor),
        )
        raise SystemExit(1)

    log.debug("Calling setup() on flavor %s with config: %s", flavor, cfg)
    flavor.setup(cfg, flavor_dir)

    # Ensure the current arch is supported by the flavor
    if cfg.arch_info.arch not in flavor.supported_architectures:
        log.error(
            "Flavor '%s' does not support architecture '%s'. Supported architectures: %s",
            flavor.id,
            cfg.arch_info.arch,
            flavor.supported_architectures,
        )
        raise SystemExit(1)
    else:
        log.debug(
            "Flavor '%s' supports architecture '%s'",
            flavor.id,
            cfg.arch_info.arch,
        )

    log.debug(
        "Flavor is setup; description: %s; supported_architectures: %s",
        flavor.description,
        flavor.supported_architectures,
    )

    return flavor
