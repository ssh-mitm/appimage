# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""The ``check`` subcommand: resolve and report the build configuration."""

import logging
import platform
from pathlib import Path
from typing import Final

from appimage.ctl._appimagetool import _appimagetool_cache_path, _runtime_cache_path
from appimage.ctl._base import BuildConfig, _resolve, _ResolvedBuild
from appimage.ctl._download import _resolution_source
from appimage.ctl._python import _python_tarball_cache_path

_log: Final = logging.getLogger(__name__)


def _icon_display(icon: Path | None) -> str:
    """Return a display-friendly path string for an icon."""
    if not icon:
        return "NOT FOUND"
    if icon.is_relative_to(Path.cwd()):
        return str(icon.relative_to(Path.cwd()))
    return str(icon)


def _optional_check_rows(resolved: _ResolvedBuild) -> list[tuple[str, str, str]]:
    """Return extra rows for optional config fields that are set."""
    cfg = "[tool.appimage]"
    candidates = [
        ("apprun", resolved.apprun),
        ("update_info", resolved.update_info),
        ("python_date", resolved.python_date),
        ("python_archive", resolved.python_archive),
        ("python_sha256", resolved.python_sha256),
        ("python_dir", resolved.python_dir),
        ("appimage_version", resolved.appimage_version),
        ("appimage_sha256", resolved.appimage_sha256),
        ("appimagectl_version", resolved.appimagectl_version),
        ("appimagetool", resolved.appimagetool),
        ("appimagetool_version", resolved.appimagetool_version),
        ("appimagetool_sha256", resolved.appimagetool_sha256),
        ("runtime_file", resolved.runtime_file),
        ("runtime_sha256", resolved.runtime_sha256),
        ("pylock", resolved.pylock),
        ("build_pylock", resolved.build_pylock),
    ]
    rows = [(name, value, cfg) for name, value in candidates if value]
    if resolved.reproducible:
        rows.append(("reproducible", "true", cfg))
    if resolved.verify_downloads:
        rows.append(("verify_downloads", "true", cfg))
    if resolved.require_zsyncmake:
        rows.append(("require_zsyncmake", "true", cfg))
    if resolved.require_pylock:
        rows.append(("require_pylock", "true", cfg))
    if resolved.require_build_pylock:
        rows.append(("require_build_pylock", "true", cfg))
    return rows


_RUNTIME_MODULE_REPRODUCIBILITY_PINS: Final = ("appimage_version", "appimage_sha256")
_PACKAGE_REPRODUCIBILITY_PINS: Final = ("appimagetool_sha256", "runtime_sha256")


