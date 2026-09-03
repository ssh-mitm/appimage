# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""Resolving, downloading, and verifying appimagetool and the AppImage runtime stub."""

import logging
import subprocess  # nosec B404
from pathlib import Path
from typing import Final

from appimage.ctl._base import _ResolvedBuild
from appimage.ctl._download import (
    _download,
    _fetch_latest_versioned_release_asset_digest,
    _require_or_warn_unverified,
    _resolution_source,
    _verify_sha256,
)

_log: Final = logging.getLogger(__name__)

# Both repos also publish a "continuous" release, reused and overwritten on
# every rebuild - fine for someone always building from scratch, but a hash
# pinned against it can become permanently unfetchable once upstream cuts a
# new one (GitHub doesn't keep the overwritten bytes). These patterns match
# only their genuine, immutable, versioned releases instead - confirmed by
# hand against the real tag lists: appimagetool uses semver ("1.9.1"),
# type2-runtime uses a release date ("20251108"). Neither matches
# "continuous", "old", or "previous" (other non-versioned tags seen on
# type2-runtime).
_APPIMAGETOOL_VERSION_TAG_PATTERN: Final = r"\d+\.\d+\.\d+"
_RUNTIME_VERSION_TAG_PATTERN: Final = r"\d{8}"

# appimagetool/type2-runtime use different architecture tags than
# python-build-standalone for the same physical hardware (e.g. "armhf"
# rather than "armv7").
_APPIMAGETOOL_ARCH_MAP: Final[dict[str, str]] = {
    "x86_64": "x86_64",
    "aarch64": "aarch64",
    "armv7l": "armhf",
}


def _appimagetool_cache_path(build_dir: Path, arch: str) -> Path:
    """Return the conventional build-cache path for a resolved appimagetool binary.

    The one place encoding this filename convention - callers that need to
    know where appimagetool would be cached without actually resolving it
    (``build()``, ``init``, and ``check()``'s ``verify_downloads``
    prediction) all call this instead of reconstructing the f-string
    themselves. Note *arch* is the raw ``platform.machine()`` value, not
    run through ``_APPIMAGETOOL_ARCH_MAP`` - that mapping only affects
    which GitHub release asset gets downloaded, not the local cache
    filename.
    """
    return build_dir / f"appimagetool-{arch}.AppImage"


def _runtime_cache_path(build_dir: Path, arch: str) -> Path:
    """Return the conventional build-cache path for a resolved AppImage runtime stub.

    See ``_appimagetool_cache_path`` - same rationale, same raw-``arch``
    convention.
    """
    return build_dir / f"runtime-{arch}"


# AppImage/AppImageKit's classic C appimagetool is no longer maintained (its
# bundled mksquashfs has a documented non-deterministic multi-threaded
# compression bug, see https://github.com/AppImage/AppImageKit/issues/929)
# and its own release notes now point downloads at this successor instead.
# It bundles a modern, fixed squashfs-tools and - unlike AppImageKit's
# "continuous" release - publishes a sha256 digest per asset via the
# GitHub API, so it doubles as the source for the free-verification
# digest used when appimagetool_sha256 is not explicitly configured.
_APPIMAGETOOL_REPO: Final = "AppImage/appimagetool"
_APPIMAGETOOL_ASSET: Final = "appimagetool-{arch}.AppImage"

# The runtime ELF stub that appimagetool prepends to the squashfs image.
# Newer appimagetool versions download this at packaging time instead of
# bundling it, which both defeats verification (nothing checks what was
# fetched) and hangs in network environments where the tool's bundled
# libcurl can't complete the download (e.g. behind certain TLS-intercepting
# proxies) - pre-fetching and pinning it here avoids both problems.
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


# Byte sequences that only turn up in a build of the classic, unmaintained
# probonopd/AppImageKit appimagetool - never shipped stripped, so its own C
# source tree's absolute paths leak into the binary as debug-info strings.
# Checked as a set (any one is enough - each was individually confirmed to
# produce zero hits against a real AppImage/appimagetool build), not a
# single bare "/AppImageKit/" substring: the current default's own
# ``--help`` text incidentally links to ``github.com/AppImage/AppImageKit``'s
# wiki once, which a looser check would misidentify.
_APPIMAGEKIT_BUILD_PATH_MARKERS: Final[tuple[bytes, ...]] = (
    b"/AppImageKit/build/",
    b"/AppImageKit/lib/",
    b"libappimage_hashlib",
    b"libappimage_shared",
    b"squashfuse-EXTERNAL",
)


