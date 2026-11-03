"""Canvas LMS(mylms) 도구.

함수 본문은 server.py에 있던 코드와 같다. 등록 시 전역 이름은 server 모듈에 묶인다.
"""

from typing import Any

from mcp.types import CallToolResult

async def kupid_lms_courses() -> dict[str, Any]:
    """Canvas LMS 수강과목 목록을 조회합니다.

    mylms.korea.ac.kr의 Canvas LMS에서 수강 중인 과목 목록을 가져옵니다.
    SSO 로그인이 필요합니다.
    """
    try:
        courses = await _lms_with_retry(fetch_lms_courses)
        return {
            "success": True,
            "count": len(courses),
            "courses": [
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "course_code": c.get("course_code"),
                    "term": c.get("term", {}).get("name") if c.get("term") else None,
                    "workflow_state": c.get("workflow_state"),
                }
                for c in courses
            ],
        }
    except Exception as e:
        logger.error(f"Failed to fetch LMS courses: {e}")
        return {"success": False, "message": f"LMS 수강과목 조회 실패: {e}"}

async def kupid_lms_assignments(
    course_id: int, upcoming_only: bool = False
) -> dict[str, Any]:
    """Canvas LMS 과제 목록을 조회합니다.

    특정 과목의 전체 과제(assignments) 목록을 가져옵니다.
    기본적으로 완료/마감 과제 포함 전체를 반환합니다.
    kupid_lms_courses로 course_id를 먼저 확인하세요.

    Args:
        course_id: 과목 ID (kupid_lms_courses의 id 필드)
        upcoming_only: True이면 마감 전 과제만 표시 (기본값: False, 전체 과제 반환)
    """
    try:

        async def _fetch(session, cid=course_id, upcoming=upcoming_only):
            return await fetch_lms_assignments(session, cid, upcoming)

        assignments = await _lms_with_retry(_fetch)
        return {
            "success": True,
            "course_id": course_id,
            "count": len(assignments),
            "assignments": [
                {
                    "id": a.get("id"),
                    "name": a.get("name"),
                    "due_at": a.get("due_at"),
                    "lock_at": a.get("lock_at"),
                    "unlock_at": a.get("unlock_at"),
                    "points_possible": a.get("points_possible"),
                    "submission_types": a.get("submission_types"),
                    "submission": {
                        "workflow_state": a.get("submission", {}).get("workflow_state"),
                        "submitted_at": a.get("submission", {}).get("submitted_at"),
                        "score": a.get("submission", {}).get("score"),
                        "grade": a.get("submission", {}).get("grade"),
                        "late": a.get("submission", {}).get("late"),
                        "missing": a.get("submission", {}).get("missing"),
                    }
                    if a.get("submission")
                    else None,
                    "description": (a.get("description") or "")[:2000],
                    "html_url": a.get("html_url"),
                }
                for a in assignments
            ],
        }
    except Exception as e:
        logger.error(f"Failed to fetch LMS assignments: {e}")
        return {"success": False, "message": f"LMS 과제 조회 실패: {e}"}

async def kupid_lms_modules(course_id: int) -> dict[str, Any]:
    """Canvas LMS 강의자료(모듈)를 조회합니다.

    주차별 강의 모듈과 포함된 자료를 가져옵니다.
    kupid_lms_courses로 course_id를 먼저 확인하세요.

    Args:
        course_id: 과목 ID (kupid_lms_courses의 id 필드)
    """
    try:

        async def _fetch(session, cid=course_id):
            return await fetch_lms_modules(session, cid)

        modules = await _lms_with_retry(_fetch)
        return {
            "success": True,
            "course_id": course_id,
            "count": len(modules),
            "modules": [
                {
                    "id": m.get("id"),
                    "name": m.get("name"),
                    "position": m.get("position"),
                    "state": m.get("state"),
                    "items_count": m.get("items_count"),
                    "items": [
                        {
                            "id": item.get("id"),
                            "title": item.get("title"),
                            "type": item.get("type"),
                            "content_id": item.get("content_id"),
                            "url": item.get("url"),
                            "html_url": item.get("html_url"),
                        }
                        for item in m.get("items", [])
                    ],
                }
                for m in modules
            ],
        }
    except Exception as e:
        logger.error(f"Failed to fetch LMS modules: {e}")
        return {"success": False, "message": f"LMS 모듈 조회 실패: {e}"}

