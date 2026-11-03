"""AMS 학사 도구.

2차 인증, 시간표, 개설과목, 강의계획서, 강의실, 수강, 성적.
함수 본문은 server.py에 있던 코드와 같다. 등록 시 전역 이름은 server 모듈에 묶인다.
"""

from typing import Any

async def kupid_ams_auth_start() -> dict[str, Any]:
    """학사 시스템(AMS) 2차 보안인증을 시작해 이메일로 인증 코드를 보냅니다.

    수강신청내역·시간표·성적 조회는 학교 정책상 2차 보안인증이 필요합니다.
    이 tool을 호출하면 6자리 코드가 발송되며(5분 유효),
    kupid_ams_auth_verify(code)로 인증을 마치면 약 50분간 세션이 유지됩니다.

    코드는 KUPID 포털에 등록된 이메일로 갑니다. 발송 주소는 이 서버가 아니라
    학교가 정하며, 응답에는 마스킹된 주소만 담깁니다. 사용자가 따로 등록할
    것은 없고, 바꾸려면 포털 > My Page > 개인정보 수정에서 하면 됩니다.
    메일이 보이지 않으면 스팸함을 확인하도록 안내하세요.
    """
    try:
        session = ams.load_session()
        if session and session.is_valid and await ams.verify_session(session):
            return {
                "success": True,
                "already_authenticated": True,
                "message": "이미 인증된 세션이 있습니다. 바로 조회할 수 있습니다.",
            }

        masked_email = await ams.start_login()
        return {
            "success": True,
            "already_authenticated": False,
            "masked_email": masked_email,
            "message": (
                f"{masked_email or '등록된 메일'}로 6자리 인증 코드를 보냈습니다. "
                "5분 안에 kupid_ams_auth_verify(code)로 입력하세요."
            ),
        }
    except Exception as e:
        logger.error(f"AMS auth start failed: {e}")
        return {"success": False, "message": f"AMS 인증 시작 실패: {e}"}

async def kupid_ams_auth_verify(code: str) -> dict[str, Any]:
    """이메일로 받은 6자리 코드로 학사 시스템(AMS) 인증을 완료합니다.

    Args:
        code: 메일로 받은 6자리 인증 코드
    """
    try:
        await ams.complete_login(code)
        return {
            "success": True,
            "message": "AMS 인증 완료. 수강신청내역·시간표·성적을 조회할 수 있습니다.",
        }
    except Exception as e:
        logger.error(f"AMS auth verify failed: {e}")
        return {"success": False, "message": f"AMS 인증 실패: {e}"}

async def kupid_get_timetable(
    day: str = "all", ics_export: bool = False, year: str = "", semester: str = ""
) -> dict[str, Any]:
    """개인 수업시간표를 조회합니다 (AMS 2차 인증 필요).

    Args:
        day: 요일 ("all"=전체, "mon"/"tue"/"wed"/"thu"/"fri"/"sat")
        ics_export: True이면 ICS 캘린더 파일 내용도 포함
        year: 학년도 (기본값: 현재 학기)
        semester: 학기 ("1" 또는 "2")
    """
    try:
        session = await _get_ams_session()
        term = await _resolve_ams_term(session, year, semester)
        grid = await ams.fetch_timetable(session, term)
        entries = _grid_to_entries(grid)

        if day != "all":
            if day not in _DAY_LABELS:
                return {
                    "success": False,
                    "message": f"잘못된 day: {day}. all/mon/tue/wed/thu/fri/sat 중 선택",
                }
            entries = [e for e in entries if e.day_of_week == _DAY_LABELS[day]]

        result = {
            "success": True,
            "term": term,
            "day": day,
            "count": len(entries),
            "timetable": [
                {
                    "day": e.day_of_week,
                    "period": e.period,
                    "subject": e.subject_name,
                    "classroom": e.classroom,
                    "start_time": e.start_time,
                    "end_time": e.end_time,
                }
                for e in entries
            ],
        }
        if ics_export:
            result["ics"] = timetable_to_ics(entries)
        return result
    except Exception as e:
        logger.error(f"Failed to fetch timetable: {e}")
        return {"success": False, "message": f"시간표 조회 실패: {e}"}

