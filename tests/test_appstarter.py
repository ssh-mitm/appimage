"""Unit tests for appimage.appstarter."""

import os
import site
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch
from venv import EnvBuilder

import pytest

from appimage.appstarter import (
    AppStartExceptionError,
    AppStarter,
    InvalidEntryPointError,
    get_entry_points,
    patch_appimage_venv,
    setup_python_patched,
    start_entry_point,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ep(name: str, value: str = "module:func") -> MagicMock:
    ep = MagicMock()
    ep.name = name
    ep.value = value
    return ep


def make_starter(**kw) -> AppStarter:
    """Instantiate AppStarter without reading environment variables."""
    s = object.__new__(AppStarter)
    s.default_ep = kw.get("default_ep")
    s.subprocess_args = kw.get("subprocess_args")
    s.appimage = kw.get("appimage")
    s.argv0 = kw.get("argv0")
    s.env_ep = kw.get("env_ep")
    s.virtual_env = kw.get("virtual_env")
    return s


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def restore_sys_path():
    original = sys.path.copy()
    yield
    sys.path[:] = original


@pytest.fixture()
def restore_site():
    base, site_ = site.USER_BASE, site.USER_SITE
    yield
    site.USER_BASE, site.USER_SITE = base, site_


@pytest.fixture()
def restore_sys_executable():
    original = sys.executable
    yield
    sys.executable = original


@pytest.fixture()
def clean_env_builder():
    """Reset EnvBuilder monkey-patch state before and after a test."""
    original_setup = EnvBuilder.setup_python
    had_original = hasattr(EnvBuilder, "setup_python_original")
    if had_original:
        del EnvBuilder.setup_python_original
    EnvBuilder.setup_python = original_setup
    yield
    EnvBuilder.setup_python = original_setup
    if hasattr(EnvBuilder, "setup_python_original"):
        del EnvBuilder.setup_python_original


# ---------------------------------------------------------------------------
# get_entry_points
# ---------------------------------------------------------------------------

class TestGetEntryPoints:
    def test_python_310_uses_select(self):
        ep = make_ep("ssh-mitm")
        mock_eps = MagicMock()
        mock_eps.select.return_value = [ep]
        with patch("appimage.appstarter.entry_points", return_value=mock_eps):
            with patch.object(sys, "version_info", (3, 10, 0)):
                result = get_entry_points("console_scripts")
        mock_eps.select.assert_called_once_with(group="console_scripts")
        assert result == [ep]

    def test_python_39_uses_get(self):
        ep = make_ep("ssh-mitm")
        mock_eps = MagicMock()
        mock_eps.get.return_value = [ep]
        with patch("appimage.appstarter.entry_points", return_value=mock_eps):
            with patch.object(sys, "version_info", (3, 9, 0)):
                result = get_entry_points("console_scripts")
        mock_eps.get.assert_called_once_with("console_scripts", [])
        assert result == [ep]

    def test_python_39_missing_group_returns_empty(self):
        mock_eps = MagicMock()
        mock_eps.get.return_value = []
        with patch("appimage.appstarter.entry_points", return_value=mock_eps):
            with patch.object(sys, "version_info", (3, 9, 0)):
                result = get_entry_points("nonexistent_group")
        assert result == []


# ---------------------------------------------------------------------------
# patch_appimage_venv
# ---------------------------------------------------------------------------

class TestPatchAppimageVenv:
    def test_replaces_python3_symlink_with_appimage(self):
        context = SimpleNamespace(bin_path="/venv/bin")
        with patch.dict(os.environ, {"APPIMAGE": "/app.AppImage"}, clear=True):
            with patch("os.remove") as mock_remove:
                with patch("os.symlink") as mock_symlink:
                    with patch("appimage.appstarter.get_entry_points", return_value=[]):
                        patch_appimage_venv(context)
        mock_remove.assert_called_once_with("/venv/bin/python3")
        mock_symlink.assert_called_once_with("/app.AppImage", "/venv/bin/python3")

    def test_fallback_to_appdir_apprun_when_no_appimage(self):
        context = SimpleNamespace(bin_path="/venv/bin")
        with patch.dict(os.environ, {"APPDIR": "/appdir"}, clear=True):
            with patch("os.remove"):
                with patch("os.symlink") as mock_symlink:
                    with patch("appimage.appstarter.get_entry_points", return_value=[]):
                        patch_appimage_venv(context)
        mock_symlink.assert_called_once_with("/appdir/AppRun", "/venv/bin/python3")

    def test_exits_without_appimage_or_appdir(self):
        context = SimpleNamespace(bin_path="/venv/bin")
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SystemExit):
                patch_appimage_venv(context)

    def test_creates_missing_console_script_symlinks(self):
        context = SimpleNamespace(bin_path="/venv/bin")
        ep = make_ep("ssh-mitm")
        with patch.dict(os.environ, {"APPIMAGE": "/app.AppImage"}, clear=True):
            with patch("os.remove"):
                with patch("os.symlink") as mock_symlink:
                    with patch("os.path.isfile", return_value=False):
                        with patch("appimage.appstarter.get_entry_points", return_value=[ep]):
                            patch_appimage_venv(context)
        assert mock_symlink.call_count == 2
        mock_symlink.assert_any_call("python3", "/venv/bin/ssh-mitm")

    def test_skips_already_existing_entry_point_files(self):
        context = SimpleNamespace(bin_path="/venv/bin")
        ep = make_ep("ssh-mitm")
        with patch.dict(os.environ, {"APPIMAGE": "/app.AppImage"}, clear=True):
            with patch("os.remove"):
                with patch("os.symlink") as mock_symlink:
                    with patch("os.path.isfile", return_value=True):
                        with patch("appimage.appstarter.get_entry_points", return_value=[ep]):
                            patch_appimage_venv(context)
        assert mock_symlink.call_count == 1  # only python3 replacement


