# For LLMs and coding agents

This documentation is written for two readers on purpose: a human
skimming for what to run, and an agent that needs the deeper "why"
before touching the code. Splitting them keeps the rest of the docs
short instead of cramming both into every page. This page is the second
half - a dense reference, not a tutorial. Human readers can stop here;
nothing on this page is needed to use `appimage`.

## Module map

```
appimage/
├── __init__.py          __version__
├── appstarter.py         runtime module: entry-point dispatch, --python-* flags, venv activation
└── ctl/                  the appimagectl build tool
    ├── __main__.py        argparse CLI, subcommand dispatch, top-level exception handling
    ├── _base.py           BuildConfig (raw pyproject.toml fields), _resolve() → _ResolvedBuild,
    │                      all config-level warnings/errors (reproducible-requires-pins, etc.)
    ├── _download.py       _download, _verify_sha256, _require_or_warn_unverified,
    │                      _resolution_source (the shared "config → cache → download" classifier)
    ├── _appimagetool.py   resolve/verify appimagetool + the runtime stub; classic-build detection
    ├── _python.py         resolve/verify the python-build-standalone tarball; python_dir bypass
    ├── _toml.py           small pyproject.toml read/write helpers
    ├── build_appdir.py    AppDir assembly: install, hooks, bytecode, build-path scrubbing
    ├── build.py           packaging: resolves appimagetool/runtime, invokes it, zsync check
    ├── check.py           `check` subcommand + the shared _format_check() used by check/build/
    │                      build-appdir/init/update-tools alike
    ├── init.py             `init` subcommand: writes auto-detected + resolved pins
    ├── lock.py             `lock` subcommand: pylock.toml + build_pylock generation
    ├── enable_reproducible.py  `enable-reproducible`: init + lock + verify build + flip the flag
    ├── update_tools.py     `update-tools` subcommand
    └── templates/          AppRun.sh, desktop.template
```

`appimage.ctl.__init__` only re-exports the public API; `__main__.py`'s
imports come from there, not from the individual modules directly.

## Invariants - do not reintroduce these

Every one of these was a real, shipped bug, found and fixed by actually
running the tool end to end (not just via mocked unit tests). If you're
touching adjacent code, check you haven't reopened one of these:

