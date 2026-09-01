# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""The default subcommand: assemble the AppDir and package it into an AppImage."""

import logging
import os
import platform
import subprocess  # nosec B404
from pathlib import Path
from typing import Final

from appimage.ctl._appimagetool import _resolve_appimagetool, _resolve_runtime_file
from appimage.ctl._base import BuildConfig, _resolve
from appimage.ctl.build_appdir import _assemble_appdir
from appimage.ctl.check import _format_check

_log: Final = logging.getLogger(__name__)


def build(config: BuildConfig, project_root: Path) -> None:
    """Build an AppImage from *config* rooted at *project_root*.

    Parameters
    ----------
    config : BuildConfig
        Build configuration (explicit fields only; the rest are auto-detected).
    project_root : Path
        Absolute path to the project root directory.

    Raises
    ------
    SystemExit
        If the resolved configuration has errors that prevent building.

    """
    resolved = _resolve(config, project_root)
    _format_check(resolved)

    if resolved.appdir_errors or resolved.package_errors:
        raise SystemExit(1)

    arch = platform.machine()
    build_dir = project_root / resolved.build_dir
    appdir = build_dir / "AppDir"
    dist_dir = project_root / resolved.dist_dir
    python_cache = build_dir / "python.tar.gz"
    appimagetool_cache = build_dir / f"appimagetool-{arch}.AppImage"
    runtime_cache = build_dir / f"runtime-{arch}"
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))

    _assemble_appdir(resolved, appdir, python_cache, arch, project_root, epoch)

    appimagetool_bin = _resolve_appimagetool(resolved, appimagetool_cache, arch)
    runtime_bin = _resolve_runtime_file(resolved, runtime_cache, arch)

    dist_dir.mkdir(parents=True, exist_ok=True)
    output_name = f"{resolved.app}-{arch}.AppImage"

    cmd = [str(appimagetool_bin), "--runtime-file", str(runtime_bin)]
    if resolved.update_info:
        cmd += ["-u", resolved.update_info]
    cmd += [str(appdir), output_name]

    _log.info("Packaging AppImage...")
    subprocess.run(  # noqa: S603  # nosec B603
        cmd,
        cwd=dist_dir,
        env={**os.environ, "SOURCE_DATE_EPOCH": str(epoch)},
        check=True,
    )

    if resolved.update_info:
        _check_zsync_file(dist_dir, output_name, require=resolved.require_zsyncmake)

    _log.info("Done: %s", dist_dir / output_name)


def _check_zsync_file(dist_dir: Path, output_name: str, *, require: bool) -> None:
    """Warn (or, if *require*, abort) when appimagetool didn't produce a ``.zsync`` file.

    Checked against the real output rather than predicted beforehand: an
    earlier version of this project guessed by checking whether
    ``zsyncmake`` was on the *build host's* ``PATH`` — but appimagetool
    bundles its own copy and its ``AppRun`` puts its own ``usr/bin`` first
    on ``PATH`` (ahead of the host's), so that guess was checking the wrong
    thing entirely and gave a different, host-dependent answer on every
    machine regardless of whether packaging would actually produce a
    ``.zsync`` file or not. Checking the real output after the fact is both
    simpler and actually accurate.
    """
    zsync_path = dist_dir / f"{output_name}.zsync"
    if zsync_path.exists():
        return
    msg = (
        f"update_info is set but appimagetool did not produce {zsync_path.name} "
        "— see its own output above for why (a custom appimagetool build "
        "with no bundled zsyncmake is the most likely cause)."
    )
    if require:
        raise RuntimeError(msg)
    _log.warning(msg)
