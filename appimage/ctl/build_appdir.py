# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""The ``build-appdir`` subcommand: assemble the AppDir without packaging it."""

import csv
import importlib.resources
import json
import logging
import os
import platform
import shutil
import subprocess  # nosec B404
import tempfile
import tomllib
import urllib.request
from pathlib import Path
from typing import Final

from appimage.ctl._base import BuildConfig, _resolve, _ResolvedBuild
from appimage.ctl._python import _install_python
from appimage.ctl.check import _format_check

_log: Final = logging.getLogger(__name__)

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

_pkg = importlib.resources.files("appimage.ctl")
_APPRUN_TEMPLATE: Final = (_pkg / "templates" / "AppRun.sh").read_text(encoding="utf-8")
_DESKTOP_TEMPLATE: Final = (_pkg / "templates" / "desktop.template").read_text(
    encoding="utf-8",
)


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


def _resolve_for_appdir(
    config: BuildConfig,
    project_root: Path,
) -> tuple[_ResolvedBuild, str, Path, Path]:
    """Resolve *config*, print the check report, and derive standard AppDir build paths.

    Shared by ``build_appdir()`` and ``lock()`` (via ``appimage.ctl.lock``)
    — both need exactly this same "resolve, report, enforce
    ``appdir_errors``, then derive the conventional ``build_dir``/AppDir/
    ``python.tar.gz`` paths" preamble before going on to do their own,
    different, work with the result. Only enforces ``appdir_errors``:
    errors that block packaging specifically (e.g. missing
    ``appimagetool_sha256``/``runtime_sha256`` under ``reproducible``, or
    missing ``zsyncmake``) don't apply to either caller, since neither
    resolves appimagetool or the runtime stub.

    Returns
    -------
    tuple[_ResolvedBuild, str, Path, Path]
        The resolved build, the host architecture, the AppDir path, and
        the Python tarball cache path.

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
    return resolved, arch, appdir, python_cache


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
    resolved, arch, appdir, python_cache = _resolve_for_appdir(config, project_root)
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))

    _assemble_appdir(resolved, appdir, python_cache, arch, project_root, epoch)
    _log.info("Done: %s", appdir)
    return appdir
