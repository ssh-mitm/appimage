#!/usr/bin/env bash
# Proves appimage.ctl actually produces a bit-identical .AppImage across two
# independent builds, rather than just claiming it -- the actual claim
# reproducible builds make. Builds examples/myapp twice from scratch and
# diffs the result. Run before a release, and in CI on every push/PR. See
# docs/reproducible-builds.md.
#
# Usage: packaging/verify-appimage-reproducible-build.sh
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -m pip install . --no-deps --quiet

EXAMPLE_DIR="examples/myapp"
OUT_A=$(mktemp -d)
OUT_B=$(mktemp -d)
trap 'rm -rf "$OUT_A" "$OUT_B" "$EXAMPLE_DIR/build" "$EXAMPLE_DIR/dist"' EXIT

for OUT in "$OUT_A" "$OUT_B"; do
    echo "==> building into $OUT"
    rm -rf "$EXAMPLE_DIR/build" "$EXAMPLE_DIR/dist"
    (cd "$EXAMPLE_DIR" && python3 -m appimage.ctl)
    cp "$EXAMPLE_DIR"/dist/*.AppImage "$OUT/"
done

APPIMAGE_A=("$OUT_A"/*.AppImage)
APPIMAGE_B=("$OUT_B"/*.AppImage)

SHA_A=$(sha256sum "${APPIMAGE_A[0]}" | cut -d' ' -f1)
SHA_B=$(sha256sum "${APPIMAGE_B[0]}" | cut -d' ' -f1)

if [ "$SHA_A" != "$SHA_B" ]; then
    echo "NOT REPRODUCIBLE: two independent builds produced different AppImages" >&2
    echo "  ${APPIMAGE_A[0]}: $SHA_A" >&2
    echo "  ${APPIMAGE_B[0]}: $SHA_B" >&2
    exit 1
fi

echo "OK: bit-identical AppImage across two independent builds ($SHA_A)"
