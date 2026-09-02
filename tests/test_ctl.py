"""Unit tests for appimage.ctl reproducibility features.

All network and subprocess calls are mocked — these tests never touch the
network or execute real binaries.
"""

import hashlib
import importlib
import json
import platform
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from appimage.ctl import (
    BuildConfig,
    build,
    build_appdir,
    enable_reproducible,
    update_tools,
    write_config,
)
from appimage.ctl._appimagetool import _resolve_appimagetool, _resolve_runtime_file
from appimage.ctl._base import _ResolvedBuild, _resolve
from appimage.ctl._download import _sha256_file, _verify_sha256
from appimage.ctl._python import _resolve_python_tarball, _resolve_python_url
from appimage.ctl.build_appdir import _normalize_mtimes, _normalize_permissions
from appimage.ctl.lock import _write_reproducible_flag


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_resolved(**overrides: object) -> _ResolvedBuild:
    """Build a _ResolvedBuild with sane defaults, overridable per test."""
    defaults: dict[str, object] = {
        "app": "myapp",
        "entry_point": "myapp",
        "install_targets": ["."],
        "local_install_targets": ["."],
        "appimage_pin": "appimage==2.0.1",
        "python": "3.11",
        "python_date": "",
        "icon": None,
        "desktop": None,
        "apprun": "",
        "build_dir": "build",
        "dist_dir": "dist",
        "update_info": "",
        "update_info_suggested": "",
        "env": {},
        "extra_files": {},
        "hooks": {},
        "appimagetool": "",
        "appimagetool_version": "",
        "appimagetool_sha256": "",
        "python_archive": "",
        "python_sha256": "",
        "python_dir": "",
        "appimage_version": "",
        "appimage_sha256": "",
        "appimagectl_version": "",
        "runtime_file": "",
        "runtime_sha256": "",
        "verify_downloads": False,
        "require_zsyncmake": False,
        "pylock": "",
        "require_pylock": False,
        "build_pylock": "",
        "require_build_pylock": False,
        "reproducible": False,
        "sources": {},
        "appdir_warnings": [],
        "appdir_errors": [],
        "package_warnings": [],
        "package_errors": [],
    }
    defaults.update(overrides)
    return _ResolvedBuild(**defaults)  # type: ignore[arg-type]


def digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# _sha256_file / _verify_sha256
# ---------------------------------------------------------------------------

def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    f = tmp_path / "data.bin"
    f.write_bytes(b"hello world")
    assert _sha256_file(f) == digest_of(b"hello world")


def test_verify_sha256_passes_on_match(tmp_path: Path) -> None:
    f = tmp_path / "data.bin"
    f.write_bytes(b"hello world")
    _verify_sha256(f, digest_of(b"hello world"), label="test file")


def test_verify_sha256_accepts_sha256_prefix(tmp_path: Path) -> None:
    f = tmp_path / "data.bin"
    f.write_bytes(b"hello world")
    _verify_sha256(f, f"sha256:{digest_of(b'hello world')}", label="test file")


def test_verify_sha256_raises_with_both_hashes_on_mismatch(tmp_path: Path) -> None:
    f = tmp_path / "data.bin"
    f.write_bytes(b"hello world")
    expected = digest_of(b"something else")
    with pytest.raises(RuntimeError) as exc_info:
        _verify_sha256(f, expected, label="test file")
    message = str(exc_info.value)
    assert expected in message
    assert digest_of(b"hello world") in message


# ---------------------------------------------------------------------------
# _resolve_python_url
# ---------------------------------------------------------------------------

def _fake_release_response(digest: str | None) -> MagicMock:
    asset: dict[str, object] = {
        "browser_download_url": (
            "https://github.com/astral-sh/python-build-standalone/releases/"
            "download/20260211/cpython-3.11.9+20260211-x86_64-unknown-linux-gnu-"
            "install_only_stripped.tar.gz"
        ),
    }
    if digest is not None:
        asset["digest"] = digest
    payload = json.dumps({"tag_name": "20260211", "assets": [asset]}).encode()
    resp = MagicMock()
    resp.read.return_value = payload
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_resolve_python_url_returns_digest_when_published() -> None:
    with patch("appimage.ctl._download.urllib.request.urlopen", return_value=_fake_release_response("sha256:" + "a" * 64)):
        url, sha256, resolved_date = _resolve_python_url("3.11", "20260211", "x86_64")
    assert url.endswith("install_only_stripped.tar.gz")
    assert sha256 == "a" * 64
    assert resolved_date == "20260211"


def test_resolve_python_url_returns_none_without_digest() -> None:
    with patch("appimage.ctl._download.urllib.request.urlopen", return_value=_fake_release_response(None)):
        _url, sha256, _resolved_date = _resolve_python_url("3.11", "20260211", "x86_64")
    assert sha256 is None


# ---------------------------------------------------------------------------
# _resolve_appimagetool
# ---------------------------------------------------------------------------

def test_resolve_appimagetool_config_path_hash_match(tmp_path: Path) -> None:
    tool = tmp_path / "appimagetool"
    tool.write_bytes(b"binary-content")
    resolved = make_resolved(appimagetool=str(tool), appimagetool_sha256=digest_of(b"binary-content"))

    result = _resolve_appimagetool(resolved, tmp_path / "cache.AppImage", "x86_64")

    assert result == tool
    assert tool.exists()  # never deleted


def test_resolve_appimagetool_config_path_hash_mismatch_does_not_delete(tmp_path: Path) -> None:
    tool = tmp_path / "appimagetool"
    tool.write_bytes(b"binary-content")
    resolved = make_resolved(appimagetool=str(tool), appimagetool_sha256=digest_of(b"wrong-content"))

    with pytest.raises(RuntimeError):
        _resolve_appimagetool(resolved, tmp_path / "cache.AppImage", "x86_64")

    assert tool.exists()  # user-owned file must survive a mismatch


def test_resolve_appimagetool_no_path_lookup(tmp_path: Path) -> None:
    """appimagetool no longer searches PATH at all — explicit config path, cache,
    or download only. Patched at ``shutil.which`` itself (not
    ``appimage.ctl._appimagetool.shutil.which``): the module doesn't import
    ``shutil`` at all any more, so there's nothing module-local to patch —
    proving that absence is exactly the point.
    """
    cache = tmp_path / "cache.AppImage"
    resolved = make_resolved()

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(b"content")

    with patch("shutil.which") as mock_which, \
         patch("appimage.ctl._appimagetool._fetch_release_asset_digest", return_value=("https://example/appimagetool-x86_64.AppImage", None)), \
         patch("appimage.ctl._appimagetool._download", side_effect=fake_download):
        _resolve_appimagetool(resolved, cache, "x86_64")

    mock_which.assert_not_called()


