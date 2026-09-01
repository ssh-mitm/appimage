# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]

### Added

- Default appimagetool source switched to [`AppImage/appimagetool`](https://github.com/AppImage/appimagetool) for deterministic `mksquashfs` output
- `appimagetool_sha256`/`appimagetool_version`, `runtime_file`/`runtime_sha256`, `python_sha256` config keys (and matching CLI options) to verify build dependencies
- `verify_downloads` config key / `--verify-downloads` CLI flag to abort the build on unverified downloads
- `require_zsyncmake` config key / `--require-zsyncmake` CLI flag to abort the build when `zsyncmake` is missing
- `reproducible` config key / `--reproducible` CLI flag: shortcut requiring all reproducibility pins to be set
- `pylock`/`require_pylock` config keys and CLI options: hash-pinned dependency installs via `pip install --require-hashes`
- `build_pylock`/`require_build_pylock` config keys and CLI options: hash-pinned build-backend installs
- `lock` command: generates/refreshes `pylock.toml` and `build_pylock` via `pip lock`; `--uploaded-prior-to PnD` cooldown window
- `init` now also resolves and pins appimagetool and the runtime file
- `check`/build now print a reproducibility checklist and dependency-verification summary
- `update_info` auto-detection from `[project.urls]`
- Bundled `appimage` runtime module is hash-verified against PyPI's digest by default, independent of `pylock`
- `check`'s reproducibility checklist gains ✓/✗ marks and a ready-count header
- `enable-reproducible` command: pins the toolchain, locks dependencies, verifies with a real build, and only then turns on `reproducible`
- The build now aborts if the resolved appimagetool looks like the classic, unmaintained AppImageKit build (detected via embedded build-path strings and/or its `--version` banner) instead of the current `AppImage/appimagetool` default — `appimagetool_sha256` alone can't catch this, since it only proves the same (known non-deterministic) file is used every time, not that it's the right one. See [Classic appimagetool detected](https://appimage.readthedocs.io/en/latest/reproducible-builds.html#classic-appimagetool-detected) for the fix.
- Console-script shims (`AppDir/python/bin/<entry-point>`, `pip`, etc.) that leaked the build machine's absolute path are now relocated in place instead of deleted — their shebang rewritten to find the bundled interpreter relative to their own location (the same self-locating trick python-build-standalone's own bundled `pip` already uses), confirmed by hand to still run correctly after moving the whole AppDir. Falls back to the previous delete-outright behavior for anything that doesn't match a recognized pip/distlib shim format byte-for-byte — deliberately narrow, since a general-purpose version of this exact idea (virtualenv's old `--relocatable`) was eventually removed upstream for being unreliable.

### Changed

- **Breaking:** `--check`/`--init`/`--lock` are now subcommands (`check`/`init`/`lock`); building stays the default with no subcommand
- **Breaking:** console script renamed `appimage-build` → `appimagectl`; module renamed `appimage.build` → `appimage.ctl`
- **Breaking:** config table renamed `[tool.appimage.build]` → `[tool.appimage]`
- **Breaking:** appimagetool resolution no longer searches `PATH` — explicit `appimagetool` config path, then the build cache, then a download, matching every other resolved external input (the bundled Python, the runtime stub). It was the only one that searched `PATH`, and in practice that's exactly how a stray classic AppImageKit build got silently picked up on a real machine (see the "Classic appimagetool detected" entry below). Set `appimagetool` explicitly if you relied on a PATH-found binary.
- Installed packages are byte-compiled via `pip install --no-compile` + `compileall --invalidation-mode unchecked-hash`
- `SOURCE_DATE_EPOCH` is now applied consistently: AppDir file/directory mtimes are normalized to it, and it's passed into appimagetool's own process environment

### Fixed

