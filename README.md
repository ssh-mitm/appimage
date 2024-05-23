# appimage

## Overview

`appimage` is a Python module that makes it easy to start applications inside an AppImage using AppRun. This module is meant to be used by the AppRun script of an AppImage and not run directly.

Many Python AppImages only allow the execution of a single command, which can be limiting for more complex applications. By using the `appimage` module, you can simplify the initialization process and manage different entry points and virtual environments more easily. This makes it more flexible and convenient to work with Python applications packaged as AppImages.

A working example of its functionality is already used by the [SSH-MITM](https://github.com/ssh-mitm/ssh-mitm) project.


## Features

- Simplifies the startup process for Python applications inside an AppImage.
- Works with virtual Python environments.
- Makes it easy to update the application directly within the virtual environment.

## Example: Creating a Simple AppImage

This example shows how to create a simple AppImage using the Python AppImage from the python-appimage project.

### Steps

1. **Download and extract the Python AppImage:**

    ```sh
    # Download the Python AppImage
    curl -LO "https://github.com/niess/python-appimage/releases/download/python3.11/python3.11.9-cp311-cp311-manylinux2014_x86_64.AppImage"
    chmod +x python3.11.9-cp311-cp311-manylinux2014_x86_64.AppImage
    ./python3.11.9-cp311-cp311-manylinux2014_x86_64.AppImage --appimage-extract
    ```

2. **Install the desired Python package (e.g., ssh-mitm):**

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
    exec "$APPDIR/opt/python3.11/bin/python3.11" -m appimage ssh-mitm "$@"
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

```sh
./ssh-mitm.AppImage --python-help
usage: ssh-mitm.AppImage [--python-help | --python-interpreter | --python-venv PYTHON_VENV_DIR | --python-entry-point PYTHON_ENTRY_POINT] default_entry_point

positional arguments:
  default_entry_point   Entry point to start.

options:
  --python-help         Show this help message and exit.
  --python-interpreter  Start the Python interpreter.
  --python-venv PYTHON_VENV_DIR
                        Create a virtual environment pointing to the AppImage.
  --python-entry-point PYTHON_ENTRY_POINT
                        Start a Python entry point from console scripts (e.g., ssh-mitm).
```

**Parameter Description**

- **default_entry_point** Specifies the main entry point for starting the application. This is a required positional argument.
- **--python-help** Displays the help message and exits. Use this option to see the available commands and their usage.
- **--python-interpreter** Starts the Python interpreter included within the AppImage. This is useful for running Python commands interactively.
- **--python-venv PYTHON_VENV_DIR**: Creates a virtual environment in the specified directory (`PYTHON_VENV_DIR`) that points to the Python installation within the AppImage. This virtual environment includes all the Python packages available in the AppImage, making it convenient for setting up an isolated environment with the necessary dependencies for your Python applications.
- **--python-entry-point PYTHON_ENTRY_POINT**: Executes a specified Python entry point from the console scripts (e.g., `ssh-mitm`) or as a Python entry point (e.g., `ssmitm.cli:main`). This allows you to run specific commands or scripts packaged within the AppImage.