# ---------------------------------------------------------------------------
# setup_python_patched
# ---------------------------------------------------------------------------

class TestSetupPythonPatched:
    def test_calls_original_then_patches_venv(self):
        builder = MagicMock()
        context = SimpleNamespace(bin_path="/venv/bin")
        with patch("appimage.appstarter.patch_appimage_venv") as mock_patch:
            setup_python_patched(builder, context)
        builder.setup_python_original.assert_called_once_with(context)
        mock_patch.assert_called_once_with(context)


# ---------------------------------------------------------------------------
# AppStarter.__init__
# ---------------------------------------------------------------------------

class TestAppStarterInit:
    def test_parses_all_env_vars(self):
        env = {
            "APPIMAGE": "/app.AppImage",
            "ARGV0": "/usr/bin/ssh-mitm",
            "APP_ENTRY_POINT": "ssh-mitm",
            "VIRTUAL_ENV": "/venv",
        }
        with patch.dict(os.environ, env, clear=True):
            s = AppStarter()
        assert s.appimage == os.path.abspath("/app.AppImage")
        assert s.argv0 == "ssh-mitm"
        assert s.env_ep == "ssh-mitm"
        assert s.virtual_env == "/venv"
        assert s.default_ep is None
        assert s.subprocess_args is None

    def test_all_none_without_env_vars(self):
        with patch.dict(os.environ, {}, clear=True):
            s = AppStarter()
        assert s.appimage is None
        assert s.argv0 is None
        assert s.env_ep is None
        assert s.virtual_env is None

    def test_appimage_converted_to_absolute_path(self):
        with patch.dict(os.environ, {"APPIMAGE": "relative.AppImage"}, clear=True):
            s = AppStarter()
        assert os.path.isabs(s.appimage)

    def test_argv0_is_basename_only(self):
        with patch.dict(os.environ, {"ARGV0": "/usr/local/bin/ssh-mitm"}, clear=True):
            s = AppStarter()
        assert s.argv0 == "ssh-mitm"
        assert "/" not in s.argv0


# ---------------------------------------------------------------------------
# AppStarter.python_path
# ---------------------------------------------------------------------------

