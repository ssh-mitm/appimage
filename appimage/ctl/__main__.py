# Copyright 2023-2026 SSH-MITM Dev-Team. All rights reserved.
"""Entry point for ``python -m appimage.ctl`` (also installed as ``appimagectl``)."""

import argparse
import logging
import sys
from pathlib import Path
from typing import Final

from appimage.ctl import (
    BuildConfig,
    build,
    build_appdir,
    check,
    enable_reproducible,
    lock,
    write_config,
)


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the build-configuration overrides shared by every subcommand."""
    parser.add_argument(
        "--app",
        metavar="NAME",
        help="Application name (overrides pyproject.toml).",
    )
    parser.add_argument(
        "--entry-point",
        dest="entry_point",
        metavar="EP",
        help="Console script entry point (overrides pyproject.toml).",
    )
    parser.add_argument(
        "--python",
        metavar="MINOR",
        help="Python minor version to bundle, e.g. 3.11 (overrides pyproject.toml).",
    )
    parser.add_argument(
        "--python-date",
        dest="python_date",
        metavar="DATE",
        help="python-build-standalone release date for reproducible builds.",
    )
    parser.add_argument(
        "--extras",
        dest="extras",
        action="append",
        metavar="EXTRA",
        help="Package extras to install, repeatable (overrides pyproject.toml).",
    )
    parser.add_argument(
        "--package",
        dest="packages",
        action="append",
        metavar="PKG",
        help="Additional pip install target, repeatable.",
    )
    parser.add_argument(
        "--project-dir",
        dest="project_dir",
        default=".",
        metavar="DIR",
        help="Project root directory (default: current directory).",
    )
    parser.add_argument(
        "--appimagetool",
        dest="appimagetool",
        metavar="PATH",
        help=(
            "Path to a local appimagetool binary. "
            "When omitted, PATH is searched, then the build cache, then a download."
        ),
    )
    parser.add_argument(
        "--appimagetool-version",
        dest="appimagetool_version",
        metavar="LABEL",
        help="Informational label for the pinned appimagetool build (see --appimagetool-sha256).",
    )
    parser.add_argument(
        "--appimagetool-sha256",
        dest="appimagetool_sha256",
        metavar="SHA256",
        help=(
            "Expected sha256 of the appimagetool binary. Verified regardless of "
            "how it was resolved; a mismatch aborts the build. 'init' writes this "
            "automatically."
        ),
    )
    parser.add_argument(
        "--python-archive",
        dest="python_archive",
        metavar="PATH",
        help=(
            "Path to a local python-build-standalone tarball. "
            "When omitted, the build cache is checked, then a download."
        ),
    )
    parser.add_argument(
        "--python-sha256",
        dest="python_sha256",
        metavar="SHA256",
        help=(
            "Expected sha256 of the python-build-standalone tarball. Fresh "
            "downloads are already verified against GitHub's published digest "
            "even without this."
        ),
    )
    parser.add_argument(
        "--runtime-file",
        dest="runtime_file",
        metavar="PATH",
        help=(
            "Path to a local AppImage runtime ELF stub, passed to appimagetool "
            "as --runtime-file. When omitted, the build cache is checked, then "
            "a download."
        ),
    )
    parser.add_argument(
        "--runtime-sha256",
        dest="runtime_sha256",
        metavar="SHA256",
        help=(
            "Expected sha256 of the runtime file. Fresh downloads are already "
            "verified against GitHub's published digest even without this."
        ),
    )
    parser.add_argument(
        "--verify-downloads",
        dest="verify_downloads",
        action="store_true",
        help=(
            "Abort the build if appimagetool, the runtime file, or the Python "
            "archive would otherwise be used unverified (no configured hash and "
            "no digest published for that resolution path), instead of just "
            "warning."
        ),
    )
    parser.add_argument(
        "--require-zsyncmake",
        dest="require_zsyncmake",
        action="store_true",
        help=(
            "Abort the build if update_info is set but zsyncmake is not on "
            "PATH (no .zsync delta-update file would be generated), instead "
            "of just warning."
        ),
    )
    parser.add_argument(
        "--pylock",
        dest="pylock",
        metavar="PATH",
        help=(
            "Path to a hash-pinned pylock.toml for third-party dependencies "
            "(overrides pyproject.toml). Generate it with 'lock'."
        ),
    )
    parser.add_argument(
        "--require-pylock",
        dest="require_pylock",
        action="store_true",
        help=(
            "Abort the build if pylock is not set (dependencies would "
            "otherwise be installed unverified), instead of just warning."
        ),
    )
    parser.add_argument(
        "--build-pylock",
        dest="build_pylock",
        metavar="PATH",
        help=(
            "Path to a hash-pinned pylock-format file constraining the "
            "packaged project's own [build-system].requires (overrides "
            "pyproject.toml). Installed with 'pip install --require-hashes' "
            "before the project itself is installed with "
            "'--no-build-isolation', so its otherwise-fresh isolated build "
            "environment is hash-verified too. Generate it with 'lock', "
            "alongside pylock.toml."
        ),
    )
    parser.add_argument(
        "--require-build-pylock",
        dest="require_build_pylock",
        action="store_true",
        help=(
            "Abort the build if build_pylock is not set (the packaged "
            "project's own build backend would otherwise be installed "
            "unverified), instead of just warning."
        ),
    )
    parser.add_argument(
        "--reproducible",
        dest="reproducible",
        action="store_true",
        help=(
            "Enforce a build that is reproducible across machines and over "
            "time: implies --verify-downloads and --require-zsyncmake, and "
            "requires python_date, appimagetool_sha256, and runtime_sha256 "
            "to already be set (run 'init' first to write them). Does not "
            "resolve or write any values itself — see the 'enable-reproducible' "
            "command to also persist this permanently."
        ),
    )


def _add_uploaded_prior_to_argument(parser: argparse.ArgumentParser) -> None:
    """Add the ``--uploaded-prior-to`` cooldown option used by ``lock``/``enable-reproducible``."""
    parser.add_argument(
        "--uploaded-prior-to",
        dest="uploaded_prior_to",
        metavar="PnD",
        help=(
            "Cooldown window passed through to 'pip lock --uploaded-prior-to' "
            "(ISO 8601 PnD format, e.g. P7D) — excludes packages published "
            "more recently than that from the resolution."
        ),
    )


def _parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns
    -------
    argparse.Namespace
        Parsed arguments.

    """
    common = argparse.ArgumentParser(add_help=False)
    _add_common_arguments(common)

    lock_only = argparse.ArgumentParser(add_help=False)
    _add_uploaded_prior_to_argument(lock_only)

    parser = argparse.ArgumentParser(
        prog="python -m appimage.ctl",
        description="Build a Python application as a self-contained AppImage.",
        parents=[common],
    )
    subparsers = parser.add_subparsers(
        dest="command",
        metavar="COMMAND",
        help="Defaults to building the AppImage when omitted.",
    )
    subparsers.add_parser(
        "check",
        parents=[common],
        help="Show resolved configuration and exit without building.",
    )
    subparsers.add_parser(
        "init",
        parents=[common],
        help=(
            "Write auto-detected values, including the toolchain "
            "reproducibility pins (python_date, appimagetool_sha256, "
            "runtime_sha256), to pyproject.toml and exit."
        ),
    )
    subparsers.add_parser(
        "lock",
        parents=[common, lock_only],
        help=(
            "Generate hash-pinned lock files for third-party dependencies "
            "(pylock.toml) and the project's own build backend "
            "(build_pylock, via 'pip lock', run through the bundled "
            "interpreter) and exit. Writes 'pylock'/'build_pylock' to "
            "pyproject.toml for whichever isn't already set."
        ),
    )
    subparsers.add_parser(
        "enable-reproducible",
        parents=[common, lock_only],
        help=(
            "Pin the toolchain, generate both lock files, then run a real "
            "build with reproducible enforced — and only once that build "
            "succeeds, write reproducible = true to pyproject.toml."
        ),
    )
    subparsers.add_parser(
        "build-appdir",
        parents=[common],
        help=(
            "Assemble the AppDir (Python, packages, assets, hooks, bytecode, "
            "build-path scrubbing) without packaging it into an .AppImage — "
            "the result can be tested, inspected, or deployed some other way. "
            "Only requires the AppDir-side reproducibility pins, not "
            "appimagetool_sha256/runtime_sha256."
        ),
    )
    return parser.parse_args()


