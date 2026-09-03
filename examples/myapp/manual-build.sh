#!/usr/bin/env bash
# Fully manual, appimage.ctl-independent AppImage build, with the same
# reproducibility measures appimage.ctl applies. Verification/documentation
# artifact only - not part of the shipped tool. Run twice into different
# OUT_DIR values and diff the resulting .AppImage to prove determinism.
#
# Usage: ./manual-build.sh <out_dir>
set -euo pipefail

OUT_DIR="$1"
APP="myapp"
PYTHON_MINOR="3.11"
ARCH="x86_64"
PBS_DATE="20260901"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_DATE}/cpython-3.11.16%2B${PBS_DATE}-${ARCH}-unknown-linux-gnu-install_only_stripped.tar.gz"
export SOURCE_DATE_EPOCH=0
export PYTHONHASHSEED=0
export LC_ALL=C
export TZ=UTC
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR/AppDir" "$OUT_DIR/dist" build

# Step 2+3 - Python (cached tarball reused between the two calls of this script)
if [ ! -f build/python-cache.tar.gz ]; then
    curl -sL "$PBS_URL" -o build/python-cache.tar.gz
fi
tar -xf build/python-cache.tar.gz -C "$OUT_DIR/AppDir"

PY="$OUT_DIR/AppDir/python/bin/python3"

# Step 4 - install, then compile bytecode hash-based. -s <site-packages>
# strips the absolute build path from every compiled code object's
# co_filename - without it, each .pyc embeds e.g.
# "$OUT_DIR/AppDir/python/lib/.../site-packages/...", baking this build's
# own directory (and, via $OUT_DIR, potentially the building user's name)
# into bytecode that has nothing to do with where it happened to be built.
SITE_PACKAGES="$OUT_DIR/AppDir/python/lib/python${PYTHON_MINOR}/site-packages"
"$PY" -m pip install --no-compile --no-cache-dir --quiet appimage .
"$PY" -m compileall -qf --invalidation-mode unchecked-hash \
    -s "$SITE_PACKAGES" "$SITE_PACKAGES"

# Step 5 - AppRun
cp AppRun "$OUT_DIR/AppDir/AppRun"
chmod +x "$OUT_DIR/AppDir/AppRun"

# Step 6 - .desktop
cp myapp.desktop "$OUT_DIR/AppDir/myapp.desktop"

# Step 7 - icon. myapp has none of its own, so use appimage.ctl's own
# fallback (already sitting inside AppDir after Step 4, no extra download)
# for an exact match with what the real tool produces.
cp "$SITE_PACKAGES/appimage/assets/default_icon.svg" "$OUT_DIR/AppDir/myapp.svg"

# Scrub the build machine's own absolute path: pip's local-install
# direct_url.json (meaningless once the AppDir runs somewhere else) and
# every console-script shim's shebang (rewritten to find its interpreter
# relative to its own location, so it survives being moved/copied
# anywhere - including into a different build's own OUT_DIR, which is
# exactly what makes two otherwise-identical builds diverge if this step
# is skipped). Each dist-info's RECORD is updated to match - pip wrote it
# against the *original* file, so leaving it stale would re-embed the
# same build-path-dependent content pip already wrote into RECORD's own
# stored hash for that row, just one file removed.
python3 - "$SITE_PACKAGES" <<'PYEOF'
import base64
import csv
import hashlib
import pathlib
import sys

site_packages = pathlib.Path(sys.argv[1])
bindir = site_packages.parent.parent.parent / "bin"
appdir_bytes = str(bindir.parent.parent).encode()


def record_hash(content):
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
    return f"sha256={digest.decode()}"


relocated = {}
for script in bindir.iterdir():
    if script.is_symlink() or not script.is_file():
        continue
    content = script.read_bytes()
    if not content.startswith(b"#!" + appdir_bytes):
        continue
    first_line, _, rest = content.partition(b"\n")
    python_bin_name = first_line[2:].rsplit(b"/", 1)[-1]
    replacement = b'"$(dirname -- "$(realpath -- "$0")")/' + python_bin_name + b'"'
    new_content = b"#!/bin/sh\n'''exec' " + replacement + b' "$0" "$@"\n' + b"' '''\n" + rest
    script.write_bytes(new_content)
    relocated[script.resolve()] = new_content

for dist_info in site_packages.glob("*.dist-info"):
    record_path = dist_info / "RECORD"
    if not record_path.exists():
        continue
    rows = list(csv.reader(record_path.read_text().splitlines()))
    new_rows = []
    changed = False
    for row in rows:
        target = (site_packages / row[0]).resolve()
        if target.name == "direct_url.json":
            target.unlink()
            changed = True
            continue
        if target in relocated:
            new_content = relocated[target]
            row = [row[0], record_hash(new_content), str(len(new_content))]
            changed = True
        new_rows.append(row)
    if changed:
        with record_path.open("w", newline="") as f:
            csv.writer(f, lineterminator="\n").writerows(new_rows)
PYEOF

# Normalize permissions (clear group/other write bits) and mtimes
find "$OUT_DIR/AppDir" -not -type l -exec chmod go-w {} +
find "$OUT_DIR/AppDir" -exec touch -h -d @0 {} +

# Step 8 - appimagetool/runtime (cached between the two calls of this script)
if [ ! -f build/appimagetool-cache ]; then
    curl -sL "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage" -o build/appimagetool-cache
    chmod +x build/appimagetool-cache
fi
if [ ! -f build/runtime-cache ]; then
    curl -sL "https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-${ARCH}" -o build/runtime-cache
    chmod +x build/runtime-cache
fi

# Step 9 - pack, with the same mksquashfs flags appimage.ctl passes
build/appimagetool-cache --runtime-file build/runtime-cache \
    --mksquashfs-opt -no-xattrs \
    --mksquashfs-opt -no-duplicates \
    --mksquashfs-opt -processors --mksquashfs-opt 1 \
    "$OUT_DIR/AppDir" "$OUT_DIR/dist/${APP}-${ARCH}.AppImage" >/dev/null

sha256sum "$OUT_DIR/dist/${APP}-${ARCH}.AppImage"