async def kupid_search_courses(subject: str, campus: str = "") -> dict[str, Any]:
    """교과목명으로 개설과목을 검색합니다 (AMS 2차 인증 필요).

    학수번호, 분반, 강의실, 건물, 캠퍼스를 반환합니다. 학사 시스템이 제공하는
    검색 조건이 교과목명뿐이라, 단과대/학과 단위 목록 조회는 지원하지 않습니다.

    Args:
        subject: 교과목명 키워드 (필수, 부분일치)
        campus: 캠퍼스로 필터링 (예: "자연계", "인문사회계")
    """
    try:
        if not subject.strip():
            return {
                "success": False,
                "message": "교과목명 키워드(subject)는 필수입니다.",
            }

        session = await _get_ams_session()
        rows = await ams.fetch_room_guide(session, subject.strip())

        if campus:
            rows = [r for r in rows if campus in (r.get("buldCampsDivNm") or "")]

        return {
            "success": True,
            "keyword": subject,
            "count": len(rows),
            "courses": [
                {
                    "course_code": r.get("sbjtnb") or "",
                    "section": r.get("dvcno") or "",
                    "course_name": r.get("subjtNm") or "",
                    "classroom": r.get("lecrmNm") or "",
                    "building": r.get("buldDivNm") or "",
                    "campus": r.get("buldCampsDivNm") or "",
                    "dept_code": r.get("estblDeprtCd") or "",
                }
                for r in rows
            ],
        }
    except Exception as e:
        logger.error(f"Failed to search courses: {e}")
        return {"success": False, "message": f"개설과목 검색 실패: {e}"}