_CLI_OVERRIDE_FIELDS: Final = (
    "app",
    "entry_point",
    "python",
    "python_date",
    "extras",
    "packages",
    "appimagetool",
    "appimagetool_version",
    "appimagetool_sha256",
    "python_archive",
    "python_sha256",
    "runtime_file",
    "runtime_sha256",
    "verify_downloads",
    "require_zsyncmake",
    "pylock",
    "require_pylock",
    "build_pylock",
    "require_build_pylock",
    "reproducible",
)


def _apply_cli_overrides(config: BuildConfig, args: argparse.Namespace) -> None:
    """Apply CLI argument overrides to a BuildConfig.

    Each field in ``_CLI_OVERRIDE_FIELDS`` has a same-named CLI destination
    and ``BuildConfig`` attribute; a truthy CLI value overrides the config.
    """
    for name in _CLI_OVERRIDE_FIELDS:
        if value := getattr(args, name):
            setattr(config, name, value)


def main() -> None:
    """Build an AppImage for the project in the current directory."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    args = _parse_args()
    project_root = Path(args.project_dir).resolve()

    try:
        config = BuildConfig.from_pyproject(project_root)
    except FileNotFoundError as exc:
        sys.exit(f"Error: {exc}")

    _apply_cli_overrides(config, args)

    try:
        if args.command == "check":
            ok = check(config, project_root)
            sys.exit(0 if ok else 1)
        elif args.command == "init":
            write_config(config, project_root)
        elif args.command == "lock":
            lock(config, project_root, uploaded_prior_to=args.uploaded_prior_to or "")
        elif args.command == "enable-reproducible":
            enable_reproducible(
                config,
                project_root,
                uploaded_prior_to=args.uploaded_prior_to or "",
            )
        elif args.command == "build-appdir":
            build_appdir(config, project_root)
        else:
            build(config, project_root)
    except (FileNotFoundError, RuntimeError, OSError, ValueError) as exc:
        sys.exit(f"Error: {exc}")


if __name__ == "__main__":
    main()
