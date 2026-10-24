import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from ku_portal_mcp.drive import DriveClient, DriveSyncError, validate_folder_id
import ku_portal_mcp.server as server
import ku_portal_mcp.lms as lms

FOLDER = "testfolder12345"
DRIVE_ID = "existingDrive12345"
PDF = b"%PDF-1.7\noriginal lecture"
NEW_PDF = b"%PDF-1.7\nnew lecture content"


@pytest.fixture(autouse=True)
def allowed_folder(monkeypatch):
    monkeypatch.setenv("KU_DRIVE_FOLDER_IDS", FOLDER)


def run_sync(tmp_path, *, existing=None, payload=PDF, bad_location=False, corrupt=False,
             filename="7. Text classification 3.pdf", mime_type="application/pdf", existing_payload=None,
             drive_name=None):
    path = tmp_path / filename
    path.write_bytes(payload)
    calls = []
    uploaded = {"done": False}
    old = existing or {}
    target_id = old.get("id", DRIVE_ID)
    target_name = drive_name or (old["name"] if existing else f"[강의] {filename}")

    def metadata():
        return {"id": target_id, "name": target_name, "mimeType": mime_type,
                "size": str(len(payload)), "parents": [FOLDER],
                "appProperties": {"ku_lms_file_id": "6242752"}}

    def respond(request):
        calls.append(request)
        if request.method == "GET" and request.url.path == "/drive/v3/files":
            return httpx.Response(200, json={"files": [old] if existing else []})
        if request.method == "GET" and request.url.params.get("alt") == "media":
            body = b"x" * len(payload) if corrupt else (existing_payload if existing_payload is not None and not uploaded["done"] else payload)
            return httpx.Response(200, content=body)
        if request.method in ("POST", "PATCH") and "/upload/" in request.url.path:
            value = json.loads(request.content)
            assert value["mimeType"] == mime_type
            assert request.headers["X-Upload-Content-Type"] == mime_type
            if existing:
                assert request.method == "PATCH"
                assert request.url.path.endswith(target_id)
                assert "parents" not in value
                assert "name" not in value
            else:
                assert request.method == "POST"
                assert value["parents"] == [FOLDER]
                assert value["name"] == target_name
            location = "https://evil.example/upload" if bad_location else "https://www.googleapis.com/upload/session"
            return httpx.Response(200, headers={"Location": location})
        if request.method == "PUT":
            assert request.content == payload
            assert request.headers["Content-Type"] == mime_type
            uploaded["done"] = True
            return httpx.Response(200, json=metadata())
        if request.method == "PATCH":
            patch = json.loads(request.content)
            assert "name" not in patch
            if "appProperties" in patch:
                assert patch["appProperties"]["ku_lms_file_id"] == "6242752"
            if "mimeType" in patch:
                assert patch["mimeType"] == mime_type
            return httpx.Response(200, json=metadata())
        if request.method == "GET":
            return httpx.Response(200, json=metadata())
        raise AssertionError(str(request.url))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            return await DriveClient(http).sync_file(6242752, path, FOLDER)
    return asyncio.run(go()), calls, uploaded


def existing_pdf(**extra):
    return {"id": DRIVE_ID, "name": "7. Text classification 3.pdf",
            "mimeType": "application/pdf", "size": str(len(PDF)), "parents": [FOLDER],
            "appProperties": {"ku_lms_file_id": "6242752"}, **extra}