class TestPythonPath:
    def test_returns_sys_executable(self):
        s = make_starter()
        assert s.python_path == sys.executable


# ---------------------------------------------------------------------------
# AppStarter.appdir
# ---------------------------------------------------------------------------

class TestAppdir:
    def test_returns_appdir_from_env(self):
        with patch.dict(os.environ, {"APPDIR": "/my/appdir"}, clear=True):
            s = AppStarter()
            assert s.appdir == "/my/appdir"

    def test_raises_value_error_when_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            s = AppStarter()
        with pytest.raises(ValueError, match="APPDIR not set"):
            _ = s.appdir


# ---------------------------------------------------------------------------
# AppStarter.entry_points (cached property)
# ---------------------------------------------------------------------------

class TestEntryPointsProperty:
    def test_indexed_by_name_and_value(self):
        ep = make_ep("ssh-mitm", "ssmitm.cli:main")
        s = make_starter()
        with patch("appimage.appstarter.get_entry_points", return_value=[ep]):
            eps = s.entry_points
        assert eps["ssh-mitm"] is ep
        assert eps["ssmitm.cli:main"] is ep

    def test_empty_dict_when_no_console_scripts(self):
        s = make_starter()
        with patch("appimage.appstarter.get_entry_points", return_value=[]):
            assert s.entry_points == {}

    def test_result_is_cached(self):
        s = make_starter()
        with patch("appimage.appstarter.get_entry_points", return_value=[]) as mock_eps:
            _ = s.entry_points
            _ = s.entry_points
        mock_eps.assert_called_once()


# ---------------------------------------------------------------------------
# AppStarter.get_entry_point
# ---------------------------------------------------------------------------

class TestGetEntryPoint:
    def _starter_with_eps(self, eps: dict, **kw) -> AppStarter:
        s = make_starter(**kw)
        s.__dict__["entry_points"] = eps
        return s

    def test_env_ep_has_highest_priority(self):
        ep_env, ep_argv, ep_default = make_ep("a"), make_ep("b"), make_ep("c")
        s = self._starter_with_eps(
            {"a": ep_env, "b": ep_argv, "c": ep_default},
            env_ep="a", argv0="b", default_ep="c",
        )
        assert s.get_entry_point() is ep_env

    def test_argv0_used_when_no_env_ep(self):
        ep = make_ep("ssh-mitm")
        s = self._starter_with_eps({"ssh-mitm": ep}, argv0="ssh-mitm")
        assert s.get_entry_point() is ep

    def test_default_ep_used_as_last_resort(self):
        ep = make_ep("ssh-mitm")
        s = self._starter_with_eps({"ssh-mitm": ep}, default_ep="ssh-mitm")
        assert s.get_entry_point() is ep

    def test_ignore_default_skips_default_ep(self):
        ep = make_ep("ssh-mitm")
        s = self._starter_with_eps({"ssh-mitm": ep}, default_ep="ssh-mitm")
        assert s.get_entry_point(ignore_default=True) is None

    def test_returns_none_when_nothing_matches(self):
        s = self._starter_with_eps({}, env_ep="missing", argv0="also-missing")
        assert s.get_entry_point() is None

    def test_env_ep_not_found_falls_through_to_argv0(self):
        ep = make_ep("ssh-mitm")
        s = self._starter_with_eps({"ssh-mitm": ep}, env_ep="missing", argv0="ssh-mitm")
        assert s.get_entry_point() is ep


# ---------------------------------------------------------------------------
# AppStarter.start_entry_point (the method)
# ---------------------------------------------------------------------------

