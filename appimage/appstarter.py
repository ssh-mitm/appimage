"""Module for initializing applications within an AppImage via AppRun.

This module is designed to be invoked by the AppRun script of an AppImage and is not intended
for direct execution. The module includes the AppStarter class, which orchestrates the application
startup process based on command line arguments, controlling environment variables, interpreter
access, entry point restrictions, and default commands.

The provided AppRun bash script sets up the necessary environment and invokes the application
using this module. It should be located at the root of the AppImage filesystem.

Command Line Usage:
-------------------

    ./<appimage> --python-help
    ./<appimage> --python-interpreter
    ./<appimage> --python-venv <PYTHON_VENV_DIR>
    ./<appimage> --python-entry-point <PYTHON_ENTRY_POINT>

Arguments:
---------
- **default_entry_point**: The entry point to start the application.
- **--python-help**: Show help message and exit.
- **--python-interpreter**: Start the Python interpreter.
- **--python-venv <PYTHON_VENV_DIR>**: Create a virtual environment in the specified directory
  (PYTHON_VENV_DIR) that points to the Python installation within the AppImage. This is helpful
  for setting up an isolated environment where all Python packages from the AppImage are available.
- **--python-entry-point <PYTHON_ENTRY_POINT>**: Execute a specified Python entry point from the
  console scripts (e.g., "ssh-mitm" or "ssmitm.cli:main"). This allows you to run specific
  commands or scripts packaged within the AppImage.

Intended Usage:
---------------

This module is used within an AppImage environment, with the AppRun entry point calling the
`start_entry_point` function provided by this module. AppStarter reads the command line arguments,
determines the appropriate entry point, and initiates the application.

Example AppRun Script:
----------------------

The AppRun script is a bash script located at the root of the AppImage filesystem. It sets up
the necessary environment and calls the `appimage` module to start the application. Below is
an example AppRun script:

    #!/bin/bash

    set -e

    if [ -z $APPDIR ]; then
        export APPDIR=$(dirname $(readlink -f "$0"))
    fi

    exec "$APPDIR/opt/python3.11/bin/python3.11" -m appimage ssh-mitm "$@"

This script ensures that the APPDIR environment variable is set and then executes the Python
interpreter within the AppImage, invoking the `appimage` module to start the application.

"""

import argparse
import os
import shutil
import site
import sys
from functools import cached_property
from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING, Dict, List, Optional
from venv import EnvBuilder

if TYPE_CHECKING:
    from types import SimpleNamespace


def get_entry_points(group: str) -> List[EntryPoint]:
    """Retrieve a list of entry points for a specified group.

    This function fetches entry points from the current Python environment. It is compatible
    with different Python versions, handling the retrieval process accordingly.

    Attributes
    ----------
        group (str): The entry point group to filter and retrieve.

    Returns
    -------
        List[EntryPoint]: A list of entry points belonging to the specified group.

    """
    eps = entry_points()
    if sys.version_info >= (3, 10):
        return list(eps.select(group=group))
    return list(eps[group])


def patch_appimage_venv(context: "SimpleNamespace") -> None:
    """Patches the virtual environment within an AppImage.

    This function modifies the virtual environment by replacing the Python symlink
    with the AppImage path. It also creates symlinks for console script entry points
    within the virtual environment's bin directory.

    Attributes
    ----------
        context (SimpleNamespace): A namespace object containing the bin_path attribute
                                   which specifies the path to the virtual environment's
                                   bin directory.

    """
    symlink_target = "python3"
    appimage_path = os.environ.get("APPIMAGE", None)
    if not appimage_path:
        # AppImage extracted, so we need to create a fake APPIMAGE variable
        appidir = os.environ.get("APPDIR", None)
        if not appidir:
            # AppImage not properly configured, because APPDIR variable is missing - abort
            sys.exit("APPDIR environment variable missing!")
        appimage_path = os.path.join(appidir, "AppRun")

    # replace symlink to appimage instead of python executable
    venv_python_path = os.path.join(context.bin_path, symlink_target)
    os.remove(venv_python_path)
    os.symlink(appimage_path, venv_python_path)

    scripts = get_entry_points(group="console_scripts")
    for ep in scripts:
        ep_path = os.path.join(context.bin_path, ep.name)
        if os.path.isfile(ep_path):
            continue
        os.symlink(symlink_target, ep_path)


def setup_python_patched(self: EnvBuilder, context: "SimpleNamespace") -> None:
    """Set up a Python environment with additional patching for AppImage virtual environments.

    This function calls the original setup function and then applies additional patches
    to integrate AppImage-specific modifications into the virtual environment.

    Attributes
    ----------
        self (EnvBuilder): The environment builder instance.
        context (SimpleNamespace): The context for the environment setup, containing configuration and state information.

    """
    # call monkey patched function
    self.setup_python_original(context)  # type: ignore[attr-defined]
    patch_appimage_venv(context)


