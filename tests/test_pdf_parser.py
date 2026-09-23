"""PDF parsing from in-memory bytes (no network)."""
import asyncio
import base64

import pymupdf

from mind2web2.api_tools.tool_pdf import PDFParser


def _make_pdf(pages: list[str]) -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def test_extract_returns_text_and_one_image_per_page():
    pdf = _make_pdf(["First page about Mind2Web", "Second page"])
    images, text = asyncio.run(PDFParser().extract(pdf))
    assert "First page about Mind2Web" in text
    assert "Second page" in text
    assert len(images) == 2
    assert base64.b64decode(images[0])[:2] == b"\xff\xd8"  # JPEG


def test_extract_rejects_non_pdf_bytes():
    images, text = asyncio.run(PDFParser().extract(b"<html>not a pdf</html>"))
    assert text.startswith("PDF extraction failed")
    assert len(images) == 1
