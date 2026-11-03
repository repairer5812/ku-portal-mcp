"""저장된 HTML 픽스처로 순수 파서를 고정한다. 학교 서버는 호출하지 않는다."""

from pathlib import Path

import pytest

from ku_portal_mcp.ams import _login_forms
from ku_portal_mcp.dept_notices import parse_dept_notice_detail, parse_dept_notice_list
from ku_portal_mcp.portal_api import PORTAL_BASE, parse_academic_schedule, parse_post_detail
from ku_portal_mcp.sso import LoginPage, _find_auto_submit_form, encrypt_password, parse_login_page

FIXTURES = Path(__file__).parent / "fixtures"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_login_page_reads_key_salt_and_multiline_token():
    page = parse_login_page(_html("sso_login.html"), "https://sso.example/login")

    assert page == LoginPage(
        aes_key=b"0123456789abcdef",
        salt="fixture-salt",
        l_token="token-line1\ntoken-line2",
        url="https://sso.example/login",
    )


def test_parse_login_page_rejects_missing_salt():
    with pytest.raises(RuntimeError, match="salt"):
        parse_login_page(_html("sso_login_missing_salt.html"), "https://sso.example/login")


def test_parse_login_page_rejects_unexpected_key_length():
    with pytest.raises(RuntimeError, match="15바이트"):
        parse_login_page(_html("sso_login_bad_key.html"), "https://sso.example/login")


def test_parse_login_page_rejects_missing_token():
    with pytest.raises(RuntimeError, match="l_token"):
        parse_login_page(_html("sso_login_missing_token.html"), "https://sso.example/login")


def test_encrypt_password_matches_golden_vector_for_fixed_iv():
    page = parse_login_page(_html("sso_login.html"), "https://sso.example/login")
    token = encrypt_password("secret", page, iv=b"\x00" * 16)
    assert token == "H6f/CPAKia4XEYhCIMI53eTspYoz/oXt32aD9oKB/S8=|AAAAAAAAAAAAAAAAAAAAAA=="


def test_parse_post_detail_keeps_inline_spans_inside_words():
    detail = parse_post_detail(_html("post_detail.html"), "https://portal.example/post/1")

    assert detail.title == "2026학년도 수강신청 안내"
    assert detail.writer == "권혜정"
    assert detail.department == "차세대정보화추진팀"
    assert detail.approver == "홍길동"
    assert detail.date == "2026-03-01"
    assert detail.views == 1028
    assert detail.content == (
        "이번 2차 수강신청은 3월 4일부터입니다.\n\n문의는 학과사무실로 연락하세요."
    )
    assert "2\n차" not in detail.content
    assert detail.attachments == [
        {
            "name": "안내문",
            "size": "28KB",
            "url": f"{PORTAL_BASE}/files/guide.pdf",
        }
    ]


def test_parse_dept_notice_list_five_columns_date_before_views():
    items = parse_dept_notice_list(_html("dept_notice_5col_date_first.html"))
    assert len(items) == 1
    notice = items[0]
    assert notice.article_no == "101"
    assert notice.title == "수강신청 안내"
    assert notice.writer == "학과사무실"
    assert notice.date == "2026.03.01"
    assert notice.views == "123"
    assert notice.is_pinned is True
    assert notice.has_attachment is True


def test_parse_dept_notice_list_five_columns_views_before_date():
    items = parse_dept_notice_list(_html("dept_notice_5col_views_first.html"))
    notice = items[0]
    assert notice.article_no == "202"
    assert notice.date == "2026-02-11"
    assert notice.views == "45"
    assert notice.is_pinned is False
    assert notice.has_attachment is False


def test_parse_dept_notice_list_four_columns():
    items = parse_dept_notice_list(_html("dept_notice_4col.html"))
    notice = items[0]
    assert notice.article_no == "303"
    assert notice.writer == "조교"
    assert notice.date == "2026/01/05"
    assert notice.views == "7"
    assert notice.is_pinned is True
    assert notice.has_attachment is True


def test_parse_dept_notice_detail_collapses_blank_lines_and_resolves_files():
    detail = parse_dept_notice_detail(
        _html("dept_notice_detail.html"),
        "https://cs.example.edu/board/notice.do",
        "101",
    )
    assert detail.title == "수강신청 안내"
    assert detail.content == "첫째 줄\n\n둘째 줄"
    assert detail.attachments == [
        {
            "name": "안내.pdf",
            "url": "https://cs.example.edu/board/download.do?mode=download&articleNo=101",
        },
        {
            "name": "외부자료.pdf",
            "url": "https://files.example.edu/extra.pdf?mode=download",
        },
    ]
    assert detail.url == "https://cs.example.edu/board/notice.do?mode=view&articleNo=101"


def test_parse_academic_schedule_carries_rowspan_month():
    entries, caption = parse_academic_schedule(_html("academic_schedule.html"))
    assert caption == "2026학년도 1학기 학사일정"
    assert [(e.month, e.date, e.event) for e in entries] == [
        ("3월", "2(월)", "개강"),
        ("3월", "4(수)~6(금)", "수강신청 정정"),
        ("4월", "1(수)", "수업일수 1/4"),
    ]


def test_login_forms_skip_logout_and_actionless_forms():
    forms = _login_forms(_html("ams_login_forms.html"))
    assert [form.get("action") for form in forms] == ["/exsignon/sso/j_login_sso.do"]


def test_auto_submit_form_skips_login_fields_and_requires_submit_call():
    form = _find_auto_submit_form(_html("sso_auto_submit.html"))
    assert form is not None
    assert form.get("action") == "/sso/relay"
    assert _find_auto_submit_form(_html("sso_no_auto_submit.html")) is None
