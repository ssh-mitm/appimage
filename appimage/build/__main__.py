"""Entry point for ``python -m appimage.build``."""

import argparse
import logging
import sys
from pathlib import Path

from appimage.build import BuildConfig, build


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
    parser.add_argument(
        "--app",
        metavar="NAME",
        help="Application name, used as the AppImage filename prefix (overrides pyproject.toml).",
    )
    parser.add_argument(
        "--entry-point",
        dest="entry_point",
        metavar="EP",
        help="Console script entry point for AppRun (overrides pyproject.toml).",
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
        help="python-build-standalone release date for reproducible builds (overrides pyproject.toml).",
    )
    parser.add_argument(
        "--package",
        dest="packages",
        action="append",
        metavar="PKG",
        help="pip install target, repeatable (overrides pyproject.toml).",
    )
    parser.add_argument(
        "--project-dir",
        dest="project_dir",
        default=".",
        metavar="DIR",
        help="Project root directory (default: current directory).",
    )
    return parser.parse_args()


def main() -> None:
    """Build an AppImage for the project in the current directory."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    args = _parse_args()
    project_root = Path(args.project_dir).resolve()

    try:
        config = BuildConfig.from_pyproject(project_root)
    except (FileNotFoundError, ValueError) as exc:
        sys.exit(f"Error: {exc}")

    if args.app:
        config.app = args.app
    if args.entry_point:
        config.entry_point = args.entry_point
    if args.python:
        config.python = args.python
    if args.python_date:
        config.python_date = args.python_date
    if args.packages:
        config.packages = args.packages

    try:
        build(config, project_root)
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        sys.exit(f"Build failed: {exc}")


if __name__ == "__main__":
    main()
