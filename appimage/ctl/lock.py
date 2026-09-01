# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""The ``lock`` subcommand: generate hash-pinned lock files for reproducible installs."""

import logging
import re
import shutil
import subprocess  # nosec B404
import tomllib
from pathlib import Path
from typing import Final

from appimage.ctl._base import BuildConfig, _ResolvedBuild
from appimage.ctl._python import _install_python, _pip_version
from appimage.ctl._toml import _toml_value
from appimage.ctl.build_appdir import _isolated_subprocess_env, _resolve_for_appdir

_log: Final = logging.getLogger(__name__)

# `pip lock` (generates pylock.toml) needs pip >= 25.1; `pip install -r
# pylock.toml` (consumed by `_install_from_pylock`) needs pip >= 26.1. Only
# the generation side is checked here — by the time a build tries to
# install from an existing pylock.toml, a hard pip error surfaces the same
# problem anyway, and duplicating the check there would mean parsing pip's
# version on every single build instead of only on `lock`.
_MIN_PIP_LOCK_VERSION: Final = (25, 1)


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
        env=_isolated_subprocess_env(),
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
    resolved, arch, appdir, python_cache = _resolve_for_appdir(config, project_root)

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
