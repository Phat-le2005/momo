"""Real Pixel-Based Receipt OCR & Safe Image Decoding (Prompt 14.3 Fix 3).

Strict Provenance Invariants:
- Separates `PIXEL_OCR` (visible pixels recognized by Tesseract OCR), `IMAGE_METADATA`
  (PNG tEXt/iTXt chunks, strictly informational), and `CUSTOMER_ENTERED` fields.
- NEVER treats PNG tEXt/iTXt metadata as visible pixel OCR!
- Enforces Pillow decompression-bomb limits (`MAX_IMAGE_PIXELS`), EXIF orientation normalization,
  and metadata stripping before running Tesseract with a bounded timeout.
- Reports honest OCR engine readiness (`ocr_ready=False` if `tesseract` binary is absent).
"""

from __future__ import annotations

import io
import os
import re
import shutil
import struct
import tempfile
import zlib
from pathlib import Path
from typing import Any

MAX_RECEIPT_BYTES = 5 * 1024 * 1024
MAX_RECEIPT_DIM = 8000
MAX_RECEIPT_PIXELS = 25_000_000
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def get_receipt_storage_root() -> Path:
    """Return the resolved absolute path for private receipt storage (never relative)."""
    from app.core.config import get_settings

    configured = get_settings().receipt_storage_dir.strip()
    if configured:
        root = Path(configured).expanduser().resolve()
    else:
        root = (Path(tempfile.gettempdir()) / "finshop_private_receipts").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_private_receipt_dir(merchant_id: str) -> Path:
    """Resolve a merchant's private receipt directory with strict path-traversal protection."""
    clean_mid = merchant_id.strip()
    if not _SAFE_ID_RE.match(clean_mid):
        raise ValueError(f"INVALID_MERCHANT_PATH_SEGMENT: '{merchant_id}'")
    root = get_receipt_storage_root()
    target = (root / clean_mid).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError("PATH_TRAVERSAL_BLOCKED: receipt path escapes storage root") from exc
    target.mkdir(parents=True, exist_ok=True)
    return target


def verify_receipt_storage_writable() -> dict[str, Any]:
    """Verify the configured private receipt storage directory is absolute and writable."""
    try:
        root = get_receipt_storage_root()
        probe = root / f".write_probe_{os.getpid()}"
        probe.write_bytes(b"ok")
        probe.unlink(missing_ok=True)
        return {"writable": True, "storage_dir": str(root)}
    except Exception as exc:  # noqa: BLE001
        return {"writable": False, "storage_dir": "", "error": f"{type(exc).__name__}: {exc}"}


def parse_receipt_fields(raw_text: str) -> dict[str, Any]:
    """Parse structured receipt fields from text (used for both pixel OCR and metadata)."""
    text = (raw_text or "").strip()
    if not text:
        return {
            "raw_text": "",
            "extracted_txn_ref": None,
            "extracted_order_id": None,
            "extracted_amount_vnd": None,
            "extracted_currency": None,
        }
    ref_m = re.search(
        r"(?:ref|txn|mã giao dịch|ma giao dich|biên lai|bien lai)[:\s#]*([A-Z0-9_-]{5,40})",
        text,
        re.I,
    )
    ord_m = re.search(r"(ORD-[A-Z0-9_-]{4,32})", text, re.I)
    amt_m = re.search(r"(\d[\d.,]{3,12})\s*(?:vnd|đ|d\b)", text, re.I)
    extracted_amt: int | None = None
    if amt_m:
        digits = re.sub(r"[^\d]", "", amt_m.group(1))
        if digits.isdigit():
            extracted_amt = int(digits)
    return {
        "raw_text": text[:2000],
        "extracted_txn_ref": ref_m.group(1) if ref_m else None,
        "extracted_order_id": ord_m.group(1) if ord_m else None,
        "extracted_amount_vnd": extracted_amt,
        "extracted_currency": "VND" if extracted_amt is not None else None,
    }


def get_ocr_readiness() -> dict[str, Any]:
    """Honest runtime check for real pixel-based Tesseract OCR availability."""
    binary = shutil.which("tesseract")
    if not binary:
        return {
            "ocr_ready": False,
            "engine": "tesseract",
            "tesseract_version": None,
            "languages": [],
            "reason": "TESSERACT_BINARY_NOT_INSTALLED",
        }
    try:
        import PIL.Image  # noqa: F401
        import pytesseract  # type: ignore[import-untyped]

        ver = str(pytesseract.get_tesseract_version())
        langs = list(pytesseract.get_languages(config=""))
        has_eng = "eng" in langs
        return {
            "ocr_ready": has_eng,
            "engine": "tesseract",
            "tesseract_version": ver,
            "languages": sorted(langs),
            "reason": "READY" if has_eng else "MISSING_ENG_LANG_DATA",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ocr_ready": False,
            "engine": "tesseract",
            "tesseract_version": None,
            "languages": [],
            "reason": f"OCR_INIT_ERROR: {type(exc).__name__}",
        }