class AppStartExceptionError(Exception):
    """Base exception class for errors during the app start process."""


class InvalidEntryPointError(AppStartExceptionError):
    """Exception raised for invalid entry point."""


class AppStarter:
    """Class responsible for managing the application start process.

    This class handles reading command-line arguments, determining the correct entry point,
    and executing the application. It ensures that the application is initialized
    properly based on the provided command-line arguments and entry points.
    """

    def __init__(self) -> None:
        """Initialize the AppStarter instance.

        This method reads the default configuration and any existing 'appimage.ini' configuration
        file in the APPDIR. It also initializes various attributes based on the current environment
        variables.

        Attributes
        ----------
            default_ep (Optional[str]): The default entry point, initially set to None.
            subprocess_args (Optional[List[str]]): Arguments for subprocesses, initially set to None.
            appimage (Optional[str]): The absolute path to the AppImage, if the APPIMAGE environment
                                    variable is set.
            argv0 (Optional[str]): The base name of the initial command used to invoke the script,
                                if the ARGV0 environment variable is set.
            env_ep (Optional[str]): The entry point specified in the environment variable APP_ENTRY_POINT.
            virtual_env (Optional[str]): The path to the virtual environment, if the VIRTUAL_ENV
                                        environment variable is set.

        """
        self.default_ep: Optional[str] = None
        self.subprocess_args: Optional[List[str]] = None
        appimage_env = os.environ.get("APPIMAGE", None)
        self.appimage = os.path.abspath(appimage_env) if appimage_env else None
        argv0_complete = os.environ.get("ARGV0", None)
        self.argv0 = os.path.basename(argv0_complete) if argv0_complete else None
        self.env_ep = os.environ.get("APP_ENTRY_POINT")
        self.virtual_env = os.environ.get("VIRTUAL_ENV")

    @cached_property
    def is_niess_appimage(self) -> bool:
        """Check if sys.executable is a link or points to the AppImage.

        In such cases the AppImage was built with "niess/python-appimage"
        """
        return os.path.islink(sys.executable) or sys.executable == self.appimage

    @cached_property
    def python_path(self) -> str:
        """Return the path to the python binary included in the AppImage."""
        if self.is_niess_appimage:
            niess_python_path = os.path.join(
                sys.base_prefix,
                "bin",
                f"python{sys.version_info[0]}.{sys.version_info[1]}",
            )
            if os.path.isfile(niess_python_path):
                return niess_python_path
        return sys.executable

    @cached_property
    def appdir(self) -> str:
        """Get the application directory from the 'APPDIR' environment variable.

        If 'APPDIR' is not set in the environment, it defaults to the directory
        containing the current file (__file__).

        Returns
        -------
            str: The application directory specified by 'APPDIR'.

        Raises
        ------
            ValueError: If 'APPDIR' is not set in the environment.

        """
        if "APPDIR" not in os.environ:
            msg = "APPDIR not set - please export APPDIR variable in AppRun"
            raise ValueError(msg)
        return os.environ["APPDIR"]

    @cached_property
    def entry_points(self) -> Dict[str, EntryPoint]:
        """Retrieve and cache the entry points for console scripts.

        This cached property method fetches and stores entry points for console scripts, organizing
        them into a dictionary. Each entry point is indexed by both its name and its value, allowing
        for quick access by either identifier.

        Returns
        -------
            Dict[str, EntryPoint]: A dictionary where the keys are the names and values of the entry
            points, and the values are the EntryPoint objects themselves.

        """
        scripts = get_entry_points(group="console_scripts")
        script_eps = {}
        for ep in scripts:
            script_eps[ep.name] = ep
            script_eps[ep.value] = ep
        return script_eps

    def get_entry_point(self, *, ignore_default: bool = False) -> Optional[EntryPoint]:
        """Retrieve the appropriate entry point based on environment variables and defaults.

        This method determines the entry point to use by checking in the following order:
        1. Environment-specified entry point (`env_ep`).
        2. Command-line argument entry point (`argv0`).
        3. Default entry point (`default_ep`), unless `ignore_default` is True.

        Args:
        ----
            ignore_default (bool): If True, the default entry point (`default_ep`) will be
            ignored. Defaults to False.

        Returns:
        -------
            Optional[EntryPoint]: The selected entry point if found, otherwise None.

        """
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
        """Load a module and execute the function specified by the entry point.

        The entry point is a string in the 'module:function' format.

        Raises
        ------
            InvalidEntryPointError: If the entry point does not exist.

        """
        if self.virtual_env:
            sys.executable = os.path.join(self.virtual_env, "bin/python3")
        entry_point = self.get_entry_point()
        if entry_point:
            entry_point_loaded = entry_point.load()
            sys.exit(entry_point_loaded())

        error_msg = f"'{self.env_ep or self.default_ep or self.argv0}' is not a valid entry point!"
        raise InvalidEntryPointError(error_msg)

    def start_interpreter(self) -> None:
        """Start an interactive Python interpreter using the current Python executable.

        It passes any additional arguments provided in the command line to the interpreter.
        """
        args = [self.python_path]
        if sys.version_info >= (3, 11):
            args.append("-P")
        if self.subprocess_args and len(self.subprocess_args) > 1:
            args.extend(self.subprocess_args[1:])
        os.execvp(  # nosec # noqa: S606 # Starting a process without a shell
            self.python_path,
            args,
        )

    def create_venv(
        self,
        *,
        venv_dirs: str,
        system_site_packages: bool = False,
    ) -> None:
        """Create a virtual environment in the specified directory.

        This function sets up a virtual environment using Python's built-in `venv` module. It first
        checks if the `EnvBuilder` class has been patched to modify its behavior. If not, it applies
        a monkey patch to the `setup_python` method of `EnvBuilder`. The virtual environment is
        created with system site packages enabled and using symlinks.

        Args:
        ----
            venv_dirs (str): The directories where the virtual environments should be created.
            system_site_packages (bool): a Boolean value indicating that the system Python site-packages should be available to the environment

        """
        if not hasattr(EnvBuilder, "setup_python_original"):
            # ignore type errors from monkey patching
            EnvBuilder.setup_python_original = EnvBuilder.setup_python  # type: ignore[attr-defined]
            EnvBuilder.setup_python = setup_python_patched  # type: ignore[method-assign]

        builder = EnvBuilder(system_site_packages=system_site_packages, symlinks=True)
        for venv_dir in venv_dirs:
            builder.create(venv_dir)
        sys.exit()

    def parse_venv_command(self) -> None:
        """Parse command-line arguments for creating virtual Python environments.

        This method sets up an argparse.ArgumentParser to handle arguments related
        to the creation of virtual environments. It includes options for specifying
        target directories and whether to include system site-packages. The method
        also checks for the presence of the '-m venv' command in the arguments.

        The recognized arguments are:
        - ENV_DIR: One or more directories where the virtual environments will be created.
        - --system-site-packages: A flag to allow the virtual environment to access the
        system's site-packages directory.
        - -m: A hidden argument used to detect if the 'venv' module is being invoked.

        If the '-m venv' command is found, the method proceeds to parse the arguments
        and calls `self.create_venv` with the specified directories and options.
        """
        parser = argparse.ArgumentParser(
            prog=__name__,
            description="Creates virtual Python "
            "environments in one or "
            "more target "
            "directories.",
            epilog="Once an environment has been "
            "created, you may wish to "
            "activate it, e.g. by "
            "sourcing an activate script "
            "in its bin directory.",
        )
        parser.add_argument(
            "dirs",
            metavar="ENV_DIR",
            nargs="+",
            help="A directory to create the environment in.",
        )
        parser.add_argument(
            "--system-site-packages",
            default=False,
            action="store_true",
            dest="system_site",
            help="Give the virtual environment access to the "
            "system site-packages dir.",
        )
        parser.add_argument(
            "-m",
            dest="python_module",
            help=argparse.SUPPRESS,
        )
        venv_found = False
        try:
            index = sys.argv.index("-m")
            if sys.argv[index + 1] == "venv":
                venv_found = True
        except (ValueError, IndexError):
            pass
        if not venv_found:
            return
        args = parser.parse_args(sys.argv[1:])
        self.create_venv(
            venv_dirs=args.dirs,
            system_site_packages=self.is_niess_appimage or args.system_site,
        )

    def parse_python_args(self) -> None:
        """Parse command-line arguments for the AppImage execution.

        This function sets up an argument parser to handle various command-line options and
        configures the environment based on the parsed arguments. It supports options for displaying
        help, starting the Python interpreter, creating a virtual environment, and starting a Python
        entry point from console scripts.

        The `default_entry_point` argument is required and specifies the main entry point to start.
        """
        parser = argparse.ArgumentParser(
            prog=self.argv0,
            add_help=False,
            allow_abbrev=False,
        )
        parser.add_argument(
            "--python-help",
            action="help",
            default=argparse.SUPPRESS,
            help="Show this help message and exit.",
        )
        parser.add_argument(
            "--python-main",
            dest="default_entry_point",
            help="entry point to start.",
        )
        group = parser.add_mutually_exclusive_group()
        group.add_argument(
            "--python-interpreter",
            dest="python_interpreter",
            action="store_true",
            help="start the python intrpreter",
        )
        group.add_argument(
            "--python-venv",
            dest="python_venv_dirs",
            metavar="ENV_DIR",
            nargs="+",
            help="Creates a virtual environment pointing to the AppImage.\n"
            "Shortcut for '--python-interpreter -m venv ENV_DIR --system-site-packages'.",
        )
        group.add_argument(
            "--python-entry-point",
            dest="python_entry_point",
            metavar="ENTRY_POINT",
            help="start a python entry point from console scripts (e.g. ssh-mitm)",
        )

        args, subprocess_args = parser.parse_known_args()
        unknown_python_args = [
            arg for arg in subprocess_args if arg.startswith("--python-")
        ]
        if unknown_python_args:
            sys.exit(
                f"{self.argv0}: error: unrecognized python arguments:'{' '.join(unknown_python_args)}'",
            )

        self.default_ep = args.default_entry_point
        sys.argv = self.subprocess_args = sys.argv[:1] + subprocess_args
        if args.python_interpreter:
            self.parse_venv_command()
            self.start_interpreter()
        if args.python_venv_dirs:
            self.create_venv(venv_dirs=args.python_venv_dirs)
        if args.python_entry_point:
            self.env_ep = args.python_entry_point

    def start(self) -> None:
        """Determine the entry point and start it.

        This method determines the appropriate entry point for the application and starts it.
        If an interpreter is requested via environment variables, or if no entry point is found,
        it starts an interpreter. Otherwise, it starts the determined entry point.

        It performs the following steps:
        1. Parses Python arguments to configure the environment.
        2. Checks if a default entry point, environment entry point, or any entry point is available.
        3. If no entry point is found or an interpreter is requested, starts the Python interpreter.
        4. If an entry point is found, starts the determined entry point.
        """
        self.parse_python_args()
        if (
            not self.default_ep and not self.env_ep and not self.get_entry_point()
        ) or self.argv0 in ["python", "python3", f"python3.{sys.version_info[1]}"]:
            self.parse_venv_command()
            self.start_interpreter()
        self.start_entry_point()

    def setup_virtualenv(self) -> None:
        """Set up the virtual environment for the application.

        This function checks if a virtual environment (VIRTUAL_ENV) is set and configures the environment
        variables accordingly. It ensures that the Python user base and site paths are correctly
        set up for the virtual environment.

        If the virtual environment is not already set, it tries to determine the command path
        and resolves any symbolic links to find the appropriate virtual environment directory.
        Once the virtual environment directory is found, it updates the necessary environment
        variables and site paths.

        This function handles both direct execution within a virtual environment and scenarios
        where the command path is a symbolic link to a virtual environment.
        """

        def find_link(path: str) -> str:
            try:
                link = os.readlink(path)
                return find_link(link)
            except OSError:  # if the last is not symbolic file will throw OSError
                return path

        # Check if VIRTUAL_ENV is set and if the resolved python3 matches APPIMAGE
        if "VIRTUAL_ENV" in os.environ:
            resolved_python3 = os.path.abspath(
                find_link(os.path.join(os.environ["VIRTUAL_ENV"], "bin", "python3")),
            )
            if resolved_python3 == self.appimage:
                os.environ.pop("PYTHONNOUSERSITE", None)
                os.environ["PYTHONUSERBASE"] = os.environ["VIRTUAL_ENV"]
                os.environ["PATH"] = (
                    f"{os.environ['VIRTUAL_ENV']}/bin:{os.environ['PATH']}"
                )
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
                and os.path.abspath(find_link(os.path.join(venv_dir, "bin", "python3")))
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
    """Start the application initialization process.

    This function creates an instance of the AppStarter class and calls its start method.
    It acts as an entry point to begin the application's execution flow.

    The `start` method of the AppStarter instance will determine the appropriate entry point
    from the available configurations and proceed to execute it. If the `start` method encounters
    any issues that it cannot handle (such as configuration errors, missing entry points, etc.),
    it will raise an AppStartExceptionError.

    Raises
    ------
        AppStartExceptionError: If the application fails to start due to configuration errors
        or missing entry points.

    """
    if not os.environ.get("APPDIR"):
        sys.exit("This module must be started from an AppImage!")
    appstarter = AppStarter()
    try:
        appstarter.setup_virtualenv()
        appstarter.start()
    except AppStartExceptionError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    start_entry_point()
