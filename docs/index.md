# appimage

```{toctree}
:hidden:
:maxdepth: 2

configuration
cli
examples
reproducible-builds
runtime
internals
develop/index
changelog
```

**The `appimage` module packages Python applications as self-contained AppImages.**

It bundles a complete Python distribution from [python-build-standalone](https://github.com/astral-sh/python-build-standalone), manages entry points, and supports virtual environments that extend the bundled packages with additional ones.

> The bundled interpreter comes from [python-build-standalone](https://github.com/astral-sh/python-build-standalone) — the exact same source as `uv python install`. What you develop with locally is what gets shipped in the AppImage.

```{note}
AppImage is a Linux-only format. Building and running AppImages requires Linux.
Supported architectures: **x86_64**, **aarch64**, **armv7**.
[appimagetool](https://github.com/AppImage/appimagetool) is resolved automatically: if it is already on `PATH` it is used as-is; otherwise the build cache is checked, and finally it is downloaded. The same caching logic applies to the bundled Python distribution. Set `appimagetool_sha256` in `[tool.appimage.build]` to verify whichever binary gets resolved, regardless of source — see [Reproducible builds](reproducible-builds.md).
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
  app:            myapp                               [[project] name]
  entry_point:    myapp                               [[project] scripts]
  python:         3.11                                [[project] requires-python]
  packages:       appimage==2.0.1 .                    [default (.)]
  icon:           myapp.png                            [detected (myapp.png)]
  desktop:        (generated)                          [will be generated]
  build_dir:      build                                [default]
  dist_dir:       dist                                 [default]

  Reproducibility checklist (0/3 ready):
    ✗ Reproducibility: 0/3 pins set (python_date, appimagetool_sha256, runtime_sha256) — run --init to resolve and pin them
    ✗ Dependency verification: pylock not set — run --lock to generate pylock.toml
    ✗ Build backend verification: build_pylock not set — run --lock to generate it alongside pylock.toml
```

None of that is required to build — see [Reproducible builds](reproducible-builds.md) for what each checklist line means and how to close it.

## Write detected values to pyproject.toml

```sh
python -m appimage.build --init
```

Adds only the auto-detected fields that are not already set — so you can review and adjust them.
