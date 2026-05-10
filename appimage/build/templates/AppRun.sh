#!/bin/bash
set -e

{env_block}
if [ -n "$APPIMAGE" ]; then
    appimage_path=$(dirname "$APPIMAGE")
    if [ -d "$appimage_path/squashfs-root" ]; then
        export APPDIR="$appimage_path/squashfs-root"
    fi
fi

if [ -z "$APPDIR" ]; then
    export APPDIR=$(dirname $(readlink -f "$0"))
fi

exec "$APPDIR/python/bin/python3" -P -m appimage --python-main {entry_point} "$@"
