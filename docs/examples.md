# Examples

A minimal working example lives in [`examples/myapp/`](https://github.com/ssh-mitm/appimage/tree/main/examples/myapp) in the repository.

## Minimal project

The example shows the smallest possible project structure that `appimagectl` can package:

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
description = "Minimal example project for appimagectl"
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

No `[tool.appimage]` section is needed — `app`, `entry_point`, and `python` are all detected automatically from the `[project]` table.

## Build the example

```sh
cd examples/myapp
python -m appimage.ctl
```

The AppImage is written to `examples/myapp/dist/myapp-x86_64.AppImage`.

## Offline / CI build

When building in a network-restricted environment or sharing a cache across builds, point the tool to local copies of appimagetool, the runtime file, and the Python distribution — all three, since appimagetool alone still tries to fetch the runtime file live over the network unless one is supplied:

```sh
python -m appimage.ctl \
  --appimagetool /opt/appimagetool-x86_64.AppImage \
  --appimagetool-sha256 3f9a1c...  \
  --runtime-file /opt/runtime-x86_64 \
  --runtime-sha256 1cc49bc... \
  --python-archive /shared/cache/python.tar.gz \
  --python-sha256 78c7cb...
```

The same paths can be set permanently in `pyproject.toml`:

```toml
[tool.appimage]
appimagetool = "/opt/appimagetool-x86_64.AppImage"
appimagetool_sha256 = "3f9a1c..."
runtime_file = "/opt/runtime-x86_64"
runtime_sha256 = "1cc49bc..."
python_archive = "/shared/cache/python.tar.gz"
python_sha256 = "78c7cb..."
```

The `*_sha256` fields are optional but recommended for offline/CI builds: verifying a
local copy never touches the network, so pinning them turns "trust whatever file happens
to be at this path" into a build that fails loudly if that file is ever swapped out —
without changing the fully offline nature of this workflow. Add `verify_downloads =
true` to make an unpinned local path a hard error too, instead of just a warning.

**Resolution order for appimagetool:**

1. Path from `--appimagetool` / `appimagetool` config key
2. Cached binary in `build/appimagetool-<arch>.AppImage`
3. Downloaded from GitHub

`PATH` is never searched — see [Classic appimagetool
detected](reproducible-builds.md#classic-appimagetool-detected) for why.
Whichever binary is resolved is verified against `appimagetool_sha256` when set,
regardless of which step it came from. A fresh download (step 3) is additionally
auto-verified against GitHub's published digest even when `appimagetool_sha256` is
unset.

**Resolution order for the runtime file:**

1. Path from `--runtime-file` / `runtime_file` config key
2. Cached file in `build/runtime-<arch>`
3. Downloaded from GitHub

Same verification behavior as appimagetool, via `runtime_sha256`. Unlike appimagetool,
there is no `PATH` lookup step — the runtime file isn't something you'd have installed
system-wide.

**Resolution order for the Python archive:**

1. Path from `--python-archive` / `python_archive` config key
2. Cached archive in `build/python.tar.gz`
3. Downloaded from python-build-standalone

A local archive or cached tarball (steps 1–2) is only verified when `python_sha256` is
explicitly set. A fresh download (step 3) is always verified — against `python_sha256`
if set, otherwise against the digest GitHub already publishes for the release asset.