def _reproducibility_summary(resolved: _ResolvedBuild) -> list[str]:
    """Return a checklist of the independent pinning stories.

    Unlike the individual warnings above, this always reflects the current
    state - not just when ``reproducible``/``require_pylock``/
    ``require_build_pylock`` are set and something is missing. Without
    it, a plain ``check`` gives no signal at all about the reproducibility
    pins: they only ever surface as a warning deep inside a real ``build()``
    run (when appimagetool/runtime/python are actually resolved) or as a
    hard error once ``reproducible`` is already turned on - nothing in
    between.

    AppDir and packaging reproducibility are reported as two separate
    lines, matching ``build_appdir()``/``build()``'s own split of which
    pins each actually needs (see ``_scrub_build_paths``/``build_appdir``).
    """
    appdir_ready = bool(resolved.python_date or resolved.python_dir)
    if resolved.python_dir:
        appdir_line = (
            f"AppDir reproducibility: python_dir set ({resolved.python_dir}) "
            "- trusted directory, not hash-verified"
        )
    elif appdir_ready:
        appdir_line = "AppDir reproducibility: python_date set"
    else:
        appdir_line = (
            "AppDir reproducibility: python_date not set - run 'init' to "
            "resolve and pin it, or set python_dir"
        )

    runtime_module_pinned = [
        key for key in _RUNTIME_MODULE_REPRODUCIBILITY_PINS if getattr(resolved, key)
    ]
    runtime_module_ready = len(runtime_module_pinned) == len(
        _RUNTIME_MODULE_REPRODUCIBILITY_PINS,
    )
    if runtime_module_ready:
        runtime_module_line = (
            "Runtime module reproducibility: appimage_version, appimage_sha256 set"
        )
    else:
        missing_runtime = ", ".join(
            key
            for key in _RUNTIME_MODULE_REPRODUCIBILITY_PINS
            if not getattr(resolved, key)
        )
        runtime_module_line = (
            f"Runtime module reproducibility: {missing_runtime} not set - run "
            "'init' to resolve and pin them"
        )

    package_pinned = [
        key for key in _PACKAGE_REPRODUCIBILITY_PINS if getattr(resolved, key)
    ]
    package_ready = len(package_pinned) == len(_PACKAGE_REPRODUCIBILITY_PINS)
    if package_ready:
        package_line = (
            "Packaging reproducibility: appimagetool_sha256, runtime_sha256 set"
        )
    else:
        missing = ", ".join(
            key for key in _PACKAGE_REPRODUCIBILITY_PINS if not getattr(resolved, key)
        )
        package_line = (
            f"Packaging reproducibility: {missing} not set - run 'init' to "
            "resolve and pin them"
        )

    pylock_ready = bool(resolved.pylock)
    pylock_line = (
        f"Dependency verification: pylock set ({resolved.pylock})"
        if pylock_ready
        else "Dependency verification: pylock not set - run 'lock' to generate pylock.toml"
    )

    build_pylock_ready = bool(resolved.build_pylock)
    build_pylock_line = (
        f"Build backend verification: build_pylock set ({resolved.build_pylock})"
        if build_pylock_ready
        else "Build backend verification: build_pylock not set - run 'lock' "
        "to generate it alongside pylock.toml"
    )

    ready = [
        appdir_ready,
        runtime_module_ready,
        package_ready,
        pylock_ready,
        build_pylock_ready,
    ]
    header = f"Reproducibility checklist ({sum(ready)}/{len(ready)} ready):"
    marks = ["✓" if r else "✗" for r in ready]
    lines = [
        appdir_line,
        runtime_module_line,
        package_line,
        pylock_line,
        build_pylock_line,
    ]
    return [
        header,
        *(f"  {mark} {line}" for mark, line in zip(marks, lines, strict=True)),
    ]


def _append_unverified_download_error(
    bucket: list[str],
    label: str,
    explicit: str,
    pin: str,
    cache_path: Path,
    config_key: str,
) -> None:
    """Append an error to *bucket* if *label* would resolve unverified under ``verify_downloads``.

    A pin already configured, or a resolution that would download fresh
    (always auto-verified against the digest GitHub publishes for the
    asset, regardless of any pin), is never a problem - only an explicit
    config path or an existing build-cache hit with no matching pin is.
    """
    if pin:
        return
    if _resolution_source(explicit, cache_path) == "download":
        return
    bucket.append(
        f"verify_downloads is set, but {label} would resolve unverified (no "
        f"{config_key} pin configured) - the build would abort at that point. "
        f"Set {config_key} (run 'init' to resolve and pin it).",
    )