class TestStartEntryPointMethod:
    def test_loads_and_executes_entry_point(self):
        mock_func = MagicMock(return_value=0)
        ep = make_ep("ssh-mitm")
        ep.load.return_value = mock_func
        s = make_starter(argv0="ssh-mitm")
        s.__dict__["entry_points"] = {"ssh-mitm": ep}

        with pytest.raises(SystemExit) as exc:
            s.start_entry_point()
        assert exc.value.code == 0
        mock_func.assert_called_once()

    def test_sets_sys_executable_when_virtual_env_active(self, restore_sys_executable):
        mock_func = MagicMock(return_value=0)
        ep = make_ep("ssh-mitm")
        ep.load.return_value = mock_func
        s = make_starter(argv0="ssh-mitm", virtual_env="/venv")
        s.__dict__["entry_points"] = {"ssh-mitm": ep}

        with pytest.raises(SystemExit):
            s.start_entry_point()
        assert sys.executable == "/venv/bin/python3"

    def test_raises_invalid_entry_point_error_when_not_found(self):
        s = make_starter(env_ep="nonexistent")
        s.__dict__["entry_points"] = {}
        with pytest.raises(InvalidEntryPointError, match="nonexistent"):
            s.start_entry_point()

    def test_error_message_prefers_env_ep(self):
        s = make_starter(env_ep="missing-ep", default_ep="other")
        s.__dict__["entry_points"] = {}
        with pytest.raises(InvalidEntryPointError, match="missing-ep"):
            s.start_entry_point()

    def test_error_message_falls_back_to_default_ep(self):
        s = make_starter(default_ep="missing-default")
        s.__dict__["entry_points"] = {}
        with pytest.raises(InvalidEntryPointError, match="missing-default"):
            s.start_entry_point()


# ---------------------------------------------------------------------------
# AppStarter.start_interpreter
# ---------------------------------------------------------------------------

class TestStartInterpreter:
    def test_calls_execvp_with_python_path(self):
        s = make_starter()
        s.subprocess_args = ["/python"]
        s.__dict__["python_path"] = "/python"
        with patch("os.execvp") as mock_execvp:
            with patch.object(sys, "version_info", (3, 10, 0)):
                s.start_interpreter()
        mock_execvp.assert_called_once_with("/python", ["/python"])

    def test_adds_P_flag_on_python_311(self):
        s = make_starter()
        s.subprocess_args = ["/python"]
        s.__dict__["python_path"] = "/python"
        with patch("os.execvp") as mock_execvp:
            with patch.object(sys, "version_info", (3, 11, 0)):
                s.start_interpreter()
        mock_execvp.assert_called_once_with("/python", ["/python", "-P"])

    def test_passes_additional_subprocess_args(self):
        s = make_starter()
        s.subprocess_args = ["/python", "script.py", "--flag"]
        s.__dict__["python_path"] = "/python"
        with patch("os.execvp") as mock_execvp:
            with patch.object(sys, "version_info", (3, 10, 0)):
                s.start_interpreter()
        mock_execvp.assert_called_once_with("/python", ["/python", "script.py", "--flag"])

    def test_no_extra_args_when_subprocess_args_is_none(self):
        s = make_starter()
        s.subprocess_args = None
        s.__dict__["python_path"] = "/python"
        with patch("os.execvp") as mock_execvp:
            with patch.object(sys, "version_info", (3, 10, 0)):
                s.start_interpreter()
        mock_execvp.assert_called_once_with("/python", ["/python"])


# ---------------------------------------------------------------------------
# AppStarter.create_venv
# ---------------------------------------------------------------------------

