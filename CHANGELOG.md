# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]

### Fixed

- Fixed infinite loop in `setup_virtualenv` when following circular symlinks (depth limit: 20 hops)
- Fixed symlink traversal bug in `setup_virtualenv` where `Path.resolve()` prevented the loop from ever executing
- Fixed incorrect Python path in module docstring (`opt/python3.11/bin/python3.11` → `python/bin/python3`)
- Removed false reference to `appimage.ini` in `AppStarter.__init__` docstring

### Changed

- Replaced monkey-patching of `EnvBuilder.setup_python` with a proper `_AppImageEnvBuilder` subclass
- Black formatting check now runs only once (Python 3.11) in CI instead of once per matrix version
- CI lint step now uses `hatch run +py=<version> lint:check` to avoid running the full matrix per job

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

[Unreleased]: https://github.com/ssh-mitm/appimage/compare/1.1.1...main
[1.1.1]: https://github.com/ssh-mitm/appimage/compare/1.1.0...1.1.1
[1.1.0]: https://github.com/ssh-mitm/appimage/compare/1.0.0...1.1.0
[1.0.0]: https://github.com/ssh-mitm/appimage/compare/0.0.0...1.0.0
[0.0.0]: https://github.com/ssh-mitm/appimage/releases/tag/0.0.0
