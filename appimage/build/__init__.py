# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""Build an AppImage from a Python project configured via pyproject.toml."""

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

_PBS_API: Final = (
    "https://api.github.com/repos/astral-sh/python-build-standalone/releases"
)
_APPIMAGETOOL_URL: Final = (
    "https://github.com/AppImage/AppImageKit/releases/download/continuous"
    "/appimagetool-{arch}.AppImage"
)

_ICON_SEARCH_DIRS: Final = (".", "appimage", "assets", "packaging", "data", "icons")
_ICON_EXTENSIONS: Final = (".png", ".svg")
_DESKTOP_SEARCH_DIRS: Final = (".", "appimage", "assets", "packaging", "data")

_pkg = importlib.resources.files("appimage.build")
_APPRUN_TEMPLATE: Final = (_pkg / "templates" / "AppRun.sh").read_text(encoding="utf-8")
_DESKTOP_TEMPLATE: Final = (_pkg / "templates" / "desktop.template").read_text(encoding="utf-8")


@dataclass
class BuildConfig:
    """Explicit build configuration from ``[tool.appimage.build]``.

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
    python_archive : str
        Path to a local python-build-standalone tarball. When empty, the
        archive is looked up in the build cache and then downloaded.

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
    python_archive: str = ""

    @classmethod
    def from_pyproject(cls, project_root: Path) -> "BuildConfig":
        """Load explicit configuration from ``[tool.appimage.build]``.

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
        cfg = data.get("tool", {}).get("appimage", {}).get("build", {})
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
            python_archive=cfg.get("python_archive", ""),
        )


@dataclass
class _ResolvedBuild:
    """Fully resolved build parameters, ready to execute."""

    app: str
    entry_point: str
    install_targets: list[str]
    python: str
    python_date: str
    icon: Path | None
    desktop: Path | None
    apprun: str
    build_dir: str
    dist_dir: str
    update_info: str
    env: dict[str, str]
    extra_files: dict[str, str]
    hooks: dict[str, str]
    appimagetool: str
    python_archive: str
    sources: dict[str, str]
    warnings: list[str]
    errors: list[str]


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


