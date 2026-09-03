# Reproducible Builds

Two independent builds of the same project - run at different times, with
nothing pinned - produce a **byte-for-byte identical** `.AppImage` file:

```bash
$ sha256sum dist/myapp-x86_64.AppImage
db8b648c9ddcc50773b740219d3ecb4910b6bf3b18907b566f2eb1b624a79e35  dist/myapp-x86_64.AppImage
$ rm -rf build dist && python -m appimage.ctl build
$ sha256sum dist/myapp-x86_64.AppImage
db8b648c9ddcc50773b740219d3ecb4910b6bf3b18907b566f2eb1b624a79e35  dist/myapp-x86_64.AppImage
```

No configuration required - this is the default behavior. As far as we're
aware, no other Python-to-AppImage packaging tool makes this claim, let
alone verifies it.

## Getting to full reproducibility

The guarantee above - same input, same bytes, on one machine, right now -
needs no configuration. Two further layers are opt-in on top of it, each
closing a different gap, each independent of the other: pinning the
toolchain and hash-pinning every dependency, and turning on the umbrella
flag that makes a missing pin a hard build failure instead of a warning.

### The recipe

Four steps, the same ones every time, regardless of which form below you use:

1. Pin the toolchain - `python_date`, `appimagetool_sha256`, `runtime_sha256`.
2. Hash-pin every dependency - `pylock.toml`, plus a lock file for the build backend.
3. Build once, for real, with those pins enforced - proving they actually work together, not just that they resolve.
4. Only once that build succeeds, turn `reproducible = true` on.

Automated, as one command:

```bash
python -m appimage.ctl enable-reproducible
```

`enable-reproducible` runs exactly those four steps - step 1 is `init`,
step 2 is `lock`, step 3 is a real build with `reproducible` enforced,
and step 4 only happens if step 3 didn't raise. It's not a separate
mechanism from what's below, just the same recipe with nothing to run by
hand.

Or run each step yourself, for more control - to inspect what changed
between steps, adopt only some of them, or re-pin just one later without
repeating the rest:

```bash
python -m appimage.ctl init
python -m appimage.ctl lock
python -m appimage.ctl build --reproducible
```

