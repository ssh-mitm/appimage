# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""The ``enable-reproducible`` subcommand: onboard a project onto reproducible builds."""

from pathlib import Path

from appimage.ctl._base import BuildConfig
from appimage.ctl.build import build
from appimage.ctl.init import write_config
from appimage.ctl.lock import _write_reproducible_flag, lock


def enable_reproducible(
    config: BuildConfig,
    project_root: Path,
    *,
    uploaded_prior_to: str = "",
) -> None:
    """Onboard a project onto reproducible builds in a single step.

    Equivalent to running ``init`` then ``lock``, then a real build with
    ``reproducible`` enforced, and finally — only once that build has
    actually succeeded — writing ``reproducible = true`` to
    ``pyproject.toml``. Never writes the flag as a side effect of merely
    resolving or locking values: see docs/reproducible-builds.md for why
    that would be premature (the pins could still fail to build together).

    Parameters
    ----------
    config : BuildConfig
        Explicit configuration already loaded from ``pyproject.toml``.
    project_root : Path
        Project root directory.
    uploaded_prior_to : str
        Optional cooldown window passed through to ``lock()``, see there.

    Raises
    ------
    SystemExit
        If the resolved configuration has errors that prevent building.

    """
    write_config(config, project_root)
    # Re-read after each write so the next step sees the pins/locks that
    # were just written, not a stale/unpinned config — same reasoning as
    # the previous --init/--lock flag combo used to apply.
    config = BuildConfig.from_pyproject(project_root)
    lock(config, project_root, uploaded_prior_to=uploaded_prior_to)
    config = BuildConfig.from_pyproject(project_root)
    config.reproducible = True
    build(config, project_root)
    _write_reproducible_flag(project_root)
