# appimage

## Overview

**The `appimage` module simplifies starting Python applications within an AppImage using AppRun.**

Many AppImages allow only the execution of a single command, which can be limiting for more complex applications.
This module manages entry points, virtual environments, and interpreter access, making it easy to package Python applications as self-contained AppImages.

The [SSH-MITM](https://github.com/ssh-mitm/ssh-mitm) project uses this module to package its application and enable plugin development against the bundled Python environment.

> **Note:** This module is invoked by the AppRun script of an AppImage and is not intended to be executed directly.

## How it works

An AppImage built with this module bundles a complete Python distribution from [astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone).
The AppRun script sets up the environment and delegates to `python -m appimage`, which then selects and starts the correct entry point.

> **Already using uv?** Then you already rely on python-build-standalone — it is the exact same source uv uses internally to install and manage Python versions.
> The Python interpreter bundled in your AppImage comes from the same place as `uv python install`.

The result is a single executable file that:
- runs the application by default
- exposes the bundled Python interpreter
- can create virtual environments that extend the AppImage's packages with additional ones


## AppDir structure

```
AppDir/
├── AppRun              ← bash entry point (executable)
├── myapp.desktop       ← desktop integration file
├── myapp.png           ← application icon
└── python/             ← extracted python-build-standalone
    ├── bin/
    │   └── python3
    └── lib/
        └── python3.x/
            └── site-packages/
                ├── appimage/   ← this module
                └── myapp/      ← your application
```


## Building an AppImage

### 1. Choose a Python release

Python distributions are published at [github.com/astral-sh/python-build-standalone/releases](https://github.com/astral-sh/python-build-standalone/releases).

Use the `install_only_stripped` variant — it contains everything needed to run Python applications and produces the smallest AppImage.

The URL follows this pattern:

```
https://github.com/astral-sh/python-build-standalone/releases/download/{date}/cpython-{python_version}+{date}-{arch}-unknown-linux-gnu-install_only_stripped.tar.gz
```

Supported architectures:

| `uname -m` | URL arch token |
|---|---|
| `x86_64` | `x86_64` |
| `aarch64` | `aarch64` |
| `armv7l` | `armv7` |

To resolve the download URL for a specific Python minor version and the current architecture, run:

```sh
PYTHON_MINOR="3.11"  # change to 3.12, 3.13, etc. as needed
RELEASE_DATE=$(curl -s "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" \
  | grep '"tag_name"' | cut -d'"' -f4)
curl -s "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/${RELEASE_DATE}" \
  | grep '"browser_download_url"' \
  | grep "cpython-${PYTHON_MINOR}\." \
  | grep "$(uname -m)-unknown-linux-gnu-install_only_stripped" \
  | grep -v "freethreaded" \
  | cut -d'"' -f4
```

> **Tip:** Set only `PYTHON_MINOR` to switch between Python series (3.11, 3.12, 3.13 …).
> Pin `RELEASE_DATE` explicitly in the build script for reproducible builds; omit it to always use the latest release.

> **Note:** [uv](https://docs.astral.sh/uv/) uses the same python-build-standalone distributions under the hood.
> If uv is available on your build machine, you can let it handle the download instead — see [Build script](#4-build-script) for both variants.

### 2. AppRun script

Place this file at `AppDir/AppRun` and make it executable (`chmod +x`):

```bash
#!/bin/bash

set -e

# If the AppImage was extracted next to a squashfs-root directory, use that.
if [ -n "$APPIMAGE" ]; then
    appimage_path=$(dirname "$APPIMAGE")
    if [ -d "$appimage_path/squashfs-root" ]; then
        export APPDIR="$appimage_path/squashfs-root"
    fi
fi

if [ -z "$APPDIR" ]; then
    export APPDIR=$(dirname $(readlink -f "$0"))
fi

exec "$APPDIR/python/bin/python3" -P -m appimage --python-main myapp "$@"
```

Replace `myapp` with the console script entry point of your application.

The `-P` flag prevents Python from loading user-level site packages, ensuring the AppImage uses only its own bundled packages unless a virtual environment is active.

### 3. Desktop file

AppImageKit requires a `.desktop` file at the root of the AppDir:

```ini
[Desktop Entry]
Type=Application
Name=My Application
Icon=myapp
Categories=Utility;
Terminal=true
```

An icon file (`myapp.png`) must also be present at the root of the AppDir.

### 4. Build script

```bash
#!/bin/bash

set -e

ARCH=$(uname -m)

# Map uname architecture to python-build-standalone naming
PBS_ARCH="$ARCH"
if [ "$ARCH" = "armv7l" ]; then
    PBS_ARCH="armv7"
fi

# Set the Python minor version — change to 3.12, 3.13, etc. as needed
PYTHON_MINOR="3.11"

# Resolve the latest release date and matching download URL automatically
RELEASE_DATE=$(curl -s "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" \
  | grep '"tag_name"' | cut -d'"' -f4)
PYTHON_URL=$(curl -s "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/${RELEASE_DATE}" \
  | grep '"browser_download_url"' \
  | grep "cpython-${PYTHON_MINOR}\." \
  | grep "${PBS_ARCH}-unknown-linux-gnu-install_only_stripped" \
  | grep -v "freethreaded" \
  | head -1 \
  | cut -d'"' -f4)

if [ -z "$PYTHON_URL" ]; then
    echo "Error: no Python ${PYTHON_MINOR} asset found for ${PBS_ARCH} in release ${RELEASE_DATE}" >&2
    exit 1
fi

APPIMAGETOOL_URL="https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-${ARCH}.AppImage"

# Prepare AppDir
rm -rf build/AppDir
mkdir -p build/AppDir

# Download and extract Python (cached)
if [ ! -f build/python.tar.gz ]; then
    curl -L -o build/python.tar.gz "$PYTHON_URL"
fi
tar -xf build/python.tar.gz -C build/AppDir

# Install your application and the appimage module
build/AppDir/python/bin/python3 -m pip install appimage myapp

# Copy AppImage assets
cp AppRun build/AppDir/
cp myapp.desktop myapp.png build/AppDir/

# Download appimagetool (cached)
if [ ! -x build/appimagetool ]; then
    curl -L -o build/appimagetool "$APPIMAGETOOL_URL"
    chmod +x build/appimagetool
fi

mkdir -p dist
build/appimagetool build/AppDir "dist/myapp-${ARCH}.AppImage"
```

Alternatively, if [uv](https://docs.astral.sh/uv/) is available, it can replace the Python download steps — uv uses the same python-build-standalone distributions internally:

```bash
#!/bin/bash

set -e

ARCH=$(uname -m)

# Set the Python minor version — change to 3.12, 3.13, etc. as needed
PYTHON_MINOR="3.11"

# Install Python via uv (download and caching handled automatically)
uv python install "$PYTHON_MINOR"
UV_PYTHON_DIR=$(dirname "$(dirname "$(uv python find "$PYTHON_MINOR")")")

APPIMAGETOOL_URL="https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-${ARCH}.AppImage"

# Prepare AppDir
rm -rf build/AppDir
mkdir -p build/AppDir/python

# Copy Python from uv cache into AppDir
cp -r "$UV_PYTHON_DIR/." build/AppDir/python/

# Install your application and the appimage module
build/AppDir/python/bin/python3 -m pip install appimage myapp

# Copy AppImage assets
cp AppRun build/AppDir/
cp myapp.desktop myapp.png build/AppDir/

# Download appimagetool (cached)
if [ ! -x build/appimagetool ]; then
    curl -L -o build/appimagetool "$APPIMAGETOOL_URL"
    chmod +x build/appimagetool
fi

mkdir -p dist
build/appimagetool build/AppDir "dist/myapp-${ARCH}.AppImage"
```

### 5. Run the build

```sh
chmod +x build.sh
./build.sh
```

The resulting AppImage is written to `dist/myapp-x86_64.AppImage` (or the corresponding architecture name).


## Extracted AppImages (squashfs-root)

On systems where FUSE is not available (some containers, CI environments), an AppImage can be extracted and run directly:

```sh
./myapp-x86_64.AppImage --appimage-extract
./squashfs-root/AppRun
```

The AppRun script detects whether a `squashfs-root` directory exists next to the original AppImage file and uses it automatically as the application directory.
This means you can also distribute an extracted AppImage alongside the `.AppImage` file for environments that cannot run AppImages natively.


## Command-line options

All `--python-*` options are handled by the `appimage` module before your application sees any arguments.

| Option | Description |
|---|---|
| `--python-help` | Show available `--python-*` options and exit. |
| `--python-main ENTRY_POINT` | Set the default entry point to start. Used in AppRun. |
| `--python-interpreter` | Start the bundled Python interpreter interactively. |
| `--python-entry-point EP` | Run a specific console script or `module:function` entry point. |

Virtual environments are created via the `python -m venv` interface:

```sh
./myapp-x86_64.AppImage --python-interpreter -m venv ENV_DIR [options]
```

Supported options:

| Option | Description |
|---|---|
| `--clear` | Delete the environment directory before creation if it already exists. |
| `--upgrade` | Update an existing venv after the AppImage has been replaced with a newer version. |
| `--prompt PROMPT` | Set an alternative shell prompt prefix for the environment. |
| `--without-scm-ignore-files` | Skip creating `.gitignore` in the venv (Python ≥ 3.13 only). |

> **Note:** `--system-site-packages` has no effect and is not exposed — the AppImage's bundled
> packages are always accessible regardless, because Python finds them via its compiled-in
> `sys.prefix` (`APPDIR/python/`), not through the venv's `pyvenv.cfg`.
> `--copies`, `--upgrade-deps`, `--without-pip`, and `--symlinks` are also not supported.

Any argument not starting with `--python-` is passed through unchanged to the application.


## Virtual environments

The `--python-venv` option creates a virtual environment whose `python3` symlink points to the AppImage itself.
This makes all packages bundled in the AppImage available in the virtual environment, and allows installing additional packages on top:

```sh
# Create the virtual environment
./myapp-x86_64.AppImage --python-interpreter -m venv ~/.venv/myapp

# Install additional packages
~/.venv/myapp/bin/pip install extra-package

# Run the application via the virtual environment
~/.venv/myapp/bin/myapp
```

When invoked through a virtual environment symlink, the `appimage` module automatically activates the correct environment so that packages installed into it take precedence.


## Development

### Prerequisites

Install the required Python versions and hatch:

```sh
uv python install 3.11 3.12 3.13 3.14
pip install hatch
```

### Running tests

```sh
# All supported Python versions
hatch test --all

# Single version
hatch test --python 3.13

# Specific test
hatch test -- -k test_appstarter
```

### Linting

```sh
# All supported Python versions
hatch env run -e lint check

# Single version
hatch run +py=3.13 lint:check
```


## Accessing the bundled Python

```sh
# Interactive interpreter
./myapp-x86_64.AppImage --python-interpreter

# Run a script
./myapp-x86_64.AppImage --python-interpreter script.py

# Run a module (e.g. pip)
./myapp-x86_64.AppImage --python-interpreter -m pip list

# Run a specific entry point
./myapp-x86_64.AppImage --python-entry-point myapp.cli:main
```