def _looks_like_classic_appimagekit(tool: Path) -> str | None:
    """Best-effort check for the classic, unmaintained AppImageKit ``appimagetool``.

    Its bundled ``mksquashfs`` has a documented non-deterministic
    multi-threaded compression bug (AppImageKit#929) - the reason this
    project's own default download switched to the ``AppImage/appimagetool``
    fork instead (see ``_APPIMAGETOOL_REPO``). Hash-pinning
    (``appimagetool_sha256``) only proves *which exact file* is in use, not
    that it's the *right* one: an explicitly configured or cached binary
    that happens to be this classic build gets faithfully pinned and
    "verified" on every later build, silently defeating byte-for-byte
    reproducibility the whole time - confirmed by hand: an otherwise fully
    pinned, ``--reproducible`` build still produced two different
    ``.AppImage`` files across two runs, traced back to exactly this
    (found back when this project's own resolution still searched ``PATH``
    by default - since removed for exactly this reason, see
    ``_locate_appimagetool``).

    Neither signal here is individually authoritative on its own - a future
    release of either project could change - so two independent ones are
    combined:

    - Debug-info strings leaking the classic build's own absolute source
      paths (see ``_APPIMAGEKIT_BUILD_PATH_MARKERS``).
    - The ``--version`` banner's own wording: the classic build's template
      says ``"(commit ...)"``; the current default's says
      ``"(git version ...)"``. A runtime string, not a debug symbol, so it
      would survive the classic build being stripped in some future release
      even though today's isn't.

    Neither signal fires on the current default fork (verified against a
    real build of each), but this is still a heuristic - its caller
    (``_abort_if_classic_appimagekit``) treats a match as build-blocking, so
    a false positive would refuse a legitimate build. Deliberate trade-off:
    a silent non-deterministic build is worse than an occasional false
    abort someone has to work around (see ``docs/reproducible-builds.md``'s
    "Classic appimagetool detected" section for the fix).

    Returns
    -------
    str | None
        A short, human-readable reason if *tool* looks like the classic
        build, else ``None``.

    """
    try:
        content = tool.read_bytes()
    except OSError:
        return None

    if any(marker in content for marker in _APPIMAGEKIT_BUILD_PATH_MARKERS):
        return "its own AppImageKit build paths are embedded in the binary"

    try:
        version = _appimagetool_version_string(tool)
    except OSError:
        return None
    if "(commit " in version and "git version" not in version:
        return f"its --version banner reads {version!r}"

    return None


_CLASSIC_APPIMAGEKIT_DOC_URL: Final = (
    "https://appimage.readthedocs.io/en/latest/"
    "reproducible-builds.html#classic-appimagetool-detected"
)


def _abort_if_classic_appimagekit(tool: Path) -> None:
    """Raise if *tool* looks like the classic, unmaintained AppImageKit appimagetool.

    Kept short and pointed at the docs rather than explained in full here -
    see ``_looks_like_classic_appimagekit`` and
    ``docs/reproducible-builds.md``'s "Classic appimagetool detected"
    section for the full reasoning and the actual fix steps.
    """
    if reason := _looks_like_classic_appimagekit(tool):
        # Logged for troubleshooting only (below the CLI's default INFO
        # level) - how this was detected isn't the user's problem, only
        # that it was and how to fix it, which the raised message covers.
        _log.debug("%s: %s", tool, reason)
        msg = (
            f"{tool} looks like the classic, unmaintained AppImageKit "
            f"appimagetool - known non-deterministic mksquashfs "
            f"(AppImageKit#929). Refusing to build with it. See "
            f"{_CLASSIC_APPIMAGEKIT_DOC_URL} for how to fix this."
        )
        raise RuntimeError(msg)