def _predict_unverified_downloads(resolved: _ResolvedBuild, project_root: Path) -> None:
    """Append early appdir_errors/package_errors entries for downloads ``verify_downloads`` would reject.

    Computed purely from config plus a cheap existence check on the
    conventional build-cache paths - no network, no hashing - so this is
    safe to run on every ``check()`` (and, via ``_format_check``, every
    ``build()``/``build_appdir()``). Reuses ``_resolution_source``, the
    exact same precedence function ``_locate_appimagetool``/
    ``_resolve_runtime_file``/``_resolve_python_tarball`` call to decide
    the very same thing for real - there's no second copy of "explicit
    path, then cache, then download" to drift out of sync here.

    Deliberately only predicts the ``verify_downloads``-alone case.
    ``reproducible`` already requires every pin
    (``appimagetool_sha256``/``runtime_sha256`` in ``package_errors``,
    similarly for the AppDir-side pins) to be set unconditionally,
    regardless of how it would resolve - a stricter, resolution-independent
    check that already fully covers it, so predicting it here too would
    just be a redundant second error for the same root cause. Only
    ``verify_downloads`` set without ``reproducible`` needs this
    prediction, since there a resolution that would download fresh is
    perfectly fine unpinned - the naive version of this check (ignoring
    resolution kind entirely) would otherwise warn on that same, common,
    perfectly valid case.
    """
    if not resolved.verify_downloads or resolved.reproducible:
        return

    arch = platform.machine()
    build_dir = project_root / resolved.build_dir

    _append_unverified_download_error(
        resolved.package_errors,
        "appimagetool",
        resolved.appimagetool,
        resolved.appimagetool_sha256,
        _appimagetool_cache_path(build_dir, arch),
        "appimagetool_sha256",
    )
    _append_unverified_download_error(
        resolved.package_errors,
        "runtime file",
        resolved.runtime_file,
        resolved.runtime_sha256,
        _runtime_cache_path(build_dir, arch),
        "runtime_sha256",
    )
    if not resolved.python_dir:
        _append_unverified_download_error(
            resolved.appdir_errors,
            "python archive",
            resolved.python_archive,
            resolved.python_sha256,
            _python_tarball_cache_path(build_dir),
            "python_sha256",
        )


def _format_check(resolved: _ResolvedBuild, project_root: Path) -> None:
    """Log a human-readable configuration report.

    Parameters
    ----------
    resolved : _ResolvedBuild
        Resolved build configuration to report.
    project_root : Path
        Project root directory - needed to compute conventional build-cache
        paths for ``_predict_unverified_downloads``.

    """
    _predict_unverified_downloads(resolved, project_root)

    _log.info("Build configuration:")

    rows: list[tuple[str, str, str]] = [
        ("app", resolved.app, resolved.sources.get("app", "")),
        ("entry_point", resolved.entry_point, resolved.sources.get("entry_point", "")),
        ("python", resolved.python, resolved.sources.get("python", "")),
        (
            "packages",
            " ".join(resolved.install_targets),
            resolved.sources.get("packages", ""),
        ),
        ("icon", _icon_display(resolved.icon), resolved.sources.get("icon", "")),
        (
            "desktop",
            (
                str(resolved.desktop.relative_to(Path.cwd()))
                if resolved.desktop
                else "(generated)"
            ),
            resolved.sources.get("desktop", ""),
        ),
        ("build_dir", resolved.build_dir, resolved.sources.get("build_dir", "")),
        ("dist_dir", resolved.dist_dir, resolved.sources.get("dist_dir", "")),
        *_optional_check_rows(resolved),
    ]

    for name, value, source in rows:
        _log.info("  %-15s %-35s [%s]", f"{name}:", value, source)

    _log.info("")
    for line in _reproducibility_summary(resolved):
        _log.info("  %s", line)

    warnings = resolved.appdir_warnings + resolved.package_warnings
    if warnings:
        _log.info("")
        for w in warnings:
            _log.warning("  Warning: %s", w)

    errors = resolved.appdir_errors + resolved.package_errors
    if errors:
        _log.info("")
        for e in errors:
            _log.error("  Error:   %s", e)


def check(config: BuildConfig, project_root: Path) -> bool:
    """Resolve and print the build configuration.

    Parameters
    ----------
    config : BuildConfig
        Explicit configuration from ``pyproject.toml``.
    project_root : Path
        Project root directory.

    Returns
    -------
    bool
        ``True`` if the configuration is complete and the build can proceed.

    """
    resolved = _resolve(config, project_root)
    _format_check(resolved, project_root)
    return not (resolved.appdir_errors or resolved.package_errors)
