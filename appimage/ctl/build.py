# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""The default subcommand: assemble the AppDir and package it into an AppImage."""

import logging
import os
import platform
import shutil
import subprocess  # nosec B404
import tempfile
from pathlib import Path
from typing import Final

from appimage.ctl._appimagetool import (
    _appimagetool_cache_path,
    _resolve_appimagetool,
    _resolve_runtime_file,
    _runtime_cache_path,
)
from appimage.ctl._base import BuildConfig, _resolve
from appimage.ctl._python import _python_tarball_cache_path
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
    _format_check(resolved, project_root)

    if resolved.appdir_errors or resolved.package_errors:
        raise SystemExit(1)

    arch = platform.machine()
    build_dir = project_root / resolved.build_dir
    appdir = build_dir / "AppDir"
    dist_dir = project_root / resolved.dist_dir
    python_cache = _python_tarball_cache_path(
        build_dir,
        resolved.python,
        resolved.python_date,
    )
    appimagetool_cache = _appimagetool_cache_path(build_dir, arch)
    runtime_cache = _runtime_cache_path(build_dir, arch)
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))

    _assemble_appdir(resolved, appdir, python_cache, arch, project_root, epoch)

    appimagetool_bin = _resolve_appimagetool(resolved, appimagetool_cache, arch)
    runtime_bin, _runtime_tag = _resolve_runtime_file(resolved, runtime_cache, arch)

    dist_dir.mkdir(parents=True, exist_ok=True)
    output_name = f"{resolved.app}-{arch}.AppImage"

    _log.info("Packaging AppImage...")
    with tempfile.TemporaryDirectory(prefix="appimagectl-runtime-") as staging_dir:
        staged_runtime = _stage_runtime_file_for_appimagetool(
            runtime_bin,
            Path(staging_dir),
        )
        # mksquashfs preserves filesystem xattrs by default, so without this
        # a build host's own security-context labels (e.g. SELinux) leak
        # into the packaged image - and differ from a build host without
        # them, breaking reproducibility across machines.
        #
        # mksquashfs's duplicate-file detection pre-filters candidates by
        # comparing already-compressed block sizes before doing a full
        # byte comparison - confirmed by hand that this makes the packaged
        # image sensitive to incidental per-build state (observed even
        # between two immediately-successive builds from a freshly
        # reassembled, content-identical AppDir on the same machine, no
        # network or other machine involved). Trades a larger AppImage
        # (duplicate files are stored once each, not deduplicated) for
        # actually holding the "identical input produces identical output"
        # guarantee this project makes.
        #
        # -processors 1 pins mksquashfs's own compression thread count.
        # Confirmed by hand: with the thread count left to auto-detect
        # (mksquashfs's default), otherwise-identical AppDirs packaged the
        # same way produced different bytes depending on how mksquashfs
        # was invoked - not on file content, order (checked with disorderfs
        # forcing genuinely randomized directory order: no effect once
        # processors is pinned), or machine, but on something schedule-
        # dependent in how the parallel deflator threads finish and get
        # written out. Single-threaded compression removes that variable
        # entirely, at a real cost: packaging takes longer, proportional to
        # AppDir size.
        cmd = [
            str(appimagetool_bin),
            "--runtime-file",
            str(staged_runtime),
            "--mksquashfs-opt",
            "-no-xattrs",
            "--mksquashfs-opt",
            "-no-duplicates",
            "--mksquashfs-opt",
            "-processors",
            "--mksquashfs-opt",
            "1",
        ]
        if resolved.update_info:
            cmd += ["-u", resolved.update_info]
        cmd += [str(appdir), output_name]

        # LC_ALL=C so mksquashfs's own C-library string handling (and
        # appimagetool's glib argument parsing) can't vary with the build
        # host's locale - no PYTHONHASHSEED here, appimagetool/mksquashfs
        # are native binaries, not Python. TZ=UTC for the same reason as
        # _isolated_subprocess_env: SOURCE_DATE_EPOCH itself is timezone-
        # independent, but nothing rules out some code path in appimagetool
        # or mksquashfs formatting the current wall-clock time rather than
        # just clamping to the epoch - pinning the timezone here too closes
        # that off rather than trusting it doesn't happen.
        subprocess.run(  # noqa: S603  # nosec B603
            cmd,
            cwd=dist_dir,
            env={
                **os.environ,
                "SOURCE_DATE_EPOCH": str(epoch),
                "LC_ALL": "C",
                "TZ": "UTC",
            },
            check=True,
        )

    if resolved.update_info:
        _check_zsync_file(dist_dir, output_name, require=resolved.require_zsyncmake)

    _log.info("Done: %s", dist_dir / output_name)


def _stage_runtime_file_for_appimagetool(runtime_bin: Path, staging_dir: Path) -> Path:
    """Copy *runtime_bin* into *staging_dir* and return the copy's path.

    Works around a bug in the pinned ``AppImage/appimagetool`` build itself
    (confirmed by hand, isolated from appimagectl entirely): its glib-based
    CLI option parser fails to decode a ``--runtime-file`` value containing
    any non-ASCII byte - ``Option parsing failed: Invalid byte sequence in
    conversion input`` - reproducible regardless of the process's own
    locale (tested ``de_DE.UTF-8`` and ``C.UTF-8``) and regardless of
    ``G_FILENAME_ENCODING``/``G_BROKEN_FILENAMES``, so it isn't something
    appimagectl can fix by adjusting the subprocess environment. The
    *positional* AppDir/output arguments are unaffected - only this one
    flag's value goes through whatever stricter path glib uses for it.
    ``runtime_bin`` is ``project_root / build_dir / f"runtime-{arch}"``, so
    any project whose absolute path contains a non-ASCII character (a very
    ordinary thing - e.g. a home directory under a non-English username)
    would otherwise fail packaging outright, 100% of the time. Staging a
    plain copy under a guaranteed-ASCII temp path sidesteps the bug
    entirely: the flag value itself never contains anything that could
    trip it, independent of where the project actually lives. Purely a
    workaround for appimagetool's own argument parsing - the copy's
    *content* is byte-identical to ``runtime_bin`` and has no bearing on
    the packaged output.
    """
    staged = staging_dir / runtime_bin.name
    shutil.copy2(runtime_bin, staged)
    return staged


def _check_zsync_file(dist_dir: Path, output_name: str, *, require: bool) -> None:
    """Warn (or, if *require*, abort) when appimagetool didn't produce a ``.zsync`` file.

    Checked against the real output rather than predicted beforehand: an
    earlier version of this project guessed by checking whether
    ``zsyncmake`` was on the *build host's* ``PATH`` - but appimagetool
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
        "- see its own output above for why (a custom appimagetool build "
        "with no bundled zsyncmake is the most likely cause)."
    )
    if require:
        raise RuntimeError(msg)
    _log.warning(msg)
