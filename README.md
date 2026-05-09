<p align="center">
  <img src="https://raw.githubusercontent.com/ssh-mitm/appimage/main/appimage/assets/default_icon.png" width="96" height="96">
</p>

<h1 align="center">appimage</h1>

<p align="center">
  <strong>Package Python applications as self-contained AppImages.</strong>
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

> **The same Python that uv installs.**
> The bundled interpreter comes from [python-build-standalone](https://github.com/astral-sh/python-build-standalone) — identical to what `uv python install` provides. What you develop with locally is what gets shipped in the AppImage.


## Quick Start

```sh
pip install appimage
```

**A `pyproject.toml` is all that's needed** — and if your project already has one, you're ready to build.
`app`, `entry_point`, and `python` version are read from `[project]` automatically.

```sh
# Check what will be detected before building
python -m appimage.build --check

# Build — the AppImage is written to dist/myapp-x86_64.AppImage
python -m appimage.build

# Optionally: persist detected values to pyproject.toml to pin or adjust them
python -m appimage.build --init
```


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

The AppImage can act as the Python interpreter for a virtual environment. Packages installed into the venv extend the bundled ones — without repackaging the AppImage:

```sh
./myapp-x86_64.AppImage --python-interpreter -m venv ~/.venv/myapp
~/.venv/myapp/bin/pip install extra-package
~/.venv/myapp/bin/myapp
```

When launched through a venv symlink, the bundled `appimage` module activates the environment automatically.


## Reproducible builds

Pin the exact Python release to get byte-for-byte reproducible AppImages:

```toml
[tool.appimage.build]
python_date = "20260211"
```


## Configuration

All options go in `[tool.appimage.build]` inside `pyproject.toml` — every key is optional. Lifecycle hooks, extra files, custom AppRun scripts, and environment variable injection are supported.

→ **[Full documentation](https://appimage.readthedocs.io)**
