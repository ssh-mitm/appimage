# Examples

A minimal working example lives in [`examples/myapp/`](https://github.com/ssh-mitm/appimage/tree/main/examples/myapp) in the repository.

## Minimal project

The example shows the smallest possible project structure that `appimage-build` can package:

```
examples/myapp/
├── pyproject.toml
└── myapp/
    └── __init__.py
```

**`pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "myapp"
version = "0.1.0"
description = "Minimal example project for appimage-build"
requires-python = ">=3.11"

[project.scripts]
myapp = "myapp:main"

[tool.hatch.build.targets.wheel]
packages = ["myapp"]
```

**`myapp/__init__.py`**

```python
def main() -> None:
    print("Hello from AppImage!")
```

No `[tool.appimage.build]` section is needed — `app`, `entry_point`, and `python` are all detected automatically from the `[project]` table.

## Build the example

```sh
cd examples/myapp
python -m appimage.build
```

The AppImage is written to `examples/myapp/dist/myapp-x86_64.AppImage`.

## Offline / CI build

When building in a network-restricted environment or sharing a cache across builds, point the tool to local copies of appimagetool and the Python distribution:

```sh
python -m appimage.build \
  --appimagetool /opt/appimagetool-x86_64.AppImage \
  --python-archive /shared/cache/python.tar.gz
```

The same paths can be set permanently in `pyproject.toml`:

```toml
[tool.appimage.build]
appimagetool = "/opt/appimagetool-x86_64.AppImage"
python_archive = "/shared/cache/python.tar.gz"
```

**Resolution order for appimagetool:**

1. Path from `--appimagetool` / `appimagetool` config key
2. `appimagetool` found on `PATH`
3. Cached binary in `build/appimagetool-<arch>.AppImage`
4. Downloaded from GitHub

**Resolution order for the Python archive:**

1. Path from `--python-archive` / `python_archive` config key
2. Cached archive in `build/python.tar.gz`
3. Downloaded from python-build-standalone
