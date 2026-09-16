"""Mocked tests for PDF fetch safety bounds (VH-01).

No real network, no real Poppler: the curl_cffi session and the subprocess
creation are both stubbed out. Only the new helpers in metadata.images are
exercised end to end.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import metadata.images as images_module
from metadata.images import _download_pdf_capped, _run_pdftoppm


class FakeHeaders:
    def __init__(self, headers: dict):
        self._h = {k.lower(): v for k, v in headers.items()}

    def get(self, key, default=None):
        return self._h.get(key.lower(), default)


class FakeResponse:
    """Mimics curl_cffi Response just enough for _download_pdf_capped."""

    def __init__(self, status_code=200, headers=None, chunks=None, redirects=None):
        self.status_code = status_code
        self.headers = FakeHeaders(headers or {})
        self._chunks = chunks or []
        self.closed = False

    async def aclose(self):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    async def aiter_content(self, chunk_size):
        for c in self._chunks:
            yield c


class FakeSession:
    """Records every requested URL; serves canned responses per URL."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.requested: list[str] = []

    async def get(self, url, **kwargs):
        self.requested.append(url)
        assert (
            kwargs.get("allow_redirects") is False
        ), "must never follow redirects automatically"
        assert kwargs.get("stream") is True
        return self.responses[url]


# --- Scenario 1: public -> private redirect must never be followed ----------


async def test_public_to_private_redirect_is_never_followed():
    session = FakeSession(
        {
            "https://example.com/redirect": FakeResponse(
                status_code=302,
                headers={"location": "http://169.254.169.254/latest/meta-data/"},
            ),
            "http://169.254.169.254/latest/meta-data/": FakeResponse(),
        }
    )
    result = await _download_pdf_capped(
        session, "https://example.com/redirect", max_bytes=1024
    )
    assert result is None
    # The redirect was detected on hop 1; the private target was never requested.
    assert session.requested == ["https://example.com/redirect"]


async def test_public_to_private_hostname_redirect_is_never_requested():
    # A public page redirects to a hostname that resolves to a private
    # address (metadata.internal). The FakeSession has no canned response,
    # so any attempt to request it would raise; the helper must reject the
    # resolved target before ever calling get() on it.
    session = FakeSession(
        {
            "https://example.com/redirect": FakeResponse(
                status_code=302,
                headers={"location": "https://metadata.internal/secret.pdf"},
            ),
        }
    )
    result = await _download_pdf_capped(
        session, "https://example.com/redirect", max_bytes=1024
    )
    assert result is None
    # The .internal name resolved to a non-global address and was rejected.
    assert session.requested == ["https://example.com/redirect"]


# --- Scenario 2: oversized stream terminates at the byte limit --------------


async def test_oversized_stream_terminates_at_byte_limit():
    chunk = b"x" * (64 * 1024)
    many_chunks = [chunk] * 40  # 2.5 MB total
    resp = FakeResponse(
        status_code=200,
        headers={"content-type": "application/pdf"},
        chunks=many_chunks,
    )
    session = FakeSession({"https://example.com/a.pdf": resp})
    limit = 1024 * 1024  # 1 MB
    result = await _download_pdf_capped(session, "https://example.com/a.pdf", limit)
    assert result is None

    # A stream just under the limit goes through whole.
    small_chunks = [chunk] * 15  # 960 KB
    resp_small = FakeResponse(
        status_code=200,
        headers={"content-type": "application/pdf"},
        chunks=small_chunks,
    )
    session_small = FakeSession({"https://example.com/a.pdf": resp_small})
    result_small = await _download_pdf_capped(
        session_small, "https://example.com/a.pdf", limit
    )
    assert result_small is not None
    assert len(result_small) == 15 * (64 * 1024)


# --- Scenario 3: a hung Poppler is killed within the timeout -----------------


class HungProc:
    def __init__(self):
        self.returncode = None
        self.killed = False

    async def communicate(self):
        await asyncio.sleep(999)  # simulates a hung renderer

    async def wait(self):
        self.returncode = -9
        return -9

    def kill(self):
        self.killed = True
        self.returncode = -9


async def test_hung_pdftoppm_is_killed_within_timeout(monkeypatch):
    proc = HungProc()

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(
        images_module, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(images_module, "PDF_POPPLER_TIMEOUT_SECONDS", 0.05)

    import time

    start = time.monotonic()
    try:
        await _run_pdftoppm("dummy.pdf", "output")
        raise AssertionError("expected TimeoutError")
    except TimeoutError as exc:
        elapsed = time.monotonic() - start
        assert "killed" in str(exc)
        assert elapsed < 2.0, f"killed after {elapsed}s, should be near the timeout"
    assert proc.killed is True
    assert proc.returncode == -9
