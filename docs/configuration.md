# Configuration

All options go in `[tool.appimage]` inside `pyproject.toml`. Every key is optional — omitted keys are resolved automatically from `[project]` metadata.

## Build options

| Key | Default | Description |
|---|---|---|
| `app` | `project.name` | Application name — used as the AppImage filename prefix. |
| `entry_point` | `project.scripts` | Console script entry point launched by default. |
| `python` | `requires-python` | Python minor version to bundle, e.g. `"3.11"`. |
| `python_date` | *(latest)* | python-build-standalone release date for reproducible builds (e.g. `"20260211"`). |
| `extras` | `[]` | Extras to install from the current package, e.g. `["production"]` → `pip install ".[production]"`. |
| `packages` | `[]` | Additional pip install targets beyond the current package. Resolved by whatever pip picks up from the environment — see [Private package indexes](reproducible-builds.md#private-package-indexes-artifactory-nexus-devpi-) for pointing this at an internal mirror. |
| `icon` | auto-detected, then built-in default | Path to the icon file, relative to the project root. |
| `desktop` | auto-detected, then generated | Path to the `.desktop` file, relative to the project root. |
| `apprun` | *(generated)* | Path to a custom AppRun script. |
| `build_dir` | `"build"` | Directory for intermediate artefacts (Python tarball, appimagetool, runtime file). |
| `dist_dir` | `"dist"` | Directory where the finished AppImage is written. |
| `update_info` | — | Update information string passed to appimagetool via `-u` (e.g. for zsync). When unset and an unambiguous GitHub repo can be identified from `[project.urls]`, a `gh-releases-zsync` value is suggested via `check` and written by `init` — never applied to a live build on its own. |
| `appimagetool` | — | Path to a local appimagetool binary. When omitted, the build cache is checked first, then a download — unlike most other resolved paths in this table, `PATH` is deliberately never searched (see [Classic appimagetool detected](reproducible-builds.md#classic-appimagetool-detected)). |
| `appimagetool_version` | — | Informational label recording which appimagetool build `appimagetool_sha256` corresponds to. Written automatically by `init`. |
| `appimagetool_sha256` | — | Expected sha256 of the appimagetool binary. When set, verified against whichever binary is resolved (explicit path, `PATH`, build cache, or download) — a mismatch aborts the build. A fresh download is auto-verified against GitHub's published digest even when unset; only a config-path/`PATH`/cache resolution with no pin falls back to an unverified warning logging its actual hash. |
| `python_archive` | — | Path to a local python-build-standalone tarball. When omitted, the build cache is checked first, then a download. |
| `python_sha256` | — | Expected sha256 of the python-build-standalone tarball. Fresh downloads are already verified against the digest GitHub publishes per release, even without this set; set explicitly to also verify a local `python_archive` or a cached tarball. |
| `python_dir` | — | Path to an already-extracted Python distribution directory, copied into `AppDir/python` directly instead of extracting a tarball. Config-only — no CLI flag, since setting it is meant to be a deliberate, committed decision. Used exactly as given, with no hash verification (there's no single archive file left to hash) and no interaction with `python_archive`/`python_sha256` — set at most one of `python_dir`/`python_archive`. Meant for a directory whose *provenance* was already verified elsewhere (e.g. `uv python install`, or a prior `python_archive` + `python_sha256` run) and is now trusted as a fixed input in its own right — the same path yields the same bytes every time, the same way pointing `appimagetool`/`runtime_file` at a local path already works elsewhere in this tool. `reproducible` accepts `python_dir` in place of `python_date` for exactly this reason, but `check`'s reproducibility checklist marks it as *trusted, unverified* rather than showing it identically to a hash-checked pin. |
| `runtime_file` | — | Path to a local AppImage runtime ELF stub, passed to appimagetool as `--runtime-file`. When omitted, the build cache is checked first, then a download — pre-fetching it this way avoids appimagetool's own live, unverified download at packaging time. |
| `runtime_sha256` | — | Expected sha256 of the runtime file, verified the same way as `appimagetool_sha256`. |
| `verify_downloads` | `false` | Abort the build instead of warning whenever appimagetool, the runtime file, or the Python archive would otherwise be used unverified. `check` (and, via the same code path, `build`/`build-appdir`) already predicts this without downloading anything: a fresh download is always auto-verified regardless of any pin, so only an explicit config path or an existing build-cache hit with no matching `*_sha256` pin is flagged — surfacing as an early error instead of only failing after a full install. |
| `require_zsyncmake` | `false` | Abort the build instead of warning when `update_info` is set but appimagetool didn't actually produce a `.zsync` file — checked after packaging, against the real output. appimagetool bundles its own `zsyncmake` (see [Zsync and the build host's PATH](reproducible-builds.md#zsync-and-the-build-host-path)), so this normally succeeds regardless of the build host; it only fires for an unusually minimal appimagetool build. No effect when `update_info` is empty. |
| `pylock` | — | Path to a hash-pinned `pylock.toml`, relative to the project root. When set, third-party dependencies are installed with `pip install --require-hashes` instead of a live, unverified resolution. Generate it with `lock` — see [Verified dependencies](reproducible-builds.md#verified-dependencies). |
| `require_pylock` | `false` | Abort the build instead of warning when `pylock` is not set. |
| `build_pylock` | — | Path to a hash-pinned pylock-format file constraining the packaged project's own `[build-system].requires` (e.g. `hatchling`, `setuptools`, `uv_build`). When set, converted to a classic hash-pinned constraints file and passed as `pip install --build-constraint`, so the isolated build environment pip creates for installing the project's own source is hash-verified too, instead of resolved live — the build stays isolated rather than reusing the main interpreter. Generate it with `lock`, alongside `pylock.toml` — see [Verified build backend](reproducible-builds.md#verified-build-backend). |
| `require_build_pylock` | `false` | Abort the build instead of warning when `build_pylock` is not set. |
| `reproducible` | `false` | Shortcut for a build that's reproducible across machines and over time: implies `verify_downloads` and `require_zsyncmake`, and additionally requires `python_date`, `appimagetool_sha256`, and `runtime_sha256` to already be set — resolving those three fresh on every build is exactly what defeats cross-machine reproducibility. Run `init` first to write them; `reproducible` itself never resolves or writes values. Deliberately independent of `pylock`/`require_pylock` — dependency hash-pinning and byte-identical output are separate concerns; opt into both explicitly. |

## Coming from python-appimage or a custom build script

Most of `[tool.appimage]` never needs to be written by hand when switching from another Python-to-AppImage setup — `app`, `entry_point`, `python`, `icon`, and `desktop` are all auto-detected from `[project]` metadata and standard file locations (see the table above), the same way they would be for a project that never had an AppImage build before.

For example, [ssh-mitm](https://github.com/ssh-mitm/ssh-mitm) used to build its AppImage with a hand-rolled `appimage/build.sh` (hardcoding a python-build-standalone download URL, an appimagetool download URL, and a `pip install` invocation) plus a separate `appimage/AppRun` and `appimage/ssh-mitm.desktop`. Switching to `appimage` deleted all three files; the entire addition to `pyproject.toml` was:

```toml
[tool.appimage]
extras = ["production"]
python_date = "20260211"
update_info = "gh-releases-zsync|ssh-mitm|ssh-mitm|latest|ssh-mitm-x86_64.AppImage.zsync"
```

- `extras` is whatever `[project.optional-dependencies]` group the old install command already named.
- `python_date` is optional — only needed to opt into the pinned reproducibility this tool adds that a python-appimage/custom-script setup never had.
- `update_info` is often suggested automatically (see the table above) when `[project.urls]` already points at the project's GitHub repo, as it did here.

A python-appimage "recipe" folder (`requirements.txt`, a `.desktop` file, an icon, optionally `entrypoint.*`) maps onto the same options: `requirements.txt` entries become `extras`/`packages`, and the `.desktop`/icon files are picked up automatically once placed at one of the auto-detected locations (`icon`/`desktop` in the table above) or referenced explicitly.

## Environment variables in AppRun

Extra environment variables are exported in the generated AppRun script:

```toml
[tool.appimage.env]
MY_PLUGIN_PATH = "/opt/plugins"
DEBUG = "0"
```

## Extra files

Copy additional files or directories into the AppDir:

```toml
[tool.appimage.extra_files]
"assets/" = "assets/"
"config.toml" = "config.toml"
```

Keys are source paths relative to the project root; values are destination paths relative to AppDir.

## Lifecycle hooks

Shell scripts called at specific points during the build. The `APPDIR` environment variable is set to the AppDir path when the hook runs.

```toml
[tool.appimage.hooks]
post_install = "scripts/post_install.sh"   # after pip install, before assets are copied
pre_package  = "scripts/pre_package.sh"    # after all files are in place, before appimagetool
```

Installed packages are byte-compiled (hash-based, reproducible `.pyc`) right
after `pre_package` runs and before appimagetool packages the AppDir, so a
hook that edits an installed package's source is still reflected in the
compiled bytecode.

## Custom AppRun

When `apprun` is set, the file is copied as-is instead of generating one from the template. This gives full control over environment setup and the launch command:

```toml
[tool.appimage]
apprun = "packaging/AppRun"
```