def _locate_appimagetool(
    resolved: _ResolvedBuild,
    appimagetool_cache: Path,
    arch: str,
) -> tuple[Path, bool, str | None]:
    """Return appimagetool's path, whether it was just downloaded, and its API-published sha256.

    Precedence: explicit config path, then the build cache, then a fresh
    download - see ``_resolve_appimagetool`` for what each means for
    verification. Deliberately never searches ``PATH`` (unlike an earlier
    version of this function): every other resolved external input in this
    project - the bundled Python, the runtime stub - is explicit-config-or-
    download only, and appimagetool searching ``PATH`` was both the odd one
    out and, in practice, exactly how a stray classic AppImageKit build got
    silently picked up on a real machine (see
    ``_looks_like_classic_appimagekit``). An explicit ``appimagetool`` path
    already covers "use a specific binary I already have" without that
    ambiguity.
    """
    source = _resolution_source(resolved.appimagetool, appimagetool_cache)

    if source == "config":
        tool = Path(resolved.appimagetool)
        if not tool.exists():
            msg = f"appimagetool not found: {tool}"
            raise FileNotFoundError(msg)
        _log.info("Using appimagetool: %s", tool)
        return tool, False, None

    if source == "cache":
        _log.info("Using cached appimagetool")
        return appimagetool_cache, False, None

    appimagetool_arch = _APPIMAGETOOL_ARCH_MAP.get(arch, arch)
    asset_name = _APPIMAGETOOL_ASSET.format(arch=appimagetool_arch)
    url, api_sha256, _tag = _fetch_latest_versioned_release_asset_digest(
        _APPIMAGETOOL_REPO,
        _APPIMAGETOOL_VERSION_TAG_PATTERN,
        asset_name,
    )
    _download(url, appimagetool_cache)
    appimagetool_cache.chmod(0o755)
    return appimagetool_cache, True, api_sha256


def _resolve_appimagetool(
    resolved: _ResolvedBuild,
    appimagetool_cache: Path,
    arch: str,
) -> Path:
    """Return the path to appimagetool, downloading if necessary.

    When ``appimagetool_sha256`` is set, the resolved binary is verified
    against it regardless of where it came from (explicit path, build
    cache, or download) - a mismatch aborts the build. Only a binary this
    function downloaded itself is deleted on mismatch, so a retry
    re-downloads cleanly; a user-configured path is never touched. A fresh
    download is additionally auto-verified against the digest GitHub
    publishes for the asset, at no extra network cost, even when
    ``appimagetool_sha256`` is unset. Only a config-path or cache
    resolution with no pin configured is used unverified (with a warning
    logging its actual hash). Unless it was just downloaded
    (by definition from the right source), also checked against
    ``_looks_like_classic_appimagekit`` - a match aborts the build outright,
    regardless of ``appimagetool_sha256``: pinning only proves it's the
    same (known-bad) file every time, not that it produces reproducible
    output.
    """
    tool, downloaded, api_sha256 = _locate_appimagetool(
        resolved,
        appimagetool_cache,
        arch,
    )

    if not downloaded:
        _abort_if_classic_appimagekit(tool)

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
) -> tuple[Path, str | None]:
    """Return the path to the AppImage runtime ELF stub, downloading if necessary.

    Newer appimagetool releases fetch this at packaging time via their own
    bundled libcurl instead of embedding it, which leaves it both unverified
    (nothing checks what was downloaded) and prone to hanging in network
    environments that libcurl can't negotiate (e.g. some TLS-intercepting
    proxies). Resolving and verifying it here, the same way as
    ``_resolve_appimagetool``, closes both gaps and lets it be passed via
    appimagetool's own ``--runtime-file`` flag to skip its live download
    entirely.

    Returns
    -------
    tuple[Path, str | None]
        The runtime file's path, and the release tag it was just resolved
        from - only when freshly downloaded (``None`` for a config path or
        an already-cached file, since there's no tag to report then).
        ``init`` uses the tag as ``runtime_version``, a human-readable label
        alongside ``runtime_sha256`` - the runtime stub has no ``--version``
        of its own the way appimagetool does.

    """
    runtime: Path
    downloaded = False
    api_sha256: str | None = None
    tag: str | None = None

    source = _resolution_source(resolved.runtime_file, runtime_cache)

    if source == "config":
        runtime = Path(resolved.runtime_file)
        if not runtime.exists():
            msg = f"runtime file not found: {runtime}"
            raise FileNotFoundError(msg)
        _log.info("Using runtime file: %s", runtime)
    elif source == "cache":
        runtime = runtime_cache
        _log.info("Using cached runtime file")
    else:
        runtime_arch = _APPIMAGETOOL_ARCH_MAP.get(arch, arch)
        asset_name = _RUNTIME_ASSET.format(arch=runtime_arch)
        url, api_sha256, tag = _fetch_latest_versioned_release_asset_digest(
            _RUNTIME_REPO,
            _RUNTIME_VERSION_TAG_PATTERN,
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

    return runtime, tag
