# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""Resolving, downloading, and installing the bundled python-build-standalone Python."""

import logging
import re
import shutil
import subprocess  # nosec B404
import tarfile
from pathlib import Path
from typing import Final

from appimage.ctl._base import _ResolvedBuild
from appimage.ctl._download import (
    _asset_sha256,
    _download,
    _github_api_get,
    _require_or_warn_unverified,
    _resolution_source,
    _verify_sha256,
)

_log: Final = logging.getLogger(__name__)


def _python_tarball_cache_path(build_dir: Path, python: str, python_date: str) -> Path:
    """Return the conventional build-cache path for a resolved Python tarball.

    The one place encoding this filename convention - see
    ``_appimagetool._appimagetool_cache_path`` for the equivalent for
    appimagetool/the runtime stub, and the same rationale.

    Keyed by *python* and *python_date* - a fixed ``python.tar.gz`` name
    would silently keep serving whatever happened to be cached under it
    even after ``python`` (e.g. ``3.11`` -> ``3.12``) or ``python_date``
    changes in config, extracting a Python that doesn't match what the
    rest of the build assumes (``_compile_pyc``/``_scrub_build_paths``
    both derive their own ``site-packages`` path from *python*) - and
    without ``python_sha256`` set yet, nothing else catches the mismatch
    either (see ``_resolve_python_tarball``: the cached file is only
    hash-verified when a hash is actually configured). Changing either
    value now simply misses the old cache entry instead of reusing it.
    """
    return build_dir / f"python-{python}-{python_date or 'latest'}.tar.gz"


_ARCH_MAP: Final[dict[str, str]] = {
    "x86_64": "x86_64",
    "aarch64": "aarch64",
    "armv7l": "armv7",
}

_PBS_API: Final = (
    "https://api.github.com/repos/astral-sh/python-build-standalone/releases"
)


def _resolve_python_url(
    python: str,
    date: str,
    arch: str,
) -> tuple[str, str | None, str]:
    """Return the python-build-standalone download URL, its sha256, and the release date.

    Parameters
    ----------
    python : str
        Python minor version, e.g. ``"3.11"``.
    date : str
        Release date tag such as ``"20260211"``, or empty for the latest release.
    arch : str
        Host architecture from ``platform.machine()``.

    Returns
    -------
    tuple[str, str | None, str]
        Direct download URL for the matching ``install_only_stripped``
        tarball, its sha256 hex digest if GitHub published one for this
        asset (``None`` otherwise), and the release's own date tag -
        *date* echoed back if it was already given, or whatever "latest"
        actually resolved to otherwise, so callers can persist it (see
        ``_pinned_download_fields``) without a second API round trip.

    Raises
    ------
    RuntimeError
        If the architecture is not supported or no matching asset is found.

    """
    pbs_arch = _ARCH_MAP.get(arch)
    if pbs_arch is None:
        msg = f"Unsupported architecture: {arch}"
        raise RuntimeError(msg)

    api_url = f"{_PBS_API}/tags/{date}" if date else f"{_PBS_API}/latest"
    _log.info("Resolving Python %s download URL...", python)

    release = _github_api_get(api_url)
    # Both api_url forms above are single-release endpoints, never a list.
    assert isinstance(release, dict)  # noqa: S101  # nosec B101

    resolved_date = str(release.get("tag_name", date or "latest"))
    assets: list[dict[str, object]] = release.get("assets", [])  # type: ignore[assignment]
    for asset in assets:
        url = str(asset["browser_download_url"])
        if (
            f"cpython-{python}." in url
            and f"{pbs_arch}-unknown-linux-gnu-install_only_stripped" in url
            and "freethreaded" not in url
        ):
            return url, _asset_sha256(asset), resolved_date

    msg = f"No Python {python} asset found for {pbs_arch} in release {resolved_date}"
    raise RuntimeError(msg)


def _install_python(
    resolved: _ResolvedBuild,
    appdir: Path,
    python_cache: Path,
    arch: str,
) -> None:
    """Populate ``appdir/python`` from ``python_dir`` or a resolved tarball.

    ``python_dir`` bypasses tarball resolution, caching, and download
    entirely - it's copied in as given, unverified by design (see
    ``BuildConfig.python_dir``). Otherwise, behaves exactly as a plain
    build always has: resolve a python-build-standalone tarball (local
    ``python_archive``, build cache, or download) and extract it.
    """
    if resolved.python_dir:
        source = Path(resolved.python_dir)
        if not source.is_dir():
            msg = f"python_dir not found or not a directory: {source}"
            raise FileNotFoundError(msg)
        _log.info("Using Python directory (trusted, unverified): %s", source)
        shutil.copytree(source, appdir / "python")
        _verify_installed_python_version(resolved, appdir)
        return

    python_tarball = _resolve_python_tarball(resolved, python_cache, arch)
    _log.info("Extracting Python...")
    with tarfile.open(python_tarball) as tar:
        tar.extractall(appdir)  # noqa: S202  # nosec B202
    _verify_installed_python_version(resolved, appdir)


