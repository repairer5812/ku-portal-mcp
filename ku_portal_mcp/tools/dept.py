"""학과 홈페이지 공지 도구.

함수 본문은 server.py에 있던 코드와 같다. 등록 시 전역 이름은 server 모듈에 묶인다.
"""

from typing import Any

async def kupid_dept_notices(
    site_name: str = "", page: int = 1, count: int = 20
) -> dict[str, Any]:
    """학과/대학원 홈페이지 공지사항을 조회합니다 (인증 불필요).

    고려대학교 학과 홈페이지의 공지사항 게시판을 스크래핑합니다.
    site_name을 지정하지 않으면 사용 가능한 사이트 목록을 반환합니다.

    환경변수 KU_DEPT_URLS로 소속 학과를 설정할 수 있습니다.
    형식: "라벨|URL,라벨|URL,..."
    예: "SW·AI융합대학원|https://gscit.korea.ac.kr/gscit/board/notice_master.do"

    Args:
        site_name: 사이트 이름 또는 키 (빈 문자열이면 사이트 목록 반환)
        page: 페이지 번호 (기본값: 1)
        count: 한 페이지당 항목 수 (기본값: 20)
    """
    try:
        if not site_name:
            sites = list_all_sites()
            if not sites:
                return {
                    "success": True,
                    "message": (
                        "설정된 학과 사이트가 없습니다. "
                        "환경변수 KU_DEPT_URLS를 설정하거나 site_name에 "
                        "내장 사이트 키(gscit_master, cs_under 등)를 지정하세요."
                    ),
                    "available_sites": [
                        {"key": k, "label": v["label"], "url": v["url"]}
                        for k, v in DEFAULT_SITES.items()
                    ],
                }
            return {
                "success": True,
                "message": "사이트를 선택해주세요",
                "sites": sites,
            }

        site = resolve_site(site_name)
        if not site:
            return {
                "success": False,
                "message": f"사이트를 찾을 수 없습니다: {site_name}",
                "available_sites": list_all_sites(),
            }

        offset = (page - 1) * count
        items = await fetch_dept_notice_list(site["url"], offset=offset, limit=count)
        return {
            "success": True,
            "site": site["label"],
            "page": page,
            "count": len(items),
            "notices": [
                {
                    "article_no": n.article_no,
                    "title": n.title,
                    "writer": n.writer,
                    "date": n.date,
                    "views": n.views,
                    "is_pinned": n.is_pinned,
                    "has_attachment": n.has_attachment,
                }
                for n in items
            ],
        }
    except Exception as e:
        logger.error(f"Failed to fetch dept notices: {e}")
        return {"success": False, "message": f"학과 공지사항 조회 실패: {e}"}

async def kupid_dept_notice_detail(site_name: str, article_no: str) -> dict[str, Any]:
    """학과/대학원 공지사항의 상세 내용을 조회합니다 (인증 불필요).

    kupid_dept_notices로 조회한 공지의 상세 내용을 가져옵니다.

    Args:
        site_name: 사이트 이름 또는 키 (kupid_dept_notices에서 사용한 값)
        article_no: 게시글 번호 (kupid_dept_notices 결과의 article_no 필드)
    """
    try:
        site = resolve_site(site_name)
        if not site:
            return {
                "success": False,
                "message": f"사이트를 찾을 수 없습니다: {site_name}",
                "available_sites": list_all_sites(),
            }

        detail = await fetch_dept_notice_detail(site["url"], article_no)
        return {
            "success": True,
            "site": site["label"],
            "notice": {
                "article_no": detail.article_no,
                "title": detail.title,
                "content": detail.content,
                "attachments": detail.attachments,
                "url": detail.url,
            },
        }
    except Exception as e:
        logger.error(f"Failed to fetch dept notice detail: {e}")
        return {"success": False, "message": f"학과 공지사항 상세 조회 실패: {e}"}

TOOLS = [
    kupid_dept_notices,
    kupid_dept_notice_detail,
]
