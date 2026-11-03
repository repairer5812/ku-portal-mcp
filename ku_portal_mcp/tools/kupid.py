"""KUPID 공개 포털 도구.

공지·학사일정·장학·검색·도서관·포털 로그인. 함수 본문은 server.py에 있던
코드와 같다. 등록 시 전역 이름은 server 모듈에 묶인다.
"""

from typing import Any

async def kupid_login() -> dict[str, Any]:
    """KUPID 포털에 로그인하고 세션을 확인합니다.

    환경변수 KU_PORTAL_ID, KU_PORTAL_PW가 설정되어 있어야 합니다.
    세션이 유효하면 캐시된 세션을 재사용합니다.
    """
    try:
        session = await _get_session()
        return {
            "success": True,
            "message": "KUPID 로그인 성공",
            "session_valid": session.is_valid,
        }
    except Exception as e:
        clear_session()
        return {"success": False, "message": f"로그인 실패: {e}"}

async def kupid_get_notices(page: int = 1, count: int = 20) -> dict[str, Any]:
    """KUPID 포털의 공지사항 목록을 조회합니다. (로그인 불필요)

    상단 고정 공지가 먼저 오고 그 다음 최신순으로 정렬됩니다.
    무인증 조회는 최신 500건까지만 가능합니다.

    Args:
        page: 페이지 번호 (기본값: 1)
        count: 한 페이지당 항목 수 (기본값: 20)
    """
    try:
        posts, total = await fetch_board_page(BOARD_NOTICE, page=page, count=count)
        return {
            "success": True,
            "count": len(posts),
            "total": total,
            "notices": _format_posts(posts),
        }
    except Exception as e:
        logger.error(f"Failed to fetch notices: {e}")
        return {"success": False, "message": f"공지사항 조회 실패: {e}"}

async def kupid_get_notice_detail(post_seq: int) -> dict[str, Any]:
    """KUPID 공지사항의 상세 정보를 조회합니다. (로그인 불필요)

    차세대 포털은 본문 전문에 로그인을 요구하므로 요약과 원문 링크를 제공합니다.

    Args:
        post_seq: 공지사항 post_seq (kupid_get_notices 결과의 post_seq 필드)
    """
    return await _board_detail(BOARD_NOTICE, post_seq, "공지사항")

async def kupid_get_schedules(
    year: int = 0, semester: int = 0, month: str = ""
) -> dict[str, Any]:
    """고려대학교 학사일정을 조회합니다. (로그인 불필요)

    교무처 학사일정표(registrar.korea.ac.kr)에서 학기별 일정을 가져옵니다.

    Args:
        year: 학년도 (예: 2026). 0이면 현재 학년도
        semester: 학기 (1 또는 2). 0이면 현재 학기
        month: 특정 월만 필터링 (예: "8월"). 비우면 전체
    """
    try:
        resolved_year, resolved_semester = resolve_year_semester(
            str(year) if year else "", str(semester) if semester else ""
        )
        resolved_semester = _normalize_calendar_semester(resolved_semester)
        if resolved_semester not in ("1", "2"):
            return {
                "success": False,
                "message": f"학사일정은 1 또는 2학기만 지원합니다 (입력: {semester})",
            }

        entries, caption = await fetch_academic_schedule(
            resolved_year, resolved_semester
        )

        if month:
            entries = [e for e in entries if e.month == month]

        return {
            "success": True,
            "year": resolved_year,
            "semester": resolved_semester,
            "caption": caption,
            "count": len(entries),
            "schedules": [
                {"month": e.month, "date": e.date, "event": e.event} for e in entries
            ],
        }
    except Exception as e:
        logger.error(f"Failed to fetch schedules: {e}")
        return {"success": False, "message": f"학사일정 조회 실패: {e}"}

async def kupid_get_scholarships(page: int = 1, count: int = 20) -> dict[str, Any]:
    """KUPID 포털의 장학공지 목록을 조회합니다. (로그인 불필요)

    Args:
        page: 페이지 번호 (기본값: 1)
        count: 한 페이지당 항목 수 (기본값: 20)
    """
    try:
        posts, total = await fetch_board_page(BOARD_SCHOLARSHIP, page=page, count=count)
        return {
            "success": True,
            "count": len(posts),
            "total": total,
            "scholarships": _format_posts(posts),
        }
    except Exception as e:
        logger.error(f"Failed to fetch scholarships: {e}")
        return {"success": False, "message": f"장학공지 조회 실패: {e}"}

