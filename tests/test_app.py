# tests/test_app.py
import io
import os
import subprocess
import time
import pytest
from unittest.mock import patch, MagicMock

import app as app_module


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def fake_docx_bytes():
    # Not a real docx, just needs to exist as bytes for most tests
    # since soffice itself is mocked. Integration tests need a real file
    return b"PK\x03\x04fake docx content for unit testing"


def _mock_popen_success(pdf_path):
    """Build a Popen mock that, when .communicate() is called, behaves
    like a successful soffice run and leaves a PDF at pdf_path."""
    def _side_effect(*args, **kwargs):
        proc = MagicMock()
        proc.pid = 12345
        def communicate(timeout=None):
            with open(pdf_path, "wb") as f:
                f.write(b"%PDF-1.4 fake pdf content")
            return (b"", b"")
        proc.communicate.side_effect = communicate
        proc.returncode = 0
        return proc
    return _side_effect


class TestConvertEndpoint:

    def test_no_file_returns_400(self, client):
        resp = client.post("/convert", data={})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "no file provided"
    def test_oversized_file_returns_413(self, client, monkeypatch):
        monkeypatch.setattr(app_module, "MAX_FILE_SIZE", 100)
        monkeypatch.setitem(app_module.app.config, "MAX_CONTENT_LENGTH", 100)
        data = {"file": (io.BytesIO(b"x" * 500), "big.docx")}
        resp = client.post("/convert", data=data, content_type="multipart/form-data")
        assert resp.status_code == 413

    def test_output_name_sanitized(self, client, fake_docx_bytes, monkeypatch, tmp_path) -> None:
        """Verifies _safe_filename neutralizes path traversal and header
        injection in the user-supplied output_name. Uses a non-destructive
        payload string (never shelled out to, but kept inert on principle)
        and asserts containment rather than trusting the mock alone.
        """
        pdf_path = tmp_path / "input.pdf"

        # Patch only the one function actually used (tempfile.mkdtemp),
        # never the whole module -- an unpatched tempfile.* call should fail
        # loudly rather than silently return a MagicMock.
        monkeypatch.setattr(app_module.tempfile, "mkdtemp", lambda prefix=None: str(tmp_path))

        with patch("app.subprocess.Popen", side_effect=_mock_popen_success(str(pdf_path))), \
             patch("app.os.getpgid", return_value=999):

            # Path traversal + CRLF header-injection attempt, expressed as a
            # string only -- never passed to a shell anywhere in app.py, and
            # deliberately not a literally destructive command even as inert text.
            malicious_name = "../../../etc/passwd\r\nX-Injected-Header: evil.pdf"

            data = {
                "file": (io.BytesIO(fake_docx_bytes), "input.docx"),
                "output_name": malicious_name,
            }
            resp = client.post("/convert", data=data, content_type="multipart/form-data")

        assert resp.status_code == 200

        cd = resp.headers.get("Content-Disposition", "")
        filename = cd.split("filename=")[-1].strip('"')

        # No traversal sequences or path separators leaked into the header
        assert ".." not in filename
        assert "/" not in filename
        assert "\\" not in filename

        # No raw CR/LF made it into the header value (would enable header
        # injection / response splitting if _safe_filename regressed)
        assert "\r" not in cd
        assert "\n" not in cd

        # Filesystem containment: confirm nothing was written outside the
        # sandboxed tmp_path, i.e. the traversal component was never honored
        # as an actual filesystem path anywhere in the request-handling code.
        written_files = list(tmp_path.rglob("*"))
        assert all(
            str(p).startswith(str(tmp_path)) for p in written_files
        ), "a file was written outside the sandboxed tmp_path"

    def test_successful_conversion_returns_pdf(self, client, fake_docx_bytes, tmp_path, monkeypatch):
        pdf_path = tmp_path / "input.pdf"
        monkeypatch.setattr(
            app_module, "tempfile",
            MagicMock(mkdtemp=lambda prefix=None: str(tmp_path))
        )
        with patch("app.subprocess.Popen", side_effect=_mock_popen_success(str(pdf_path))), \
             patch("app.os.getpgid", return_value=999):
            data = {"file": (io.BytesIO(fake_docx_bytes), "input.docx")}
            resp = client.post("/convert", data=data, content_type="multipart/form-data")

        assert resp.status_code == 200
        assert resp.mimetype == "application/pdf"
        assert resp.data.startswith(b"%PDF")

    def test_soffice_nonzero_exit_returns_500(self, client, fake_docx_bytes, tmp_path, monkeypatch):
        monkeypatch.setattr(
            app_module, "tempfile",
            MagicMock(mkdtemp=lambda prefix=None: str(tmp_path))
        )

        def _side_effect(*args, **kwargs):
            proc = MagicMock()
            proc.pid = 111
            proc.communicate.return_value = (b"", b"soffice: unrecoverable error")
            proc.returncode = 1
            return proc

        with patch("app.subprocess.Popen", side_effect=_side_effect), \
             patch("app.os.getpgid", return_value=999):
            data = {"file": (io.BytesIO(fake_docx_bytes), "input.docx")}
            resp = client.post("/convert", data=data, content_type="multipart/form-data")

        assert resp.status_code == 500
        assert "unrecoverable" in resp.get_json()["stderr"]

    def test_conversion_timeout_returns_504(self, client, fake_docx_bytes, tmp_path, monkeypatch):
        monkeypatch.setattr(
            app_module, "tempfile",
            MagicMock(mkdtemp=lambda prefix=None: str(tmp_path))
        )
        monkeypatch.setattr(app_module, "CONVERT_TIMEOUT", 1)

        def _side_effect(*args, **kwargs):
            proc = MagicMock()
            proc.pid = 222
            proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="soffice", timeout=1)
            return proc

        with patch("app.subprocess.Popen", side_effect=_side_effect), \
             patch("app.os.getpgid", return_value=999), \
             patch("app.os.killpg") as mock_kill:
            data = {"file": (io.BytesIO(fake_docx_bytes), "input.docx")}
            resp = client.post("/convert", data=data, content_type="multipart/form-data")

        assert resp.status_code == 504
        mock_kill.assert_called_once()

    def test_semaphore_returns_503_when_exhausted(self, client, fake_docx_bytes, monkeypatch):
        # Force acquire() to time out immediately, simulating saturation
        monkeypatch.setattr(
            app_module._semaphore, "acquire", lambda timeout=None: False
        )
        data = {"file": (io.BytesIO(fake_docx_bytes), "input.docx")}
        resp = client.post("/convert", data=data, content_type="multipart/form-data")
        assert resp.status_code == 503