Each command re-reads `pyproject.toml` from disk when it starts, so
`lock` picks up whatever `init` just wrote even run as a separate
invocation afterwards, and `build --reproducible` picks up both. Once
that build succeeds, set `reproducible = true` in `pyproject.toml`
yourself - `enable-reproducible` does the identical thing automatically,
see [The umbrella flag](#the-umbrella-flag) for why it waits for a
successful build first rather than writing the flag right away. Details:
[Pinning for cross-machine
reproducibility](#pinning-for-cross-machine-reproducibility),
[Verified dependencies](#verified-dependencies).

### The umbrella flag

```toml
[tool.appimage]
reproducible = true
```

Written automatically by `enable-reproducible` once a build has actually
succeeded with the pins from `init`/`lock` - or set it by hand after
verifying a build yourself, if you went the piecewise route. `init`/`lock`
never flip it themselves: it turns a missing pin into a hard build
failure, and that should follow a build that's proven to work, not just
the act of writing the pins. Refuses to build unless the toolchain pins
are already set - see [Pinning for cross-machine
reproducibility](#pinning-for-cross-machine-reproducibility).

`python -m appimage.ctl check` reports where a project stands at any
point, without building anything:

```
Reproducibility checklist (3/5 ready):
  ✓ AppDir reproducibility: python_date set
  ✓ Runtime module reproducibility: appimage_version, appimage_sha256 set
  ✓ Packaging reproducibility: appimagetool_sha256, runtime_sha256 set
  ✗ Dependency verification: pylock not set - run 'lock' to generate pylock.toml
  ✗ Build backend verification: build_pylock not set - run 'lock' to generate it alongside pylock.toml
```

Neither layer is required for the byte-identical guarantee itself - they
exist for projects that also need cross-machine/over-time reproducibility
and supply-chain verification, and each can be adopted alone.

## Why this is hard

An AppImage is an ELF runtime with a SquashFS image appended. Getting a
byte-identical result out of that pipeline turned out to need seven
independent fixes - any one missing was enough to make two builds differ,
even with everything else already correct:

1. **Bytecode.** `pip install`'s default `.pyc` cache embeds the
   install-time mtime of each source file. Installed at a different
   wall-clock time, same source → different `.pyc` bytes. Fixed by
   compiling with `--invalidation-mode unchecked-hash` instead
   (hash-based validity), forced (`-f`) to also catch any `.pyc` a module
   already picked up from merely being *imported* earlier in the build.
   See [internals.md](internals.md#building-manually) for where this was
   actually found.
2. **File timestamps.** `mksquashfs` embeds each file's mtime in the
   packed image. Every file `appimage.ctl` installs or generates gets
   its mtime normalized to a fixed value (`SOURCE_DATE_EPOCH`,
   [the reproducible-builds.org
   convention](https://reproducible-builds.org/specs/source-date-epoch/))
   immediately before packaging.
3. **The packer itself.** The classic `AppImageKit` `appimagetool` bundles
   a `mksquashfs` with a genuine, documented non-deterministic
   multi-threaded compression bug - two packaging runs of the *identical*
   input directory produce different bytes once the tree passes roughly
   50–100 files (see [AppImageKit
   #929](https://github.com/AppImage/AppImageKit/issues/929)). No amount
   of input normalization fixes this; it's a bug in the tool doing the
   packing. `appimage.ctl` defaults to its maintained successor,
   [`AppImage/appimagetool`](https://github.com/AppImage/appimagetool),
   which bundles a fixed, modern squashfs-tools. If you ever build
   `appimagetool` yourself instead of using the pinned download, use
   squashfs-tools >= 4.5.1 - the version
   [reproducible-builds.org](https://reproducible-builds.org/docs/system-images/)
   itself calls out as the first to honor `SOURCE_DATE_EPOCH` and not
   reorder fragments based on multithreading.
4. **appimagetool's own side effects.** Packaging touches a few paths of
   its own (e.g. `.DirIcon`) that live outside the AppDir tree
   `appimage.ctl` controls. `SOURCE_DATE_EPOCH` is passed into
   appimagetool's *own* process environment too, not just applied to the
   AppDir beforehand.
5. **The runtime stub.** Newer appimagetool releases fetch the AppImage
   runtime ELF stub live, over the network, at packaging time - a source
   of both non-determinism and an unverified download. `appimage.ctl`
   pre-fetches and pins it instead, then hands it to appimagetool via
   `--runtime-file` so no live download happens.
6. **The build machine's own absolute path.** Compiled bytecode, stray
   stdlib `.pyc` files, and pip's own install-time bookkeeping
   (`direct_url.json`, console-script shims) all bake the build
   directory's absolute path - and via `$HOME`, typically the building
   user's own name - into files that ship inside the AppImage. That's
   enough to make two builds of identical source differ if run from
   different checkout locations, e.g. two developers' home directories or
   two CI providers. Fixed by compiling with `-s <site-packages>` to
   strip the path from every code object, `PYTHONDONTWRITEBYTECODE=1` on
   every subprocess so nothing compiles outside that controlled step, and
   scrubbing pip's two artifacts: `direct_url.json` is deleted (it only
   records *where this came from*, meaningless once the AppDir runs
   somewhere else), while console-script shims are relocated in place -
   shebang rewritten to find the bundled interpreter relative to their
   own location, so `AppDir/python/bin/<entry-point>` keeps working for
   anyone using the AppDir directly (see [internals.md](internals.md) for
   the mechanism). A final sweep of the whole AppDir for the build path
   turns "did this actually work" into something the build verifies on
   every run rather than something checked by hand.
7. **Rolling releases.** Both `AppImage/appimagetool` and `AppImage/type2-runtime` also
   publish a `continuous` release - the asset is overwritten in place on every upstream
   rebuild, same filename, same URL. A sha256 pinned against today's `continuous` asset
   can become permanently unfetchable the moment upstream cuts the next one, since
   GitHub doesn't keep the bytes it overwrote - the pin still looks valid in
   `pyproject.toml`, but a fresh resolution (a cleared cache, a new CI runner, a new
   contributor's machine) has nothing left to verify it against. `appimage.ctl` resolves
   the newest *genuine, versioned* release instead - never `continuous` - which GitHub
   never reuses for a later build: appimagetool publishes semver tags (`1.9.1`),
   type2-runtime dated ones (`20251108`).

See [internals.md](internals.md) for exactly where each of these fits in
the build sequence, and [For LLMs and coding agents](llms.md) for the
full mechanism-level detail behind each fix.

## Classic appimagetool detected

appimagetool is resolved from an explicit `appimagetool` config path,
then the build cache, then a fresh download - `PATH` is never searched
(see ["The packer itself" above](#why-this-is-hard), item 3).

**This is the exception, not the rule.** A project that never sets
`appimagetool` always takes the download branch: a verified fetch of the
current [`AppImage/appimagetool`](https://github.com/AppImage/appimagetool)
default, cached for next time. That cached copy stays safe on every later
build too - nothing but this project's own download step ever writes to
it. The check below exists for the two remaining ways a *different*
binary can still end up in use:

- An `appimagetool` path set explicitly in `[tool.appimage]`, typically
  for an offline/air-gapped build (see below) - worth double-checking
  it's actually an `AppImage/appimagetool` build, not a copy of the
  classic one grabbed years ago.
- A build cache seeded by hand rather than by `appimage.ctl`'s own
  download step.

Pinning `appimagetool_sha256` doesn't catch either case: it only proves
the same file is used every time, not that it's the *right* file - the
classic build's non-deterministic `mksquashfs` (see item 3) still
produces a different `.AppImage` on every run, just with a build that
"successfully" verifies its own hash each time. So the build aborts
instead of just warning when the resolved binary looks like the classic
build - based on debug-info strings from its own source tree still
present in the binary, or, if those were stripped, its `--version`
banner's wording. Neither signal is airtight alone, which is why a match
aborts rather than being silently trusted.

**Fix - network available (the common case):** stop pointing at the
classic build and let `appimage.ctl` fetch the right one itself.

- If `appimagetool` is set explicitly in `[tool.appimage]`, either unset
  it (falls through to cache/download) or point it at a real
  `AppImage/appimagetool` build (see below).
- If it's in the build cache (`<build_dir>/appimagetool-<arch>.AppImage`)
  from being seeded by hand, delete that file so the next build downloads
  a fresh, correct one.

Then rerun `init` (or just the build) to re-resolve and re-pin
`appimagetool_sha256` against the correct binary.

**Fix - offline/air-gapped:** download the right asset yourself from
[the `AppImage/appimagetool` releases page](https://github.com/AppImage/appimagetool/releases)
- the newest *versioned* release's `appimagetool-<arch>.AppImage` (`<arch>` is
`x86_64`, `aarch64`, or `armhf`), **not** the `continuous` release: `continuous` is
overwritten in place on every upstream rebuild, so a sha256 pinned against it today can
become permanently unfetchable later - see
[Why this is hard](#why-this-is-hard) above. The GitHub API's own published sha256 for
the asset you download is what a networked build would auto-verify against. Then
either:

- place it at `<build_dir>/appimagetool-<arch>.AppImage` so it resolves as
  a cache hit, or
- set `appimagetool = "path/to/appimagetool"` in `[tool.appimage]`
  explicitly.

Either way, also set `appimagetool_sha256` to that asset's published
digest so it's verified rather than merely trusted - `check` prints a
warning naming the actual hash it resolved if this is left unset.

## Zsync and the build host PATH

`update_info`/`require_zsyncmake` used to work like this: before packaging,
check whether `zsyncmake` is on the *build host's* `PATH`; warn (or, under
`require_zsyncmake`, abort) if it isn't. That check was wrong, in the same
way - and for the same underlying reason - as [the classic appimagetool
detection above](#classic-appimagetool-detected): it asked about the
*host's* `PATH`, when what actually determines whether a `.zsync` file
gets produced is entirely internal to appimagetool itself.

appimagetool's own `AppRun` does this:

```sh
this_dir="$(dirname -- "$(readlink -f -- "$0")")"
export PATH="$this_dir"/usr/bin:"$PATH"
exec "$this_dir"/usr/bin/appimagetool "$@"
```

- it bundles its own `zsyncmake` right next to `mksquashfs` in its own
`usr/bin`, and puts that directory *first* on `PATH` before running the
real binary. appimagetool's own C source then does a plain PATH lookup
(`g_find_program_in_path("zsyncmake")`) to decide whether to generate a
`.zsync` file - which, because of the `AppRun` above, finds appimagetool's
*own* bundled copy first, regardless of whether the build host has
`zsyncmake` installed separately or not. Confirmed by hand: extracting and
running the bundled `usr/bin/zsyncmake` directly, on a host with no system
`zsyncmake` at all, produces a valid `.zsync` file with no network access
and no dependency on anything outside the appimagetool download itself.

So `.zsync` generation was already self-contained and host-independent -
the old PATH check just asked the wrong question and produced a
false warning (or, under `require_zsyncmake`/`--reproducible`, a false
*abort*) on any build host that didn't happen to have a *separate* system
`zsyncmake` installed, even though the actual build would have succeeded
and produced a correct `.zsync` file regardless. Fixed by checking the
real, deterministic outcome instead: after packaging, whether
`<dist_dir>/<app>-<arch>.AppImage.zsync` actually exists. The only way
this still fails is a genuinely unusual `appimagetool` (a hand-built copy
without a bundled `zsyncmake`, explicitly configured via `appimagetool` in
`[tool.appimage]`) - not the build host's own installed packages.

## Verify it yourself

```bash
python -m appimage.ctl build
mv dist/myapp-x86_64.AppImage /tmp/build-a.AppImage
rm -rf build dist
python -m appimage.ctl build
sha256sum /tmp/build-a.AppImage dist/myapp-x86_64.AppImage
```

Matching hashes prove it for your project on your machine. To prove it
*across* machines or over time, the appimagetool/runtime binaries and the
bundled Python release also need to be pinned - see below.

This isn't just a claim in this document - the exact same check runs on
every push and pull request, via
[`packaging/verify-appimage-reproducible-build.sh`](https://github.com/ssh-mitm/appimage/blob/main/packaging/verify-appimage-reproducible-build.sh):
it builds [`examples/myapp`](https://github.com/ssh-mitm/appimage/tree/main/examples/myapp)
twice, from scratch, and fails the build if the two `.AppImage` files
aren't byte-identical. See the `reproducible-appimage-build` job in
[`.github/workflows/python-package.yml`](https://github.com/ssh-mitm/appimage/blob/main/.github/workflows/python-package.yml).

## Pinning for cross-machine reproducibility

Fixes 1, 2, and 4 above are fully automatic and need no configuration.
Fix 3 (which appimagetool binary gets used), fix 5 (which runtime binary),
and the Python release are all rolling/latest-by-default - reproducible
*within* a build environment, but not guaranteed to still match what
another machine, or the same machine next month, resolves, unless pinned
explicitly:

```toml
[tool.appimage]
python_date = "20260211"
appimagetool_sha256 = "3f9a1c..."
runtime_sha256 = "1cc49bc..."
```

Run `python -m appimage.ctl init` to resolve whatever's currently
available (downloading appimagetool and the runtime file if needed) and
write both hashes - plus a human-readable `appimagetool_version` label -
into `pyproject.toml` automatically.

Without a pin, appimagetool and the runtime file are still used - whatever
currently resolves - and a warning logs the actual hash so it can be
copied into config later. Set `verify_downloads = true` to make an
unverified resolution a hard error instead of a warning, for release
builds where "give me the exact bits I asked for, or fail" matters more
than convenience.

Run `python -m appimage.ctl build --reproducible` (after `init` has written
the pins) as a shortcut that enforces all of the above at once: it implies
`verify_downloads` and `require_zsyncmake` (see
[configuration.md](configuration.md)), and refuses to build at all if
`python_date`, `appimagetool_sha256`, or `runtime_sha256` is still unset -
since resolving any of those three fresh on every build is exactly what
defeats cross-machine reproducibility in the first place.

### Pin `appimage` itself, not just what it downloads

Everything above pins what `appimage.ctl` *downloads* - it says nothing
about `appimage.ctl` *itself*. If your build environment installs the
`appimage` package with an unpinned or range requirement (`pip install
appimage`, `appimage>=3.0.0`, a bare `appimage` dependency in a Hatch/tox
environment, etc.), a newer release can get resolved silently - on CI,
on a teammate's machine, or on the same machine after a cache is cleared
- without ever touching `pyproject.toml`. Since packaging behavior itself
lives in this package (e.g. which `mksquashfs` flags get passed), two
builds that pin every artifact `appimage.ctl` touches can still diverge
if they're not running the *same* `appimage.ctl`.

This is exactly how a real cross-machine reproducibility investigation
went off track: local and CI produced different bytes, and every
downloaded artifact (Python, appimagetool, the runtime stub, all
dependencies) checked out as byte-identical - because the divergence was
never in what was downloaded. Local was running a newer, patched
`appimage` than CI, resolved from an unpinned dependency, with different
default packaging flags. No amount of pinning `python_date`,
`appimagetool_sha256`, or `runtime_sha256` closes that gap, because none
of them touch which `appimage.ctl` runs the build in the first place.

Pin an exact version with a hash, the same way `pylock`/`build_pylock`
hash-pin everything else:

```toml
[tool.hatch.envs.appimage]
dependencies = [
    "appimage @ https://files.pythonhosted.org/packages/.../appimage-3.0.1-py3-none-any.whl#sha256=...",
]
```

pip verifies the hash fragment on a direct URL reference like this even
outside `--require-hashes` mode. A plain exact pin (`appimage==3.0.1`)
is better than a range, but still isn't hash-verified - if the release
on PyPI can't change after publishing, either is fine; if you don't want
to depend on that, use the hash form above.

### The exact packaging command line

For anyone trying to reproduce or debug a build by hand, this is the
full, effective command line `build()` runs - `appimage/ctl/build.py`,
not paraphrased:

```sh
appimagetool --runtime-file <staged copy of the file pinned by runtime_sha256> \
  --mksquashfs-opt -no-xattrs \
  --mksquashfs-opt -no-duplicates \
  [-u <update_info>] \
  <AppDir> <app>-<arch>.AppImage
```

Run with `cwd` set to `dist_dir` and `SOURCE_DATE_EPOCH` set in the
environment (`0` unless already set by the caller). `-no-xattrs` and
`-no-duplicates` are unconditional - not configurable, not skippable -
for the reasons in the `## Fixed` entries of the changelog: build-host
xattrs (e.g. SELinux labels) otherwise leak into the image, and
mksquashfs's duplicate-detection pre-filter otherwise makes the packaged
bytes sensitive to incidental per-build state even from an unchanged
AppDir.

appimagetool itself then builds its own `mksquashfs` invocation
(`sfs_mksquashfs()` in `AppImage/appimagetool`'s `appimagetool.c` -
read directly from source, not inferred from behavior):

```sh
mksquashfs <AppDir> <destination> \
  -offset <runtime size in bytes> \
  -comp zstd \
  -root-owned \
  -noappend \
  -b 128K \
  [-wildcards -ef .appimageignore]   # only if that file exists in cwd
  [-mkfs-time 0]                     # only if SOURCE_DATE_EPOCH is NOT set in the environment
  <our --mksquashfs-opt flags, appended last>
```

The `-mkfs-time 0` branch is normally *not* taken in this project's own
builds, since `SOURCE_DATE_EPOCH` is always set - mksquashfs reads it
from the environment directly instead. `-offset` is how appimagetool
appends the squashfs image after the runtime ELF stub in one file rather
than concatenating two files afterward; `-comp`/`-b` come from
appimagetool's own defaults for zstd, not from this project.

If you're trying to reproduce a specific build outside this tool
entirely - to bisect a divergence, or to verify a release by hand - this
is the actual, complete set of arguments both tools receive; nothing
else is passed.

### AppDir-only builds need fewer pins

`build-appdir` assembles the AppDir - installing Python and packages,
copying assets, compiling bytecode, scrubbing build-machine paths - and
stops there, without ever resolving appimagetool or the runtime stub:

```sh
python -m appimage.ctl build-appdir
```

Useful on its own: the result is a complete, runnable installation tree
that can be tested, inspected, or deployed some other way, without ever
producing a single-file `.AppImage`. Because of that, `reproducible`
only requires `python_date` (or `python_dir`, below) for this command -
`appimagetool_sha256`/`runtime_sha256` are irrelevant to something that
never touches appimagetool. `check` reports the two halves as separate
checklist lines, "AppDir reproducibility" and "Packaging reproducibility",
for exactly this reason. A full build (no command, or `enable-reproducible`)
still requires all three, since it does go on to package.

### Bundling an already-extracted Python

```toml
[tool.appimage]
python_dir = "/opt/verified-python"
```

An alternative to `python_archive` for a Python distribution that's
already unpacked on disk - copied into `AppDir/python` as-is, instead of
extracting a tarball. Config-only, deliberately with no CLI flag: setting
it is meant to be a considered, committed-to-`pyproject.toml` decision.

There's no single archive file left to hash by the time a directory
exists, so this is used exactly as given, with no verification. It's for
a directory whose *provenance* was already established elsewhere
(`uv python install`, or a prior `python_archive` + `python_sha256` run)
and is now trusted as a fixed input - the same way pointing
`appimagetool`/`runtime_file` at a local path is trusted without a hash
pin. `reproducible` accepts `python_dir` in place of `python_date` for
that reason - but `check`'s checklist marks it *trusted, unverified*
rather than a hash-checked pin, since you're asserting that trust, not
`appimage.ctl`.

Set at most one of `python_dir`/`python_archive` - having both is
ambiguous and rejected as a config error.

## Verified dependencies

Everything above pins *appimage.ctl's own* build tooling - appimagetool,
the runtime stub, the interpreter. None of it touches how your project's
third-party dependencies get installed: by default, `pip install
".[extras]"` resolves and downloads whatever the index currently serves,
unverified. A compromised or typosquatted package pulled in that way ends
up inside the AppImage with nothing to catch it.

One dependency is the exception, verified with no configuration at all:
the bundled `appimage` runtime module itself (the one AppRun and the
`--python-*` flags depend on) is always installed pinned to the exact
version of `appimage.ctl` doing the build, hash-verified against the
digest PyPI publishes for that release - its correct hash is always
knowable in advance, unlike third-party packages. Falls back to a
warning (or a hard error under `verify_downloads`) if PyPI can't be
reached. `pylock` (below) covers it the normal way once configured,
since `appimage_pin` joins the lock alongside everything else.

`pylock` closes that gap:

```toml
[tool.appimage]
pylock = "pylock.toml"
```

```sh
python -m appimage.ctl lock              # generate/refresh pylock.toml
python -m appimage.ctl build --require-pylock  # abort if pylock isn't set
```

### `lock` is a thin wrapper, not a new mechanism

`lock` does not implement any hashing or dependency resolution itself.
It runs `pip lock` (built into pip since 25.1) through the bundled
python-build-standalone interpreter rather than your own, once for
runtime dependencies and once for the build backend (below), writing
`pylock.toml` and a second pylock-format file respectively:

```sh
build/AppDir/python/bin/python3 -m pip lock \
    appimage==3.0.0 ".[extras]" <packages...> \
    -o pylock.toml
```

Running it through the bundled interpreter, not your local one, is the
one thing `lock` adds over typing that command by hand: `pip lock`
resolves wheels for whatever interpreter runs it, so a lock generated with
your local Python could pin a different platform/ABI than what the
AppImage actually bundles. `lock` also reads `extras`/`packages` from
`[tool.appimage]` for you, so that list isn't maintained twice.

`appimage==3.0.0` and any `packages` entries are real PyPI distributions
and stay in the lock with their own hash like any other dependency - only
the local project (`.`/`.[extras]`) has no stable hash to pin between
source edits, so it's installed separately at build time (below) instead.
`lock` resolves everything together, then strips just the local project's
entry from the result afterwards - deliberately not via `pip lock
--only-deps`, which excludes *every* given requirement, not a chosen one,
and would have dropped `appimage`'s and `packages`' own pins too (see
[For LLMs and coding agents](llms.md) for the full reasoning).

### What the real build does with it

With `pylock` configured, `_prepare_python` runs two separate `pip
install` calls instead of one:

```sh
pip install --no-compile --no-deps .[extras]              # local source, trusted, unhashed
pip install --no-compile --require-hashes -r pylock.toml  # everything else, hash-verified
```

Two calls, not one, because pip's hash-checking mode - triggered the
moment any requirement in a given invocation carries a hash - then demands
*every* requirement in that same invocation carry one; mixing the unhashed
local project into the `--require-hashes` call would fail outright.
`--no-deps` on the local install keeps it strictly to its own listed
dependencies. The lock install deliberately omits `--no-deps`: pip still
checks each locked package's declared dependencies against what
`pylock.toml` provides, so a stale or incomplete lock aborts loudly here
instead of silently installing an AppDir missing a transitive dependency.

### Cooldowns

`pip lock` also accepts `--uploaded-prior-to`, passed through via
`--uploaded-prior-to PnD` on `lock` (e.g. `P7D`): excludes packages
published more recently than that window from the resolution, giving the
community time to catch a compromised release before it gets locked in.
It only makes sense at generation time - the real build installs exactly
what's already pinned in `pylock.toml`, so a cooldown there would have
nothing left to act on.

### Private package indexes (Artifactory, Nexus, devpi, ...)

Neither `lock` nor a normal build passes any pip-specific flags for index
selection or authentication - no `--index-url`, no custom `env=` for the
subprocess. Every `pip`/`pip lock` call in `appimage.ctl` inherits the
calling process's environment as-is, so pip's own standard mechanisms
already work with no configuration on appimage's side:

- `PIP_INDEX_URL` / `PIP_EXTRA_INDEX_URL` / `PIP_TRUSTED_HOST` / `PIP_CERT`
  environment variables
- `pip.conf` (or `PIP_CONFIG_FILE`) at whatever location pip normally
  searches
- `.netrc` for per-host credentials

Point these at an internal Artifactory/Nexus/devpi mirror the same way you
would for any other pip invocation, and both `packages`/`extras`
installs and `lock`'s dependency resolution pick it up automatically.
Credentials belong in environment/config, not as CLI arguments to a
subprocess - an argument list can leak to other users on the same
machine via process listings.

No one-off CLI passthrough for occasional overrides yet (e.g. pointing a
single `lock` run at a different index without touching `pip.conf`) -
only the persistent env/config path above.

### Relationship to `reproducible`

`pylock`/`require_pylock` are deliberately independent of `reproducible`
- hash-pinned dependencies and byte-identical output are separate
guarantees, and `reproducible` does not imply or require `pylock`. Opt
into both explicitly if you want both - `enable-reproducible` happens to
set up both together as a convenience, but nothing stops you from setting
`reproducible = true` by hand after a piecewise `init`, without ever
running `lock`.

### Known limits

`pip lock` is documented by pip itself as experimental - its behavior may
change without notice in a future pip release. `pip install -r
pylock.toml --require-hashes` needs pip >= 26.1 in the bundled
interpreter; `lock` checks for pip >= 25.1 (what `pip lock` itself
needs) before generating, but a build against an existing `pylock.toml`
with an older bundled pip will fail with a plain pip error rather than
this tool's own message.

A package pinned in `pylock.toml` with no prebuilt wheel - only an
`sdist` - is built from source at install time, inside pip's own
randomly-named isolated build directory. If that package compiles a
native extension, the result can embed that random temp path in its
debug info - a different path from the AppDir this project scrubs (see
["The build machine's own absolute path"
above](#why-this-is-hard)), so that scrubbing has no way to catch it.
`lock` and every later `check`/`build` warn when this is the case,
naming the package - harmless if it's pure Python, worth a second look
if it isn't.

## Verified build backend

`pylock` hash-pins the packaged project's *third-party* dependencies -
not the project itself. Installing the project's own source always
triggers a PEP 517 isolated build, and by default pip populates that
isolated environment by resolving the project's `[build-system].requires`
(e.g. `setuptools`, `hatchling`, `uv_build`) fresh from the index, on
every single build. Unpinned and unverified - the same class of gap
`pylock` closes for runtime dependencies, just one level down.

`build_pylock` closes it, generated by `lock` alongside `pylock.toml` -
no separate flag or hand-written file needed:

```sh
python -m appimage.ctl lock
```

```toml
[tool.appimage]
build_pylock = "pylock.build.toml"
```

`[build-system].requires` changes rarely, so `lock` re-locks it on
every run alongside `pylock.toml` rather than needing a dedicated flag -
cheap when nothing changed, and it means one command keeps both in sync.
Point `build_pylock` at whatever path fits your project's own
conventions; `pylock.build.toml` above is just `lock`'s default.

Consumed differently from `pylock.toml`: `pip install --build-constraint`
[doesn't accept the pylock
format](https://pip.pypa.io/en/stable/cli/pip_install/#cmdoption-build-constraint)
PEP 751 defines, so `build_pylock` is converted to a classic
hash-pinned constraints file at install time and passed as
`--build-constraint` when installing the project itself - pip still
builds it in its own fresh, throwaway isolated environment; only what
gets installed *into* that environment is now hash-verified instead of
resolved live. (An earlier approach installed the backend directly into
the main interpreter with `--no-build-isolation` instead - left it
permanently installed in the shipped AppImage and broke reproducibility
via its own install-time bytecode timestamps; see [For LLMs and coding
agents](llms.md) for why that was dropped.)

`require_build_pylock = true` aborts the build instead of warning when
`build_pylock` isn't set, mirroring `require_pylock`.

### Known limits

`build_pylock` only covers packages resolved while building your
project's wheel - it doesn't change how the project's *own*
`[build-system].requires` version specifier itself is resolved (an
unpinned specifier like `"hatchling"` still floats to whatever `pip lock`
resolves as latest, unless pinned). For most backends, pin
`[build-system].requires` to an exact version in your own
`pyproject.toml` too, so `lock` has one specific release to hash
rather than a moving target - see the [development
chapter](develop/reproducible-builds.md) for this project's own choice of
backend and how it's pinned (a bounded range rather than an exact pin,
for reasons specific to that backend, and pinned via a separate,
hand-generated `requirements-build.txt` for *this project's own* PyPI
wheel - a different, unrelated mechanism from `build_pylock` above).

## Hooks, extra files, and a custom AppRun are outside this guarantee

Everything on this page so far is something `appimage.ctl` fully controls and can make
deterministic on your behalf. Four config keys hand control to something it can't:
`[tool.appimage.hooks]`'s `post_install`/`pre_package` (arbitrary shell scripts),
`[tool.appimage.extra_files]` (arbitrary files copied in as-is), `apprun` (a fully
custom AppRun, copied in place of the generated one), and `python_dir` (see
["Bundling an already-extracted Python"](#bundling-an-already-extracted-python) above -
a pre-extracted interpreter, used exactly as given).

None of these are hash-verified or checked for determinism - there's nothing to check
against. A `post_install` hook that writes the current wall-clock time into a generated
file, an `extra_files` entry whose source is itself regenerated fresh on every checkout,
or a hand-written `apprun` that embeds `$(uuidgen)`, all make the final AppImage
non-reproducible - and `appimage.ctl` has no way to detect or prevent it, for the same
reason `python_dir` is used "exactly as given, with no verification": the content is
arbitrary, supplied by the project, and never inspected.

This isn't a gap to close. Hooks and extra files exist precisely so a project can do
something `appimage.ctl` doesn't know about - there's no way to verify determinism for
code the tool has never seen. Keeping such a hook deterministic (no wall-clock, no
randomness, same input always produces the same output) is the project author's own
responsibility for that one piece, the same discipline any reproducible build needs
elsewhere. One thing still applies regardless: bytecode compilation (with its `-s`
path-stripping) runs *after* `pre_package`, so whatever a hook does to an installed
package's source is still compiled hash-based and path-scrubbed like everything else -
see ["The build machine's own absolute path"](#why-this-is-hard) (item 6).

## Not covered here

This page is about the AppImages `appimage.ctl` produces for *your*
project. For the reproducibility of the `appimage` package's own PyPI
wheel, see the [development chapter](develop/reproducible-builds.md).
