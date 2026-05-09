# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]

### Added

- Bundled default icon (AppImage box + Python logo) used as fallback when no project icon is found — build no longer fails without an icon
- Icon is always copied into the AppDir as `{app}.{ext}`, matching the `Icon={app}` entry in the `.desktop` file

### Changed

- README: unified configuration reference into a single table; restructured into Build, CLI, and Runtime sections; highlighted python-build-standalone / uv shared source

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

[Unreleased]: https://github.com/ssh-mitm/appimage/compare/1.2.0...main
[1.2.0]: https://github.com/ssh-mitm/appimage/compare/1.1.1...1.2.0
[1.1.1]: https://github.com/ssh-mitm/appimage/compare/1.1.0...1.1.1
[1.1.0]: https://github.com/ssh-mitm/appimage/compare/1.0.0...1.1.0
[1.0.0]: https://github.com/ssh-mitm/appimage/compare/0.0.0...1.0.0
[0.0.0]: https://github.com/ssh-mitm/appimage/releases/tag/0.0.0
