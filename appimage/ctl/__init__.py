# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""Build an AppImage from a Python project configured via pyproject.toml."""

import csv
import hashlib
import importlib.metadata
import importlib.resources
import json
import logging
import os
import platform
import re
import shutil
import subprocess  # nosec B404
import tarfile
import tempfile
import tomllib
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

_log: Final = logging.getLogger(__name__)
_DEFAULT_ICON: Final = Path(__file__).parent.parent / "assets" / "default_icon.svg"

_ARCH_MAP: Final[dict[str, str]] = {
    "x86_64": "x86_64",
    "aarch64": "aarch64",
    "armv7l": "armv7",
}

# appimagetool/type2-runtime use different architecture tags than
# python-build-standalone for the same physical hardware (e.g. "armhf"
# rather than "armv7").
_APPIMAGETOOL_ARCH_MAP: Final[dict[str, str]] = {
    "x86_64": "x86_64",
    "aarch64": "aarch64",
    "armv7l": "armhf",
}

_PBS_API: Final = (
    "https://api.github.com/repos/astral-sh/python-build-standalone/releases"
)

# Used to suggest an update_info value from [project.urls] — matches only a
# bare repository root (no /issues, /blob/... paths), since those aren't
# valid gh-releases-zsync targets.
_GITHUB_REPO_PATTERN: Final = re.compile(
    r"^https?://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
)
_PREFERRED_URL_KEYS: Final = ("source", "source code", "repository", "github", "code")

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

_GITHUB_RELEASE_TAG_API: Final = (
    "https://api.github.com/repos/{repo}/releases/tags/{tag}"
)

# TODO(manfred-kaiser): the only network call in this module that hardcodes a  # noqa: TD003, FIX002
# specific host instead of going through pip's own index-selection
# machinery (PIP_INDEX_URL / pip.conf / .netrc) like every pip/pip lock
# subprocess call elsewhere here. A private index that mirrors PyPI
# internally but blocks pypi.org itself would make this lookup fail even
# though the package is perfectly reachable — it degrades to a warning
# (or a hard error under verify_downloads) rather than blocking the
# build, but revisit if that turns out to bite real users. There's no
# clean way to ask pip itself for a not-yet-installed package's hash
# without a full 'pip download'/'pip lock' round trip.
_PYPI_JSON_API: Final = "https://pypi.org/pypi/{name}/{version}/json"

_ICON_SEARCH_DIRS: Final = (".", "appimage", "assets", "packaging", "data", "icons")
_ICON_EXTENSIONS: Final = (".png", ".svg")
_DESKTOP_SEARCH_DIRS: Final = (".", "appimage", "assets", "packaging", "data")

_pkg = importlib.resources.files("appimage.ctl")
_APPRUN_TEMPLATE: Final = (_pkg / "templates" / "AppRun.sh").read_text(encoding="utf-8")
_DESKTOP_TEMPLATE: Final = (_pkg / "templates" / "desktop.template").read_text(
    encoding="utf-8",
)


@dataclass
class BuildConfig:
    """Explicit build configuration from ``[tool.appimage]``.

    All fields are optional. Missing fields are resolved automatically from
    ``[project]`` metadata during the build.

    Attributes
    ----------
    app : str | None
        Application name used as the AppImage filename prefix.
        Defaults to ``project.name``.
    entry_point : str | None
        Console script entry point for AppRun.
        Defaults to the matching or sole entry in ``project.scripts``.
    extras : list[str]
        Extras to install from the current package (e.g. ``["production"]``
        installs ``".[production]"``).
    packages : list[str]
        Additional pip install targets beyond the current package.
    python : str | None
        Python minor version to bundle (e.g. ``"3.11"``).
        Defaults to the minimum version from ``requires-python``.
    python_date : str
        python-build-standalone release date for reproducible builds.
        Empty string resolves the latest release.
    icon : str | None
        Path to icon file relative to project root.
        Auto-detected when omitted.
    desktop : str | None
        Path to ``.desktop`` file relative to project root.
        Auto-detected or generated when omitted.
    apprun : str
        Path to a custom AppRun script. Generated from template when empty.
    build_dir : str
        Directory for intermediate build artefacts (default: ``"build"``).
    dist_dir : str
        Directory for the finished AppImage (default: ``"dist"``).
    update_info : str
        Update information string passed to appimagetool via ``-u``.
    env : dict[str, str]
        Extra environment variables exported in the generated AppRun script.
    extra_files : dict[str, str]
        Additional files/directories to copy into AppDir.
        Keys are source paths; values are destinations relative to AppDir.
    hooks : dict[str, str]
        Lifecycle hook scripts. Supported keys: ``post_install``, ``pre_package``.
    appimagetool : str
        Path to a local appimagetool binary. When empty, the tool is looked up
        in ``PATH``, then in the build cache, and finally downloaded.
    appimagetool_version : str
        Informational label recording which appimagetool build
        ``appimagetool_sha256`` corresponds to (e.g. its own ``--version``
        banner). Not used to select a download — AppImageKit's ``continuous``
        release has no addressable historical versions — purely a
        human-readable record of what the pinned hash means. Written
        automatically by ``init`` alongside the hash.
    appimagetool_sha256 : str
        Expected sha256 of the appimagetool binary. When set, verified
        against whichever binary is resolved (explicit path, ``PATH``, build
        cache, or download) — a mismatch aborts the build. When empty,
        appimagetool is used unverified, whatever is currently resolved.
    python_archive : str
        Path to a local python-build-standalone tarball. When empty, the
        archive is looked up in the build cache and then downloaded.
    python_sha256 : str
        Expected sha256 of the python-build-standalone tarball. Fresh
        downloads are also verified against the digest GitHub publishes per
        release asset even when this is empty; set explicitly to also verify
        a local ``python_archive`` or a cached tarball.
    python_dir : str
        Path to an already-extracted Python distribution directory, copied
        into ``AppDir/python`` directly instead of extracting a tarball.
        Config-only — deliberately has no CLI override, since setting it is
        meant to be a considered, committed-to-``pyproject.toml`` decision,
        not a one-off flag. There is no single archive file left to hash by
        the time a directory exists, so this is used exactly as given, with
        no verification and no interaction with ``python_archive``/
        ``python_date``/``python_sha256`` (set at most one of ``python_dir``
        or ``python_archive``). Not a gap in the tool's own verification —
        it exists for a directory whose *provenance* was already verified
        elsewhere (e.g. via ``uv python install`, or via ``python_archive``
        + ``python_sha256`` on a prior run) and is now trusted as a fixed,
        reproducible-by-construction input in its own right: the same path
        yields the same bytes every time, same as pointing ``appimagetool``/
        ``runtime_file`` at a local path already does elsewhere in this
        tool. ``reproducible`` accepts ``python_dir`` in place of
        ``python_date`` for exactly this reason — but ``check``'s
        reproducibility checklist marks it as a *trusted, unverified*
        pin rather than showing it identically to a hash-checked one, since
        that trust is asserted by you, not established by this tool.
    appimage_version : str
        Exact version of the ``appimage`` runtime module — the one AppRun
        and the ``--python-*`` flags depend on — to install into the
        bundled site-packages, regardless of the packaged project's own
        declared dependencies. Empty resolves to the version of
        ``appimage.ctl`` currently doing the build, which is what pins the
        module to *some* known version even unset — but that's implicit,
        not a committed value in ``pyproject.toml``, so it can silently
        differ between machines running different ``appimage.ctl``
        releases. Set explicitly (``init`` does this automatically) for
        the same reason ``python_date`` is pinned rather than left to
        resolve "latest" fresh every time.
    appimage_sha256 : str
        Expected sha256 of the ``appimage`` wheel for ``appimage_version``.
        When empty, the digest PyPI publishes for that release is looked
        up and used instead (a network call, best-effort — falls back to
        a warning, or a hard error under ``verify_downloads``, if it
        can't be reached) — set explicitly to verify without that lookup,
        e.g. for a fully offline build.
    appimagectl_version : str
        Expected version of ``appimage.ctl`` itself — the tool doing the
        build, not the bundled runtime module (``appimage_version`` above,
        a different concern). Config-only — deliberately no CLI override,
        since the entire point is a committed value to compare *against*,
        not something a single invocation should be able to wave away.
        Empty skips the check (no expectation recorded, so no possible
        drift). Which version of ``appimage.ctl`` actually gets installed
        remains ordinary Python dependency management on your end (pip,
        pipx, a pinned dev-dependency, ...); this only records what that
        was expected to resolve to, so a later drift — a colleague, or CI,
        running a newer or older ``appimage.ctl`` than the project was
        last built with — surfaces as a visible warning (or a hard error
        under ``verify_downloads``) instead of silently changing build
        behavior no other pin here would catch. ``init`` writes the
        currently-running version automatically.
    runtime_file : str
        Path to a local AppImage runtime ELF stub, passed to appimagetool as
        ``--runtime-file``. When empty, it is looked up in the build cache
        and then downloaded — pre-fetching it this way (rather than letting
        appimagetool download it itself at packaging time) makes it
        verifiable and avoids hangs in network environments where
        appimagetool's bundled libcurl cannot complete the download.
    runtime_sha256 : str
        Expected sha256 of the runtime file. Fresh downloads are also
        verified against the digest GitHub publishes per release asset even
        when this is empty.
    verify_downloads : bool
        When true, any of appimagetool, the runtime file, or the Python
        archive that would otherwise be used unverified (no configured hash
        and no digest published by GitHub for that resolution path — e.g. a
        ``PATH``-found or cached binary) aborts the build instead of
        logging a warning and continuing.
    require_zsyncmake : bool
        When true, abort the build if ``update_info`` is set but
        ``zsyncmake`` is not found on ``PATH`` — instead of logging a
        warning and packaging an AppImage with no ``.zsync`` delta-update
        file. Has no effect when ``update_info`` is empty.
    pylock : str
        Path to a PEP 751 ``pylock.toml`` file, relative to the project
        root, pinning every third-party runtime dependency to an exact
        version and sha256 hash. When set, the build installs the local
        project itself (untouched, trusted source) with ``--no-deps``, then
        installs everything else from this file with ``pip install
        --require-hashes`` — a compromised or typosquatted dependency
        pulled in at build time is rejected instead of silently installed.
        Generate it with ``lock`` (see docs/reproducible-builds.md).
    require_pylock : bool
        When true, abort the build if ``pylock`` is not set — instead of
        logging a warning and installing dependencies unverified.
    build_pylock : str
        Path to a hash-pinned PEP 751 ``pylock.toml``-format file, relative
        to the project root, pinning the packaged project's *own*
        ``[build-system].requires`` (e.g. ``setuptools``, ``hatchling``,
        ``poetry-core``) to an exact version and sha256 hash. Installing
        the project from source always triggers a PEP 517 isolated build,
        which otherwise installs that backend fresh from the index,
        unpinned and unverified, into that (throwaway) isolated
        environment, on every build — a gap ``pylock`` does not cover,
        since the local project itself is stripped out of it. When set,
        this file is converted to a classic hash-pinned constraints file
        and passed as ``pip install --build-constraint`` (pylock.toml
        isn't a format ``--build-constraint`` accepts directly), so pip's
        own isolated build environment is still used — just hash-verified
        instead of resolved live, rather than installing the backend into
        the main interpreter with ``--no-build-isolation``, which would
        leave it permanently bundled in the shipped AppImage. Generated by
        ``lock`` alongside ``pylock`` (see
        docs/reproducible-builds.md) — not something hand-written.
    require_build_pylock : bool
        When true, abort the build if ``build_pylock`` is not set —
        instead of logging a warning and installing the build backend
        unverified.
    reproducible : bool
        Shortcut that sets every option needed for a build that is
        reproducible across machines and over time, not just within the
        current build environment: implies ``verify_downloads`` and
        ``require_zsyncmake``, and additionally requires ``python_date``
        (or ``python_dir``), ``appimage_version``, ``appimage_sha256``,
        ``appimagetool_sha256``, and ``runtime_sha256`` to already be set
        — resolving any of those fresh on every build is exactly what
        defeats cross-machine reproducibility (see
        docs/reproducible-builds.md). Run ``init`` first to write them.

    """

    app: str | None = None
    entry_point: str | None = None
    extras: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    python: str | None = None
    python_date: str = ""
    icon: str | None = None
    desktop: str | None = None
    apprun: str = ""
    build_dir: str = "build"
    dist_dir: str = "dist"
    update_info: str = ""
    env: dict[str, str] = field(default_factory=dict)
    extra_files: dict[str, str] = field(default_factory=dict)
    hooks: dict[str, str] = field(default_factory=dict)
    appimagetool: str = ""
    appimagetool_version: str = ""
    appimagetool_sha256: str = ""
    python_archive: str = ""
    python_sha256: str = ""
    python_dir: str = ""
    appimage_version: str = ""
    appimage_sha256: str = ""
    appimagectl_version: str = ""
    runtime_file: str = ""
    runtime_sha256: str = ""
    verify_downloads: bool = False
    require_zsyncmake: bool = False
    pylock: str = ""
    require_pylock: bool = False
    build_pylock: str = ""
    require_build_pylock: bool = False
    reproducible: bool = False

    @classmethod
    def from_pyproject(cls, project_root: Path) -> "BuildConfig":
        """Load explicit configuration from ``[tool.appimage]``.

        Only keys present in the TOML section are applied; everything else
        stays ``None`` and is resolved automatically during the build.

        Parameters
        ----------
        project_root : Path
            Directory containing ``pyproject.toml``.

        Returns
        -------
        BuildConfig
            Partially-populated configuration object.

        Raises
        ------
        FileNotFoundError
            If ``pyproject.toml`` does not exist.

        """
        pyproject = project_root / "pyproject.toml"
        if not pyproject.exists():
            msg = f"pyproject.toml not found in {project_root}"
            raise FileNotFoundError(msg)
        with pyproject.open("rb") as f:
            data = tomllib.load(f)
        cfg = data.get("tool", {}).get("appimage", {})
        return cls(
            app=cfg.get("app"),
            entry_point=cfg.get("entry_point"),
            extras=cfg.get("extras", []),
            packages=cfg.get("packages", []),
            python=cfg.get("python"),
            python_date=cfg.get("python_date", ""),
            icon=cfg.get("icon"),
            desktop=cfg.get("desktop"),
            apprun=cfg.get("apprun", ""),
            build_dir=cfg.get("build_dir", "build"),
            dist_dir=cfg.get("dist_dir", "dist"),
            update_info=cfg.get("update_info", ""),
            env=cfg.get("env", {}),
            extra_files=cfg.get("extra_files", {}),
            hooks=cfg.get("hooks", {}),
            appimagetool=cfg.get("appimagetool", ""),
            appimagetool_version=cfg.get("appimagetool_version", ""),
            appimagetool_sha256=cfg.get("appimagetool_sha256", ""),
            python_archive=cfg.get("python_archive", ""),
            python_sha256=cfg.get("python_sha256", ""),
            python_dir=cfg.get("python_dir", ""),
            appimage_version=cfg.get("appimage_version", ""),
            appimage_sha256=cfg.get("appimage_sha256", ""),
            appimagectl_version=cfg.get("appimagectl_version", ""),
            runtime_file=cfg.get("runtime_file", ""),
            runtime_sha256=cfg.get("runtime_sha256", ""),
            verify_downloads=cfg.get("verify_downloads", False),
            require_zsyncmake=cfg.get("require_zsyncmake", False),
            pylock=cfg.get("pylock", ""),
            require_pylock=cfg.get("require_pylock", False),
            build_pylock=cfg.get("build_pylock", ""),
            require_build_pylock=cfg.get("require_build_pylock", False),
            reproducible=cfg.get("reproducible", False),
        )