class TestCreateVenv:
    def test_creates_venv_for_each_dir(self):
        s = make_starter()
        mock_builder = MagicMock()
        with patch("appimage.appstarter.EnvBuilder", return_value=mock_builder):
            with pytest.raises(SystemExit):
                s.create_venv(venv_dirs=["/venv1", "/venv2"])
        mock_builder.create.assert_any_call("/venv1")
        mock_builder.create.assert_any_call("/venv2")
        assert mock_builder.create.call_count == 2

    def test_always_exits_after_creating(self):
        s = make_starter()
        with patch("appimage.appstarter.EnvBuilder"):
            with pytest.raises(SystemExit):
                s.create_venv(venv_dirs=["/venv"])

    def test_passes_system_site_packages_to_builder(self):
        s = make_starter()
        with patch("appimage.appstarter.EnvBuilder") as MockCls:
            MockCls.return_value = MagicMock()
            with pytest.raises(SystemExit):
                s.create_venv(venv_dirs=["/venv"], system_site_packages=True)
        MockCls.assert_called_once_with(system_site_packages=True, symlinks=True)

    def test_uses_symlinks(self):
        s = make_starter()
        with patch("appimage.appstarter.EnvBuilder") as MockCls:
            MockCls.return_value = MagicMock()
            with pytest.raises(SystemExit):
                s.create_venv(venv_dirs=["/venv"])
        _, kwargs = MockCls.call_args
        assert kwargs.get("symlinks") is True

    def test_monkey_patches_env_builder_on_first_call(self, clean_env_builder):
        from appimage.appstarter import setup_python_patched
        s = make_starter()
        with patch("appimage.appstarter.EnvBuilder", EnvBuilder):
            with pytest.raises(SystemExit):
                s.create_venv(venv_dirs=["/tmp/test_venv_unused"])
        assert hasattr(EnvBuilder, "setup_python_original")
        assert EnvBuilder.setup_python is setup_python_patched

    def test_monkey_patch_applied_only_once(self, clean_env_builder):
        s = make_starter()
        original_setup = EnvBuilder.setup_python
        with patch("appimage.appstarter.EnvBuilder", EnvBuilder):
            with pytest.raises(SystemExit):
                s.create_venv(venv_dirs=["/tmp/unused1"])
        first_original = EnvBuilder.setup_python_original

        with patch("appimage.appstarter.EnvBuilder", EnvBuilder):
            with pytest.raises(SystemExit):
                s.create_venv(venv_dirs=["/tmp/unused2"])
        assert EnvBuilder.setup_python_original is first_original


# ---------------------------------------------------------------------------
# AppStarter.parse_venv_command
# ---------------------------------------------------------------------------

class TestParseVenvCommand:
    def test_no_op_when_m_venv_not_in_argv(self):
        s = make_starter()
        with patch.object(sys, "argv", ["/python", "ssh-mitm"]):
            with patch.object(s, "create_venv") as mock_create:
                s.parse_venv_command()
        mock_create.assert_not_called()

    def test_no_op_when_m_used_with_other_module(self):
        s = make_starter()
        with patch.object(sys, "argv", ["/python", "-m", "other"]):
            with patch.object(s, "create_venv") as mock_create:
                s.parse_venv_command()
        mock_create.assert_not_called()

    def test_creates_venv_when_m_venv_detected(self):
        s = make_starter()
        with patch.object(sys, "argv", ["/python", "-m", "venv", "/myenv"]):
            with patch.object(s, "create_venv") as mock_create:
                s.parse_venv_command()
        mock_create.assert_called_once_with(venv_dirs=["/myenv"], system_site_packages=False)

    def test_passes_system_site_packages_flag(self):
        s = make_starter()
        with patch.object(sys, "argv", ["/python", "-m", "venv", "--system-site-packages", "/myenv"]):
            with patch.object(s, "create_venv") as mock_create:
                s.parse_venv_command()
        mock_create.assert_called_once_with(venv_dirs=["/myenv"], system_site_packages=True)

    def test_handles_multiple_venv_dirs(self):
        s = make_starter()
        with patch.object(sys, "argv", ["/python", "-m", "venv", "/env1", "/env2"]):
            with patch.object(s, "create_venv") as mock_create:
                s.parse_venv_command()
        mock_create.assert_called_once_with(venv_dirs=["/env1", "/env2"], system_site_packages=False)


# ---------------------------------------------------------------------------
# AppStarter.parse_python_args
# ---------------------------------------------------------------------------

