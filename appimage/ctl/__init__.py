# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""Build an AppImage from a Python project configured via pyproject.toml.

This package is organized by CLI subcommand: each submodule implements one
subcommand (or a small group of shared low-level helpers), depending only on
``_base`` (shared ``BuildConfig``/``_ResolvedBuild``/``_resolve()`` machinery)
and on each other — never back on this ``__init__.py`` itself, so the import
graph stays a plain DAG with no cycles. This module's only job is to compose
the public API from those submodules, so ``appimage.ctl.__main__`` (and
anything else) can keep importing it exactly as before the split.
"""

from appimage.ctl._base import BuildConfig
from appimage.ctl.build import build
from appimage.ctl.build_appdir import build_appdir
from appimage.ctl.check import check
from appimage.ctl.enable_reproducible import enable_reproducible
from appimage.ctl.init import write_config
from appimage.ctl.lock import lock
from appimage.ctl.update_tools import update_tools

__all__ = [
    "BuildConfig",
    "build",
    "build_appdir",
    "check",
    "enable_reproducible",
    "lock",
    "update_tools",
    "write_config",
]