@dataclass
class _ResolvedBuild:
    """Fully resolved build parameters, ready to execute."""

    app: str
    entry_point: str
    install_targets: list[str]
    local_install_targets: list[str]
    appimage_pin: str
    appimage_version: str
    appimage_sha256: str
    appimagectl_version: str
    python: str
    python_date: str
    icon: Path | None
    desktop: Path | None
    apprun: str
    build_dir: str
    dist_dir: str
    update_info: str
    update_info_suggested: str
    env: dict[str, str]
    extra_files: dict[str, str]
    hooks: dict[str, str]
    appimagetool: str
    appimagetool_version: str
    appimagetool_sha256: str
    python_archive: str
    python_sha256: str
    python_dir: str
    runtime_file: str
    runtime_sha256: str
    verify_downloads: bool
    require_zsyncmake: bool
    pylock: str
    require_pylock: bool
    build_pylock: str
    require_build_pylock: bool
    reproducible: bool
    sources: dict[str, str]
    # Split by which step each belongs to — AppDir assembly vs. packaging
    # into the final .AppImage — so build_appdir() can enforce only what it
    # actually needs, instead of demanding appimagetool/runtime pins it
    # never touches. build() enforces both; check() shows both.
    appdir_warnings: list[str]
    appdir_errors: list[str]
    package_warnings: list[str]
    package_errors: list[str]


def _detect_entry_point(scripts: dict[str, str], app: str) -> str | None:
    """Return the best-matching entry point from ``project.scripts``.

    Parameters
    ----------
    scripts : dict[str, str]
        Mapping of script name to module:function from ``[project.scripts]``.
    app : str
        Application name used as the preferred match.

    Returns
    -------
    str | None
        Entry point name, or ``None`` if it cannot be determined.

    """
    if not scripts:
        return None
    if app in scripts:
        return app
    if len(scripts) == 1:
        return next(iter(scripts))
    return None


def _python_from_requires(requires: str) -> str:
    """Extract the minimum Python minor version from a ``requires-python`` specifier.

    Parameters
    ----------
    requires : str
        Specifier string such as ``">= 3.11"`` or ``">=3.11,<4"``.

    Returns
    -------
    str
        Minor version string such as ``"3.11"``.

    """
    match = re.search(r"(\d+)\.(\d+)", requires)
    if match:
        return f"{match.group(1)}.{match.group(2)}"
    return "3.11"


def _find_icon(app: str, project_root: Path) -> Path | None:
    """Search common project locations for an application icon.

    Parameters
    ----------
    app : str
        Application name used as the base filename.
    project_root : Path
        Project root to search within.

    Returns
    -------
    Path | None
        Absolute path to the icon file, or ``None`` if not found.

    """
    for dir_name in _ICON_SEARCH_DIRS:
        for ext in _ICON_EXTENSIONS:
            candidate = project_root / dir_name / f"{app}{ext}"
            if candidate.exists():
                return candidate
    return None


def _find_desktop(app: str, project_root: Path) -> Path | None:
    """Search common project locations for a ``.desktop`` file.

    Parameters
    ----------
    app : str
        Application name used as the base filename.
    project_root : Path
        Project root to search within.

    Returns
    -------
    Path | None
        Absolute path to the ``.desktop`` file, or ``None`` if not found.

    """
    for dir_name in _DESKTOP_SEARCH_DIRS:
        candidate = project_root / dir_name / f"{app}.desktop"
        if candidate.exists():
            return candidate
    return None


def _detect_github_repo(project: dict[str, object]) -> tuple[str, str] | None:
    """Find an unambiguous ``owner/repo`` pair from ``[project.urls]``.

    Checks well-known keys first (``source``, ``repository``, ``github``,
    ``code``, case-insensitive); falls back to scanning all url values only
    if exactly one matches the strict bare-repo pattern (no ``/issues``,
    ``/blob/...`` paths — those aren't valid ``gh-releases-zsync`` targets).

    Parameters
    ----------
    project : dict[str, object]
        The ``[project]`` table, which may contain a ``urls`` sub-table.

    Returns
    -------
    tuple[str, str] | None
        ``(owner, repo)`` if an unambiguous match was found, else ``None``.

    """
    urls: dict[str, object] = project.get("urls", {})  # type: ignore[assignment]
    if not urls:
        return None

    lower = {str(k).lower(): str(v) for k, v in urls.items()}
    for key in _PREFERRED_URL_KEYS:
        if key in lower and (match := _GITHUB_REPO_PATTERN.match(lower[key])):
            return match.group(1), match.group(2)

    candidates: set[tuple[str, str]] = {
        (match.group(1), match.group(2))
        for v in urls.values()
        if (match := _GITHUB_REPO_PATTERN.match(str(v)))
    }
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


