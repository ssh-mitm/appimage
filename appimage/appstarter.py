"""Module for initializing applications within an AppImage via AppRun.

This module is designed to be invoked by the AppRun script of an AppImage and is not intended
for direct execution. The module includes the AppStarter class, which orchestrates the application
startup process based on configurations defined in a .ini file, controlling environment variables,
interpreter access, entry point restrictions, and default commands.

The .ini configuration file must be named 'appimage.ini' and located within the root firectory
of an AppImage next to the AppRun.

The provided AppRun bash script sets up the necessary environment and invokes the application
using this module. It should be located at the root of the AppImage filesystem.

Configuration:
--------------


Intended Usage:
---------------

This module is used within an AppImage environment, with the AppRun entry point calling the
`start_entry_point` function provided by this module. AppStarter reads the configurations,
determines the appropriate entry point, and initiates the application.
"""

import argparse
import os
import shutil
import site
import sys
from functools import cached_property
from typing import TYPE_CHECKING, Dict, List, Optional
from venv import EnvBuilder

if sys.version_info >= (3, 11):
    from importlib.metadata import EntryPoint, entry_points
else:
    from importlib_metadata import EntryPoint, entry_points

if TYPE_CHECKING:
    from types import SimpleNamespace


def patch_appimage_venv(context: "SimpleNamespace") -> None:
    symlink_target = "python3"
    # if executed as AppImage override python symlink
    # this is not relevant for extracted AppImages
    appimage_path = os.environ.get("APPIMAGE")
    appdir = os.environ.get("APPDIR")
    if not appimage_path or not appdir or sys.version_info < (3, 10):
        sys.exit("venv command only supported by AppImages")

    # replace symlink to appimage instead of python executable
    python_path = os.path.join(context.bin_path, symlink_target)
    os.remove(python_path)
    os.symlink(appimage_path, python_path)

    eps = entry_points()
    scripts = eps.select(group="console_scripts")  # type: ignore[attr-defined, unused-ignore] # ignore old python < 3.10
    for ep in scripts:
        ep_path = os.path.join(context.bin_path, ep.name)
        if os.path.isfile(ep_path):
            continue
        os.symlink(symlink_target, ep_path)


def setup_python_patched(self: EnvBuilder, context: "SimpleNamespace") -> None:
    # call monkey patched function
    self.setup_python_original(context)  # type: ignore[attr-defined]
    patch_appimage_venv(context)


class AppStartException(Exception):
    """Base exception class for errors during the app start process."""


class InvalidEntryPoint(AppStartException):
    """Exception raised for invalid entry point."""


