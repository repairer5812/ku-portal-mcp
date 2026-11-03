"""강의계획서는 특정 대학원 코드를 기본값으로 조회하지 않는다."""

import asyncio
import inspect
from pathlib import Path

import pytest

from ku_portal_mcp import ams


def test_product_code_does_not_hardcode_grad_dept_7298():
    for path in Path("ku_portal_mcp").rglob("*.py"):
        assert "7298" not in path.read_text(encoding="utf-8"), path
    assert not hasattr(ams, "GSCIT_GRAD_DEPT")


def test_fetch_syllabus_grad_dept_is_required_keyword():
    param = inspect.signature(ams.fetch_syllabus).parameters["grad_dept"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


def test_fetch_syllabus_rejects_blank_grad_dept_without_network():
    with pytest.raises(ValueError, match="grad_dept"):
        asyncio.run(ams.fetch_syllabus("COSE101", "2026", "1R", grad_dept="  "))


def test_configured_grad_dept_prefers_argument_over_environment(monkeypatch):
    monkeypatch.setenv("KU_GRAD_DEPT", "7298")
    assert ams.configured_grad_dept(" 7309 ") == "7309"
    assert ams.configured_grad_dept("") == "7298"
    monkeypatch.delenv("KU_GRAD_DEPT")
    assert ams.configured_grad_dept("  ") == ""
