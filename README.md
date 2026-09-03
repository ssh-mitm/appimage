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
- **Reproducible builds, opt-in**: pin the toolchain once and get byte-identical `.AppImage` output across machines and over time ([details below](#reproducible-builds))


## Quick Start — just want an AppImage

```sh
python -m pip install appimage
```

**A `pyproject.toml` is all that's needed to build.** If your project already has one, `app`, `entry_point`, and `python` version are read from `[project]` automatically.

> No `pyproject.toml` yet? [`uv init`](https://docs.astral.sh/uv/guides/projects/) creates a minimal one in seconds.

```sh
# Check what will be detected before building
python -m appimage.ctl check

# Writes the AppImage to dist/myapp-x86_64.AppImage
python -m appimage.ctl build
```

✅ **That's it - a working AppImage.** Shipping to production? See [Reproducible builds](#reproducible-builds) below for byte-identical output across machines and over time.


## Reproducible builds

Two reasons this matters: **security** - anyone can independently rebuild from source and verify the result matches, instead of trusting the build server - and **production operation** - an unpinned toolchain silently changes what gets bundled as tools and dependencies release new versions, so the same build command can produce a different result next month even without a single code change.

**To guarantee byte-identical builds across machines and over time:**

```sh
# See what's pinned and what's missing
python -m appimage.ctl check

# Pin the toolchain, hash-pin every dependency, verify with a real build,
# then turn reproducible = true on in pyproject.toml - only once that build succeeds
python -m appimage.ctl enable-reproducible

# Every later build already enforces it - no flag needed
python -m appimage.ctl build
```

**Important:** these pins go stale - re-run periodically to pick up newer releases:

```sh
python -m appimage.ctl lock           # refresh dependency pins
python -m appimage.ctl update-tools   # refresh toolchain pins
```

See [Reproducible builds](https://appimage.readthedocs.io/en/latest/reproducible-builds.html) for the piecewise form and what each step does.


## Bundled interpreter access

Run the bundled Python directly, or switch to any other installed console script by name - handy for a REPL, a one-off script, or an app that bundles more than one command:

```sh
./myapp-x86_64.AppImage --python-interpreter              # interactive REPL
./myapp-x86_64.AppImage --python-interpreter script.py    # run a script
./myapp-x86_64.AppImage --python-list-entry-points        # list all entry points
./myapp-x86_64.AppImage --python-entry-point other:main   # switch entry point
```

**Example: want to start a Django application from an AppImage?** A Django project needs a WSGI server to actually serve traffic (`gunicorn`) and separate management commands (`django-admin migrate`, `createsuperuser`, ...) - all running the exact same code and dependencies:

```sh
./mysite-x86_64.AppImage --python-entry-point gunicorn mysite.wsgi:application
./mysite-x86_64.AppImage --python-entry-point django-admin migrate
./mysite-x86_64.AppImage --python-entry-point django-admin createsuperuser
```


## Virtual environments

Install a plugin without rebuilding the AppImage. [SSH-MITM](https://github.com/ssh-mitm/ssh-mitm), for example, discovers plugins as installed entry points - a venv built on the AppImage's own interpreter lets `pip install` add one, picked up automatically.

```sh
./myapp-x86_64.AppImage --python-interpreter -m venv ~/.venv/myapp
~/.venv/myapp/bin/pip install extra-package
~/.venv/myapp/bin/myapp
```

When launched through a venv symlink, the bundled `appimage` module activates the environment automatically.


## Configuration

All options go in `[tool.appimage]` inside `pyproject.toml`, and every key is optional. Lifecycle hooks, extra files, custom AppRun scripts, and environment variable injection are supported. See the [full documentation](https://appimage.readthedocs.io) for details.
