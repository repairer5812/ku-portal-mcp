"""강의계획서 대학원 코드는 미설정 시 7298, 설정 시 그 값을 쓴다."""

import asyncio
import inspect

import pytest

from ku_portal_mcp import ams


def test_fetch_syllabus_grad_dept_is_required_keyword():
    param = inspect.signature(ams.fetch_syllabus).parameters["grad_dept"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty


def test_fetch_syllabus_rejects_blank_grad_dept_without_network():
    with pytest.raises(ValueError, match="grad_dept"):
        asyncio.run(ams.fetch_syllabus("COSE101", "2026", "1R", grad_dept="  "))


def test_configured_grad_dept_defaults_to_swai_when_unset(monkeypatch):
    """KU_GRAD_DEPT가 없으면 대학원 코드는 SW·AI융합대학원 7298이다."""
    monkeypatch.delenv("KU_GRAD_DEPT", raising=False)
    assert ams.configured_grad_dept() == "7298"
    assert ams.configured_grad_dept("  ") == "7298"


def test_configured_grad_dept_uses_environment_when_set(monkeypatch):
    monkeypatch.setenv("KU_GRAD_DEPT", "7001")
    assert ams.configured_grad_dept() == "7001"
    assert ams.configured_grad_dept("") == "7001"


def test_configured_grad_dept_prefers_argument_over_environment(monkeypatch):
    monkeypatch.setenv("KU_GRAD_DEPT", "7298")
    assert ams.configured_grad_dept(" 7309 ") == "7309"
    assert ams.configured_grad_dept("") == "7298"
    monkeypatch.delenv("KU_GRAD_DEPT")
    assert ams.configured_grad_dept("  ") == "7298"
