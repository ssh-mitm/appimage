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
| `--python VERSION` | Override the Python version to bundle (e.g. `3.13`). |
| `--python-date DATE` | Override the python-build-standalone release date for reproducible builds (e.g. `20260211`). |
| `--package TARGET` | Override pip install target(s). May be repeated. |
| `--project-dir PATH` | Path to the project root (default: current directory). |

### Examples

```sh
# Build with a specific Python version
python -m appimage.build --python 3.13

# Reproducible build pinned to a specific release date
python -m appimage.build --python-date 20260211

# Install additional packages into the AppImage
python -m appimage.build --package ".[production]" --package extra-lib

# Build from a different project directory
python -m appimage.build --project-dir /path/to/project
```
