"""Unit tests for appimage.ctl reproducibility features.

All network and subprocess calls are mocked — these tests never touch the
network or execute real binaries.
"""

import hashlib
import importlib
import json
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from appimage.ctl import BuildConfig, build, build_appdir, enable_reproducible, write_config
from appimage.ctl._appimagetool import _resolve_appimagetool, _resolve_runtime_file
from appimage.ctl._base import _ResolvedBuild, _resolve
from appimage.ctl._download import _sha256_file, _verify_sha256
from appimage.ctl._python import _resolve_python_tarball, _resolve_python_url
from appimage.ctl.build_appdir import _normalize_mtimes
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


def test_resolve_appimagetool_from_path_no_hash_warns_and_skips_download(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    tool_on_path = tmp_path / "appimagetool"
    tool_on_path.write_bytes(b"whatever-was-on-path")
    resolved = make_resolved()

    with patch("appimage.ctl._appimagetool.shutil.which", return_value=str(tool_on_path)), \
         patch("appimage.ctl._appimagetool._download") as mock_download, \
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

    with patch("appimage.ctl._appimagetool.shutil.which", return_value=str(tool_on_path)):
        with pytest.raises(RuntimeError):
            _resolve_appimagetool(resolved, tmp_path / "cache.AppImage", "x86_64")


def test_resolve_appimagetool_cache_hash_mismatch_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache.AppImage"
    cache.write_bytes(b"stale-cached-binary")
    resolved = make_resolved(appimagetool_sha256=digest_of(b"the-expected-binary"))

    with patch("appimage.ctl._appimagetool.shutil.which", return_value=None):
        with pytest.raises(RuntimeError):
            _resolve_appimagetool(resolved, cache, "x86_64")


def test_resolve_appimagetool_download_mismatch_deletes_cache(tmp_path: Path) -> None:
    cache = tmp_path / "cache.AppImage"
    resolved = make_resolved(appimagetool_sha256=digest_of(b"the-expected-binary"))

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(b"a-different-binary")

    with patch("appimage.ctl._appimagetool.shutil.which", return_value=None), \
         patch("appimage.ctl._appimagetool._fetch_release_asset_digest", return_value=("https://example/appimagetool-x86_64.AppImage", None)), \
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

    with patch("appimage.ctl._appimagetool.shutil.which", return_value=None), \
         patch("appimage.ctl._appimagetool._fetch_release_asset_digest", return_value=("https://example/appimagetool-x86_64.AppImage", digest_of(content))), \
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

    with patch("appimage.ctl._appimagetool.shutil.which", return_value=None), \
         patch("appimage.ctl._appimagetool._fetch_release_asset_digest", side_effect=fake_fetch_digest), \
         patch("appimage.ctl._appimagetool._download", side_effect=fake_download):
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
    """Unlike appimagetool, the runtime stub is never looked up on PATH."""
    cache = tmp_path / "cache"
    resolved = make_resolved()

    def fake_download(_url: str, dest: Path) -> None:
        dest.write_bytes(b"content")

    with patch("appimage.ctl._appimagetool.shutil.which", return_value="/usr/bin/runtime-x86_64") as mock_which, \
         patch("appimage.ctl._appimagetool._fetch_release_asset_digest", return_value=("https://example/runtime-x86_64", None)), \
         patch("appimage.ctl._appimagetool._download", side_effect=fake_download):
        _resolve_runtime_file(resolved, cache, "x86_64")

    mock_which.assert_not_called()


# ---------------------------------------------------------------------------
# verify_downloads (strict mode)
# ---------------------------------------------------------------------------

def test_verify_downloads_raises_instead_of_warning_for_appimagetool(tmp_path: Path) -> None:
    tool_on_path = tmp_path / "appimagetool"
    tool_on_path.write_bytes(b"whatever-was-on-path")
    resolved = make_resolved(verify_downloads=True)

    with patch("appimage.ctl._appimagetool.shutil.which", return_value=str(tool_on_path)):
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
# zsyncmake availability (checked in _resolve, so `check` sees it too)
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

    with patch("appimage.ctl._base.shutil.which", return_value=None):
        resolved = _resolve(config, tmp_path)

    assert not _has_zsyncmake_message(resolved.package_warnings)
    assert resolved.package_errors == []


def test_zsyncmake_noop_when_found(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(update_info="zsync|https://example/app.AppImage.zsync")

    with patch("appimage.ctl._base.shutil.which", return_value="/usr/bin/zsyncmake"):
        resolved = _resolve(config, tmp_path)

    assert not _has_zsyncmake_message(resolved.package_warnings)
    assert resolved.package_errors == []


def test_zsyncmake_warns_by_default(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(update_info="zsync|https://example/app.AppImage.zsync")

    with patch("appimage.ctl._base.shutil.which", return_value=None):
        resolved = _resolve(config, tmp_path)

    assert resolved.package_errors == []
    assert _has_zsyncmake_message(resolved.package_warnings)


def test_zsyncmake_errors_with_require_zsyncmake(tmp_path: Path) -> None:
    _write_minimal_project(tmp_path)
    config = BuildConfig(
        update_info="zsync|https://example/app.AppImage.zsync", require_zsyncmake=True,
    )

    with patch("appimage.ctl._base.shutil.which", return_value=None):
        resolved = _resolve(config, tmp_path)

    assert not _has_zsyncmake_message(resolved.package_warnings)
    assert _has_zsyncmake_message(resolved.package_errors)


def test_zsyncmake_warns_not_errors_with_verify_downloads_alone(tmp_path: Path) -> None:
    """verify_downloads is independent of require_zsyncmake — only warns."""
    _write_minimal_project(tmp_path)
    config = BuildConfig(
        update_info="zsync|https://example/app.AppImage.zsync", verify_downloads=True,
    )

    with patch("appimage.ctl._base.shutil.which", return_value=None):
        resolved = _resolve(config, tmp_path)

    assert resolved.package_errors == []
    assert any("zsyncmake is not on PATH" in w for w in resolved.package_warnings)


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
         patch.object(build_module.subprocess, "run", manager.subprocess_run), \
         patch.dict("os.environ", {"SOURCE_DATE_EPOCH": "1700000000"}):
        manager._resolve_appimagetool.return_value = Path("/fake/appimagetool")
        manager._resolve_runtime_file.return_value = Path("/fake/runtime-x86_64")
        build(config, tmp_path)

    packaging_call = next(c for c in manager.mock_calls if c[0] == "subprocess_run")
    assert packaging_call.kwargs["env"]["SOURCE_DATE_EPOCH"] == "1700000000"


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
    assert "--no-deps" in lock_call
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
