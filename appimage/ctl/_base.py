# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""Shared build configuration and auto-detection logic used by every subcommand.

``BuildConfig``, ``_ResolvedBuild``, and ``_resolve()`` (plus its auto-detect
helpers) live here rather than in ``appimage/ctl/__init__.py`` so that every
CLI-subcommand submodule can depend on them without creating a circular
import back through the package's own ``__init__.py`` (which re-exports the
public API from those same submodules).
"""

import importlib.metadata
import logging
import platform
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

_log: Final = logging.getLogger(__name__)
_DEFAULT_ICON: Final = Path(__file__).parent.parent / "assets" / "default_icon.svg"

# Used to suggest an update_info value from [project.urls] — matches only a
# bare repository root (no /issues, /blob/... paths), since those aren't
# valid gh-releases-zsync targets.
_GITHUB_REPO_PATTERN: Final = re.compile(
    r"^https?://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
)
_PREFERRED_URL_KEYS: Final = ("source", "source code", "repository", "github", "code")

_ICON_SEARCH_DIRS: Final = (".", "appimage", "assets", "packaging", "data", "icons")
_ICON_EXTENSIONS: Final = (".png", ".svg")
_DESKTOP_SEARCH_DIRS: Final = (".", "appimage", "assets", "packaging", "data")


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
        cached binary) aborts the build instead of logging a warning and
        continuing.
    require_zsyncmake : bool
        When true, abort the build if ``update_info`` is set but appimagetool
        didn't actually produce a ``.zsync`` delta-update file next to the
        packaged AppImage — instead of logging a warning. Checked after
        packaging, against the real output, not a prediction: appimagetool
        bundles its own ``zsyncmake`` (its ``AppRun`` puts its own ``usr/bin``
        first on ``PATH``, ahead of anything on the build host's), so this
        normally succeeds regardless of what the build host has installed —
        it only fires for a genuinely broken or unusually minimal
        appimagetool build. Has no effect when ``update_info`` is empty.
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
