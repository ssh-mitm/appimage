# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""The ``update-tools`` subcommand: move every toolchain pin forward."""

import logging
from pathlib import Path
from typing import Final

from appimage.ctl._base import BuildConfig, _resolve
from appimage.ctl._toml import _replace_or_append_toml_fields, _toml_value
from appimage.ctl.check import _format_check
from appimage.ctl.init import _pinned_download_fields

_log: Final = logging.getLogger(__name__)


def update_tools(config: BuildConfig, project_root: Path) -> None:
    """Move every toolchain pin forward to whatever's currently available.

    Refreshes ``python_date``/``python_sha256``, ``appimage_version``/
    ``appimage_sha256``, ``appimagetool_version``/``appimagetool_sha256``,
    ``runtime_sha256``, and ``appimagectl_version`` unconditionally,
    overwriting whatever's already configured - the same "move pins
    forward" role ``packaging/update-requirements.sh --upgrade`` plays for
    this project's own build-backend pin, applied here to appimage.ctl's
    own toolchain. Never touches ``pylock``/``build_pylock`` (already
    regenerated on every ``lock`` run regardless of what's configured) or
    project metadata (``app``/``entry_point``/``icon``/``desktop``) - this
    is specifically for the pins ``init`` would otherwise leave alone once
    set once.

    Parameters
    ----------
    config : BuildConfig
        Explicit configuration already loaded from ``pyproject.toml``.
    project_root : Path
        Project root directory.

    """
    resolved = _resolve(config, project_root)
    _format_check(resolved, project_root)

    new = _pinned_download_fields(resolved, project_root, existing=set())

    pyproject_path = project_root / "pyproject.toml"
    _replace_or_append_toml_fields(pyproject_path, new)

    _log.info("")
    _log.info("Updated in pyproject.toml:")
    for k, v in new.items():
        _log.info("  %s = %s", k, _toml_value(v))