def _suggest_update_info(
    app: str,
    project: dict[str, object],
    warnings: list[str],
) -> str:
    """Suggest a ``gh-releases-zsync`` ``update_info`` string from ``[project.urls]``.

    Never applied automatically — a wrong guess (private repo, no GitHub
    Releases flow, different asset naming) would embed a plausible-looking
    but broken update pointer into the packaged AppImage. Only surfaced as
    a warning (``check``) and written by ``init``, same as every other
    auto-detected field. Only called when ``update_info`` isn't already set.

    Parameters
    ----------
    app : str
        Resolved application name, used as the AppImage filename prefix.
    project : dict[str, object]
        The ``[project]`` table.
    warnings : list[str]
        Appended to in place with a suggestion message when a repo is
        found — avoids a second return value purely to shuttle it back to
        the caller's own warnings list.

    Returns
    -------
    str
        The suggested string, or an empty string if no unambiguous GitHub
        repo could be identified (most projects don't want zsync updates,
        and silence is correct there — nothing is appended to *warnings*).

    """
    repo = _detect_github_repo(project)
    if repo is None:
        return ""
    owner, name = repo
    arch = platform.machine()
    suggestion = f"gh-releases-zsync|{owner}|{name}|latest|{app}-{arch}.AppImage.zsync"
    warnings.append(
        f"update_info not set — detected GitHub repo {owner}/{name} from "
        f'[project.urls]. Add update_info = "{suggestion}" in '
        "[tool.appimage] to enable zsync delta-updates, or run 'init' "
        "to write it automatically.",
    )
    return suggestion


def _resolve_app(config: BuildConfig, project: dict[str, object]) -> tuple[str, str]:
    """Resolve app name from config or project metadata."""
    if config.app is not None:
        return config.app, "[tool.appimage]"
    if project_name := project.get("name"):
        return str(project_name), "[project] name"
    msg = (
        "Cannot determine app name: set 'app' in [tool.appimage] "
        "or 'name' in [project]"
    )
    raise ValueError(msg)


def _resolve_entry_point(
    config: BuildConfig,
    project: dict[str, object],
    app: str,
) -> tuple[str, str, list[str]]:
    """Resolve entry point from config or project.scripts."""
    if config.entry_point is not None:
        return config.entry_point, "[tool.appimage]", []
    scripts: dict[str, str] = project.get("scripts", {})  # type: ignore[assignment]
    ep = _detect_entry_point(scripts, app)
    if ep is not None:
        return ep, "[project] scripts", []
    error = (
        "Cannot determine entry_point: add it to [tool.appimage] "
        "or define it in [project.scripts]"
    )
    return app, "", [error]


def _resolve_python(
    config: BuildConfig,
    project: dict[str, object],
) -> tuple[str, str]:
    """Resolve Python version from config or requires-python."""
    if config.python is not None:
        return config.python, "[tool.appimage]"
    if requires := project.get("requires-python"):
        return _python_from_requires(str(requires)), "[project] requires-python"
    return "3.11", "default"


def _resolve_icon_path(
    config: BuildConfig,
    project_root: Path,
    app: str,
) -> tuple[Path | None, str, list[str]]:
    """Resolve icon path from config or filesystem search."""
    if config.icon is not None:
        return project_root / config.icon, "[tool.appimage]", []
    icon = _find_icon(app, project_root)
    if icon is not None:
        return icon, f"detected ({icon.relative_to(project_root)})", []
    warning = (
        f"No icon found — add {app}.png to the project root "
        f"or set 'icon' in [tool.appimage]. "
        f"Using the built-in default icon."
    )
    return _DEFAULT_ICON, "default (bundled)", [warning]


def _resolve_desktop_path(
    config: BuildConfig,
    project_root: Path,
    app: str,
) -> tuple[Path | None, str, list[str]]:
    """Resolve desktop file path from config or filesystem search."""
    if config.desktop is not None:
        return project_root / config.desktop, "[tool.appimage]", []
    desktop = _find_desktop(app, project_root)
    if desktop is not None:
        return desktop, f"detected ({desktop.relative_to(project_root)})", []
    warning = (
        f"No .desktop file found — one will be generated from [project] metadata. "
        f"Add {app}.desktop to customise it."
    )
    return None, "will be generated", [warning]


def _resolve(config: BuildConfig, project_root: Path) -> _ResolvedBuild:
    """Resolve all auto-detected fields into a complete build configuration.

    Parameters
    ----------
    config : BuildConfig
        Explicit configuration from ``pyproject.toml``.
    project_root : Path
        Project root directory.

    Returns
    -------
    _ResolvedBuild
        Fully resolved build parameters with source annotations.

    Raises
    ------
    FileNotFoundError
        If ``pyproject.toml`` does not exist.
    ValueError
        If required values cannot be determined automatically.

    """
    pyproject = project_root / "pyproject.toml"
    with pyproject.open("rb") as f:
        data = tomllib.load(f)
    project: dict[str, object] = data.get("project", {})

    sources: dict[str, str] = {}
    appdir_warnings: list[str] = []
    appdir_errors: list[str] = []
    package_warnings: list[str] = []
    package_errors: list[str] = []

    app, sources["app"] = _resolve_app(config, project)
    entry_point, sources["entry_point"], ep_errors = _resolve_entry_point(
        config,
        project,
        app,
    )
    appdir_errors.extend(ep_errors)
    python, sources["python"] = _resolve_python(config, project)
    icon, sources["icon"], icon_warnings = _resolve_icon_path(config, project_root, app)
    appdir_warnings.extend(icon_warnings)
    desktop, sources["desktop"], desktop_warnings = _resolve_desktop_path(
        config,
        project_root,
        app,
    )
    appdir_warnings.extend(desktop_warnings)

    if config.extras:
        extras_str = ",".join(config.extras)
        base = f".[{extras_str}]"
        sources["packages"] = "[tool.appimage] extras"
    else:
        base = "."
        sources["packages"] = "default (.)"

    # The `appimage` runtime module handles entry point dispatch and the
    # `--python-*` flags inside the built AppImage. It must be installed into
    # the bundled site-packages regardless of whether the packaged project
    # declares it as a dependency. Defaults to the currently running build's
    # own version, which keeps AppRun's expectations and the bundled runtime
    # in sync — but that's an implicit pin, not a value committed to
    # pyproject.toml; set appimage_version explicitly (`init` does this) so
    # it doesn't silently vary with whichever appimage.ctl release built it.
    appimage_version = config.appimage_version or importlib.metadata.version("appimage")
    appimage_pin = f"appimage=={appimage_version}"
    install_targets = [appimage_pin, base, *config.packages]

    sources["build_dir"] = (
        "[tool.appimage]" if config.build_dir != "build" else "default"
    )
    sources["dist_dir"] = "[tool.appimage]" if config.dist_dir != "dist" else "default"

    if config.python_archive and config.python_dir:
        appdir_errors.append(
            "Set at most one of python_archive or python_dir in "
            "[tool.appimage] — which one would apply is ambiguous.",
        )

    verify_downloads = config.verify_downloads or config.reproducible
    require_zsyncmake = config.require_zsyncmake or config.reproducible

    if config.appimagectl_version:
        running_version = importlib.metadata.version("appimage")
        if config.appimagectl_version != running_version:
            mismatch_msg = (
                f"appimagectl_version expects {config.appimagectl_version}, but "
                f"{running_version} is actually running — reproducibility may no "
                "longer hold if this build differs from the one that pinned it. "
                "Update appimagectl_version once you've confirmed the new version "
                "still builds the same way, or reinstall the expected version."
            )
            (appdir_errors if verify_downloads else appdir_warnings).append(
                mismatch_msg,
            )

    if config.reproducible:
        if not config.python_dir and not config.python_date:
            appdir_errors.append(
                "reproducible requires python_date (or python_dir) to be set "
                "in [tool.appimage] — run 'init' to resolve and write it.",
            )
        appdir_errors.extend(
            f"reproducible requires {key} to be set in "
            "[tool.appimage] — run 'init' to resolve and write it."
            for key in ("appimage_version", "appimage_sha256")
            if not getattr(config, key)
        )
        package_errors.extend(
            f"reproducible requires {key} to be set in "
            "[tool.appimage] — run 'init' to resolve and write it."
            for key in ("appimagetool_sha256", "runtime_sha256")
            if not getattr(config, key)
        )

    if config.update_info and not shutil.which("zsyncmake"):
        zsyncmake_msg = (
            "update_info is set but zsyncmake is not on PATH — no .zsync "
            "delta-update file will be generated. Install the 'zsync' package "
            "(provides zsyncmake), or unset update_info."
        )
        (package_errors if require_zsyncmake else package_warnings).append(
            zsyncmake_msg,
        )

    if not config.pylock:
        pylock_msg = (
            "No pylock configured — third-party dependencies are installed "
            "without hash verification. Run 'lock' to generate pylock.toml, "
            'then set pylock = "pylock.toml" in [tool.appimage].'
        )
        (appdir_errors if config.require_pylock else appdir_warnings).append(pylock_msg)

    if not config.build_pylock:
        build_pylock_msg = (
            "No build_pylock configured — the packaged project's own "
            "build backend (declared in its [build-system].requires) is "
            "installed without hash verification into the isolated build "
            "environment. Run 'lock' to generate it alongside pylock.toml."
        )
        (appdir_errors if config.require_build_pylock else appdir_warnings).append(
            build_pylock_msg,
        )

    return _ResolvedBuild(
        app=app,
        entry_point=entry_point,
        install_targets=install_targets,
        local_install_targets=[base],
        appimage_pin=appimage_pin,
        appimage_version=config.appimage_version,
        appimage_sha256=config.appimage_sha256,
        appimagectl_version=config.appimagectl_version,
        python=python,
        python_date=config.python_date,
        icon=icon,
        desktop=desktop,
        apprun=config.apprun,
        build_dir=config.build_dir,
        dist_dir=config.dist_dir,
        update_info=config.update_info,
        update_info_suggested=(
            ""
            if config.update_info
            else _suggest_update_info(app, project, package_warnings)
        ),
        env=config.env,
        extra_files=config.extra_files,
        hooks=config.hooks,
        appimagetool=config.appimagetool,
        appimagetool_version=config.appimagetool_version,
        appimagetool_sha256=config.appimagetool_sha256,
        python_archive=config.python_archive,
        python_sha256=config.python_sha256,
        python_dir=config.python_dir,
        runtime_file=config.runtime_file,
        runtime_sha256=config.runtime_sha256,
        verify_downloads=verify_downloads,
        require_zsyncmake=require_zsyncmake,
        pylock=config.pylock,
        require_pylock=config.require_pylock,
        build_pylock=config.build_pylock,
        require_build_pylock=config.require_build_pylock,
        reproducible=config.reproducible,
        sources=sources,
        appdir_warnings=appdir_warnings,
        appdir_errors=appdir_errors,
        package_warnings=package_warnings,
        package_errors=package_errors,
    )


