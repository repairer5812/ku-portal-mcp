"""배포 버전은 pyproject.toml 한 곳에서만 온다."""

import re
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import ku_portal_mcp
from ku_portal_mcp.server import main


def _pyproject_version() -> str:
    text = Path("pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert match, "pyproject.toml에 version이 없습니다"
    return match.group(1)


def test_version_matches_installed_metadata_and_pyproject():
    expected = _pyproject_version()
    assert ku_portal_mcp.__version__ == expected
    assert version("ku-portal-mcp") == expected
    assert expected != "0.18.1"


def test_version_falls_back_to_pyproject_when_package_is_not_installed(monkeypatch):
    def missing(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr(ku_portal_mcp, "version", missing)
    assert ku_portal_mcp._package_version() == _pyproject_version()


def test_cli_version_prints_package_version(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["ku-portal-mcp", "--version"])
    main()
    assert f"ku-portal-mcp {ku_portal_mcp.__version__}" in capsys.readouterr().out
