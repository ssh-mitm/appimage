# CLI Reference

```sh
python -m appimage.ctl [COMMAND] [OPTIONS]
appimagectl [COMMAND] [OPTIONS]
```

`python -m appimage.ctl` is the recommended form — the same reasoning as
`python -m pip` over a bare `pip`: it guarantees the interpreter running
the tool is the one actually resolving it, rather than whatever
`appimagectl` happens to be first on `PATH`. The console script is
installed alongside it for convenience and behaves identically.

`COMMAND` defaults to building the AppImage when omitted — `appimagectl`
on its own, with no configuration required, already builds. The other
commands (`check`, `init`, `lock`, `enable-reproducible`) are each a
distinct, mutually exclusive action.

## Commands

| Command | Description |
|---|---|
| *(none)* | Build the AppImage. |
| `check` | Show detected build configuration and exit without building. |
| `init` | Write auto-detected values to `[tool.appimage]` in `pyproject.toml` (only missing keys) and exit. Resolves the latest python-build-standalone release to write `python_date`/`python_sha256` (a lightweight API call, no download), and resolves appimagetool and the runtime file (possibly downloading them, ~8 MB + ~1 MB) to write `appimagetool_version`/`appimagetool_sha256`/`runtime_sha256`, whichever of these aren't already set. |
| `lock` | Generate hash-pinned lock files and exit — a thin wrapper around `pip lock`, run through the bundled interpreter (see [Verified dependencies](reproducible-builds.md#verified-dependencies)). Generates `pylock.toml` for third-party dependencies *and* a build-backend lock file for the packaged project's own `[build-system].requires` in the same run, writing `pylock`/`build_pylock` to `pyproject.toml` for whichever isn't already set. |
| `enable-reproducible` | One-command onboarding: runs `init` then `lock`, then a real build with `reproducible` enforced — and only once that build succeeds, writes `reproducible = true` to `pyproject.toml`. See [Getting to full reproducibility](reproducible-builds.md#getting-to-full-reproducibility). |

## Options

Shared by every command above (a command-specific note is called out where
one applies):

| Option | Description |
|---|---|
| `--app NAME` | Override the application name. |
| `--entry-point EP` | Override the console script entry point. |
| `--python VERSION` | Override the Python version to bundle (e.g. `3.13`). |
| `--python-date DATE` | Override the python-build-standalone release date for reproducible builds (e.g. `20260211`). |
| `--extras EXTRA` | Override extras to install (e.g. `production`). May be repeated. |
| `--package TARGET` | Additional pip install target. May be repeated. |
| `--project-dir PATH` | Path to the project root (default: current directory). |
| `--appimagetool PATH` | Path to a local appimagetool binary. Skips PATH lookup and download. |
| `--appimagetool-version LABEL` | Informational label for the pinned appimagetool build. |
| `--appimagetool-sha256 SHA256` | Expected sha256 of the appimagetool binary, verified regardless of how it was resolved. |
| `--python-archive PATH` | Path to a local python-build-standalone tarball. Skips the download. |
| `--python-sha256 SHA256` | Expected sha256 of the python-build-standalone tarball. |
| `--runtime-file PATH` | Path to a local AppImage runtime ELF stub, passed to appimagetool as `--runtime-file`. Skips the download. |
| `--runtime-sha256 SHA256` | Expected sha256 of the runtime file, verified regardless of how it was resolved. |
| `--verify-downloads` | Abort the build instead of warning whenever appimagetool, the runtime file, or the Python archive would otherwise be used unverified. |
| `--require-zsyncmake` | Abort the build instead of warning when `update_info` is set but `zsyncmake` is not on `PATH`. |
| `--pylock PATH` | Path to a hash-pinned `pylock.toml` for third-party dependencies. Generate it with `lock`. |
| `--require-pylock` | Abort the build instead of warning when `pylock` is not set. |
| `--build-pylock PATH` | Path to a hash-pinned pylock-format file constraining the packaged project's own `[build-system].requires`. Converted to a classic hash-pinned constraints file and passed as `pip install --build-constraint` when installing the project itself, so pip's isolated build environment is hash-verified too. Generate it with `lock`, alongside `pylock.toml`. |
| `--require-build-pylock` | Abort the build instead of warning when `build_pylock` is not set. |
| `--uploaded-prior-to PnD` | Only meaningful on `lock`/`enable-reproducible`: passed through to `pip lock --uploaded-prior-to` as a cooldown window (e.g. `P7D` excludes packages published in the last 7 days) — gives the community time to catch a compromised release before it gets locked in. Applies to both lock files generated. |
| `--reproducible` | Enforce a build that's reproducible across machines and over time: implies `--verify-downloads` and `--require-zsyncmake`, and requires `python_date`/`appimagetool_sha256`/`runtime_sha256` to already be set (run `init` first). Does not resolve or write any values itself — for that, see `enable-reproducible` above. Independent of `--pylock`/`--require-pylock`/`--build-pylock`/`--require-build-pylock` — opt into dependency and build-backend hash-pinning separately. |

## Examples

```sh
# Build with a specific Python version
python -m appimage.ctl --python 3.13

# Reproducible build pinned to a specific release date
python -m appimage.ctl --python-date 20260211

# Override app name and entry point
python -m appimage.ctl --app myapp --entry-point myapp.cli:main

# Install extras and additional packages
python -m appimage.ctl --extras production --package extra-lib

# Build from a different project directory
python -m appimage.ctl --project-dir /path/to/project

# Use a locally installed appimagetool instead of downloading
python -m appimage.ctl --appimagetool /opt/appimagetool-x86_64.AppImage

# Use a previously downloaded Python archive (e.g. from another build)
python -m appimage.ctl --python-archive /shared/cache/python.tar.gz

# Fully offline build using local copies of appimagetool, the runtime, and Python
python -m appimage.ctl \
  --appimagetool /opt/appimagetool-x86_64.AppImage \
  --runtime-file /opt/runtime-x86_64 \
  --python-archive /shared/cache/python-3.11-x86_64.tar.gz

# Pin and verify the toolchain automatically, then build reproducibly
python -m appimage.ctl init   # writes python_date/python_sha256/appimagetool_version/appimagetool_sha256/runtime_sha256
python -m appimage.ctl

# Fail loudly instead of warning if anything ends up unverified
python -m appimage.ctl --verify-downloads

# One command: pin the toolchain, hash-pin every dependency, verify with a
# real build, and turn reproducible = true on once that build succeeds
python -m appimage.ctl enable-reproducible

# Piecewise equivalent, plus a one-off enforced build afterwards
python -m appimage.ctl init
python -m appimage.ctl lock
python -m appimage.ctl --reproducible

# Generate hash-pinned lock files (pylock.toml + build_pylock), then build against them
python -m appimage.ctl lock
python -m appimage.ctl --require-pylock --require-build-pylock

# Regenerate both locks with a 7-day cooldown, excluding just-published releases
python -m appimage.ctl lock --uploaded-prior-to P7D
```