async def kupid_syllabus(
    course_code: str,
    year: str = "",
    semester: str = "",
    section: str = "00",
    grad_dept: str = "",
) -> dict[str, Any]:
    """학수번호로 강의계획서를 조회합니다 (로그인 불필요).

    평가 비중(중간/기말/과제 %), 주차별 강의계획, 교재·참고문헌, 성적평가
    방식(절대/상대), 강의시간·강의실, 담당교수 연락처를 반환합니다.

    Args:
        course_code: 학수번호 (예: "AAI117", "BDC108")
        year: 학년도 (기본: 현재 학년도)
        semester: 학기 "1" 또는 "2" (기본: 현재 또는 다가오는 정규학기)
        section: 분반 (기본 "00")
        grad_dept: 대학원·단과대 코드. 생략 시 환경변수 KU_GRAD_DEPT.
            둘 다 없으면 조회하지 않습니다. 학과마다 코드가 다릅니다.
    """
    try:
        code = course_code.strip().upper()
        if not code:
            return {"success": False, "message": "학수번호(course_code)는 필수입니다."}

        resolved_dept = ams.configured_grad_dept(grad_dept)
        if not resolved_dept:
            return {
                "success": False,
                "message": (
                    "대학원 코드(grad_dept)가 필요합니다. 인자로 넘기거나 "
                    "KU_GRAD_DEPT 환경변수에 설정하세요. 학과·대학원마다 코드가 "
                    "다르며, 생략하면 조회하지 않습니다."
                ),
            }

        syy, term = _resolve_syllabus_term(year, semester)
        result = await ams.fetch_syllabus(
            code,
            syy,
            term,
            section=section or "00",
            grad_dept=resolved_dept,
        )

        if result is None:
            return {
                "success": False,
                "message": (
                    f"{syy}학년도 {term} {code} 과목을 찾을 수 없습니다. "
                    "미개설이거나 대학원 코드(grad_dept)가 다를 수 있습니다."
                ),
            }

        base = result["base"]
        evaluation = [
            {"item": r.get("evlItemCtnt") or "", "percent": r.get("evlSco") or 0}
            for r in result["evaluation"]
        ]

        return {
            "success": True,
            "course_code": base.get("sbjtnb") or code,
            "course_name": base.get("subjtNm") or "",
            "year": syy,
            "semester": term,
            "section": base.get("dvcno") or section,
            "credits": base.get("cdt"),
            "category": (base.get("cmpsjNm") or "").strip(),
            "department": base.get("estblDeprtNm") or "",
            "schedule": base.get("lctreTimePlaceLisup") or "",
            "professor": {
                "name": base.get("per001KorNm") or "",
                "email": base.get("per001EmailAddr") or "",
                "department": base.get("per001DeptNm") or "",
                "office": base.get("per001WorkPlace") or "",
                "office_hours": base.get("cnslgPosblTimeDesc") or "",
                "homepage": base.get("per001Homepage") or "",
            },
            "grading_method": base.get("gradeEvlMthdNm") or "",
            "grading_note": base.get("gradeEvlMthdDesc") or "",
            "evaluation": evaluation,
            "evaluation_total": sum(e["percent"] for e in evaluation),
            "goal": base.get("lrnGoalCtnt") or "",
            "outline": base.get("profSylblCtnt") or "",
            "prerequisites": base.get("atnlcRqistCtnt") or "",
            "textbook": base.get("txtbkCtnt") or "",
            "references": [
                {
                    "title": r.get("referBookNm") or "",
                    "publisher": r.get("pblcmNm") or "",
                    "isbn": r.get("isbnVal") or "",
                }
                for r in result["references"]
            ],
            "weekly": [
                {
                    "week": r.get("lessnWkOdr"),
                    "content": (r.get("lctreCtnt") or "").strip(),
                    "midterm": r.get("mdtexOprtYn") == "1",
                    "final": r.get("fnlexOprtYn") == "1",
                }
                for r in result["weekly"]
            ],
            "note": (
                "" if evaluation else "담당교수가 아직 평가 비중을 등록하지 않았습니다."
            ),
        }
    except Exception as e:
        logger.error(f"Failed to fetch syllabus: {e!r}")
        # 네트워크 예외는 메시지가 비어 오는 일이 있어 타입을 함께 남긴다.
        return {
            "success": False,
            "message": f"강의계획서 조회 실패: {type(e).__name__}: {e}",
        }

async def kupid_room_schedule(
    subject: str, building: str = "", room: str = ""
) -> dict[str, Any]:
    """교과목명으로 강의실을 조회합니다 (AMS 2차 인증 필요).

    학사 시스템의 강의실안내조회는 교과목명 키워드로만 검색할 수 있어,
    "이 강의실에 무슨 수업이 있나"가 아니라 "이 과목이 어느 강의실인가"를 답합니다.
    건물·호실은 결과를 좁히는 필터로 쓰입니다.

    Args:
        subject: 교과목명 키워드 (필수, 부분일치)
        building: 건물명으로 결과 필터링 (예: "정보통신관")
        room: 강의실명으로 결과 필터링 (예: "604")
    """
    try:
        if not subject.strip():
            return {
                "success": False,
                "message": "교과목명 키워드(subject)는 필수입니다.",
            }

        session = await _get_ams_session()
        rows = await ams.fetch_room_guide(session, subject.strip())

        if building:
            rows = [r for r in rows if building in (r.get("buldDivNm") or "")]
        if room:
            rows = [r for r in rows if room in (r.get("lecrmNm") or "")]

        return {
            "success": True,
            "subject": subject,
            "count": len(rows),
            "rooms": [
                {
                    "course_code": r.get("sbjtnb") or "",
                    "section": r.get("dvcno") or "",
                    "course_name": r.get("subjtNm") or "",
                    "classroom": r.get("lecrmNm") or "",
                    "building": r.get("buldDivNm") or "",
                    "campus": r.get("buldCampsDivNm") or "",
                    "room_type": r.get("lecrmDivNm") or "",
                    "dept_code": r.get("estblDeprtCd") or "",
                }
                for r in rows
            ],
        }
    except Exception as e:
        logger.error(f"Failed to fetch room guide: {e}")
        return {"success": False, "message": f"강의실 조회 실패: {e}"}

