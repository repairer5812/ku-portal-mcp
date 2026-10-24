"""Fork-only tool bodies for the personal remote deployment.

server.py keeps every tool registration (name, arguments, description) and
calls into this module, so the fork's additions stay out of the upstream
server.py and upstream changes merge with fewer conflicts.

Collaborators that tests patch on ``ku_portal_mcp.server`` (LMS download,
session retry, Drive client, access token) are passed in by the server.py
wrappers at call time instead of being imported here.
"""

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from mcp.types import (
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    TextContent,
    Tool as MCPTool,
)

from .drive import DriveSyncError, MAX_FILE_BYTES, configured_course_folders, validate_folder_id
from .lms import LMSFileCourseError, LMSFileTooLargeError

logger = logging.getLogger("ku_portal_mcp.server")

SERVER_INSTRUCTIONS = (
    "Google Drive로 LMS PDF를 보내거나 동기화할 때는 kupid_lms_sync_to_drive를 사용하세요. "
    "게시판 첨부의 실제 canvas_file_id와 기존 과목 폴더 ID를 전달하세요. "
    "원격 다운로드의 urn이나 서버 경로를 다른 플러그인의 file_uri로 꾸미지 마세요."
)


DEFAULT_MAX_EMBEDDED_FILE_BYTES = 20 * 1024 * 1024
ABSOLUTE_MAX_EMBEDDED_FILE_BYTES = 100 * 1024 * 1024


def max_embedded_file_bytes() -> int:
    """Read the embed limit, falling back safely for invalid configuration."""
    raw_value = os.environ.get(
        "KU_MCP_MAX_EMBEDDED_FILE_BYTES",
        str(DEFAULT_MAX_EMBEDDED_FILE_BYTES),
    )
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        logger.warning(
            "Invalid KU_MCP_MAX_EMBEDDED_FILE_BYTES=%r; using default %d",
            raw_value,
            DEFAULT_MAX_EMBEDDED_FILE_BYTES,
        )
        return DEFAULT_MAX_EMBEDDED_FILE_BYTES
    if value > ABSOLUTE_MAX_EMBEDDED_FILE_BYTES:
        logger.warning(
            "KU_MCP_MAX_EMBEDDED_FILE_BYTES=%d exceeds hard cap %d; clamping",
            value,
            ABSOLUTE_MAX_EMBEDDED_FILE_BYTES,
        )
        return ABSOLUTE_MAX_EMBEDDED_FILE_BYTES
    return value


def install_security_schemes(server) -> None:
    """Publish OAuth security schemes on every tool.

    FastMCP supports _meta but not the top-level ChatGPT auth extension.
    Publish both representations, and keep the ordinary MCP input/output schemas.
    """
    _original_list_tools = server.list_tools

    async def _list_tools_with_security_schemes() -> list[MCPTool]:
        tools = await _original_list_tools()
        for tool in tools:
            scopes = ["ku:read", "ku:drive"] if tool.name == "kupid_lms_sync_to_drive" else ["ku:read"]
            schemes = [{"type": "oauth2", "scopes": scopes}]
            tool.securitySchemes = schemes
            tool.meta = {**(tool.meta or {}), "securitySchemes": schemes}
        return tools

    server.list_tools = _list_tools_with_security_schemes
    server._mcp_server.list_tools()(_list_tools_with_security_schemes)