async def kupid_get_scholarship_detail(post_seq: int) -> dict[str, Any]:
    """KUPID 장학공지의 상세 정보를 조회합니다. (로그인 불필요)

    차세대 포털은 본문 전문에 로그인을 요구하므로 요약과 원문 링크를 제공합니다.

    Args:
        post_seq: 장학공지 post_seq (kupid_get_scholarships 결과의 post_seq 필드)
    """
    return await _board_detail(BOARD_SCHOLARSHIP, post_seq, "장학공지")

async def kupid_search(
    keyword: str, board: str = "all", count: int = 20
) -> dict[str, Any]:
    """KUPID 포털 게시판에서 키워드로 검색합니다. (로그인 불필요)

    제목 또는 요약에 키워드가 포함된 항목을 반환합니다.
    학사일정은 게시판이 아니므로 kupid_get_schedules를 사용하세요.

    Args:
        keyword: 검색할 키워드
        board: 검색 대상 ("all", "notice", "scholarship")
        count: 최대 결과 수 (기본값: 20)
    """
    try:
        boards = {
            "notice": BOARD_NOTICE,
            "scholarship": BOARD_SCHOLARSHIP,
        }
        if board == "all":
            target_ids = list(boards.values())
        elif board in boards:
            target_ids = [boards[board]]
        else:
            return {
                "success": False,
                "message": f"잘못된 board: {board}. all/notice/scholarship 중 선택",
            }

        matches = await search_boards(keyword, target_ids, limit=MAX_LIST_SIZE)
        results = [
            {
                "board": BOARD_NAMES.get(board_id, str(board_id)),
                **_format_posts([post])[0],
            }
            for board_id, post in matches[:count]
        ]

        return {
            "success": True,
            "keyword": keyword,
            "count": len(results),
            "results": results,
        }
    except Exception as e:
        logger.error(f"Failed to search: {e}")
        return {"success": False, "message": f"검색 실패: {e}"}

async def kupid_get_library_seats(library_name: str = "") -> dict[str, Any]:
    """고려대학교 도서관 열람실 좌석 현황을 조회합니다.

    인증 없이 실시간 좌석 현황을 확인할 수 있습니다.

    Args:
        library_name: 도서관 이름 필터 (빈 문자열이면 전체 도서관 조회)
            - 중앙도서관, 중앙광장, 백주년기념 학술정보관, 과학도서관, 하나스퀘어, 법학도서관
    """
    try:
        if library_name:
            # Find matching library code
            code = None
            for c, name in LIBRARY_CODES.items():
                if library_name in name or name in library_name:
                    code = c
                    break
            if not code:
                return {
                    "success": False,
                    "message": f"도서관을 찾을 수 없습니다: {library_name}",
                    "available_libraries": list(LIBRARY_CODES.values()),
                }
            rooms = await fetch_library_seats(code)
            library_data = {LIBRARY_CODES[code]: [asdict(r) for r in rooms]}
        else:
            all_data = await fetch_all_seats()
            library_data = {
                name: [asdict(r) for r in rooms] for name, rooms in all_data.items()
            }

        # Calculate totals
        total_seats = 0
        total_available = 0
        total_in_use = 0
        for rooms in library_data.values():
            for room in rooms:
                total_seats += room["total_seats"]
                total_available += room["available"]
                total_in_use += room["in_use"]

        return {
            "success": True,
            "libraries": library_data,
            "summary": {
                "total_seats": total_seats,
                "total_available": total_available,
                "total_in_use": total_in_use,
                "occupancy_rate": f"{(total_in_use / total_seats * 100):.1f}%"
                if total_seats
                else "0%",
            },
        }
    except Exception as e:
        logger.error(f"Failed to fetch library seats: {e}")
        return {"success": False, "message": f"도서관 좌석 조회 실패: {e}"}

TOOLS = [
    kupid_login,
    kupid_get_notices,
    kupid_get_notice_detail,
    kupid_get_schedules,
    kupid_get_scholarships,
    kupid_get_scholarship_detail,
    kupid_search,
    kupid_get_library_seats,
]
