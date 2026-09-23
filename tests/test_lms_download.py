import asyncio
import errno
from pathlib import Path
from types import SimpleNamespace

import pytest

from ku_portal_mcp import lms


class _FakeResponse:
    def __init__(
        self,
        chunks: list[bytes],
        content_type: str,
        exception_after: BaseException | None = None,
    ):
        self._chunks = chunks
        self._exception_after = exception_after
        self.headers = {"content-type": content_type}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    async def aiter_bytes(self, chunk_size: int):
        assert chunk_size == 65536
        for chunk in self._chunks:
            yield chunk
        if self._exception_after is not None:
            raise self._exception_after


class _FakeClient:
    def __init__(self, response: _FakeResponse, **kwargs):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method: str, url: str):
        assert method == "GET"
        assert url == "https://example.test/file"
        return self.response


def _run_download(
    monkeypatch,
    tmp_path,
    chunks,
    response_content_type,
    *,
    display_name="lecture.pdf",
    declared_content_type="application/pdf",
    filename=None,
    exception_after=None,
):
    async def fake_fetch_lms_file_info(session, file_id):
        assert file_id == 123
        return {
            "url": "https://example.test/file",
            "display_name": display_name,
            "content-type": declared_content_type,
        }

    response = _FakeResponse(chunks, response_content_type, exception_after)
    monkeypatch.setattr(lms, "fetch_lms_file_info", fake_fetch_lms_file_info)
    monkeypatch.setattr(
        lms.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeClient(response, **kwargs),
    )
    session = SimpleNamespace(cookies={})
    return asyncio.run(lms.download_lms_file(session, 123, tmp_path, filename))


def test_download_lms_pdf_validates_magic_bytes(monkeypatch, tmp_path):
    payload = b"%PDF-1.7\nbody\n%%EOF"

    result = _run_download(
        monkeypatch,
        tmp_path,
        [payload[:3], payload[3:]],
        "application/pdf",
    )

    assert (tmp_path / "lecture.pdf").read_bytes() == payload
    assert result["size"] == len(payload)
    assert result["content_type"] == "application/pdf"


def test_download_lms_pdf_rejects_html_and_removes_file(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="HTML 응답"):
        _run_download(
            monkeypatch,
            tmp_path,
            [b"<!DOCTYPE html><html>login</html>"],
            "text/html; charset=utf-8",
        )

    assert not (tmp_path / "lecture.pdf").exists()


def test_download_lms_non_pdf_rejects_html_and_removes_file(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="HTML 응답"):
        _run_download(
            monkeypatch,
            tmp_path,
            [b"<!DOCTYPE html><html>login</html>"],
            "text/html; charset=utf-8",
            display_name="lecture.pptx",
            declared_content_type=(
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            ),
        )

    assert not (tmp_path / "lecture.pptx").exists()


def test_download_lms_rejects_html_body_with_misleading_mime(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="HTML 본문"):
        _run_download(
            monkeypatch,
            tmp_path,
            [b"  <!DOCTYPE html><html>login</html>"],
            "application/octet-stream",
            display_name="lecture.zip",
            declared_content_type="application/zip",
        )

    assert not (tmp_path / "lecture.zip").exists()


def test_download_lms_ignores_html_override_when_deciding_expected_type(
    monkeypatch, tmp_path
):
    with pytest.raises(RuntimeError, match="HTML 응답"):
        _run_download(
            monkeypatch,
            tmp_path,
            [b"<!DOCTYPE html><html>login</html>"],
            "text/html; charset=utf-8",
            display_name="lecture.zip",
            declared_content_type="application/zip",
            filename="renamed.html",
        )

    assert not (tmp_path / "renamed.html").exists()


