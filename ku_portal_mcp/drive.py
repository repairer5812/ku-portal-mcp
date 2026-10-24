"""Server-side LMS file delivery to explicitly allowed Google Drive folders."""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .drive_sync_policy import choose_drive_filename, prefixed_filename

DRIVE_API = "https://www.googleapis.com/drive/v3/files"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_PDF_BYTES = MAX_FILE_BYTES  # Compatibility with the first PDF-only implementation.
FILE_FIELDS = "id,name,mimeType,size,parents,sha256Checksum,appProperties,modifiedTime"
_FOLDER_MIME = "application/vnd.google-apps.folder"
_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
_SOURCE_KEY = "ku_lms_file_id"
_MIME_OVERRIDES = {
    ".pdf": "application/pdf",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".md": "text/markdown",
}

# Resumable upload URLs and OAuth responses must not enter request logs.
logging.getLogger("httpx").setLevel(logging.WARNING)


class DriveSyncError(RuntimeError):
    def __init__(self, message: str, *, code: str = "DRIVE_SYNC_ERROR", stage: str | None = None):
        super().__init__(message)
        self.error_code = code
        self.stage = stage


def allowed_folder_ids() -> set[str]:
    return {x.strip() for x in os.environ.get("KU_DRIVE_FOLDER_IDS", "").split(",") if x.strip()}


def validate_folder_id(folder_id: str) -> None:
    if not _ID.fullmatch(folder_id) or folder_id not in allowed_folder_ids():
        raise DriveSyncError(
            "설정된 기존 과목 폴더만 사용할 수 있습니다. 새 폴더는 만들지 않습니다.",
            code="FOLDER_NOT_ALLOWED", stage="validate_arguments",
        )


def configured_course_folders() -> dict[int, str]:
    try:
        raw = json.loads(os.environ.get("KU_DRIVE_COURSE_FOLDERS", "{}"))
        if not isinstance(raw, dict):
            raise ValueError
        mapping = {int(course_id): folder for course_id, folder in raw.items()}
        if any(cid <= 0 or not isinstance(folder, str) for cid, folder in mapping.items()):
            raise ValueError
    except (TypeError, ValueError):
        raise DriveSyncError(
            "서버의 과목별 Drive 폴더 연결 설정을 확인하세요.",
            code="COURSE_MAPPING_INVALID", stage="resolve_course",
        ) from None
    for folder in mapping.values():
        validate_folder_id(folder)
    if not mapping:
        raise DriveSyncError(
            "서버에 과목별 Drive 폴더 연결이 설정되지 않았습니다.",
            code="COURSE_MAPPING_MISSING", stage="resolve_course",
        )
    return mapping


def _query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def file_mime_type(path: Path, data: bytes) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf" and not data.lstrip().startswith(b"%PDF-"):
        raise DriveSyncError(
            "원본 파일이 실제 PDF가 아닙니다. 파일 이름은 임의로 바꾸지 않습니다.",
            code="INVALID_FILE_FORMAT", stage="validate_file",
        )
    if suffix == ".ipynb":
        try:
            notebook = json.loads(data)
            version = notebook.get("nbformat") if isinstance(notebook, dict) else None
            cells = notebook.get("cells") if version and version >= 4 else notebook.get("worksheets")
            if isinstance(version, bool) or not isinstance(version, int) or version < 3 or not isinstance(cells, list):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise DriveSyncError(
                "원본 .ipynb 파일이 올바른 Jupyter 노트북 JSON이 아닙니다.",
                code="INVALID_FILE_FORMAT", stage="validate_file",
            ) from None
        return "application/x-ipynb+json"
    return _MIME_OVERRIDES.get(suffix) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


class DriveClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.stage = "authenticate_drive"

    def _error(self, message: str, code: str) -> DriveSyncError:
        return DriveSyncError(message, code=code, stage=self.stage)

    @classmethod
    async def connect(cls) -> "DriveClient":
        credential_path = Path(os.environ.get("KU_DRIVE_CREDENTIALS_FILE", "/run/secrets/ku_drive_credentials"))
        try:
            info = json.loads(credential_path.read_text())
            refresh_data = {key: info[key] for key in ("client_id", "client_secret", "refresh_token")}
        except (OSError, KeyError, ValueError):
            raise DriveSyncError(
                "서버의 Google Drive 인증 설정을 확인하세요.",
                code="DRIVE_AUTH_CONFIGURATION_ERROR", stage="authenticate_drive",
            ) from None
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as auth:
                response = await auth.post(
                    "https://oauth2.googleapis.com/token",
                    data={"grant_type": "refresh_token", **refresh_data},
                )
                if response.status_code != 200:
                    raise DriveSyncError(
                        f"Google Drive 인증 갱신 실패 (HTTP {response.status_code})",
                        code="DRIVE_AUTH_FAILED", stage="authenticate_drive",
                    )
                token = response.json().get("access_token")
                if not isinstance(token, str) or not token:
                    raise DriveSyncError(
                        "Google Drive 인증 응답에 접근 토큰이 없습니다.",
                        code="DRIVE_AUTH_FAILED", stage="authenticate_drive",
                    )
        except httpx.HTTPError:
            raise DriveSyncError(
                "Google Drive 인증 갱신 중 네트워크 오류.",
                code="DRIVE_NETWORK_ERROR", stage="authenticate_drive",
            ) from None
        except ValueError:
            raise DriveSyncError(
                "Google Drive 인증 응답을 해석할 수 없습니다.",
                code="DRIVE_AUTH_FAILED", stage="authenticate_drive",
            ) from None
        return cls(httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=20),
            headers={"Authorization": "Bearer " + token},
            follow_redirects=False,
        ))

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            response = await self.client.request(method, url, **kwargs)
        except httpx.HTTPError:
            raise self._error(
                "Google Drive 네트워크 오류. 다음 호출에서 기존 파일을 먼저 조회하세요.",
                "DRIVE_NETWORK_ERROR",
            ) from None
        if response.status_code not in (200, 201):
            raise self._error(f"Google Drive 요청 실패 (HTTP {response.status_code})", "DRIVE_HTTP_ERROR")
        return response

    async def check_folder(self, folder_id: str) -> None:
        validate_folder_id(folder_id)
        self.stage = "check_folder"
        response = await self._request("GET", f"{DRIVE_API}/{folder_id}", params={
            "fields": "id,mimeType,trashed,capabilities(canAddChildren)",
            "supportsAllDrives": "true",
        })
        metadata = response.json()
        if (metadata.get("mimeType") != _FOLDER_MIME or metadata.get("trashed")
                or not metadata.get("capabilities", {}).get("canAddChildren")):
            raise self._error("기존 대상 폴더의 업로드 권한을 확인하세요.", "FOLDER_ACCESS_DENIED")

    async def _find(self, folder_id: str, file_id: int, name: str) -> dict | None:
        new_name = prefixed_filename(name)
        query = (
            f"trashed=false and '{_query_value(folder_id)}' in parents and "
            f"(appProperties has {{ key='{_SOURCE_KEY}' and value='{file_id}' }} "
            f"or name='{_query_value(name)}' or name='{_query_value(new_name)}')"
        )
        files = []
        page = None
        while True:
            params = {"q": query, "fields": f"nextPageToken,files({FILE_FIELDS})",
                      "pageSize": "100", "supportsAllDrives": "true",
                      "includeItemsFromAllDrives": "true"}
            if page:
                params["pageToken"] = page
            result = (await self._request("GET", DRIVE_API, params=params)).json()
            files.extend(result.get("files", []))
            page = result.get("nextPageToken")
            if len(files) > 1 or not page:
                break
        if len(files) > 1:
            raise self._error(
                "대상 폴더에 같은 이름 또는 LMS ID의 파일이 여러 개 있습니다. 자동 덮어쓰기를 중단합니다.",
                "DESTINATION_CONFLICT",
            )
        if not files:
            return None
        existing = files[0]
        if existing.get("mimeType", "").startswith("application/vnd.google-apps."):
            raise self._error(
                "같은 이름의 기존 항목은 Google 문서·폴더·바로가기이므로 덮어쓰지 않습니다.",
                "DESTINATION_TYPE_CONFLICT",
            )
        linked_id = existing.get("appProperties", {}).get(_SOURCE_KEY)
        if linked_id and linked_id != str(file_id):
            raise self._error("같은 이름의 기존 파일이 다른 LMS 파일에 연결되어 있습니다.", "DESTINATION_CONFLICT")
        return existing

    async def _read_hash(self, drive_id: str, expected_size: int) -> str:
        digest = hashlib.sha256()
        total = 0
        try:
            async with self.client.stream("GET", f"{DRIVE_API}/{drive_id}",
                                          params={"alt": "media", "supportsAllDrives": "true"}) as response:
                if response.status_code != 200:
                    raise self._error(f"Drive 파일 재다운로드 실패 (HTTP {response.status_code})", "DRIVE_HTTP_ERROR")
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        raise self._error("Drive 재다운로드 파일이 허용 크기를 초과했습니다.", "FILE_TOO_LARGE")
                    digest.update(chunk)
        except httpx.HTTPError:
            raise self._error("Drive 파일 재다운로드 중 네트워크 오류.", "DRIVE_NETWORK_ERROR") from None
        if total != expected_size:
            raise self._error("Drive 재다운로드 파일 크기가 LMS 원본과 다릅니다.", "SIZE_MISMATCH")
        return digest.hexdigest()

    async def _metadata(self, drive_id: str) -> dict:
        return (await self._request("GET", f"{DRIVE_API}/{drive_id}",
                                   params={"fields": FILE_FIELDS, "supportsAllDrives": "true"})).json()

    async def sync_file(self, file_id: int, path: Path, folder_id: str) -> dict:
        validate_folder_id(folder_id)
        self.stage = "validate_file"
        size = path.stat().st_size
        if not 0 < size <= MAX_FILE_BYTES:
            raise self._error("전송할 파일은 0바이트보다 크고 20MiB 이하여야 합니다.", "INVALID_FILE_SIZE")
        data = path.read_bytes()
        mime_type = file_mime_type(path, data)
        source_hash = hashlib.sha256(data).hexdigest()
        self.stage = "compare_drive"
        existing = await self._find(folder_id, file_id, path.name)
        decision = choose_drive_filename(
            path.name, existing_drive_name=(existing or {}).get("name"),
        )
        # Preserve the exact current name, including user formatting. Existing
        # file updates omit name entirely so a concurrent rename also survives.
        drive_name = existing["name"] if existing else decision.drive_filename
        properties = dict((existing or {}).get("appProperties", {}))
        properties[_SOURCE_KEY] = str(file_id)
        properties["ku_lms_sha256"] = source_hash
        drive_id = (existing or {}).get("id")
        action = "created"
        if existing:
            if int(existing.get("size", -1)) == size and await self._read_hash(drive_id, size) == source_hash:
                patch = {}
                if existing.get("appProperties", {}).get(_SOURCE_KEY) != str(file_id):
                    patch["appProperties"] = properties
                if existing.get("mimeType") != mime_type:
                    patch["mimeType"] = mime_type
                if patch:
                    await self._request("PATCH", f"{DRIVE_API}/{drive_id}",
                                        params={"supportsAllDrives": "true"}, json=patch)
                return {"file_id": file_id, "filename": drive_name, "source_filename": path.name,
                        "mime_type": mime_type, "action": "skipped",
                        "drive_file_id": drive_id, "size": size, "sha256": source_hash,
                        "verified": True, "drive_url": f"https://drive.google.com/file/d/{drive_id}/view"}
            action = "updated"
        self.stage = "upload_drive"
        metadata = {"mimeType": mime_type, "appProperties": properties}
        if not existing:
            metadata["name"] = drive_name
            metadata["parents"] = [folder_id]
        url = f"{UPLOAD_API}/{drive_id}" if existing else UPLOAD_API
        start = await self._request("PATCH" if existing else "POST", url,
                                   params={"uploadType": "resumable", "supportsAllDrives": "true",
                                           "fields": FILE_FIELDS},
                                   headers={"X-Upload-Content-Type": mime_type,
                                            "X-Upload-Content-Length": str(size)}, json=metadata)
        location = start.headers.get("Location", "")
        parsed = urlparse(location)
        if parsed.scheme != "https" or parsed.netloc != "www.googleapis.com":
            raise self._error("Drive 재개 가능 업로드 URL의 출처가 올바르지 않습니다.", "UPLOAD_ORIGIN_INVALID")
        uploaded = (await self._request("PUT", location, content=data,
                    headers={"Content-Type": mime_type, "Content-Length": str(size),
                             "Content-Range": f"bytes 0-{size-1}/{size}"})).json()
        uploaded_id = uploaded.get("id")
        if not uploaded_id or (drive_id and drive_id != uploaded_id):
            raise self._error("Drive 업로드 결과의 기존 파일 ID가 일치하지 않습니다.", "DRIVE_ID_MISMATCH")
        self.stage = "verify_drive"
        final = await self._metadata(uploaded_id)
        if folder_id not in final.get("parents", []) or int(final.get("size", -1)) != size:
            raise self._error("Drive 업로드 후 파일 크기 또는 대상 폴더가 일치하지 않습니다.", "DESTINATION_MISMATCH")
        if await self._read_hash(uploaded_id, size) != source_hash:
            raise self._error("Drive 업로드 후 SHA-256이 LMS 원본과 다릅니다.", "SHA256_MISMATCH")
        return {"file_id": file_id, "filename": final["name"], "source_filename": path.name,
                "mime_type": mime_type, "action": action,
                "drive_file_id": uploaded_id, "size": size, "sha256": source_hash,
                "verified": True, "drive_url": f"https://drive.google.com/file/d/{uploaded_id}/view"}

    async def sync_pdf(self, file_id: int, path: Path, folder_id: str) -> dict:
        """Compatibility alias; supports the same original LMS files as sync_file."""
        return await self.sync_file(file_id, path, folder_id)