def _verify_installed_python_version(resolved: _ResolvedBuild, appdir: Path) -> None:
    """Confirm ``appdir/python`` actually contains the configured *python* version.

    A backstop independent of *how* ``appdir/python`` was populated - a
    stale cache entry, a hand-edited ``python_archive``, or a
    ``python_dir`` pointed at the wrong install could all produce a tree
    whose ``lib/pythonX.Y`` doesn't match ``resolved.python``, and every
    later step (``_compile_pyc``, ``_scrub_build_paths``) derives its own
    ``site-packages`` path from *that* value - so a mismatch here doesn't
    fail loudly where it happened, it fails confusingly several steps
    later (or not at all, silently shipping the wrong interpreter).
    Checking the one thing that actually matters - does the version this
    build asked for exist where everything downstream expects it - right
    after installation catches it immediately, with a message that
    actually names the problem.
    """
    expected = appdir / "python" / "lib" / f"python{resolved.python}"
    if not expected.is_dir():
        msg = (
            f"Expected Python {resolved.python} in the bundled interpreter "
            f"(missing {expected}), but the installed Python doesn't have it - "
            "wrong python_archive/python_dir, or a stale build cache from a "
            "different python/python_date. Remove the build directory's cached "
            "Python archive and rebuild."
        )
        raise RuntimeError(msg)


def _resolve_python_tarball(
    resolved: _ResolvedBuild,
    python_cache: Path,
    arch: str,
) -> Path:
    """Return the path to the Python tarball, downloading if necessary.

    A fresh download is verified against ``python_sha256`` when set, else
    against the digest GitHub publishes for the asset, at no extra network
    cost. A local ``python_archive`` or a cached tarball is only verified
    when ``python_sha256`` is explicitly set - otherwise, unless
    ``verify_downloads`` is also set, the documented offline/CI workflow
    stays fully network-free by default and this is used unverified.
    """
    source = _resolution_source(resolved.python_archive, python_cache)

    if source == "config":
        tarball = Path(resolved.python_archive)
        if not tarball.exists():
            msg = f"Python archive not found: {tarball}"
            raise FileNotFoundError(msg)
        _log.info("Using Python archive: %s", tarball)
        if resolved.python_sha256:
            _verify_sha256(tarball, resolved.python_sha256, label="python archive")
        else:
            _require_or_warn_unverified(
                tarball,
                label="python archive",
                config_key="python_sha256",
                strict=resolved.verify_downloads,
            )
        return tarball
    if source == "cache":
        _log.info("Using cached python.tar.gz")
        if resolved.python_sha256:
            _verify_sha256(python_cache, resolved.python_sha256, label="python archive")
        else:
            _require_or_warn_unverified(
                python_cache,
                label="python archive",
                config_key="python_sha256",
                strict=resolved.verify_downloads,
            )
        return python_cache
    python_url, api_sha256, _resolved_date = _resolve_python_url(
        resolved.python,
        resolved.python_date,
        arch,
    )
    _download(python_url, python_cache)
    expected = resolved.python_sha256 or api_sha256
    if expected:
        try:
            _verify_sha256(python_cache, expected, label="python archive")
        except RuntimeError:
            python_cache.unlink(missing_ok=True)
            raise
    else:
        try:
            _require_or_warn_unverified(
                python_cache,
                label="python archive",
                config_key="python_sha256",
                strict=resolved.verify_downloads,
            )
        except RuntimeError:
            python_cache.unlink(missing_ok=True)
            raise
    return python_cache


def _pip_version(python_bin: Path) -> tuple[int, int]:
    """Return the (major, minor) version of pip installed for *python_bin*."""
    result = subprocess.run(  # noqa: S603  # nosec B603
        [str(python_bin), "-m", "pip", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    match = re.match(r"pip (\d+)\.(\d+)", result.stdout)
    if not match:
        msg = f"Could not determine pip version from: {result.stdout.strip()!r}"
        raise RuntimeError(msg)
    return int(match.group(1)), int(match.group(2))