def _icon_display(icon: Path | None) -> str:
    """Return a display-friendly path string for an icon."""
    if not icon:
        return "NOT FOUND"
    if icon.is_relative_to(Path.cwd()):
        return str(icon.relative_to(Path.cwd()))
    return str(icon)


def _optional_check_rows(resolved: _ResolvedBuild) -> list[tuple[str, str, str]]:
    """Return extra rows for optional config fields that are set."""
    cfg = "[tool.appimage]"
    candidates = [
        ("apprun", resolved.apprun),
        ("update_info", resolved.update_info),
        ("python_date", resolved.python_date),
        ("python_archive", resolved.python_archive),
        ("python_sha256", resolved.python_sha256),
        ("python_dir", resolved.python_dir),
        ("appimage_version", resolved.appimage_version),
        ("appimage_sha256", resolved.appimage_sha256),
        ("appimagectl_version", resolved.appimagectl_version),
        ("appimagetool", resolved.appimagetool),
        ("appimagetool_version", resolved.appimagetool_version),
        ("appimagetool_sha256", resolved.appimagetool_sha256),
        ("runtime_file", resolved.runtime_file),
        ("runtime_sha256", resolved.runtime_sha256),
        ("pylock", resolved.pylock),
        ("build_pylock", resolved.build_pylock),
    ]
    rows = [(name, value, cfg) for name, value in candidates if value]
    if resolved.reproducible:
        rows.append(("reproducible", "true", cfg))
    if resolved.verify_downloads:
        rows.append(("verify_downloads", "true", cfg))
    if resolved.require_zsyncmake:
        rows.append(("require_zsyncmake", "true", cfg))
    if resolved.require_pylock:
        rows.append(("require_pylock", "true", cfg))
    if resolved.require_build_pylock:
        rows.append(("require_build_pylock", "true", cfg))
    return rows


_RUNTIME_MODULE_REPRODUCIBILITY_PINS: Final = ("appimage_version", "appimage_sha256")
_PACKAGE_REPRODUCIBILITY_PINS: Final = ("appimagetool_sha256", "runtime_sha256")


def _reproducibility_summary(resolved: _ResolvedBuild) -> list[str]:
    """Return a checklist of the independent pinning stories.

    Unlike the individual warnings above, this always reflects the current
    state — not just when ``reproducible``/``require_pylock``/
    ``require_build_pylock`` are set and something is missing. Without
    it, a plain ``check`` gives no signal at all about the reproducibility
    pins: they only ever surface as a warning deep inside a real ``build()``
    run (when appimagetool/runtime/python are actually resolved) or as a
    hard error once ``reproducible`` is already turned on — nothing in
    between.

    AppDir and packaging reproducibility are reported as two separate
    lines, matching ``build_appdir()``/``build()``'s own split of which
    pins each actually needs (see ``_scrub_build_paths``/``build_appdir``).
    """
    appdir_ready = bool(resolved.python_date or resolved.python_dir)
    if resolved.python_dir:
        appdir_line = (
            f"AppDir reproducibility: python_dir set ({resolved.python_dir}) "
            "— trusted directory, not hash-verified"
        )
    elif appdir_ready:
        appdir_line = "AppDir reproducibility: python_date set"
    else:
        appdir_line = (
            "AppDir reproducibility: python_date not set — run 'init' to "
            "resolve and pin it, or set python_dir"
        )

    runtime_module_pinned = [
        key for key in _RUNTIME_MODULE_REPRODUCIBILITY_PINS if getattr(resolved, key)
    ]
    runtime_module_ready = len(runtime_module_pinned) == len(
        _RUNTIME_MODULE_REPRODUCIBILITY_PINS,
    )
    if runtime_module_ready:
        runtime_module_line = (
            "Runtime module reproducibility: appimage_version, appimage_sha256 set"
        )
    else:
        missing_runtime = ", ".join(
            key
            for key in _RUNTIME_MODULE_REPRODUCIBILITY_PINS
            if not getattr(resolved, key)
        )
        runtime_module_line = (
            f"Runtime module reproducibility: {missing_runtime} not set — run "
            "'init' to resolve and pin them"
        )

    package_pinned = [
        key for key in _PACKAGE_REPRODUCIBILITY_PINS if getattr(resolved, key)
    ]
    package_ready = len(package_pinned) == len(_PACKAGE_REPRODUCIBILITY_PINS)
    if package_ready:
        package_line = (
            "Packaging reproducibility: appimagetool_sha256, runtime_sha256 set"
        )
    else:
        missing = ", ".join(
            key for key in _PACKAGE_REPRODUCIBILITY_PINS if not getattr(resolved, key)
        )
        package_line = (
            f"Packaging reproducibility: {missing} not set — run 'init' to "
            "resolve and pin them"
        )

    pylock_ready = bool(resolved.pylock)
    pylock_line = (
        f"Dependency verification: pylock set ({resolved.pylock})"
        if pylock_ready
        else "Dependency verification: pylock not set — run 'lock' to generate pylock.toml"
    )

    build_pylock_ready = bool(resolved.build_pylock)
    build_pylock_line = (
        f"Build backend verification: build_pylock set ({resolved.build_pylock})"
        if build_pylock_ready
        else "Build backend verification: build_pylock not set — run 'lock' "
        "to generate it alongside pylock.toml"
    )

    ready = [
        appdir_ready,
        runtime_module_ready,
        package_ready,
        pylock_ready,
        build_pylock_ready,
    ]
    header = f"Reproducibility checklist ({sum(ready)}/{len(ready)} ready):"
    marks = ["✓" if r else "✗" for r in ready]
    lines = [
        appdir_line,
        runtime_module_line,
        package_line,
        pylock_line,
        build_pylock_line,
    ]
    return [
        header,
        *(f"  {mark} {line}" for mark, line in zip(marks, lines, strict=True)),
    ]


def _format_check(resolved: _ResolvedBuild) -> None:
    """Log a human-readable configuration report.

    Parameters
    ----------
    resolved : _ResolvedBuild
        Resolved build configuration to report.

    """
    _log.info("Build configuration:")

    rows: list[tuple[str, str, str]] = [
        ("app", resolved.app, resolved.sources.get("app", "")),
        ("entry_point", resolved.entry_point, resolved.sources.get("entry_point", "")),
        ("python", resolved.python, resolved.sources.get("python", "")),
        (
            "packages",
            " ".join(resolved.install_targets),
            resolved.sources.get("packages", ""),
        ),
        ("icon", _icon_display(resolved.icon), resolved.sources.get("icon", "")),
        (
            "desktop",
            (
                str(resolved.desktop.relative_to(Path.cwd()))
                if resolved.desktop
                else "(generated)"
            ),
            resolved.sources.get("desktop", ""),
        ),
        ("build_dir", resolved.build_dir, resolved.sources.get("build_dir", "")),
        ("dist_dir", resolved.dist_dir, resolved.sources.get("dist_dir", "")),
        *_optional_check_rows(resolved),
    ]

    for name, value, source in rows:
        _log.info("  %-15s %-35s [%s]", f"{name}:", value, source)

    _log.info("")
    for line in _reproducibility_summary(resolved):
        _log.info("  %s", line)

    warnings = resolved.appdir_warnings + resolved.package_warnings
    if warnings:
        _log.info("")
        for w in warnings:
            _log.warning("  Warning: %s", w)

    errors = resolved.appdir_errors + resolved.package_errors
    if errors:
        _log.info("")
        for e in errors:
            _log.error("  Error:   %s", e)


def _toml_value(v: object) -> str:
    """Serialise a Python value to its TOML representation.

    Parameters
    ----------
    v : object
        Value to serialise (str, list of str, or other).

    Returns
    -------
    str
        TOML-formatted value string.

    """
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, list):
        parts = ", ".join(_toml_value(i) for i in v)
        return f"[{parts}]"
    return str(v)


def check(config: BuildConfig, project_root: Path) -> bool:
    """Resolve and print the build configuration.

    Parameters
    ----------
    config : BuildConfig
        Explicit configuration from ``pyproject.toml``.
    project_root : Path
        Project root directory.

    Returns
    -------
    bool
        ``True`` if the configuration is complete and the build can proceed.

    """
    resolved = _resolve(config, project_root)
    _format_check(resolved)
    return not (resolved.appdir_errors or resolved.package_errors)


