# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""The ``build-appdir`` subcommand: assemble the AppDir without packaging it."""

import base64
import csv
import hashlib
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
from appimage.ctl._python import _install_python, _python_tarball_cache_path
from appimage.ctl.check import _format_check

_log: Final = logging.getLogger(__name__)

# TODO(manfred-kaiser): the only network call in this module that hardcodes a  # noqa: TD003, FIX002
# specific host instead of going through pip's own index-selection
# machinery (PIP_INDEX_URL / pip.conf / .netrc) like every pip/pip lock
# subprocess call elsewhere here. A private index that mirrors PyPI
# internally but blocks pypi.org itself would make this lookup fail even
# though the package is perfectly reachable - it degrades to a warning
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

    Same isolated environment as every other install subprocess here
    (``PYTHONNOUSERSITE``/``PYTHONDONTWRITEBYTECODE``, see
    ``_isolated_subprocess_env``), plus ``APPDIR``. Predates this project's
    reproducibility work - added long before it, never revisited when that
    landed elsewhere - but a hook is documented as running between install
    steps specifically to edit the AppDir's installed packages
    (``post_install``/``pre_package``), so anything it does through the
    bundled interpreter is exactly as exposed to a host-side PEP 370 leak
    as ``pip install`` itself was. The rest of the inherited environment
    (``PATH`` etc.) stays untouched - a hook still needs real host tools to
    do anything useful.

    Parameters
    ----------
    script : str
        Hook script path relative to *project_root*.
    project_root : Path
        Project root directory used as working directory.
    appdir : Path
        AppDir path exposed to the hook as ``APPDIR``.

    """
    env = {**_isolated_subprocess_env(), "APPDIR": str(appdir)}
    subprocess.run(  # noqa: S603  # nosec B603
        [str(project_root / script)],
        cwd=project_root,
        env=env,
        check=True,
    )


def _pylock_to_build_constraint(pylock_path: Path) -> str:
    """Convert a pylock-format file into classic requirements+hash constraint syntax.

    ``pip install --build-constraint`` doesn't accept the pylock.toml
    format PEP 751 defines - it parses a constraint file as classic
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
    ``[build-system].requires`` fresh from the index - unpinned and
    unverified - into that isolated, throwaway environment, on every
    build. ``build_pylock`` pins it; since ``--build-constraint`` won't
    read its pylock.toml format directly (see
    ``_pylock_to_build_constraint``), this converts it into a classic
    hash-pinned constraints file and passes that instead. Deliberately
    *not* ``--no-build-isolation``: pip keeps building in its own
    throwaway environment, verified against this file instead of resolved
    live - reusing the main interpreter's site-packages instead would
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
    dependency, including ``appimage_pin`` and ``config.packages`` - not
    the local project itself, stripped out at generation time (see
    ``_strip_local_directory_entries``) since it has no stable hash to pin
    against between source edits. Split into two ``pip install`` calls
    because pip's hash-checking mode, once triggered by any ``--hash``
    in a requirement set, demands *every* requirement in that same
    invocation carry one - mixing the unhashed local project into the
    ``--require-hashes`` call would fail outright.

    ``--no-deps`` on the local install keeps it strictly to what it's
    given: it won't reach past its own listed dependencies, since those
    are meant to come exclusively from the lock install below. The lock
    install deliberately does *not* pass ``--no-deps``: leaving normal
    dependency resolution on means pip still checks each locked package's
    declared dependencies against what ``pylock.toml`` provides. If the
    lock is incomplete (a generation bug, or ``pyproject.toml`` changed
    without re-running ``lock``), resolution needs a candidate for the
    missing package, finds none with a hash, and aborts loudly right here
    - instead of installing a silently incomplete AppDir that only fails
    with an import error when the built AppImage is actually run.
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
        env=_isolated_subprocess_env(),
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
            "--require-hashes",
            "-r",
            str(pylock_path),
        ],
        cwd=project_root,
        env=_isolated_subprocess_env(),
        check=True,
    )