async def download_file(
    file_id: int,
    save_dir: str,
    filename: str,
    *,
    lms_with_retry,
    download_lms_file,
) -> CallToolResult:
    """Body of kupid_lms_download_file (see server.py for the tool contract)."""
    remote_mode = bool(os.environ.get("MCP_PUBLIC_BASE_URL"))
    remote_temp_dir: Path | None = None
    try:
        if remote_mode:
            # A remote caller cannot access or safely choose a server filesystem path.
            remote_temp_dir = Path(tempfile.mkdtemp(prefix="ku-mcp-download-"))
            target_dir = remote_temp_dir
        else:
            # Expand ~ and validate absolute path for local stdio callers.
            raw_path = save_dir.strip()
            if not raw_path:
                message = "save_dir가 비어 있습니다."
                return CallToolResult(
                    isError=True,
                    content=[TextContent(type="text", text=message)],
                    structuredContent={"success": False, "message": message},
                )
            target_dir = Path(raw_path).expanduser()
            if not target_dir.is_absolute():
                message = f"save_dir는 절대경로여야 합니다: {save_dir}"
                return CallToolResult(
                    isError=True,
                    content=[TextContent(type="text", text=message)],
                    structuredContent={"success": False, "message": message},
                )
            if ".." in target_dir.parts:
                message = "save_dir에 '..'를 포함할 수 없습니다."
                return CallToolResult(
                    isError=True,
                    content=[TextContent(type="text", text=message)],
                    structuredContent={"success": False, "message": message},
                )

        fname = filename.strip() or None

        async def _fetch(session, fid=file_id, d=target_dir, fn=fname):
            return await download_lms_file(session, fid, d, fn)

        result = await lms_with_retry(_fetch)
        downloaded_path = Path(result["path"])
        size = downloaded_path.stat().st_size
        max_embedded_bytes = max_embedded_file_bytes()
        metadata = {
            "success": True,
            "file_id": file_id,
            **result,
        }
        if remote_mode:
            metadata["path"] = None
        if size > max_embedded_bytes:
            if remote_mode:
                metadata.update(
                    {
                        "success": False,
                        "path": None,
                        "delivery": "not_delivered",
                        "embedded": False,
                        "message": (
                            f"파일 크기 {size} bytes가 원격 MCP 임베드 제한 "
                            f"{max_embedded_bytes} bytes를 초과해 전달할 수 없습니다."
                            " 서버 임시 파일은 요청 종료 시 삭제됩니다."
                        ),
                    }
                )
                return CallToolResult(
                    isError=True,
                    content=[TextContent(type="text", text=metadata["message"])],
                    structuredContent=metadata,
                )
            metadata["delivery"] = "filesystem"
            metadata["embedded"] = False
            metadata["message"] = (
                f"다운로드 완료: {downloaded_path.name} ({size} bytes). "
                f"MCP 임베드 제한 {max_embedded_bytes} bytes를 초과하여 "
                "파일 본문은 임베드하지 않았습니다."
            )
            return CallToolResult(
                content=[TextContent(type="text", text=metadata["message"])],
                structuredContent=metadata,
            )

        file_bytes = downloaded_path.read_bytes()
        mime_type = (
            result.get("content_type")
            or mimetypes.guess_type(downloaded_path.name)[0]
            or "application/octet-stream"
        )
        file_sha256 = hashlib.sha256(file_bytes).hexdigest()
        metadata.update(
            {
                "size": size,
                "content_type": mime_type,
                "sha256": file_sha256,
                "delivery": "mcp_embedded_resource",
                "embedded": True,
            }
        )
        resource_uri = (
            f"urn:ku-portal-mcp:lms-file:{file_id}:{file_sha256[:16]}"
            if remote_mode
            else downloaded_path.as_uri()
        )
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(
                        f"다운로드 완료: {downloaded_path.name} "
                        f"({size} bytes, {mime_type})"
                    ),
                ),
                EmbeddedResource(
                    type="resource",
                    resource=BlobResourceContents(
                        uri=resource_uri,
                        mimeType=mime_type,
                        blob=base64.b64encode(file_bytes).decode("ascii"),
                    ),
                ),
            ],
            structuredContent=metadata,
        )
    except Exception as e:
        logger.error(f"Failed to download LMS file {file_id}: {e}")
        message = f"LMS 파일 다운로드 실패: {e}"
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=message)],
            structuredContent={"success": False, "message": message},
        )
    finally:
        if remote_temp_dir is not None:
            shutil.rmtree(remote_temp_dir, ignore_errors=True)
            if remote_temp_dir.exists():
                logger.error(
                    "Failed to remove remote LMS temporary directory: %s",
                    remote_temp_dir,
                )


_drive_sync_lock = asyncio.Lock()