def _extract_png_metadata_only(raw: bytes) -> str:
    """Safely extract PNG tEXt/iTXt chunks as informational metadata only (never pixel OCR)."""
    pos = 8
    n = len(raw)
    idat_chunks: list[bytes] = []
    meta_texts: list[str] = []
    while pos + 12 <= n:
        length = struct.unpack(">I", raw[pos : pos + 4])[0]
        ctype = raw[pos + 4 : pos + 8]
        data_start = pos + 8
        data_end = data_start + length
        if data_end + 4 > n:
            raise ValueError("MALFORMED_PNG_TRUNCATED_CHUNK")
        cdata = raw[data_start:data_end]
        if ctype == b"IDAT":
            idat_chunks.append(cdata)
        elif ctype == b"tEXt":
            parts = cdata.split(b"\x00", 1)
            if len(parts) == 2:
                meta_texts.append(parts[1].decode("utf-8", errors="ignore").strip())
        elif ctype == b"iTXt":
            parts = cdata.split(b"\x00", 5)
            if len(parts) >= 6:
                meta_texts.append(parts[-1].decode("utf-8", errors="ignore").strip())
        elif ctype == b"IEND":
            break
        pos = data_end + 4

    if idat_chunks:
        decomp = zlib.decompressobj()
        out = decomp.decompress(b"".join(idat_chunks), MAX_RECEIPT_PIXELS * 4)
        if decomp.unconsumed_tail:
            raise ValueError("DECOMPRESSION_BOMB_BLOCKED")
        _ = len(out)
    return "\n".join(t for t in meta_texts if t).strip()


def extract_receipt_with_pixel_ocr(raw: bytes, content_type: str) -> dict[str, Any]:
    """Decode image safely and run real pixel-based Tesseract OCR.

    NEVER treats PNG tEXt/iTXt metadata as visible pixel OCR!
    """
    norm = content_type.strip().lower().split(";")[0]
    meta_text = ""
    if norm == "image/png":
        meta_text = _extract_png_metadata_only(raw)
    meta_fields = parse_receipt_fields(meta_text)

    readiness = get_ocr_readiness()
    pixel_text = ""
    extraction_status = "OCR_ENGINE_UNAVAILABLE"
    extraction_method = "NONE"

    # Always decode pixels via Pillow when installed to validate image integrity
    try:
        from PIL import Image, ImageOps

        Image.MAX_IMAGE_PIXELS = MAX_RECEIPT_PIXELS
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            w, h = img.size
            if w <= 0 or h <= 0 or w > MAX_RECEIPT_DIM or h > MAX_RECEIPT_DIM:
                raise ValueError("DECOMPRESSION_BOMB_BLOCKED")
            # Normalize orientation and strip metadata into a fresh grayscale buffer
            clean_img = ImageOps.exif_transpose(img).convert("L")
            if readiness["ocr_ready"]:
                import pytesseract

                langs = readiness.get("languages") or []
                lang_arg = "vie+eng" if ("vie" in langs and "eng" in langs) else "eng"
                pixel_text = str(
                    pytesseract.image_to_string(clean_img, lang=lang_arg, timeout=5) or ""
                ).strip()
                extraction_method = "TESSERACT_PIXEL_OCR"
                extraction_status = "PIXEL_OCR_EXTRACTED" if pixel_text else "UNREADABLE_IMAGE"
    except ValueError:
        raise
    except ImportError:
        extraction_status = "OCR_ENGINE_UNAVAILABLE"
        extraction_method = "NONE"
    except Exception:  # noqa: BLE001
        if readiness["ocr_ready"]:
            extraction_status = "UNREADABLE_IMAGE"
            extraction_method = "TESSERACT_PIXEL_OCR"
        else:
            extraction_status = "OCR_ENGINE_UNAVAILABLE"
            extraction_method = "NONE"

    pixel_fields = parse_receipt_fields(pixel_text)

    # Detect contradiction between visible pixel OCR and hidden image metadata
    meta_pixel_contradiction = False
    if pixel_fields["extracted_txn_ref"] and meta_fields["extracted_txn_ref"]:
        if (
            str(pixel_fields["extracted_txn_ref"]).upper()
            != str(meta_fields["extracted_txn_ref"]).upper()
        ):
            meta_pixel_contradiction = True
    if (
        pixel_fields["extracted_amount_vnd"] is not None
        and meta_fields["extracted_amount_vnd"] is not None
        and int(pixel_fields["extracted_amount_vnd"]) != int(meta_fields["extracted_amount_vnd"])
    ):
        meta_pixel_contradiction = True

    return {
        "extraction_status": extraction_status,
        "extraction_method": extraction_method,
        "provenance": "PIXEL_OCR" if extraction_status == "PIXEL_OCR_EXTRACTED" else "NONE",
        "ocr_ready": bool(readiness["ocr_ready"]),
        "pixel_ocr": pixel_fields,
        "image_metadata": meta_fields,
        "metadata_pixel_contradiction": meta_pixel_contradiction,
        # Top-level extracted fields come STRICTLY from PIXEL_OCR (never from PNG metadata!)
        "raw_extracted_text": pixel_fields["raw_text"],
        "extracted_txn_ref": pixel_fields["extracted_txn_ref"],
        "extracted_order_id": pixel_fields["extracted_order_id"],
        "extracted_amount_vnd": pixel_fields["extracted_amount_vnd"],
        "extracted_currency": pixel_fields["extracted_currency"],
    }