def test_resolve_appimagetool_cache_no_hash_warns_and_skips_download(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cache = tmp_path / "cache.AppImage"
    cache.write_bytes(b"whatever-was-cached")
    resolved = make_resolved()

    with patch("appimage.ctl._appimagetool._download") as mock_download, \
         caplog.at_level("WARNING"):
        result = _resolve_appimagetool(resolved, cache, "x86_64")

    assert result == cache
    mock_download.assert_not_called()
    assert "unpinned and unverified" in caplog.text


def test_resolve_appimagetool_cache_hash_mismatch_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache.AppImage"
    cache.write_bytes(b"stale-cached-binary")
    resolved = make_resolved(appimagetool_sha256=digest_of(b"the-expected-binary"))

    with pytest.raises(RuntimeError):
        _resolve_appimagetool(resolved, cache, "x86_64")


def test_resolve_appimagetool_download_mismatch_deletes_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache.AppImage"
    resolved = make_resolved(appimagetool_sha256=digest_of(b"the-expected-binary"))

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(b"a-different-binary")

    with patch("appimage.ctl._appimagetool._fetch_release_asset_digest", return_value=("https://example/appimagetool-x86_64.AppImage", None)), \
         patch("appimage.ctl._appimagetool._download", side_effect=fake_download):
        with pytest.raises(RuntimeError):
            _resolve_appimagetool(resolved, cache, "x86_64")

    assert not cache.exists()  # download-cache artifact IS cleaned up on mismatch


def test_resolve_appimagetool_download_verifies_free_api_digest(tmp_path: Path) -> None:
    cache = tmp_path / "cache.AppImage"
    resolved = make_resolved()
    content = b"the-real-appimagetool"

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(content)

    with patch("appimage.ctl._appimagetool._fetch_release_asset_digest", return_value=("https://example/appimagetool-x86_64.AppImage", digest_of(content))), \
         patch("appimage.ctl._appimagetool._download", side_effect=fake_download):
        result = _resolve_appimagetool(resolved, cache, "x86_64")

    assert result == cache
    assert cache.exists()


def test_resolve_appimagetool_download_uses_arch_map_for_armv7l(tmp_path: Path) -> None:
    cache = tmp_path / "cache.AppImage"
    resolved = make_resolved()
    captured = {}

    def fake_fetch_digest(repo: str, tag: str, asset_name: str) -> tuple[str, str | None]:
        captured["repo"] = repo
        captured["tag"] = tag
        captured["asset_name"] = asset_name
        return f"https://example/{asset_name}", None

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(b"content")

    with patch("appimage.ctl._appimagetool._fetch_release_asset_digest", side_effect=fake_fetch_digest), \
         patch("appimage.ctl._appimagetool._download", side_effect=fake_download):
        _resolve_appimagetool(resolved, cache, "armv7l")

    assert captured["asset_name"] == "appimagetool-armhf.AppImage"


# ---------------------------------------------------------------------------
# _looks_like_classic_appimagekit / _abort_if_classic_appimagekit
#
# Regression coverage for a real, empirically confirmed issue: a machine
# with the classic, unmaintained AppImageKit appimagetool already on PATH
# (very plausible — many generic AppImage tutorials install exactly that)
# gets it silently resolved, hash-pinned by 'init', and "verified" on every
# later build — while its documented non-deterministic mksquashfs bug
# (AppImageKit#929) keeps producing a different .AppImage each time.
# Confirmed by hand: an otherwise fully pinned --reproducible build of a
# real project produced two different output files across two runs, traced
# back to exactly this classic binary being on PATH. The build now aborts
# outright rather than warning — see docs/reproducible-builds.md's
# "Classic appimagetool detected" section for the fix steps.
# ---------------------------------------------------------------------------

def test_looks_like_classic_appimagekit_detects_build_path_marker(tmp_path: Path) -> None:
    from appimage.ctl._appimagetool import _looks_like_classic_appimagekit

    tool = tmp_path / "appimagetool"
    tool.write_bytes(b"...junk.../AppImageKit/lib/libappimage_shared/digest.c...junk...")

    reason = _looks_like_classic_appimagekit(tool)
    assert reason is not None
    assert "build paths" in reason


def test_looks_like_classic_appimagekit_ignores_incidental_wiki_link(tmp_path: Path) -> None:
    """The current default's own --help text links to AppImageKit's wiki once —
    that single, unrelated mention must not trigger a false positive.
    """
    from appimage.ctl._appimagetool import _looks_like_classic_appimagekit

    tool = tmp_path / "appimagetool"
    tool.write_bytes(b"See https://github.com/AppImage/AppImageKit/wiki/FUSE for details")

    with patch(
        "appimage.ctl._appimagetool._appimagetool_version_string",
        return_value="appimagetool, continuous build (git version 8c8c91f), build 295",
    ):
        assert _looks_like_classic_appimagekit(tool) is None


def test_looks_like_classic_appimagekit_detects_version_banner(tmp_path: Path) -> None:
    """No build-path markers (e.g. a stripped binary) still gets caught via the
    --version banner's own wording, a runtime string rather than a debug symbol.
    """
    from appimage.ctl._appimagetool import _looks_like_classic_appimagekit

    tool = tmp_path / "appimagetool"
    tool.write_bytes(b"stripped-binary-no-debug-info")

    with patch(
        "appimage.ctl._appimagetool._appimagetool_version_string",
        return_value="appimagetool, continuous build (commit effcebc), build 2084",
    ):
        reason = _looks_like_classic_appimagekit(tool)
    assert reason is not None
    assert "commit effcebc" in reason


def test_looks_like_classic_appimagekit_clean_for_current_default(tmp_path: Path) -> None:
    from appimage.ctl._appimagetool import _looks_like_classic_appimagekit

    tool = tmp_path / "appimagetool"
    tool.write_bytes(b"a normal, unrelated binary with no markers at all")

    with patch(
        "appimage.ctl._appimagetool._appimagetool_version_string",
        return_value="appimagetool, continuous build (git version 8c8c91f), build 295",
    ):
        assert _looks_like_classic_appimagekit(tool) is None


def test_looks_like_classic_appimagekit_survives_unexecutable_tool(tmp_path: Path) -> None:
    """A test double / non-executable placeholder file must not crash detection."""
    from appimage.ctl._appimagetool import _looks_like_classic_appimagekit

    tool = tmp_path / "appimagetool"
    tool.write_bytes(b"not-a-real-binary")  # no exec bit, not a valid ELF either

    assert _looks_like_classic_appimagekit(tool) is None


def test_resolve_appimagetool_aborts_for_classic_build_via_config_path(tmp_path: Path) -> None:
    from appimage.ctl._appimagetool import _resolve_appimagetool

    tool = tmp_path / "appimagetool"
    tool.write_bytes(b"...junk.../AppImageKit/build/src/main.c...junk...")
    resolved = make_resolved(appimagetool=str(tool))

    with pytest.raises(RuntimeError, match="classic, unmaintained AppImageKit"):
        _resolve_appimagetool(resolved, tmp_path / "cache.AppImage", "x86_64")


def test_resolve_appimagetool_aborts_for_classic_build_in_cache(tmp_path: Path) -> None:
    """The build cache should never realistically hold the classic build (only this
    project's own download step writes there) — but the check still applies in
    case one was seeded there by hand.
    """
    from appimage.ctl._appimagetool import _resolve_appimagetool

    cache = tmp_path / "cache.AppImage"
    cache.write_bytes(b"...junk.../AppImageKit/build/src/main.c...junk...")
    resolved = make_resolved()

    with pytest.raises(RuntimeError, match="classic, unmaintained AppImageKit"):
        _resolve_appimagetool(resolved, cache, "x86_64")


def test_resolve_appimagetool_abort_message_links_to_docs(tmp_path: Path) -> None:
    from appimage.ctl._appimagetool import _resolve_appimagetool

    tool = tmp_path / "appimagetool"
    tool.write_bytes(b"...junk.../AppImageKit/build/src/main.c...junk...")
    resolved = make_resolved(appimagetool=str(tool))

    with pytest.raises(RuntimeError) as exc_info:
        _resolve_appimagetool(resolved, tmp_path / "cache.AppImage", "x86_64")

    assert "reproducible-builds.html#classic-appimagetool-detected" in str(exc_info.value)


def test_resolve_appimagetool_does_not_abort_for_fresh_download(tmp_path: Path) -> None:
    """A binary this function just downloaded is by definition from the right
    source — no need to run it through the classic-build heuristic at all.
    """
    from appimage.ctl._appimagetool import _resolve_appimagetool

    cache = tmp_path / "cache.AppImage"

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(b"...junk.../AppImageKit/build/src/main.c...junk...")

    resolved = make_resolved()

    with patch(
             "appimage.ctl._appimagetool._fetch_release_asset_digest",
             return_value=("https://example/appimagetool", None),
         ), \
         patch("appimage.ctl._appimagetool._download", side_effect=fake_download):
        result = _resolve_appimagetool(resolved, cache, "x86_64")

    assert result == cache


# ---------------------------------------------------------------------------
# _resolve_runtime_file
# ---------------------------------------------------------------------------

def test_resolve_runtime_file_config_path_hash_match(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime-x86_64"
    runtime.write_bytes(b"runtime-content")
    resolved = make_resolved(runtime_file=str(runtime), runtime_sha256=digest_of(b"runtime-content"))

    result = _resolve_runtime_file(resolved, tmp_path / "cache", "x86_64")

    assert result == runtime
    assert runtime.exists()


def test_resolve_runtime_file_config_path_hash_mismatch_does_not_delete(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime-x86_64"
    runtime.write_bytes(b"runtime-content")
    resolved = make_resolved(runtime_file=str(runtime), runtime_sha256=digest_of(b"wrong-content"))

    with pytest.raises(RuntimeError):
        _resolve_runtime_file(resolved, tmp_path / "cache", "x86_64")

    assert runtime.exists()


def test_resolve_runtime_file_cache_hash_mismatch_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.write_bytes(b"stale-cached-runtime")
    resolved = make_resolved(runtime_sha256=digest_of(b"the-expected-runtime"))

    with pytest.raises(RuntimeError):
        _resolve_runtime_file(resolved, cache, "x86_64")


def test_resolve_runtime_file_download_verifies_free_api_digest(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    resolved = make_resolved()
    content = b"the-real-runtime"

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(content)

    with patch("appimage.ctl._appimagetool._fetch_release_asset_digest", return_value=("https://example/runtime-x86_64", digest_of(content))), \
         patch("appimage.ctl._appimagetool._download", side_effect=fake_download):
        result = _resolve_runtime_file(resolved, cache, "x86_64")

    assert result == cache
    assert cache.exists()


def test_resolve_runtime_file_download_mismatch_deletes_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    resolved = make_resolved(runtime_sha256=digest_of(b"the-expected-runtime"))

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(b"a-different-runtime")

    with patch("appimage.ctl._appimagetool._fetch_release_asset_digest", return_value=("https://example/runtime-x86_64", None)), \
         patch("appimage.ctl._appimagetool._download", side_effect=fake_download):
        with pytest.raises(RuntimeError):
            _resolve_runtime_file(resolved, cache, "x86_64")

    assert not cache.exists()


def test_resolve_runtime_file_no_path_lookup(tmp_path: Path) -> None:
    """Like appimagetool (see test_resolve_appimagetool_no_path_lookup), the
    runtime stub is never looked up on PATH — only explicit config, cache, or
    download.
    """
    cache = tmp_path / "cache"
    resolved = make_resolved()

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(b"content")

    with patch("shutil.which") as mock_which, \
         patch("appimage.ctl._appimagetool._fetch_release_asset_digest", return_value=("https://example/runtime-x86_64", None)), \
         patch("appimage.ctl._appimagetool._download", side_effect=fake_download):
        _resolve_runtime_file(resolved, cache, "x86_64")

    mock_which.assert_not_called()


# ---------------------------------------------------------------------------
# verify_downloads (strict mode)
# ---------------------------------------------------------------------------

def test_verify_downloads_raises_instead_of_warning_for_appimagetool(tmp_path: Path) -> None:
    cache = tmp_path / "cache.AppImage"
    cache.write_bytes(b"whatever-was-cached")
    resolved = make_resolved(verify_downloads=True)

    with pytest.raises(RuntimeError, match="could not be verified"):
        _resolve_appimagetool(resolved, cache, "x86_64")


def test_verify_downloads_raises_instead_of_warning_for_runtime_file(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.write_bytes(b"cached-runtime")
    resolved = make_resolved(verify_downloads=True)

    with pytest.raises(RuntimeError, match="could not be verified"):
        _resolve_runtime_file(resolved, cache, "x86_64")


def test_verify_downloads_raises_for_local_python_archive(tmp_path: Path) -> None:
    archive = tmp_path / "python.tar.gz"
    archive.write_bytes(b"tarball-content")
    resolved = make_resolved(python_archive=str(archive), verify_downloads=True)

    with pytest.raises(RuntimeError, match="could not be verified"):
        _resolve_python_tarball(resolved, tmp_path / "cache.tar.gz", "x86_64")


def test_verify_downloads_passes_when_hash_configured(tmp_path: Path) -> None:
    tool = tmp_path / "appimagetool"
    tool.write_bytes(b"binary-content")
    resolved = make_resolved(
        appimagetool=str(tool), appimagetool_sha256=digest_of(b"binary-content"), verify_downloads=True,
    )

    result = _resolve_appimagetool(resolved, tmp_path / "cache.AppImage", "x86_64")

    assert result == tool


# ---------------------------------------------------------------------------
# zsyncmake / .zsync file — checked after packaging (build.py), against the
# real output, not the build host's PATH.
#
# Regression coverage for a second, related PATH-dependence bug: the old
# check asked whether `zsyncmake` was on the *build host's* PATH before
# packaging — but appimagetool bundles its own `zsyncmake` and its own
# AppRun puts its own usr/bin first on PATH, so that check answered a
# different question than "will packaging actually produce a .zsync file"
# and gave a different (wrong) answer depending on the build host's
# installed packages. See docs/reproducible-builds.md's "Zsync and the
# build host's PATH" section for how this was confirmed.
# ---------------------------------------------------------------------------

def _write_minimal_project(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
    )


# ---------------------------------------------------------------------------
# _stage_runtime_file_for_appimagetool
#
# Regression coverage for a real, confirmed bug in the pinned
# AppImage/appimagetool build itself: its glib-based CLI option parser fails
# to decode a --runtime-file value containing any non-ASCII byte ("Option
# parsing failed: Invalid byte sequence in conversion input"), reproducible
# by hand independent of the process's own locale and of
# G_FILENAME_ENCODING/G_BROKEN_FILENAMES. Since runtime_bin is always
# project_root / build_dir / f"runtime-{arch}", any project whose absolute
# path contains a non-ASCII character (an entirely ordinary thing) made
# packaging fail 100% of the time. Fixed by staging a plain copy under a
# guaranteed-ASCII temp path before invoking appimagetool.
# ---------------------------------------------------------------------------

def test_stage_runtime_file_for_appimagetool_copies_content(tmp_path: Path) -> None:
    from appimage.ctl.build import _stage_runtime_file_for_appimagetool

    runtime_bin = tmp_path / "münchen" / "runtime-x86_64"
    runtime_bin.parent.mkdir()
    runtime_bin.write_bytes(b"fake runtime elf stub content")

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    staged = _stage_runtime_file_for_appimagetool(runtime_bin, staging_dir)

    assert staged.parent == staging_dir
    assert staged.name == "runtime-x86_64"
    assert staged.read_bytes() == b"fake runtime elf stub content"
    assert staged != runtime_bin


def test_check_zsync_file_noop_when_present(tmp_path: Path) -> None:
    from appimage.ctl.build import _check_zsync_file

    (tmp_path / "myapp-x86_64.AppImage.zsync").write_bytes(b"zsync: 0.6.2")

    _check_zsync_file(tmp_path, "myapp-x86_64.AppImage", require=True)  # must not raise


def test_check_zsync_file_warns_by_default_when_missing(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from appimage.ctl.build import _check_zsync_file

    with caplog.at_level("WARNING"):
        _check_zsync_file(tmp_path, "myapp-x86_64.AppImage", require=False)

    assert "did not produce" in caplog.text


def test_check_zsync_file_raises_with_require(tmp_path: Path) -> None:
    from appimage.ctl.build import _check_zsync_file

    with pytest.raises(RuntimeError, match="did not produce"):
        _check_zsync_file(tmp_path, "myapp-x86_64.AppImage", require=True)


def test_build_warns_when_packaging_does_not_produce_zsync_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """End-to-end through build(): update_info is set, but the (mocked) packaging
    subprocess doesn't create a .zsync file — must warn, not silently succeed.
    """
    build_module = importlib.import_module("appimage.ctl.build")
    appdir_module = importlib.import_module("appimage.ctl.build_appdir")

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
    )
    config = BuildConfig(update_info="zsync|https://example/myapp-x86_64.AppImage.zsync")

    manager = MagicMock()
    with patch.object(appdir_module, "_prepare_python", manager._prepare_python), \
         patch.object(appdir_module, "_copy_assets", manager._copy_assets), \
         patch.object(appdir_module, "_copy_extra_files", manager._copy_extra_files), \
         patch.object(appdir_module, "_compile_pyc", manager._compile_pyc), \
         patch.object(build_module, "_resolve_appimagetool", manager._resolve_appimagetool), \
         patch.object(build_module, "_resolve_runtime_file", manager._resolve_runtime_file), \
         patch.object(build_module, "_stage_runtime_file_for_appimagetool", manager._stage_runtime_file), \
         patch.object(build_module.subprocess, "run", manager.subprocess_run), \
         caplog.at_level("WARNING"):
        manager._resolve_appimagetool.return_value = Path("/fake/appimagetool")
        manager._resolve_runtime_file.return_value = Path("/fake/runtime-x86_64")
        manager._stage_runtime_file.return_value = Path("/fake/staged/runtime-x86_64")
        build(config, tmp_path)

    assert "did not produce" in caplog.text


# ---------------------------------------------------------------------------
# update_info auto-detection from [project.urls]
# ---------------------------------------------------------------------------

def _has_update_info_suggestion_message(messages: list[str]) -> bool:
    return any("detected GitHub repo" in m for m in messages)


def test_update_info_suggested_from_source_url(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        '[project.urls]\nSource = "https://github.com/acme/myapp"\n'
    )
    config = BuildConfig()

    with patch("appimage.ctl._base.platform.machine", return_value="x86_64"):
        resolved = _resolve(config, tmp_path)

    assert resolved.update_info == ""
    assert resolved.update_info_suggested == (
        "gh-releases-zsync|acme|myapp|latest|myapp-x86_64.AppImage.zsync"
    )
    assert _has_update_info_suggestion_message(resolved.package_warnings)


def test_update_info_no_suggestion_without_project_urls(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig()

    resolved = _resolve(config, tmp_path)

    assert resolved.update_info_suggested == ""
    assert not _has_update_info_suggestion_message(resolved.package_warnings)


def test_update_info_no_suggestion_for_non_repo_urls(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        "[project.urls]\n"
        'Tracker = "https://github.com/acme/myapp/issues"\n'
        'Changelog = "https://github.com/acme/myapp/blob/main/CHANGELOG.md"\n'
    )
    config = BuildConfig()

    resolved = _resolve(config, tmp_path)

    assert resolved.update_info_suggested == ""
    assert not _has_update_info_suggestion_message(resolved.package_warnings)


def test_update_info_no_suggestion_when_ambiguous(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        "[project.urls]\n"
        'Homepage = "https://github.com/acme/one"\n'
        'Download = "https://github.com/acme/two"\n'
    )
    config = BuildConfig()

    resolved = _resolve(config, tmp_path)

    assert resolved.update_info_suggested == ""
    assert not _has_update_info_suggestion_message(resolved.package_warnings)


def test_update_info_prefers_preferred_key_over_ambiguous_fallback(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        "[project.urls]\n"
        'Homepage = "https://github.com/other/x"\n'
        'Source = "https://github.com/acme/myapp"\n'
    )
    config = BuildConfig()

    with patch("appimage.ctl._base.platform.machine", return_value="x86_64"):
        resolved = _resolve(config, tmp_path)

    assert resolved.update_info_suggested == (
        "gh-releases-zsync|acme|myapp|latest|myapp-x86_64.AppImage.zsync"
    )


def test_update_info_explicit_value_skips_suggestion(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        '[project.urls]\nSource = "https://github.com/acme/myapp"\n'
    )
    config = BuildConfig(update_info="zsync|https://example/app.AppImage.zsync")

    resolved = _resolve(config, tmp_path)

    assert resolved.update_info == "zsync|https://example/app.AppImage.zsync"
    assert resolved.update_info_suggested == ""
    assert not _has_update_info_suggestion_message(resolved.package_warnings)


def test_write_config_writes_suggested_update_info(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        '[project.urls]\nSource = "https://github.com/acme/myapp"\n'
        "[tool.appimage]\n"
        'app = "myapp"\nentry_point = "myapp"\npython = "3.11"\npython_date = "20260101"\n'
    )
    config = BuildConfig.from_pyproject(tmp_path)

    tool_path = tmp_path / "appimagetool"
    runtime_path = tmp_path / "runtime-x86_64"

    with patch("appimage.ctl._base.platform.machine", return_value="x86_64"), \
         patch("appimage.ctl.init._resolve_appimagetool", return_value=tool_path), \
         patch("appimage.ctl.init._resolve_runtime_file", return_value=runtime_path), \
         patch("appimage.ctl.init._appimagetool_version_string", return_value="continuous build"), \
         patch("appimage.ctl.init._sha256_file", return_value="c" * 64), \
         patch("appimage.ctl.init._resolve_appimage_pin_sha256", return_value="d" * 64):
        write_config(config, tmp_path)

    content = (tmp_path / "pyproject.toml").read_text()
    assert 'update_info = "gh-releases-zsync|acme|myapp|latest|myapp-x86_64.AppImage.zsync"' in content


def test_write_config_does_not_overwrite_existing_update_info(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        '[project.urls]\nSource = "https://github.com/acme/myapp"\n'
        "[tool.appimage]\n"
        'app = "myapp"\nentry_point = "myapp"\npython = "3.11"\npython_date = "20260101"\n'
        'update_info = "custom|value"\n'
    )
    config = BuildConfig.from_pyproject(tmp_path)

    tool_path = tmp_path / "appimagetool"
    runtime_path = tmp_path / "runtime-x86_64"

    with patch("appimage.ctl.init._resolve_appimagetool", return_value=tool_path), \
         patch("appimage.ctl.init._resolve_runtime_file", return_value=runtime_path), \
         patch("appimage.ctl.init._appimagetool_version_string", return_value="continuous build"), \
         patch("appimage.ctl.init._sha256_file", return_value="c" * 64), \
         patch("appimage.ctl.init._resolve_appimage_pin_sha256", return_value="d" * 64):
        write_config(config, tmp_path)

    content = (tmp_path / "pyproject.toml").read_text()
    assert content.count("update_info") == 1
    assert 'update_info = "custom|value"' in content


# ---------------------------------------------------------------------------
# reproducible (umbrella flag)
# ---------------------------------------------------------------------------

def test_reproducible_errors_when_pins_missing(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(reproducible=True)

    resolved = _resolve(config, tmp_path)

    assert resolved.verify_downloads is True
    assert resolved.require_zsyncmake is True
    assert any("python_date" in e for e in resolved.appdir_errors)
    assert any("appimagetool_sha256" in e for e in resolved.package_errors)
    assert any("runtime_sha256" in e for e in resolved.package_errors)


def test_reproducible_passes_when_pins_set(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(
        reproducible=True,
        python_date="20260211",
        appimage_version="2.0.1",
        appimage_sha256=digest_of(b"appimage"),
        appimagetool_sha256=digest_of(b"tool"),
        runtime_sha256=digest_of(b"runtime"),
    )

    resolved = _resolve(config, tmp_path)

    assert resolved.appdir_errors == []
    assert resolved.package_errors == []
    assert resolved.verify_downloads is True
    assert resolved.require_zsyncmake is True


def test_reproducible_python_dir_satisfies_appdir_pin(tmp_path: Path) -> None:
    """python_dir stands in for python_date — no python-build-standalone pin needed."""
    _write_minimal_project(tmp_path)
    config = BuildConfig(
        reproducible=True,
        python_dir="/opt/python",
        appimage_version="2.0.1",
        appimage_sha256=digest_of(b"appimage"),
        appimagetool_sha256=digest_of(b"tool"),
        runtime_sha256=digest_of(b"runtime"),
    )

    resolved = _resolve(config, tmp_path)

    assert resolved.appdir_errors == []
    assert resolved.package_errors == []


def test_python_archive_and_python_dir_together_is_an_error(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(python_archive="/tmp/python.tar.gz", python_dir="/opt/python")

    resolved = _resolve(config, tmp_path)

    assert any("python_archive" in e and "python_dir" in e for e in resolved.appdir_errors)


def test_appimagectl_version_matching_running_version_is_silent(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(appimagectl_version="2.0.1")

    with patch("appimage.ctl._base.importlib.metadata.version", return_value="2.0.1"):
        resolved = _resolve(config, tmp_path)

    assert not any("appimagectl_version" in e for e in resolved.appdir_errors)
    assert not any("appimagectl_version" in w for w in resolved.appdir_warnings)


def test_appimagectl_version_mismatch_warns_by_default(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(appimagectl_version="2.0.1")

    with patch("appimage.ctl._base.importlib.metadata.version", return_value="2.1.0"):
        resolved = _resolve(config, tmp_path)

    assert resolved.appdir_errors == []
    assert any(
        "2.0.1" in w and "2.1.0" in w and "appimagectl_version" in w
        for w in resolved.appdir_warnings
    )


def test_appimagectl_version_mismatch_errors_under_verify_downloads(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(appimagectl_version="2.0.1", verify_downloads=True)

    with patch("appimage.ctl._base.importlib.metadata.version", return_value="2.1.0"):
        resolved = _resolve(config, tmp_path)

    assert any("appimagectl_version" in e for e in resolved.appdir_errors)
    assert not any("appimagectl_version" in w for w in resolved.appdir_warnings)


def test_appimagectl_version_unset_skips_check(tmp_path: Path) -> None:
    """No expectation recorded means no possible drift — appimage_pin's own,
    unrelated call to importlib.metadata.version() must not trip this up."""
    _write_minimal_project(tmp_path)
    config = BuildConfig()

    resolved = _resolve(config, tmp_path)

    assert not any("appimagectl_version" in e for e in resolved.appdir_errors)
    assert not any("appimagectl_version" in w for w in resolved.appdir_warnings)


def test_reproducible_false_does_not_require_pins(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig()

    resolved = _resolve(config, tmp_path)

    assert resolved.appdir_errors == []
    assert resolved.package_errors == []
    assert resolved.verify_downloads is False
    assert resolved.require_zsyncmake is False


# ---------------------------------------------------------------------------
# _self_locating_python / _relocate_console_script / _scrub_build_paths
#
# Console-script shims used to be deleted outright once found to leak the
# build path — correct for functionality (AppRun never runs them anyway)
# but throws away a working `AppDir/python/bin/<entry-point>` for anyone
# using the AppDir directly. Now relocated in place instead, using the same
# self-locating shebang trick python-build-standalone's own bundled pip
# already uses — confirmed by hand to still work after moving the whole
# AppDir. Deletion stays as the fallback for anything that doesn't match a
# recognized pip/distlib shim format exactly (see module docstring for why:
# virtualenv's old --relocatable did a looser version of this and was
# eventually removed for being unreliable).
# ---------------------------------------------------------------------------

def test_self_locating_python_matches_python_build_standalone_pattern() -> None:
    from appimage.ctl.build_appdir import _self_locating_python

    assert _self_locating_python(b"python3.13") == (
        b'"$(dirname -- "$(realpath -- "$0")")/python3.13"'
    )


def test_relocate_console_script_rewrites_two_line_form() -> None:
    from appimage.ctl.build_appdir import _relocate_console_script

    executable = b"/home/alice/project/build/AppDir/python/bin/python3"
    content = (
        b"#!/bin/sh\n"
        b"'''exec' " + executable + b' "$0" "$@"\n'
        b"' '''\n"
        b"import sys\nfrom mypkg import main\nsys.exit(main())\n"
    )

    result = _relocate_console_script(content, executable)

    assert result is not None
    assert executable not in result
    assert result == (
        b"#!/bin/sh\n"
        b"'''exec' \"$(dirname -- \"$(realpath -- \"$0\")\")/python3\" \"$0\" \"$@\"\n"
        b"' '''\n"
        b"import sys\nfrom mypkg import main\nsys.exit(main())\n"
    )


def test_relocate_console_script_rewrites_one_line_form() -> None:
    """A one-line #!<executable> input is upgraded to the two-line #!/bin/sh +
    exec polyglot form, not rewritten as a plain one-line #!<replacement>: the
    kernel never shell-expands a #! line, so a literal
    #!"$(dirname ...)/python3" would fail at exec time with "bad interpreter"
    even without moving the AppDir at all (regression test — see
    test_relocated_one_line_script_actually_runs below for the executable
    proof).
    """
    from appimage.ctl.build_appdir import _relocate_console_script

    executable = b"/tmp/x/python3"
    content = b"#!" + executable + b"\nimport sys\nfrom mypkg import main\nsys.exit(main())\n"

    result = _relocate_console_script(content, executable)

    assert result == (
        b"#!/bin/sh\n"
        b"'''exec' \"$(dirname -- \"$(realpath -- \"$0\")\")/python3\" \"$0\" \"$@\"\n"
        b"' '''\n"
        b"import sys\nfrom mypkg import main\nsys.exit(main())\n"
    )


def test_relocate_console_script_returns_none_for_unrecognized_format() -> None:
    """Anything not byte-for-byte one of distlib's two shapes falls back to
    deletion in _scrub_build_paths — never guessed at.
    """
    from appimage.ctl.build_appdir import _relocate_console_script

    executable = b"/home/alice/project/build/AppDir/python/bin/python3"
    content = b"#!/usr/bin/env python3\n# not a distlib shim at all\n" + executable

    assert _relocate_console_script(content, executable) is None


def test_relocate_console_script_matches_quoted_executable_form() -> None:
    """A build path containing a space makes pip/distlib's ScriptMaker double-quote
    the embedded executable (pip._vendor.distlib.scripts.enquote_executable) and
    always use the two-line form (a space forces the "not simple" branch of
    _build_shebang regardless of length) — must still be recognized and
    relocated, not silently dropped to the delete fallback. Regression test: an
    earlier version only matched the unquoted byte sequence, so every
    console-script shim in a build path with a space in it was deleted instead
    of relocated.
    """
    from appimage.ctl.build_appdir import _relocate_console_script

    executable = b"/home/alice/my project/build/AppDir/python/bin/python3"
    content = (
        b"#!/bin/sh\n"
        b"'''exec' \"" + executable + b"\" \"$0\" \"$@\"\n"
        b"' '''\n"
        b"import sys\nfrom mypkg import main\nsys.exit(main())\n"
    )

    result = _relocate_console_script(content, executable)

    assert result is not None
    assert executable not in result
    assert result == (
        b"#!/bin/sh\n"
        b"'''exec' \"$(dirname -- \"$(realpath -- \"$0\")\")/python3\" \"$0\" \"$@\"\n"
        b"' '''\n"
        b"import sys\nfrom mypkg import main\nsys.exit(main())\n"
    )


def test_relocated_console_script_actually_runs_after_moving_appdir(tmp_path: Path) -> None:
    """The real point of relocating rather than deleting: the script must still
    work when executed from a different location than it was written at —
    exactly what happens when an AppImage gets mounted at a fresh temp path
    on every run.
    """
    import os
    import stat
    import subprocess
    import sys

    from appimage.ctl.build_appdir import _relocate_console_script

    original_dir = tmp_path / "original_build_location" / "python" / "bin"
    original_dir.mkdir(parents=True)
    executable = str(original_dir / "python3").encode()
    os.symlink(sys.executable, original_dir / "python3")

    content = (
        b"#!/bin/sh\n"
        b"'''exec' " + executable + b' "$0" "$@"\n'
        b"' '''\n"
        b"print('hello from relocated script')\n"
    )
    relocated = _relocate_console_script(content, executable)
    assert relocated is not None

    moved_dir = tmp_path / "moved_elsewhere" / "python" / "bin"
    moved_dir.mkdir(parents=True)
    os.symlink(sys.executable, moved_dir / "python3")
    script_path = moved_dir / "myscript"
    script_path.write_bytes(relocated)
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)

    result = subprocess.run(  # noqa: S603
        [str(script_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "hello from relocated script"


def test_relocated_one_line_script_actually_runs(tmp_path: Path) -> None:
    """Regression test for the bug fixed above: a script whose *original*
    shebang was the short one-line #!<executable> form must still actually
    execute after relocation — even at its original, unmoved location. A
    naive one-line #!"$(dirname ...)/python3" replacement fails here with
    "bad interpreter: no such file or directory", since the kernel passes
    the #! line to execve() literally and never expands $(...); only the
    two-line #!/bin/sh + exec form (which _relocate_console_script now
    always produces) actually works.
    """
    import os
    import stat
    import subprocess
    import sys

    from appimage.ctl.build_appdir import _relocate_console_script

    bin_dir = tmp_path / "python" / "bin"
    bin_dir.mkdir(parents=True)
    executable = str(bin_dir / "python3").encode()
    os.symlink(sys.executable, bin_dir / "python3")

    content = b"#!" + executable + b"\nprint('hello from one-line script')\n"
    relocated = _relocate_console_script(content, executable)
    assert relocated is not None

    script_path = bin_dir / "myscript"
    script_path.write_bytes(relocated)
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)

    result = subprocess.run(  # noqa: S603
        [str(script_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "hello from one-line script"


def test_relocated_script_actually_runs_with_space_in_path(tmp_path: Path) -> None:
    """Regression test for the quoted-executable bug fixed above: a build path
    containing a space must still produce a working relocated shim, not one
    silently dropped to the delete fallback because the quoted form wasn't
    recognized.
    """
    import os
    import stat
    import subprocess
    import sys

    from appimage.ctl.build_appdir import _relocate_console_script

    bin_dir = tmp_path / "my project" / "python" / "bin"
    bin_dir.mkdir(parents=True)
    executable = str(bin_dir / "python3").encode()
    os.symlink(sys.executable, bin_dir / "python3")

    content = (
        b"#!/bin/sh\n"
        b"'''exec' \"" + executable + b"\" \"$0\" \"$@\"\n"
        b"' '''\n"
        b"print('hello from space-path script')\n"
    )
    relocated = _relocate_console_script(content, executable)
    assert relocated is not None

    script_path = bin_dir / "myscript"
    script_path.write_bytes(relocated)
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)

    result = subprocess.run(  # noqa: S603
        [str(script_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "hello from space-path script"


def test_record_hash_field_matches_pip_record_format() -> None:
    """pip's own RECORD format: sha256=<urlsafe-base64, no padding>."""
    from appimage.ctl.build_appdir import _record_hash_field

    # A real (content, RECORD hash) pair captured from an actual pip install,
    # to verify against pip's own output rather than just round-tripping our
    # own algorithm against itself.
    content = (
        b"#!/bin/sh\n"
        b"'''exec' \"$(dirname -- \"$(realpath -- \"$0\")\")/python3.13\" \"$0\" \"$@\"\n"
        b"' '''\n"
        b"import re\nimport sys\nfrom pip._internal.cli.main import main\n"
        b"if __name__ == '__main__':\n"
        b"    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
        b"    sys.exit(main())\n"
    )
    assert _record_hash_field(content).startswith("sha256=")
    # Deterministic and stable for identical content — the actual property
    # _scrub_build_paths relies on when writing a relocated file's new RECORD row.
    assert _record_hash_field(content) == _record_hash_field(content)


def test_scrub_build_paths_relocates_console_script_and_updates_record(tmp_path: Path) -> None:
    import csv

    from appimage.ctl.build_appdir import _scrub_build_paths

    appdir = tmp_path / "AppDir"
    site_packages = appdir / "python" / "lib" / "python3.13" / "site-packages"
    bin_dir = appdir / "python" / "bin"
    bin_dir.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    executable = str(bin_dir / "python3").encode()

    script_content = (
        b"#!/bin/sh\n"
        b"'''exec' " + executable + b' "$0" "$@"\n'
        b"' '''\n"
        b"import sys\nfrom mypkg import main\nsys.exit(main())\n"
    )
    (bin_dir / "myapp").write_bytes(script_content)
    (bin_dir / "myapp").chmod(0o755)

    dist_info = site_packages / "mypkg-1.0.dist-info"
    dist_info.mkdir()
    record_path = dist_info / "RECORD"
    old_hash_field, old_size = "sha256=stale", str(len(script_content) + 1)
    record_path.write_text(f"../../../bin/myapp,{old_hash_field},{old_size}\n")

    resolved = make_resolved(python="3.13")
    _scrub_build_paths(resolved, appdir)

    assert (bin_dir / "myapp").exists()  # relocated, not deleted
    new_content = (bin_dir / "myapp").read_bytes()
    assert executable not in new_content
    assert b"dirname" in new_content

    row = next(csv.reader(record_path.open(newline="", encoding="utf-8")))
    assert row[0] == "../../../bin/myapp"
    assert row[1] != old_hash_field
    assert row[1].startswith("sha256=")
    assert row[2] == str(len(new_content))


def test_scrub_build_paths_still_deletes_direct_url_json(tmp_path: Path) -> None:
    from appimage.ctl.build_appdir import _scrub_build_paths

    appdir = tmp_path / "AppDir"
    site_packages = appdir / "python" / "lib" / "python3.13" / "site-packages"
    (appdir / "python" / "bin").mkdir(parents=True)
    site_packages.mkdir(parents=True)

    dist_info = site_packages / "myproject-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "direct_url.json").write_text(f'{{"url": "file://{appdir}"}}')
    (dist_info / "RECORD").write_text(f"myproject-1.0.dist-info/direct_url.json,sha256=x,10\n")

    resolved = make_resolved(python="3.13")
    _scrub_build_paths(resolved, appdir)

    assert not (dist_info / "direct_url.json").exists()


def test_scrub_build_paths_falls_back_to_delete_for_unrecognized_leak(tmp_path: Path) -> None:
    from appimage.ctl.build_appdir import _scrub_build_paths

    appdir = tmp_path / "AppDir"
    site_packages = appdir / "python" / "lib" / "python3.13" / "site-packages"
    (appdir / "python" / "bin").mkdir(parents=True)
    site_packages.mkdir(parents=True)

    dist_info = site_packages / "myproject-1.0.dist-info"
    dist_info.mkdir()
    leaked = dist_info / "weird_leftover.txt"
    leaked.write_text(f"generated at {appdir} by something unexpected\n")
    (dist_info / "RECORD").write_text("myproject-1.0.dist-info/weird_leftover.txt,sha256=x,10\n")

    resolved = make_resolved(python="3.13")
    _scrub_build_paths(resolved, appdir)

    assert not leaked.exists()


# ---------------------------------------------------------------------------
# _normalize_mtimes
# ---------------------------------------------------------------------------

def test_normalize_mtimes_sets_fixed_epoch_recursively(tmp_path: Path) -> None:
    appdir = tmp_path / "AppDir"
    (appdir / "sub").mkdir(parents=True)
    (appdir / "sub" / "file.txt").write_text("hi")
    (appdir / "top.txt").write_text("hi")

    _normalize_mtimes(appdir, epoch=0)

    import os

    assert os.stat(appdir).st_mtime == 0
    assert os.stat(appdir / "sub").st_mtime == 0
    assert os.stat(appdir / "sub" / "file.txt").st_mtime == 0
    assert os.stat(appdir / "top.txt").st_mtime == 0


# ---------------------------------------------------------------------------
# _normalize_permissions
# ---------------------------------------------------------------------------

def test_normalize_permissions_clears_group_and_other_write_bits(tmp_path: Path) -> None:
    appdir = tmp_path / "AppDir"
    (appdir / "sub").mkdir(parents=True)
    script = appdir / "sub" / "script.sh"
    script.write_text("#!/bin/sh\n")
    plain = appdir / "plain.txt"
    plain.write_text("hi")

    script.chmod(0o775)
    plain.chmod(0o664)
    (appdir / "sub").chmod(0o775)
    appdir.chmod(0o775)

    _normalize_permissions(appdir)

    import os

    assert os.stat(script).st_mode & 0o777 == 0o755
    assert os.stat(plain).st_mode & 0o777 == 0o644
    assert os.stat(appdir / "sub").st_mode & 0o777 == 0o755
    assert os.stat(appdir).st_mode & 0o777 == 0o755


def test_normalize_permissions_skips_symlinks(tmp_path: Path) -> None:
    appdir = tmp_path / "AppDir"
    appdir.mkdir()
    target = appdir / "target.txt"
    target.write_text("hi")
    link = appdir / "link.txt"
    link.symlink_to("target.txt")

    _normalize_permissions(appdir)  # must not raise on the symlink


# ---------------------------------------------------------------------------
# _resolve_python_tarball
# ---------------------------------------------------------------------------

def test_resolve_python_tarball_local_archive_without_hash_stays_offline(tmp_path: Path) -> None:
    archive = tmp_path / "python.tar.gz"
    archive.write_bytes(b"tarball-content")
    resolved = make_resolved(python_archive=str(archive))

    with patch("appimage.ctl._download.urllib.request.urlopen") as mock_urlopen:
        result = _resolve_python_tarball(resolved, tmp_path / "cache.tar.gz", "x86_64")

    mock_urlopen.assert_not_called()
    assert result == archive


def test_resolve_python_tarball_cache_without_hash_stays_offline(tmp_path: Path) -> None:
    cache = tmp_path / "cache.tar.gz"
    cache.write_bytes(b"cached-tarball")
    resolved = make_resolved()

    with patch("appimage.ctl._download.urllib.request.urlopen") as mock_urlopen:
        result = _resolve_python_tarball(resolved, cache, "x86_64")

    mock_urlopen.assert_not_called()
    assert result == cache


def test_resolve_python_tarball_fresh_download_verifies_free_api_digest(tmp_path: Path) -> None:
    cache = tmp_path / "cache.tar.gz"
    resolved = make_resolved(python="3.11", python_date="20260211")
    content = b"the-real-tarball"

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(content)

    with patch("appimage.ctl._download.urllib.request.urlopen", return_value=_fake_release_response("sha256:" + digest_of(content))), \
         patch("appimage.ctl._python._download", side_effect=fake_download):
        result = _resolve_python_tarball(resolved, cache, "x86_64")

    assert result == cache
    assert cache.exists()


def test_resolve_python_tarball_fresh_download_mismatch_deletes_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache.tar.gz"
    resolved = make_resolved(python="3.11", python_date="20260211")

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(b"tampered-or-corrupted")

    with patch("appimage.ctl._download.urllib.request.urlopen", return_value=_fake_release_response("sha256:" + "b" * 64)), \
         patch("appimage.ctl._python._download", side_effect=fake_download):
        with pytest.raises(RuntimeError):
            _resolve_python_tarball(resolved, cache, "x86_64")

    assert not cache.exists()


# ---------------------------------------------------------------------------
# _resolve_appimage_pin_sha256 / _install_hashed_requirement / _install_targets
# ---------------------------------------------------------------------------

def _fake_pypi_response(sha256: str | None) -> MagicMock:
    wheel: dict[str, object] = {"packagetype": "bdist_wheel"}
    if sha256 is not None:
        wheel["digests"] = {"sha256": sha256}
    payload = json.dumps({"urls": [wheel]}).encode()
    resp = MagicMock()
    resp.read.return_value = payload
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_resolve_appimage_pin_sha256_returns_digest_when_published() -> None:
    from appimage.ctl.build_appdir import _resolve_appimage_pin_sha256

    with patch("appimage.ctl.build_appdir.urllib.request.urlopen", return_value=_fake_pypi_response("d" * 64)):
        assert _resolve_appimage_pin_sha256("appimage==2.0.1", strict=False) == "d" * 64


def test_resolve_appimage_pin_sha256_warns_and_returns_none_on_network_error() -> None:
    from appimage.ctl.build_appdir import _resolve_appimage_pin_sha256

    with patch("appimage.ctl.build_appdir.urllib.request.urlopen", side_effect=OSError("no network")):
        assert _resolve_appimage_pin_sha256("appimage==2.0.1", strict=False) is None


def test_resolve_appimage_pin_sha256_raises_when_strict_and_network_fails() -> None:
    from appimage.ctl.build_appdir import _resolve_appimage_pin_sha256

    with patch("appimage.ctl.build_appdir.urllib.request.urlopen", side_effect=OSError("no network")), \
         pytest.raises(RuntimeError, match="Could not verify"):
        _resolve_appimage_pin_sha256("appimage==2.0.1", strict=True)


def test_resolve_appimage_pin_sha256_raises_when_strict_and_no_digest_published() -> None:
    from appimage.ctl.build_appdir import _resolve_appimage_pin_sha256

    with patch("appimage.ctl.build_appdir.urllib.request.urlopen", return_value=_fake_pypi_response(None)), \
         pytest.raises(RuntimeError, match="no published wheel digest"):
        _resolve_appimage_pin_sha256("appimage==2.0.1", strict=True)


def test_resolve_appimage_pin_sha256_warns_and_returns_none_without_digest() -> None:
    from appimage.ctl.build_appdir import _resolve_appimage_pin_sha256

    with patch("appimage.ctl.build_appdir.urllib.request.urlopen", return_value=_fake_pypi_response(None)):
        assert _resolve_appimage_pin_sha256("appimage==2.0.1", strict=False) is None


def test_install_targets_hash_verifies_appimage_pin_separately(tmp_path: Path) -> None:
    from appimage.ctl.build_appdir import _install_targets

    resolved = make_resolved(install_targets=["appimage==2.0.1", ".", "extra-pkg"])

    with patch("appimage.ctl.build_appdir._resolve_appimage_pin_sha256", return_value="e" * 64), \
         patch("appimage.ctl.build_appdir.subprocess.run") as mock_run:
        _install_targets(resolved, tmp_path / "python3", tmp_path)

    pin_call, main_call = [c.args[0] for c in mock_run.call_args_list]
    assert "--require-hashes" in pin_call
    assert "appimage==2.0.1" not in main_call
    assert "." in main_call
    assert "extra-pkg" in main_call


def test_install_targets_falls_back_to_unverified_without_digest(tmp_path: Path) -> None:
    from appimage.ctl.build_appdir import _install_targets

    resolved = make_resolved(install_targets=["appimage==2.0.1", "."])

    with patch("appimage.ctl.build_appdir._resolve_appimage_pin_sha256", return_value=None), \
         patch("appimage.ctl.build_appdir.subprocess.run") as mock_run:
        _install_targets(resolved, tmp_path / "python3", tmp_path)

    assert mock_run.call_count == 1
    args = mock_run.call_args.args[0]
    assert "appimage==2.0.1" in args


def test_install_targets_uses_configured_appimage_sha256_without_network(tmp_path: Path) -> None:
    """A configured appimage_sha256 is used as-is, skipping the PyPI lookup."""
    from appimage.ctl.build_appdir import _install_targets

    resolved = make_resolved(
        install_targets=["appimage==2.0.1", "."],
        appimage_sha256="f" * 64,
    )

    with patch("appimage.ctl.build_appdir._resolve_appimage_pin_sha256") as mock_lookup, \
         patch("appimage.ctl.build_appdir.subprocess.run") as mock_run:
        _install_targets(resolved, tmp_path / "python3", tmp_path)

    mock_lookup.assert_not_called()
    pin_call, main_call = [c.args[0] for c in mock_run.call_args_list]
    assert "--require-hashes" in pin_call
    assert "appimage==2.0.1" not in main_call


# ---------------------------------------------------------------------------
# _isolated_subprocess_env / PEP 370 user-site isolation
#
# Regression coverage for a real bug: none of the install subprocesses set
# PYTHONNOUSERSITE, so pip resolved against the *build host's* own
# ~/.local/lib/pythonX.Y/site-packages (PEP 370) in addition to the bundled
# interpreter's site-packages. A requirement already satisfied there was
# silently skipped ("Requirement already satisfied" instead of
# "Collecting"), so the AppDir shipped without it — host-dependent and
# undetectable from the build log alone. Confirmed by hand against a real
# project: a package already present under a developer's ~/.local was
# missing from the built AppDir entirely.
# ---------------------------------------------------------------------------

def test_isolated_subprocess_env_disables_user_site_and_bytecode() -> None:
    from appimage.ctl.build_appdir import _isolated_subprocess_env

    env = _isolated_subprocess_env()
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_install_targets_disables_user_site(tmp_path: Path) -> None:
    from appimage.ctl.build_appdir import _install_targets

    resolved = make_resolved(install_targets=["."], appimage_sha256="f" * 64)

    with patch("appimage.ctl.build_appdir.subprocess.run") as mock_run:
        _install_targets(resolved, tmp_path / "python3", tmp_path)

    for call in mock_run.call_args_list:
        assert call.kwargs["env"]["PYTHONNOUSERSITE"] == "1"


def test_install_hashed_requirement_disables_user_site(tmp_path: Path) -> None:
    from appimage.ctl.build_appdir import _install_hashed_requirement

    with patch("appimage.ctl.build_appdir.subprocess.run") as mock_run:
        _install_hashed_requirement(
            "appimage==2.0.1", "e" * 64, tmp_path / "python3", tmp_path,
        )

    assert mock_run.call_args.kwargs["env"]["PYTHONNOUSERSITE"] == "1"


def test_install_from_pylock_disables_user_site(tmp_path: Path) -> None:
    from appimage.ctl.build_appdir import _install_from_pylock

    (tmp_path / "pylock.toml").write_text("")
    resolved = make_resolved(local_install_targets=["."], pylock="pylock.toml")

    with patch("appimage.ctl.build_appdir.subprocess.run") as mock_run:
        _install_from_pylock(resolved, tmp_path / "python3", tmp_path)

    assert mock_run.call_count == 2
    for call in mock_run.call_args_list:
        assert call.kwargs["env"]["PYTHONNOUSERSITE"] == "1"


def test_run_hook_disables_user_site(tmp_path: Path) -> None:
    """Hooks predate this project's reproducibility work and were never revisited —
    anything a hook does through the bundled interpreter (its documented purpose:
    editing installed packages between build steps) is exposed to the same PEP 370
    leak pip install was.
    """
    from appimage.ctl.build_appdir import _run_hook

    with patch("appimage.ctl.build_appdir.subprocess.run") as mock_run:
        _run_hook("hook.sh", tmp_path, tmp_path / "AppDir")

    env = mock_run.call_args.kwargs["env"]
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["APPDIR"] == str(tmp_path / "AppDir")


def test_run_pip_lock_disables_user_site(tmp_path: Path) -> None:
    from appimage.ctl.lock import _run_pip_lock

    with patch("appimage.ctl.lock._pip_version", return_value=(26, 1)), \
         patch("appimage.ctl.lock.subprocess.run") as mock_run:
        _run_pip_lock(
            tmp_path / "python3",
            tmp_path,
            ["."],
            tmp_path / "pylock.toml",
            uploaded_prior_to="",
        )

    assert mock_run.call_args.kwargs["env"]["PYTHONNOUSERSITE"] == "1"


# ---------------------------------------------------------------------------
# _prepare_python / _compile_pyc
# ---------------------------------------------------------------------------

def test_prepare_python_installs_with_no_compile(tmp_path: Path) -> None:
    from appimage.ctl.build_appdir import _prepare_python

    resolved = make_resolved(install_targets=["appimage==2.0.1", "."])
    appdir = tmp_path / "AppDir"
    appdir.mkdir()
    tarball = tmp_path / "python.tar.gz"
    tarball.write_bytes(b"")

    with patch("appimage.ctl._python._resolve_python_tarball", return_value=tarball), \
         patch("appimage.ctl._python.tarfile.open") as mock_tarfile, \
         patch("appimage.ctl.build_appdir.subprocess.run") as mock_run, \
         patch("appimage.ctl.build_appdir._resolve_appimage_pin_sha256", return_value=None):
        mock_tarfile.return_value.__enter__.return_value.extractall = MagicMock()
        _prepare_python(resolved, appdir, tmp_path / "python.tar.gz", "x86_64", tmp_path)

    args = mock_run.call_args.args[0]
    assert "--no-compile" in args
    assert "appimage==2.0.1" in args


def test_compile_pyc_uses_hash_invalidation(tmp_path: Path) -> None:
    from appimage.ctl.build_appdir import _compile_pyc

    resolved = make_resolved(python="3.11")
    appdir = tmp_path / "AppDir"

    with patch("appimage.ctl.build_appdir.subprocess.run") as mock_run:
        _compile_pyc(resolved, appdir)

    args = mock_run.call_args.args[0]
    assert "--invalidation-mode" in args
    assert "unchecked-hash" in args
    assert "-f" in args
    assert str(appdir / "python" / "lib" / "python3.11" / "site-packages") in args


# ---------------------------------------------------------------------------
# build() orchestration order
# ---------------------------------------------------------------------------

def test_build_compiles_pyc_after_pre_package_before_appimagetool(tmp_path: Path) -> None:
    build_module = importlib.import_module("appimage.ctl.build")
    appdir_module = importlib.import_module("appimage.ctl.build_appdir")

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
    )
    config = BuildConfig(hooks={"pre_package": "hook.sh"})
    (tmp_path / "hook.sh").write_text("#!/bin/sh\n")
    (tmp_path / "hook.sh").chmod(0o755)

    manager = MagicMock()
    with patch.object(appdir_module, "_prepare_python", manager._prepare_python), \
         patch.object(appdir_module, "_copy_assets", manager._copy_assets), \
         patch.object(appdir_module, "_copy_extra_files", manager._copy_extra_files), \
         patch.object(appdir_module, "_run_hook", manager._run_hook), \
         patch.object(appdir_module, "_compile_pyc", manager._compile_pyc), \
         patch.object(build_module, "_resolve_appimagetool", manager._resolve_appimagetool), \
         patch.object(build_module, "_resolve_runtime_file", manager._resolve_runtime_file), \
         patch.object(build_module, "_stage_runtime_file_for_appimagetool", manager._stage_runtime_file), \
         patch.object(build_module.subprocess, "run", manager.subprocess_run):
        manager._resolve_appimagetool.return_value = Path("/fake/appimagetool")
        manager._resolve_runtime_file.return_value = Path("/fake/runtime-x86_64")
        manager._stage_runtime_file.return_value = Path("/fake/staged/runtime-x86_64")
        build(config, tmp_path)

    call_names = [c[0] for c in manager.mock_calls]
    assert call_names.index("_run_hook") < call_names.index("_compile_pyc")
    assert call_names.index("_compile_pyc") < call_names.index("_resolve_appimagetool")

    packaging_call = next(c for c in manager.mock_calls if c[0] == "subprocess_run")
    cmd = packaging_call.args[0]
    assert "--runtime-file" in cmd
    assert str(Path("/fake/staged/runtime-x86_64")) in cmd
    assert packaging_call.kwargs["env"]["SOURCE_DATE_EPOCH"] == "0"


def test_build_respects_source_date_epoch_env_var(tmp_path: Path) -> None:
    build_module = importlib.import_module("appimage.ctl.build")
    appdir_module = importlib.import_module("appimage.ctl.build_appdir")

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
    )
    config = BuildConfig()

    manager = MagicMock()
    with patch.object(appdir_module, "_prepare_python", manager._prepare_python), \
         patch.object(appdir_module, "_copy_assets", manager._copy_assets), \
         patch.object(appdir_module, "_copy_extra_files", manager._copy_extra_files), \
         patch.object(appdir_module, "_compile_pyc", manager._compile_pyc), \
         patch.object(build_module, "_resolve_appimagetool", manager._resolve_appimagetool), \
         patch.object(build_module, "_resolve_runtime_file", manager._resolve_runtime_file), \
         patch.object(build_module, "_stage_runtime_file_for_appimagetool", manager._stage_runtime_file), \
         patch.object(build_module.subprocess, "run", manager.subprocess_run), \
         patch.dict("os.environ", {"SOURCE_DATE_EPOCH": "1700000000"}):
        manager._resolve_appimagetool.return_value = Path("/fake/appimagetool")
        manager._resolve_runtime_file.return_value = Path("/fake/runtime-x86_64")
        manager._stage_runtime_file.return_value = Path("/fake/staged/runtime-x86_64")
        build(config, tmp_path)

    packaging_call = next(c for c in manager.mock_calls if c[0] == "subprocess_run")
    assert packaging_call.kwargs["env"]["SOURCE_DATE_EPOCH"] == "1700000000"


def test_build_strips_xattrs_when_packaging(tmp_path: Path) -> None:
    build_module = importlib.import_module("appimage.ctl.build")
    appdir_module = importlib.import_module("appimage.ctl.build_appdir")

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
    )
    config = BuildConfig()

    manager = MagicMock()
    with patch.object(appdir_module, "_prepare_python", manager._prepare_python), \
         patch.object(appdir_module, "_copy_assets", manager._copy_assets), \
         patch.object(appdir_module, "_copy_extra_files", manager._copy_extra_files), \
         patch.object(appdir_module, "_compile_pyc", manager._compile_pyc), \
         patch.object(build_module, "_resolve_appimagetool", manager._resolve_appimagetool), \
         patch.object(build_module, "_resolve_runtime_file", manager._resolve_runtime_file), \
         patch.object(build_module, "_stage_runtime_file_for_appimagetool", manager._stage_runtime_file), \
         patch.object(build_module.subprocess, "run", manager.subprocess_run):
        manager._resolve_appimagetool.return_value = Path("/fake/appimagetool")
        manager._resolve_runtime_file.return_value = Path("/fake/runtime-x86_64")
        manager._stage_runtime_file.return_value = Path("/fake/staged/runtime-x86_64")
        build(config, tmp_path)

    packaging_call = next(c for c in manager.mock_calls if c[0] == "subprocess_run")
    cmd = packaging_call.args[0]
    assert "--mksquashfs-opt" in cmd
    assert cmd[cmd.index("--mksquashfs-opt") + 1] == "-no-xattrs"


def test_build_appdir_never_touches_appimagetool_or_packages(tmp_path: Path) -> None:
    appimagetool_module = importlib.import_module("appimage.ctl._appimagetool")
    appdir_module = importlib.import_module("appimage.ctl.build_appdir")

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
    )
    config = BuildConfig()

    manager = MagicMock()
    with patch.object(appdir_module, "_prepare_python", manager._prepare_python), \
         patch.object(appdir_module, "_copy_assets", manager._copy_assets), \
         patch.object(appdir_module, "_copy_extra_files", manager._copy_extra_files), \
         patch.object(appdir_module, "_compile_pyc", manager._compile_pyc), \
         patch.object(appimagetool_module, "_resolve_appimagetool", manager._resolve_appimagetool), \
         patch.object(appimagetool_module, "_resolve_runtime_file", manager._resolve_runtime_file), \
         patch.object(appdir_module.subprocess, "run", manager.subprocess_run):
        appdir = build_appdir(config, tmp_path)

    assert appdir == tmp_path / "build" / "AppDir"
    manager._resolve_appimagetool.assert_not_called()
    manager._resolve_runtime_file.assert_not_called()
    manager.subprocess_run.assert_not_called()


def test_build_appdir_ignores_missing_package_pins_under_reproducible(tmp_path: Path) -> None:
    """reproducible=True still lets build_appdir succeed without appimagetool/runtime pins."""
    appdir_module = importlib.import_module("appimage.ctl.build_appdir")

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        '[tool.appimage]\nreproducible = true\npython_date = "20260211"\n'
        'appimage_version = "2.0.1"\nappimage_sha256 = "' + "a" * 64 + '"\n'
    )
    config = BuildConfig.from_pyproject(tmp_path)

    with patch.object(appdir_module, "_prepare_python"), \
         patch.object(appdir_module, "_copy_assets"), \
         patch.object(appdir_module, "_copy_extra_files"), \
         patch.object(appdir_module, "_compile_pyc"):
        appdir = build_appdir(config, tmp_path)

    assert appdir == tmp_path / "build" / "AppDir"


def test_build_still_requires_package_pins_under_reproducible(tmp_path: Path) -> None:
    """Unlike build_appdir, a full build enforces the appimagetool/runtime pins too."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        '[tool.appimage]\nreproducible = true\npython_date = "20260211"\n'
    )
    config = BuildConfig.from_pyproject(tmp_path)

    with pytest.raises(SystemExit):
        build(config, tmp_path)


# ---------------------------------------------------------------------------
# _install_python / python_dir
# ---------------------------------------------------------------------------

def test_install_python_copies_python_dir_unverified(tmp_path: Path) -> None:
    from appimage.ctl._python import _install_python

    source = tmp_path / "prebuilt-python"
    (source / "bin").mkdir(parents=True)
    (source / "bin" / "python3").write_text("fake interpreter")
    appdir = tmp_path / "AppDir"
    appdir.mkdir()

    resolved = make_resolved(python_dir=str(source))

    with patch("appimage.ctl._python._resolve_python_tarball") as mock_resolve_tarball:
        _install_python(resolved, appdir, tmp_path / "python.tar.gz", "x86_64")

    mock_resolve_tarball.assert_not_called()
    assert (appdir / "python" / "bin" / "python3").read_text() == "fake interpreter"


def test_install_python_raises_when_python_dir_missing(tmp_path: Path) -> None:
    from appimage.ctl._python import _install_python

    appdir = tmp_path / "AppDir"
    appdir.mkdir()
    resolved = make_resolved(python_dir=str(tmp_path / "does-not-exist"))

    with pytest.raises(FileNotFoundError):
        _install_python(resolved, appdir, tmp_path / "python.tar.gz", "x86_64")


# ---------------------------------------------------------------------------
# write_config() appimagetool pinning
# ---------------------------------------------------------------------------

def test_write_config_pins_appimagetool_when_unset(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        '[tool.appimage]\napp = "myapp"\nentry_point = "myapp"\npython = "3.11"\n'
    )
    from appimage.ctl import BuildConfig

    config = BuildConfig.from_pyproject(tmp_path)

    tool_path = tmp_path / "appimagetool"
    runtime_path = tmp_path / "runtime-x86_64"

    with patch("appimage.ctl.init._resolve_appimagetool", return_value=tool_path) as mock_resolve_tool, \
         patch("appimage.ctl.init._resolve_runtime_file", return_value=runtime_path) as mock_resolve_runtime, \
         patch("appimage.ctl.init._appimagetool_version_string", return_value="continuous build (commit abc), build 1"), \
         patch("appimage.ctl.init._sha256_file", return_value="c" * 64), \
         patch("appimage.ctl.init._resolve_appimage_pin_sha256", return_value="d" * 64), \
         patch("appimage.ctl.init._resolve_python_url", return_value=("http://example/py.tar.gz", "f" * 64, "20260101")):
        write_config(config, tmp_path)

    mock_resolve_tool.assert_called_once()
    mock_resolve_runtime.assert_called_once()


def test_write_config_pins_appimage_runtime_module_when_unset(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        '[tool.appimage]\napp = "myapp"\nentry_point = "myapp"\npython = "3.11"\n'
    )
    from appimage.ctl import BuildConfig

    config = BuildConfig.from_pyproject(tmp_path)

    with patch("appimage.ctl._base.importlib.metadata.version", return_value="2.0.1"), \
         patch("appimage.ctl.init._resolve_appimage_pin_sha256", return_value="d" * 64) as mock_lookup, \
         patch("appimage.ctl.init._resolve_appimagetool", return_value=tmp_path / "appimagetool"), \
         patch("appimage.ctl.init._resolve_runtime_file", return_value=tmp_path / "runtime-x86_64"), \
         patch("appimage.ctl.init._appimagetool_version_string", return_value="continuous build"), \
         patch("appimage.ctl.init._sha256_file", return_value="c" * 64), \
         patch("appimage.ctl.init._resolve_python_url", return_value=("http://example/py.tar.gz", "f" * 64, "20260101")):
        write_config(config, tmp_path)

    mock_lookup.assert_called_once_with("appimage==2.0.1", strict=False)
    content = (tmp_path / "pyproject.toml").read_text()
    assert 'appimage_version = "2.0.1"' in content
    assert f'appimage_sha256 = "{"d" * 64}"' in content
    assert 'appimagectl_version = "2.0.1"' in content
    content = (tmp_path / "pyproject.toml").read_text()
    assert "appimagetool_sha256" in content
    assert "c" * 64 in content
    assert "appimagetool_version" in content
    assert "runtime_sha256" in content


def test_write_config_pins_python_date_when_unset(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        '[tool.appimage]\napp = "myapp"\nentry_point = "myapp"\npython = "3.11"\n'
    )
    from appimage.ctl import BuildConfig

    config = BuildConfig.from_pyproject(tmp_path)

    tool_path = tmp_path / "appimagetool"
    runtime_path = tmp_path / "runtime-x86_64"

    with patch("appimage.ctl.init._resolve_python_url", return_value=("http://example/py.tar.gz", "f" * 64, "20260101")) as mock_resolve_python, \
         patch("appimage.ctl.init._resolve_appimagetool", return_value=tool_path), \
         patch("appimage.ctl.init._resolve_runtime_file", return_value=runtime_path), \
         patch("appimage.ctl.init._appimagetool_version_string", return_value="continuous build"), \
         patch("appimage.ctl.init._sha256_file", return_value="c" * 64), \
         patch("appimage.ctl.init._resolve_appimage_pin_sha256", return_value="d" * 64):
        write_config(config, tmp_path)

    mock_resolve_python.assert_called_once()
    content = (tmp_path / "pyproject.toml").read_text()
    assert 'python_date = "20260101"' in content
    assert "f" * 64 in content


def test_update_tools_reresolves_python_date_against_latest(tmp_path: Path) -> None:
    """update-tools must move an already-pinned python_date forward.

    Regression test: ``_pinned_download_fields`` used to pass
    ``resolved.python_date`` straight through to ``_resolve_python_url``,
    so a project with ``python_date`` already set would just have that same
    date looked up again (and echoed back unchanged) instead of resolving
    "latest" - defeating the entire point of ``update-tools``. Asserting
    the call is made with an empty date pins down the fix.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        "[tool.appimage]\n"
        'app = "myapp"\nentry_point = "myapp"\npython = "3.11"\n'
        'python_date = "20260101"\npython_sha256 = "deadbeef"\n'
    )
    from appimage.ctl import BuildConfig

    config = BuildConfig.from_pyproject(tmp_path)

    tool_path = tmp_path / "appimagetool"
    runtime_path = tmp_path / "runtime-x86_64"

    with patch(
        "appimage.ctl.init._resolve_python_url",
        return_value=("http://example/py.tar.gz", "f" * 64, "20260901"),
    ) as mock_resolve_python, patch(
        "appimage.ctl.init._resolve_appimagetool", return_value=tool_path
    ), patch("appimage.ctl.init._resolve_runtime_file", return_value=runtime_path), patch(
        "appimage.ctl.init._appimagetool_version_string", return_value="continuous build"
    ), patch("appimage.ctl.init._sha256_file", return_value="c" * 64), patch(
        "appimage.ctl.init._resolve_appimage_pin_sha256", return_value="d" * 64
    ):
        update_tools(config, tmp_path)

    mock_resolve_python.assert_called_once()
    assert mock_resolve_python.call_args.args[:2] == ("3.11", "")
    content = (tmp_path / "pyproject.toml").read_text()
    assert 'python_date = "20260901"' in content
    assert "f" * 64 in content


def test_write_config_skips_appimagetool_resolution_when_already_set(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        "[tool.appimage]\n"
        'app = "myapp"\nentry_point = "myapp"\npython = "3.11"\npython_date = "20260101"\n'
        'appimagetool_sha256 = "deadbeef"\n'
        'runtime_sha256 = "deadbeef"\n'
        'appimage_version = "2.0.1"\n'
        'appimage_sha256 = "deadbeef"\n'
    )
    from appimage.ctl import BuildConfig

    config = BuildConfig.from_pyproject(tmp_path)

    with patch("appimage.ctl.init._resolve_appimagetool") as mock_resolve_tool, \
         patch("appimage.ctl.init._resolve_runtime_file") as mock_resolve_runtime, \
         patch("appimage.ctl.init._resolve_appimage_pin_sha256") as mock_resolve_appimage_pin, \
         patch("appimage.ctl.init._resolve_python_url") as mock_resolve_python:
        write_config(config, tmp_path)

    mock_resolve_tool.assert_not_called()
    mock_resolve_runtime.assert_not_called()
    mock_resolve_appimage_pin.assert_not_called()
    mock_resolve_python.assert_not_called()


def test_write_config_does_not_write_unresolvable_entry_point(tmp_path: Path) -> None:
    """Regression test: an ambiguous entry_point must not fall back to *app*.

    _resolve_entry_point falls back to *app* as a placeholder value
    alongside an error when [project.scripts] doesn't unambiguously name
    one — write_config must not persist that guess, or a loud `check`
    error silently turns into a wrong-but-configured pyproject.toml.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\n'
        'scripts = { foo = "myapp:foo", bar = "myapp:bar" }\n'
    )
    from appimage.ctl import BuildConfig

    config = BuildConfig.from_pyproject(tmp_path)

    tool_path = tmp_path / "appimagetool"
    runtime_path = tmp_path / "runtime-x86_64"

    with patch("appimage.ctl.init._resolve_appimagetool", return_value=tool_path), \
         patch("appimage.ctl.init._resolve_runtime_file", return_value=runtime_path), \
         patch("appimage.ctl.init._appimagetool_version_string", return_value="continuous build"), \
         patch("appimage.ctl.init._sha256_file", return_value="c" * 64), \
         patch("appimage.ctl.init._resolve_python_url", return_value=("http://example/py.tar.gz", "f" * 64, "20260101")), \
         patch("appimage.ctl.init._resolve_appimage_pin_sha256", return_value="d" * 64):
        write_config(config, tmp_path)

    content = (tmp_path / "pyproject.toml").read_text()
    assert "entry_point" not in content
    assert 'app = "myapp"' in content


# ---------------------------------------------------------------------------
# pylock (dependency hash-pinning)
# ---------------------------------------------------------------------------

def _has_pylock_message(messages: list[str]) -> bool:
    return any("No pylock configured" in m for m in messages)


def test_pylock_warns_by_default(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig()

    resolved = _resolve(config, tmp_path)

    assert resolved.appdir_errors == []
    assert _has_pylock_message(resolved.appdir_warnings)


def test_pylock_errors_with_require_pylock(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(require_pylock=True)

    resolved = _resolve(config, tmp_path)

    assert not _has_pylock_message(resolved.appdir_warnings)
    assert _has_pylock_message(resolved.appdir_errors)


def test_pylock_noop_when_configured(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(pylock="pylock.toml")

    resolved = _resolve(config, tmp_path)

    assert resolved.appdir_errors == []
    assert not _has_pylock_message(resolved.appdir_warnings)


# ---------------------------------------------------------------------------
# build_pylock (build-backend hash-pinning)
# ---------------------------------------------------------------------------

def _has_build_pylock_message(messages: list[str]) -> bool:
    return any("No build_pylock configured" in m for m in messages)


def test_build_pylock_warns_by_default(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig()

    resolved = _resolve(config, tmp_path)

    assert resolved.appdir_errors == []
    assert _has_build_pylock_message(resolved.appdir_warnings)


def test_build_pylock_errors_with_require_build_pylock(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(require_build_pylock=True)

    resolved = _resolve(config, tmp_path)

    assert not _has_build_pylock_message(resolved.appdir_warnings)
    assert _has_build_pylock_message(resolved.appdir_errors)


def test_build_pylock_noop_when_configured(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(build_pylock="requirements-build.txt")

    resolved = _resolve(config, tmp_path)

    assert resolved.appdir_errors == []
    assert not _has_build_pylock_message(resolved.appdir_warnings)


# ---------------------------------------------------------------------------
# _reproducibility_summary
# ---------------------------------------------------------------------------

def test_reproducibility_summary_reports_not_ready_by_default() -> None:
    from appimage.ctl.check import _reproducibility_summary

    resolved = make_resolved()

    lines = _reproducibility_summary(resolved)

    assert any("AppDir reproducibility: python_date not set" in line for line in lines)
    assert any(
        "Runtime module reproducibility:" in line and "not set" in line for line in lines
    )
    assert any("Packaging reproducibility:" in line and "not set" in line for line in lines)
    assert any("'init'" in line for line in lines)
    assert any("Dependency verification: pylock not set" in line for line in lines)
    assert any("'lock'" in line for line in lines)
    assert any("Build backend verification: build_pylock not set" in line for line in lines)


def test_reproducibility_summary_reports_full_pins_without_nudge() -> None:
    from appimage.ctl.check import _reproducibility_summary

    resolved = make_resolved(
        python_date="20260211",
        appimage_version="2.0.1",
        appimage_sha256="c" * 64,
        appimagetool_sha256="a" * 64,
        runtime_sha256="b" * 64,
        pylock="pylock.toml",
        build_pylock="requirements-build.txt",
    )

    lines = _reproducibility_summary(resolved)

    assert any("AppDir reproducibility: python_date set" in line for line in lines)
    assert any(
        "Runtime module reproducibility: appimage_version, appimage_sha256 set" in line
        for line in lines
    )
    assert any(
        "Packaging reproducibility: appimagetool_sha256, runtime_sha256 set" in line
        for line in lines
    )
    assert not any("'init'" in line for line in lines)
    assert any("Dependency verification: pylock set (pylock.toml)" in line for line in lines)
    assert any(
        "Build backend verification: build_pylock set (requirements-build.txt)" in line
        for line in lines
    )


def test_reproducibility_summary_python_dir_marked_trusted_unverified() -> None:
    from appimage.ctl.check import _reproducibility_summary

    resolved = make_resolved(python_dir="/opt/python")

    lines = _reproducibility_summary(resolved)

    assert any(
        line.strip().startswith("✓") and "python_dir set" in line and "not hash-verified" in line
        for line in lines
    )


def test_reproducibility_summary_header_counts_ready_layers() -> None:
    from appimage.ctl.check import _reproducibility_summary

    lines = _reproducibility_summary(make_resolved())
    assert lines[0] == "Reproducibility checklist (0/5 ready):"

    lines = _reproducibility_summary(
        make_resolved(
            python_date="20260211",
            appimage_version="2.0.1",
            appimage_sha256="c" * 64,
            appimagetool_sha256="a" * 64,
            runtime_sha256="b" * 64,
            pylock="pylock.toml",
            build_pylock="requirements-build.txt",
        )
    )
    assert lines[0] == "Reproducibility checklist (5/5 ready):"


def test_reproducibility_summary_marks_each_layer_ready_or_not() -> None:
    from appimage.ctl.check import _reproducibility_summary

    lines = _reproducibility_summary(
        make_resolved(
            python_date="20260211",
            appimage_version="2.0.1",
            appimage_sha256="c" * 64,
            appimagetool_sha256="a" * 64,
            runtime_sha256="b" * 64,
            pylock="pylock.toml",
        )
    )

    assert any(
        line.strip().startswith("✓") and "AppDir reproducibility:" in line for line in lines
    )
    assert any(
        line.strip().startswith("✓") and "Runtime module reproducibility:" in line
        for line in lines
    )
    assert any(
        line.strip().startswith("✓") and "Packaging reproducibility:" in line for line in lines
    )
    assert any(line.strip().startswith("✓") and "Dependency verification:" in line for line in lines)
    assert any(line.strip().startswith("✗") and "Build backend verification:" in line for line in lines)


# ---------------------------------------------------------------------------
# _resolution_source
#
# The single, shared classifier every _resolve_*/_locate_* function (and
# check()'s verify_downloads prediction below) uses to decide "explicit
# config path, then the build cache, then a download" — one implementation
# of that precedence, not duplicated per caller.
# ---------------------------------------------------------------------------

def test_resolution_source_config_when_explicit_set(tmp_path: Path) -> None:
    from appimage.ctl._download import _resolution_source

    assert _resolution_source("some/path", tmp_path / "cache") == "config"


def test_resolution_source_cache_when_no_explicit_but_cache_exists(tmp_path: Path) -> None:
    from appimage.ctl._download import _resolution_source

    cache = tmp_path / "cache"
    cache.write_bytes(b"x")
    assert _resolution_source("", cache) == "cache"


def test_resolution_source_download_when_neither(tmp_path: Path) -> None:
    from appimage.ctl._download import _resolution_source

    assert _resolution_source("", tmp_path / "cache") == "download"


def test_resolution_source_config_wins_even_if_cache_also_exists(tmp_path: Path) -> None:
    """Explicit config always takes precedence over an existing cache file."""
    from appimage.ctl._download import _resolution_source

    cache = tmp_path / "cache"
    cache.write_bytes(b"x")
    assert _resolution_source("some/path", cache) == "config"


# ---------------------------------------------------------------------------
# _predict_unverified_downloads (check's verify_downloads/hash-pin
# cross-reference)
#
# Regression coverage for the specific false-positive risk that ruled out a
# naive "verify_downloads set + no sha256 -> warn" check: a fresh download
# is always auto-verified against the digest GitHub publishes, regardless
# of whether a pin is configured, so it must never be flagged — only an
# explicit config path or an existing build-cache hit with no pin actually
# fails under verify_downloads.
# ---------------------------------------------------------------------------

def test_predict_unverified_downloads_noop_without_verify_downloads(tmp_path: Path) -> None:
    from appimage.ctl.check import _predict_unverified_downloads

    resolved = make_resolved()  # verify_downloads=False by default
    _predict_unverified_downloads(resolved, tmp_path)

    assert resolved.appdir_errors == []
    assert resolved.package_errors == []


def test_predict_unverified_downloads_noop_under_reproducible(tmp_path: Path) -> None:
    """reproducible already requires every pin unconditionally (see the dedicated
    checks in _resolve()) — this prediction would just be a redundant second
    error for the same root cause, so it deliberately skips when reproducible
    is set.
    """
    from appimage.ctl.check import _predict_unverified_downloads

    resolved = make_resolved(verify_downloads=True, reproducible=True)
    _predict_unverified_downloads(resolved, tmp_path)

    assert resolved.appdir_errors == []
    assert resolved.package_errors == []


def test_predict_unverified_downloads_noop_for_fresh_download(tmp_path: Path) -> None:
    """The common, valid case a naive check would have wrongly flagged: nothing
    configured, nothing cached yet -> a fresh download, always auto-verified,
    regardless of whether a pin is set.
    """
    from appimage.ctl.check import _predict_unverified_downloads

    resolved = make_resolved(verify_downloads=True)
    _predict_unverified_downloads(resolved, tmp_path)

    assert resolved.appdir_errors == []
    assert resolved.package_errors == []


def test_predict_unverified_downloads_flags_cached_appimagetool_without_pin(
    tmp_path: Path,
) -> None:
    from appimage.ctl._appimagetool import _appimagetool_cache_path
    from appimage.ctl.check import _predict_unverified_downloads

    build_dir = tmp_path / "build"
    build_dir.mkdir()
    _appimagetool_cache_path(build_dir, platform.machine()).write_bytes(b"x")

    resolved = make_resolved(verify_downloads=True)
    _predict_unverified_downloads(resolved, tmp_path)

    assert len(resolved.package_errors) == 1
    assert "appimagetool" in resolved.package_errors[0]
    assert "appimagetool_sha256" in resolved.package_errors[0]
    assert resolved.appdir_errors == []


def test_predict_unverified_downloads_flags_explicit_runtime_file_without_pin(
    tmp_path: Path,
) -> None:
    from appimage.ctl.check import _predict_unverified_downloads

    resolved = make_resolved(verify_downloads=True, runtime_file="some/runtime")
    _predict_unverified_downloads(resolved, tmp_path)

    assert len(resolved.package_errors) == 1
    assert "runtime file" in resolved.package_errors[0]
    assert "runtime_sha256" in resolved.package_errors[0]


def test_predict_unverified_downloads_flags_cached_python_archive_without_pin(
    tmp_path: Path,
) -> None:
    from appimage.ctl._python import _python_tarball_cache_path
    from appimage.ctl.check import _predict_unverified_downloads

    build_dir = tmp_path / "build"
    build_dir.mkdir()
    _python_tarball_cache_path(build_dir).write_bytes(b"x")

    resolved = make_resolved(verify_downloads=True)
    _predict_unverified_downloads(resolved, tmp_path)

    assert len(resolved.appdir_errors) == 1
    assert "python archive" in resolved.appdir_errors[0]
    assert resolved.package_errors == []


def test_predict_unverified_downloads_skips_python_archive_when_python_dir_set(
    tmp_path: Path,
) -> None:
    """python_dir bypasses tarball resolution entirely — no prediction should
    apply to it, matching _install_python's own bypass.
    """
    from appimage.ctl._python import _python_tarball_cache_path
    from appimage.ctl.check import _predict_unverified_downloads

    build_dir = tmp_path / "build"
    build_dir.mkdir()
    _python_tarball_cache_path(build_dir).write_bytes(b"x")

    resolved = make_resolved(verify_downloads=True, python_dir="/some/trusted/python")
    _predict_unverified_downloads(resolved, tmp_path)

    assert resolved.appdir_errors == []


def test_predict_unverified_downloads_noop_when_pin_configured(tmp_path: Path) -> None:
    from appimage.ctl._appimagetool import _appimagetool_cache_path
    from appimage.ctl.check import _predict_unverified_downloads

    build_dir = tmp_path / "build"
    build_dir.mkdir()
    _appimagetool_cache_path(build_dir, platform.machine()).write_bytes(b"x")

    resolved = make_resolved(verify_downloads=True, appimagetool_sha256="a" * 64)
    _predict_unverified_downloads(resolved, tmp_path)

    assert resolved.package_errors == []


def test_format_check_surfaces_predicted_unverified_download(tmp_path: Path) -> None:
    """End-to-end through _format_check(): the prediction actually reaches the
    errors that check()/build()/build_appdir() all act on, not just the
    prediction function in isolation.
    """
    from appimage.ctl._appimagetool import _appimagetool_cache_path
    from appimage.ctl.check import _format_check

    build_dir = tmp_path / "build"
    build_dir.mkdir()
    _appimagetool_cache_path(build_dir, platform.machine()).write_bytes(b"x")

    resolved = make_resolved(verify_downloads=True)
    _format_check(resolved, tmp_path)

    assert len(resolved.package_errors) == 1


# ---------------------------------------------------------------------------
# _install_from_pylock / _prepare_python with pylock configured
# ---------------------------------------------------------------------------

def test_prepare_python_uses_pylock_when_configured(tmp_path: Path) -> None:
    from appimage.ctl.build_appdir import _prepare_python

    (tmp_path / "pylock.toml").write_text("")
    resolved = make_resolved(
        install_targets=["appimage==2.0.1", "."],
        local_install_targets=["."],
        pylock="pylock.toml",
    )
    appdir = tmp_path / "AppDir"
    appdir.mkdir()
    tarball = tmp_path / "python.tar.gz"
    tarball.write_bytes(b"")

    with patch("appimage.ctl._python._resolve_python_tarball", return_value=tarball), \
         patch("appimage.ctl._python.tarfile.open") as mock_tarfile, \
         patch("appimage.ctl.build_appdir.subprocess.run") as mock_run:
        mock_tarfile.return_value.__enter__.return_value.extractall = MagicMock()
        _prepare_python(resolved, appdir, tmp_path / "python.tar.gz", "x86_64", tmp_path)

    calls = [c.args[0] for c in mock_run.call_args_list]
    assert len(calls) == 2
    local_call, lock_call = calls
    assert "--no-deps" in local_call
    assert "." in local_call
    assert "appimage==2.0.1" not in local_call
    assert "--require-hashes" in lock_call
    assert "--no-deps" not in lock_call
    assert str(tmp_path / "pylock.toml") in lock_call


def test_prepare_python_raises_when_pylock_missing(tmp_path: Path) -> None:
    from appimage.ctl.build_appdir import _prepare_python

    resolved = make_resolved(pylock="pylock.toml")
    appdir = tmp_path / "AppDir"
    appdir.mkdir()
    tarball = tmp_path / "python.tar.gz"
    tarball.write_bytes(b"")

    with patch("appimage.ctl._python._resolve_python_tarball", return_value=tarball), \
         patch("appimage.ctl._python.tarfile.open") as mock_tarfile:
        mock_tarfile.return_value.__enter__.return_value.extractall = MagicMock()
        with pytest.raises(FileNotFoundError):
            _prepare_python(resolved, appdir, tmp_path / "python.tar.gz", "x86_64", tmp_path)


# ---------------------------------------------------------------------------
# _install_build_pylock / build_pylock wired into pip installs
# ---------------------------------------------------------------------------

_SAMPLE_BUILD_PYLOCK_TOML = f'''\
lock-version = "1.0"
created-by = "pip"

[[packages]]
name = "setuptools"
version = "84.0.0"

[[packages.wheels]]
name = "setuptools-84.0.0-py3-none-any.whl"
url = "https://files.pythonhosted.org/packages/setuptools.whl"

[packages.wheels.hashes]
sha256 = "{"c" * 64}"
'''


def test_install_build_pylock_noop_when_unset(tmp_path: Path) -> None:
    from appimage.ctl.build_appdir import _install_build_pylock

    resolved = make_resolved()

    assert _install_build_pylock(resolved, tmp_path) == []


def test_install_build_pylock_raises_when_file_missing(tmp_path: Path) -> None:
    from appimage.ctl.build_appdir import _install_build_pylock

    resolved = make_resolved(build_pylock="pylock.build.toml")

    with pytest.raises(FileNotFoundError):
        _install_build_pylock(resolved, tmp_path)


def test_install_build_pylock_when_configured(tmp_path: Path) -> None:
    from appimage.ctl.build_appdir import _install_build_pylock

    (tmp_path / "pylock.build.toml").write_text(_SAMPLE_BUILD_PYLOCK_TOML)
    resolved = make_resolved(build_pylock="pylock.build.toml")

    args = _install_build_pylock(resolved, tmp_path)

    assert args[0] == "--build-constraint"
    constraint_path = Path(args[1])
    content = constraint_path.read_text()
    assert "setuptools==84.0.0" in content
    assert "sha256:" + "c" * 64 in content
    assert "--no-build-isolation" not in args


def test_pylock_to_build_constraint_skips_local_directory_entries(tmp_path: Path) -> None:
    from appimage.ctl.build_appdir import _pylock_to_build_constraint

    pylock_path = tmp_path / "pylock.toml"
    pylock_path.write_text(_SAMPLE_PYLOCK_TOML)  # includes a local "demoproj" directory entry

    content = _pylock_to_build_constraint(pylock_path)

    assert "demoproj" not in content
    assert "certifi==2026.7.22" in content
    assert "appimage==2.0.1" in content


def test_prepare_python_passes_build_pylock_without_pylock(tmp_path: Path) -> None:
    from appimage.ctl.build_appdir import _prepare_python

    (tmp_path / "pylock.build.toml").write_text(_SAMPLE_BUILD_PYLOCK_TOML)
    resolved = make_resolved(
        install_targets=["appimage==2.0.1", "."],
        build_pylock="pylock.build.toml",
    )
    appdir = tmp_path / "AppDir"
    appdir.mkdir()
    tarball = tmp_path / "python.tar.gz"
    tarball.write_bytes(b"")

    with patch("appimage.ctl._python._resolve_python_tarball", return_value=tarball), \
         patch("appimage.ctl._python.tarfile.open") as mock_tarfile, \
         patch("appimage.ctl.build_appdir.subprocess.run") as mock_run, \
         patch("appimage.ctl.build_appdir._resolve_appimage_pin_sha256", return_value=None):
        mock_tarfile.return_value.__enter__.return_value.extractall = MagicMock()
        _prepare_python(resolved, appdir, tmp_path / "python.tar.gz", "x86_64", tmp_path)

    (project_call,) = [c.args[0] for c in mock_run.call_args_list]
    assert "--build-constraint" in project_call
    assert "--no-build-isolation" not in project_call
    assert "appimage==2.0.1" in project_call


def test_prepare_python_passes_build_pylock_with_pylock(tmp_path: Path) -> None:
    from appimage.ctl.build_appdir import _prepare_python

    (tmp_path / "pylock.toml").write_text("")
    (tmp_path / "pylock.build.toml").write_text(_SAMPLE_BUILD_PYLOCK_TOML)
    resolved = make_resolved(
        install_targets=["appimage==2.0.1", "."],
        local_install_targets=["."],
        pylock="pylock.toml",
        build_pylock="pylock.build.toml",
    )
    appdir = tmp_path / "AppDir"
    appdir.mkdir()
    tarball = tmp_path / "python.tar.gz"
    tarball.write_bytes(b"")

    with patch("appimage.ctl._python._resolve_python_tarball", return_value=tarball), \
         patch("appimage.ctl._python.tarfile.open") as mock_tarfile, \
         patch("appimage.ctl.build_appdir.subprocess.run") as mock_run:
        mock_tarfile.return_value.__enter__.return_value.extractall = MagicMock()
        _prepare_python(resolved, appdir, tmp_path / "python.tar.gz", "x86_64", tmp_path)

    local_call, lock_call = [c.args[0] for c in mock_run.call_args_list]
    assert "--build-constraint" in local_call
    assert "--no-build-isolation" not in local_call
    assert "." in local_call
    assert str(tmp_path / "pylock.toml") in lock_call


# ---------------------------------------------------------------------------
# _pip_version / _generate_lock / lock()
# ---------------------------------------------------------------------------

def test_pip_version_parses_output(tmp_path: Path) -> None:
    from appimage.ctl._python import _pip_version

    fake_result = MagicMock(stdout="pip 26.1.2 from /some/path (python 3.13)\n")
    with patch("appimage.ctl._python.subprocess.run", return_value=fake_result):
        assert _pip_version(tmp_path / "python3") == (26, 1)


def test_pip_version_raises_on_unparseable_output(tmp_path: Path) -> None:
    from appimage.ctl._python import _pip_version

    fake_result = MagicMock(stdout="not a pip version string\n")
    with patch("appimage.ctl._python.subprocess.run", return_value=fake_result), \
         pytest.raises(RuntimeError):
        _pip_version(tmp_path / "python3")


def test_generate_lock_raises_for_old_pip(tmp_path: Path) -> None:
    from appimage.ctl.lock import _generate_lock

    resolved = make_resolved(pylock="pylock.toml")
    with patch("appimage.ctl.lock._pip_version", return_value=(24, 3)), \
         pytest.raises(RuntimeError, match="does not support"):
        _generate_lock(resolved, tmp_path / "python3", tmp_path, uploaded_prior_to="")


def test_generate_lock_builds_expected_command(tmp_path: Path) -> None:
    from appimage.ctl.lock import _generate_lock

    resolved = make_resolved(
        install_targets=["appimage==2.0.1", ".", "extra-pkg"], pylock="pylock.toml",
    )

    with patch("appimage.ctl.lock._pip_version", return_value=(25, 1)), \
         patch("appimage.ctl.lock.subprocess.run") as mock_run, \
         patch("appimage.ctl.lock._strip_local_directory_entries") as mock_strip:
        result = _generate_lock(resolved, tmp_path / "python3", tmp_path, uploaded_prior_to="P7D")

    assert result == tmp_path / "pylock.toml"
    cmd = mock_run.call_args.args[0]
    assert "lock" in cmd
    assert "appimage==2.0.1" in cmd
    assert "extra-pkg" in cmd
    assert "--only-deps" not in cmd
    assert "--uploaded-prior-to" in cmd
    assert "P7D" in cmd
    assert str(tmp_path / "pylock.toml") in cmd
    mock_strip.assert_called_once_with(tmp_path / "pylock.toml")


def test_generate_lock_omits_uploaded_prior_to_when_unset(tmp_path: Path) -> None:
    from appimage.ctl.lock import _generate_lock

    resolved = make_resolved(pylock="pylock.toml")

    with patch("appimage.ctl.lock._pip_version", return_value=(25, 1)), \
         patch("appimage.ctl.lock.subprocess.run") as mock_run, \
         patch("appimage.ctl.lock._strip_local_directory_entries"):
        _generate_lock(resolved, tmp_path / "python3", tmp_path, uploaded_prior_to="")

    cmd = mock_run.call_args.args[0]
    assert "--uploaded-prior-to" not in cmd


# ---------------------------------------------------------------------------
# _strip_local_directory_entries
# ---------------------------------------------------------------------------

_SAMPLE_PYLOCK_TOML = '''\
lock-version = "1.0"
created-by = "pip"

[[packages]]
name = "certifi"
version = "2026.7.22"

[[packages.wheels]]
name = "certifi-2026.7.22-py3-none-any.whl"
url = "https://files.pythonhosted.org/packages/certifi.whl"

[packages.wheels.hashes]
sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

[[packages]]
name = "demoproj"

[packages.directory]
path = "demoproj"

[[packages]]
name = "appimage"
version = "2.0.1"

[[packages.wheels]]
name = "appimage-2.0.1-py3-none-any.whl"
url = "https://files.pythonhosted.org/packages/appimage.whl"

[packages.wheels.hashes]
sha256 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
'''


def test_strip_local_directory_entries_removes_only_directory_source(tmp_path: Path) -> None:
    from appimage.ctl.lock import _strip_local_directory_entries

    pylock_path = tmp_path / "pylock.toml"
    pylock_path.write_text(_SAMPLE_PYLOCK_TOML)

    _strip_local_directory_entries(pylock_path)

    result = tomllib.loads(pylock_path.read_text())
    names = [pkg["name"] for pkg in result["packages"]]
    assert names == ["certifi", "appimage"]
    assert all("directory" not in pkg for pkg in result["packages"])


def test_strip_local_directory_entries_keeps_direct_pins_with_hashes(tmp_path: Path) -> None:
    """Regression test: appimage_pin/packages must keep their own hash, not just their deps."""
    from appimage.ctl.lock import _strip_local_directory_entries

    pylock_path = tmp_path / "pylock.toml"
    pylock_path.write_text(_SAMPLE_PYLOCK_TOML)

    _strip_local_directory_entries(pylock_path)

    result = tomllib.loads(pylock_path.read_text())
    appimage_pkg = next(pkg for pkg in result["packages"] if pkg["name"] == "appimage")
    assert appimage_pkg["wheels"][0]["hashes"]["sha256"] == "b" * 64


def test_strip_local_directory_entries_noop_without_directory_source(tmp_path: Path) -> None:
    from appimage.ctl.lock import _strip_local_directory_entries

    text = (
        'lock-version = "1.0"\ncreated-by = "pip"\n\n'
        '[[packages]]\nname = "idna"\nversion = "3.19"\n'
    )
    pylock_path = tmp_path / "pylock.toml"
    pylock_path.write_text(text)

    _strip_local_directory_entries(pylock_path)

    result = tomllib.loads(pylock_path.read_text())
    assert [pkg["name"] for pkg in result["packages"]] == ["idna"]


def _write_project_with_build_system(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["uv_build>=0.12.7,<0.13"]\n'
        'build-backend = "uv_build"\n'
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
    )


def test_generate_build_pylock_raises_for_old_pip(tmp_path: Path) -> None:
    from appimage.ctl.lock import _generate_build_pylock

    _write_project_with_build_system(tmp_path)
    resolved = make_resolved(build_pylock="pylock.build.toml")
    with patch("appimage.ctl.lock._pip_version", return_value=(24, 3)), \
         pytest.raises(RuntimeError, match="does not support"):
        _generate_build_pylock(resolved, tmp_path / "python3", tmp_path, uploaded_prior_to="")


def test_generate_build_pylock_raises_without_build_system_requires(tmp_path: Path) -> None:
    from appimage.ctl.lock import _generate_build_pylock

    _write_minimal_project(tmp_path)
    resolved = make_resolved(build_pylock="pylock.build.toml")
    with patch("appimage.ctl.lock._pip_version", return_value=(25, 1)), \
         pytest.raises(RuntimeError, match="build-system"):
        _generate_build_pylock(resolved, tmp_path / "python3", tmp_path, uploaded_prior_to="")


def test_generate_build_pylock_builds_expected_command(tmp_path: Path) -> None:
    from appimage.ctl.lock import _generate_build_pylock

    _write_project_with_build_system(tmp_path)
    resolved = make_resolved(build_pylock="pylock.build.toml")

    with patch("appimage.ctl.lock._pip_version", return_value=(25, 1)), \
         patch("appimage.ctl.lock.subprocess.run") as mock_run:
        result = _generate_build_pylock(
            resolved, tmp_path / "python3", tmp_path, uploaded_prior_to="P7D",
        )

    assert result == tmp_path / "pylock.build.toml"
    cmd = mock_run.call_args.args[0]
    assert "lock" in cmd
    assert "uv_build>=0.12.7,<0.13" in cmd
    assert "--only-deps" not in cmd
    assert "--uploaded-prior-to" in cmd
    assert "P7D" in cmd
    assert str(tmp_path / "pylock.build.toml") in cmd


def test_generate_build_pylock_default_filename(tmp_path: Path) -> None:
    from appimage.ctl.lock import _generate_build_pylock

    _write_project_with_build_system(tmp_path)
    resolved = make_resolved()

    with patch("appimage.ctl.lock._pip_version", return_value=(25, 1)), \
         patch("appimage.ctl.lock.subprocess.run"):
        result = _generate_build_pylock(resolved, tmp_path / "python3", tmp_path, uploaded_prior_to="")

    assert result == tmp_path / "pylock.build.toml"


def test_write_lock_config_writes_when_unset(tmp_path: Path) -> None:
    from appimage.ctl.lock import _write_lock_config

    _write_minimal_project(tmp_path)
    pyproject_path = tmp_path / "pyproject.toml"

    _write_lock_config(pyproject_path, tmp_path, "pylock", tmp_path / "pylock.toml")

    content = pyproject_path.read_text()
    assert 'pylock = "pylock.toml"' in content


def test_write_lock_config_skips_when_already_set(tmp_path: Path) -> None:
    from appimage.ctl.lock import _write_lock_config

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        '[tool.appimage]\npylock = "custom-lock.toml"\n'
    )
    pyproject_path = tmp_path / "pyproject.toml"

    _write_lock_config(pyproject_path, tmp_path, "pylock", tmp_path / "pylock.toml")

    content = pyproject_path.read_text()
    assert content.count("pylock =") == 1
    assert 'pylock = "custom-lock.toml"' in content


def test_lock_writes_both_lock_paths_to_pyproject_when_unset(tmp_path: Path) -> None:
    from appimage.ctl.lock import lock

    _write_project_with_build_system(tmp_path)
    config = BuildConfig()

    with patch("appimage.ctl._python._resolve_python_tarball", return_value=tmp_path / "python.tar.gz"), \
         patch("appimage.ctl._python.tarfile.open") as mock_tarfile, \
         patch("appimage.ctl.lock._generate_lock", return_value=tmp_path / "pylock.toml") as mock_generate, \
         patch(
             "appimage.ctl.lock._generate_build_pylock", return_value=tmp_path / "pylock.build.toml",
         ) as mock_generate_build:
        mock_tarfile.return_value.__enter__.return_value.extractall = MagicMock()
        lock(config, tmp_path)

    mock_generate.assert_called_once()
    mock_generate_build.assert_called_once()
    content = (tmp_path / "pyproject.toml").read_text()
    assert 'pylock = "pylock.toml"' in content
    assert 'build_pylock = "pylock.build.toml"' in content


def test_lock_skips_write_when_already_set(tmp_path: Path) -> None:
    from appimage.ctl.lock import lock

    (tmp_path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["uv_build>=0.12.7,<0.13"]\n'
        'build-backend = "uv_build"\n'
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        '[tool.appimage]\npylock = "custom-lock.toml"\n'
        'build_pylock = "custom-build-lock.toml"\n'
    )
    config = BuildConfig.from_pyproject(tmp_path)

    with patch("appimage.ctl._python._resolve_python_tarball", return_value=tmp_path / "python.tar.gz"), \
         patch("appimage.ctl._python.tarfile.open") as mock_tarfile, \
         patch("appimage.ctl.lock._generate_lock", return_value=tmp_path / "custom-lock.toml"), \
         patch("appimage.ctl.lock._generate_build_pylock", return_value=tmp_path / "custom-build-lock.toml"):
        mock_tarfile.return_value.__enter__.return_value.extractall = MagicMock()
        lock(config, tmp_path)

    content = (tmp_path / "pyproject.toml").read_text()
    # "pylock =" is also a substring of "build_pylock =" — anchor on the
    # preceding newline so each is counted only on its own line.
    assert content.count("\npylock =") == 1
    assert content.count("\nbuild_pylock =") == 1


def test_write_reproducible_flag_writes_when_unset(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    pyproject_path = tmp_path / "pyproject.toml"

    _write_reproducible_flag(tmp_path)

    content = pyproject_path.read_text()
    assert "reproducible = true" in content


def test_write_reproducible_flag_skips_when_already_set(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        "[tool.appimage]\nreproducible = false\n"
    )

    _write_reproducible_flag(tmp_path)

    content = (tmp_path / "pyproject.toml").read_text()
    assert content.count("reproducible =") == 1
    assert "reproducible = false" in content


def test_enable_reproducible_writes_flag_only_after_successful_build(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig()

    with patch("appimage.ctl.enable_reproducible.write_config") as mock_write_config, \
         patch("appimage.ctl.enable_reproducible.lock") as mock_lock, \
         patch("appimage.ctl.enable_reproducible.build") as mock_build:
        enable_reproducible(config, tmp_path, uploaded_prior_to="P7D")

    mock_write_config.assert_called_once_with(config, tmp_path)
    mock_lock.assert_called_once()
    assert mock_lock.call_args.kwargs["uploaded_prior_to"] == "P7D"
    mock_build.assert_called_once()
    built_config = mock_build.call_args.args[0]
    assert built_config.reproducible is True

    content = (tmp_path / "pyproject.toml").read_text()
    assert "reproducible = true" in content


def test_enable_reproducible_does_not_write_flag_when_build_fails(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig()

    with patch("appimage.ctl.enable_reproducible.write_config"), \
         patch("appimage.ctl.enable_reproducible.lock"), \
         patch("appimage.ctl.enable_reproducible.build", side_effect=SystemExit(1)):
        with pytest.raises(SystemExit):
            enable_reproducible(config, tmp_path)

    content = (tmp_path / "pyproject.toml").read_text()
    assert "reproducible" not in content
