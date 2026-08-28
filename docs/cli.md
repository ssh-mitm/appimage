# CLI Reference

## Build command

```sh
python -m appimage.build [OPTIONS]
appimage-build [OPTIONS]
```

### Options

| Option | Description |
|---|---|
| `--check` | Show detected build configuration and exit without building. |
| `--init` | Write auto-detected values to `[tool.appimage.build]` in `pyproject.toml` (only missing keys). May resolve appimagetool and the runtime file (possibly downloading them, ~8 MB + ~1 MB) to also write `appimagetool_version`/`appimagetool_sha256`/`runtime_sha256` when not already set. |
| `--lock` | Generate a hash-pinned `pylock.toml` for third-party dependencies and exit — a thin wrapper around `pip lock`, run through the bundled interpreter (see [Verified dependencies](reproducible-builds.md#verified-dependencies)). Writes `pylock` to `pyproject.toml` if not already set. |
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
| `--pylock PATH` | Path to a hash-pinned `pylock.toml` for third-party dependencies. Generate it with `--lock`. |
| `--require-pylock` | Abort the build instead of warning when `pylock` is not set. |
| `--uploaded-prior-to PnD` | Only used with `--lock`: passed through to `pip lock --uploaded-prior-to` as a cooldown window (e.g. `P7D` excludes packages published in the last 7 days) — gives the community time to catch a compromised release before it gets locked in. |
| `--reproducible` | Shortcut for a build that's reproducible across machines and over time: implies `--verify-downloads` and `--require-zsyncmake`, and requires `python_date`/`appimagetool_sha256`/`runtime_sha256` to already be set (run `--init` first). Does not resolve or write any values itself. Independent of `--pylock`/`--require-pylock` — opt into dependency hash-pinning separately. |

### Examples

```sh
# Build with a specific Python version
python -m appimage.build --python 3.13

# Reproducible build pinned to a specific release date
python -m appimage.build --python-date 20260211

# Override app name and entry point
python -m appimage.build --app myapp --entry-point myapp.cli:main

# Install extras and additional packages
python -m appimage.build --extras production --package extra-lib

# Build from a different project directory
python -m appimage.build --project-dir /path/to/project

# Use a locally installed appimagetool instead of downloading
python -m appimage.build --appimagetool /opt/appimagetool-x86_64.AppImage

# Use a previously downloaded Python archive (e.g. from another build)
python -m appimage.build --python-archive /shared/cache/python.tar.gz

# Fully offline build using local copies of appimagetool, the runtime, and Python
python -m appimage.build \
  --appimagetool /opt/appimagetool-x86_64.AppImage \
  --runtime-file /opt/runtime-x86_64 \
  --python-archive /shared/cache/python-3.11-x86_64.tar.gz

# Pin and verify appimagetool/runtime automatically, then build reproducibly
python -m appimage.build --init   # writes appimagetool_version/appimagetool_sha256/runtime_sha256
python -m appimage.build --python-date 20260211

# Fail loudly instead of warning if anything ends up unverified
python -m appimage.build --verify-downloads

# Pin everything once, then enforce full cross-machine reproducibility on every build
python -m appimage.build --init
python -m appimage.build --reproducible

# Generate a hash-pinned pylock.toml for dependencies, then build against it
python -m appimage.build --lock
python -m appimage.build --require-pylock

# Regenerate the lock with a 7-day cooldown, excluding just-published releases
python -m appimage.build --lock --uploaded-prior-to P7D
```
