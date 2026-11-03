"""도메인별 MCP 도구.

도구 함수의 본문은 각 모듈에 두고, 등록할 때 ``server`` 모듈의 전역
네임스페이스에 묶는다. 그래서 세션 헬퍼와 테스트의 monkeypatch 지점은
분리 전과 같다. 함수를 이 패키지에서 직접 호출하면 그 전역 이름이 없다.
"""

import types

# 분리 전과 같은 등록 순서.
_ORDER = (
    "kupid_ams_auth_start",
    "kupid_ams_auth_verify",
    "kupid_login",
    "kupid_get_notices",
    "kupid_get_notice_detail",
    "kupid_get_schedules",
    "kupid_get_scholarships",
    "kupid_get_scholarship_detail",
    "kupid_search",
    "kupid_get_library_seats",
    "kupid_get_timetable",
    "kupid_search_courses",
    "kupid_syllabus",
    "kupid_room_schedule",
    "kupid_my_courses",
    "kupid_get_all_grades",
    "kupid_dept_notices",
    "kupid_dept_notice_detail",
    "kupid_lms_courses",
    "kupid_lms_assignments",
    "kupid_lms_modules",
    "kupid_lms_todo",
    "kupid_lms_dashboard",
    "kupid_lms_announcements",
    "kupid_lms_grades",
    "kupid_lms_submissions",
    "kupid_lms_quizzes",
    "kupid_lms_syllabus",
    "kupid_lms_download_file",
    "kupid_lms_list_boards",
    "kupid_lms_list_board_posts",
    "kupid_lms_get_board_post",
)


def adopt(fn, namespace: dict):
    """함수 본문은 유지하고 전역 이름만 namespace에서 찾게 한다."""
    adopted = types.FunctionType(
        fn.__code__,
        namespace,
        fn.__name__,
        fn.__defaults__,
        fn.__closure__,
    )
    adopted.__doc__ = fn.__doc__
    adopted.__annotations__ = dict(fn.__annotations__)
    adopted.__kwdefaults__ = fn.__kwdefaults__
    adopted.__module__ = fn.__module__
    adopted.__qualname__ = fn.__qualname__
    return adopted


def register_all(mcp, namespace: dict) -> None:
    from .ams import TOOLS as ams_tools
    from .canvas import TOOLS as canvas_tools
    from .dept import TOOLS as dept_tools
    from .kupid import TOOLS as kupid_tools

    by_name = {
        fn.__name__: fn
        for fn in (*kupid_tools, *ams_tools, *dept_tools, *canvas_tools)
    }
    if set(by_name) != set(_ORDER):
        missing = sorted(set(_ORDER) - set(by_name))
        extra = sorted(set(by_name) - set(_ORDER))
        raise RuntimeError(f"도구 목록 불일치 missing={missing} extra={extra}")

    for name in _ORDER:
        mcp.tool()(adopt(by_name[name], namespace))
