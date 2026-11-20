import subprocess

from digtk.lib import doctor

TOOL = {
    "name": "widget", "bin": "widget", "version_flag": "--version",
    "used_by": "test", "install_url": "https://example.com/widget",
}


def test_check_tool_reports_missing_when_not_on_path(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda bin: None)

    result = doctor.check_tool(TOOL)

    assert result == {
        "name": "widget", "bin": "widget", "available": False, "path": None,
        "version": None, "warning": None, "used_by": "test", "install_url": "https://example.com/widget",
    }


def test_check_tool_reports_available_with_version(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda bin: "/usr/bin/widget")
    monkeypatch.setattr(doctor.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout="widget 1.2.3\n", stderr="",
    ))

    result = doctor.check_tool(TOOL)

    assert result["available"] is True
    assert result["path"] == "/usr/bin/widget"
    assert result["version"] == "widget 1.2.3"
    assert result["warning"] is None


def test_check_tool_falls_back_to_stderr_for_version(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda bin: "/usr/bin/widget")
    monkeypatch.setattr(doctor.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, stdout="", stderr="widget version 9\n",
    ))

    result = doctor.check_tool(TOOL)

    assert result["version"] == "widget version 9"


def test_check_tool_reports_nonzero_version_call_as_warning_not_version(monkeypatch):
    """A `which`-able binary that errors out when actually run (e.g. macOS's java
    stub with no JRE installed, or ffmpeg missing a shared library) must not have
    its error text reported as if it were a real version string."""
    monkeypatch.setattr(doctor.shutil, "which", lambda bin: "/usr/bin/widget")
    monkeypatch.setattr(doctor.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 1, stdout="", stderr="widget: error, no runtime found\n",
    ))

    result = doctor.check_tool(TOOL)

    assert result["available"] is True
    assert result["version"] is None
    assert result["warning"] == "widget: error, no runtime found"


def test_check_tool_stays_available_when_version_flag_is_unrecognized(monkeypatch):
    """A tool-specific version flag digtk guessed wrong must not flip a present
    binary to 'missing' — availability comes from shutil.which() alone."""
    monkeypatch.setattr(doctor.shutil, "which", lambda bin: "/usr/bin/widget")

    def raise_oserror(argv, **kwargs):
        raise OSError("boom")
    monkeypatch.setattr(doctor.subprocess, "run", raise_oserror)

    result = doctor.check_tool(TOOL)

    assert result["available"] is True
    assert result["version"] is None


def test_check_tool_handles_version_call_timeout(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda bin: "/usr/bin/widget")

    def raise_timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
    monkeypatch.setattr(doctor.subprocess, "run", raise_timeout)

    result = doctor.check_tool(TOOL)

    assert result["available"] is True
    assert result["version"] is None


def test_check_all_checks_every_registered_tool(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda bin: None)

    results = doctor.check_all()

    assert [r["name"] for r in results] == [t["name"] for t in doctor.TOOLS]
    assert all(r["available"] is False for r in results)