def _appimagetool_version_string(tool: Path) -> str:
    """Return appimagetool's own ``--version`` banner as a human-readable label."""
    result = subprocess.run(  # noqa: S603  # nosec B603
        [str(tool), "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (result.stderr or result.stdout).strip()


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


def _replace_or_append_toml_fields(pyproject_path: Path, new: dict[str, object]) -> None:
    """Write *new* key/value pairs into ``[tool.appimage]``, overwriting existing lines.

    The opposite of ``write_config``'s insertion, which only ever adds
    missing keys and never touches an existing one — this is what
    ``update_tools`` needs to move pins forward instead of filling gaps.
    Scoped strictly to the ``[tool.appimage]`` section's own scalar lines,
    stopping at the next ``[`` header (a subtable like
    ``[tool.appimage.env]``, or an unrelated table), so a same-named key
    elsewhere is never touched.
    """
    header = "[tool.appimage]"
    content = pyproject_path.read_text()
    if header not in content:
        lines = "\n".join(f"{k} = {_toml_value(v)}" for k, v in new.items())
        pyproject_path.write_text(content + f"\n{header}\n{lines}\n")
        return

    start = content.index(header) + len(header)
    next_header = re.search(r"^\[", content[start:], re.MULTILINE)
    end = start + next_header.start() if next_header else len(content)
    section = content[start:end]

    remaining = dict(new)

    def _replace_line(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in remaining:
            return f"{key} = {_toml_value(remaining.pop(key))}"
        return match.group(0)

    section = re.sub(r"^([A-Za-z_][A-Za-z0-9_]*) = .*$", _replace_line, section, flags=re.MULTILINE)
    if remaining:
        addition = "\n".join(f"{k} = {_toml_value(v)}" for k, v in remaining.items())
        section = section.rstrip("\n") + f"\n{addition}\n"

    pyproject_path.write_text(content[:start] + section + content[end:])


def update_tools(config: BuildConfig, project_root: Path) -> None:
    """Move every toolchain pin forward to whatever's currently available.

    Refreshes ``python_date``/``python_sha256``, ``appimage_version``/
    ``appimage_sha256``, ``appimagetool_version``/``appimagetool_sha256``,
    ``runtime_sha256``, and ``appimagectl_version`` unconditionally,
    overwriting whatever's already configured — the same "move pins
    forward" role ``packaging/update-requirements.sh --upgrade`` plays for
    this project's own build-backend pin, applied here to appimage.ctl's
    own toolchain. Never touches ``pylock``/``build_pylock`` (already
    regenerated on every ``lock`` run regardless of what's configured) or
    project metadata (``app``/``entry_point``/``icon``/``desktop``) — this
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
    _format_check(resolved)

    new = _pinned_download_fields(resolved, project_root, existing=set())

    pyproject_path = project_root / "pyproject.toml"
    _replace_or_append_toml_fields(pyproject_path, new)

    _log.info("")
    _log.info("Updated in pyproject.toml:")
    for k, v in new.items():
        _log.info("  %s = %s", k, _toml_value(v))


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
        asset (``None`` otherwise), and the release's own date tag —
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

    req = urllib.request.Request(  # noqa: S310
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310  # nosec B310
        release: dict[str, object] = json.loads(resp.read())

    resolved_date = str(release.get("tag_name", date or "latest"))
    assets: list[dict[str, object]] = release.get("assets", [])  # type: ignore[assignment]
    for asset in assets:
        url = str(asset["browser_download_url"])
        if (
            f"cpython-{python}." in url
            and f"{pbs_arch}-unknown-linux-gnu-install_only_stripped" in url
            and "freethreaded" not in url
        ):
            digest = asset.get("digest")
            sha256 = (
                str(digest).removeprefix("sha256:")
                if isinstance(digest, str) and digest.startswith("sha256:")
                else None
            )
            return url, sha256, resolved_date

    msg = f"No Python {python} asset found for {pbs_arch} in release {resolved_date}"
    raise RuntimeError(msg)


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


def _fetch_release_asset_digest(
    repo: str,
    tag: str,
    asset_name: str,
) -> tuple[str, str | None]:
    """Return an asset's download URL and sha256 digest, if GitHub publishes one.

    Parameters
    ----------
    repo : str
        GitHub repository as ``owner/name``.
    tag : str
        Release tag (e.g. ``"continuous"``).
    asset_name : str
        Exact asset filename to look up within the release.

    Returns
    -------
    tuple[str, str | None]
        Direct download URL, and its sha256 hex digest if published
        (``None`` otherwise).

    Raises
    ------
    RuntimeError
        If the release or the named asset cannot be found.

    """
    api_url = _GITHUB_RELEASE_TAG_API.format(repo=repo, tag=tag)
    req = urllib.request.Request(  # noqa: S310
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req) as resp:  # noqa: S310  # nosec B310
        release: dict[str, object] = json.loads(resp.read())

    assets: list[dict[str, object]] = release.get("assets", [])  # type: ignore[assignment]
    for asset in assets:
        if asset.get("name") == asset_name:
            digest = asset.get("digest")
            sha256 = (
                str(digest).removeprefix("sha256:")
                if isinstance(digest, str) and digest.startswith("sha256:")
                else None
            )
            return str(asset["browser_download_url"]), sha256

    msg = f"Asset {asset_name!r} not found in {repo}@{tag}"
    raise RuntimeError(msg)


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
            f"expected {expected}, got {actual}. "
            "Remove the file and retry, or correct the configured hash."
        )
        raise RuntimeError(msg)
    _log.info("%s sha256 verified: %s", label, actual)


def _generate_apprun(resolved: _ResolvedBuild, dest: Path) -> None:
    """Write a generated AppRun script from the resolved configuration.

    Parameters
    ----------
    resolved : _ResolvedBuild
        Resolved build configuration.
    dest : Path
        Destination path for the AppRun file.

    """
    env_lines = "\n".join(f'export {k}="{v}"' for k, v in resolved.env.items())
    env_block = (env_lines + "\n") if env_lines else ""
    dest.write_text(
        _APPRUN_TEMPLATE.format(env_block=env_block, entry_point=resolved.entry_point),
    )


def _generate_desktop(resolved: _ResolvedBuild, project_root: Path, dest: Path) -> None:
    """Generate a ``.desktop`` file from project metadata.

    Parameters
    ----------
    resolved : _ResolvedBuild
        Resolved build configuration.
    project_root : Path
        Project root used to read ``[project]`` metadata.
    dest : Path
        Destination path for the generated file.

    """
    with (project_root / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    project: dict[str, object] = data.get("project", {})

    description = str(project.get("description", ""))
    comment_line = f"Comment={description}\n" if description else ""

    name = str(project.get("name", resolved.app))
    dest.write_text(
        _DESKTOP_TEMPLATE.format(
            name=name,
            comment_line=comment_line,
            app=resolved.app,
        ),
    )


def _run_hook(script: str, project_root: Path, appdir: Path) -> None:
    """Execute a lifecycle hook script.

    Parameters
    ----------
    script : str
        Hook script path relative to *project_root*.
    project_root : Path
        Project root directory used as working directory.
    appdir : Path
        AppDir path exposed to the hook as ``APPDIR``.

    """
    env = {**os.environ, "APPDIR": str(appdir)}
    subprocess.run(  # noqa: S603  # nosec B603
        [str(project_root / script)],
        cwd=project_root,
        env=env,
        check=True,
    )


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
        ``resolved.verify_downloads`` — when true, raise instead of warn.

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


def _install_python(
    resolved: _ResolvedBuild,
    appdir: Path,
    python_cache: Path,
    arch: str,
) -> None:
    """Populate ``appdir/python`` from ``python_dir`` or a resolved tarball.

    ``python_dir`` bypasses tarball resolution, caching, and download
    entirely — it's copied in as given, unverified by design (see
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
        return

    python_tarball = _resolve_python_tarball(resolved, python_cache, arch)
    _log.info("Extracting Python...")
    with tarfile.open(python_tarball) as tar:
        tar.extractall(appdir)  # noqa: S202  # nosec B202


def _resolve_python_tarball(
    resolved: _ResolvedBuild,
    python_cache: Path,
    arch: str,
) -> Path:
    """Return the path to the Python tarball, downloading if necessary.

    A fresh download is verified against ``python_sha256`` when set, else
    against the digest GitHub publishes for the asset, at no extra network
    cost. A local ``python_archive`` or a cached tarball is only verified
    when ``python_sha256`` is explicitly set — otherwise, unless
    ``verify_downloads`` is also set, the documented offline/CI workflow
    stays fully network-free by default and this is used unverified.
    """
    if resolved.python_archive:
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
    if python_cache.exists():
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


# `pip lock` (generates pylock.toml) needs pip >= 25.1; `pip install -r
# pylock.toml` (consumed by `_install_from_pylock`) needs pip >= 26.1. Only
# the generation side is checked here — by the time a build tries to
# install from an existing pylock.toml, a hard pip error surfaces the same
# problem anyway, and duplicating the check there would mean parsing pip's
# version on every single build instead of only on `lock`.
_MIN_PIP_LOCK_VERSION: Final = (25, 1)


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


def _run_pip_lock(
    python_bin: Path,
    project_root: Path,
    requirements: list[str],
    output_path: Path,
    *,
    uploaded_prior_to: str,
) -> None:
    """Run ``pip lock`` (pip >= 25.1) for *requirements*, writing *output_path*.

    Shared by ``_generate_lock`` (runtime dependencies) and
    ``_generate_build_pylock`` (``[build-system].requires``) — both are a
    pip-version check plus one ``pip lock`` invocation, differing only in
    which requirements go in. Run through *python_bin*, the same bundled
    python-build-standalone interpreter the real build installs into, so
    the resolved wheels/hashes match that exact platform and Python build
    rather than whatever the developer's own interpreter would resolve.
    """
    major, minor = _pip_version(python_bin)
    if (major, minor) < _MIN_PIP_LOCK_VERSION:
        msg = (
            f"Bundled pip {major}.{minor} does not support 'pip lock' "
            f"(needs >= {_MIN_PIP_LOCK_VERSION[0]}.{_MIN_PIP_LOCK_VERSION[1]}). "
            "Pin a newer python_date and retry."
        )
        raise RuntimeError(msg)

    cmd = [str(python_bin), "-m", "pip", "lock", *requirements, "-o", str(output_path)]
    if uploaded_prior_to:
        cmd += ["--uploaded-prior-to", uploaded_prior_to]

    _log.info("Generating %s...", output_path)
    subprocess.run(  # noqa: S603  # nosec B603
        cmd,
        cwd=project_root,
        env=_no_bytecode_env(),
        check=True,
    )
    _log.info("Done: %s", output_path)


_PYLOCK_PACKAGE_BLOCK: Final = re.compile(r"(?=^\[\[packages\]\]$)", re.MULTILINE)


def _strip_local_directory_entries(pylock_path: Path) -> None:
    """Remove local-directory package entries (the project itself) from a pylock file.

    ``pip lock --only-deps`` excludes *every* given requirement from its
    output, not just a chosen one ("No user-supplied requirements will be
    handled, even if they were dependencies of other user-supplied
    requirements" per its own ``--help``) — so locking ``appimage_pin``
    and ``config.packages`` alongside the local project under
    ``--only-deps`` silently dropped their own direct pins too, only their
    *transitive* deps made it into the lock. Locking without
    ``--only-deps`` instead resolves everything together in one
    consistent pass, and this strips the local project's entry from the
    result afterwards — identified structurally by its
    ``[packages.directory]`` table (the schema ``pip lock`` uses for a
    local path source), not by name, so no PEP 503 name-normalization is
    needed to find it.
    """
    text = pylock_path.read_text()
    blocks = _PYLOCK_PACKAGE_BLOCK.split(text)
    kept = [blocks[0]]
    for block in blocks[1:]:
        pkg = tomllib.loads(block)["packages"][0]
        if "directory" not in pkg:
            kept.append(block)
    pylock_path.write_text("".join(kept))


def _generate_lock(
    resolved: _ResolvedBuild,
    python_bin: Path,
    project_root: Path,
    *,
    uploaded_prior_to: str,
) -> Path:
    """Generate a hash-pinned ``pylock.toml`` for the project's dependencies.

    Locks ``appimage_pin``, the local project, and ``config.packages``
    (all inside ``resolved.install_targets``) together in one resolution,
    then strips the local project's own entry — see
    ``_strip_local_directory_entries`` for why.
    """
    pylock_path = project_root / (resolved.pylock or "pylock.toml")
    _run_pip_lock(
        python_bin,
        project_root,
        resolved.install_targets,
        pylock_path,
        uploaded_prior_to=uploaded_prior_to,
    )
    _strip_local_directory_entries(pylock_path)
    return pylock_path


def _generate_build_pylock(
    resolved: _ResolvedBuild,
    python_bin: Path,
    project_root: Path,
    *,
    uploaded_prior_to: str,
) -> Path:
    """Generate a hash-pinned pylock-format file for ``[build-system].requires``.

    Same ``pip lock`` mechanism as ``_generate_lock``, aimed at the
    project's own build backend requirement instead of its runtime
    dependencies. ``pylock`` deliberately excludes the local project via
    ``--only-deps`` since it has no stable hash to pin between source
    edits — but its *build-system requirement* is a real, hashable PyPI
    distribution like any other, so no such exclusion applies here.
    """
    pyproject_path = project_root / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)
    requires: list[str] = data.get("build-system", {}).get("requires", [])
    if not requires:
        msg = "No [build-system].requires found in pyproject.toml — nothing to lock."
        raise RuntimeError(msg)

    build_pylock_path = project_root / (resolved.build_pylock or "pylock.build.toml")
    _run_pip_lock(
        python_bin,
        project_root,
        requires,
        build_pylock_path,
        uploaded_prior_to=uploaded_prior_to,
    )
    return build_pylock_path


def _write_lock_config(
    pyproject_path: Path,
    project_root: Path,
    key: str,
    path: Path,
) -> None:
    """Write a generated lock file's path into ``[tool.appimage]`` if not already set."""
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)
    if key in data.get("tool", {}).get("appimage", {}):
        return

    rel = path.relative_to(project_root)
    content = pyproject_path.read_text()
    line = f'{key} = "{rel}"\n'
    if "[tool.appimage]" in content:
        content = content.replace("[tool.appimage]", f"[tool.appimage]\n{line}", 1)
    else:
        content += f"\n[tool.appimage]\n{line}\n"
    pyproject_path.write_text(content)
    _log.info("")
    _log.info("Added to pyproject.toml: %s = %s", key, _toml_value(str(rel)))


def _write_reproducible_flag(project_root: Path) -> None:
    """Write ``reproducible = true`` into ``[tool.appimage]`` if not already set."""
    pyproject_path = project_root / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)
    if "reproducible" in data.get("tool", {}).get("appimage", {}):
        return

    content = pyproject_path.read_text()
    line = "reproducible = true\n"
    if "[tool.appimage]" in content:
        content = content.replace("[tool.appimage]", f"[tool.appimage]\n{line}", 1)
    else:
        content += f"\n[tool.appimage]\n{line}\n"
    pyproject_path.write_text(content)
    _log.info("")
    _log.info("Added to pyproject.toml: reproducible = true")