def test_identical_pdf_skips_without_upload(tmp_path):
    result, calls, uploaded = run_sync(tmp_path, existing=existing_pdf())
    assert result["action"] == "skipped"
    assert result["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert not uploaded["done"]
    assert all(r.method == "GET" for r in calls)


def test_existing_unlinked_pdf_is_adopted_without_reupload(tmp_path):
    result, calls, uploaded = run_sync(tmp_path, existing=existing_pdf(appProperties={}))
    assert result["action"] == "skipped"
    assert result["drive_file_id"] == DRIVE_ID
    assert len([r for r in calls if r.method == "PATCH"]) == 1
    assert not uploaded["done"]


@pytest.mark.parametrize("drive_name", [
    "[강의] 7. Text classification 3.pdf",
    "내가 정리한 강의.pdf",
    "  [강의] 7. Text classification 3.pdf  ",
])
def test_identical_file_preserves_exact_drive_name_without_writes(tmp_path, drive_name):
    result, calls, uploaded = run_sync(tmp_path, existing=existing_pdf(name=drive_name))
    assert result["action"] == "skipped"
    assert result["filename"] == drive_name
    assert result["source_filename"] == "7. Text classification 3.pdf"
    assert result["drive_file_id"] == DRIVE_ID
    assert all(r.method == "GET" for r in calls)
    assert not uploaded["done"]


def test_prefixed_unlinked_file_is_found_and_adopted_without_renaming(tmp_path):
    drive_name = "[강의] 7. Text classification 3.pdf"
    result, calls, uploaded = run_sync(
        tmp_path, existing=existing_pdf(name=drive_name, appProperties={}),
    )
    query = calls[0].url.params["q"]
    assert f"name='{drive_name}'" in query
    assert "name='7. Text classification 3.pdf'" in query
    assert result["filename"] == drive_name
    assert result["drive_file_id"] == DRIVE_ID
    patches = [r for r in calls if r.method == "PATCH"]
    assert len(patches) == 1
    assert set(json.loads(patches[0].content)) == {"appProperties"}
    assert not uploaded["done"]


@pytest.mark.parametrize("payload", [NEW_PDF, PDF.replace(b"original", b"modified")])
def test_revision_preserves_prefixed_drive_name_and_id(tmp_path, payload):
    drive_name = "[강의] 7. Text classification 3.pdf"
    result, calls, uploaded = run_sync(
        tmp_path, existing=existing_pdf(name=drive_name), payload=payload, existing_payload=PDF,
    )
    assert result["action"] == "updated"
    assert result["filename"] == drive_name
    assert result["source_filename"] == "7. Text classification 3.pdf"
    assert result["drive_file_id"] == DRIVE_ID
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["verified"] and uploaded["done"]
    assert not any(r.method == "POST" for r in calls)


def test_revision_updates_same_drive_id_and_reads_back(tmp_path):
    result, calls, uploaded = run_sync(tmp_path, existing=existing_pdf(), payload=NEW_PDF)
    assert result["action"] == "updated"
    assert result["drive_file_id"] == DRIVE_ID
    assert result["sha256"] == hashlib.sha256(NEW_PDF).hexdigest()
    assert result["verified"] and uploaded["done"]
    assert not any(r.method == "POST" for r in calls)


def test_new_pdf_creates_in_existing_folder_and_verifies(tmp_path):
    result, calls, uploaded = run_sync(tmp_path)
    assert result["action"] == "created" and result["verified"]
    assert uploaded["done"]
    assert result["filename"] == "[강의] 7. Text classification 3.pdf"
    assert result["source_filename"] == "7. Text classification 3.pdf"
    assert any(r.url.params.get("alt") == "media" for r in calls)


def test_foreign_resumable_url_is_rejected_before_put(tmp_path):
    with pytest.raises(DriveSyncError, match="출처"):
        run_sync(tmp_path, bad_location=True)


def test_bad_readback_is_not_reported_as_verified(tmp_path):
    with pytest.raises(DriveSyncError, match="SHA-256"):
        run_sync(tmp_path, corrupt=True)


def test_unknown_folder_and_non_pdf_are_rejected(tmp_path):
    with pytest.raises(DriveSyncError):
        validate_folder_id("unapprovedFolder12345")
    with pytest.raises(DriveSyncError, match="실제 PDF"):
        run_sync(tmp_path, payload=b"<html>login</html>")


def test_duplicate_destination_is_not_overwritten(tmp_path):
    path = tmp_path / "file.pdf"
    path.write_bytes(PDF)
    def respond(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"files": [existing_pdf(), existing_pdf(id="otherDrive12345")]})
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            await DriveClient(http).sync_file(6242752, path, FOLDER)
    with pytest.raises(DriveSyncError, match="여러"):
        asyncio.run(go())


