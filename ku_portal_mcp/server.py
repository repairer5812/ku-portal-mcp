"""KUPID Portal MCP Server.

FastMCP 인스턴스, 세션, 응답 매핑, 진입점을 둔다.
도구 32개의 본문은 ku_portal_mcp.tools 의 도메인 모듈에 있다.

Provides tools for accessing Korea University portal (KUPID):
- Login/session management
- Notice board (bulletin API b=6, no auth)
- Academic schedule (registrar.korea.ac.kr, no auth)
- Scholarship notices (bulletin API b=10, no auth)
- Search across all boards
- Library seat availability
- Academic records via AMS (수강신청/시간표/성적/개설과목/강의실, 2차 인증 필요)
- Canvas LMS (mylms.korea.ac.kr) integration
"""

import asyncio
import base64
import re
import sys
import logging
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, TextContent

# 이 임포트와 아래 헬퍼는 도구 함수의 전역 이름이다. tools/가 이 모듈
# 네임스페이스로 실행되므로, 도구에서만 쓰는 이름도 여기서 지우면 안 된다.
from . import ams
from .academic import resolve_year_semester
from .auth import login, clear_session, make_client, Session
from .portal_api import (
    BoardPost,
    BOARD_NAMES,
    BOARD_NOTICE,
    BOARD_SCHOLARSHIP,
    MAX_LIST_SIZE,
    fetch_academic_schedule,
    fetch_board,
    fetch_board_page,
    fetch_post_detail,
    search_boards,
)
from .library import (
    fetch_library_seats,
    fetch_all_seats,
    LIBRARY_CODES,
)
from .timetable import (
    TimetableEntry,
    resolve_period_time,
    timetable_to_ics,
)
from .dept_notices import fetch_dept_notice_list, fetch_dept_notice_detail
from .dept_registry import resolve_site, list_all_sites, DEFAULT_SITES
from .lms import (
    lms_login,
    LMSSession,
    _clear_lms_session,
    fetch_lms_courses,
    fetch_lms_assignments,
    fetch_lms_modules,
    fetch_lms_todo,
    fetch_lms_upcoming_events,
    fetch_lms_dashboard,
    fetch_lms_announcements,
    fetch_lms_grades,
    fetch_lms_submissions,
    fetch_lms_quizzes,
    fetch_lms_syllabus,
    download_lms_file,
    fetch_lms_boards,
    fetch_lms_board_posts,
    fetch_lms_board_post,
)
from . import __version__

# Load .env file (looks in cwd, then project root)
load_dotenv()

# Logging setup — file logging only to keep stdout clean for MCP protocol
log_dir = Path.home() / ".cache" / "ku-portal-mcp"
log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(log_dir / "server.log")],
)
logger = logging.getLogger(__name__)

server = FastMCP(
    "KU Portal",
    dependencies=["httpx", "beautifulsoup4", "lxml"],
)

# Module-level session cache with async locks to prevent race conditions
_session: Session | None = None
_lms_session: LMSSession | None = None
_session_lock = asyncio.Lock()
_lms_session_lock = asyncio.Lock()

# Errors that may indicate a stale session (worth retrying with fresh login).
# Deliberately excludes KeyError/IndexError/AttributeError: those signal a
# response-schema/parsing bug, not an expired session, so re-logging in would
# only repeat the same failure (and risk account lockout from rapid re-auth).
# HTTPError covers 401/403 + network blips; ValueError/RuntimeError cover the
# portal returning a login page that then fails to parse.
_RETRIABLE = (
    httpx.HTTPError,
    ValueError,
    RuntimeError,
)


async def _get_session() -> Session:
    """Get or create a valid session. Proactive refresh near expiry."""
    global _session
    async with _session_lock:
        if _session and _session.is_valid and not _session.should_refresh:
            return _session
        if _session and _session.should_refresh:
            logger.info("KUPID session near expiry, proactively refreshing")
        elif _session:
            logger.info("KUPID session expired, re-logging in")
        _session = await login()
        return _session


def _format_posts(posts: list[BoardPost]) -> list[dict]:
    return [
        {
            "post_seq": post.post_seq,
            "title": post.title,
            "date": post.date,
            "writer": post.writer,
            "department": post.department,
            "views": post.views,
            "is_notice": post.is_notice,
            "attachments": post.attachments,
            "comments": post.comments,
            "summary": post.summary,
            "url": post.url,
        }
        for post in posts
    ]


