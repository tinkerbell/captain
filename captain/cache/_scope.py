"""Where to read cache entries from, and where (if anywhere) to write them.

Two levels: *downstream* (a fork, eg. ghcr.io/rpardini/captain) and *upstream*
(ghcr.io/tinkerbell/captain). Reads try downstream first; writes only ever go
to the level the build itself belongs to. See docs/build-caching.md.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from captain.config import Config

log = logging.getLogger(__name__)

UPSTREAM_REPOSITORY = "tinkerbell/captain"
DEFAULT_UPSTREAM = f"ghcr.io/{UPSTREAM_REPOSITORY}"

_GITHUB_URL = re.compile(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$")


@dataclass(frozen=True)
class Location:
    host: str
    prefix: str  # eg. "rpardini/captain"
    github_repo: str | None = None  # "owner/name", when it is a GitHub repo's cache

    @classmethod
    def parse(cls, value: str) -> Location:
        host, _, prefix = value.strip().strip("/").partition("/")
        return cls(host, prefix.lower())

    @classmethod
    def for_github(cls, repo: str) -> Location:
        return cls("ghcr.io", repo.lower(), github_repo=repo)

    def repository(self, kind: str) -> str:
        return f"{self.prefix}/cache/{kind}"

    @property
    def source_url(self) -> str | None:
        return f"https://github.com/{self.github_repo}" if self.github_repo else None

    def __str__(self) -> str:
        return f"{self.host}/{self.prefix}"


@dataclass
class CacheScope:
    context: str  # human description of the detected context
    read: list[Location] = field(default_factory=list)
    write: Location | None = None
    no_write_reason: str = ""  # why *write* is None, for humans


def _upstream() -> Location | None:
    value = os.environ.get("CAPTAIN_CACHE_UPSTREAM", DEFAULT_UPSTREAM)
    if value.lower() in ("", "none", "off"):
        return None
    if value == DEFAULT_UPSTREAM:
        return Location.for_github(UPSTREAM_REPOSITORY)
    return Location.parse(value)


def _override_downstream() -> Location | None:
    value = os.environ.get("CAPTAIN_CACHE_DOWNSTREAM")
    return Location.parse(value) if value else None


def _git(project_dir: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), *args], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _github_repo_of_remote(project_dir: Path, remote: str) -> str | None:
    url = _git(project_dir, "remote", "get-url", remote)
    match = _GITHUB_URL.search(url) if url else None
    return f"{match[1]}/{match[2]}" if match else None


def _local_downstream(project_dir: Path) -> tuple[Location | None, str]:
    """The fork this checkout belongs to: the current branch's remote, else origin."""
    remotes: list[str] = []
    branch = _git(project_dir, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch:
        tracking = _git(project_dir, "config", f"branch.{branch}.remote")
        if tracking and tracking != ".":
            remotes.append(tracking)
    remotes.append("origin")
    for remote in remotes:
        repo = _github_repo_of_remote(project_dir, remote)
        if repo and repo.lower() != UPSTREAM_REPOSITORY:
            return Location.for_github(repo), f"git remote '{remote}' ({repo})"
    return None, "no GitHub fork remote found"


def _dedupe(locations: list[Location | None]) -> list[Location]:
    seen: list[Location] = []
    for loc in locations:
        if loc is not None and str(loc) not in {str(s) for s in seen}:
            seen.append(loc)
    return seen


def _github_actions_scope(cfg: Config, upstream: Location | None) -> CacheScope:
    repo = os.environ["GITHUB_REPOSITORY"]
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    own = _override_downstream() or Location.for_github(repo)

    head_repo: str | None = None
    if event.startswith("pull_request"):
        try:
            payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
            head_repo = payload["pull_request"]["head"]["repo"]["full_name"]
        except (KeyError, OSError, TypeError, json.JSONDecodeError) as exc:
            log.warning("Could not determine the PR head repository (%s); assuming a fork", exc)
            head_repo = "unknown/unknown"
    fork_pr = head_repo is not None and head_repo.lower() != repo.lower()

    head = None
    if fork_pr and head_repo is not None and head_repo != "unknown/unknown":
        head = Location.for_github(head_repo)
    read = _dedupe([head, own, upstream])
    write, no_write_reason = own, ""
    if fork_pr:
        write, no_write_reason = None, "PR from a fork"
    elif cfg.cache_mode != "on":
        write, no_write_reason = None, f"--cache {cfg.cache_mode}"

    kind = "upstream" if upstream is not None and str(own) == str(upstream) else "downstream"
    context = f"GitHub Actions, {kind} repo {repo}, event {event or '?'}"
    if fork_pr:
        context += f", PR from fork {head_repo} (read-only)"
    return CacheScope(context, read, write, no_write_reason)


def _local_scope(cfg: Config, upstream: Location | None) -> CacheScope:
    downstream = _override_downstream()
    how = "CAPTAIN_CACHE_DOWNSTREAM"
    if downstream is None:
        downstream, how = _local_downstream(cfg.project_dir)
    write, no_write_reason = None, ""
    if cfg.cache_mode != "on":
        no_write_reason = f"--cache {cfg.cache_mode}"
    elif downstream is None:
        if cfg.cache_push:
            log.warning("--cache-push given, but there is no downstream cache to push to")
        no_write_reason = "no downstream (fork) cache to push to"
    elif cfg.cache_push:
        write = downstream
    else:
        no_write_reason = f"local build; add --cache-push to push to {downstream}"
    read = _dedupe([downstream, upstream])
    return CacheScope(f"local build, downstream from {how}", read, write, no_write_reason)


def resolve_scope(cfg: Config) -> CacheScope:
    if cfg.cache_mode == "off":
        return CacheScope("disabled", no_write_reason="--cache off")
    upstream = _upstream()
    if os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("GITHUB_REPOSITORY"):
        return _github_actions_scope(cfg, upstream)
    return _local_scope(cfg, upstream)