@pytest.mark.parametrize("token", [None, SimpleNamespace(scopes=["ku:read"])])
def test_remote_tool_requires_explicit_drive_scope(monkeypatch, token):
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "https://example.test/ku-mcp")
    monkeypatch.setattr(server, "get_access_token", lambda: token)
    result = asyncio.run(server.kupid_lms_sync_to_drive([6242752], FOLDER))
    assert result.isError
    assert "ku:drive" in result.meta["mcp/www_authenticate"]
    assert 'resource_metadata="https://example.test/.well-known/oauth-protected-resource/ku-mcp/mcp"' in result.meta["mcp/www_authenticate"]


def test_batch_failure_continues_and_cleans_temp_files(monkeypatch):
    monkeypatch.setenv("MCP_PUBLIC_BASE_URL", "https://example.test/ku-mcp")
    monkeypatch.setattr(server, "get_access_token", lambda: SimpleNamespace(scopes=["ku:read", "ku:drive"]))
    dirs = []
    class FakeDrive:
        closed = False
        async def check_folder(self, folder): assert folder == FOLDER
        async def sync_file(self, file_id, path, folder):
            assert path.exists()
            if file_id == 1:
                raise DriveSyncError("first failed", code="INVALID_FILE_FORMAT", stage="validate_file")
            return {"file_id": file_id, "action": "skipped", "verified": True}
        async def close(self): self.closed = True
    fake = FakeDrive()
    async def connect(): return fake
    async def download(session, file_id, save_dir, filename=None, **kwargs):
        dirs.append(save_dir)
        path = save_dir / "native.pdf"
        path.write_bytes(PDF)
        return {"path": str(path)}
    async def retry(fn): return await fn(object())
    monkeypatch.setattr(server.DriveClient, "connect", connect)
    monkeypatch.setattr(server, "download_lms_file", download)
    monkeypatch.setattr(server, "_lms_with_retry", retry)
    result = asyncio.run(server.kupid_lms_sync_to_drive([1, 2], FOLDER))
    assert result.isError
    assert [r["success"] for r in result.structuredContent["results"]] == [False, True]
    assert all(not p.exists() for p in dirs)
    assert fake.closed
    failure = result.structuredContent["results"][0]
    assert failure["error_code"] == "INVALID_FILE_FORMAT"
    assert failure["error_message"] == "first failed"
    assert failure["stage"] == "validate_file"
    assert '"error_code": "INVALID_FILE_FORMAT"' in result.content[0].text


def test_tool_descriptor_exposes_write_annotations_and_scopes():
    tools = asyncio.run(server.server.list_tools())
    tool = next(t for t in tools if t.name == "kupid_lms_sync_to_drive")
    assert not tool.annotations.readOnlyHint
    assert tool.annotations.destructiveHint
    assert tool.meta["securitySchemes"][0]["scopes"] == ["ku:read", "ku:drive"]
    assert tool.model_dump()["securitySchemes"][0]["scopes"] == ["ku:read", "ku:drive"]
    assert all("securitySchemes" in t.model_dump() for t in tools)


def test_download_limit_stops_stream_and_removes_partial(monkeypatch, tmp_path):
    async def info(session, file_id):
        return {"url": "https://mylms.korea.ac.kr/file", "display_name": "native.pdf"}
    original_client = httpx.AsyncClient
    def client(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=PDF * 100, headers={"content-type": "application/pdf"})))
    monkeypatch.setattr(lms, "fetch_lms_file_info", info)
    monkeypatch.setattr(lms.httpx, "AsyncClient", client)
    with pytest.raises(lms.LMSFileTooLargeError):
        asyncio.run(lms.download_lms_file(SimpleNamespace(cookies={}), 1, tmp_path, max_bytes=32))
    assert not list(tmp_path.iterdir())


