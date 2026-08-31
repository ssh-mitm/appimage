# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""The ``init`` subcommand: write auto-detected and toolchain-pinned values."""

import importlib.metadata
import logging
import platform
import tomllib
from pathlib import Path
from typing import Final

from appimage.ctl._appimagetool import (
    _appimagetool_version_string,
    _resolve_appimagetool,
    _resolve_runtime_file,
)
from appimage.ctl._base import _DEFAULT_ICON, BuildConfig, _resolve, _ResolvedBuild
from appimage.ctl._download import _sha256_file
from appimage.ctl._python import _resolve_python_url
from appimage.ctl._toml import _toml_value
from appimage.ctl.build_appdir import _resolve_appimage_pin_sha256
from appimage.ctl.check import _format_check

_log: Final = logging.getLogger(__name__)


def _auto_detected_fields(
    resolved: _ResolvedBuild,
    project_root: Path,
    existing: set[str],
) -> dict[str, object]:
    """Return auto-detected app/entry_point/python/icon/desktop values to add."""
    new: dict[str, object] = {}
    if "app" not in existing:
        new["app"] = resolved.app
    # An empty source means _resolve_entry_point couldn't determine one and
    # fell back to *app* as a placeholder alongside an error in
    # resolved.appdir_errors — writing that guess here would silently turn
    # a loud check error into a wrong-but-configured value.
    if "entry_point" not in existing and resolved.sources.get("entry_point"):
        new["entry_point"] = resolved.entry_point
    if "python" not in existing:
        new["python"] = resolved.python
    if (
        "icon" not in existing
        and resolved.icon is not None
        and resolved.icon != _DEFAULT_ICON
    ):
        new["icon"] = str(resolved.icon.relative_to(project_root))
    if "desktop" not in existing and resolved.desktop is not None:
        new["desktop"] = str(resolved.desktop.relative_to(project_root))
    if "update_info" not in existing and resolved.update_info_suggested:
        new["update_info"] = resolved.update_info_suggested
    return new


def _pinned_download_fields(
    resolved: _ResolvedBuild,
    project_root: Path,
    existing: set[str],
) -> dict[str, object]:
    """Resolve toolchain pins and return their fields to add.

    Covers python_date/python_sha256, appimage_version/appimage_sha256,
    appimagetool_version/appimagetool_sha256, runtime_sha256, and
    appimagectl_version. Only resolves what isn't already configured; may
    trigger downloads (except appimagectl_version, a local metadata read).
    """
    new: dict[str, object] = {}
    arch = platform.machine()
    build_dir = project_root / resolved.build_dir
    build_dir.mkdir(parents=True, exist_ok=True)

    if "python_date" not in existing:
        _url, api_sha256, resolved_date = _resolve_python_url(
            resolved.python,
            resolved.python_date,
            arch,
        )
        new["python_date"] = resolved_date
        if api_sha256 and "python_sha256" not in existing:
            new["python_sha256"] = api_sha256

    if "appimage_version" not in existing and "appimage_sha256" not in existing:
        version = importlib.metadata.version("appimage")
        digest = _resolve_appimage_pin_sha256(f"appimage=={version}", strict=False)
        new["appimage_version"] = version
        if digest:
            new["appimage_sha256"] = digest

    if "appimagetool_version" not in existing and "appimagetool_sha256" not in existing:
        appimagetool_cache = build_dir / f"appimagetool-{arch}.AppImage"
        tool = _resolve_appimagetool(resolved, appimagetool_cache, arch)
        new["appimagetool_version"] = _appimagetool_version_string(tool)
        new["appimagetool_sha256"] = _sha256_file(tool)

    if "runtime_sha256" not in existing:
        runtime_cache = build_dir / f"runtime-{arch}"
        runtime = _resolve_runtime_file(resolved, runtime_cache, arch)
        new["runtime_sha256"] = _sha256_file(runtime)

    if "appimagectl_version" not in existing:
        new["appimagectl_version"] = importlib.metadata.version("appimage")

    return new


def write_config(config: BuildConfig, project_root: Path) -> None:
    """Write auto-detected values to ``pyproject.toml``.

    Only fields that are not already explicitly set in ``[tool.appimage]``
    are written. Existing values are never overwritten.

    When ``python_date`` isn't already configured, this resolves whatever
    python-build-standalone currently publishes as latest and writes its
    release date, plus ``python_sha256`` when GitHub publishes a digest
    for it — a lightweight API call, no tarball download. When
    ``appimagetool_version``/``appimagetool_sha256`` are not already
    configured, this also resolves appimagetool (via the same lookup order
    as a real build — explicit path, ``PATH``, build cache, or download) and
    writes its sha256 and self-reported version banner, so a subsequent
    build can pin against exactly this binary. The same applies to
    ``runtime_sha256`` and the runtime ELF stub. Together these may trigger
    downloads (~8 MB and ~1 MB respectively) the first time this runs if
    neither is otherwise available locally.

    Parameters
    ----------
    config : BuildConfig
        Explicit configuration already loaded from ``pyproject.toml``.
    project_root : Path
        Project root directory.

    """
    resolved = _resolve(config, project_root)
    _format_check(resolved)

    pyproject_path = project_root / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)
    existing = set(data.get("tool", {}).get("appimage", {}).keys())

    new: dict[str, object] = _auto_detected_fields(resolved, project_root, existing)
    new.update(_pinned_download_fields(resolved, project_root, existing))

    if not new:
        _log.info("")
        _log.info("Nothing to add — all detected values are already configured.")
        return

    lines = "\n".join(f"{k} = {_toml_value(v)}" for k, v in new.items())
    content = pyproject_path.read_text()

    if "[tool.appimage]" in content:
        content = content.replace(
            "[tool.appimage]",
            f"[tool.appimage]\n{lines}",
            1,
        )
    else:
        content += f"\n[tool.appimage]\n{lines}\n"

    pyproject_path.write_text(content)
    _log.info("")
    _log.info("Added to pyproject.toml:")
    for k, v in new.items():
        _log.info("  %s = %s", k, _toml_value(v))