async def _find_post(board_id: int, post_seq: int) -> BoardPost | None:
    """게시판 목록에서 post_seq에 해당하는 글을 찾는다.

    무인증으로는 게시글 단건 조회 API가 없어 목록에서 선형 탐색한다.
    """
    posts, _ = await fetch_board(board_id, limit=MAX_LIST_SIZE)
    for post in posts:
        if post.post_seq == post_seq:
            return post
    return None


def _normalize_calendar_semester(semester: str) -> str:
    """학사일정표는 정규학기(1/2)만 지원하므로 계절학기를 인접 학기로 매핑한다.

    학사일정표의 2학기는 8월~다음해 1월을 포함하므로, 여름(7~8월)과
    겨울(1~2월) 계절학기는 모두 해당 학년도 2학기 표에 속한다.
    """
    return "2" if semester in ("summer", "winter") else semester


async def _board_detail(board_id: int, post_seq: int, label: str) -> dict[str, Any]:
    """게시글 상세를 반환한다.

    포털 로그인이 되면 본문 전문과 첨부파일을, 실패하면 목록의 요약을 반환한다.
    """
    try:
        post = await _find_post(board_id, post_seq)
        if not post:
            return {
                "success": False,
                "message": (
                    f"post_seq={post_seq}인 {label}을(를) 최근 {MAX_LIST_SIZE}건에서 "
                    f"찾지 못했습니다. 오래된 글은 무인증 조회 범위를 벗어납니다."
                ),
            }
    except Exception as e:
        logger.error(f"Failed to fetch {label} list: {e}")
        return {"success": False, "message": f"{label} 조회 실패: {e}"}

    summary = _format_posts([post])[0]

    try:
        session = await _get_session()
        async with make_client(session) as client:
            detail = await fetch_post_detail(client, post.url)
    except Exception as e:
        # 로그인 없이도 목록 수준 정보는 돌려준다.
        logger.warning(f"{label} 본문 조회 실패, 요약으로 대체: {e}")
        return {
            "success": True,
            "detail": summary,
            "note": (
                f"본문 전문은 포털 로그인이 필요합니다 ({e}). "
                "요약(summary)과 원문 링크(url)만 제공됩니다."
            ),
        }

    return {
        "success": True,
        "detail": {
            **summary,
            # 팝업 페이지에는 작성자 이름이 없어 목록 값을 유지한다.
            "title": detail.title or summary["title"],
            "date": detail.date or summary["date"],
            "department": detail.department or summary["department"],
            "approver": detail.approver,
            "views": detail.views or summary["views"],
            "content": detail.content,
            "attachments": detail.attachments,
        },
    }


# AMS 시간표 격자의 요일 접두어 → 표시 이름
_DAY_LABELS = {
    "mon": "월",
    "tue": "화",
    "wed": "수",
    "thu": "목",
    "fri": "금",
    "sat": "토",
}


async def _resolve_ams_term(
    session: ams.AmsSession, year: str = "", semester: str = ""
) -> str:
    """학년도/학기를 AMS 학기 코드(예: 20261R)로 바꾼다.

    조회 가능한 학기만 서버가 알려주므로, 지정된 값이 목록에 없으면
    가장 최근 학기로 되돌린다.
    """
    terms = await ams.fetch_terms(session)
    if not terms:
        raise RuntimeError("조회 가능한 학기가 없습니다.")

    if year and semester:
        wanted = f"{year}{semester}R"
        for term in terms:
            if term.get("code") == wanted:
                return term["code"]
        logger.warning(f"{wanted} 학기를 찾지 못해 최근 학기로 조회합니다")

    return terms[0]["code"]


def _sum_credits(rows: list[dict]) -> float:
    """'2.0(2)' 형태의 학점 문자열을 합산한다."""
    total = 0.0
    for row in rows:
        raw = (row.get("cdtTime") or "").split("(")[0].strip()
        try:
            total += float(raw)
        except ValueError:
            continue
    return round(total, 1)


def _cell_classroom(cell: str) -> str:
    """격자 칸에서 강의실만 뽑는다.

    칸은 '<학수번호-분반><br><과목명><br><교수><br><강의실>' 형태로 온다.
    """
    parts = [p.strip() for p in re.split(r"<br\s*/?>", cell or "") if p.strip()]
    return parts[-1] if len(parts) > 1 else ""


