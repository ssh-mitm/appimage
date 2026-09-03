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
llms
changelog
```

**The `appimage` module packages Python applications as self-contained AppImages.**

It bundles a complete Python distribution from [python-build-standalone](https://github.com/astral-sh/python-build-standalone), manages entry points, and supports virtual environments that extend the bundled packages with additional ones.

> The bundled interpreter comes from [python-build-standalone](https://github.com/astral-sh/python-build-standalone) - the exact same source as `uv python install`. What you develop with locally is what gets shipped in the AppImage.

```{note}
AppImage is a Linux-only format. Building and running AppImages requires Linux.
Supported architectures: **x86_64**, **aarch64**, **armv7**.
[appimagetool](https://github.com/AppImage/appimagetool) is resolved automatically: the build cache is checked first, then it is downloaded (`PATH` is never searched). The same caching logic applies to the bundled Python distribution. Set `appimagetool_sha256` in `[tool.appimage]` to verify whichever binary gets resolved - see [Reproducible builds](reproducible-builds.md).
```

## Install

```sh
pip install appimage
```

## Build

```sh
python -m appimage.ctl build
```

`app`, `entry_point`, and `python` version are detected automatically from `[project]` in your existing `pyproject.toml` - no appimage-specific configuration required. The `.desktop` file and AppRun script are generated automatically. If no icon is found a built-in default icon is used - add `myapp.png` to your project root to use your own.

The AppImage is written to `dist/myapp-x86_64.AppImage` (or the matching architecture name).

> **Tip:** `appimagectl` is also available as a standalone command after installation - `python -m appimage.ctl` and `appimagectl` behave identically.

## Check what was detected

```sh
python -m appimage.ctl check
```

```
Build configuration:
  app:            myapp                               [[project] name]
  entry_point:    myapp                               [[project] scripts]
  python:         3.11                                [[project] requires-python]
  packages:       appimage==3.0.0 .                    [default (.)]
  icon:           myapp.png                            [detected (myapp.png)]
  desktop:        (generated)                          [will be generated]
  build_dir:      build                                [default]
  dist_dir:       dist                                 [default]

  Reproducibility checklist (0/5 ready):
    ✗ AppDir reproducibility: python_date not set - run 'init' to resolve and pin it, or set python_dir
    ✗ Runtime module reproducibility: appimage_version, appimage_sha256 not set - run 'init' to resolve and pin them
    ✗ Packaging reproducibility: appimagetool_sha256, runtime_sha256 not set - run 'init' to resolve and pin them
    ✗ Dependency verification: pylock not set - run 'lock' to generate pylock.toml
    ✗ Build backend verification: build_pylock not set - run 'lock' to generate it alongside pylock.toml
```

None of that is required to build - see [Reproducible builds](reproducible-builds.md) for what each checklist line means and how to close it.

## Write detected values to pyproject.toml

```sh
python -m appimage.ctl init
```

Adds only the auto-detected fields that are not already set - so you can review and adjust them.

## Using a coding agent?

See [For LLMs and coding agents](llms.md) for a dense reference - module
map and the non-obvious invariants worth knowing before changing anything
in this codebase.
