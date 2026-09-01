# How AppImages are built

This page explains the mechanics behind AppImage packaging for Python applications — both the
manual process and what `appimage.ctl` automates. Understanding the manual steps makes it
clear what the module does and why each piece exists.

## What an AppImage is

An AppImage is a single executable file that bundles an application together with all its
dependencies into a SquashFS filesystem image. A small ELF runtime is prepended to the
image; when the file is executed, that runtime uses FUSE to mount the SquashFS and then
runs the `AppRun` entry point inside it. The application sees a consistent, self-contained
directory tree at `$APPDIR`, regardless of what is installed on the host system.

## The AppDir layout

Before `appimagetool` packs everything into a `.AppImage` file, it expects a directory with
this structure:

```
AppDir/
├── AppRun              ← executable entry point (bash script)
├── myapp.desktop       ← Freedesktop metadata
├── myapp.png           ← application icon
└── python/             ← complete Python distribution
    ├── bin/
    │   └── python3
    └── lib/
        └── python3.x/
            └── site-packages/
                ├── appimage/   ← the runtime module
                └── myapp/      ← your application
```

Three files at the root are mandatory for AppImage: `AppRun`, a `.desktop` file, and an icon
matching the desktop file's `Icon=` value. Everything else is up to you — for a Python
application that means shipping a complete Python distribution under `python/`.

## python-build-standalone and uv