class TestParsePythonArgs:
    def test_python_interpreter_flag_starts_interpreter(self):
        s = make_starter(argv0="ssh-mitm")
        with patch.object(sys, "argv", ["/python", "--python-interpreter"]):
            with patch.object(s, "parse_venv_command"):
                with patch.object(s, "start_interpreter") as mock_interp:
                    s.parse_python_args()
        mock_interp.assert_called_once()

    def test_python_venv_flag_creates_venv(self):
        s = make_starter(argv0="ssh-mitm")
        with patch.object(sys, "argv", ["/python", "--python-venv", "/myenv"]):
            with patch.object(s, "create_venv") as mock_create:
                s.parse_python_args()
        mock_create.assert_called_once_with(venv_dirs=["/myenv"])

    def test_python_entry_point_sets_env_ep(self):
        s = make_starter(argv0="ssh-mitm")
        with patch.object(sys, "argv", ["/python", "--python-entry-point", "other-tool"]):
            s.parse_python_args()
        assert s.env_ep == "other-tool"

    def test_python_main_sets_default_ep(self):
        s = make_starter(argv0="ssh-mitm")
        with patch.object(sys, "argv", ["/python", "--python-main", "ssh-mitm"]):
            s.parse_python_args()
        assert s.default_ep == "ssh-mitm"

    def test_unknown_python_args_cause_exit(self):
        s = make_starter(argv0="ssh-mitm")
        with patch.object(sys, "argv", ["/python", "--python-unknown-flag"]):
            with pytest.raises(SystemExit):
                s.parse_python_args()

    def test_non_python_args_passed_through_to_subprocess_args(self):
        s = make_starter(argv0="ssh-mitm")
        with patch.object(sys, "argv", ["/python", "--python-main", "ssh-mitm", "connect", "--host", "example.com"]):
            s.parse_python_args()
        assert "connect" in s.subprocess_args
        assert "--host" in s.subprocess_args
        assert "example.com" in s.subprocess_args

    def test_python_main_arg_not_in_subprocess_args(self):
        s = make_starter(argv0="ssh-mitm")
        with patch.object(sys, "argv", ["/python", "--python-main", "ssh-mitm", "connect"]):
            s.parse_python_args()
        assert "--python-main" not in s.subprocess_args
        assert "ssh-mitm" not in s.subprocess_args


# ---------------------------------------------------------------------------
# AppStarter.start
# ---------------------------------------------------------------------------

class TestStart:
    def test_starts_entry_point_when_default_ep_set(self):
        ep = make_ep("ssh-mitm")
        s = make_starter(default_ep="ssh-mitm")
        s.__dict__["entry_points"] = {"ssh-mitm": ep}
        with patch.object(s, "parse_python_args"):
            with patch.object(s, "start_entry_point") as mock_start:
                s.start()
        mock_start.assert_called_once()

    def test_starts_interpreter_when_no_entry_point_found(self):
        s = make_starter()
        s.__dict__["entry_points"] = {}
        with patch.object(s, "parse_python_args"):
            with patch.object(s, "parse_venv_command"):
                with patch.object(s, "start_interpreter") as mock_interp:
                    with patch.object(s, "start_entry_point"):
                        s.start()
        mock_interp.assert_called_once()

    def test_starts_interpreter_when_argv0_is_python(self):
        ep = make_ep("ssh-mitm")
        s = make_starter(argv0="python", default_ep="ssh-mitm")
        s.__dict__["entry_points"] = {"ssh-mitm": ep}
        with patch.object(s, "parse_python_args"):
            with patch.object(s, "parse_venv_command"):
                with patch.object(s, "start_interpreter") as mock_interp:
                    with patch.object(s, "start_entry_point"):
                        s.start()
        mock_interp.assert_called_once()

    def test_starts_interpreter_when_argv0_is_python3(self):
        s = make_starter(argv0="python3")
        s.__dict__["entry_points"] = {}
        with patch.object(s, "parse_python_args"):
            with patch.object(s, "parse_venv_command"):
                with patch.object(s, "start_interpreter") as mock_interp:
                    with patch.object(s, "start_entry_point"):
                        s.start()
        mock_interp.assert_called_once()


# ---------------------------------------------------------------------------
# AppStarter._activate_venv
# ---------------------------------------------------------------------------

