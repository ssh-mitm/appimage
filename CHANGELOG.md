# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]

### Added

- Default appimagetool source switched from AppImageKit's legacy `continuous` build to its successor [`AppImage/appimagetool`](https://github.com/AppImage/appimagetool), fixing a non-deterministic `mksquashfs` that made byte-identical output impossible (see [AppImageKit#929](https://github.com/AppImage/AppImageKit/issues/929))
- `appimagetool_sha256`/`appimagetool_version` config keys and `--appimagetool-sha256`/`--appimagetool-version` CLI options: verify appimagetool's sha256 regardless of source; a fresh download is auto-verified against GitHub's published digest even without this set
- `runtime_file`/`runtime_sha256` config keys and `--runtime-file`/`--runtime-sha256` CLI options: pre-fetch and verify the AppImage runtime ELF stub ourselves instead of letting appimagetool download it live and unverified at packaging time
- `verify_downloads` config key / `--verify-downloads` CLI flag: abort the build instead of warning when appimagetool, the runtime file, or the Python archive would be used unverified
- `python_sha256` config key / `--python-sha256` CLI option: verify the python-build-standalone tarball's sha256
- `--init` now also resolves and pins appimagetool and the runtime file when not already configured
- `require_zsyncmake` config key / `--require-zsyncmake` CLI flag: abort the build instead of warning when `update_info` is set but `zsyncmake` is not on `PATH` — previously a missing `zsyncmake` only produced appimagetool's own easy-to-miss stderr line, with no `.zsync` delta-update file and no build failure. Surfaced during `--check` too, not just at packaging time
- `reproducible` config key / `--reproducible` CLI flag: shortcut that implies `verify_downloads` and `require_zsyncmake`, and additionally requires `python_date`, `appimagetool_sha256`, and `runtime_sha256` to already be set — the three values that must be pinned (e.g. via `--init`) for a build to be reproducible across machines and over time, not just within one build environment
- Hash-pinned build dependencies (`requirements-build.txt`, `packaging/update-requirements.sh`, `packaging/verify-reproducible-build.sh`) and a bit-for-bit reproducibility proof for this package's own PyPI wheel — see `docs/reproducible-builds.md`
- `pylock` config key / `--pylock` CLI option: path to a hash-pinned `pylock.toml` for third-party dependencies — when set, they're installed with `pip install --require-hashes` instead of a live, unverified resolution
- `require_pylock` config key / `--require-pylock` CLI flag: abort the build instead of warning when `pylock` is not set
- `--lock` CLI mode: generates/refreshes `pylock.toml` via `pip lock` (pip >= 25.1), run through the bundled interpreter so hashes match its actual platform/Python build; writes `pylock` to `pyproject.toml` if not already set. `--uploaded-prior-to PnD` passes a cooldown window through to `pip lock` to exclude just-published releases from the resolution
- `--check`/`build` now always print a "Reproducibility: N/3 pins set" and "Dependency verification: ..." summary — previously the three reproducibility pins (`python_date`, `appimagetool_sha256`, `runtime_sha256`) had no visibility at all until either a real build resolved them or `reproducible` was already turned on and failed
- `tests/test_build.py`: first unit test coverage for the build module
- `build_pylock` config key / `--build-pylock` CLI option: path to a hash-pinned pylock-format file constraining the packaged project's *own* `[build-system].requires`. Installing the project's own source always triggers a PEP 517 isolated build, which otherwise installs that backend fresh from the index, unpinned and unverified, on every build — a gap `pylock` explicitly does not cover, since it excludes the local project via `--only-deps` at generation time. Installed with `pip install --require-hashes` before the project itself is installed with `--no-build-isolation`, so pip's own isolated build environment is skipped in favor of the already-verified backend
- `require_build_pylock` config key / `--require-build-pylock` CLI flag: abort the build instead of warning when `build_pylock` is not set
- `--check`'s "Dependency verification" summary line gains a sibling "Build backend verification" line for `build_pylock`
- `update_info` auto-detection: derives a `gh-releases-zsync` string from `[project.urls]` when an unambiguous GitHub repo is found and `update_info` isn't already set — surfaced as a warning via `--check`, written by `--init`, never silently applied to a live build (a wrong guess would embed a broken update pointer in the packaged AppImage)
- `--check`'s reproducibility summary is now a checklist: each of the three independent pinning layers (toolchain, `pylock`, `build_pylock`) gets a ✓/✗ mark, headed by a "Reproducibility checklist (N/3 ready)" count — same information as before, easier to scan at a glance
- `docs/reproducible-builds.md`: new "Getting to full reproducibility" walkthrough at the top of the page, sequencing `--init` → `--lock` → `--reproducible` as one linear path instead of leaving readers to piece it together from the independent sections below

### Changed

- `--lock` now also generates `build_pylock` alongside `pylock.toml` on every run (previously required a hand-generated `requirements-build.txt` via external `pip-compile`) — `[build-system].requires` changes rarely, so re-locking it every time costs little and closes the last manually-generated gap in the reproducibility story with no external tooling
- `--init` and `--lock` can now be combined in one invocation (`--init --lock`) to pin the toolchain and generate both lock files against it in a single step — `--init` resolution always runs first internally, regardless of argument order, so `--lock` never pins hashes against a stale/unpinned interpreter. `--check` still cannot be combined with either. Neither this nor any other combination sets `reproducible = true` — that stays a deliberate, separate step
- CI now installs this package's own build dependencies hash-verified (`pip install --require-hashes -r requirements-build.txt`) and builds the release wheel with `pip wheel --build-constraint requirements-build.txt --no-deps`, instead of an unverified `hatch build`; a new `reproducible-build` job runs `packaging/verify-reproducible-build.sh` on every push/PR to prove the wheel build is bit-identical across independent runs
- CI pins `hatch`, `pip`, and `build` to exact versions instead of installing whatever the index currently resolves (previously only this project's own build backend was pinned; the CI tooling that installs and drives it was not)
- Installed packages are byte-compiled via `pip install --no-compile` + `compileall --invalidation-mode unchecked-hash` instead of pip's default timestamp-based bytecode cache
- Every file and directory in the AppDir has its mtime normalized to `SOURCE_DATE_EPOCH` immediately before packaging
- `SOURCE_DATE_EPOCH` is now also passed into appimagetool's own process environment during packaging, not just applied to the AppDir
- README "Reproducible builds" section documents `appimagetool_sha256` and `SOURCE_DATE_EPOCH` as required alongside `python_date` for byte-for-byte reproducibility

### Fixed

- `write_config()`/`--init` no longer crashes with `ValueError` when a project has no icon file
- appimagetool/runtime-file download URLs now map `armv7l` to the actual `armhf` asset name instead of `armv7`, which does not exist and would 404

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
