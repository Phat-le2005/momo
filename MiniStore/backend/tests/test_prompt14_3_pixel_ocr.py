"""Prompt 14.3 Fix 3 Tests: Real Pixel-Based Receipt OCR & Strict Provenance Separation."""

from __future__ import annotations

import io
import struct
import zlib

from PIL import Image, ImageDraw, ImageFont

from app.core.ocr import extract_receipt_with_pixel_ocr, get_ocr_readiness


def _inject_png_text_chunk(png_bytes: bytes, keyword: str, text: str) -> bytes:
    """Insert a PNG tEXt chunk immediately before IEND."""
    iend_pos = png_bytes.rfind(b"IEND") - 4
    assert iend_pos > 8
    payload = keyword.encode("latin-1") + b"\x00" + text.encode("utf-8")
    crc = zlib.crc32(b"tEXt" + payload) & 0xFFFFFFFF
    chunk = struct.pack(">I", len(payload)) + b"tEXt" + payload + struct.pack(">I", crc)
    return png_bytes[:iend_pos] + chunk + png_bytes[iend_pos:]


def _render_receipt_png_pixels(lines: list[str]) -> bytes:
    """Render clean high-contrast receipt text onto real PNG pixels with ZERO tEXt metadata."""
    img = Image.new("RGB", (900, 320), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=32)
    except TypeError:
        font = ImageFont.load_default()
    y = 30
    for line in lines:
        draw.text((30, y), line, fill=(0, 0, 0), font=font)
        y += 60
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_png_metadata_is_never_treated_as_pixel_ocr() -> None:
    """A blank PNG carrying only PNG tEXt metadata MUST NEVER claim PIXEL_OCR extraction."""
    blank = Image.new("RGB", (300, 120), color=(255, 255, 255))
    buf = io.BytesIO()
    blank.save(buf, format="PNG")
    meta_only_png = _inject_png_text_chunk(
        buf.getvalue(),
        "Description",
        "Ref: MOCK-TXN-99999 ORD-META-01 250,000 VND",
    )

    res = extract_receipt_with_pixel_ocr(meta_only_png, "image/png")

    assert res["provenance"] == "NONE"
    assert res["extraction_status"] in ("UNREADABLE_IMAGE", "OCR_ENGINE_UNAVAILABLE")
    assert res["raw_extracted_text"] == ""
    assert res["extracted_txn_ref"] is None
    assert res["extracted_amount_vnd"] is None
    # Informational metadata is strictly segregated under image_metadata
    assert res["image_metadata"]["extracted_txn_ref"] == "MOCK-TXN-99999"
    assert res["image_metadata"]["extracted_amount_vnd"] == 250000


def test_real_pixel_ocr_on_rendered_receipt_with_zero_metadata() -> None:
    """Prove real pixel-based Tesseract OCR extracts receipt fields from pure rendered pixels.

    When tesseract is installed (CI / Docker), asserts genuine PIXEL_OCR extraction and
    metadata-vs-pixel contradiction detection. When absent on local host, asserts honest
    OCR_ENGINE_UNAVAILABLE without fabricating fields.
    """
    readiness = get_ocr_readiness()
    pure_pixel_png = _render_receipt_png_pixels(
        [
            "FINSHOP PAYMENT RECEIPT",
            "Ref: TXN987654321",
            "Order: ORD-PIXEL-01",
            "Amount: 250,000 VND",
        ]
    )
    assert b"tEXt" not in pure_pixel_png
    assert b"iTXt" not in pure_pixel_png

    res = extract_receipt_with_pixel_ocr(pure_pixel_png, "image/png")
    if not readiness["ocr_ready"]:
        assert res["ocr_ready"] is False
        assert res["extraction_status"] == "OCR_ENGINE_UNAVAILABLE"
        assert res["provenance"] == "NONE"
        assert res["raw_extracted_text"] == ""
        return

    assert res["ocr_ready"] is True
    assert res["extraction_status"] == "PIXEL_OCR_EXTRACTED"
    assert res["extraction_method"] == "TESSERACT_PIXEL_OCR"
    assert res["provenance"] == "PIXEL_OCR"
    assert res["extracted_txn_ref"] == "TXN987654321"
    assert res["extracted_order_id"] == "ORD-PIXEL-01"
    assert res["extracted_amount_vnd"] == 250000
    assert res["metadata_pixel_contradiction"] is False

    # Now inject contradicting PNG metadata into the same pixel image -> MUST flag contradiction!
    tampered_meta_png = _inject_png_text_chunk(
        pure_pixel_png,
        "Comment",
        "Ref: FAKE-META-000 999,000 VND",
    )
    res_tampered = extract_receipt_with_pixel_ocr(tampered_meta_png, "image/png")
    assert res_tampered["provenance"] == "PIXEL_OCR"
    assert res_tampered["extracted_txn_ref"] == "TXN987654321"
    assert res_tampered["metadata_pixel_contradiction"] is True
