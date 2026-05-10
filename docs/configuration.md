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
| `build_dir` | `"build"` | Directory for intermediate artefacts (Python tarball, appimagetool). |
| `dist_dir` | `"dist"` | Directory where the finished AppImage is written. |
| `update_info` | — | Update information string passed to appimagetool via `-u` (e.g. for zsync). |
| `appimagetool` | — | Path to a local appimagetool binary. When omitted, `PATH` is searched first, then the build cache, and finally a download. |
| `python_archive` | — | Path to a local python-build-standalone tarball. When omitted, the build cache is checked first, then a download. |

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

## Custom AppRun

When `apprun` is set, the file is copied as-is instead of generating one from the template. This gives full control over environment setup and the launch command:

```toml
[tool.appimage.build]
apprun = "packaging/AppRun"
```