def _resolve_appimage_pin_sha256(pin: str, *, strict: bool) -> str | None:
    """Look up *pin*'s wheel sha256 from PyPI's JSON API, if possible.

    Best-effort: unlike the essential downloads (Python, appimagetool, the
    runtime file), this never blocks a build on a network hiccup - return
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


def _isolated_subprocess_env() -> dict[str, str]:
    """Return an environment for every subprocess that installs into the AppDir.

    Two independent host leaks, closed together because every install
    subprocess needs both:

    - ``PYTHONDONTWRITEBYTECODE=1``: merely *importing* a stdlib module
      during ``pip install`` (pip and build backends both import plenty)
      is enough for CPython to write a fresh, timestamp-invalidated
      ``.pyc`` for it if none already validates - outside
      ``site-packages``, so ``_compile_pyc``'s own recompilation pass
      never reaches or fixes it. Such a stray ``.pyc`` embeds the
      absolute path it was compiled from (``co_filename``), which bakes
      the build machine's directory structure - and with it, typically,
      the building user's name - into the shipped AppImage.
    - ``PYTHONNOUSERSITE=1``: without it, pip resolves against the
      *build user's* ``~/.local/lib/pythonX.Y/site-packages`` too (PEP
      370) in addition to the bundled interpreter's own site-packages.
      If that happens to already satisfy a requirement - any unrelated
      Python install on the build host, not just this AppDir's - pip
      silently skips installing it into the AppDir at all: "Requirement
      already satisfied" instead of "Collecting". The AppDir then ships
      without that package, and the built AppImage fails at runtime with
      ``ModuleNotFoundError`` on a host where the build user's home
      directory doesn't happen to carry the same leftover package -
      while looking, on the build host itself, exactly like a successful
      build. Confirmed by hand: a build on a machine with an unrelated
      ``typing_extensions`` already installed under ``~/.local`` silently
      omitted it from the AppDir entirely, in a project that has it as a
      real, needed dependency.

    Also pins ``PYTHONHASHSEED``/``LC_ALL``/``TZ`` so pip's and the build
    backend's own behavior can't vary with the build host's hash
    randomization seed, locale, or timezone, the same reasoning as
    ``_ensure_reproducible_process_env`` in ``__main__.py`` for this
    project's own process. ``TZ=UTC`` matters here specifically because
    this environment also reaches arbitrary third-party code this project
    doesn't control - a build backend's ``setup.py``, a dependency's own
    build-time code generation, a ``post_install``/``pre_package`` hook -
    any of which could format the current wall-clock time (e.g.
    ``datetime.now()``, a shelled-out ``date``) into something that ends
    up installed. Unlike ``SOURCE_DATE_EPOCH``, nothing here can force such
    code to use a *fixed* instant instead of the real one, but pinning the
    timezone at least makes that formatted string the same across build
    hosts in different timezones at the same moment, rather than adding a
    second, independent source of cross-machine variance on top.

    Every subprocess that runs the bundled interpreter for an install -
    ``pip install`` or ``pip lock`` alike - gets this environment so none
    of them can reintroduce any of these leaks.
    """
    return {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONHASHSEED": "0",
        "LC_ALL": "C",
        "TZ": "UTC",
    }


def _install_hashed_requirement(
    requirement: str,
    sha256: str,
    python_bin: Path,
    project_root: Path,
) -> None:
    """Install a single *requirement* with pip's hash-checking mode.

    pip has no standalone ``--hash`` CLI flag for ``pip install`` - hash
    pins only work via the requirements-file syntax - so this writes a
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
            env=_isolated_subprocess_env(),
            check=True,
        )


