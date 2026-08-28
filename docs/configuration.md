# Configuration

All options go in `[tool.appimage.build]` inside `pyproject.toml`. Every key is optional — omitted keys are resolved automatically from `[project]` metadata.

## Build options

| Key | Default | Description |
|---|---|---|
| `app` | `project.name` | Application name — used as the AppImage filename prefix. |
| `entry_point` | `project.scripts` | Console script entry point launched by default. |
| `python` | `requires-python` | Python minor version to bundle, e.g. `"3.11"`. |
| `python_date` | *(latest)* | python-build-standalone release date for reproducible builds (e.g. `"20260211"`). |
| `extras` | `[]` | Extras to install from the current package, e.g. `["production"]` → `pip install ".[production]"`. |
| `packages` | `[]` | Additional pip install targets beyond the current package. |
| `icon` | auto-detected, then built-in default | Path to the icon file, relative to the project root. |
| `desktop` | auto-detected, then generated | Path to the `.desktop` file, relative to the project root. |
| `apprun` | *(generated)* | Path to a custom AppRun script. |
| `build_dir` | `"build"` | Directory for intermediate artefacts (Python tarball, appimagetool, runtime file). |
| `dist_dir` | `"dist"` | Directory where the finished AppImage is written. |
| `update_info` | — | Update information string passed to appimagetool via `-u` (e.g. for zsync). |
| `appimagetool` | — | Path to a local appimagetool binary. When omitted, `PATH` is searched first, then the build cache, and finally a download. |
| `appimagetool_version` | — | Informational label recording which appimagetool build `appimagetool_sha256` corresponds to. Written automatically by `--init`. |
| `appimagetool_sha256` | — | Expected sha256 of the appimagetool binary. When set, verified against whichever binary is resolved (explicit path, `PATH`, build cache, or download) — a mismatch aborts the build. A fresh download is auto-verified against GitHub's published digest even when unset; only a config-path/`PATH`/cache resolution with no pin falls back to an unverified warning logging its actual hash. |
| `python_archive` | — | Path to a local python-build-standalone tarball. When omitted, the build cache is checked first, then a download. |
| `python_sha256` | — | Expected sha256 of the python-build-standalone tarball. Fresh downloads are already verified against the digest GitHub publishes per release, even without this set; set explicitly to also verify a local `python_archive` or a cached tarball. |
| `runtime_file` | — | Path to a local AppImage runtime ELF stub, passed to appimagetool as `--runtime-file`. When omitted, the build cache is checked first, then a download — pre-fetching it this way avoids appimagetool's own live, unverified download at packaging time. |
| `runtime_sha256` | — | Expected sha256 of the runtime file, verified the same way as `appimagetool_sha256`. |
| `verify_downloads` | `false` | Abort the build instead of warning whenever appimagetool, the runtime file, or the Python archive would otherwise be used unverified. |

## Environment variables in AppRun

Extra environment variables are exported in the generated AppRun script:

```toml
[tool.appimage.build.env]
MY_PLUGIN_PATH = "/opt/plugins"
DEBUG = "0"
```

## Extra files

Copy additional files or directories into the AppDir:

```toml
[tool.appimage.build.extra_files]
"assets/" = "assets/"
"config.toml" = "config.toml"
```

Keys are source paths relative to the project root; values are destination paths relative to AppDir.

## Lifecycle hooks

Shell scripts called at specific points during the build. The `APPDIR` environment variable is set to the AppDir path when the hook runs.

```toml
[tool.appimage.build.hooks]
post_install = "scripts/post_install.sh"   # after pip install, before assets are copied
pre_package  = "scripts/pre_package.sh"    # after all files are in place, before appimagetool
```

Installed packages are byte-compiled (hash-based, reproducible `.pyc`) right
after `pre_package` runs and before appimagetool packages the AppDir, so a
hook that edits an installed package's source is still reflected in the
compiled bytecode.

## Custom AppRun

When `apprun` is set, the file is copied as-is instead of generating one from the template. This gives full control over environment setup and the launch command:

```toml
[tool.appimage.build]
apprun = "packaging/AppRun"
```
