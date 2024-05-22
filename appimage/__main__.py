"""appimage module.

This module initializes applications within an AppImage by calling the appimage module.

Example usage:
    python -m appimage <default_entry_point>
"""

from appimage.appstarter import start_entry_point

if __name__ == "__main__":
    start_entry_point()