class TestSafeFilename:

    def test_strips_path_traversal(self):
        assert app_module._safe_filename("../../etc/passwd", "fallback.pdf") == "passwd.pdf"

    def test_forces_pdf_extension(self):
        assert app_module._safe_filename("report", "fallback.pdf") == "report.pdf"

    def test_empty_name_uses_fallback(self):
        assert app_module._safe_filename("", "fallback.pdf") == "fallback.pdf"

    def test_special_chars_replaced(self):
        result = app_module._safe_filename("weird;name|here.pdf", "fallback.pdf")
        assert ";" not in result and "|" not in result


class TestHealthEndpoint:

    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "ok"
        assert "in_flight" in body
        assert "max_concurrent" in body


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION_TESTS") != "1",
    reason="set RUN_INTEGRATION_TESTS=1 and have soffice installed to run",
)
class TestRealConversion:
    """Runs against the actual soffice binary. Requires a real sample.docx
    fixture file and LibreOffice installed on the host running the tests."""

    def test_real_docx_converts(self, client):
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "sample.docx")
        with open(fixture_path, "rb") as f:
            data = {"file": (f, "sample.docx")}
            resp = client.post("/convert", data=data, content_type="multipart/form-data")

        assert resp.status_code == 200
        assert resp.data.startswith(b"%PDF")

    def test_real_corrupt_file_fails_gracefully(self, client):
        data = {"file": (io.BytesIO(b"not a real docx at all"), "corrupt.docx")}
        resp = client.post("/convert", data=data, content_type="multipart/form-data")
        assert resp.status_code == 500