- **appimagetool/runtime-file resolution never searches `PATH`.** Only
  explicit config path → build cache → download, for both. This was
  changed deliberately (see [Classic appimagetool
  detected](reproducible-builds.md#classic-appimagetool-detected)) - a
  stray classic AppImageKit build on `PATH` was silently picked up on a
  real machine before this. Don't add a `shutil.which(...)` fallback back
  in for either.
- **A resolved appimagetool binary is checked against
  `_looks_like_classic_appimagekit()` before use**, unless it was *just*
  downloaded this run (by definition from the right source). Two
  independent signals: build-path debug strings
  (`_APPIMAGEKIT_BUILD_PATH_MARKERS`) and the `--version` banner's
  wording (`"(commit "` vs `"(git version "`). Neither is airtight alone;
  that's why a match aborts the build rather than being logged and
  ignored - `appimagetool_sha256` alone can't catch this, since it only
  proves the same (possibly wrong) file is used every time.
- **Console-script shim relocation must handle *both* the quoted and
  unquoted executable form.** `pip._vendor.distlib.scripts.
  enquote_executable` wraps the embedded interpreter path in `"..."`
  whenever it contains a space, and a space also forces the two-line
  `#!/bin/sh` fallback regardless of length. `_relocate_console_script`
  tries the executable both bare and `"quoted"`, for both the one-line
  and two-line shebang shapes. A self-locating replacement (embedding
  `$(dirname ...)`) must *always* be written in the two-line
  `#!/bin/sh` + `exec` form - the kernel's `#!` handling never
  shell-expands a one-line shebang, so a literal one-line
  `#!"$(dirname ...)/python3"` fails at exec time with "bad interpreter",
  even unmoved.
- **`--runtime-file`'s value is staged to a plain-ASCII temp path before
  invoking appimagetool** (`_stage_runtime_file_for_appimagetool` in
  `build.py`). The pinned `AppImage/appimagetool` build's own glib-based
  CLI option parser fails to decode a non-ASCII byte in that flag's
  value specifically - confirmed by isolating the failure outside
  appimagectl entirely, independent of locale (`de_DE.UTF-8`, `C.UTF-8`
  both reproduce it) and of `G_FILENAME_ENCODING`/`G_BROKEN_FILENAMES`.
  Positional arguments (the AppDir path, the output filename) are
  unaffected - only this one flag. A project path containing any
  non-ASCII character (an ordinary thing - e.g. a home directory under a
  non-English username) would otherwise fail packaging 100% of the time.
- **Every subprocess that runs the bundled interpreter for an install
  gets `PYTHONNOUSERSITE=1`** (`_isolated_subprocess_env` in
  `build_appdir.py`), including `post_install`/`pre_package` hooks and
  `pip lock`. Without it, pip additionally resolves against the *build
  user's* own `~/.local/lib/pythonX.Y/site-packages` (PEP 370) - if a
  requirement happens to already be satisfied there, pip silently skips
  installing it into the AppDir at all ("Requirement already satisfied"
  instead of "Collecting"), and the built AppImage fails at runtime with
  `ModuleNotFoundError` on any *other* host, while the build itself
  reports success.
- **The `pylock.toml` dependency install does not pass `--no-deps`.**
  pip's hash-checking mode, once triggered by any `--hash` in the
  requirement set, demands every requirement in that invocation carry
  one - so leaving normal dependency resolution on means a stale or
  incomplete lock aborts the build loudly (no hash-matching candidate for
  a missing transitive dependency) instead of silently shipping an AppDir
  that's missing it. The *local project* install (`pip install --no-deps
  .[extras]`) keeps `--no-deps` - that one's deliberately excluded from
  the lock and must not reach past its own listed dependencies.
- **`pylock` generation does not use `pip lock --only-deps`.** That flag
  excludes *every* given requirement from its own output, not a chosen
  one ("No user-supplied requirements will be handled, even if they were
  dependencies of other user-supplied requirements" - pip's own
  `--help`). Using it would silently drop the `appimage` runtime
  module's own pin (and any `packages` entries) from `pylock.toml`,
  leaving only their transitive dependencies locked - a real, shipped bug
  ("No module named appimage" at AppImage startup). `lock` instead
  resolves everything together and strips just the local project's entry
  afterwards, identified structurally by its local directory source
  rather than by name.
- **`build_pylock` is applied via `pip install --build-constraint`, not
  `--no-build-isolation`.** The `--no-build-isolation` approach installed
  the build backend into the main interpreter and reused it - left the
  backend permanently installed in the shipped AppImage, and its
  import-time bytecode caches carried install-time timestamps that broke
  byte-for-byte reproducibility across independent builds.
  `--build-constraint` keeps pip's own isolated, throwaway build
  environment, just hash-verifies what goes into it.
- **`_scrub_build_paths` reads each package's own `RECORD` file** to find
  what to scrub, rather than guessing at pip's console-script format -
  that format has more than one variant (a plain interpreter, or
  distlib's `#!/bin/sh` polyglot fallback) and guessing would silently
  stop working on a pip version that changes it. `direct_url.json` is
  always deleted (meaningless once the AppDir runs somewhere else);
  console-script shims are relocated in place instead of deleted, with a
  narrow, exact-byte-match rewrite - anything that doesn't match a known
  distlib shim format falls back to deletion rather than risking a
  script that's broken in a new way (the lesson from virtualenv's old
  `--relocatable`, eventually removed upstream for exactly this
  unreliability).
- **`_resolution_source(explicit, cache_path)` is the only place that
  decides "config path, then build cache, then download."** Every
  resolver (`_locate_appimagetool`, `_resolve_runtime_file`,
  `_resolve_python_tarball`) calls it, and so does `check`'s
  `_predict_unverified_downloads` - which is how `check`/`build`/
  `build-appdir` can predict a `verify_downloads` failure without
  downloading or hashing anything, without a second, driftable copy of
  the precedence logic. Don't reintroduce a local `if explicit: ... elif
  cache.exists(): ...` anywhere this matters.
- **`build_appdir()` never resolves appimagetool or the runtime
  file - by design.** AppDir assembly must work completely independently
  of appimagetool; only the full `build()` (default subcommand) needs
  both. `check`'s `appdir_errors`/`package_errors` split, and
  `_predict_unverified_downloads`'s python-archive-vs-appimagetool/
  runtime-file bucketing, both preserve this split deliberately - don't
  merge them.
- **`__main__.py`'s exception handling wraps the whole dispatch,
  including `BuildConfig.from_pyproject()`**, in one
  `try/except (FileNotFoundError, RuntimeError, OSError, ValueError,
  subprocess.CalledProcessError)`. A previously separate, narrower
  `except FileNotFoundError` just around config loading meant a
  malformed `pyproject.toml` (`tomllib.TOMLDecodeError`, a `ValueError`
  subclass) produced a raw traceback instead of a clean `Error: ...`
  line - keep it as one block.

## Testing changes end to end, not just via mocked unit tests

Several of the bugs above only surfaced from a real build (network
downloads, real `pip install`, real `appimagetool` subprocess) - the
mocked unit test suite passed the whole time. When changing anything in
`_appimagetool.py`, `build_appdir.py`, or `build.py`, prefer verifying
against a real build of `examples/myapp/` over trusting mocks alone; see
[Development](develop/index.md) for the exact commands
(`hatch run appimagectl --project-dir examples/myapp`, then the
build-twice-and-diff pattern for anything touching output determinism).
Directory-shape variations are a specific, previously-bug-prone axis worth
checking deliberately: a build path containing a space or a non-ASCII
character, a very short or very long/deeply nested path, a renamed
`build_dir`/`dist_dir`, a build invoked through a symlink - all of these
are expected to produce byte-identical output to a plain path; two of
them (space, non-ASCII) didn't, until fixed.

## Where the narrative detail lives

This page intentionally doesn't repeat the full explanations - each
invariant above links to (or is a compressed version of) fuller prose
elsewhere:

- [Reproducible builds](reproducible-builds.md) - the six independent
  fixes behind byte-identical output, the classic-appimagetool check,
  the zsync/`PATH` story, dependency and build-backend hash-pinning.
- [How AppImages are built](internals.md) - the manual, step-by-step
  equivalent of what `appimage.ctl` automates.
- [CLI Reference](cli.md) / [Configuration](configuration.md) - every
  flag and config key, kept intentionally terse; this page is where the
  "why" that used to live in those tables moved to.
- [Development](develop/index.md) - environment setup, running the test
  suite and lint, building an AppImage from a working copy.