def lock(
    config: BuildConfig,
    project_root: Path,
    *,
    uploaded_prior_to: str = "",
) -> None:
    """Resolve the bundled Python and generate both hash-pinned lock files.

    Extracts the same python-build-standalone interpreter a real build
    would use into ``<build_dir>/AppDir`` (overwriting it, same as
    ``build()`` does — nothing there survives past the next real build
    anyway) purely to run ``pip lock`` through it, then generates
    ``pylock.toml`` (runtime dependencies) and a build-backend pylock file
    (``[build-system].requires``) together, writing ``pylock``/
    ``build_pylock`` into ``[tool.appimage]`` for whichever of the
    two isn't already set — the same way ``init`` writes its own
    auto-detected fields. Both are generated on every ``lock`` run
    rather than needing separate flags: ``[build-system].requires``
    changes rarely, and re-locking it costs little when it hasn't.

    Parameters
    ----------
    config : BuildConfig
        Explicit configuration already loaded from ``pyproject.toml``.
    project_root : Path
        Project root directory.
    uploaded_prior_to : str
        Optional ``pip lock --uploaded-prior-to`` cooldown window (ISO 8601
        ``PnD`` format, e.g. ``"P7D"``) — excludes packages published more
        recently than that from the resolution, giving the community time
        to catch a compromised release before it gets locked in. Applies
        only to this resolution step; irrelevant once pylock.toml exists,
        since the real build then installs exactly what's already pinned.

    Raises
    ------
    SystemExit
        If the resolved configuration has errors that prevent building.

    """
    resolved = _resolve(config, project_root)
    _format_check(resolved)
    if resolved.appdir_errors:
        raise SystemExit(1)

    arch = platform.machine()
    build_dir = project_root / resolved.build_dir
    appdir = build_dir / "AppDir"
    python_cache = build_dir / "python.tar.gz"

    if appdir.exists():
        shutil.rmtree(appdir)
    appdir.mkdir(parents=True)

    _install_python(resolved, appdir, python_cache, arch)
    python_bin = appdir / "python" / "bin" / "python3"

    pylock_path = _generate_lock(
        resolved,
        python_bin,
        project_root,
        uploaded_prior_to=uploaded_prior_to,
    )
    build_pylock_path = _generate_build_pylock(
        resolved,
        python_bin,
        project_root,
        uploaded_prior_to=uploaded_prior_to,
    )

    pyproject_path = project_root / "pyproject.toml"
    _write_lock_config(pyproject_path, project_root, "pylock", pylock_path)
    _write_lock_config(pyproject_path, project_root, "build_pylock", build_pylock_path)


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


def _pylock_to_build_constraint(pylock_path: Path) -> str:
    """Convert a pylock-format file into classic requirements+hash constraint syntax.

    ``pip install --build-constraint`` doesn't accept the pylock.toml
    format PEP 751 defines — it parses a constraint file as classic
    requirements-txt syntax and rejects the ``lock-version`` header
    outright. Generating ``build_pylock`` via the same ``pip lock``
    machinery used for ``pylock.toml`` (see ``_generate_build_pylock``) is
    still worth it for one shared generation mechanism, so this converts
    the result at install time instead of switching formats at generation
    time. Local-directory entries are skipped defensively (mirroring
    ``_strip_local_directory_entries``), though ``[build-system].requires``
    should never produce one.
    """
    data: dict[str, object] = tomllib.loads(pylock_path.read_text())
    packages: list[dict[str, object]] = data.get("packages", [])  # type: ignore[assignment]
    lines: list[str] = []
    for pkg in packages:
        if "directory" in pkg:
            continue
        artifacts: list[dict[str, object]] = [*pkg.get("wheels", [])]  # type: ignore[misc]
        if sdist := pkg.get("sdist"):
            artifacts.append(sdist)  # type: ignore[arg-type]
        hashes = [
            str(sha256)
            for artifact in artifacts
            if (sha256 := artifact.get("hashes", {}).get("sha256"))  # type: ignore[attr-defined]
        ]
        if not hashes:
            continue
        hash_args = " \\\n".join(f"    --hash=sha256:{h}" for h in hashes)
        lines.append(f"{pkg['name']}=={pkg.get('version', '')} \\\n{hash_args}\n")
    return "\n".join(lines)


