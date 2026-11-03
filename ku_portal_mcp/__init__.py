"""Korea University KUPID portal MCP server."""

import re
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def _package_version() -> str:
    """배포 메타데이터의 버전을 읽고, 미설치 소스 트리는 pyproject.toml로 폴백한다."""
    try:
        return version("ku-portal-mcp")
    except PackageNotFoundError:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
        if not match:
            raise PackageNotFoundError("ku-portal-mcp") from None
        return match.group(1)


__version__ = _package_version()
