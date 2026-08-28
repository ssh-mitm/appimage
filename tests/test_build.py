"""Unit tests for appimage.build reproducibility features.

All network and subprocess calls are mocked — these tests never touch the
network or execute real binaries.
"""

import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from appimage.build import (
    BuildConfig,
    _ResolvedBuild,
    _normalize_mtimes,
    _resolve,
    _resolve_appimagetool,
    _resolve_python_tarball,
    _resolve_python_url,
    _resolve_runtime_file,
    _sha256_file,
    _verify_sha256,
    build,
    write_config,
)


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
        "python": "3.11",
        "python_date": "",
        "icon": None,
        "desktop": None,
        "apprun": "",
        "build_dir": "build",
        "dist_dir": "dist",
        "update_info": "",
        "env": {},
        "extra_files": {},
        "hooks": {},
        "appimagetool": "",
        "appimagetool_version": "",
        "appimagetool_sha256": "",
        "python_archive": "",
        "python_sha256": "",
        "runtime_file": "",
        "runtime_sha256": "",
        "verify_downloads": False,
        "require_zsyncmake": False,
        "pylock": "",
        "require_pylock": False,
        "reproducible": False,
        "sources": {},
        "warnings": [],
        "errors": [],
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
    with patch("appimage.build.urllib.request.urlopen", return_value=_fake_release_response("sha256:" + "a" * 64)):
        url, sha256 = _resolve_python_url("3.11", "20260211", "x86_64")
    assert url.endswith("install_only_stripped.tar.gz")
    assert sha256 == "a" * 64


def test_resolve_python_url_returns_none_without_digest() -> None:
    with patch("appimage.build.urllib.request.urlopen", return_value=_fake_release_response(None)):
        _url, sha256 = _resolve_python_url("3.11", "20260211", "x86_64")
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


