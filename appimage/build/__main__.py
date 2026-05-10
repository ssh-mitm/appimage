"""Entry point for ``python -m appimage.build``."""

import argparse
import logging
import sys
from pathlib import Path

from appimage.build import BuildConfig, build, check, write_config


def _parse_args() -> argparse.Namespace:
    """Parse command line arguments.

    Returns
    -------
    argparse.Namespace
        Parsed arguments.

    """
    parser = argparse.ArgumentParser(
        prog="python -m appimage.build",
        description="Build a Python application as a self-contained AppImage.",
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Show resolved configuration and exit without building.",
    )
    mode.add_argument(
        "--init",
        action="store_true",
        help="Write auto-detected values to pyproject.toml and exit.",
    )

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
        "--python-archive",
        dest="python_archive",
        metavar="PATH",
        help=(
            "Path to a local python-build-standalone tarball. "
            "When omitted, the build cache is checked, then a download."
        ),
    )
    return parser.parse_args()


def _apply_cli_overrides(config: BuildConfig, args: argparse.Namespace) -> None:
    """Apply CLI argument overrides to a BuildConfig."""
    if args.app:
        config.app = args.app
    if args.entry_point:
        config.entry_point = args.entry_point
    if args.python:
        config.python = args.python
    if args.python_date:
        config.python_date = args.python_date
    if args.extras:
        config.extras = args.extras
    if args.packages:
        config.packages = args.packages
    if args.appimagetool:
        config.appimagetool = args.appimagetool
    if args.python_archive:
        config.python_archive = args.python_archive


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
        if args.check:
            ok = check(config, project_root)
            sys.exit(0 if ok else 1)
        elif args.init:
            write_config(config, project_root)
        else:
            build(config, project_root)
    except (FileNotFoundError, RuntimeError, OSError, ValueError) as exc:
        sys.exit(f"Error: {exc}")


if __name__ == "__main__":
    main()