def _resolve_app(config: BuildConfig, project: dict[str, object]) -> tuple[str, str]:
    """Resolve app name from config or project metadata."""
    if config.app is not None:
        return config.app, "[tool.appimage.build]"
    if project_name := project.get("name"):
        return str(project_name), "[project] name"
    msg = (
        "Cannot determine app name: set 'app' in [tool.appimage.build] "
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
        return config.entry_point, "[tool.appimage.build]", []
    scripts: dict[str, str] = project.get("scripts", {})  # type: ignore[assignment]
    ep = _detect_entry_point(scripts, app)
    if ep is not None:
        return ep, "[project] scripts", []
    error = (
        "Cannot determine entry_point: add it to [tool.appimage.build] "
        "or define it in [project.scripts]"
    )
    return app, "", [error]


def _resolve_python(
    config: BuildConfig,
    project: dict[str, object],
) -> tuple[str, str]:
    """Resolve Python version from config or requires-python."""
    if config.python is not None:
        return config.python, "[tool.appimage.build]"
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
        return project_root / config.icon, "[tool.appimage.build]", []
    icon = _find_icon(app, project_root)
    if icon is not None:
        return icon, f"detected ({icon.relative_to(project_root)})", []
    warning = (
        f"No icon found — add {app}.png to the project root "
        f"or set 'icon' in [tool.appimage.build]. "
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
        return project_root / config.desktop, "[tool.appimage.build]", []
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
    warnings: list[str] = []
    errors: list[str] = []

    app, sources["app"] = _resolve_app(config, project)
    entry_point, sources["entry_point"], ep_errors = _resolve_entry_point(config, project, app)
    errors.extend(ep_errors)
    python, sources["python"] = _resolve_python(config, project)
    icon, sources["icon"], icon_warnings = _resolve_icon_path(config, project_root, app)
    warnings.extend(icon_warnings)
    desktop, sources["desktop"], desktop_warnings = _resolve_desktop_path(config, project_root, app)
    warnings.extend(desktop_warnings)

    if config.extras:
        extras_str = ",".join(config.extras)
        base = f".[{extras_str}]"
        sources["packages"] = "[tool.appimage.build] extras"
    else:
        base = "."
        sources["packages"] = "default (.)"

    # The `appimage` runtime module handles entry point dispatch and the
    # `--python-*` flags inside the built AppImage. It must be installed into
    # the bundled site-packages regardless of whether the packaged project
    # declares it as a dependency. Pinning to the currently running build
    # version keeps AppRun's expectations and the bundled runtime in sync.
    appimage_pin = f"appimage=={importlib.metadata.version('appimage')}"
    install_targets = [appimage_pin, base, *config.packages]

    sources["build_dir"] = (
        "[tool.appimage.build]" if config.build_dir != "build" else "default"
    )
    sources["dist_dir"] = (
        "[tool.appimage.build]" if config.dist_dir != "dist" else "default"
    )

    return _ResolvedBuild(
        app=app,
        entry_point=entry_point,
        install_targets=install_targets,
        python=python,
        python_date=config.python_date,
        icon=icon,
        desktop=desktop,
        apprun=config.apprun,
        build_dir=config.build_dir,
        dist_dir=config.dist_dir,
        update_info=config.update_info,
        env=config.env,
        extra_files=config.extra_files,
        hooks=config.hooks,
        appimagetool=config.appimagetool,
        python_archive=config.python_archive,
        sources=sources,
        warnings=warnings,
        errors=errors,
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
    cfg = "[tool.appimage.build]"
    candidates = [
        ("apprun", resolved.apprun),
        ("update_info", resolved.update_info),
        ("python_date", resolved.python_date),
        ("python_archive", resolved.python_archive),
        ("appimagetool", resolved.appimagetool),
    ]
    return [(name, value, cfg) for name, value in candidates if value]


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
        ("packages", " ".join(resolved.install_targets), resolved.sources.get("packages", "")),
        ("icon", _icon_display(resolved.icon), resolved.sources.get("icon", "")),
        (
            "desktop",
            str(resolved.desktop.relative_to(Path.cwd())) if resolved.desktop else "(generated)",
            resolved.sources.get("desktop", ""),
        ),
        ("build_dir", resolved.build_dir, resolved.sources.get("build_dir", "")),
        ("dist_dir", resolved.dist_dir, resolved.sources.get("dist_dir", "")),
        *_optional_check_rows(resolved),
    ]

    for name, value, source in rows:
        _log.info("  %-15s %-35s [%s]", f"{name}:", value, source)

    if resolved.warnings:
        _log.info("")
        for w in resolved.warnings:
            _log.warning("  Warning: %s", w)

    if resolved.errors:
        _log.info("")
        for e in resolved.errors:
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
    return not resolved.errors


def write_config(config: BuildConfig, project_root: Path) -> None:
    """Write auto-detected values to ``pyproject.toml``.

    Only fields that are not already explicitly set in ``[tool.appimage.build]``
    are written. Existing values are never overwritten.

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
    existing = set(data.get("tool", {}).get("appimage", {}).get("build", {}).keys())

    new: dict[str, object] = {}
    if "app" not in existing:
        new["app"] = resolved.app
    if "entry_point" not in existing:
        new["entry_point"] = resolved.entry_point
    if "python" not in existing:
        new["python"] = resolved.python
    if "icon" not in existing and resolved.icon is not None:
        new["icon"] = str(resolved.icon.relative_to(project_root))
    if "desktop" not in existing and resolved.desktop is not None:
        new["desktop"] = str(resolved.desktop.relative_to(project_root))

    if not new:
        _log.info("")
        _log.info("Nothing to add — all detected values are already configured.")
        return

    lines = "\n".join(f"{k} = {_toml_value(v)}" for k, v in new.items())
    content = pyproject_path.read_text()

    if "[tool.appimage.build]" in content:
        content = content.replace(
            "[tool.appimage.build]",
            f"[tool.appimage.build]\n{lines}",
            1,
        )
    else:
        content += f"\n[tool.appimage.build]\n{lines}\n"

    pyproject_path.write_text(content)
    _log.info("")
    _log.info("Added to pyproject.toml:")
    for k, v in new.items():
        _log.info("  %s = %s", k, _toml_value(v))


def _resolve_python_url(python: str, date: str, arch: str) -> str:
    """Return the python-build-standalone download URL.

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
    str
        Direct download URL for the matching ``install_only_stripped`` tarball.

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

    assets: list[dict[str, str]] = release.get("assets", [])  # type: ignore[assignment]
    for asset in assets:
        url = asset["browser_download_url"]
        if (
            f"cpython-{python}." in url
            and f"{pbs_arch}-unknown-linux-gnu-install_only_stripped" in url
            and "freethreaded" not in url
        ):
            return url

    tag = release.get("tag_name", date or "latest")
    msg = f"No Python {python} asset found for {pbs_arch} in release {tag}"
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


def _resolve_python_tarball(
    resolved: _ResolvedBuild,
    python_cache: Path,
    arch: str,
) -> Path:
    """Return the path to the Python tarball, downloading if necessary."""
    if resolved.python_archive:
        tarball = Path(resolved.python_archive)
        if not tarball.exists():
            msg = f"Python archive not found: {tarball}"
            raise FileNotFoundError(msg)
        _log.info("Using Python archive: %s", tarball)
        return tarball
    if python_cache.exists():
        _log.info("Using cached python.tar.gz")
        return python_cache
    python_url = _resolve_python_url(resolved.python, resolved.python_date, arch)
    _download(python_url, python_cache)
    return python_cache


def _resolve_appimagetool(
    resolved: _ResolvedBuild,
    appimagetool_cache: Path,
    arch: str,
) -> Path:
    """Return the path to appimagetool, downloading if necessary."""
    if resolved.appimagetool:
        tool = Path(resolved.appimagetool)
        if not tool.exists():
            msg = f"appimagetool not found: {tool}"
            raise FileNotFoundError(msg)
        _log.info("Using appimagetool: %s", tool)
        return tool
    if path_tool := shutil.which("appimagetool"):
        _log.info("Using appimagetool from PATH: %s", path_tool)
        return Path(path_tool)
    if appimagetool_cache.exists():
        _log.info("Using cached appimagetool")
        return appimagetool_cache
    _download(_APPIMAGETOOL_URL.format(arch=arch), appimagetool_cache)
    appimagetool_cache.chmod(0o755)
    return appimagetool_cache


def _prepare_python(
    resolved: _ResolvedBuild,
    python_tarball: Path,
    appdir: Path,
    project_root: Path,
) -> None:
    """Extract Python and install packages into AppDir."""
    _log.info("Extracting Python...")
    with tarfile.open(python_tarball) as tar:
        tar.extractall(appdir)  # noqa: S202  # nosec B202

    python_bin = appdir / "python" / "bin" / "python3"
    _log.info("Installing packages: %s", " ".join(resolved.install_targets))
    subprocess.run(  # noqa: S603  # nosec B603
        [str(python_bin), "-m", "pip", "install", *resolved.install_targets],
        cwd=project_root,
        check=True,
    )

    if hook := resolved.hooks.get("post_install"):
        _log.info("Running post_install hook...")
        _run_hook(hook, project_root, appdir)


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


def _copy_extra_files(resolved: _ResolvedBuild, project_root: Path, appdir: Path) -> None:
    """Copy extra files and directories into AppDir."""
    for src_str, dst_str in resolved.extra_files.items():
        src_path = project_root / src_str
        dst_path = appdir / dst_str
        if src_path.is_dir():
            shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
        else:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)


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

    if resolved.errors:
        raise SystemExit(1)

    arch = platform.machine()
    build_dir = project_root / resolved.build_dir
    appdir = build_dir / "AppDir"
    dist_dir = project_root / resolved.dist_dir
    python_cache = build_dir / "python.tar.gz"
    appimagetool_cache = build_dir / f"appimagetool-{arch}.AppImage"

    _log.info("")
    _log.info("Preparing AppDir...")
    if appdir.exists():
        shutil.rmtree(appdir)
    appdir.mkdir(parents=True)

    python_tarball = _resolve_python_tarball(resolved, python_cache, arch)
    _prepare_python(resolved, python_tarball, appdir, project_root)
    _copy_assets(resolved, project_root, appdir)
    _copy_extra_files(resolved, project_root, appdir)

    if hook := resolved.hooks.get("pre_package"):
        _log.info("Running pre_package hook...")
        _run_hook(hook, project_root, appdir)

    appimagetool_bin = _resolve_appimagetool(resolved, appimagetool_cache, arch)

    dist_dir.mkdir(parents=True, exist_ok=True)
    output_name = f"{resolved.app}-{arch}.AppImage"

    cmd = [str(appimagetool_bin)]
    if resolved.update_info:
        cmd += ["-u", resolved.update_info]
    cmd += [str(appdir), output_name]

    _log.info("Packaging AppImage...")
    subprocess.run(cmd, cwd=dist_dir, check=True)  # noqa: S603  # nosec B603
    _log.info("Done: %s", dist_dir / output_name)