def test_download_lms_uses_source_pdf_name_despite_non_pdf_override(
    monkeypatch, tmp_path
):
    with pytest.raises(RuntimeError, match="PDF 다운로드 실패"):
        _run_download(
            monkeypatch,
            tmp_path,
            [b"NOT A PDF"],
            "application/octet-stream",
            display_name="source.pdf",
            declared_content_type="application/octet-stream",
            filename="renamed.bin",
        )

    assert list(tmp_path.iterdir()) == []


def test_download_lms_validates_pdf_response_mime(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="PDF 다운로드 실패"):
        _run_download(
            monkeypatch,
            tmp_path,
            [b"NOT A PDF"],
            "application/pdf",
            display_name="download.bin",
            declared_content_type="application/octet-stream",
        )

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "payload",
    [
        b"\xef\xbb\xbf<!DOCTYPE html><html>login</html>",
        b"<?xml version='1.0'?><html>login</html>",
        b" " * 1500 + b"<html>login</html>",
        b" " * 9000 + b"<html>login</html>",
    ],
)
def test_download_lms_rejects_obscured_html_body(monkeypatch, tmp_path, payload):
    with pytest.raises(RuntimeError, match="HTML 본문"):
        _run_download(
            monkeypatch,
            tmp_path,
            [payload[:700], payload[700:]],
            "application/octet-stream",
            display_name="lecture.zip",
            declared_content_type="application/zip",
        )

    assert not (tmp_path / "lecture.zip").exists()


def test_download_lms_cancellation_removes_partial_file(monkeypatch, tmp_path):
    with pytest.raises(asyncio.CancelledError):
        _run_download(
            monkeypatch,
            tmp_path,
            [b"partial"],
            "application/octet-stream",
            display_name="lecture.zip",
            declared_content_type="application/zip",
            exception_after=asyncio.CancelledError(),
        )

    assert list(tmp_path.iterdir()) == []


def test_download_lms_publishes_collision_free_complete_file(monkeypatch, tmp_path):
    (tmp_path / "lecture.zip").write_bytes(b"existing")

    result = _run_download(
        monkeypatch,
        tmp_path,
        [b"new payload"],
        "application/zip",
        display_name="lecture.zip",
        declared_content_type="application/zip",
    )

    assert (tmp_path / "lecture.zip").read_bytes() == b"existing"
    assert (tmp_path / "lecture_1.zip").read_bytes() == b"new payload"
    assert result["filename"] == "lecture_1.zip"
    assert not list(tmp_path.glob("*.part"))


def test_download_lms_falls_back_when_hard_links_are_unavailable(
    monkeypatch, tmp_path
):
    def unavailable_link(source, target):
        raise OSError(errno.EACCES, "hard links unavailable")

    monkeypatch.setattr(lms.os, "link", unavailable_link)
    result = _run_download(
        monkeypatch,
        tmp_path,
        [b"payload"],
        "application/zip",
        display_name="lecture.zip",
        declared_content_type="application/zip",
    )

    assert (tmp_path / "lecture.zip").read_bytes() == b"payload"
    assert result["filename"] == "lecture.zip"


def test_download_lms_publish_failure_removes_partial(monkeypatch, tmp_path):
    monkeypatch.setattr(
        lms,
        "_publish_download",
        lambda *args: (_ for _ in ()).throw(OSError("publish failed")),
    )

    with pytest.raises(OSError, match="publish failed"):
        _run_download(
            monkeypatch,
            tmp_path,
            [b"payload"],
            "application/zip",
            display_name="lecture.zip",
            declared_content_type="application/zip",
        )

    assert list(tmp_path.iterdir()) == []


def test_download_lms_truncates_long_filename_safely(monkeypatch, tmp_path):
    display_name = "긴파일명" * 50 + ".zip"
    result = _run_download(
        monkeypatch,
        tmp_path,
        [b"payload"],
        "application/zip",
        display_name=display_name,
        declared_content_type="application/zip",
    )

    assert len(result["filename"].encode("utf-8")) <= 220
    assert result["filename"].endswith(".zip")
    assert Path(result["path"]).read_bytes() == b"payload"
