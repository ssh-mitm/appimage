# Runtime

All `--python-*` options are handled by the `appimage` module before your application sees any arguments.

## Runtime options

| Option | Description |
|---|---|
| `--python-help` | Show available `--python-*` options and exit. |
| `--python-main ENTRY_POINT` | Set the default entry point to start. Used in AppRun. |
| `--python-interpreter` | Start the bundled Python interpreter interactively. |
| `--python-entry-point EP` | Run a specific console script or `module:function` entry point. |
| `--python-list-entry-points` | List all available console script entry points and exit. |
| `--python-appimage-debug` | Print startup debug information to stderr. |

Any argument not starting with `--python-` is passed through unchanged to the application.

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

## Virtual environments

The `--python-interpreter -m venv` option creates a virtual environment whose `python3` symlink points to the AppImage itself. This makes all packages bundled in the AppImage available in the virtual environment, and allows installing additional packages on top:

```sh
# Create the virtual environment
./myapp-x86_64.AppImage --python-interpreter -m venv ~/.venv/myapp

# Install additional packages
~/.venv/myapp/bin/pip install extra-package

# Run the application via the virtual environment
~/.venv/myapp/bin/myapp
```

When invoked through a virtual environment symlink, the `appimage` module automatically activates the correct environment so that packages installed into it take precedence.

### Supported venv options

| Option | Description |
|---|---|
| `--clear` | Delete the environment directory before creation if it already exists. |
| `--upgrade` | Update an existing venv after the AppImage has been replaced with a newer version. |
| `--prompt PROMPT` | Set an alternative shell prompt prefix for the environment. |
| `--without-scm-ignore-files` | Skip creating `.gitignore` in the venv (Python ≥ 3.13 only). |

> **Note:** `--system-site-packages` has no effect — the AppImage's bundled packages are always accessible regardless, because Python finds them via its compiled-in `sys.prefix` (`APPDIR/python/`), not through the venv's `pyvenv.cfg`. `--copies`, `--upgrade-deps`, `--without-pip`, and `--symlinks` are also not supported.

## Extracted AppImages (no FUSE)

On systems where FUSE is not available (some containers, CI environments), an AppImage can be extracted and run directly:

```sh
./myapp-x86_64.AppImage --appimage-extract
./squashfs-root/AppRun
```

The AppRun script detects whether a `squashfs-root` directory exists next to the original AppImage file and uses it automatically as the application directory.
