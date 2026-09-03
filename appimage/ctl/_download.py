# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""Low-level download, hashing, and sha256-verification helpers."""

import hashlib
import json
import logging
import re
import urllib.request
from pathlib import Path
from typing import Final

_log: Final = logging.getLogger(__name__)

_GITHUB_RELEASES_API: Final = "https://api.github.com/repos/{repo}/releases"


def _download(url: str, dest: Path) -> None:
    """Download *url* to *dest*.

    Parameters
    ----------
    url : str
        Remote URL to fetch.
    dest : Path
        Local destination path.

    """
    _log.info("Downloading %s", dest.name)
    with urllib.request.urlopen(url) as resp:  # noqa: S310  # nosec B310
        dest.write_bytes(resp.read())


def _github_api_get(url: str) -> dict[str, object] | list[dict[str, object]]:
    """Fetch and JSON-decode a GitHub REST API response.

    Shared by every GitHub API caller in ``appimage.ctl`` (release-by-tag
    lookups here, and the python-build-standalone release lookup in
    ``_python``) so the request headers only need to be right in one place.
    A single-release endpoint returns a JSON object; a list endpoint (e.g.
    ``/releases``) returns a JSON array - callers know which they asked for.
    """
    req = urllib.request.Request(  # noqa: S310
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310  # nosec B310
        data: dict[str, object] | list[dict[str, object]] = json.loads(resp.read())
    return data


def _asset_sha256(asset: dict[str, object]) -> str | None:
    """Return an asset's sha256 digest from its GitHub API ``digest`` field, if present."""
    digest = asset.get("digest")
    return (
        str(digest).removeprefix("sha256:")
        if isinstance(digest, str) and digest.startswith("sha256:")
        else None
    )


def _asset_from_release(
    release: dict[str, object],
    asset_name: str,
) -> tuple[str, str | None] | None:
    """Return an asset's download URL and sha256 digest from an already-fetched release."""
    assets: list[dict[str, object]] = release.get("assets", [])  # type: ignore[assignment]
    for asset in assets:
        if asset.get("name") == asset_name:
            return str(asset["browser_download_url"]), _asset_sha256(asset)
    return None


def _fetch_latest_versioned_release_asset_digest(
    repo: str,
    tag_pattern: str,
    asset_name: str,
) -> tuple[str, str | None, str]:
    """Return an asset's URL, sha256, and tag from the newest *versioned* release.

    Deliberately doesn't take a fixed tag - lists every release and picks
    the most recently published one whose tag matches *tag_pattern*,
    skipping rolling tags like ``"continuous"`` (reused release, assets
    overwritten on every rebuild -
    a hash pinned against it today can become permanently unfetchable once
    upstream cuts a new one, since GitHub doesn't retain the overwritten
    bytes). A tag matching *tag_pattern* is - by convention on both
    ``AppImage/appimagetool`` (semver, e.g. ``"1.9.1"``) and
    ``AppImage/type2-runtime`` (dated, e.g. ``"20251108"``) - a real,
    immutable release that's never reused for a later build.

    Parameters
    ----------
    repo : str
        GitHub repository as ``owner/name``.
    tag_pattern : str
        Regex a genuine version tag must fully match (``re.fullmatch``).
    asset_name : str
        Exact asset filename to look up within the matched release.

    Returns
    -------
    tuple[str, str | None, str]
        Direct download URL, its sha256 hex digest if published, and the
        resolved tag name.

    Raises
    ------
    RuntimeError
        If no release tag matches *tag_pattern*, or the named asset isn't
        in the newest matching one.

    """
    releases = _github_api_get(_GITHUB_RELEASES_API.format(repo=repo))
    assert isinstance(releases, list)  # noqa: S101  # the releases list endpoint always returns a list
    candidates = [
        r
        for r in releases
        if isinstance(tag := r.get("tag_name"), str) and re.fullmatch(tag_pattern, tag)
    ]
    if not candidates:
        msg = f"No release tag matching {tag_pattern!r} found in {repo}"
        raise RuntimeError(msg)
    newest = max(candidates, key=lambda r: str(r.get("published_at", "")))
    tag = str(newest["tag_name"])

    found = _asset_from_release(newest, asset_name)
    if found is None:
        msg = f"Asset {asset_name!r} not found in {repo}@{tag}"
        raise RuntimeError(msg)
    url, sha256 = found
    return url, sha256, tag


def _resolution_source(explicit: str, cache_path: Path) -> str:
    """Classify which branch a ``_resolve_*``/``_locate_*`` function would take.

    Returns ``"config"`` (an explicit path is configured), ``"cache"`` (no
    explicit path, but *cache_path* already exists), or ``"download"``
    (neither - a fresh download would happen). Pure and I/O-limited to a
    single existence check (no hashing, no network) so it's cheap enough to
    call from ``check()`` too, not just from an actual build.

    This is the one place encoding "explicit config path, then the build
    cache, then a download" precedence - every ``_resolve_*``/``_locate_*``
    function in ``appimage.ctl`` (appimagetool, the runtime stub, the
    Python archive) calls this rather than re-implementing the same
    if/elif/else, and ``check()`` calls it too to predict, without
    downloading anything, whether ``verify_downloads`` would actually abort
    a real build for a given artifact: only ``"config"``/``"cache"`` can
    ever be used unverified without a pin - a ``"download"`` is always
    auto-verified against the digest GitHub publishes for the asset. Kept
    in sync with the real resolvers automatically, since there's only one
    implementation of the precedence to drift from.
    """
    if explicit:
        return "config"
    if cache_path.exists():
        return "cache"
    return "download"


def _sha256_file(path: Path) -> str:
    """Return the lowercase hex sha256 digest of *path*."""
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_sha256(path: Path, expected: str, *, label: str) -> None:
    """Raise ``RuntimeError`` if *path* does not match the *expected* sha256.

    Parameters
    ----------
    path : Path
        File to hash and verify.
    expected : str
        Expected sha256 hex digest (an optional ``sha256:`` prefix is
        stripped, matching the format GitHub's Releases API uses).
    label : str
        Human-readable name of the file, used in the error message.

    Raises
    ------
    RuntimeError
        If the computed digest does not match *expected*.

    """
    actual = _sha256_file(path)
    if actual.lower() != expected.lower().removeprefix("sha256:"):
        msg = (
            f"{label} sha256 mismatch for {path}: "
            f"expected {expected}, got {actual}. A freshly downloaded file "
            "is removed automatically so a retry re-downloads cleanly; a "
            "locally configured or already-cached file is left as-is - "
            "remove it yourself and retry, or correct the configured hash."
        )
        raise RuntimeError(msg)
    _log.info("%s sha256 verified: %s", label, actual)


def _require_or_warn_unverified(
    path: Path,
    *,
    label: str,
    config_key: str,
    strict: bool,
) -> None:
    """Handle a resolved file with no hash available to verify it against.

    Parameters
    ----------
    path : Path
        The resolved (but unverified) file.
    label : str
        Human-readable name of the file, used in the message.
    config_key : str
        Name of the ``[tool.appimage]`` key that would pin it.
    strict : bool
        ``resolved.verify_downloads`` - when true, raise instead of warn.

    Raises
    ------
    RuntimeError
        If *strict* is true.

    """
    digest = _sha256_file(path)
    if strict:
        msg = (
            f"{label} could not be verified: {path} (sha256 {digest}). "
            f'Set {config_key} = "{digest}" in [tool.appimage], '
            "or unset verify_downloads to allow unverified downloads."
        )
        raise RuntimeError(msg)
    _log.warning(
        '%s is unpinned and unverified (%s, sha256 %s). Pin it with: %s = "%s" '
        "in [tool.appimage].",
        label,
        path,
        digest,
        config_key,
        digest,
    )