def test_resolve_appimagetool_from_path_no_hash_warns_and_skips_download(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    tool_on_path = tmp_path / "appimagetool"
    tool_on_path.write_bytes(b"whatever-was-on-path")
    resolved = make_resolved()

    with patch("appimage.build.shutil.which", return_value=str(tool_on_path)), \
         patch("appimage.build._download") as mock_download, \
         caplog.at_level("WARNING"):
        result = _resolve_appimagetool(resolved, tmp_path / "cache.AppImage", "x86_64")

    assert result == tool_on_path
    mock_download.assert_not_called()
    assert "unpinned and unverified" in caplog.text


def test_resolve_appimagetool_from_path_hash_set_mismatch_raises(tmp_path: Path) -> None:
    """A stale/wrong binary sitting on PATH must be caught once a hash is pinned."""
    tool_on_path = tmp_path / "appimagetool"
    tool_on_path.write_bytes(b"a-random-binary-from-2019")
    resolved = make_resolved(appimagetool_sha256=digest_of(b"the-expected-binary"))

    with patch("appimage.build.shutil.which", return_value=str(tool_on_path)):
        with pytest.raises(RuntimeError):
            _resolve_appimagetool(resolved, tmp_path / "cache.AppImage", "x86_64")


def test_resolve_appimagetool_cache_hash_mismatch_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache.AppImage"
    cache.write_bytes(b"stale-cached-binary")
    resolved = make_resolved(appimagetool_sha256=digest_of(b"the-expected-binary"))

    with patch("appimage.build.shutil.which", return_value=None):
        with pytest.raises(RuntimeError):
            _resolve_appimagetool(resolved, cache, "x86_64")


def test_resolve_appimagetool_download_mismatch_deletes_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache.AppImage"
    resolved = make_resolved(appimagetool_sha256=digest_of(b"the-expected-binary"))

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(b"a-different-binary")

    with patch("appimage.build.shutil.which", return_value=None), \
         patch("appimage.build._fetch_release_asset_digest", return_value=("https://example/appimagetool-x86_64.AppImage", None)), \
         patch("appimage.build._download", side_effect=fake_download):
        with pytest.raises(RuntimeError):
            _resolve_appimagetool(resolved, cache, "x86_64")

    assert not cache.exists()  # download-cache artifact IS cleaned up on mismatch


def test_resolve_appimagetool_download_verifies_free_api_digest(tmp_path: Path) -> None:
    cache = tmp_path / "cache.AppImage"
    resolved = make_resolved()
    content = b"the-real-appimagetool"

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(content)

    with patch("appimage.build.shutil.which", return_value=None), \
         patch("appimage.build._fetch_release_asset_digest", return_value=("https://example/appimagetool-x86_64.AppImage", digest_of(content))), \
         patch("appimage.build._download", side_effect=fake_download):
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

    with patch("appimage.build.shutil.which", return_value=None), \
         patch("appimage.build._fetch_release_asset_digest", side_effect=fake_fetch_digest), \
         patch("appimage.build._download", side_effect=fake_download):
        _resolve_appimagetool(resolved, cache, "armv7l")

    assert captured["asset_name"] == "appimagetool-armhf.AppImage"


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

    with patch("appimage.build._fetch_release_asset_digest", return_value=("https://example/runtime-x86_64", digest_of(content))), \
         patch("appimage.build._download", side_effect=fake_download):
        result = _resolve_runtime_file(resolved, cache, "x86_64")

    assert result == cache
    assert cache.exists()


def test_resolve_runtime_file_download_mismatch_deletes_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    resolved = make_resolved(runtime_sha256=digest_of(b"the-expected-runtime"))

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(b"a-different-runtime")

    with patch("appimage.build._fetch_release_asset_digest", return_value=("https://example/runtime-x86_64", None)), \
         patch("appimage.build._download", side_effect=fake_download):
        with pytest.raises(RuntimeError):
            _resolve_runtime_file(resolved, cache, "x86_64")

    assert not cache.exists()


def test_resolve_runtime_file_no_path_lookup(tmp_path: Path) -> None:
    """Unlike appimagetool, the runtime stub is never looked up on PATH."""
    cache = tmp_path / "cache"
    resolved = make_resolved()

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(b"content")

    with patch("appimage.build.shutil.which", return_value="/usr/bin/runtime-x86_64") as mock_which, \
         patch("appimage.build._fetch_release_asset_digest", return_value=("https://example/runtime-x86_64", None)), \
         patch("appimage.build._download", side_effect=fake_download):
        _resolve_runtime_file(resolved, cache, "x86_64")

    mock_which.assert_not_called()


# ---------------------------------------------------------------------------
# verify_downloads (strict mode)
# ---------------------------------------------------------------------------

def test_verify_downloads_raises_instead_of_warning_for_appimagetool(tmp_path: Path) -> None:
    tool_on_path = tmp_path / "appimagetool"
    tool_on_path.write_bytes(b"whatever-was-on-path")
    resolved = make_resolved(verify_downloads=True)

    with patch("appimage.build.shutil.which", return_value=str(tool_on_path)):
        with pytest.raises(RuntimeError, match="could not be verified"):
            _resolve_appimagetool(resolved, tmp_path / "cache.AppImage", "x86_64")


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
# zsyncmake availability (checked in _resolve, so --check sees it too)
# ---------------------------------------------------------------------------

def _write_minimal_project(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
    )


def _has_zsyncmake_message(messages: list[str]) -> bool:
    return any("zsyncmake is not on PATH" in m for m in messages)


def test_zsyncmake_noop_without_update_info(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig()

    with patch("appimage.build.shutil.which", return_value=None):
        resolved = _resolve(config, tmp_path)

    assert not _has_zsyncmake_message(resolved.warnings)
    assert resolved.errors == []


def test_zsyncmake_noop_when_found(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(update_info="zsync|https://example/app.AppImage.zsync")

    with patch("appimage.build.shutil.which", return_value="/usr/bin/zsyncmake"):
        resolved = _resolve(config, tmp_path)

    assert not _has_zsyncmake_message(resolved.warnings)
    assert resolved.errors == []


def test_zsyncmake_warns_by_default(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(update_info="zsync|https://example/app.AppImage.zsync")

    with patch("appimage.build.shutil.which", return_value=None):
        resolved = _resolve(config, tmp_path)

    assert resolved.errors == []
    assert _has_zsyncmake_message(resolved.warnings)


def test_zsyncmake_errors_with_require_zsyncmake(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(
        update_info="zsync|https://example/app.AppImage.zsync", require_zsyncmake=True,
    )

    with patch("appimage.build.shutil.which", return_value=None):
        resolved = _resolve(config, tmp_path)

    assert not _has_zsyncmake_message(resolved.warnings)
    assert _has_zsyncmake_message(resolved.errors)


def test_zsyncmake_warns_not_errors_with_verify_downloads_alone(tmp_path: Path) -> None:
    """verify_downloads is independent of require_zsyncmake — only warns."""
    _write_minimal_project(tmp_path)
    config = BuildConfig(
        update_info="zsync|https://example/app.AppImage.zsync", verify_downloads=True,
    )

    with patch("appimage.build.shutil.which", return_value=None):
        resolved = _resolve(config, tmp_path)

    assert resolved.errors == []
    assert any("zsyncmake is not on PATH" in w for w in resolved.warnings)


# ---------------------------------------------------------------------------
# reproducible (umbrella flag)
# ---------------------------------------------------------------------------

def test_reproducible_errors_when_pins_missing(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(reproducible=True)

    resolved = _resolve(config, tmp_path)

    assert resolved.verify_downloads is True
    assert resolved.require_zsyncmake is True
    assert any("python_date" in e for e in resolved.errors)
    assert any("appimagetool_sha256" in e for e in resolved.errors)
    assert any("runtime_sha256" in e for e in resolved.errors)


def test_reproducible_passes_when_pins_set(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(
        reproducible=True,
        python_date="20260211",
        appimagetool_sha256=digest_of(b"tool"),
        runtime_sha256=digest_of(b"runtime"),
    )

    resolved = _resolve(config, tmp_path)

    assert resolved.errors == []
    assert resolved.verify_downloads is True
    assert resolved.require_zsyncmake is True


def test_reproducible_false_does_not_require_pins(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig()

    resolved = _resolve(config, tmp_path)

    assert resolved.errors == []
    assert resolved.verify_downloads is False
    assert resolved.require_zsyncmake is False


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
# _resolve_python_tarball
# ---------------------------------------------------------------------------

def test_resolve_python_tarball_local_archive_without_hash_stays_offline(tmp_path: Path) -> None:
    archive = tmp_path / "python.tar.gz"
    archive.write_bytes(b"tarball-content")
    resolved = make_resolved(python_archive=str(archive))

    with patch("appimage.build.urllib.request.urlopen") as mock_urlopen:
        result = _resolve_python_tarball(resolved, tmp_path / "cache.tar.gz", "x86_64")

    mock_urlopen.assert_not_called()
    assert result == archive


def test_resolve_python_tarball_cache_without_hash_stays_offline(tmp_path: Path) -> None:
    cache = tmp_path / "cache.tar.gz"
    cache.write_bytes(b"cached-tarball")
    resolved = make_resolved()

    with patch("appimage.build.urllib.request.urlopen") as mock_urlopen:
        result = _resolve_python_tarball(resolved, cache, "x86_64")

    mock_urlopen.assert_not_called()
    assert result == cache


def test_resolve_python_tarball_fresh_download_verifies_free_api_digest(tmp_path: Path) -> None:
    cache = tmp_path / "cache.tar.gz"
    resolved = make_resolved(python="3.11", python_date="20260211")
    content = b"the-real-tarball"

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(content)

    with patch("appimage.build.urllib.request.urlopen", return_value=_fake_release_response("sha256:" + digest_of(content))), \
         patch("appimage.build._download", side_effect=fake_download):
        result = _resolve_python_tarball(resolved, cache, "x86_64")

    assert result == cache
    assert cache.exists()


def test_resolve_python_tarball_fresh_download_mismatch_deletes_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache.tar.gz"
    resolved = make_resolved(python="3.11", python_date="20260211")

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(b"tampered-or-corrupted")

    with patch("appimage.build.urllib.request.urlopen", return_value=_fake_release_response("sha256:" + "b" * 64)), \
         patch("appimage.build._download", side_effect=fake_download):
        with pytest.raises(RuntimeError):
            _resolve_python_tarball(resolved, cache, "x86_64")

    assert not cache.exists()


# ---------------------------------------------------------------------------
# _prepare_python / _compile_pyc
# ---------------------------------------------------------------------------

def test_prepare_python_installs_with_no_compile(tmp_path: Path) -> None:
    from appimage.build import _prepare_python

    resolved = make_resolved(install_targets=["appimage==2.0.1", "."])
    appdir = tmp_path / "AppDir"
    appdir.mkdir()
    tarball = tmp_path / "python.tar.gz"
    tarball.write_bytes(b"")

    with patch("appimage.build.tarfile.open") as mock_tarfile, \
         patch("appimage.build.subprocess.run") as mock_run:
        mock_tarfile.return_value.__enter__.return_value.extractall = MagicMock()
        _prepare_python(resolved, tarball, appdir, tmp_path)

    args = mock_run.call_args.args[0]
    assert "--no-compile" in args
    assert "appimage==2.0.1" in args


def test_compile_pyc_uses_hash_invalidation(tmp_path: Path) -> None:
    from appimage.build import _compile_pyc

    resolved = make_resolved(python="3.11")
    appdir = tmp_path / "AppDir"

    with patch("appimage.build.subprocess.run") as mock_run:
        _compile_pyc(resolved, appdir)

    args = mock_run.call_args.args[0]
    assert "--invalidation-mode" in args
    assert "unchecked-hash" in args
    assert str(appdir / "python" / "lib" / "python3.11" / "site-packages") in args


# ---------------------------------------------------------------------------
# build() orchestration order
# ---------------------------------------------------------------------------

def test_build_compiles_pyc_after_pre_package_before_appimagetool(tmp_path: Path) -> None:
    from appimage import build as build_module

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
    )
    config = build_module.BuildConfig(hooks={"pre_package": "hook.sh"})
    (tmp_path / "hook.sh").write_text("#!/bin/sh\n")
    (tmp_path / "hook.sh").chmod(0o755)

    manager = MagicMock()
    with patch.object(build_module, "_resolve_python_tarball", manager._resolve_python_tarball), \
         patch.object(build_module, "_prepare_python", manager._prepare_python), \
         patch.object(build_module, "_copy_assets", manager._copy_assets), \
         patch.object(build_module, "_copy_extra_files", manager._copy_extra_files), \
         patch.object(build_module, "_run_hook", manager._run_hook), \
         patch.object(build_module, "_compile_pyc", manager._compile_pyc), \
         patch.object(build_module, "_resolve_appimagetool", manager._resolve_appimagetool), \
         patch.object(build_module, "_resolve_runtime_file", manager._resolve_runtime_file), \
         patch.object(build_module.subprocess, "run", manager.subprocess_run):
        manager._resolve_appimagetool.return_value = Path("/fake/appimagetool")
        manager._resolve_runtime_file.return_value = Path("/fake/runtime-x86_64")
        build(config, tmp_path)

    call_names = [c[0] for c in manager.mock_calls]
    assert call_names.index("_run_hook") < call_names.index("_compile_pyc")
    assert call_names.index("_compile_pyc") < call_names.index("_resolve_appimagetool")

    packaging_call = next(c for c in manager.mock_calls if c[0] == "subprocess_run")
    cmd = packaging_call.args[0]
    assert "--runtime-file" in cmd
    assert str(Path("/fake/runtime-x86_64")) in cmd
    assert packaging_call.kwargs["env"]["SOURCE_DATE_EPOCH"] == "0"


def test_build_respects_source_date_epoch_env_var(tmp_path: Path) -> None:
    from appimage import build as build_module

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
    )
    config = build_module.BuildConfig()

    manager = MagicMock()
    with patch.object(build_module, "_resolve_python_tarball", manager._resolve_python_tarball), \
         patch.object(build_module, "_prepare_python", manager._prepare_python), \
         patch.object(build_module, "_copy_assets", manager._copy_assets), \
         patch.object(build_module, "_copy_extra_files", manager._copy_extra_files), \
         patch.object(build_module, "_compile_pyc", manager._compile_pyc), \
         patch.object(build_module, "_resolve_appimagetool", manager._resolve_appimagetool), \
         patch.object(build_module, "_resolve_runtime_file", manager._resolve_runtime_file), \
         patch.object(build_module.subprocess, "run", manager.subprocess_run), \
         patch.dict("os.environ", {"SOURCE_DATE_EPOCH": "1700000000"}):
        manager._resolve_appimagetool.return_value = Path("/fake/appimagetool")
        manager._resolve_runtime_file.return_value = Path("/fake/runtime-x86_64")
        build(config, tmp_path)

    packaging_call = next(c for c in manager.mock_calls if c[0] == "subprocess_run")
    assert packaging_call.kwargs["env"]["SOURCE_DATE_EPOCH"] == "1700000000"


# ---------------------------------------------------------------------------
# write_config() appimagetool pinning
# ---------------------------------------------------------------------------

def test_write_config_pins_appimagetool_when_unset(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        '[tool.appimage.build]\napp = "myapp"\nentry_point = "myapp"\npython = "3.11"\n'
    )
    from appimage.build import BuildConfig

    config = BuildConfig.from_pyproject(tmp_path)

    tool_path = tmp_path / "appimagetool"
    runtime_path = tmp_path / "runtime-x86_64"

    with patch("appimage.build._resolve_appimagetool", return_value=tool_path) as mock_resolve_tool, \
         patch("appimage.build._resolve_runtime_file", return_value=runtime_path) as mock_resolve_runtime, \
         patch("appimage.build._appimagetool_version_string", return_value="continuous build (commit abc), build 1"), \
         patch("appimage.build._sha256_file", return_value="c" * 64):
        write_config(config, tmp_path)

    mock_resolve_tool.assert_called_once()
    mock_resolve_runtime.assert_called_once()
    content = (tmp_path / "pyproject.toml").read_text()
    assert "appimagetool_sha256" in content
    assert "c" * 64 in content
    assert "appimagetool_version" in content
    assert "runtime_sha256" in content


def test_write_config_skips_appimagetool_resolution_when_already_set(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        "[tool.appimage.build]\n"
        'app = "myapp"\nentry_point = "myapp"\npython = "3.11"\n'
        'appimagetool_sha256 = "deadbeef"\n'
        'runtime_sha256 = "deadbeef"\n'
    )
    from appimage.build import BuildConfig

    config = BuildConfig.from_pyproject(tmp_path)

    with patch("appimage.build._resolve_appimagetool") as mock_resolve_tool, \
         patch("appimage.build._resolve_runtime_file") as mock_resolve_runtime:
        write_config(config, tmp_path)

    mock_resolve_tool.assert_not_called()
    mock_resolve_runtime.assert_not_called()


# ---------------------------------------------------------------------------
# pylock (dependency hash-pinning)
# ---------------------------------------------------------------------------

def _has_pylock_message(messages: list[str]) -> bool:
    return any("No pylock configured" in m for m in messages)


def test_pylock_warns_by_default(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig()

    resolved = _resolve(config, tmp_path)

    assert resolved.errors == []
    assert _has_pylock_message(resolved.warnings)


def test_pylock_errors_with_require_pylock(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(require_pylock=True)

    resolved = _resolve(config, tmp_path)

    assert not _has_pylock_message(resolved.warnings)
    assert _has_pylock_message(resolved.errors)


def test_pylock_noop_when_configured(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(pylock="pylock.toml")

    resolved = _resolve(config, tmp_path)

    assert resolved.errors == []
    assert not _has_pylock_message(resolved.warnings)


# ---------------------------------------------------------------------------
# _reproducibility_summary
# ---------------------------------------------------------------------------

def test_reproducibility_summary_reports_zero_of_three_by_default() -> None:
    from appimage.build import _reproducibility_summary

    resolved = make_resolved()

    lines = _reproducibility_summary(resolved)

    assert any("Reproducibility: 0/3 pins set" in line for line in lines)
    assert any("--init" in line for line in lines)
    assert any("Dependency verification: pylock not set" in line for line in lines)
    assert any("--lock" in line for line in lines)


def test_reproducibility_summary_reports_full_pins_without_nudge() -> None:
    from appimage.build import _reproducibility_summary

    resolved = make_resolved(
        python_date="20260211",
        appimagetool_sha256="a" * 64,
        runtime_sha256="b" * 64,
        pylock="pylock.toml",
    )

    lines = _reproducibility_summary(resolved)

    assert any("Reproducibility: 3/3 pins set" in line for line in lines)
    assert not any("--init" in line for line in lines)
    assert any("Dependency verification: pylock set (pylock.toml)" in line for line in lines)


def test_reproducibility_summary_reports_partial_pins() -> None:
    from appimage.build import _reproducibility_summary

    resolved = make_resolved(python_date="20260211")

    lines = _reproducibility_summary(resolved)

    assert any("Reproducibility: 1/3 pins set" in line for line in lines)


# ---------------------------------------------------------------------------
# _install_from_pylock / _prepare_python with pylock configured
# ---------------------------------------------------------------------------

def test_prepare_python_uses_pylock_when_configured(tmp_path: Path) -> None:
    from appimage.build import _prepare_python

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

    with patch("appimage.build.tarfile.open") as mock_tarfile, \
         patch("appimage.build.subprocess.run") as mock_run:
        mock_tarfile.return_value.__enter__.return_value.extractall = MagicMock()
        _prepare_python(resolved, tarball, appdir, tmp_path)

    calls = [c.args[0] for c in mock_run.call_args_list]
    assert len(calls) == 2
    local_call, lock_call = calls
    assert "--no-deps" in local_call
    assert "." in local_call
    assert "appimage==2.0.1" not in local_call
    assert "--require-hashes" in lock_call
    assert "--no-deps" in lock_call
    assert str(tmp_path / "pylock.toml") in lock_call


def test_prepare_python_raises_when_pylock_missing(tmp_path: Path) -> None:
    from appimage.build import _prepare_python

    resolved = make_resolved(pylock="pylock.toml")
    appdir = tmp_path / "AppDir"
    appdir.mkdir()
    tarball = tmp_path / "python.tar.gz"
    tarball.write_bytes(b"")

    with patch("appimage.build.tarfile.open") as mock_tarfile:
        mock_tarfile.return_value.__enter__.return_value.extractall = MagicMock()
        with pytest.raises(FileNotFoundError):
            _prepare_python(resolved, tarball, appdir, tmp_path)


# ---------------------------------------------------------------------------
# _pip_version / _generate_lock / lock()
# ---------------------------------------------------------------------------

def test_pip_version_parses_output(tmp_path: Path) -> None:
    from appimage.build import _pip_version

    fake_result = MagicMock(stdout="pip 26.1.2 from /some/path (python 3.13)\n")
    with patch("appimage.build.subprocess.run", return_value=fake_result):
        assert _pip_version(tmp_path / "python3") == (26, 1)


def test_pip_version_raises_on_unparseable_output(tmp_path: Path) -> None:
    from appimage.build import _pip_version

    fake_result = MagicMock(stdout="not a pip version string\n")
    with patch("appimage.build.subprocess.run", return_value=fake_result), \
         pytest.raises(RuntimeError):
        _pip_version(tmp_path / "python3")


def test_generate_lock_raises_for_old_pip(tmp_path: Path) -> None:
    from appimage.build import _generate_lock

    resolved = make_resolved(pylock="pylock.toml")
    with patch("appimage.build._pip_version", return_value=(24, 3)), \
         pytest.raises(RuntimeError, match="does not support"):
        _generate_lock(resolved, tmp_path / "python3", tmp_path, uploaded_prior_to="")


def test_generate_lock_builds_expected_command(tmp_path: Path) -> None:
    from appimage.build import _generate_lock

    resolved = make_resolved(
        install_targets=["appimage==2.0.1", ".", "extra-pkg"], pylock="pylock.toml",
    )

    with patch("appimage.build._pip_version", return_value=(25, 1)), \
         patch("appimage.build.subprocess.run") as mock_run:
        result = _generate_lock(resolved, tmp_path / "python3", tmp_path, uploaded_prior_to="P7D")

    assert result == tmp_path / "pylock.toml"
    cmd = mock_run.call_args.args[0]
    assert "lock" in cmd
    assert "appimage==2.0.1" in cmd
    assert "extra-pkg" in cmd
    assert "--only-deps" in cmd
    assert "--uploaded-prior-to" in cmd
    assert "P7D" in cmd
    assert str(tmp_path / "pylock.toml") in cmd


def test_generate_lock_omits_uploaded_prior_to_when_unset(tmp_path: Path) -> None:
    from appimage.build import _generate_lock

    resolved = make_resolved(pylock="pylock.toml")

    with patch("appimage.build._pip_version", return_value=(25, 1)), \
         patch("appimage.build.subprocess.run") as mock_run:
        _generate_lock(resolved, tmp_path / "python3", tmp_path, uploaded_prior_to="")

    cmd = mock_run.call_args.args[0]
    assert "--uploaded-prior-to" not in cmd


def test_lock_writes_pylock_to_pyproject_when_unset(tmp_path: Path) -> None:
    from appimage.build import lock

    _write_minimal_project(tmp_path)
    config = BuildConfig()

    with patch("appimage.build._resolve_python_tarball", return_value=tmp_path / "python.tar.gz"), \
         patch("appimage.build.tarfile.open") as mock_tarfile, \
         patch("appimage.build._generate_lock", return_value=tmp_path / "pylock.toml") as mock_generate:
        mock_tarfile.return_value.__enter__.return_value.extractall = MagicMock()
        lock(config, tmp_path)

    mock_generate.assert_called_once()
    content = (tmp_path / "pyproject.toml").read_text()
    assert 'pylock = "pylock.toml"' in content


def test_lock_skips_write_when_already_set(tmp_path: Path) -> None:
    from appimage.build import lock

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "myapp"\nscripts = { myapp = "myapp:main" }\n'
        '[tool.appimage.build]\npylock = "custom-lock.toml"\n'
    )
    config = BuildConfig.from_pyproject(tmp_path)

    with patch("appimage.build._resolve_python_tarball", return_value=tmp_path / "python.tar.gz"), \
         patch("appimage.build.tarfile.open") as mock_tarfile, \
         patch("appimage.build._generate_lock", return_value=tmp_path / "custom-lock.toml"):
        mock_tarfile.return_value.__enter__.return_value.extractall = MagicMock()
        lock(config, tmp_path)

    content = (tmp_path / "pyproject.toml").read_text()
    assert content.count("pylock =") == 1
