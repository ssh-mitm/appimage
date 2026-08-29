# Reproducible Builds

Two independent builds of the same project — run at different times, with
nothing pinned — produce a **byte-for-byte identical** `.AppImage` file:

```bash
$ sha256sum dist/myapp-x86_64.AppImage
db8b648c9ddcc50773b740219d3ecb4910b6bf3b18907b566f2eb1b624a79e35  dist/myapp-x86_64.AppImage
$ rm -rf build dist && python -m appimage.build
$ sha256sum dist/myapp-x86_64.AppImage
db8b648c9ddcc50773b740219d3ecb4910b6bf3b18907b566f2eb1b624a79e35  dist/myapp-x86_64.AppImage
```

No configuration required — this is the default behavior. As far as we're
aware, no other Python-to-AppImage packaging tool makes this claim, let
alone verifies it.

## Getting to full reproducibility

The guarantee above — same input, same bytes, on one machine, right now —
needs no configuration. Two further layers are opt-in on top of it, each
closing a different gap, each independent of the other:

1. **Pin the toolchain and hash-pin every dependency** — which Python,
   which appimagetool, which runtime stub, and every third-party package
   pulled in at build time, including the project's own build backend:
   ```bash
   python -m appimage.build --init --lock
   ```
   `--init` writes `python_date`, `appimagetool_sha256`, and
   `runtime_sha256` to `pyproject.toml`; `--lock` then generates both
   `pylock.toml` (runtime dependencies) and a build-backend lock file
   against that now-pinned interpreter, in the same run — `--init` always
   resolves first internally, regardless of argument order, so `--lock`
   never pins hashes against a stale/unpinned interpreter. Each of the two
   can also be run on its own, e.g. to re-pin just one later. Details:
   [Pinning for cross-machine
   reproducibility](#pinning-for-cross-machine-reproducibility),
   [Verified dependencies](#verified-dependencies).

2. **Turn on the umbrella flag**, once step 1 is in place and a build has
   actually succeeded with it:
   ```toml
   [tool.appimage.build]
   reproducible = true
   ```
   Deliberately a separate, manual step, never set automatically by
   `--init --lock` — flipping it turns a missing pin into a hard build
   failure instead of a warning, which is a policy decision only you
   should make, not a side effect of resolving some values. Refuses to
   build unless the step 1 toolchain pins are already set — see [Pinning
   for cross-machine
   reproducibility](#pinning-for-cross-machine-reproducibility).

`python -m appimage.build --check` reports where a project stands at any
point, without building anything:

```
Reproducibility checklist (1/3 ready):
  ✓ Reproducibility: 3/3 pins set
  ✗ Dependency verification: pylock not set — run --lock to generate pylock.toml
  ✗ Build backend verification: build_pylock not set — run --lock to generate it alongside pylock.toml
```

Neither layer is required for the byte-identical guarantee itself — they
exist for projects that also need cross-machine/over-time reproducibility
and supply-chain verification, and each can be adopted alone.

## Why this is hard

An AppImage is an ELF runtime with a SquashFS image appended. Getting a
byte-identical result out of that pipeline turned out to need five
independent fixes — any one missing was enough to make two builds differ,
even with everything else already correct:

1. **Bytecode.** `pip install`'s default `.pyc` cache embeds the
   install-time mtime of each source file in the invalidation header.
   Installed at a different wall-clock time, same source → different
   `.pyc` bytes. Fixed by compiling with `--invalidation-mode
   unchecked-hash` instead, which ties validity to a content hash —
   forced (`-f`) to also override any `.pyc` a module already got from
   merely being *imported* earlier in the build (e.g. a build backend),
   which carries the same install-time-mtime problem and would otherwise
   be left untouched. See [internals.md](internals.md#building-manually)
   for where this was actually found.
2. **File timestamps.** `mksquashfs` embeds each file's mtime in the
   packed image. Every file `appimage.build` installs or generates gets
   its mtime normalized to a fixed value (`SOURCE_DATE_EPOCH`,
   [the reproducible-builds.org
   convention](https://reproducible-builds.org/specs/source-date-epoch/))
   immediately before packaging.
3. **The packer itself.** The classic `AppImageKit` `appimagetool` bundles
   a `mksquashfs` with a genuine, documented non-deterministic
   multi-threaded compression bug — two packaging runs of the *identical*
   input directory produce different bytes once the tree passes roughly
   50–100 files (see [AppImageKit
   #929](https://github.com/AppImage/AppImageKit/issues/929)). No amount
   of input normalization fixes this; it's a bug in the tool doing the
   packing. `appimage.build` defaults to its maintained successor,
   [`AppImage/appimagetool`](https://github.com/AppImage/appimagetool),
   which bundles a fixed, modern squashfs-tools.
4. **appimagetool's own side effects.** Packaging touches a few paths of
   its own (e.g. `.DirIcon`) that live outside the AppDir tree
   `appimage.build` controls. `SOURCE_DATE_EPOCH` is passed into
   appimagetool's *own* process environment too, not just applied to the
   AppDir beforehand.
5. **The runtime stub.** Newer appimagetool releases fetch the AppImage
   runtime ELF stub live, over the network, at packaging time — a source
   of both non-determinism and an unverified download. `appimage.build`
   pre-fetches and pins it instead, then hands it to appimagetool via
   `--runtime-file` so no live download happens.

See [internals.md](internals.md) for exactly where each of these fits in
the build sequence.

## Verify it yourself

```bash
python -m appimage.build
mv dist/myapp-x86_64.AppImage /tmp/build-a.AppImage
rm -rf build dist
python -m appimage.build
sha256sum /tmp/build-a.AppImage dist/myapp-x86_64.AppImage
```

Matching hashes prove it for your project on your machine. To prove it
*across* machines or over time, the appimagetool/runtime binaries and the
bundled Python release also need to be pinned — see below.

## Pinning for cross-machine reproducibility

Fixes 1, 2, and 4 above are fully automatic and need no configuration.
Fix 3 (which appimagetool binary gets used), fix 5 (which runtime binary),
and the Python release are all rolling/latest-by-default — reproducible
*within* a build environment, but not guaranteed to still match what
another machine, or the same machine next month, resolves, unless pinned
explicitly:

```toml
[tool.appimage.build]
python_date = "20260211"
appimagetool_sha256 = "3f9a1c..."
runtime_sha256 = "1cc49bc..."
```

Run `python -m appimage.build --init` to resolve whatever's currently
available (downloading appimagetool and the runtime file if needed) and
write both hashes — plus a human-readable `appimagetool_version` label —
into `pyproject.toml` automatically.

Without a pin, appimagetool and the runtime file are still used — whatever
currently resolves — and a warning logs the actual hash so it can be
copied into config later. Set `verify_downloads = true` to make an
unverified resolution a hard error instead of a warning, for release
builds where "give me the exact bits I asked for, or fail" matters more
than convenience.

Run `python -m appimage.build --reproducible` (after `--init` has written
the pins) as a shortcut that enforces all of the above at once: it implies
`verify_downloads` and `require_zsyncmake` (see
[configuration.md](configuration.md)), and refuses to build at all if
`python_date`, `appimagetool_sha256`, or `runtime_sha256` is still unset —
since resolving any of those three fresh on every build is exactly what
defeats cross-machine reproducibility in the first place.

## Verified dependencies

Everything above pins *appimage.build's own* build tooling — appimagetool,
the runtime stub, the interpreter. None of it touches how your project's
third-party dependencies get installed: by default, `pip install
".[extras]"` resolves and downloads whatever the index currently serves,
unverified. A compromised or typosquatted package pulled in that way ends
up inside the AppImage with nothing to catch it.

One dependency is the exception, verified with no configuration at all:
the bundled `appimage` runtime module itself (the one AppRun and the
`--python-*` flags depend on) is always installed pinned to the exact
version of `appimage.build` doing the build, and its install is
hash-verified against the digest PyPI publishes for that release — the
same free-verification pattern used above for appimagetool/the runtime
file/the Python archive, since this one dependency's correct hash is
always independently knowable in advance, unlike arbitrary third-party
packages. Falls back to a warning (or a hard error under
`verify_downloads`) if PyPI can't be reached. Configuring `pylock` (below)
covers this the normal way instead, since `appimage_pin` is included in
`pylock.toml` alongside every other dependency once one exists.

`pylock` closes that gap:

```toml
[tool.appimage.build]
pylock = "pylock.toml"
```

```sh
python -m appimage.build --lock       # generate/refresh pylock.toml
python -m appimage.build --require-pylock   # abort if pylock isn't set
```

### `--lock` is a thin wrapper, not a new mechanism

`--lock` does not implement any hashing or dependency resolution itself.
It runs `pip lock` (built into pip since 25.1) through the bundled
python-build-standalone interpreter rather than your own, once for
runtime dependencies and once for the build backend (below), writing
`pylock.toml` and a second pylock-format file respectively:

```sh
build/AppDir/python/bin/python3 -m pip lock \
    appimage==2.0.1 ".[extras]" <packages...> \
    -o pylock.toml
```

Running it through the bundled interpreter, not your local one, is the
one thing `--lock` adds over typing that command by hand: `pip lock`
resolves wheels for whatever interpreter runs it, so a lock generated with
your local Python could pin a different platform/ABI than what the
AppImage actually bundles. `--lock` also reads `extras`/`packages` from
`[tool.appimage.build]` for you, so that list isn't maintained twice.

`appimage==2.0.1` and any `packages` entries are real PyPI distributions
and stay in the lock with their own hash like any other dependency — only
the local project (`.`/`.[extras]`) has no stable hash to pin between
source edits, so it's installed separately at build time (below) instead.
Excluding *just* the local project sounds like a job for `pip lock
--only-deps`, but that flag excludes *every* given requirement from its
output, not a chosen one ("No user-supplied requirements will be handled,
even if they were dependencies of other user-supplied requirements" per
its own `--help`) — using it here would have silently dropped
`appimage`'s and `packages`' own pins too, leaving only their transitive
dependencies locked. `--lock` instead resolves everything together
without `--only-deps`, then strips just the local project's entry from
the resulting file afterwards, identified structurally by its local
directory source rather than by name.

### What the real build does with it

With `pylock` configured, `_prepare_python` runs two separate `pip
install` calls instead of one:

```sh
pip install --no-compile --no-deps .[extras]                       # local source, trusted, unhashed
pip install --no-compile --no-deps --require-hashes -r pylock.toml # everything else, hash-verified
```

Two calls, not one, because pip's hash-checking mode — triggered the
moment any requirement in a given invocation carries a hash — then demands
*every* requirement in that same invocation carry one; mixing the unhashed
local project into the `--require-hashes` call would fail outright.
`--no-deps` on both keeps each strictly to what it's given: the local
install won't reach past its own listed dependencies, and the lock install
won't silently pull in anything beyond what got hashed.

### Cooldowns

`pip lock` also accepts `--uploaded-prior-to`, passed through via
`--uploaded-prior-to PnD` on `--lock` (e.g. `P7D`): excludes packages
published more recently than that window from the resolution, giving the
community time to catch a compromised release before it gets locked in.
It only makes sense at generation time — the real build installs exactly
what's already pinned in `pylock.toml`, so a cooldown there would have
nothing left to act on.

### Private package indexes (Artifactory, Nexus, devpi, ...)

Neither `--lock` nor a normal build passes any pip-specific flags for index
selection or authentication — no `--index-url`, no custom `env=` for the
subprocess. Every `pip`/`pip lock` call in `appimage.build` inherits the
calling process's environment as-is, so pip's own standard mechanisms
already work with no configuration on appimage's side:

- `PIP_INDEX_URL` / `PIP_EXTRA_INDEX_URL` / `PIP_TRUSTED_HOST` / `PIP_CERT`
  environment variables
- `pip.conf` (or `PIP_CONFIG_FILE`) at whatever location pip normally
  searches
- `.netrc` for per-host credentials

Point these at an internal Artifactory/Nexus/devpi mirror the same way you
would for any other pip invocation, and both `packages`/`extras`
installs and `--lock`'s dependency resolution pick it up automatically.
This is deliberate, not just an accident of not having built anything
else yet: credentials belong in environment/config, not as CLI arguments
to a subprocess — an argument list can leak to other users on the same
machine via process listings in a way an environment variable set only
for that process does not.

There's currently no equivalent of `--build-constraint`-style one-off CLI
passthrough for occasional, non-persistent overrides (e.g. pointing a
single `--lock` run at a different index without touching `pip.conf`) —
only the persistent env/config path above is supported today.

### Relationship to `reproducible`

`pylock`/`require_pylock` are deliberately independent of `reproducible`
— hash-pinned dependencies and byte-identical output are separate
guarantees, and `reproducible` does not imply or require `pylock`. Opt
into both explicitly if you want both.

### Known limits

`pip lock` is documented by pip itself as experimental — its behavior may
change without notice in a future pip release. `pip install -r
pylock.toml --require-hashes` needs pip >= 26.1 in the bundled
interpreter; `--lock` checks for pip >= 25.1 (what `pip lock` itself
needs) before generating, but a build against an existing `pylock.toml`
with an older bundled pip will fail with a plain pip error rather than
this tool's own message.

## Verified build backend

`pylock` hash-pins the packaged project's *third-party* dependencies —
not the project itself. Installing the project's own source always
triggers a PEP 517 isolated build, and by default pip populates that
isolated environment by resolving the project's `[build-system].requires`
(e.g. `setuptools`, `hatchling`, `uv_build`) fresh from the index, on
every single build. Unpinned and unverified — the same class of gap
`pylock` closes for runtime dependencies, just one level down.

`build_pylock` closes it, generated by `--lock` alongside `pylock.toml` —
no separate flag or hand-written file needed:

```sh
python -m appimage.build --lock
```

```toml
[tool.appimage.build]
build_pylock = "pylock.build.toml"
```

`[build-system].requires` changes rarely, so `--lock` re-locks it on
every run alongside `pylock.toml` rather than needing a dedicated flag —
cheap when nothing changed, and it means one command keeps both in sync.
Point `build_pylock` at whatever path fits your project's own
conventions; `pylock.build.toml` above is just `--lock`'s default.

Consumed differently from `pylock.toml`: `pip install --build-constraint`
[doesn't accept the pylock
format](https://pip.pypa.io/en/stable/cli/pip_install/#cmdoption-build-constraint)
PEP 751 defines, so `build_pylock` is converted to a classic
hash-pinned constraints file at install time and passed as
`--build-constraint` when installing the project itself — pip still
builds it in its own fresh, throwaway isolated environment; only what
gets installed *into* that environment is now hash-verified instead of
resolved live. An earlier approach installed the backend directly into
the main interpreter with `--no-build-isolation` instead, reusing it for
the project build — that avoided the format mismatch too, but left the
build backend permanently installed in the shipped AppImage (on top of
python-build-standalone's own bundled `pip`/`setuptools`, kept
deliberately for `--python-interpreter -m pip`/venv support — see
[Runtime](runtime.md)),
and its own import-time bytecode caches carried install-time timestamps
that broke byte-for-byte reproducibility across independent builds.
`--build-constraint` avoids both problems by keeping the build properly
isolated.

`require_build_pylock = true` aborts the build instead of warning when
`build_pylock` isn't set, mirroring `require_pylock`.

### Known limits

`build_pylock` only covers packages resolved while building your
project's wheel — it doesn't change how the project's *own*
`[build-system].requires` version specifier itself is resolved (an
unpinned specifier like `"hatchling"` still floats to whatever `pip lock`
resolves as latest, unless pinned). For most backends, pin
`[build-system].requires` to an exact version in your own
`pyproject.toml` too, so `--lock` has one specific release to hash
rather than a moving target — see the [development
chapter](develop/reproducible-builds.md) for this project's own choice of
backend and how it's pinned (a bounded range rather than an exact pin,
for reasons specific to that backend, and pinned via a separate,
hand-generated `requirements-build.txt` for *this project's own* PyPI
wheel — a different, unrelated mechanism from `build_pylock` above).

## Not covered here

This page is about the AppImages `appimage.build` produces for *your*
project. For the reproducibility of the `appimage` package's own PyPI
wheel, see the [development chapter](develop/reproducible-builds.md).
