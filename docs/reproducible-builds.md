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

## Why this is hard

An AppImage is an ELF runtime with a SquashFS image appended. Getting a
byte-identical result out of that pipeline turned out to need five
independent fixes — any one missing was enough to make two builds differ,
even with everything else already correct:

1. **Bytecode.** `pip install`'s default `.pyc` cache embeds the
   install-time mtime of each source file in the invalidation header.
   Installed at a different wall-clock time, same source → different
   `.pyc` bytes. Fixed by compiling with `--invalidation-mode
   unchecked-hash` instead, which ties validity to a content hash.
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
It runs exactly one command — `pip lock` (built into pip since 25.1) —
through the bundled python-build-standalone interpreter rather than your
own:

```sh
build/AppDir/python/bin/python3 -m pip lock \
    appimage==2.0.1 ".[extras]" <packages...> \
    --only-deps -o pylock.toml
```

Running it through the bundled interpreter, not your local one, is the
one thing `--lock` adds over typing that command by hand: `pip lock`
resolves wheels for whatever interpreter runs it, so a lock generated with
your local Python could pin a different platform/ABI than what the
AppImage actually bundles. `--lock` also reads `extras`/`packages` from
`[tool.appimage.build]` for you, so that list isn't maintained twice.
Nothing about it is otherwise different from running the command above
yourself.

`--only-deps` excludes the local project (`.`/`.[extras]`) from the lock —
it has no stable hash to pin between source edits, so it stays out and is
installed separately at build time (below). `appimage==2.0.1` and any
`packages` entries *are* real PyPI distributions, though, and stay in the
lock with their own hash like any other dependency.

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

## Not covered here

This page is about the AppImages `appimage.build` produces for *your*
project. For the reproducibility of the `appimage` package's own PyPI
wheel, see the [development chapter](develop/reproducible-builds.md).