async def kupid_lms_todo() -> dict[str, Any]:
    """Canvas LMS 할 일(Todo) 목록을 조회합니다.

    마감이 다가오는 과제, 퀴즈 등을 보여줍니다.
    """
    try:

        async def _fetch_all(session):
            todos = await fetch_lms_todo(session)
            events = await fetch_lms_upcoming_events(session)
            return todos, events

        todos, events = await _lms_with_retry(_fetch_all)
        return {
            "success": True,
            "todo_count": len(todos),
            "todos": [
                {
                    "type": t.get("type"),
                    "context_name": t.get("context_name"),
                    "assignment": {
                        "name": t.get("assignment", {}).get("name"),
                        "due_at": t.get("assignment", {}).get("due_at"),
                        "course_id": t.get("assignment", {}).get("course_id"),
                        "points_possible": t.get("assignment", {}).get(
                            "points_possible"
                        ),
                        "html_url": t.get("assignment", {}).get("html_url"),
                    }
                    if t.get("assignment")
                    else None,
                }
                for t in todos
            ],
            "upcoming_events_count": len(events),
            "upcoming_events": [
                {
                    "title": e.get("title"),
                    "start_at": e.get("start_at"),
                    "end_at": e.get("end_at"),
                    "html_url": e.get("html_url"),
                }
                for e in events
            ],
        }
    except Exception as e:
        logger.error(f"Failed to fetch LMS todo: {e}")
        return {"success": False, "message": f"LMS 할 일 조회 실패: {e}"}

async def kupid_lms_dashboard() -> dict[str, Any]:
    """Canvas LMS 대시보드를 조회합니다.

    현재 수강 중인 과목 카드와 과제/이벤트 현황을 보여줍니다.
    """
    try:
        cards = await _lms_with_retry(fetch_lms_dashboard)
        course_ids = [c.get("id") for c in cards if c.get("id")]
        announcements = []
        if course_ids:
            try:
                announcements = await _lms_with_retry(
                    fetch_lms_announcements, course_ids[:10]
                )
            except Exception:
                pass  # Announcements may not be available

        return {
            "success": True,
            "courses": [
                {
                    "id": c.get("id"),
                    "name": c.get("shortName", c.get("longName")),
                    "course_code": c.get("courseCode"),
                    "term": c.get("term"),
                }
                for c in cards
            ],
            "announcements": [
                {
                    "id": a.get("id"),
                    "title": a.get("title"),
                    "posted_at": a.get("posted_at"),
                    "context_code": a.get("context_code"),
                    "message": (a.get("message") or "")[:300],
                }
                for a in announcements[:10]
            ],
        }
    except Exception as e:
        logger.error(f"Failed to fetch LMS dashboard: {e}")
        return {"success": False, "message": f"LMS 대시보드 조회 실패: {e}"}

async def kupid_lms_announcements(course_id: int | None = None) -> dict[str, Any]:
    """Canvas LMS 공지(announcement)를 조회합니다.

    course_id를 지정하면 해당 과목, 생략하면 현재 활성 과목 전체의 공지를
    가져옵니다. kupid_lms_dashboard와 달리 message 본문을 절단하지 않고
    전문(HTML)으로 반환합니다.

    Args:
        course_id: 과목 ID (생략 시 활성 과목 전체, kupid_lms_courses 참조)
    """
    try:

        async def _fetch(session, cid=course_id):
            if cid is not None:
                ids = [cid]
            else:
                cards = await fetch_lms_dashboard(session)
                ids = [c.get("id") for c in cards if c.get("id")]
            if not ids:
                return []
            return await fetch_lms_announcements(session, ids)

        announcements = await _lms_with_retry(_fetch)
        return {
            "success": True,
            "count": len(announcements),
            "announcements": [
                {
                    "id": a.get("id"),
                    "title": a.get("title"),
                    "posted_at": a.get("posted_at"),
                    "context_code": a.get("context_code"),
                    "author": (a.get("author") or {}).get("display_name"),
                    "url": a.get("html_url") or a.get("url"),
                    "message": a.get("message"),
                }
                for a in announcements
            ],
        }
    except Exception as e:
        logger.error(f"Failed to fetch LMS announcements: {e}")
        return {"success": False, "message": f"LMS 공지 조회 실패: {e}"}

