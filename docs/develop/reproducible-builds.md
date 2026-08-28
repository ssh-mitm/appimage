# Reproducible Wheel Builds

Developer-facing page: this is about **this package's own** PyPI wheel
build hygiene, not about the AppImages `appimage` builds for other
projects — for that, the headline feature, see [Reproducible
builds](../reproducible-builds.md).

The wheel published to [pypi.org](https://pypi.org/project/appimage/) is
built via `hatchling`, the PEP 518 build backend declared in
`[build-system]` in `pyproject.toml`. This page covers how that build is
made hash-verified and provably bit-identical across independent builds of
the same commit — the same pattern used by
[ssh-mitm](https://github.com/ssh-mitm/ssh-mitm/blob/master/doc/develop/reproducible-builds.md)
and [minicms](https://github.com/manfred-kaiser/minicms/blob/main/docs/reproducible-builds.md).

## Hash-pinning the build dependencies

`requirements-build.txt` pins every build-time dependency (`hatchling` and
its transitive deps — `packaging`, `pathspec`, `pluggy`, `tomlkit`,
`trove-classifiers`, `editables`) to an exact version *and* a sha256 hash.
Passing it as a
[`--build-constraint`](https://pip.pypa.io/en/stable/cli/pip_install/#cmdoption-build-constraint)
makes pip verify every installed build-tool file against that hash before
using it, rather than trusting whatever the index currently serves:

```bash
pip wheel --build-constraint requirements-build.txt --no-deps .
```

Unlike ssh-mitm's equivalent setup, `[build-system].requires` here is
**exactly pinned** (`hatchling==1.32.0`) rather than left loose — belt and
braces alongside the constraint file rather than relying on the constraint
file as the sole source of truth. Neither is strictly required for this
project's simpler build-dependency graph, but an exact pin is what makes
`--require-hashes` mode (used more strictly elsewhere, e.g. a CI install
step) actually enforceable rather than merely advisory: without it, pip
accepts an unpinned requirement as long as *some* installable version
exists, hashed or not.

All of `hatchling`'s build-time dependencies are pure-Python packages with
a single universal (`py3-none-any`) wheel, so `pip-compile
--generate-hashes` already resolves exactly one hash per package on its
own — no wheelhouse detour needed (unlike packages with several
platform-specific wheels, e.g. C extensions, where an unrestricted
`--generate-hashes` would pin every platform variant at once).

Regenerate the file after changing `[build-system].requires` or to pick up
newer build-tool releases:

```bash
packaging/update-requirements.sh            # keep existing pins stable
packaging/update-requirements.sh --upgrade  # move pins forward
```

## Proving bit-identical builds

Hash-pinning proves every installed file is the one you expect — it
doesn't by itself prove the build *process* is deterministic. Run:

```bash
packaging/verify-reproducible-build.sh
```

This builds the wheel twice, independently, and compares `sha256sum`. It
should print:

```
OK: bit-identical wheel across two independent builds (<hash>)
```

Not part of the regular `hatch run lint:check` loop — building the wheel
twice is too slow for the everyday dev loop. Run it as a pre-release
check instead.

## Known limits / not covered here

- CI does not yet run `verify-reproducible-build.sh` as part of a release
  workflow.
- The runtime `appimage` package itself has no required third-party
  dependencies (only optional `docs` extras), so there is no separate
  runtime-dependency hash-pinning story here the way ssh-mitm's
  `requirements.txt` has — this page is build-dependency pinning only.