async def kupid_my_courses(year: str = "", semester: str = "") -> dict[str, Any]:
    """내 수강신청 내역을 조회합니다 (AMS 2차 인증 필요).

    학수번호, 강의시간, 강의실, 교수, 학점, 이수구분 등 상세 정보를 반환합니다.
    대학원 과목도 포함됩니다.

    Args:
        year: 학년도 (기본값: 현재 학기 기준 자동 선택)
        semester: 학기 ("1"=1학기, "2"=2학기)
    """
    try:
        session = await _get_ams_session()
        term = await _resolve_ams_term(session, year, semester)
        rows = await ams.fetch_enrollment(session, term)

        courses = [
            {
                "course_code": r.get("sbjtnb") or "",
                "section": r.get("dvcno") or "",
                "course_name": r.get("subjtNm") or "",
                "professor": r.get("cgprfNmLisup") or "",
                "credits": r.get("cdtTime") or "",
                "course_type": (r.get("cmpsjNm") or "").strip(),
                "schedule": r.get("lctreTimePlaceLisup") or "",
                "status": r.get("sttusNm") or "",
                "payment": r.get("payDt") or "",
                "dept_code": r.get("estblDeprtCd") or "",
            }
            for r in rows
        ]
        return {
            "success": True,
            "term": term,
            "count": len(courses),
            "total_credits": _sum_credits(rows),
            "courses": courses,
        }
    except Exception as e:
        logger.error(f"Failed to fetch my courses: {e}")
        return {"success": False, "message": f"수강신청 내역 조회 실패: {e}"}

async def kupid_get_all_grades(year_term: str = "") -> dict[str, Any]:
    """전체 성적, 누적 GPA, 취득학점을 조회합니다 (AMS 2차 인증 필요).

    Args:
        year_term: 학년도/학기 코드로 필터 (예: "20261R"). 비우면 전체
    """
    try:
        session = await _get_ams_session()
        rows, summary = await ams.fetch_grades(session)

        if year_term:
            rows = [
                r for r in rows if f"{r.get('syy')}{r.get('smtDivcd')}" == year_term
            ]

        grades = [
            {
                "year": r.get("syy") or "",
                "semester": r.get("smtDivcd") or "",
                "course_code": r.get("sbjtnb") or "",
                "section": r.get("dvcno") or "",
                "course_name": r.get("subjtNm") or "",
                "course_type": r.get("cmpsjDivNm") or "",
                "credits": r.get("cdt"),
                "grade": r.get("gradeGrdDivcd") or "",
                "grade_point": r.get("cmpsjGp"),
                "retake_of": r.get("ratlcSyySmtNm") or "",
            }
            for r in rows
        ]

        acmtl = summary[0] if summary else {}
        return {
            "success": True,
            "count": len(grades),
            "grades": grades,
            "summary": {
                "gpa": acmtl.get("gpa"),
                "earned_credits": acmtl.get("aplyCdt"),
                "total_grade_points": acmtl.get("tgp"),
                "converted_score": acmtl.get("covsnSco"),
                "major_credits": acmtl.get("cmpsjCdt"),
            },
        }
    except Exception as e:
        logger.error(f"Failed to fetch grades: {e}")
        return {"success": False, "message": f"성적 조회 실패: {e}"}

TOOLS = [
    kupid_ams_auth_start,
    kupid_ams_auth_verify,
    kupid_get_timetable,
    kupid_search_courses,
    kupid_syllabus,
    kupid_room_schedule,
    kupid_my_courses,
    kupid_get_all_grades,
]
