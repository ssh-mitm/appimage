# appimage

## Overview

**The `appimage` module simplifies starting Python applications within an AppImage using AppRun.**

Many AppImages allow only the execution of a single command, which can be limiting for more complex applications.
This module helps manage various entry points and virtual environments more effectively, making it easier to work with Python applications packaged as AppImages.

The [SSH-MITM](https://github.com/ssh-mitm/ssh-mitm) project uses this module to facilitate plugin development and simplify further development by providing access to the integrated Python environment.

_**Note:** This module is used by the AppRun script of an AppImage and is not intended to be executed directly._



## Features

- Simplifies the startup process for Python applications inside an AppImage.
- Works with virtual Python environments.
- Makes it easy to update the application directly within the virtual environment.

## Example: Creating a Simple AppImage

This example shows how to create a simple AppImage using the Python AppImage from the [python-appimage](https://github.com/niess/python-appimage) project.

### Steps

1. **Download and extract the Python AppImage:**

    ```sh
    # Download the Python AppImage
    curl -LO "https://github.com/niess/python-appimage/releases/download/python3.11/python3.11.9-cp311-cp311-manylinux2014_x86_64.AppImage"
    chmod +x python3.11.9-cp311-cp311-manylinux2014_x86_64.AppImage
    ./python3.11.9-cp311-cp311-manylinux2014_x86_64.AppImage --appimage-extract
    ```

2. **Install the `appimage` package along with the desired application (e.g., `ssh-mitm`):**

    ```sh
    ./squashfs-root/opt/python3.11/bin/python3.11 -m pip install appimage ssh-mitm
    ```

3. **Edit the existing AppRun script:**

    Open the `AppRun` script located in `squashfs-root/AppRun` with a text editor (e.g., vim, nano, gedit). For example, using nano:

    ```sh
    nano squashfs-root/AppRun
    ```

    Then, change the last line to:

    ```sh
    # Call Python
    exec "$APPDIR/opt/python3.11/bin/python3.11" -m appimage --python-main ssh-mitm "$@"
    ```

    Save the changes and exit the editor.


4. **Create the new AppImage:**

    ```sh
    # Download appimagetool
    curl -LO "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x appimagetool-x86_64.AppImage

    # Create the new AppImage
    ./appimagetool-x86_64.AppImage squashfs-root/ ssh-mitm.AppImage
    ```

5. **Start the created AppImage**

    ```sh
    ./ssh-mitm.AppImage server
    ```


You now have a new AppImage (`ssh-mitm.AppImage`) that includes the `ssh-mitm` package and uses the `appimage` module to start the application.

## Usage

You can use the following options with the AppImage:

**Parameter Description**

- **--python-help** Displays the help message and exits. Use this option to see the available commands and their usage.
- **--python-main** Specifies the main (default) entry point for starting the application.
- **--python-interpreter** Starts the Python interpreter included within the AppImage. This is useful for running Python commands interactively.
- **--python-venv PYTHON_VENV_DIR**: Creates a virtual environment in the specified directory (`PYTHON_VENV_DIR`) that points to the Python installation within the AppImage. This virtual environment includes all the Python packages available in the AppImage, making it convenient for setting up an isolated environment with the necessary dependencies for your Python applications.
- **--python-entry-point PYTHON_ENTRY_POINT**: Executes a specified Python entry point from the console scripts (e.g., `ssh-mitm`) or as a Python entry point (e.g., `ssmitm.cli:main`). This allows you to run specific commands or scripts packaged within the AppImage.
