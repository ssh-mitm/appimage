# appimage

```{toctree}
:hidden:
:maxdepth: 2

configuration
cli
runtime
changelog
```

**The `appimage` module packages Python applications as self-contained AppImages.**

It bundles a complete Python distribution from [python-build-standalone](https://github.com/astral-sh/python-build-standalone), manages entry points, and supports virtual environments that extend the bundled packages with additional ones.

> The bundled interpreter comes from [python-build-standalone](https://github.com/astral-sh/python-build-standalone) — the exact same source as `uv python install`. What you develop with locally is what gets shipped in the AppImage.

```{note}
AppImage is a Linux-only format. Building and running AppImages requires Linux.
Supported architectures: **x86_64**, **aarch64**, **armv7**.
[appimagetool](https://github.com/AppImage/AppImageKit) is downloaded automatically during the build — no manual installation required.
```

## Install

```sh
pip install appimage
```

## Build

```sh
python -m appimage.build
```

`app`, `entry_point`, and `python` version are detected automatically from `[project]` in your existing `pyproject.toml` — no appimage-specific configuration required. The `.desktop` file and AppRun script are generated automatically. If no icon is found a built-in default icon is used — add `myapp.png` to your project root to use your own.

The AppImage is written to `dist/myapp-x86_64.AppImage` (or the matching architecture name).

> **Tip:** `appimage-build` is also available as a standalone command after installation.

## Check what was detected

```sh
python -m appimage.build --check
```

```
Build configuration:
  app:            myapp          [project] name
  entry_point:    myapp          [project] scripts
  python:         3.11           [project] requires-python
  packages:       .              default (.)
  icon:           myapp.png      detected (myapp.png)
  desktop:        (generated)    will be generated
  build_dir:      build          default
  dist_dir:       dist           default
```

## Write detected values to pyproject.toml

```sh
python -m appimage.build --init
```

Adds only the auto-detected fields that are not already set — so you can review and adjust them.

## Integration with hatch

```toml
[tool.hatch.envs.appimage]
dependencies = ["appimage"]

[tool.hatch.envs.appimage.scripts]
build = ["python -m appimage.build"]
```

```sh
hatch run appimage:build
```
