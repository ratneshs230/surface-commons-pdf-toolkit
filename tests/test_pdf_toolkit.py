"""Integration tests for /commons/pdf — run against a live surface-api at
127.0.0.1:3000 with a service-key + an existing seeded agent (the test
harness provisions one).

These tests use real fixtures (small PDFs + small PNG) and real binaries
(ghostscript, tesseract, poppler) — no mocks. The surface VM image is
expected to have all three installed.
"""
from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import httpx
import pytest


FIX = Path(__file__).parent / "fixtures"
BASE = os.environ.get("SURFACE_BASE_URL", "http://127.0.0.1:3000")
SVC_KEY = os.environ.get("SURFACE_SERVICE_KEY") or os.environ.get("SERVICE_API_KEY")
AGENT_ID = os.environ.get("SURFACE_TEST_AGENT_ID")


def _client() -> httpx.Client:
    if not SVC_KEY or not AGENT_ID:
        pytest.skip("SURFACE_SERVICE_KEY + SURFACE_TEST_AGENT_ID required")
    return httpx.Client(
        base_url=BASE,
        headers={"X-Service-Key": SVC_KEY, "X-Agent-Id": AGENT_ID},
        timeout=120.0,
    )


def test_health_public():
    r = httpx.get(f"{BASE}/commons/pdf/health", timeout=10)
    assert r.status_code == 200
    j = r.json()
    assert j["service"] == "commons.pdf"
    assert "available" in j
    assert "sc_cost" in j


def test_merge():
    with _client() as c:
        files = [
            ("files", ("a.pdf", (FIX / "a.pdf").read_bytes(), "application/pdf")),
            ("files", ("b.pdf", (FIX / "b.pdf").read_bytes(), "application/pdf")),
        ]
        r = c.post("/commons/pdf/merge", files=files)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "application/pdf"
        assert r.headers["x-pages"] == "5"
        assert r.content[:4] == b"%PDF"


def test_split():
    with _client() as c:
        files = [("file", ("a.pdf", (FIX / "a.pdf").read_bytes(), "application/pdf"))]
        r = c.post("/commons/pdf/split", files=files, data={"ranges": "1-2,3"})
        assert r.status_code == 200, r.text
        assert r.headers["x-parts"] == "2"
        z = zipfile.ZipFile(io.BytesIO(r.content))
        assert len(z.namelist()) == 2


def test_compress():
    with _client() as c:
        files = [("file", ("a.pdf", (FIX / "a.pdf").read_bytes(), "application/pdf"))]
        r = c.post("/commons/pdf/compress", files=files, data={"level": "ebook"})
        assert r.status_code == 200, r.text
        assert r.content[:4] == b"%PDF"
        assert "x-ratio" in r.headers


def test_image_to_pdf():
    with _client() as c:
        files = [
            ("files", ("img1.jpg", (FIX / "img1.jpg").read_bytes(), "image/jpeg")),
            ("files", ("img2.jpg", (FIX / "img2.jpg").read_bytes(), "image/jpeg")),
        ]
        r = c.post("/commons/pdf/image-to-pdf", files=files)
        assert r.status_code == 200, r.text
        assert r.headers["x-images"] == "2"
        assert r.content[:4] == b"%PDF"


def test_pdf_to_image():
    with _client() as c:
        files = [("file", ("a.pdf", (FIX / "a.pdf").read_bytes(), "application/pdf"))]
        r = c.post("/commons/pdf/pdf-to-image", files=files, data={"fmt": "png", "dpi": "100"})
        assert r.status_code == 200, r.text
        assert r.headers["x-pages"] == "3"
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = z.namelist()
        assert len(names) == 3
        assert all(n.endswith(".png") for n in names)


def test_ocr_image():
    with _client() as c:
        files = [("file", ("ocr_sample.png", (FIX / "ocr_sample.png").read_bytes(), "image/png"))]
        r = c.post("/commons/pdf/ocr", files=files, data={"languages": "eng"})
        assert r.status_code == 200, r.text
        assert r.content[:4] == b"%PDF"
        # Searchable text layer present?
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(r.content))
        text = "".join(p.extract_text() or "" for p in reader.pages).lower()
        assert "surface" in text or "commons" in text or "pdf" in text


def test_auth_rejected_without_key():
    r = httpx.post(f"{BASE}/commons/pdf/merge", timeout=10)
    assert r.status_code in (401, 422)
