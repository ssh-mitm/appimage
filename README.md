<p align="center">
  <img src="https://raw.githubusercontent.com/ssh-mitm/appimage/main/appimage/assets/default_icon.png" width="96" height="96">
</p>

<h1 align="center">appimage</h1>

<p align="center">
  <strong>Zero-setup, reproducible builds.</strong>
</p>

<p align="center">
  <a href="https://appimage.readthedocs.io">
    <img src="https://raw.githubusercontent.com/ssh-mitm/appimage/main/docs/_static/readthedocslogo.png" width="256" alt="Read the Docs">
  </a>
</p>

<p align="center">
  <a href="https://pypi.org/project/appimage"><img src="https://img.shields.io/pypi/v/appimage" alt="PyPI"></a>
  <a href="https://pypi.org/project/appimage"><img src="https://img.shields.io/pypi/pyversions/appimage" alt="Python versions"></a>
  <a href="https://github.com/ssh-mitm/appimage/blob/main/LICENSE"><img src="https://img.shields.io/github/license/ssh-mitm/appimage" alt="License"></a>
  <a href="https://appimage.readthedocs.io"><img src="https://readthedocs.org/projects/appimage/badge/?version=latest" alt="Documentation Status"></a>
</p>

---

`appimage` bundles a complete Python distribution together with your application and all its dependencies into a single executable file.

- **Same Python as uv**: the bundled interpreter comes from [python-build-standalone](https://github.com/astral-sh/python-build-standalone), the same builds `uv python install` uses - what you develop and test with locally is what ships in the AppImage
- **Reproducible builds**: two independent builds of the same project produce a byte-for-byte identical `.AppImage`, no configuration needed ([details below](#reproducible-builds))


## Quick Start

```sh
pip install appimage
```

A `pyproject.toml` is all that's needed to build. If your project already has one, `app`, `entry_point`, and `python` version are read from `[project]` automatically.

```sh
# Check what will be detected before building
python -m appimage.ctl check

# Writes the AppImage to dist/myapp-x86_64.AppImage
python -m appimage.ctl

# Optionally: persist detected values to pyproject.toml to pin or adjust them
python -m appimage.ctl init
```


## Reproducible builds

Covers bytecode compilation, file timestamps, and the `appimagetool` binary itself - `appimage` defaults to the [maintained successor](https://github.com/AppImage/appimagetool) of the classic tool, whose bundled `mksquashfs` has a [documented non-deterministic compression bug](https://github.com/AppImage/AppImageKit/issues/929).

To guarantee this across machines and over time too, with every dependency hash-verified:

```sh
python -m appimage.ctl enable-reproducible   # pin the toolchain, hash-pin every
                                              # dependency, verify with a real build,
                                              # then turn reproducible = true on
```

See [Reproducible builds](https://appimage.readthedocs.io/en/latest/reproducible-builds.html) for what's automatic, how to pin appimagetool/Python for cross-machine guarantees, and how to hash-verify third-party dependencies with `lock`.


## Bundled interpreter access

The bundled Python is accessible at runtime without extracting the AppImage:

```sh
./myapp-x86_64.AppImage --python-interpreter            # interactive REPL
./myapp-x86_64.AppImage --python-interpreter script.py  # run a script
./myapp-x86_64.AppImage --python-interpreter -m pip list
./myapp-x86_64.AppImage --python-list-entry-points      # list all entry points
./myapp-x86_64.AppImage --python-entry-point other:main # switch entry point
```


## Virtual environments

The AppImage can act as the Python interpreter for a virtual environment. Packages installed into the venv extend the bundled ones, without repackaging the AppImage:

```sh
./myapp-x86_64.AppImage --python-interpreter -m venv ~/.venv/myapp
~/.venv/myapp/bin/pip install extra-package
~/.venv/myapp/bin/myapp
```

When launched through a venv symlink, the bundled `appimage` module activates the environment automatically.


## Configuration

All options go in `[tool.appimage]` inside `pyproject.toml`, and every key is optional. Lifecycle hooks, extra files, custom AppRun scripts, and environment variable injection are supported. See the [full documentation](https://appimage.readthedocs.io) for details.