class TestActivateVenv:
    def test_sets_pythonuserbase(self):
        s = make_starter()
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            s._activate_venv("/my/venv")
            assert os.environ["PYTHONUSERBASE"] == "/my/venv"

    def test_prepends_venv_bin_to_path(self):
        s = make_starter()
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            s._activate_venv("/my/venv")
            assert os.environ["PATH"].startswith("/my/venv/bin:")
            assert "/usr/bin" in os.environ["PATH"]

    def test_removes_pythonnousersite(self):
        s = make_starter()
        with patch.dict(os.environ, {"PATH": "/usr/bin", "PYTHONNOUSERSITE": "1"}, clear=True):
            s._activate_venv("/my/venv")
            assert "PYTHONNOUSERSITE" not in os.environ

    def test_sets_site_user_base(self, restore_site):
        s = make_starter()
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            s._activate_venv("/my/venv")
        assert site.USER_BASE == "/my/venv"

    def test_sets_site_user_site_with_correct_python_version(self, restore_site):
        s = make_starter()
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            s._activate_venv("/my/venv")
        expected = f"/my/venv/lib/python{sys.version_info[0]}.{sys.version_info[1]}/site-packages"
        assert site.USER_SITE == expected

    def test_inserts_site_at_front_of_sys_path(self, restore_sys_path, restore_site):
        s = make_starter()
        with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
            s._activate_venv("/my/venv")
        expected = f"/my/venv/lib/python{sys.version_info[0]}.{sys.version_info[1]}/site-packages"
        assert sys.path[0] == expected


# ---------------------------------------------------------------------------
# AppStarter.setup_virtualenv
# ---------------------------------------------------------------------------

