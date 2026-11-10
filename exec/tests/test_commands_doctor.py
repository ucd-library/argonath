import json

from typer.testing import CliRunner

from digtk.lib import doctor as doctor_lib
from digtk.main import app

runner = CliRunner()


def last_json_line(output):
    return json.loads(output.strip().splitlines()[-1])


def test_check_reports_all_available(monkeypatch):
    monkeypatch.setattr(doctor_lib, "check_all", lambda opts={}: [
        {"name": "tesseract", "bin": "tesseract", "available": True, "path": "/usr/bin/tesseract",
         "version": "tesseract 5.3.0", "warning": None, "used_by": "x", "install_url": "https://example.com"},
    ])

    result = runner.invoke(app, ["doctor", "check"])

    assert result.exit_code == 0
    assert "[OK]      tesseract: /usr/bin/tesseract (tesseract 5.3.0)" in result.output
    body = last_json_line(result.output)
    assert body["status"] == "success"
    assert body["all_available"] is True
    assert body["tools"][0]["name"] == "tesseract"


def test_check_reports_missing_tools_with_install_url(monkeypatch):
    monkeypatch.setattr(doctor_lib, "check_all", lambda opts={}: [
        {"name": "tesseract", "bin": "tesseract", "available": True, "path": "/usr/bin/tesseract",
         "version": None, "warning": None, "used_by": "x", "install_url": "https://example.com/tesseract"},
        {"name": "verapdf", "bin": "verapdf", "available": False, "path": None,
         "version": None, "warning": None, "used_by": "y", "install_url": "https://example.com/verapdf"},
    ])

    result = runner.invoke(app, ["doctor", "check"])

    assert result.exit_code == 0
    assert "[MISSING] verapdf: install from https://example.com/verapdf" in result.output
    body = last_json_line(result.output)
    assert body["all_available"] is False
    assert [t["name"] for t in body["tools"]] == ["tesseract", "verapdf"]


def test_check_reports_available_with_warning(monkeypatch):
    monkeypatch.setattr(doctor_lib, "check_all", lambda opts={}: [
        {"name": "java", "bin": "java", "available": True, "path": "/usr/bin/java",
         "version": None, "warning": "Unable to locate a Java Runtime.",
         "used_by": "z", "install_url": "https://example.com/java"},
    ])

    result = runner.invoke(app, ["doctor", "check"])

    assert result.exit_code == 0
    assert "[OK]      java: /usr/bin/java (warning: Unable to locate a Java Runtime.)" in result.output