async def kupid_lms_grades(course_id: int) -> dict[str, Any]:
    """Canvas LMS 성적/점수를 조회합니다.

    과목별 현재 점수, 최종 점수, 학점(grade)을 확인합니다.
    kupid_lms_courses로 course_id를 먼저 확인하세요.

    Args:
        course_id: 과목 ID (kupid_lms_courses의 id 필드)
    """
    try:

        async def _fetch(session, cid=course_id):
            return await fetch_lms_grades(session, cid)

        enrollments = await _lms_with_retry(_fetch)
        return {
            "success": True,
            "course_id": course_id,
            "enrollments": [
                {
                    "type": e.get("type"),
                    "enrollment_state": e.get("enrollment_state"),
                    "last_activity_at": e.get("last_activity_at"),
                    "grades": {
                        "current_score": e.get("grades", {}).get("current_score"),
                        "current_grade": e.get("grades", {}).get("current_grade"),
                        "final_score": e.get("grades", {}).get("final_score"),
                        "final_grade": e.get("grades", {}).get("final_grade"),
                        "current_period_score": e.get("grades", {}).get(
                            "current_period_computed_current_score"
                        ),
                        "current_period_grade": e.get("grades", {}).get(
                            "current_period_computed_current_grade"
                        ),
                        "html_url": e.get("grades", {}).get("html_url"),
                    }
                    if e.get("grades")
                    else None,
                }
                for e in enrollments
            ],
        }
    except Exception as e:
        logger.error(f"Failed to fetch LMS grades: {e}")
        return {"success": False, "message": f"LMS 성적 조회 실패: {e}"}

async def kupid_lms_submissions(course_id: int) -> dict[str, Any]:
    """Canvas LMS 과제 제출 현황을 조회합니다.

    과목의 전체 과제에 대한 제출 여부, 점수, 채점 상태를 확인합니다.
    kupid_lms_courses로 course_id를 먼저 확인하세요.

    Args:
        course_id: 과목 ID (kupid_lms_courses의 id 필드)
    """
    try:

        async def _fetch(session, cid=course_id):
            return await fetch_lms_submissions(session, cid)

        submissions = await _lms_with_retry(_fetch)
        return {
            "success": True,
            "course_id": course_id,
            "count": len(submissions),
            "submissions": [
                {
                    "assignment_id": s.get("assignment_id"),
                    "assignment_name": s.get("assignment", {}).get("name")
                    if s.get("assignment")
                    else None,
                    "due_at": s.get("assignment", {}).get("due_at")
                    if s.get("assignment")
                    else None,
                    "submitted_at": s.get("submitted_at"),
                    "workflow_state": s.get("workflow_state"),
                    "score": s.get("score"),
                    "grade": s.get("grade"),
                    "late": s.get("late"),
                    "missing": s.get("missing"),
                    "points_deducted": s.get("points_deducted"),
                    "attempt": s.get("attempt"),
                    "preview_url": s.get("preview_url"),
                    "attachments": [
                        {
                            "filename": a.get("filename"),
                            "url": a.get("url"),
                            "canvas_file_id": a.get("id"),
                        }
                        for a in (s.get("attachments") or [])
                    ],
                    "comments": [
                        {
                            "author_name": c.get("author_name"),
                            "comment": c.get("comment"),
                            "created_at": c.get("created_at"),
                        }
                        for c in (s.get("submission_comments") or [])
                    ],
                }
                for s in submissions
            ],
        }
    except Exception as e:
        logger.error(f"Failed to fetch LMS submissions: {e}")
        return {"success": False, "message": f"LMS 제출 현황 조회 실패: {e}"}