class TestSetupVirtualenv:
    APPIMAGE = "/path/to/app.AppImage"

    def test_activates_when_virtual_env_python3_resolves_to_appimage(self):
        s = make_starter(appimage=self.APPIMAGE)
        venv = "/path/to/venv"
        with patch.dict(os.environ, {"VIRTUAL_ENV": venv}, clear=True):
            with patch("os.path.realpath", return_value=self.APPIMAGE):
                with patch.object(s, "_activate_venv") as mock_activate:
                    s.setup_virtualenv()
        mock_activate.assert_called_once_with(venv)

    def test_no_activation_when_virtual_env_points_elsewhere(self):
        s = make_starter(appimage=self.APPIMAGE, argv0=None)
        with patch.dict(os.environ, {"VIRTUAL_ENV": "/venv"}, clear=True):
            with patch("os.path.realpath", return_value="/other/python"):
                with patch.object(s, "_activate_venv") as mock_activate:
                    s.setup_virtualenv()
        mock_activate.assert_not_called()

    def test_returns_early_without_argv0(self):
        s = make_starter(appimage=self.APPIMAGE, argv0=None)
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(s, "_activate_venv") as mock_activate:
                s.setup_virtualenv()
        mock_activate.assert_not_called()

    def test_uses_full_argv0_path_when_slash_present(self):
        s = make_starter(appimage=self.APPIMAGE, argv0="ssh-mitm")
        with patch.dict(os.environ, {"ARGV0": "/venv/bin/ssh-mitm"}, clear=True):
            with patch("os.path.islink", return_value=False) as mock_islink:
                with patch.object(s, "_activate_venv"):
                    s.setup_virtualenv()
        # cmd_path should be the full ARGV0 value, not the result of shutil.which
        first_islink_arg = mock_islink.call_args_list[0][0][0]
        assert first_islink_arg == "/venv/bin/ssh-mitm"

    def test_uses_shutil_which_when_argv0_has_no_slash(self):
        s = make_starter(appimage=self.APPIMAGE, argv0="ssh-mitm")
        with patch.dict(os.environ, {"ARGV0": "ssh-mitm"}, clear=True):
            with patch("shutil.which", return_value=None) as mock_which:
                with patch("os.path.islink", return_value=False):
                    s.setup_virtualenv()
        mock_which.assert_called_once_with("ssh-mitm")

    def test_no_activation_when_cmd_path_is_not_symlink(self):
        s = make_starter(appimage=self.APPIMAGE, argv0="ssh-mitm")
        with patch.dict(os.environ, {"ARGV0": "/venv/bin/ssh-mitm"}, clear=True):
            with patch("os.path.islink", return_value=False):
                with patch.object(s, "_activate_venv") as mock_activate:
                    s.setup_virtualenv()
        mock_activate.assert_not_called()

    def test_activates_venv_found_via_symlink_traversal(self):
        s = make_starter(appimage=self.APPIMAGE, argv0="ssh-mitm")
        venv_dir = "/venv"
        cmd = f"{venv_dir}/bin/ssh-mitm"
        python_symlink = f"{venv_dir}/bin/python3"

        def islink(path):
            return path in [cmd, python_symlink]

        def isfile(path):
            return path in [f"{venv_dir}/pyvenv.cfg", f"{venv_dir}/bin/activate"]

        def realpath(path):
            return self.APPIMAGE if "python3" in path else path

        with patch.dict(os.environ, {"ARGV0": cmd}, clear=True):
            with patch("os.path.islink", side_effect=islink):
                with patch("os.path.isfile", side_effect=isfile):
                    with patch("os.path.realpath", side_effect=realpath):
                        with patch("os.path.abspath", side_effect=lambda p: p):
                            with patch.object(s, "_activate_venv") as mock_activate:
                                s.setup_virtualenv()
        mock_activate.assert_called_once_with(venv_dir)

    def test_relative_symlink_resolved_relative_to_symlink_dir(self):
        """A relative symlink target must be resolved against the symlink's directory."""
        s = make_starter(appimage=self.APPIMAGE, argv0="ssh-mitm")
        venv_dir = "/venv"
        cmd = f"{venv_dir}/bin/ssh-mitm"
        resolved_python = f"{venv_dir}/bin/python3"

        islink_calls: list = []

        def islink(path):
            islink_calls.append(path)
            return path == cmd  # only the command is a symlink

        with patch.dict(os.environ, {"ARGV0": cmd}, clear=True):
            with patch("os.path.islink", side_effect=islink):
                with patch("os.path.isfile", return_value=False):
                    with patch("os.path.realpath", return_value="/other"):
                        with patch("os.readlink", return_value="python3"):
                            with patch("os.path.abspath", side_effect=os.path.abspath):
                                with patch.object(s, "_activate_venv"):
                                    s.setup_virtualenv()

        # After following the relative symlink "python3" from /venv/bin/ssh-mitm,
        # the next islink check must be for /venv/bin/python3 (not ./python3 from CWD).
        assert resolved_python in islink_calls


# ---------------------------------------------------------------------------
# Module-level start_entry_point
# ---------------------------------------------------------------------------

class TestModuleLevelStartEntryPoint:
    def test_exits_when_appdir_not_set(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SystemExit) as exc:
                start_entry_point()
        assert "AppImage" in str(exc.value)

    def test_calls_setup_virtualenv_and_start(self):
        with patch.dict(os.environ, {"APPDIR": "/appdir"}, clear=True):
            with patch("appimage.appstarter.AppStarter") as MockStarter:
                instance = MockStarter.return_value
                start_entry_point()
        instance.setup_virtualenv.assert_called_once()
        instance.start.assert_called_once()

    def test_catches_app_start_exception_and_exits(self):
        with patch.dict(os.environ, {"APPDIR": "/appdir"}, clear=True):
            with patch("appimage.appstarter.AppStarter") as MockStarter:
                MockStarter.return_value.start.side_effect = AppStartExceptionError("bad entry point")
                with pytest.raises(SystemExit) as exc:
                    start_entry_point()
        assert "bad entry point" in str(exc.value)

    def test_does_not_catch_other_exceptions(self):
        with patch.dict(os.environ, {"APPDIR": "/appdir"}, clear=True):
            with patch("appimage.appstarter.AppStarter") as MockStarter:
                MockStarter.return_value.start.side_effect = RuntimeError("unexpected")
                with pytest.raises(RuntimeError):
                    start_entry_point()
