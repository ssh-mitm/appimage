# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""Resolving, downloading, and verifying appimagetool and the AppImage runtime stub."""

import logging
import shutil
import subprocess  # nosec B404
from pathlib import Path
from typing import Final

from appimage.ctl._base import _ResolvedBuild
from appimage.ctl._download import (
    _download,
    _fetch_release_asset_digest,
    _require_or_warn_unverified,
    _verify_sha256,
)

_log: Final = logging.getLogger(__name__)

# appimagetool/type2-runtime use different architecture tags than
# python-build-standalone for the same physical hardware (e.g. "armhf"
# rather than "armv7").
_APPIMAGETOOL_ARCH_MAP: Final[dict[str, str]] = {
    "x86_64": "x86_64",
    "aarch64": "aarch64",
    "armv7l": "armhf",
}

# AppImage/AppImageKit's classic C appimagetool is no longer maintained (its
# bundled mksquashfs has a documented non-deterministic multi-threaded
# compression bug, see https://github.com/AppImage/AppImageKit/issues/929)
# and its own release notes now point downloads at this successor instead.
# It bundles a modern, fixed squashfs-tools and — unlike AppImageKit's
# "continuous" release — publishes a sha256 digest per asset via the
# GitHub API, so it doubles as the source for the free-verification
# digest used when appimagetool_sha256 is not explicitly configured.
_APPIMAGETOOL_REPO: Final = "AppImage/appimagetool"
_APPIMAGETOOL_ASSET: Final = "appimagetool-{arch}.AppImage"

# The runtime ELF stub that appimagetool prepends to the squashfs image.
# Newer appimagetool versions download this at packaging time instead of
# bundling it, which both defeats verification (nothing checks what was
# fetched) and hangs in network environments where the tool's bundled
# libcurl can't complete the download (e.g. behind certain TLS-intercepting
# proxies) — pre-fetching and pinning it here avoids both problems.
_RUNTIME_REPO: Final = "AppImage/type2-runtime"
_RUNTIME_ASSET: Final = "runtime-{arch}"


def _appimagetool_version_string(tool: Path) -> str:
    """Return appimagetool's own ``--version`` banner as a human-readable label."""
    result = subprocess.run(  # noqa: S603  # nosec B603
        [str(tool), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (result.stderr or result.stdout).strip()


def _resolve_appimagetool(
    resolved: _ResolvedBuild,
    appimagetool_cache: Path,
    arch: str,
) -> Path:
    """Return the path to appimagetool, downloading if necessary.

    When ``appimagetool_sha256`` is set, the resolved binary is verified
    against it regardless of where it came from (explicit path, ``PATH``,
    build cache, or download) — a mismatch aborts the build. Only a binary
    this function downloaded itself is deleted on mismatch, so a retry
    re-downloads cleanly; a user-configured path or a ``PATH``-found binary
    is never touched. A fresh download is additionally auto-verified
    against the digest GitHub publishes for the asset, at no extra network
    cost, even when ``appimagetool_sha256`` is unset. Only a config-path,
    ``PATH``, or cache resolution with no pin configured is used unverified
    (with a warning logging its actual hash).
    """
    tool: Path
    downloaded = False
    api_sha256: str | None = None

    if resolved.appimagetool:
        tool = Path(resolved.appimagetool)
        if not tool.exists():
            msg = f"appimagetool not found: {tool}"
            raise FileNotFoundError(msg)
        _log.info("Using appimagetool: %s", tool)
    elif path_tool := shutil.which("appimagetool"):
        tool = Path(path_tool)
        _log.info("Using appimagetool from PATH: %s", tool)
    elif appimagetool_cache.exists():
        tool = appimagetool_cache
        _log.info("Using cached appimagetool")
    else:
        appimagetool_arch = _APPIMAGETOOL_ARCH_MAP.get(arch, arch)
        asset_name = _APPIMAGETOOL_ASSET.format(arch=appimagetool_arch)
        url, api_sha256 = _fetch_release_asset_digest(
            _APPIMAGETOOL_REPO,
            "continuous",
            asset_name,
        )
        _download(url, appimagetool_cache)
        appimagetool_cache.chmod(0o755)
        tool = appimagetool_cache
        downloaded = True

    expected = resolved.appimagetool_sha256 or api_sha256
    if expected:
        try:
            _verify_sha256(tool, expected, label="appimagetool")
        except RuntimeError:
            if downloaded:
                tool.unlink(missing_ok=True)
            raise
    else:
        try:
            _require_or_warn_unverified(
                tool,
                label="appimagetool",
                config_key="appimagetool_sha256",
                strict=resolved.verify_downloads,
            )
        except RuntimeError:
            if downloaded:
                tool.unlink(missing_ok=True)
            raise

    return tool


def _resolve_runtime_file(
    resolved: _ResolvedBuild,
    runtime_cache: Path,
    arch: str,
) -> Path:
    """Return the path to the AppImage runtime ELF stub, downloading if necessary.

    Newer appimagetool releases fetch this at packaging time via their own
    bundled libcurl instead of embedding it, which leaves it both unverified
    (nothing checks what was downloaded) and prone to hanging in network
    environments that libcurl can't negotiate (e.g. some TLS-intercepting
    proxies). Resolving and verifying it here, the same way as
    ``_resolve_appimagetool``, closes both gaps and lets it be passed via
    appimagetool's own ``--runtime-file`` flag to skip its live download
    entirely.
    """
    runtime: Path
    downloaded = False
    api_sha256: str | None = None

    if resolved.runtime_file:
        runtime = Path(resolved.runtime_file)
        if not runtime.exists():
            msg = f"runtime file not found: {runtime}"
            raise FileNotFoundError(msg)
        _log.info("Using runtime file: %s", runtime)
    elif runtime_cache.exists():
        runtime = runtime_cache
        _log.info("Using cached runtime file")
    else:
        runtime_arch = _APPIMAGETOOL_ARCH_MAP.get(arch, arch)
        asset_name = _RUNTIME_ASSET.format(arch=runtime_arch)
        url, api_sha256 = _fetch_release_asset_digest(
            _RUNTIME_REPO,
            "continuous",
            asset_name,
        )
        _download(url, runtime_cache)
        runtime_cache.chmod(0o755)
        runtime = runtime_cache
        downloaded = True

    expected = resolved.runtime_sha256 or api_sha256
    if expected:
        try:
            _verify_sha256(runtime, expected, label="runtime file")
        except RuntimeError:
            if downloaded:
                runtime.unlink(missing_ok=True)
            raise
    else:
        try:
            _require_or_warn_unverified(
                runtime,
                label="runtime file",
                config_key="runtime_sha256",
                strict=resolved.verify_downloads,
            )
        except RuntimeError:
            if downloaded:
                runtime.unlink(missing_ok=True)
            raise

    return runtime