NOTEBOOK = b'{"cells": [{"cell_type": "code", "source": ["a"], "outputs": []}], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}\n'


@pytest.mark.parametrize("filename,payload,mime,drive_name", [
    ("7장_데이터_시각화와_Agent.ipynb", NOTEBOOK, "application/x-ipynb+json", "[실습] 7장_데이터_시각화와_Agent.ipynb"),
    ("data.csv", b"x,y\n1,2\n", "text/csv", "[실습데이터] data.csv"),
    ("lecture.pptx", b"PK\x03\x04lecture slides", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "[강의] lecture.pptx"),
    ("code.py", b"print('hello')\n", "text/x-python", "[코드] code.py"),
    ("dataset.zip", b"PK\x03\x04dataset", "application/zip", "[자료] dataset.zip"),
    ("assignment2.ipynb", NOTEBOOK, "application/x-ipynb+json", "[과제] assignment2.ipynb"),
    ("assignment2_solution.pdf", PDF, "application/pdf", "[정답] assignment2_solution.pdf"),
    ("syllabus.pdf", PDF, "application/pdf", "[강의계획서] syllabus.pdf"),
    ("README.md", b"course guide", "text/markdown", "[안내] README.md"),
    ("[실습] native.ipynb", NOTEBOOK, "application/x-ipynb+json", "[실습] native.ipynb"),
])
def test_native_teaching_formats_keep_original_bytes_and_mime(tmp_path, filename, payload, mime, drive_name):
    result, calls, uploaded = run_sync(tmp_path, filename=filename, payload=payload, mime_type=mime,
                                      drive_name=drive_name)
    assert result["action"] == "created" and result["verified"]
    assert result["filename"] == drive_name
    assert result["source_filename"] == filename
    assert result["mime_type"] == mime
    assert result["size"] == len(payload)
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert uploaded["done"]


def test_identical_notebook_skips_binary_upload_and_corrects_old_mime(tmp_path):
    filename = "native.ipynb"
    existing = existing_pdf(name=filename, size=str(len(NOTEBOOK)), mimeType="application/octet-stream")
    result, calls, uploaded = run_sync(tmp_path, filename=filename, payload=NOTEBOOK,
                                      existing=existing, mime_type="application/x-ipynb+json")
    assert result["action"] == "skipped" and result["verified"]
    assert not uploaded["done"]
    assert not any("/upload/" in r.url.path for r in calls)
    assert any(r.method == "PATCH" for r in calls)


def test_same_size_notebook_revision_updates_existing_id(tmp_path):
    revised = NOTEBOOK.replace(b'["a"]', b'["b"]')
    assert len(revised) == len(NOTEBOOK) and revised != NOTEBOOK
    existing = existing_pdf(name="native.ipynb", size=str(len(NOTEBOOK)), mimeType="application/x-ipynb+json")
    result, calls, uploaded = run_sync(tmp_path, filename="native.ipynb", payload=revised,
        existing_payload=NOTEBOOK, existing=existing, mime_type="application/x-ipynb+json")
    assert result["action"] == "updated" and result["drive_file_id"] == DRIVE_ID
    assert result["sha256"] == hashlib.sha256(revised).hexdigest()
    assert result["verified"] and uploaded["done"]


def test_invalid_notebook_returns_precise_validation_error(tmp_path):
    with pytest.raises(DriveSyncError) as error:
        run_sync(tmp_path, filename="bad.ipynb", payload=b"<html>login</html>")
    assert error.value.error_code == "INVALID_FILE_FORMAT"
    assert error.value.stage == "validate_file"


def test_native_google_document_is_not_replaced_by_binary(tmp_path):
    with pytest.raises(DriveSyncError) as error:
        run_sync(tmp_path, existing=existing_pdf(mimeType="application/vnd.google-apps.document"))
    assert error.value.error_code == "DESTINATION_TYPE_CONFLICT"
    assert error.value.stage == "compare_drive"