def _install_build_pylock(resolved: _ResolvedBuild, project_root: Path) -> list[str]:
    """Return pip args that hash-verify the project's own build backend.

    Installing the local project always triggers a PEP 517 isolated
    build, which otherwise installs the project's own
    ``[build-system].requires`` fresh from the index — unpinned and
    unverified — into that isolated, throwaway environment, on every
    build. ``build_pylock`` pins it; since ``--build-constraint`` won't
    read its pylock.toml format directly (see
    ``_pylock_to_build_constraint``), this converts it into a classic
    hash-pinned constraints file and passes that instead. Deliberately
    *not* ``--no-build-isolation``: pip keeps building in its own
    throwaway environment, verified against this file instead of resolved
    live — reusing the main interpreter's site-packages instead would
    leave the build backend permanently installed in the shipped
    AppImage, and its own import-time bytecode caches would carry
    install-time timestamps ``_compile_pyc``'s ``-f`` then has to paper
    over rather than never seeing in the first place. A no-op (no extra
    args) when ``build_pylock`` isn't configured.
    """
    if not resolved.build_pylock:
        return []
    build_pylock_path = project_root / resolved.build_pylock
    if not build_pylock_path.exists():
        msg = f"build_pylock file not found: {build_pylock_path}. Run 'lock' to generate it."
        raise FileNotFoundError(msg)
    constraint_path = project_root / resolved.build_dir / "build_pylock_constraint.txt"
    constraint_path.parent.mkdir(parents=True, exist_ok=True)
    constraint_path.write_text(_pylock_to_build_constraint(build_pylock_path))
    return ["--build-constraint", str(constraint_path)]


def _install_from_pylock(
    resolved: _ResolvedBuild,
    python_bin: Path,
    project_root: Path,
) -> None:
    """Install the local project unhashed, then its dependencies hash-verified.

    ``pylock.toml`` (generated by ``lock``) pins every third-party
    dependency, including ``appimage_pin`` and ``config.packages`` — not
    the local project itself, stripped out at generation time (see
    ``_strip_local_directory_entries``) since it has no stable hash to pin
    against between source edits. Split into two ``pip install`` calls
    because pip's hash-checking mode, once triggered by any ``--hash``
    in a requirement set, demands *every* requirement in that same
    invocation carry one — mixing the unhashed local project into the
    ``--require-hashes`` call would fail outright. ``--no-deps`` on both
    calls keeps each strictly to what it's given: the local install won't
    reach past its own listed dependencies, and the lock install won't
    silently pull in anything beyond what got hashed.
    """
    pylock_path = project_root / resolved.pylock
    if not pylock_path.exists():
        msg = f"pylock file not found: {pylock_path}. Run 'lock' to generate it."
        raise FileNotFoundError(msg)

    _log.info(
        "Installing project (unverified, own source): %s",
        " ".join(resolved.local_install_targets),
    )
    subprocess.run(  # noqa: S603  # nosec B603
        [
            str(python_bin),
            "-m",
            "pip",
            "install",
            "--no-compile",
            "--no-deps",
            *_install_build_pylock(resolved, project_root),
            *resolved.local_install_targets,
        ],
        cwd=project_root,
        env=_no_bytecode_env(),
        check=True,
    )

    _log.info("Installing dependencies (hash-verified): %s", pylock_path)
    subprocess.run(  # noqa: S603  # nosec B603
        [
            str(python_bin),
            "-m",
            "pip",
            "install",
            "--no-compile",
            "--no-deps",
            "--require-hashes",
            "-r",
            str(pylock_path),
        ],
        cwd=project_root,
        env=_no_bytecode_env(),
        check=True,
    )


def _resolve_appimage_pin_sha256(pin: str, *, strict: bool) -> str | None:
    """Look up *pin*'s wheel sha256 from PyPI's JSON API, if possible.

    Best-effort: unlike the essential downloads (Python, appimagetool, the
    runtime file), this never blocks a build on a network hiccup — return
    ``None`` and let the caller fall back to an unverified install, unless
    *strict* (``resolved.verify_downloads``) asks for a hard failure
    instead, mirroring ``_require_or_warn_unverified``.
    """
    name, _, version = pin.partition("==")
    url = _PYPI_JSON_API.format(name=name, version=version)
    digest: str | None = None
    try:
        req = urllib.request.Request(  # noqa: S310
            url,
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:  # noqa: S310  # nosec B310
            data: dict[str, object] = json.loads(resp.read())
        urls: list[dict[str, object]] = data.get("urls", [])  # type: ignore[assignment]
        for file_info in urls:
            if file_info.get("packagetype") == "bdist_wheel":
                digests: dict[str, str] = file_info.get("digests", {})  # type: ignore[assignment]
                digest = digests.get("sha256")
                break
    except (OSError, ValueError) as exc:
        if strict:
            msg = f"Could not verify {pin} against PyPI's published digest: {exc}"
            raise RuntimeError(msg) from exc
        _log.warning(
            "Could not verify %s against PyPI's published digest: %s. Installing unverified.",
            pin,
            exc,
        )
        return None

    if digest is None:
        if strict:
            msg = f"PyPI has no published wheel digest for {pin}"
            raise RuntimeError(msg)
        _log.warning(
            "PyPI has no published wheel digest for %s. Installing unverified.",
            pin,
        )
    return digest


def _no_bytecode_env() -> dict[str, str]:
    """Return an environment that stops the interpreter writing stray ``.pyc`` files.

    Merely *importing* a stdlib module during ``pip install`` (pip and
    build backends both import plenty) is enough for CPython to write a
    fresh, timestamp-invalidated ``.pyc`` for it if none already validates
    — outside ``site-packages``, so ``_compile_pyc``'s own recompilation
    pass never reaches or fixes it. Such a stray ``.pyc`` embeds the
    absolute path it was compiled from (``co_filename``), which bakes the
    build machine's directory structure — and with it, typically, the
    building user's name — into the shipped AppImage. Every subprocess
    that runs the bundled interpreter for an install gets this in its
    environment so none of them can write one in the first place.
    """
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def _install_hashed_requirement(
    requirement: str,
    sha256: str,
    python_bin: Path,
    project_root: Path,
) -> None:
    """Install a single *requirement* with pip's hash-checking mode.

    pip has no standalone ``--hash`` CLI flag for ``pip install`` — hash
    pins only work via the requirements-file syntax — so this writes a
    one-line requirements file rather than passing anything on the
    command line.
    """
    with tempfile.NamedTemporaryFile(
        "w",
        suffix=".txt",
        prefix="appimage-pin-",
    ) as req_file:
        req_file.write(f"{requirement} --hash=sha256:{sha256}\n")
        req_file.flush()
        _log.info(
            "Installing %s (hash-verified against PyPI): sha256=%s",
            requirement,
            sha256,
        )
        subprocess.run(  # noqa: S603  # nosec B603
            [
                str(python_bin),
                "-m",
                "pip",
                "install",
                "--no-compile",
                "--no-deps",
                "--require-hashes",
                "-r",
                req_file.name,
            ],
            cwd=project_root,
            env=_no_bytecode_env(),
            check=True,
        )


def _install_targets(
    resolved: _ResolvedBuild,
    python_bin: Path,
    project_root: Path,
) -> None:
    """Install ``install_targets``, hash-verifying ``appimage_pin`` when possible.

    Used only when ``pylock`` isn't configured — with it, ``appimage_pin``
    is already hash-pinned inside ``pylock.toml`` (see ``_generate_lock``/
    ``_strip_local_directory_entries``). Without it, the rest of
    ``install_targets`` (the local project, ``config.packages``) stays
    unverified as always, but ``appimage_pin``'s exact version is always
    independently knowable in advance — either explicitly via
    ``appimage_version``, or the currently-running ``appimage.ctl``
    release itself — so it gets the same free auto-verification treatment
    appimagetool/the runtime file/the Python archive already get, rather
    than needing a full ``pylock`` opt-in just to protect the one
    dependency this tool controls end to end.
    """
    sha256 = resolved.appimage_sha256 or _resolve_appimage_pin_sha256(
        resolved.appimage_pin,
        strict=resolved.verify_downloads,
    )
    targets = resolved.install_targets
    if sha256:
        _install_hashed_requirement(
            resolved.appimage_pin,
            sha256,
            python_bin,
            project_root,
        )
        targets = [t for t in targets if t != resolved.appimage_pin]

    _log.info("Installing packages: %s", " ".join(targets))
    subprocess.run(  # noqa: S603  # nosec B603
        [
            str(python_bin),
            "-m",
            "pip",
            "install",
            "--no-compile",
            *_install_build_pylock(resolved, project_root),
            *targets,
        ],
        cwd=project_root,
        env=_no_bytecode_env(),
        check=True,
    )


def _prepare_python(
    resolved: _ResolvedBuild,
    appdir: Path,
    python_cache: Path,
    arch: str,
    project_root: Path,
) -> None:
    """Install Python and packages into AppDir."""
    _install_python(resolved, appdir, python_cache, arch)

    python_bin = appdir / "python" / "bin" / "python3"

    if resolved.pylock:
        _install_from_pylock(resolved, python_bin, project_root)
    else:
        _install_targets(resolved, python_bin, project_root)

    if hook := resolved.hooks.get("post_install"):
        _log.info("Running post_install hook...")
        _run_hook(hook, project_root, appdir)


def _compile_pyc(resolved: _ResolvedBuild, appdir: Path) -> None:
    """Byte-compile installed packages to hash-based, timestamp-free ``.pyc``.

    Packages installed via a normal ``pip install --no-compile`` have no
    bytecode yet — but ``--no-compile`` only suppresses pip's own
    post-install compile step, not bytecode CPython's import machinery
    writes out on its own the moment something merely *imports* a module
    (a ``post_install``/``pre_package`` hook, some package's own install
    hook, anything). Such a stray ``.pyc`` is timestamp-invalidated, not
    hash-invalidated, and without ``-f`` here ``compileall`` treats it as
    already up to date and leaves that install-time timestamp in place —
    the exact non-determinism this function exists to eliminate,
    reintroduced through a side door (an earlier ``--no-build-isolation``
    approach for ``build_pylock`` used to trigger exactly this by running
    the build backend inside the same interpreter; the fix there was to
    stop doing that, but ``-f`` stays as a general safeguard against any
    other organic import doing the same). ``-f`` forces every ``.pyc`` to
    be regenerated here, in ``unchecked-hash`` mode, regardless of what
    already exists.

    ``-s`` strips *site_packages* from the ``co_filename`` every
    compiled code object carries — without it, each one embeds the
    *absolute* build path (e.g. ``/home/alice/project/build/AppDir/...``),
    baking the building machine's directory layout, and typically the
    building user's name, into every single compiled file, not just
    stray ones. ``_scrub_build_paths`` (later) closes the same class of
    leak for what's left: install-time metadata and script shims that
    ``-s`` doesn't touch.

    Run once, at the very end of AppDir assembly — after ``post_install``,
    extra files, and the ``pre_package`` hook — so any step that edits an
    installed package's source is reflected in the compiled cache.

    Deliberately single-threaded (no ``-j``): parallel workers finish in
    scheduling-dependent order, which changes the order ``.pyc`` files are
    created inside each ``__pycache__`` directory between runs — squashfs
    packs directory entries in readdir order, so that alone was enough to
    make the final ``.AppImage`` differ even though every file's *content*
    was already identical.
    """
    python_bin = appdir / "python" / "bin" / "python3"
    site_packages = (
        appdir / "python" / "lib" / f"python{resolved.python}" / "site-packages"
    )
    _log.info("Compiling bytecode (hash-based, reproducible)...")
    subprocess.run(  # noqa: S603  # nosec B603
        [
            str(python_bin),
            "-m",
            "compileall",
            "-q",
            "-f",
            "--invalidation-mode",
            "unchecked-hash",
            "-s",
            str(site_packages),
            str(site_packages),
        ],
        env=_no_bytecode_env(),
        check=True,
    )


def _find_build_path_leaks(appdir: Path, marker: bytes) -> list[Path]:
    """Return every regular file under *appdir* whose content still contains *marker*."""
    return [
        path
        for path in appdir.rglob("*")
        if path.is_file() and not path.is_symlink() and marker in path.read_bytes()
    ]


def _scrub_build_paths(resolved: _ResolvedBuild, appdir: Path) -> None:
    """Remove the build machine's absolute path from install-time artifacts.

    ``pip`` writes two kinds of file that embed the absolute path it ran
    from — which varies by machine, checkout location, and (via the home
    directory) the building user's own name, none of which has any
    business ending up in a shipped, redistributable AppImage:

    - Each local install's ``direct_url.json`` (PEP 610), recording the
      ``file://`` source URL it was installed from.
    - Every console-script shim pip generates for an entry point, whose
      shebang embeds the absolute path to the bundled interpreter (on
      *some* line — pip falls back to a two-line ``#!/bin/sh`` + exec
      trick once the interpreter path is too long for the OS's shebang
      limit, so which line varies).

    ``AppRun`` never runs either of these — it execs the bundled
    interpreter directly (see ``templates/AppRun.sh``) — so both are
    safe to delete outright rather than neutralized in place. Which
    files exist to delete is read from each ``RECORD`` — the same
    manifest pip itself wrote when it installed that file — rather than
    inferred by matching against pip's script format, so a future pip
    version changing that format doesn't silently defeat this: any
    RECORD-listed file that still contains the build path once inspected
    gets removed, whatever it looks like.

    Run late, after ``_compile_pyc``: a ``post_install``/``pre_package``
    hook installing something of its own could produce either artifact
    too. Ends with a whole-AppDir sweep for the same marker as a backstop
    — the exact class of bug ``_normalize_mtimes`` exists to close for
    timestamps, just for paths instead — and raises if anything is still
    found, since at that point it's a gap in this function itself rather
    than something a config option could route around.

    Parameters
    ----------
    resolved : _ResolvedBuild
        Fully resolved build parameters (for the bundled Python version).
    appdir : Path
        AppDir path, already fully installed into.

    Raises
    ------
    RuntimeError
        If the build path still appears anywhere in *appdir* afterwards.

    """
    site_packages = (
        appdir / "python" / "lib" / f"python{resolved.python}" / "site-packages"
    )
    appdir_marker = str(appdir).encode()

    if site_packages.is_dir():
        for dist_info in sorted(site_packages.glob("*.dist-info")):
            record_path = dist_info / "RECORD"
            if not record_path.exists():
                continue
            with record_path.open(newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))

            new_rows = []
            changed = False
            for row in rows:
                if not row:
                    continue
                target = (site_packages / row[0]).resolve()
                is_direct_url = target.name == "direct_url.json"
                if (
                    target.is_file()
                    and not target.is_symlink()
                    and (is_direct_url or appdir_marker in target.read_bytes())
                ):
                    target.unlink()
                    changed = True
                    continue
                new_rows.append(row)

            if changed:
                with record_path.open("w", newline="", encoding="utf-8") as f:
                    csv.writer(f, lineterminator="\n").writerows(new_rows)

    leaked = _find_build_path_leaks(appdir, appdir_marker)
    if leaked:
        names = ", ".join(str(p.relative_to(appdir)) for p in leaked)
        msg = (
            "Build machine path leaked into the AppImage despite scrubbing "
            f"(this is a bug in appimage.ctl itself, please report it): {names}"
        )
        raise RuntimeError(msg)


