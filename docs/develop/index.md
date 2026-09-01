# Development

How to work on `appimage` itself - set up an environment, run the checks,
and build an actual AppImage from source to confirm a change works.

## Environment setup

Everything runs through [Hatch](https://hatch.pypa.io) environments;
nothing needs installing globally beyond `pip install hatch`.

```bash
git clone https://github.com/ssh-mitm/appimage.git
cd appimage
hatch run lint:check   # creates the lint env on first run
```

## Running tests and lint

```bash
hatch run test:run     # pytest tests/
hatch run lint:check   # pytest + black + bandit + ruff + flake8 + pylint + mypy
```

`lint:check` is what CI runs (`hatch run +py=<version> lint:check` across
the Python 3.11–3.14 matrix) - a change isn't done until this passes.

## Building an AppImage from source

The fastest way to confirm a change actually works end to end is building
the minimal example project in `examples/myapp/` with your working copy of
`appimage.ctl`, rather than a published release:

```bash
hatch run appimagectl --project-dir examples/myapp
./examples/myapp/dist/myapp-x86_64.AppImage
```

`hatch run appimagectl` runs the `appimagectl` console script from
inside the `lint`/`test` env, which has your checkout installed - so it
picks up local changes to `appimage/ctl/` immediately, no reinstall step
needed. If you only need to see what would be built without actually
packaging anything, use `check` instead.

For a change that touches build output determinism specifically (bytecode
handling, mtime normalization, appimagetool/runtime resolution, or the
build-path scrubbing that keeps output independent of where the checkout
lives), build twice and diff:

```bash
hatch run appimagectl --project-dir examples/myapp
mv examples/myapp/dist/myapp-x86_64.AppImage /tmp/build-a.AppImage
rm -rf examples/myapp/build examples/myapp/dist
hatch run appimagectl --project-dir examples/myapp
sha256sum /tmp/build-a.AppImage examples/myapp/dist/myapp-x86_64.AppImage
```

Matching hashes confirm the change didn't introduce new non-determinism -
see [Reproducible builds](../reproducible-builds.md) for what this
actually guarantees and why it's non-trivial for AppImages specifically.

## Building the documentation

```bash
hatch run docs:build   # sphinx-build docs build/html
```

## This package's own PyPI wheel

Development environment and AppImage-output reproducibility above cover
working on the *tool*. The tool's own release artifact - the wheel
published to PyPI - has a separate, narrower reproducibility story:

```{toctree}
:maxdepth: 1

reproducible-builds
```
