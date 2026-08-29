#!/usr/bin/env bash
# Regenerates requirements-build.txt, the hash-pinned PEP 518 build
# dependency (uv_build) used to make appimage's PyPI wheel reproducibly
# buildable via `pip wheel --build-constraint requirements-build.txt`. See
# docs/reproducible-builds.md for the full rationale.
#
# uv_build has no transitive dependencies of its own -- unlike hatchling,
# which this project used before, it ships as a single compiled binary per
# platform (no packaging/pathspec/pluggy/etc. chain to pin alongside it).
# It IS platform-specific, though: `pip-compile --generate-hashes` without
# a wheelhouse restriction resolves every published wheel variant (linux,
# macOS, Windows, multiple arches) under the one version pin, so whichever
# platform actually runs the install (this project's CI only ever builds
# on ubuntu-latest x86_64) still finds its hash in the list.
#
# Usage: packaging/update-requirements.sh [--upgrade]
#   --upgrade   move pins forward to the latest version satisfying
#               pyproject.toml's build-system bounds. A plain re-run keeps
#               existing pins stable (pip-compile's own in-place-compile
#               behavior).
set -euo pipefail
cd "$(dirname "$0")/.."

command -v pip-compile >/dev/null || {
    echo "pip-compile not found -- pip install pip-tools" >&2
    exit 1
}

UPGRADE=()
[ "${1:-}" = "--upgrade" ] && UPGRADE=(--upgrade)

pip-compile --all-build-deps --only-build-deps --generate-hashes \
    --allow-unsafe --no-emit-find-links "${UPGRADE[@]}" \
    pyproject.toml -o requirements-build.txt

echo "==> done: requirements-build.txt"