async def kupid_lms_quizzes(course_id: int) -> dict[str, Any]:
    """Canvas LMS 퀴즈/시험 목록을 조회합니다.

    과목의 퀴즈, 시험, 설문 목록과 마감일, 시간제한 등을 확인합니다.
    kupid_lms_courses로 course_id를 먼저 확인하세요.

    Args:
        course_id: 과목 ID (kupid_lms_courses의 id 필드)
    """
    try:

        async def _fetch(session, cid=course_id):
            return await fetch_lms_quizzes(session, cid)

        quizzes = await _lms_with_retry(_fetch)
        return {
            "success": True,
            "course_id": course_id,
            "count": len(quizzes),
            "quizzes": [
                {
                    "id": q.get("id"),
                    "title": q.get("title"),
                    "quiz_type": q.get("quiz_type"),
                    "due_at": q.get("due_at"),
                    "lock_at": q.get("lock_at"),
                    "time_limit": q.get("time_limit"),
                    "question_count": q.get("question_count"),
                    "points_possible": q.get("points_possible"),
                    "published": q.get("published"),
                    "html_url": q.get("html_url"),
                }
                for q in quizzes
            ],
        }
    except Exception as e:
        logger.error(f"Failed to fetch LMS quizzes: {e}")
        return {"success": False, "message": f"LMS 퀴즈 조회 실패: {e}"}

async def kupid_lms_syllabus(
    course_code: str = "", course_id: int = 0
) -> dict[str, Any]:
    """Canvas LMS 수업 계획서(syllabus)를 조회합니다.

    과목의 수업 계획서 내용을 가져옵니다.
    course_code(예: BDC115) 또는 course_id 중 하나를 입력하세요.
    course_code를 입력하면 수강과목 목록에서 자동으로 course_id를 찾습니다.

    Args:
        course_code: 과목코드 (예: BDC115). course_id 대신 사용 가능
        course_id: 과목 ID (kupid_lms_courses의 id 필드). course_code 대신 사용 가능
    """
    from bs4 import BeautifulSoup

    if not course_code and not course_id:
        return {
            "success": False,
            "message": "course_code 또는 course_id 중 하나를 입력하세요.",
        }

    try:
        # course_code로 조회 시 course_id 찾기
        if course_code and not course_id:
            courses = await _lms_with_retry(fetch_lms_courses)
            keyword = course_code.upper()
            matched = [
                c
                for c in courses
                if keyword in (c.get("course_code") or "").upper()
                or keyword in (c.get("name") or "").upper()
            ]
            if not matched:
                return {
                    "success": False,
                    "message": f"'{course_code}'에 해당하는 수강과목을 찾을 수 없습니다. "
                    "kupid_lms_courses로 수강과목 목록을 확인하세요.",
                }
            if len(matched) > 1:
                return {
                    "success": False,
                    "message": f"'{course_code}'에 해당하는 과목이 {len(matched)}개 있습니다. course_id를 지정하세요.",
                    "candidates": [
                        {
                            "id": c.get("id"),
                            "name": c.get("name"),
                            "course_code": c.get("course_code"),
                        }
                        for c in matched
                    ],
                }
            course_id = matched[0]["id"]

        async def _fetch(session, cid=course_id):
            return await fetch_lms_syllabus(session, cid)

        course_data = await _lms_with_retry(_fetch)

        syllabus_html = course_data.get("syllabus_body") or ""
        syllabus_text = ""

        if syllabus_html:
            soup = BeautifulSoup(syllabus_html, "lxml")

            syllabus_text = soup.get_text(separator="\n", strip=True)

        return {
            "success": True,
            "course_id": course_id,
            "course_name": course_data.get("name"),
            "course_code": course_data.get("course_code"),
            "term": course_data.get("term", {}).get("name")
            if course_data.get("term")
            else None,
            "syllabus_url": f"https://mylms.korea.ac.kr/courses/{course_id}/assignments/syllabus",
            "syllabus": syllabus_text
            if syllabus_text
            else "(수업 계획서가 비어 있습니다)",
        }
    except Exception as e:
        logger.error(f"Failed to fetch LMS syllabus: {e}")
        return {"success": False, "message": f"LMS 수업 계획서 조회 실패: {e}"}