def _normalize_mtimes(appdir: Path, epoch: int) -> None:
    """Set every file's and directory's mtime in *appdir* to a fixed value.

    ``mksquashfs`` embeds each inode's mtime into the packed image, so two
    otherwise byte-identical AppDirs still produce a different squashfs (and
    thus a different final ``.AppImage``) if their files were installed or
    generated at different wall-clock times — which they always are, one
    build run to the next. appimagetool exposes no flag to normalize this
    itself, so it has to happen here, on the fully assembled AppDir, right
    before packaging.

    This alone is not sufficient — appimagetool touches a few paths of its
    own during packaging (e.g. ``.DirIcon``), which need ``SOURCE_DATE_EPOCH``
    set in *its* environment to normalize too; see ``build()``.
    """
    for root, dirs, files in os.walk(appdir):
        for name in (*dirs, *files):
            os.utime(Path(root) / name, (epoch, epoch), follow_symlinks=False)
    os.utime(appdir, (epoch, epoch), follow_symlinks=False)


def _copy_assets(resolved: _ResolvedBuild, project_root: Path, appdir: Path) -> None:
    """Copy icon, desktop file, and AppRun script into AppDir."""
    _log.info("Copying assets...")
    if resolved.icon:
        shutil.copy2(resolved.icon, appdir / (resolved.app + resolved.icon.suffix))
    if resolved.desktop:
        shutil.copy2(resolved.desktop, appdir / resolved.desktop.name)
    else:
        _generate_desktop(resolved, project_root, appdir / f"{resolved.app}.desktop")

    apprun_dest = appdir / "AppRun"
    if resolved.apprun:
        apprun_src = project_root / resolved.apprun
        if not apprun_src.exists():
            msg = f"AppRun not found: {apprun_src}"
            raise FileNotFoundError(msg)
        shutil.copy2(apprun_src, apprun_dest)
    else:
        _generate_apprun(resolved, apprun_dest)
    apprun_dest.chmod(0o755)


def _copy_extra_files(
    resolved: _ResolvedBuild,
    project_root: Path,
    appdir: Path,
) -> None:
    """Copy extra files and directories into AppDir."""
    for src_str, dst_str in resolved.extra_files.items():
        src_path = project_root / src_str
        dst_path = appdir / dst_str
        if src_path.is_dir():
            shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
        else:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)


def _assemble_appdir(
    resolved: _ResolvedBuild,
    appdir: Path,
    python_cache: Path,
    arch: str,
    project_root: Path,
    epoch: int,
) -> None:
    """Do the actual AppDir assembly work, given an already-resolved config.

    Shared by ``build_appdir()`` and ``build()`` so there's exactly one
    place that does this, regardless of which validation (``appdir_errors``
    only, or both buckets) already ran in the caller.
    """
    _log.info("")
    _log.info("Preparing AppDir...")
    if appdir.exists():
        shutil.rmtree(appdir)
    appdir.mkdir(parents=True)

    _prepare_python(resolved, appdir, python_cache, arch, project_root)
    _copy_assets(resolved, project_root, appdir)
    _copy_extra_files(resolved, project_root, appdir)

    if hook := resolved.hooks.get("pre_package"):
        _log.info("Running pre_package hook...")
        _run_hook(hook, project_root, appdir)

    _compile_pyc(resolved, appdir)
    _scrub_build_paths(resolved, appdir)
    # https://reproducible-builds.org/specs/source-date-epoch/ — respected
    # both here and, for a full build, again in appimagetool's own process
    # environment (it touches a few paths of its own, e.g. .DirIcon, which
    # this AppDir-side normalization can't reach).
    _normalize_mtimes(appdir, epoch)


def build_appdir(config: BuildConfig, project_root: Path) -> Path:
    """Assemble the AppDir from *config*, without packaging it into an AppImage.

    Everything a full build does through mtime normalization — installing
    Python and packages, copying assets/extra files, running hooks,
    compiling bytecode, and scrubbing build-machine paths — with none of
    the appimagetool/runtime resolution or packaging that follows. The
    result is a complete, runnable installation tree that can be tested,
    inspected, or deployed some other way without ever producing a
    single-file ``.AppImage``.

    Only enforces ``appdir_errors``: errors that block packaging
    specifically (e.g. missing ``appimagetool_sha256``/``runtime_sha256``
    under ``reproducible``, or missing ``zsyncmake``) don't apply here,
    since this never resolves appimagetool or the runtime stub at all.

    Parameters
    ----------
    config : BuildConfig
        Build configuration (explicit fields only; the rest are auto-detected).
    project_root : Path
        Absolute path to the project root directory.

    Returns
    -------
    Path
        Path to the assembled AppDir.

    Raises
    ------
    SystemExit
        If the resolved configuration has AppDir-blocking errors.

    """
    resolved = _resolve(config, project_root)
    _format_check(resolved)

    if resolved.appdir_errors:
        raise SystemExit(1)

    arch = platform.machine()
    build_dir = project_root / resolved.build_dir
    appdir = build_dir / "AppDir"
    python_cache = build_dir / "python.tar.gz"
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))

    _assemble_appdir(resolved, appdir, python_cache, arch, project_root, epoch)
    _log.info("Done: %s", appdir)
    return appdir


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
    _log.info("Done: %s", dist_dir / output_name)


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
