"""도구 32개의 이름과 인자 스키마를 고정한다.

도메인 모듈로 옮겨도 MCP 클라이언트가 보는 계약은 같아야 한다.
"""

import asyncio

import ku_portal_mcp.server as server_module

# 이름 → (필수 인자, {인자: 기본값}). 기본값이 없으면 None이 아니라 키를 두지 않는다.
EXPECTED_TOOLS = {
    "kupid_ams_auth_start": ([], {}),
    "kupid_ams_auth_verify": (["code"], {}),
    "kupid_login": ([], {}),
    "kupid_get_notices": ([], {"page": 1, "count": 20}),
    "kupid_get_notice_detail": (["post_seq"], {}),
    "kupid_get_schedules": ([], {"year": 0, "semester": 0, "month": ""}),
    "kupid_get_scholarships": ([], {"page": 1, "count": 20}),
    "kupid_get_scholarship_detail": (["post_seq"], {}),
    "kupid_search": (["keyword"], {"board": "all", "count": 20}),
    "kupid_get_library_seats": ([], {"library_name": ""}),
    "kupid_get_timetable": (
        [],
        {"day": "all", "ics_export": False, "year": "", "semester": ""},
    ),
    "kupid_search_courses": (["subject"], {"campus": ""}),
    "kupid_syllabus": (
        ["course_code"],
        {"year": "", "semester": "", "section": "00", "grad_dept": ""},
    ),
    "kupid_room_schedule": (["subject"], {"building": "", "room": ""}),
    "kupid_my_courses": ([], {"year": "", "semester": ""}),
    "kupid_get_all_grades": ([], {"year_term": ""}),
    "kupid_dept_notices": ([], {"site_name": "", "page": 1, "count": 20}),
    "kupid_dept_notice_detail": (["site_name", "article_no"], {}),
    "kupid_lms_courses": ([], {}),
    "kupid_lms_assignments": (["course_id"], {"upcoming_only": False}),
    "kupid_lms_modules": (["course_id"], {}),
    "kupid_lms_todo": ([], {}),
    "kupid_lms_dashboard": ([], {}),
    "kupid_lms_announcements": ([], {"course_id": None}),
    "kupid_lms_grades": (["course_id"], {}),
    "kupid_lms_submissions": (["course_id"], {}),
    "kupid_lms_quizzes": (["course_id"], {}),
    "kupid_lms_syllabus": ([], {"course_code": "", "course_id": 0}),
    "kupid_lms_download_file": (["file_id", "save_dir"], {"filename": ""}),
    "kupid_lms_list_boards": (["course_id"], {}),
    "kupid_lms_list_board_posts": (
        ["course_id", "board_id"],
        {"page": 1, "keyword": ""},
    ),
    "kupid_lms_get_board_post": (["course_id", "board_id", "post_id"], {}),
}


def test_registered_tools_match_name_and_signature_snapshot():
    tools = asyncio.run(server_module.server.list_tools())
    actual = {}
    for tool in tools:
        schema = tool.inputSchema or {}
        properties = schema.get("properties") or {}
        defaults = {
            name: spec.get("default")
            for name, spec in properties.items()
            if "default" in spec
        }
        actual[tool.name] = (list(schema.get("required") or []), defaults)

    assert actual == EXPECTED_TOOLS
    assert len(actual) == 32