- `write_config()`/`init` no longer crashes with `ValueError` when a project has no icon file
- appimagetool/runtime-file download URLs now map `armv7l` to `armhf` correctly
- `pylock` generation no longer drops the `appimage` runtime module or `packages` entries
- `init`/`write_config()` no longer writes a wrong `entry_point` guess into `pyproject.toml`
- `init`/`write_config()` now resolves and writes `python_date` and `python_sha256`
- `_compile_pyc` now forces recompilation, fixing a reproducibility break from stale `.pyc` timestamps
- `build_pylock` is now applied via `pip install --build-constraint` instead of `--no-build-isolation`, so the build backend no longer stays installed in the shipped AppImage
- The build machine's own absolute path no longer leaks into the AppImage (compiled bytecode, stray stdlib `.pyc`, `direct_url.json`, console-script shims)
- Every `pip install`/`pip lock` subprocess now sets `PYTHONNOUSERSITE=1`. Without it, pip additionally resolved against the *build user's* own `~/.local/lib/pythonX.Y/site-packages` (PEP 370) — if a requirement happened to already be satisfied there, pip silently skipped installing it into the AppDir at all ("Requirement already satisfied" instead of "Collecting"). The built AppImage then failed at runtime with `ModuleNotFoundError` on any host where the build user's home directory didn't carry the same leftover package, while the build itself reported success — both a correctness bug and a reproducibility break, since the result depended on unpinned build-host state rather than only the pinned inputs.
- `post_install`/`pre_package` hooks now get the same isolated environment as every install subprocess (`PYTHONNOUSERSITE`/`PYTHONDONTWRITEBYTECODE`). The hook mechanism predates this project's reproducibility work and was never revisited when it landed — anything a hook does through the bundled interpreter (its documented purpose: editing installed packages between build steps) was exposed to the same PEP 370 leak above.
- `require_zsyncmake`/the `update_info` "no `.zsync` file will be generated" warning no longer check whether `zsyncmake` is on the *build host's* `PATH` — checked instead after packaging, against whether appimagetool actually produced the `.zsync` file. The old check answered the wrong question: appimagetool bundles its own `zsyncmake` and its `AppRun` puts its own `usr/bin` first on `PATH`, so `.zsync` generation was already host-independent — the check just wasn't, giving a different (and sometimes wrongly negative, aborting under `require_zsyncmake`/`--reproducible`) answer depending on what happened to be installed on whichever machine ran the build. See [Zsync and the build host PATH](https://appimage.readthedocs.io/en/latest/reproducible-builds.html#zsync-and-the-build-host-path).
- The `pylock.toml` dependency install no longer passes `--no-deps`. Since pip's hash-checking mode (already triggered automatically by any `--hash` in the file) rejects a resolved candidate with no matching hash, leaving normal dependency resolution on means an incomplete lock — a stale `pylock.toml` not regenerated after a `pyproject.toml` change, or a `lock` bug — now aborts the build loudly, instead of silently installing an AppDir missing a transitive dependency that only surfaced as `ModuleNotFoundError` when the built AppImage was actually run.

## [2.0.1] - 2026-07-25

### Fixed

- `appimage.build` now always installs the `appimage` runtime module into the bundled `site-packages`, pinned to the currently running build version, instead of relying on the packaged project declaring `appimage` as a dependency itself. Previously, a built AppImage would fail at startup with `No module named appimage` unless `appimage` was explicitly listed in `[project.dependencies]`, which none of the documented examples did.

## [2.0.0] - 2026-05-10

### Added

- Bundled default icon (AppImage box + Python logo) used as fallback when no project icon is found — build no longer fails without an icon
- Icon is always copied into the AppDir as `{app}.{ext}`, matching the `Icon={app}` entry in the `.desktop` file
- `appimagetool` config key and `--appimagetool PATH` CLI option: use a local binary instead of downloading; resolution order is config/CLI → `PATH` → build cache → download
- `python_archive` config key and `--python-archive PATH` CLI option: use a local python-build-standalone tarball; resolution order is config/CLI → build cache → download
- `examples/myapp/`: minimal example project demonstrating zero-configuration usage

### Changed

- README: restructured with visual header, badges, RTD button; simplified to highlight unique features
- License changed from GPL v3 to Apache 2.0
- Project metadata: corrected description, homepage, documentation URL and PyPI classifiers
- AppRun and `.desktop` templates moved from inline strings to `appimage/build/templates/` and loaded via `importlib.resources`
- `generate_icon.py` developer script moved from `appimage/assets/` to `scripts/` (not part of the installed package)

### Documentation

- Added Sphinx documentation hosted on Read the Docs (sphinx-rtd-theme)
- Covers Quick Start, Configuration, CLI reference, Runtime, Internals, and Changelog
- CLI reference completed with missing `--app`, `--entry-point`, `--extras`, `--appimagetool`, `--python-archive` options
- Added `examples.md` page with minimal project walkthrough and offline/CI build instructions
- Added `internals.md` page explaining how AppImages are built

## [1.2.0] - 2026-05-08

### Added

- `--python-list-entry-points`: lists all available console script entry points (`name = module:function`) and exits
- `--python-appimage-debug`: prints startup debug information to stderr (venv detection, symlink traversal, entry point resolution, interpreter invocation)
- Virtual environment creation now uses the native `python -m venv` interface via `--python-interpreter -m venv ENV_DIR [options]`
- All standard `python -m venv` options are now supported: `--system-site-packages`, `--clear`, `--upgrade`, `--prompt`, `--without-scm-ignore-files`
- Python 3.13+: `scm_ignore_files` is now passed to `EnvBuilder` so `.gitignore` generation can be controlled via `--without-scm-ignore-files`
- `_activate_venv` now sets `VIRTUAL_ENV`, `sys.prefix`, `sys.exec_prefix`, and `sysconfig` base/platbase so activated environments are fully recognised by tooling

### Changed

- Extracted symlink traversal loop from `setup_virtualenv` into `_find_venv_dir_from_symlink` to reduce method complexity
- Removed duplicate venv-parsing logic in `parse_python_args`; now delegates to `parse_venv_command`
- Replaced monkey-patching of `EnvBuilder.setup_python` with a proper `_AppImageEnvBuilder` subclass
- Black formatting check now runs only once (Python 3.11) in CI instead of once per matrix version
- CI lint step now uses `hatch run +py=<version> lint:check` to avoid running the full matrix per job
- README build script now resolves the Python download URL dynamically from the GitHub API using only `PYTHON_MINOR` (e.g. `3.11`), eliminating the need to manually track patch versions and release dates

### Fixed

- Exception handling in `parse_venv_command` is now narrowed: `ValueError` is caught only for the `.index()` call, preventing unrelated errors from being silently swallowed
- Symlink depth limit in `setup_virtualenv` now emits a warning to stderr when exceeded instead of failing silently
- Fixed infinite loop in `setup_virtualenv` when following circular symlinks (depth limit: 20 hops)
- Fixed symlink traversal bug in `setup_virtualenv` where `Path.resolve()` prevented the loop from ever executing
- Fixed incorrect Python path in module docstring (`opt/python3.11/bin/python3.11` → `python/bin/python3`)
- Removed false reference to `appimage.ini` in `AppStarter.__init__` docstring
- Fixed `mkdir -p build` in README build script example — `build/AppDir` was never created, causing `tar` to fail

### Removed

- Removed `--python-venv` CLI option; use `--python-interpreter -m venv ENV_DIR` instead

## [1.1.1] - 2026-05-04

### Fixed

- Fixed `Path.readlink()` false positive (`assignment-from-no-return`) reported by pylint under Python 3.14

### Changed

- Added Python version matrix to hatch `test` and `lint` environments (3.11–3.14)
- Migrated CI test step from `hatch run test:run` to `hatch test`

## [1.1.0] - 2026-05-04

### Changed

- Replaced [python-appimage](https://github.com/niess/python-appimage) support with [astral.sh](https://astral.sh) as the standard AppImage base
- Migrated version management from `bumpversion` to `bump-my-version` (config in `pyproject.toml`)
- Updated GitHub Actions to current versions (`actions/checkout@v4`, `actions/setup-python@v5`)
- Migrated PyPI publishing to Trusted Publisher (OIDC), removing the need for a `PYPI_PASSWORD` secret

### Removed

- Removed support for [python-appimage](https://github.com/niess/python-appimage) by niess

## [1.0.0] - 2024-05-27

### Added

- added compatibility for [python-appimage](https://github.com/niess/python-appimage)


## [0.0.0] - 2023-11-01

Initial release on pypi.org

[Unreleased]: https://github.com/ssh-mitm/appimage/compare/2.0.1...main
[2.0.1]: https://github.com/ssh-mitm/appimage/compare/2.0.0...2.0.1
[2.0.0]: https://github.com/ssh-mitm/appimage/compare/1.2.0...2.0.0
[1.2.0]: https://github.com/ssh-mitm/appimage/compare/1.1.1...1.2.0
[1.1.1]: https://github.com/ssh-mitm/appimage/compare/1.1.0...1.1.1
[1.1.0]: https://github.com/ssh-mitm/appimage/compare/1.0.0...1.1.0
[1.0.0]: https://github.com/ssh-mitm/appimage/compare/0.0.0...1.0.0
[0.0.0]: https://github.com/ssh-mitm/appimage/releases/tag/0.0.0