def _install_targets(
    resolved: _ResolvedBuild,
    python_bin: Path,
    project_root: Path,
) -> None:
    """Install ``install_targets``, hash-verifying ``appimage_pin`` when possible.

    Used only when ``pylock`` isn't configured - with it, ``appimage_pin``
    is already hash-pinned inside ``pylock.toml`` (see ``_generate_lock``/
    ``_strip_local_directory_entries``). Without it, the rest of
    ``install_targets`` (the local project, ``config.packages``) stays
    unverified as always, but ``appimage_pin``'s exact version is always
    independently knowable in advance - either explicitly via
    ``appimage_version``, or the currently-running ``appimage.ctl``
    release itself - so it gets the same free auto-verification treatment
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
        env=_isolated_subprocess_env(),
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
    bytecode yet - but ``--no-compile`` only suppresses pip's own
    post-install compile step, not bytecode CPython's import machinery
    writes out on its own the moment something merely *imports* a module
    (a ``post_install``/``pre_package`` hook, some package's own install
    hook, anything). Such a stray ``.pyc`` is timestamp-invalidated, not
    hash-invalidated, and without ``-f`` here ``compileall`` treats it as
    already up to date and leaves that install-time timestamp in place -
    the exact non-determinism this function exists to eliminate,
    reintroduced through a side door (an earlier ``--no-build-isolation``
    approach for ``build_pylock`` used to trigger exactly this by running
    the build backend inside the same interpreter; the fix there was to
    stop doing that, but ``-f`` stays as a general safeguard against any
    other organic import doing the same). ``-f`` forces every ``.pyc`` to
    be regenerated here, in ``unchecked-hash`` mode, regardless of what
    already exists.

    ``-s`` strips *site_packages* from the ``co_filename`` every
    compiled code object carries - without it, each one embeds the
    *absolute* build path (e.g. ``/home/alice/project/build/AppDir/...``),
    baking the building machine's directory layout, and typically the
    building user's name, into every single compiled file, not just
    stray ones. ``_scrub_build_paths`` (later) closes the same class of
    leak for what's left: install-time metadata and script shims that
    ``-s`` doesn't touch.

    Run once, at the very end of AppDir assembly - after ``post_install``,
    extra files, and the ``pre_package`` hook - so any step that edits an
    installed package's source is reflected in the compiled cache.

    Deliberately single-threaded (no ``-j``): parallel workers finish in
    scheduling-dependent order, which changes the order ``.pyc`` files are
    created inside each ``__pycache__`` directory between runs - squashfs
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
        env=_isolated_subprocess_env(),
        check=True,
    )


def _find_build_path_leaks(appdir: Path, marker: bytes) -> list[Path]:
    """Return every regular file under *appdir* whose content still contains *marker*."""
    return [
        path
        for path in appdir.rglob("*")
        if path.is_file() and not path.is_symlink() and marker in path.read_bytes()
    ]


def _self_locating_python(python_bin_name: bytes) -> bytes:
    """Return a POSIX-sh expression that finds *python_bin_name* next to the running script.

    Same pattern python-build-standalone's own bootstrap ``pip``/``pip3``
    scripts already use for exactly this reason - confirmed by hand: still
    runs correctly after moving the whole AppDir to an unrelated path,
    since ``dirname``/``realpath`` resolve relative to ``$0`` (the script's
    own, current location on disk) rather than the absolute path it
    happened to be installed at. Not a pip/distlib feature - pip's own
    script generator (``pip._vendor.distlib.scripts.ScriptMaker``) always
    writes a plain, non-relocatable absolute path; python-build-standalone
    applies this trick itself, only to its own bundled scripts.
    """
    return b'"$(dirname -- "$(realpath -- "$0")")/' + python_bin_name + b'"'


def _relocate_console_script(content: bytes, executable: bytes) -> bytes | None:
    """Rewrite a pip-generated console-script shim to find its interpreter relative to itself.

    Matches exactly the two shebang forms ``pip._vendor.distlib.scripts.
    ScriptMaker._build_shebang`` ever produces for a POSIX target - a plain
    ``#!<executable>`` when short enough, otherwise a two-line ``#!/bin/sh``
    + polyglot ``exec`` fallback (triggered past 127 bytes on Linux, 512 on
    macOS). Returns ``None``, deferring to deletion, for anything that
    doesn't match either byte-for-byte - deliberately narrow rather than a
    loose regex: virtualenv shipped a general-purpose ``--relocatable``
    doing the analogous rewrite for years and eventually removed it
    (unreliable, mainly around compiled-code packages - see
    https://github.com/pypa/virtualenv/issues/90) - better to fall back to
    the always-safe delete than to guess at a format distlib didn't
    actually write and risk producing a script that's broken in a new way.

    Both input forms are rewritten to the two-line ``#!/bin/sh`` + polyglot
    ``exec`` form, never to a plain one-line ``#!<replacement>``: the kernel
    never shell-expands a ``#!`` line - it passes everything after ``#!`` to
    ``execve()`` literally - so a self-locating replacement (which embeds a
    ``$(dirname ...)`` command substitution, see ``_self_locating_python``)
    is only ever runnable via a real shell, which only the two-line form
    invokes. A one-line input's original shebang was short enough to fit
    the OS's shebang-length limit, but the *replacement* isn't a literal
    path, so that limit is beside the point here - confirmed by hand: a
    plain ``#!"$(dirname ...)/python3"`` line fails at exec time with
    "bad interpreter: no such file or directory", even unmoved, since
    ``$(...)`` is never evaluated.

    *executable* is also tried wrapped in double quotes: ``pip._vendor.
    distlib.scripts.enquote_executable`` wraps it in ``"..."`` whenever it
    contains a space (e.g. a build path with a space in it) before
    ``_build_shebang`` embeds it - and a space anywhere also forces the
    two-line form regardless of length, since ``_build_shebang`` treats any
    embedded space as "too complex" for the simple one-line form the same
    way it treats an overlong path. Confirmed by hand: without matching the
    quoted form too, a build path with a space in it silently fell through
    to the delete fallback below for every console-script shim, dropping
    ``AppDir/python/bin/<entry-point>`` entirely instead of relocating it.

    Parameters
    ----------
    content : bytes
        The shim file's current content.
    executable : bytes
        The absolute interpreter path distlib embedded (what installed it,
        e.g. ``.../AppDir/python/bin/python3``) - must match exactly
        (optionally double-quoted) for either pattern to be recognized.

    Returns
    -------
    bytes | None
        The rewritten content, or ``None`` if *content* doesn't match a
        known shim format exactly.

    """
    python_bin_name = executable.rsplit(b"/", 1)[-1]
    replacement = _self_locating_python(python_bin_name)
    new_prefix = b"#!/bin/sh\n'''exec' " + replacement + b' "$0" "$@"\n' + b"' '''\n"

    for embedded in (executable, b'"' + executable + b'"'):
        two_line = b"#!/bin/sh\n'''exec' " + embedded + b' "$0" "$@"\n' + b"' '''\n"
        if content.startswith(two_line):
            return new_prefix + content[len(two_line) :]

        one_line = b"#!" + embedded + b"\n"
        if content.startswith(one_line):
            return new_prefix + content[len(one_line) :]

    return None


def _record_hash_field(content: bytes) -> str:
    """Return a RECORD-format hash field (``sha256=<urlsafe-base64-no-padding>``) for *content*."""
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
    return f"sha256={digest.decode()}"


def _scrub_record_row(
    target: Path,
    row: list[str],
    appdir_marker: bytes,
    executable: bytes,
) -> list[str] | None:
    """Handle one RECORD row for ``_scrub_build_paths``: delete, relocate, or keep as-is.

    Returns the row to keep (unchanged, or rewritten with a relocated
    file's new hash/size), or ``None`` if *target* was deleted.
    """
    if not (target.is_file() and not target.is_symlink()):
        return row

    if target.name == "direct_url.json":
        target.unlink()
        return None

    content = target.read_bytes()
    if appdir_marker not in content:
        return row

    relocated = _relocate_console_script(content, executable)
    if relocated is None:
        target.unlink()
        return None

    target.write_bytes(relocated)
    return [row[0], _record_hash_field(relocated), str(len(relocated))]


def _scrub_build_paths(resolved: _ResolvedBuild, appdir: Path) -> None:
    """Remove or fix up the build machine's absolute path in install-time artifacts.

    ``pip`` writes two kinds of file that embed the absolute path it ran
    from - which varies by machine, checkout location, and (via the home
    directory) the building user's own name, none of which has any
    business ending up in a shipped, redistributable AppImage:

    - Each local install's ``direct_url.json`` (PEP 610), recording the
      ``file://`` source URL it was installed from. Always deleted: its
      entire purpose is recording *where this came from*, which for an
      AppDir that runs somewhere else entirely isn't just leaked, it's
      meaningless - there's no relocatable version of "the build-time
      path", and nothing reads this file at AppImage runtime.
    - Every console-script shim pip generates for an entry point, whose
      shebang embeds the absolute path to the bundled interpreter (on
      *some* line - pip falls back to a two-line ``#!/bin/sh`` + exec
      trick once the interpreter path is too long for the OS's shebang
      limit, so which line varies). Unlike ``direct_url.json``, these are
      relocated in place (see ``_relocate_console_script``) rather than
      deleted, so e.g. ``AppDir/python/bin/<entry-point>`` still works for
      anyone poking at an extracted AppDir directly - deleted only as a
      fallback if the shim doesn't match a recognized pip/distlib format
      exactly.

    ``AppRun`` never runs either kind of file itself - it execs the
    bundled interpreter directly (see ``templates/AppRun.sh``) - so
    neither is required for the AppImage to work; relocating the scripts
    is for the benefit of anyone using the AppDir directly, not something
    this build depends on. Which files to look at is read from each
    ``RECORD`` - the same manifest pip itself wrote when it installed that
    file - rather than inferred by matching against pip's script format,
    so a future pip version changing that format doesn't silently defeat
    this: any RECORD-listed file that still contains the build path once
    inspected gets handled, whatever it looks like. A relocated file's
    ``RECORD`` entry is rewritten with its new hash/size rather than left
    stale, matching what pip itself would have written for that content.

    Run late, after ``_compile_pyc``: a ``post_install``/``pre_package``
    hook installing something of its own could produce either artifact
    too. Ends with a whole-AppDir sweep for the same marker as a backstop
    - the exact class of bug ``_normalize_mtimes`` exists to close for
    timestamps, just for paths instead - and raises if anything is still
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
    executable = str(appdir / "python" / "bin" / "python3").encode()

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
                kept_row = _scrub_record_row(target, row, appdir_marker, executable)
                if kept_row is None:
                    changed = True
                    continue
                if kept_row != row:
                    changed = True
                new_rows.append(kept_row)

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
    generated at different wall-clock times - which they always are, one
    build run to the next. appimagetool exposes no flag to normalize this
    itself, so it has to happen here, on the fully assembled AppDir, right
    before packaging.

    This alone is not sufficient - appimagetool touches a few paths of its
    own during packaging (e.g. ``.DirIcon``), which need ``SOURCE_DATE_EPOCH``
    set in *its* environment to normalize too; see ``build()``.
    """
    for root, dirs, files in os.walk(appdir):
        for name in (*dirs, *files):
            os.utime(Path(root) / name, (epoch, epoch), follow_symlinks=False)
    os.utime(appdir, (epoch, epoch), follow_symlinks=False)


def _normalize_permissions(appdir: Path) -> None:
    """Clear the group- and other-write bits on every file and directory in *appdir*.

    ``mksquashfs`` embeds each inode's permission bits into the packed
    image, and those bits can end up group-writable depending on how a
    given build host's umask interacted with the permissions already
    stored in an installed package's own files (e.g. a python-build-
    standalone tarball's archived modes) - independent of the umask the
    build was actually invoked under. Two AppDirs with byte-identical
    file *content* have been observed to differ only in these bits
    (``0775``/``0664`` on one machine, ``0755``/``0644`` on another),
    which alone is enough to make the final ``.AppImage`` differ.
    Clearing them (equivalent to enforcing umask ``022`` after the
    fact) is safe: nothing in an AppDir should rely on group/other
    write access to its own bundled files.
    """
    for root, dirs, files in os.walk(appdir):
        for name in (*dirs, *files):
            path = Path(root) / name
            if path.is_symlink():
                continue
            mode = path.stat().st_mode
            path.chmod(mode & ~0o022)
    appdir.chmod(appdir.stat().st_mode & ~0o022)


def _copy_assets(resolved: _ResolvedBuild, project_root: Path, appdir: Path) -> None:
    """Copy icon, desktop file, and AppRun script into AppDir."""
    _log.info("Copying assets...")
    if resolved.icon:
        if not resolved.icon.exists():
            msg = f"icon not found: {resolved.icon}"
            raise FileNotFoundError(msg)
        shutil.copy2(resolved.icon, appdir / (resolved.app + resolved.icon.suffix))
    if resolved.desktop:
        if not resolved.desktop.exists():
            msg = f"desktop file not found: {resolved.desktop}"
            raise FileNotFoundError(msg)
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
    # https://reproducible-builds.org/specs/source-date-epoch/ - respected
    # both here and, for a full build, again in appimagetool's own process
    # environment (it touches a few paths of its own, e.g. .DirIcon, which
    # this AppDir-side normalization can't reach).
    _normalize_mtimes(appdir, epoch)
    _normalize_permissions(appdir)


def _resolve_for_appdir(
    config: BuildConfig,
    project_root: Path,
) -> tuple[_ResolvedBuild, str, Path, Path]:
    """Resolve *config*, print the check report, and derive standard AppDir build paths.

    Shared by ``build_appdir()`` and ``lock()`` (via ``appimage.ctl.lock``)
    - both need exactly this same "resolve, report, enforce
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
    _format_check(resolved, project_root)

    if resolved.appdir_errors:
        raise SystemExit(1)

    arch = platform.machine()
    build_dir = project_root / resolved.build_dir
    appdir = build_dir / "AppDir"
    python_cache = _python_tarball_cache_path(
        build_dir,
        resolved.python,
        resolved.python_date,
    )
    return resolved, arch, appdir, python_cache


def build_appdir(config: BuildConfig, project_root: Path) -> Path:
    """Assemble the AppDir from *config*, without packaging it into an AppImage.

    Everything a full build does through mtime normalization - installing
    Python and packages, copying assets/extra files, running hooks,
    compiling bytecode, and scrubbing build-machine paths - with none of
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
