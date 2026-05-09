# appimage

**The `appimage` module packages Python applications as self-contained AppImages.**

It bundles a complete Python distribution from [python-build-standalone](https://github.com/astral-sh/python-build-standalone), manages entry points, and supports virtual environments that extend the bundled packages with additional ones.

> The bundled interpreter comes from [python-build-standalone](https://github.com/astral-sh/python-build-standalone) — the exact same source as `uv python install`. What you develop with locally is what gets shipped in the AppImage.

```{toctree}
:maxdepth: 2
:caption: Documentation

quickstart
configuration
cli
runtime
changelog
```

## Installation

```sh
pip install appimage
```