def test_auto_course_folders_do_not_require_user_folder_and_continue_failures(monkeypatch):
    monkeypatch.delenv("MCP_PUBLIC_BASE_URL", raising=False)
    other_folder = "otherfolder12345"
    monkeypatch.setenv("KU_DRIVE_FOLDER_IDS", FOLDER + "," + other_folder)
    monkeypatch.setenv("KU_DRIVE_COURSE_FOLDERS", json.dumps({94859: FOLDER, 99088: other_folder}))
    routed = []
    class FakeDrive:
        async def check_folder(self, folder): pass
        async def sync_file(self, file_id, path, folder):
            routed.append((file_id, folder))
            return {"file_id": file_id, "action": "skipped", "verified": True}
        async def close(self): pass
    async def connect(): return FakeDrive()
    async def resolve(session, file_id, courses):
        assert set(courses) == {94859, 99088}
        if file_id == 3:
            raise lms.LMSFileCourseError("unknown course", code="COURSE_NOT_CONFIGURED")
        return 94859 if file_id == 1 else 99088
    async def download(session, file_id, save_dir, **kwargs):
        path = save_dir / "native.ipynb"
        path.write_bytes(NOTEBOOK)
        return {"path": str(path)}
    async def retry(fn): return await fn(object())
    monkeypatch.setattr(server.DriveClient, "connect", connect)
    monkeypatch.setattr(server, "resolve_lms_file_course", resolve)
    monkeypatch.setattr(server, "download_lms_file", download)
    monkeypatch.setattr(server, "_lms_with_retry", retry)
    result = asyncio.run(server.kupid_lms_sync_to_drive([1, 3, 2]))
    assert routed == [(1, FOLDER), (2, other_folder)]
    assert [r["success"] for r in result.structuredContent["results"]] == [True, False, True]
    assert result.structuredContent["results"][1]["stage"] == "resolve_course"
    assert result.structuredContent["results"][1]["error_code"] == "COURSE_NOT_CONFIGURED"


def test_download_error_does_not_expose_signed_url(monkeypatch):
    monkeypatch.delenv("MCP_PUBLIC_BASE_URL", raising=False)
    class FakeDrive:
        async def check_folder(self, folder): pass
        async def close(self): pass
    async def connect(): return FakeDrive()
    async def download(*args, **kwargs):
        raise RuntimeError("https://example.test/file?verifier=private-secret")
    async def retry(fn): return await fn(object())
    monkeypatch.setattr(server.DriveClient, "connect", connect)
    monkeypatch.setattr(server, "download_lms_file", download)
    monkeypatch.setattr(server, "_lms_with_retry", retry)
    result = asyncio.run(server.kupid_lms_sync_to_drive([1], FOLDER))
    row = result.structuredContent["results"][0]
    assert row["stage"] == "download_lms" and row["error_code"] == "LMS_DOWNLOAD_FAILED"
    assert "private-secret" not in result.content[0].text
    assert "verifier" not in json.dumps(result.structuredContent)


@pytest.mark.parametrize("matches,code", [([94859], None), ([], "COURSE_NOT_CONFIGURED"), ([94859, 99088], "COURSE_AMBIGUOUS")])
def test_course_is_proven_by_scoped_canvas_file_lookup(monkeypatch, matches, code):
    def respond(request):
        cid = int(request.url.path.split("/")[4])
        assert request.url.path.endswith("/files/6258929")
        return httpx.Response(200, json={"id": 6258929}) if cid in matches else httpx.Response(404)
    def client(session):
        return httpx.AsyncClient(base_url="https://mylms.korea.ac.kr", transport=httpx.MockTransport(respond))
    monkeypatch.setattr(lms, "_api_client", client)
    async def go(): return await lms.resolve_lms_file_course(object(), 6258929, [94859, 99088])
    if code:
        with pytest.raises(lms.LMSFileCourseError) as error:
            asyncio.run(go())
        assert error.value.error_code == code
    else:
        assert asyncio.run(go()) == 94859