async def kupid_lms_download_file(
    file_id: int,
    save_dir: str,
    filename: str = "",
) -> CallToolResult:
    """Canvas LMS 파일을 다운로드하고 MCP binary resource로 반환합니다.

    file_id는 kupid_lms_modules 결과의 items에서 type이 'File'인 항목의
    content_id 필드에서 얻을 수 있습니다.

    파일은 기존과 같이 save_dir에도 저장되며, MCP 클라이언트가 다른 tool로
    바로 전달할 수 있도록 BlobResourceContents에도 포함됩니다. 메타데이터는
    structured_content에 유지합니다.

    Args:
        file_id: Canvas 파일 ID (items[*].content_id)
        save_dir: 저장할 디렉토리 절대경로 (예: /Users/me/Documents/lecture)
        filename: 저장 파일명 (생략 시 Canvas 원본 파일명 사용)
    """
    try:
        # Expand ~ and validate absolute path
        raw_path = save_dir.strip()
        if not raw_path:
            error = {"success": False, "message": "save_dir가 비어 있습니다."}
            return CallToolResult(
                content=[TextContent(type="text", text=error["message"])],
                structuredContent=error,
                isError=True,
            )
        target_dir = Path(raw_path).expanduser()
        if not target_dir.is_absolute():
            error = {
                "success": False,
                "message": f"save_dir는 절대경로여야 합니다: {save_dir}",
            }
            return CallToolResult(
                content=[TextContent(type="text", text=error["message"])],
                structuredContent=error,
                isError=True,
            )
        if ".." in target_dir.parts:
            error = {
                "success": False,
                "message": "save_dir에 '..'를 포함할 수 없습니다.",
            }
            return CallToolResult(
                content=[TextContent(type="text", text=error["message"])],
                structuredContent=error,
                isError=True,
            )

        fname = filename.strip() or None

        async def _fetch(session, fid=file_id, d=target_dir, fn=fname):
            return await download_lms_file(session, fid, d, fn)

        result = await _lms_with_retry(_fetch)
        path = Path(result["path"])
        content_type = result.get("content_type") or "application/octet-stream"
        metadata = {
            "success": True,
            "file_id": file_id,
            "path": str(path),
            "filename": result["filename"],
            "size": result["size"],
            "content_type": content_type,
            "sha256": result["sha256"],
            "delivery": "mcp_embedded_resource",
            "embedded": True,
        }

        blob = base64.b64encode(path.read_bytes()).decode("ascii")
        resource_uri = f"ku-lms://files/{file_id}"

        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(
                        f"Downloaded {result['filename']} "
                        f"({result['size']} bytes, sha256={result['sha256']})"
                    ),
                ),
                EmbeddedResource(
                    type="resource",
                    resource=BlobResourceContents(
                        uri=resource_uri,
                        mimeType=content_type,
                        blob=blob,
                    ),
                ),
            ],
            structuredContent=metadata,
        )
    except Exception as e:
        logger.error(f"Failed to download LMS file {file_id}: {e}")
        error = {"success": False, "message": f"LMS 파일 다운로드 실패: {e}"}
        return CallToolResult(
            content=[TextContent(type="text", text=error["message"])],
            structuredContent=error,
            isError=True,
        )

async def kupid_lms_list_boards(course_id: int) -> dict[str, Any]:
    """Canvas LMS 과목의 게시판 목록을 조회합니다.

    Q&A 게시판, 강의자료실 등 교수님이 자료를 올리는 게시판들을 반환합니다.
    Canvas 네이티브 모듈(kupid_lms_modules)에 자료가 없으면 여기서 찾아보세요.

    Args:
        course_id: 과목 ID (kupid_lms_courses의 id 필드)
    """
    try:

        async def _fetch(session, cid=course_id):
            return await fetch_lms_boards(session, cid)

        boards = await _lms_with_retry(_fetch)
        return {
            "success": True,
            "course_id": course_id,
            "count": len(boards),
            "boards": [
                {
                    "id": b.get("id"),
                    "title": b.get("title"),
                    "type": b.get("type"),
                    "slug": b.get("slug"),
                    "use_attachment": b.get("use_attachment"),
                }
                for b in boards
            ],
        }
    except Exception as e:
        logger.error(f"Failed to list LMS boards: {e}")
        return {"success": False, "message": f"LMS 게시판 목록 조회 실패: {e}"}

