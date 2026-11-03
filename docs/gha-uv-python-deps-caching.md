# GitHub Actions: caching uv, CPython and Python dependencies

Every CI job runs `captain` through `uv`. A run has ~44 jobs on fresh runners, so without caching
each of them downloads uv, a CPython build and captain's Python dependencies again. This document
covers how CI avoids that. Local builds are not affected: uv's own cache already works on a single
machine.

Build outputs (tools, kernels, mkosi images, ISOs) are cached differently, in OCI registries; see
`docs/build-caching.md`. The GitHub Actions cache only holds uv and what it installs.

## The problem

Measured on a fork's run before this was set up (44 jobs using uv):

- **uv binary** (~20 MB from releases.astral.sh): downloaded by every job.
- **CPython** (~36 MB from python-build-standalone): downloaded by every job. `pyproject.toml`
  requires Python >= 3.13 and the runners' system Python is 3.12, so uv fetched the newest CPython it
  knew (3.15.2), and `astral-sh/setup-uv` ran with `cache-python: false`.
- **Python packages** (click, rich, jinja2, trogon, textual, …, from PyPI): downloaded by every job.
  `setup-uv` did restore a GitHub Actions cache, but it was 296 KB. With `prune-cache: true`
  (the default before setup-uv v9), wheels that came from PyPI are dropped before saving. And with
  `uv.lock` gitignored, the cache key never changed.

That's ~2.5 GB of downloads per run, mostly identical.

## The fix: `.github/actions/setup-uv`

A local composite action, used by every job in `ci.yml` and `release.yml` instead of calling
`astral-sh/setup-uv` directly (it must come after `actions/checkout`, which every job already has):

```yaml
- name: Install uv
  uses: ./.github/actions/setup-uv
```

It does two things.

1. **Restores the uv binary** into the runner's tool cache with `actions/cache`, keyed
   `uv-binary-<os>-<arch>-<uv version>`. `setup-uv` looks in the tool cache before downloading
   (`Found uv in tool-cache for <version>` in the log), so it doesn't download.
2. **Runs `astral-sh/setup-uv`** with:

   | Setting | Value | Why |
   |---|---|---|
   | `version` | pinned (`uv-version` input, default `0.13.6`) | stable cache keys and behaviour |
   | `python-version` | `3.13` (`python-version` input) | sets `UV_PYTHON` for the job: one CPython minor for all of CI |
   | `enable-cache` | `auto` | on for `push` / `pull_request`; see [Events without caching](#events-without-caching) |
   | `cache-python` | `true` | CPython comes from the GitHub Actions cache |
   | `cache-dependency-glob` | `pyproject.toml` | the key changes when the declared dependencies do |
   | `prune-cache` | `false` | keep downloaded wheels (also setup-uv's default since v9) |
   | `cache-suffix` | `v1` (`cache-suffix` input) | bump to start fresh caches |

The resulting cache entries, per runner architecture:

| Entry | Key (example) | Size |
|---|---|---|
| uv binary | `uv-binary-Linux-X64-0.13.6` | ~15 MiB |
| wheels | `setup-uv-2-x86_64-…-ubuntu-24.04-3.13-py-<pyproject hash>-v1` | 2–27 MiB |
| CPython | same, plus `-python` | 28–34 MiB |

The amd64 wheels entry is larger because the `prepare` job, which runs on amd64, also installs the
lint tools. All of it is small compared with the 10 GB GitHub Actions cache per repository.

## Results

The first run after the change (fork, 44 jobs):

- 41 jobs: `Found uv in tool-cache`; the other 3 started before the cache had been saved, and
  downloaded uv once more.
- 0 CPython downloads (`Using CPython 3.13.x` everywhere); 0 Python package downloads from PyPI.
- "Install uv" takes ~4–5 s.

From the second run on, nothing is downloaded from astral.sh, python-build-standalone or PyPI.

## Maintenance

- **Newer uv**: bump `uv-version` in `.github/actions/setup-uv/action.yml`. The first run with it
  downloads it once per arch and caches it.
- **Newer dependencies**: `uv.lock` is gitignored (CI always resolves the latest versions). Cached
  wheels are only refreshed when `pyproject.toml` changes; to pick up newer releases of unchanged
  dependencies sooner, bump `cache-suffix`. Even with a stale cache, uv still resolves the latest
  versions and downloads only what's missing.
- **Another Python for CI**: change `python-version` (it must satisfy `requires-python`).
- **Newer `astral-sh/setup-uv`**: since v8 it no longer moves floating major tags (no `@v10`), so it
  is pinned to an exact release (`@v10.3.2`); read its release notes for breaking changes before
  bumping.
- GitHub Actions caches unused for 7 days are evicted; `gh cache list` / `gh cache delete` manage them.

## Events without caching

`setup-uv`'s `enable-cache: auto` (since v10) skips the cache for releases, tag pushes,
`pull_request_target` and `workflow_run`, against cache poisoning. The uv binary restore follows the
same rule. So the upstream release workflow (on `v*` tag pushes) sets up uv without caches; it is a
single job. Pull requests from forks can read the base branch's caches.

## Not covered

The `prepare` job's `pyright` (the PyPI wrapper) downloads Node.js and pyright from npm, once per run.