The Python distribution bundled in the AppImage comes from
[python-build-standalone](https://github.com/astral-sh/python-build-standalone), a project
that produces pre-built, highly portable Python interpreters for Linux, macOS, and Windows.

[Astral](https://astral.sh) — the team behind `uv` — now maintains python-build-standalone
and uses it as the direct source for `uv python install`. This means the Python you get when
you run `uv python install 3.11` and the Python bundled in your AppImage are built from
**exactly the same release artifacts**. Same compiler flags, same standard library, same
binary layout.

The practical consequence: if you develop and test with `uv`, there are no interpreter
surprises at packaging time. The AppImage ships the interpreter your code already ran against.

python-build-standalone provides several artifact variants. The one used for AppImages is
`install_only_stripped` — it contains the interpreter, standard library, and headers, but
omits test suites and debug symbols, keeping the download small. The archive extracts to a
`python/` directory ready to be placed directly inside `AppDir`.

### Releases and download URLs

Releases are published at
[github.com/astral-sh/python-build-standalone/releases](https://github.com/astral-sh/python-build-standalone/releases).
Each release is tagged with a date (e.g. `20260211`). The download URL follows a fixed
pattern:

```
https://github.com/astral-sh/python-build-standalone/releases/download/{date}/
    cpython-{python_version}+{date}-{arch}-unknown-linux-gnu-install_only_stripped.tar.gz
```

The `{arch}` token depends on the host machine:

| `uname -m` | URL arch token |
|---|---|
| `x86_64` | `x86_64` |
| `aarch64` | `aarch64` |
| `armv7l` | `armv7` |

```{note}
`uv` uses the same python-build-standalone distributions when you run
`uv python install`. If `uv` is already available on the build machine, it can
download and cache the interpreter for you — but the tarball it uses internally is
the same `install_only_stripped` artifact described here.
```

## Building manually

The following steps reproduce what `appimage.ctl` does, without using the module.
Each step can be run and verified independently. Requirements: `curl`, `tar`, a Linux host.

Set these variables in your shell before running the steps below. Adjust `APP`,
`PYTHON_MINOR`, and the entry point in the AppRun script to match your project.

```bash
APP="myapp"
PYTHON_MINOR="3.11"
ARCH="$(uname -m)"
```

### Step 1 — Create the AppDir skeleton

The AppDir is the staging directory that `appimagetool` will later pack into a single file.

```bash
mkdir -p build/AppDir
```

### Step 2 — Download Python

The python-build-standalone URL uses a different arch token than `uname -m` on armv7.
Apply the mapping first:

```bash
case "$ARCH" in
  armv7l) PBS_ARCH="armv7" ;;
  *)      PBS_ARCH="$ARCH" ;;
esac
```

Then resolve the download URL and fetch the tarball:

```bash
RELEASE_DATE=$(curl -s \
  "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" \
  | grep '"tag_name"' | cut -d'"' -f4)

PBS_URL=$(curl -s \
  "https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/${RELEASE_DATE}" \
  | grep '"browser_download_url"' \
  | grep "cpython-${PYTHON_MINOR}\." \
  | grep "${PBS_ARCH}-unknown-linux-gnu-install_only_stripped" \
  | grep -v "freethreaded" \
  | cut -d'"' -f4)

curl -L "$PBS_URL" -o build/python.tar.gz
```

For reproducible builds, replace the first `curl` with a hardcoded date:

```bash
RELEASE_DATE="20260211"
```

### Step 3 — Extract Python into AppDir

The tarball extracts to a `python/` directory. Placing it directly inside `AppDir` gives
the final path `AppDir/python/bin/python3`.

```bash
tar -xf build/python.tar.gz -C build/AppDir
```

After this step, verify the interpreter works:

```bash
build/AppDir/python/bin/python3 --version
```

### Step 4 — Install the application

Use the bundled interpreter's `pip` to install the application and the `appimage` runtime
module into the bundled site-packages. This keeps everything self-contained inside `AppDir`.

```bash
build/AppDir/python/bin/python3 -m pip install --no-compile appimage myapp
```

The `appimage` package is the runtime component that handles entry point dispatch,
`--python-interpreter`, and virtual environment support at launch time.

`--no-compile` skips pip's usual bytecode compilation, which invalidates each `.pyc`
against the *install-time source mtime* — a value that differs on every build even
when the installed content is identical. `appimage.ctl` compiles bytecode itself,
once, at the very end of AppDir assembly (after any hooks have run), using
`compileall --invalidation-mode unchecked-hash` so a `.pyc`'s validity is tied to a
hash of its source instead:

```bash
build/AppDir/python/bin/python3 -m compileall -qf \
  --invalidation-mode unchecked-hash \
  build/AppDir/python/lib/python3.x/site-packages
```

`-f` (force) matters here: merely *importing* a module — not just pip compiling it —
can leave behind a `.pyc` already timestamp-invalidated by an earlier build step (a
build backend, a lifecycle hook). Without `-f`, `compileall` leaves an
existing-looking `.pyc` alone instead of regenerating it in hash-based mode,
reintroducing the same non-determinism through a side door.

### Step 5 — Write the AppRun script

`AppRun` is the executable entry point that the Linux kernel calls when the AppImage is
run. It must be at the root of `AppDir` and must be executable.

Create a file named `AppRun` with the following content:

```bash
#!/bin/bash
set -e

if [ -n "$APPIMAGE" ]; then
    appimage_path=$(dirname "$APPIMAGE")
    if [ -d "$appimage_path/squashfs-root" ]; then
        export APPDIR="$appimage_path/squashfs-root"
    fi
fi

if [ -z "$APPDIR" ]; then
    export APPDIR=$(dirname $(readlink -f "$0"))
fi

exec "$APPDIR/python/bin/python3" -P -m appimage --python-main myapp "$@"
```

Replace `myapp` in `--python-main` with the console script name your package defines in
`[project.scripts]`.

Then copy it into place and mark it executable:

```bash
cp AppRun build/AppDir/AppRun
chmod +x build/AppDir/AppRun
```

The script sets `APPDIR` to the root of the mounted filesystem, then hands off to the
`appimage` runtime module. The `squashfs-root` block at the top handles the case where
the AppImage was extracted manually (e.g. on systems without FUSE).

**The `-P` flag** passed to Python activates *isolated mode*:

- `PYTHONPATH` from the host environment is ignored
- The user site-packages directory (`~/.local/lib/python3.x/site-packages`) is not added to `sys.path`
- `PYTHONSTARTUP` is not executed

Without `-P`, a package installed in the user's home directory could shadow a package
bundled in the AppImage. The flag ensures the AppImage only ever loads what is inside
`AppDir/python/`.

### Step 6 — Write the .desktop file

AppImage requires a Freedesktop `.desktop` file at the root of `AppDir`. The filename
must match the `Icon=` value and the AppImage filename prefix.

Create a file named `myapp.desktop` with the following content:

```ini
[Desktop Entry]
Type=Application
Name=myapp
Icon=myapp
Categories=Utility;
Terminal=true
```

Then copy it into place:

```bash
cp myapp.desktop build/AppDir/myapp.desktop
```

### Step 7 — Add an icon

AppImage requires an icon file whose basename matches the `Icon=` value in the `.desktop`
file. PNG and SVG are both accepted.

```bash
cp myapp.png build/AppDir/${APP}.png
```

If no icon is available, a placeholder can be created with ImageMagick:

```bash
convert -size 256x256 xc:gray build/AppDir/${APP}.png
```

### Step 8 — Download appimagetool and the runtime stub

`appimagetool` is the official tool for packing an AppDir into a SquashFS-based AppImage.
It is distributed as a self-contained AppImage itself. `appimage.ctl` uses
[`AppImage/appimagetool`](https://github.com/AppImage/appimagetool) — the maintained
successor to `AppImage/AppImageKit`'s classic `appimagetool` — because AppImageKit's
bundled `mksquashfs` has a documented non-deterministic multi-threaded compression bug
(see [AppImageKit #929](https://github.com/AppImage/AppImageKit/issues/929)) that made
byte-identical output impossible no matter how the AppDir itself was normalized;
`AppImage/appimagetool` bundles a fixed, modern squashfs-tools instead.

```bash
curl -L \
  "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage" \
  -o build/appimagetool
chmod +x build/appimagetool
```

Newer appimagetool releases no longer embed the AppImage runtime ELF stub — they fetch
it live, over the network, at packaging time. Pre-fetching it yourself avoids depending
on that live fetch succeeding (some network environments, e.g. certain TLS-intercepting
proxies, can't complete it) and lets you verify what you got:

```bash
curl -L \
  "https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-${ARCH}" \
  -o build/runtime
chmod +x build/runtime
```

Both are rolling `continuous` releases — the asset is replaced in place whenever a new
build is published — but unlike AppImageKit's old `continuous` tag, GitHub *does*
publish a sha256 digest per asset for both of these repos via its Releases API, so a
fresh download can be verified against it at no extra cost. Downloading either one
unpinned still means two builds run at different times, or on different machines, can
end up packing with different binaries — the one piece of the pipeline `appimage.ctl`
cannot make reproducible purely through AppDir normalization. Set `appimagetool_sha256`
and `runtime_sha256` in `[tool.appimage]` (or run `init` to write both
automatically from whatever's currently resolved) to pin and verify them; without a
pin, `appimage.ctl` still logs the sha256 of whichever binaries it used, so they can
be copied into config later.

### Step 9 — Pack the AppImage

```bash
mkdir -p dist
build/appimagetool --runtime-file build/runtime build/AppDir dist/${APP}-${ARCH}.AppImage
```

`appimagetool` compresses the AppDir into a SquashFS image, prepends the AppImage
runtime (a small ELF binary that mounts and executes it — `--runtime-file` supplies the
copy from Step 8 instead of triggering another live download), and writes the result as
a single executable file.

## What `appimage.ctl` automates

`python -m appimage.ctl` performs the same steps as described above, with these
additions:

**Configuration resolution** — app name, entry point, and Python version are read from
`[project]` in `pyproject.toml`. Running `check` shows exactly what was detected and from
where before anything is built.

**GitHub API lookup** — the module queries the python-build-standalone releases API to find
the correct `install_only_stripped` asset for the current architecture and the requested
Python minor version. Passing `python_date` in `[tool.appimage]` pins the exact
release tag. A freshly downloaded tarball is also verified against the sha256 digest
GitHub publishes for the asset (or against `python_sha256`, if set) — no extra network
request needed.

**Hash-based bytecode** — installed packages are compiled with
`compileall --invalidation-mode unchecked-hash` instead of relying on pip's default
timestamp-based `.pyc` cache (see Step 4).

**Timestamp normalization** — every file and directory in the AppDir has its mtime set
to `SOURCE_DATE_EPOCH` (default: the Unix epoch) right before packaging, and the same
value is passed into appimagetool's own process environment, since it touches a few
paths of its own (e.g. `.DirIcon`) that AppDir-side normalization can't reach.

**appimagetool and runtime verification** — both resolve from an explicit path, then
the build cache, then a download; `PATH` is never searched for either (see [Classic
appimagetool detected](reproducible-builds.md#classic-appimagetool-detected)).
`appimagetool_sha256`/`runtime_sha256` verify the resolved binary before use; a
mismatch aborts rather than silently packing with the wrong one (see Step 8).
appimagetool is also checked for known signs of the classic, non-deterministic build
and refused outright on a match, pin or not. The runtime file is always pre-fetched
and passed via `--runtime-file`, so appimagetool never triggers its own live
download. `verify_downloads` turns an unpinned resolution into a hard error instead
of a warning.

**Dependency and build-backend verification** — with `pylock`/`build_pylock` set,
third-party dependencies and the project's own `[build-system].requires` backend are
installed hash-verified (`pip install --require-hashes` / `--build-constraint`)
instead of resolved live from whatever the index currently serves. Generated via
`lock`; see [Reproducible builds](reproducible-builds.md#verified-dependencies) for
the full mechanism.

**Caching** — the Python tarball, `appimagetool` binary, and runtime file are all
cached in `build/` and reused on subsequent builds.

**AppRun generation** — the AppRun script is generated from a template that also handles
the `squashfs-root` fallback for environments without FUSE (containers, some CI systems),
and injects any extra environment variables defined under `[tool.appimage.env]`.

**Desktop file generation** — the `.desktop` file is generated from `pyproject.toml`
metadata (`name`, `description`). A custom file can be provided via the `desktop` key in
`[tool.appimage]`.

**Lifecycle hooks** — shell scripts can run after `pip install` (`post_install`) or after
all files are in place but before `appimagetool` runs (`pre_package`). The `APPDIR`
environment variable is set when the hook executes, so hooks can modify the AppDir directly.
Bytecode compilation (above) runs after `pre_package`, so a hook that edits an installed
package's source is still reflected in the compiled `.pyc`.

**Extra files** — arbitrary files or directories are copied into the AppDir via
`[tool.appimage.extra_files]`.

## The runtime module

The `appimage` Python package (installed into `AppDir/python/lib/.../site-packages/`) is
the bridge between the static `AppRun` bash script and your application. It is invoked as
`python3 -P -m appimage --python-main <entry_point>` by every generated AppRun.

At startup it:

1. Reads `--python-main` to know the default entry point
2. Checks `VIRTUAL_ENV` and `ARGV0` to detect whether it was invoked through a virtual
   environment symlink — if so, it activates that environment by adjusting `sys.path`,
   `sys.prefix`, and the relevant environment variables
3. Strips all `--python-*` arguments from `sys.argv` before forwarding to the application,
   so your code never sees them

The `--python-interpreter` flag replaces the process with a fresh `python3 -P` invocation
(via `os.execvp`) passing any remaining arguments. This is how interactive use and
`-m venv` work: the module hands control back to the raw interpreter after the AppImage
environment is set up.

When creating a virtual environment via `--python-interpreter -m venv`, the module patches
the venv so that its `python3` symlink points to the AppImage file itself rather than to
the interpreter binary inside `AppDir`. This makes the AppImage act as the Python
interpreter for the venv — all bundled packages are available, and `pip install` into the
venv adds packages on top without repackaging.
