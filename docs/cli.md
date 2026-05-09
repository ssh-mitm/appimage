# CLI Reference

## Build command

```sh
python -m appimage.build [OPTIONS]
appimage-build [OPTIONS]
```

### Options

| Option | Description |
|---|---|
| `--check` | Show detected build configuration and exit without building. |
| `--init` | Write auto-detected values to `[tool.appimage.build]` in `pyproject.toml` (only missing keys). |
| `--app NAME` | Override the application name. |
| `--entry-point EP` | Override the console script entry point. |
| `--python VERSION` | Override the Python version to bundle (e.g. `3.13`). |
| `--python-date DATE` | Override the python-build-standalone release date for reproducible builds (e.g. `20260211`). |
| `--extras EXTRA` | Override extras to install (e.g. `production`). May be repeated. |
| `--package TARGET` | Additional pip install target. May be repeated. |
| `--project-dir PATH` | Path to the project root (default: current directory). |

### Examples

```sh
# Build with a specific Python version
python -m appimage.build --python 3.13

# Reproducible build pinned to a specific release date
python -m appimage.build --python-date 20260211

# Override app name and entry point
python -m appimage.build --app myapp --entry-point myapp.cli:main

# Install extras and additional packages
python -m appimage.build --extras production --package extra-lib

# Build from a different project directory
python -m appimage.build --project-dir /path/to/project
```
