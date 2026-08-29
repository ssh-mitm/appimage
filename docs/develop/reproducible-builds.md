# Reproducible Wheel Builds

Developer-facing page: this is about **this package's own** PyPI wheel
build hygiene, not about the AppImages `appimage` builds for other
projects — for that, the headline feature, see [Reproducible
builds](../reproducible-builds.md).

The wheel published to [pypi.org](https://pypi.org/project/appimage/) is
built via `uv_build`, the PEP 518 build backend declared in
`[build-system]` in `pyproject.toml` — a natural fit alongside
`appimage.ctl`'s own use of `uv python install`/python-build-standalone
for the interpreter it bundles into every AppImage (see [Reproducible
builds](../reproducible-builds.md)). This page covers how the wheel build
is made hash-verified and provably bit-identical across independent
builds of the same commit — the same pattern used by
[ssh-mitm](https://github.com/ssh-mitm/ssh-mitm/blob/master/doc/develop/reproducible-builds.md)
and [minicms](https://github.com/manfred-kaiser/minicms/blob/main/docs/reproducible-builds.md).

## Hash-pinning the build dependency

`requirements-build.txt` pins the build-time dependency (`uv_build` —
unlike `hatchling`, used by this project before, it has no transitive
dependencies of its own to pin alongside it) to an exact version *and*
every sha256 hash published for it. Passing it as a
[`--build-constraint`](https://pip.pypa.io/en/stable/cli/pip_install/#cmdoption-build-constraint)
makes pip verify the installed build-tool file against one of those
hashes before using it, rather than trusting whatever the index currently
serves:

```bash
pip wheel --build-constraint requirements-build.txt --no-deps .
```

`[build-system].requires` uses the bounded range `uv_build>=0.12.7,<0.13`
— the pattern [uv's own docs
recommend](https://docs.astral.sh/uv/concepts/build-backend/) for pinning
it, since `uv_build` treats a minor version as its compatibility unit.
This differs from ssh-mitm's/this project's own former `hatchling==X.Y.Z`
exact pin, but the actual enforcement doesn't come from that range at all
— it comes from `requirements-build.txt`/`--build-constraint`, which pins
to one exact, hash-verified version regardless of how wide the range is.

`uv_build` ships as a single compiled binary — one wheel per platform, no
`packaging`/`pathspec`/`pluggy`/... chain the way `hatchling` had. It
*is* platform-specific, though: `pip-compile --generate-hashes` without a
wheelhouse restriction resolves every published wheel variant (linux,
macOS, Windows, several architectures) under the one version pin, so
whichever platform actually runs the install still finds its hash in the
list — no per-platform lock files needed, unlike tools with
Python-version-specific wheels (e.g. compiled extensions tied to a
`cp311`/`cp312`/... ABI tag), which would need one lock per interpreter
instead of one pin covering every platform at once.

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

- **Never build this project with the `uv` CLI itself (`uv build`)
  without `--force-pep517`.** `uv build` has an internal fast path for
  `uv_build` projects that skips PEP 517 entirely and calls straight into
  whatever `uv_build` code is bundled in the `uv` binary doing the build —
  not the version pinned in `[build-system].requires`, not downloaded
  from PyPI, and not checked against `requirements-build.txt` at all.
  `--build-constraint`/`--require-hashes` silently become no-ops on that
  path (see [astral-sh/uv#20860](https://github.com/astral-sh/uv/issues/20860)).
  This project's own CI never uses `uv build` — every build here goes
  through `pip wheel`/`python -m build`, which always follow PEP 517 and
  have no such fast path — but anyone building locally with `uv` instead
  needs `uv build --force-pep517` to get the same guarantees.
- The runtime `appimage` package itself has no required third-party
  dependencies (only optional `docs` extras), so there is no separate
  runtime-dependency hash-pinning story here the way ssh-mitm's
  `requirements.txt` has — this page is build-dependency pinning only.