async def kupid_lms_list_board_posts(
    course_id: int,
    board_id: int,
    page: int = 1,
    keyword: str = "",
) -> dict[str, Any]:
    """게시판의 게시글 목록을 조회합니다.

    Args:
        course_id: 과목 ID
        board_id: 게시판 ID (kupid_lms_list_boards의 id 필드)
        page: 페이지 번호 (기본 1)
        keyword: 제목 검색어 (기본 전체)
    """
    try:

        async def _fetch(session, cid=course_id, bid=board_id, pg=page, kw=keyword):
            return await fetch_lms_board_posts(session, cid, bid, pg, kw)

        data = await _lms_with_retry(_fetch)
        items = data.get("items", []) if isinstance(data, dict) else []
        return {
            "success": True,
            "course_id": course_id,
            "board_id": board_id,
            "page": page,
            "count": len(items),
            "posts": [
                {
                    "id": p.get("id"),
                    "idx": p.get("idx"),
                    "title": p.get("title"),
                    "user_name": p.get("user_name"),
                    "attachment_count": p.get("attachment_count"),
                    "comment_count": p.get("comment_count"),
                    "view_count": p.get("view_count"),
                    "is_notice": p.get("is_notice"),
                    "created_at": p.get("created_at"),
                }
                for p in items
            ],
        }
    except Exception as e:
        logger.error(f"Failed to list board posts: {e}")
        return {"success": False, "message": f"게시글 목록 조회 실패: {e}"}

async def kupid_lms_get_board_post(
    course_id: int,
    board_id: int,
    post_id: int,
) -> dict[str, Any]:
    """게시글 상세와 첨부파일, 댓글(첨부 포함)을 조회합니다.

    attachments의 canvas_file_id를 kupid_lms_download_file의 file_id로
    넘기면 파일을 다운로드할 수 있습니다. attachments[].url은 직접
    다운로드 링크입니다(시간제한 verifier 토큰 포함).

    comments에는 각 댓글의 본문과 첨부파일(동영상/PDF 등)이 포함됩니다.
    예: 텀프로젝트 게시판에서 팀별 발표 동영상은 댓글 첨부로 제출됩니다.

    Args:
        course_id: 과목 ID
        board_id: 게시판 ID
        post_id: 게시글 ID (kupid_lms_list_board_posts의 id 필드)
    """
    try:

        async def _fetch(session, cid=course_id, bid=board_id, pid=post_id):
            return await fetch_lms_board_post(session, cid, bid, pid)

        post = await _lms_with_retry(_fetch)

        def _attachments(items):
            return [
                {
                    "id": a.get("id"),
                    "filename": a.get("filename"),
                    "filesize": a.get("filesize"),
                    "canvas_file_id": a.get("canvas_file_id"),
                    "url": a.get("url"),
                }
                for a in (items or [])
            ]

        return {
            "success": True,
            "post": {
                "id": post.get("id"),
                "title": post.get("title"),
                "user_name": post.get("user_name"),
                "content": post.get("content"),
                "created_at": post.get("created_at"),
                "updated_at": post.get("updated_at"),
                "view_count": post.get("view_count"),
                "comment_count": post.get("comment_count"),
                "attachments": _attachments(post.get("attachments")),
                "comments": [
                    {
                        "id": c.get("id"),
                        "user_name": c.get("user_name"),
                        "content": c.get("content"),
                        "created_at": c.get("created_at"),
                        "is_secret": c.get("is_secret"),
                        "attachments": _attachments(c.get("attachments")),
                    }
                    for c in post.get("comments", [])
                    if not c.get("is_deleted")
                ],
            },
        }
    except Exception as e:
        logger.error(f"Failed to get board post: {e}")
        return {"success": False, "message": f"게시글 조회 실패: {e}"}

TOOLS = [
    kupid_lms_courses,
    kupid_lms_assignments,
    kupid_lms_modules,
    kupid_lms_todo,
    kupid_lms_dashboard,
    kupid_lms_announcements,
    kupid_lms_grades,
    kupid_lms_submissions,
    kupid_lms_quizzes,
    kupid_lms_syllabus,
    kupid_lms_download_file,
    kupid_lms_list_boards,
    kupid_lms_list_board_posts,
    kupid_lms_get_board_post,
]