def _grid_to_entries(grid: list[dict]) -> list[TimetableEntry]:
    """AMS 시간표 격자(교시 행 × 요일 열)를 시간표 항목 목록으로 편다."""
    entries = []
    for row in grid:
        period = (row.get("timeTime") or "").replace("교시", "").strip()
        start, end = resolve_period_time(period)
        for prefix, label in _DAY_LABELS.items():
            subject = row.get(f"{prefix}SubjtNm")
            if not subject:
                continue
            entries.append(
                TimetableEntry(
                    day_of_week=label,
                    period=period,
                    subject_name=subject,
                    classroom=_cell_classroom(row.get(f"{prefix}Nm") or ""),
                    start_time=start,
                    end_time=end,
                )
            )
    return entries


async def _get_ams_session() -> ams.AmsSession:
    """AMS 세션을 가져온다. 없으면 2차 인증이 필요하다고 알린다."""
    session = ams.load_session()
    if session and session.is_valid:
        return session
    raise RuntimeError(
        "AMS(학사) 2차 보안인증이 필요합니다. "
        "kupid_ams_auth_start()로 인증 코드를 받은 뒤 "
        "kupid_ams_auth_verify(code)로 인증을 완료하세요."
    )


def _resolve_syllabus_term(
    year: str, semester: str, today: date | None = None
) -> tuple[str, str]:
    """학년도와 AMS 학기 코드(1R/2R)를 정한다.

    계절학기에는 강의계획서가 없다시피 하므로, 방학 중이면 다가오는 정규
    학기로 넘긴다(여름 → 그 해 2학기, 겨울 → 다음 학년도 1학기). 호출자가
    "1R" 같은 코드를 직접 주면 그대로 쓴다.
    """
    resolved_year, resolved_semester = resolve_year_semester(
        year or None, semester or None, today=today
    )

    if resolved_semester == "summer":
        return resolved_year, "2R"
    if resolved_semester == "winter":
        return str(int(resolved_year) + 1), "1R"

    return resolved_year, {"1": "1R", "2": "2R"}.get(
        resolved_semester, resolved_semester
    )


async def _get_lms_session() -> LMSSession:
    """Get or create a valid LMS session. Proactive refresh near expiry."""
    global _lms_session
    async with _lms_session_lock:
        if _lms_session and _lms_session.is_valid and not _lms_session.should_refresh:
            return _lms_session
        if _lms_session and _lms_session.should_refresh:
            logger.info("LMS session near expiry, proactively refreshing")
        elif _lms_session:
            logger.info("LMS session expired, re-logging in")
        import os

        user_id = os.environ.get("KU_PORTAL_ID", "")
        password = os.environ.get("KU_PORTAL_PW", "")
        _lms_session = await lms_login(user_id, password)
        return _lms_session


async def _lms_with_retry(fn, *args, **kwargs):
    """Execute LMS API call with auto re-login on auth failure."""
    try:
        session = await _get_lms_session()
        return await fn(session, *args, **kwargs)
    except _RETRIABLE as e:
        logger.warning(
            f"LMS request failed ({type(e).__name__}: {e}), retrying with fresh session"
        )
        global _lms_session
        async with _lms_session_lock:
            _clear_lms_session()
            _lms_session = None
        session = await _get_lms_session()
        return await fn(session, *args, **kwargs)


# 헬퍼가 모두 정의된 뒤에 등록한다. 도구 본문은 이 모듈의 전역 이름을 쓴다.
from .tools import register_all

register_all(server, globals())


def main():
    if "--version" in sys.argv or "-V" in sys.argv:
        print(f"ku-portal-mcp {__version__}")
        return
    if "--help" in sys.argv or "-h" in sys.argv:
        print("ku-portal-mcp - Korea University KUPID Portal MCP Server")
        print(f"Version: {__version__}")
        print("\nUsage:")
        print("  ku-portal-mcp          Start MCP server (stdio)")
        print("  ku-portal-mcp --version  Show version")
        print("\nEnvironment variables:")
        print("  KU_PORTAL_ID    KUPID portal ID")
        print("  KU_PORTAL_PW    KUPID portal password")
        print("  KU_GRAD_DEPT    Syllabus college code (optional; no built-in default)")
        return

    try:
        logger.info(f"Starting KU Portal MCP Server v{__version__}...")
        logger.info(f"Python: {sys.version}")
        server.run()
    except Exception as e:
        logger.error(f"Server error: {e}")
        import traceback

        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