class AppStarter:
    """
    Class responsible for managing the application start process, including
    reading the configuration, determining the correct entry point, and
    executing the application.
    """

    def __init__(self) -> None:
        """
        Initializes the AppStarter instance by reading the default configuration
        and any existing 'appimage.ini' configuration file in the APPDIR.
        """
        self.default_ep: Optional[str] = None
        self.subprocess_args: Optional[List[str]] = None
        self.appimage = os.path.abspath(os.environ.get("APPIMAGE"))
        argv0_complete = os.environ.get("ARGV0", None)
        self.argv0 = os.path.basename(argv0_complete) if argv0_complete else None
        self.env_ep = os.environ.get("APP_ENTRY_POINT")
        self.virtual_env = os.environ.get("VIRTUAL_ENV")

    @cached_property
    def appdir(self) -> str:
        """
        Get the application directory from the 'APPDIR' environment variable.
        If 'APPDIR' is not set in the environment, it defaults to the directory
        containing the current file (__file__).

        Returns:
            str: The path to the application directory.
        """
        if "APPDIR" not in os.environ:
            os.environ["APPDIR"] = os.path.dirname(__file__)  # noqa: PTH120
        return os.environ["APPDIR"]

    @cached_property
    def entry_points(self) -> Dict[str, EntryPoint]:
        eps = entry_points()
        scripts = eps.select(group="console_scripts")  # type: ignore[attr-defined, unused-ignore] # ignore old python < 3.10
        script_eps = {}
        for ep in scripts:
            script_eps[ep.name] = ep
            script_eps[ep.value] = ep
        return script_eps

    def get_entry_point(self, *, ignore_default: bool = False) -> Optional[EntryPoint]:

        if self.env_ep and self.env_ep in self.entry_points:
            return self.entry_points[self.env_ep]
        if self.argv0 and self.argv0 in self.entry_points:
            return self.entry_points[self.argv0]
        if (
            not ignore_default
            and self.default_ep
            and self.default_ep in self.entry_points
        ):
            return self.entry_points[self.default_ep]
        return None

    def start_entry_point(self) -> None:
        """
        Load a module and execute the function specified by the entry point.
        The entry point is a string in the 'module:function' format.

        Raises:
            InvalidEntryPoint: If the entry point does not exist.
        """
        if self.virtual_env:
            sys.executable = os.path.join(self.virtual_env, "bin/python3")
        entry_point = self.get_entry_point()
        if entry_point:
            entry_point_loaded = entry_point.load()
            sys.exit(entry_point_loaded())

        error_msg = f"'{self.env_ep or self.default_ep or self.argv0}' is not a valid entry point!"
        raise InvalidEntryPoint(error_msg)

    def start_interpreter(self) -> None:
        """Start an interactive Python interpreter using the current Python executable.

        It passes any additional arguments provided in the command line to the interpreter.
        """
        if sys.executable == self.appimage:
            sys.exit(
                "Can not start interpreter!\n"
                "The appimage module is not compatible with python-appimage because of unclean patches in encodings module!\n"
                "Those patches changes the 'sys.executable' path for the whole python environment to point to the AppImage file.\n"
                "Changing this path for all modules can result in an execution loop. Aborting interpreter execution!"
            )
        args = [sys.executable, "-P"]
        if len(self.subprocess_args) > 1:
            args.extend(self.subprocess_args[1:])
        os.execvp(  # nosec # noqa: S606 # Starting a process without a shell
            sys.executable, args
        )

    def create_venv(self, venv_dir: str) -> None:
        if not hasattr(EnvBuilder, "setup_python_original"):
            # ignore type errors from monkey patching
            EnvBuilder.setup_python_original = EnvBuilder.setup_python  # type: ignore[attr-defined]
            EnvBuilder.setup_python = setup_python_patched  # type: ignore[method-assign]

        builder = EnvBuilder(symlinks=True)
        builder.create(venv_dir)
        sys.exit()

    def parse_python_args(self) -> None:
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument(
            'default_entry_point',
            type=str,
            help='entry point to start.'
        )
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--python-help",
            action="help",
            default=argparse.SUPPRESS,
            help="Show this help message and exit.",
        )
        group.add_argument(
            "--python-interpreter",
            dest="python_interpreter",
            action="store_true",
            help="start the python intrpreter",
        )
        group.add_argument(
            "--python-venv",
            dest="python_venv_dir",
            help="creates a virtual env pointing to the AppImage",
        )
        group.add_argument(
            "--python-entry-point",
            dest="python_entry_point",
            help="start a python entry point from console scripts (e.g. ssh-mitm)",
        )

        args, subprocess_args = parser.parse_known_args()
        self.default_ep = args.default_entry_point
        sys.argv = self.subprocess_args =  sys.argv[:1] + subprocess_args
        if args.python_interpreter:
            self.start_interpreter()
        if args.python_venv_dir:
            self.create_venv(args.python_venv_dir)
        if args.python_entry_point:
            self.env_ep = args.python_entry_point

    def start(self) -> None:
        """
        Determine the entry point and start it. If an interpreter is requested via
        environment variables, or if no entry point is found, it starts an interpreter.
        Otherwise, it starts the determined entry point.
        """
        if sys.version_info < (3, 10):
            sys.exit(f"App starter for {self.argv0} requires Python 3.10 or later")
        self.parse_python_args()
        if (
            not self.default_ep and not self.env_ep and not self.get_entry_point()
        ) or self.argv0 in ["python", "python3", f"python3.{sys.version_info[1]}"]:
            self.start_interpreter()
        self.start_entry_point()


    def setup_virtualenv(self):

        def find_link(path: str) -> str:
            try:
                link = os.readlink(path)
                return find_link(link)
            except OSError:  # if the last is not symbolic file will throw OSError
                return path

        # Check if VIRTUAL_ENV is set and if the resolved python3 matches APPIMAGE
        if "VIRTUAL_ENV" in os.environ:
            resolved_python3 = os.path.abspath(
                find_link(os.path.join(os.environ["VIRTUAL_ENV"], "bin", "python3"))
            )
            if resolved_python3 == self.appimage:
                os.environ.pop("PYTHONNOUSERSITE", None)
                os.environ["PYTHONUSERBASE"] = os.environ["VIRTUAL_ENV"]
                os.environ["PATH"] = f"{os.environ['VIRTUAL_ENV']}/bin:{os.environ['PATH']}"
                site.USER_BASE = os.environ["VIRTUAL_ENV"]
                site.USER_SITE = os.path.join(
                    site.USER_BASE,
                    "lib",
                    f"python{sys.version_info[0]}.{sys.version_info[1]}",
                    "site-packages",
                )
                sys.path.insert(0, site.USER_SITE)
                return

        # Determine the command path
        if not self.argv0:
            return
        if "/" in self.argv0:
            cmd_path = self.argv0
        else:
            cmd_path = shutil.which(self.argv0) or "AppRun"

        # If environment not loaded and CMD_PATH is a symlink
        if not os.path.islink(cmd_path):
            return

        symlink_path = os.path.abspath(cmd_path)
        while os.path.islink(symlink_path):
            venv_dir = os.path.dirname(os.path.dirname(symlink_path))
            pyvenv_cfg = os.path.join(venv_dir, "pyvenv.cfg")
            activate_script = os.path.join(venv_dir, "bin", "activate")
            python_symlink = os.path.join(venv_dir, "bin", "python3")

            # Check if the potential VENV_DIR is valid
            if (
                os.path.isfile(pyvenv_cfg)
                and os.path.isfile(activate_script)
                and os.path.islink(python_symlink)
            ):
                if (
                    os.path.abspath(find_link(os.path.join(venv_dir, "bin", "python3")))
                    == self.appimage
                ):
                    # Execute the activation script
                    os.environ.pop("PYTHONNOUSERSITE", None)
                    os.environ["PYTHONUSERBASE"] = venv_dir
                    os.environ["PATH"] = f"{venv_dir}/bin:{os.environ['PATH']}"
                    site.USER_BASE = venv_dir
                    site.USER_SITE = os.path.join(
                        site.USER_BASE,
                        "lib",
                        f"python{sys.version_info[0]}.{sys.version_info[1]}",
                        "site-packages",
                    )
                    sys.path.insert(0, site.USER_SITE)
                    break

            # Resolve one level of symlink without following further symlinks
            resolved_link = os.readlink(symlink_path)
            symlink_path = os.path.realpath(resolved_link)


def start_entry_point() -> None:
    """
    Initiates the application start process by creating an instance of the AppStarter class
    and calling its start method. This function acts as an entry point to begin the application's
    execution flow.

    The `start` method of the AppStarter instance will determine the appropriate entry point
    from the available configurations and proceed to execute it. If the `start` method encounters
    any issues that it cannot handle (such as configuration errors, missing entry points, etc.),
    it will raise an AppStartException.
    """
    if not os.environ.get("APPDIR"):
        sys.exit("This module must be started from an AppImage!")
    appstarter = AppStarter()
    try:
        appstarter.setup_virtualenv()
        appstarter.start()
    except AppStartException as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    start_entry_point()