async def sync_to_drive(
    file_ids: list[int],
    drive_folder_id: str | None,
    *,
    lms_with_retry,
    download_lms_file,
    resolve_lms_file_course,
    drive_client,
    get_access_token,
) -> CallToolResult:
    """Body of kupid_lms_sync_to_drive (see server.py for the tool contract)."""
    def error_details(exc, stage):
        if isinstance(exc, (DriveSyncError, LMSFileCourseError)):
            return {"error_code": exc.error_code, "error_message": str(exc),
                    "stage": getattr(exc, "stage", None) or stage}
        if isinstance(exc, LMSFileTooLargeError):
            return {"error_code": "FILE_TOO_LARGE", "error_message": str(exc), "stage": stage}
        messages = {
            "resolve_course": "LMS 파일의 소속 과목을 확인하지 못했습니다.",
            "download_lms": "LMS 원본 파일 다운로드에 실패했습니다.",
            "authenticate_drive": "Google Drive 연결 또는 인증에 실패했습니다.",
            "check_folder": "기존 Drive 대상 폴더를 확인하지 못했습니다.",
            "sync_file": "Drive 전송 또는 검증 중 오류가 발생했습니다.",
        }
        return {"error_code": {"download_lms": "LMS_DOWNLOAD_FAILED", "resolve_course": "COURSE_LOOKUP_FAILED"}.get(stage, "SYNC_FAILED"),
                "error_message": messages.get(stage, "Drive 전송 설정 또는 연결을 확인하세요."),
                "stage": stage}

    def tool_result(payload, summary, *, meta=None):
        # Some clients show only text content, so do not hide per-file errors there.
        kwargs = {"_meta": meta} if meta else {}
        return CallToolResult(
            isError=not payload["success"],
            content=[TextContent(type="text", text=summary + "\n" + json.dumps(payload, ensure_ascii=False))],
            structuredContent=payload, **kwargs,
        )

    if os.environ.get("MCP_PUBLIC_BASE_URL"):
        token = get_access_token()
        if token is None or "ku:drive" not in token.scopes:
            message = "Drive 전송 권한(ku:drive)이 필요합니다. KU Portal 연결 권한을 다시 승인하세요."
            public = urlsplit(os.environ["MCP_PUBLIC_BASE_URL"].rstrip("/"))
            resource_metadata = (
                f"{public.scheme}://{public.netloc}/.well-known/oauth-protected-resource"
                f"{public.path}/mcp"
            )
            return tool_result(
                {"success": False, "message": message, "error_code": "INSUFFICIENT_SCOPE",
                 "error_message": message, "stage": "authorize", "results": []},
                message, meta={"mcp/www_authenticate": (
                    f'Bearer resource_metadata="{resource_metadata}", '
                    'error="insufficient_scope", scope="ku:read ku:drive"'
                )},
            )
    client = None
    results = []
    stage = "validate_arguments"
    try:
        if not 1 <= len(file_ids) <= 20 or any(isinstance(i, bool) or i <= 0 for i in file_ids):
            raise DriveSyncError("file_ids에 1~20개의 양의 LMS 파일 ID를 넣으세요.",
                                 code="INVALID_ARGUMENT", stage=stage)
        if len(set(file_ids)) != len(file_ids):
            raise DriveSyncError("file_ids에 같은 파일 ID를 중복해서 넣을 수 없습니다.",
                                 code="INVALID_ARGUMENT", stage=stage)
        if drive_folder_id is not None:
            validate_folder_id(drive_folder_id)
            course_folders = {}
        else:
            course_folders = configured_course_folders()
        async with _drive_sync_lock:
            stage = "authenticate_drive"
            client = await drive_client.connect()
            checked_folders = set()
            for file_id in file_ids:
                folder_id = drive_folder_id
                course_id = None
                filename = None
                try:
                    if folder_id is None:
                        stage = "resolve_course"
                        course_id = await lms_with_retry(
                            lambda session, fid=file_id: resolve_lms_file_course(session, fid, list(course_folders))
                        )
                        folder_id = course_folders[course_id]
                    if folder_id not in checked_folders:
                        stage = "check_folder"
                        await client.check_folder(folder_id)
                        checked_folders.add(folder_id)
                    with tempfile.TemporaryDirectory(prefix="ku-drive-sync-") as directory:
                        stage = "download_lms"
                        async def fetch(session, fid=file_id, target=Path(directory)):
                            return await download_lms_file(session, fid, target, max_bytes=MAX_FILE_BYTES)
                        downloaded = await lms_with_retry(fetch)
                        path = Path(downloaded["path"])
                        filename = path.name
                        stage = "sync_file"
                        result = await client.sync_file(file_id, path, folder_id)
                        results.append({"success": True, "course_id": course_id,
                                        "drive_folder_id": folder_id, **result})
                except Exception as exc:
                    details = error_details(exc, stage)
                    logger.warning("LMS Drive sync failed file_id=%d code=%s stage=%s exception=%s",
                                   file_id, details["error_code"], details["stage"], type(exc).__name__)
                    results.append({"success": False, "file_id": file_id, "filename": filename,
                                    "course_id": course_id, "drive_folder_id": folder_id, "action": "failed",
                                    "message": details["error_message"], **details})
        failures = sum(not row["success"] for row in results)
        message = f"LMS 강의자료 Drive 전송: {len(results)-failures}개 성공, {failures}개 실패."
        return tool_result(
            {"success": failures == 0, "drive_folder_id": drive_folder_id,
             "succeeded": len(results)-failures, "failed": failures, "results": results},
            message,
        )
    except Exception as exc:
        details = error_details(exc, stage)
        return tool_result({"success": False, "message": details["error_message"],
                            "results": results, **details}, details["error_message"])
    finally:
        if client:
            await client.close()
