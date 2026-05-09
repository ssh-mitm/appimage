# appimage

**The `appimage` module packages Python applications as self-contained AppImages.**

It bundles a complete Python distribution from [astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone), manages entry points, and supports virtual environments that extend the bundled packages with additional ones — as used by [SSH-MITM](https://github.com/ssh-mitm/ssh-mitm), for example.

> ### The same Python that uv installs
>
> The bundled interpreter comes from [python-build-standalone](https://github.com/astral-sh/python-build-standalone) — the exact same source as `uv python install`.
> What you develop with locally is what gets shipped in the AppImage.


## Quick Start

**1. Install:**

```sh
pip install appimage
```

**2. Build:**

```sh
python -m appimage.build
```

That's it. `app`, `entry_point`, and `python` version are detected automatically from `[project]` in your `pyproject.toml`. The `.desktop` file and AppRun script are generated automatically. If no icon is found a built-in default icon is used — add `myapp.png` to your project root to use your own.

The AppImage is written to `dist/myapp-x86_64.AppImage` (or the matching architecture name).

> **Tip:** `appimage-build` is also available as a standalone command after installation.

### Check what was detected

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

### Write detected values to pyproject.toml

```sh
python -m appimage.build --init
```

Adds only the auto-detected fields that are not already set — so you can review and adjust them.


## Configuration

All options go in `[tool.appimage.build]` inside `pyproject.toml`. Every key is optional — omitted keys are resolved automatically from `[project]` metadata.

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

### Environment variables in AppRun

Extra environment variables are exported in the generated AppRun script:

```toml
[tool.appimage.build.env]
MY_PLUGIN_PATH = "/opt/plugins"
DEBUG = "0"
```

### Extra files

Copy additional files or directories into the AppDir:

```toml
[tool.appimage.build.extra_files]
"assets/" = "assets/"
"config.toml" = "config.toml"
```

Keys are source paths relative to the project root; values are destination paths relative to AppDir.

### Lifecycle hooks

Shell scripts called at specific points during the build.
The `APPDIR` environment variable is set to the AppDir path when the hook runs.

```toml
[tool.appimage.build.hooks]
post_install = "scripts/post_install.sh"   # after pip install, before assets are copied
pre_package  = "scripts/pre_package.sh"    # after all files are in place, before appimagetool
```

### Custom AppRun

When `apprun` is set, the file is copied as-is instead of generating one from the template.
This gives full control over environment setup and the launch command:

```toml
[tool.appimage.build]
apprun = "packaging/AppRun"
```


## CLI

### Build overrides

All key settings can be overridden on the command line:

```sh
python -m appimage.build --python 3.13
python -m appimage.build --python-date 20260211   # reproducible build
python -m appimage.build --package ".[production]" --package extra-lib
python -m appimage.build --project-dir /path/to/project
```

### Integration with hatch

```toml
[tool.hatch.envs.appimage]
dependencies = ["appimage"]

[tool.hatch.envs.appimage.scripts]
build = ["python -m appimage.build"]
```

```sh
hatch run appimage:build
```


## Runtime

All `--python-*` options are handled by the `appimage` module before your application sees any arguments.

| Option | Description |
|---|---|
| `--python-help` | Show available `--python-*` options and exit. |
| `--python-main ENTRY_POINT` | Set the default entry point to start. Used in AppRun. |
| `--python-interpreter` | Start the bundled Python interpreter interactively. |
| `--python-entry-point EP` | Run a specific console script or `module:function` entry point. |
| `--python-list-entry-points` | List all available console script entry points and exit. |
| `--python-appimage-debug` | Print startup debug information to stderr. |

Any argument not starting with `--python-` is passed through unchanged to the application.

### Accessing the bundled Python

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

### Virtual environments

The `--python-interpreter -m venv` option creates a virtual environment whose `python3` symlink points to the AppImage itself.
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

Supported venv options:

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

### Extracted AppImages (no FUSE)

On systems where FUSE is not available (some containers, CI environments), an AppImage can be extracted and run directly:

```sh
./myapp-x86_64.AppImage --appimage-extract
./squashfs-root/AppRun
```

The AppRun script detects whether a `squashfs-root` directory exists next to the original AppImage file and uses it automatically as the application directory.


## Development

### Prerequisites

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
