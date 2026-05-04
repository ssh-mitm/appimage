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

To find the latest release tag via the GitHub API:

```sh
curl -s "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" \
  | grep '"tag_name"' | cut -d'"' -f4
```

> **Tip:** Pin the release date and Python version in your build script for reproducible builds.
> Only update deliberately after testing.

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

PYTHON_VERSION="3.11.14"
RELEASE_DATE="20260211"
PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${RELEASE_DATE}/cpython-${PYTHON_VERSION}+${RELEASE_DATE}-${PBS_ARCH}-unknown-linux-gnu-install_only_stripped.tar.gz"
APPIMAGETOOL_URL="https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-${ARCH}.AppImage"

# Prepare AppDir
rm -rf build/AppDir
mkdir -p build

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
| `--python-venv DIR [DIR ...]` | Create a virtual environment pointing to the AppImage's Python. |
| `--python-entry-point EP` | Run a specific console script or `module:function` entry point. |

Any argument not starting with `--python-` is passed through unchanged to the application.


## Virtual environments

The `--python-venv` option creates a virtual environment whose `python3` symlink points to the AppImage itself.
This makes all packages bundled in the AppImage available in the virtual environment, and allows installing additional packages on top:

```sh
# Create the virtual environment
./myapp-x86_64.AppImage --python-venv ~/.venv/myapp

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
