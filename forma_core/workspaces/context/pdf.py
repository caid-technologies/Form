from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
from io import BytesIO
import math
import re
from typing import Any

from PIL import Image, ImageDraw
from pypdf import PdfReader
import pypdfium2 as pdfium


MAX_PDF_BYTES = 2 * 1024 * 1024
MAX_PDF_PAGES = 40
MAX_PDF_TEXT_CHARS = 20_000
MAX_PDF_VISUAL_PAGES = 6
MAX_PDF_VISUAL_BYTES = 1_500_000
PDF_VISUAL_PAGE_WIDTH = 680
_PDF_DATA_URL_PREFIX = "data:application/pdf;base64,"
_VECTOR_OPERATOR_RE = re.compile(
    rb"(?<![A-Za-z])(?:m|l|c|v|y|h|re|S|s|f|F|f\*|B|B\*|b|b\*|n)(?![A-Za-z])"
)


class PdfContextError(ValueError):
    """Raised when an uploaded PDF cannot be safely ingested as project context."""


@dataclass(frozen=True)
class PdfPageSignal:
    page_number: int
    text: str
    image_count: int
    vector_score: int


@dataclass(frozen=True)
class PdfContextResult:
    text: str | None
    visual_data_url: str | None
    visual_page_numbers: tuple[int, ...]
    source_digest: str
    page_count: int
    text_truncated: bool


def _decode_pdf_data_url(data_url: str, *, max_bytes: int = MAX_PDF_BYTES) -> bytes:
    value = str(data_url or "").strip()
    if not value.lower().startswith(_PDF_DATA_URL_PREFIX):
        raise PdfContextError("PDF attachments must be uploaded as an application/pdf data URL.")

    encoded = value.split(",", 1)[1]
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PdfContextError("The uploaded PDF data is not valid base64.") from exc

    if not payload.startswith(b"%PDF"):
        raise PdfContextError("The uploaded file does not appear to be a valid PDF.")
    if len(payload) > max_bytes:
        raise PdfContextError(
            f"PDF attachments are limited to {max_bytes // (1024 * 1024)} MB for context ingestion."
        )
    return payload


def _normalize_page_text(value: str) -> str:
    text = str(value or "").replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _resolve_pdf_object(value: Any) -> Any:
    getter = getattr(value, "get_object", None)
    if callable(getter):
        try:
            return getter()
        except Exception:
            return value
    return value


def _page_image_count(page: Any) -> int:
    """Count image XObjects without decoding their pixels."""

    try:
        resources = _resolve_pdf_object(page.get("/Resources"))
        if not isinstance(resources, dict):
            return 0
        xobjects = _resolve_pdf_object(resources.get("/XObject"))
        if not isinstance(xobjects, dict):
            return 0
    except Exception:
        return 0

    count = 0
    for value in xobjects.values():
        try:
            obj = _resolve_pdf_object(value)
            if isinstance(obj, dict) and str(obj.get("/Subtype") or "") == "/Image":
                count += 1
        except Exception:
            continue
    return count


def _page_vector_score(page: Any) -> int:
    """Estimate vector-diagram density from bounded content-stream operators."""

    try:
        contents = page.get_contents()
        if contents is None:
            return 0
        data = contents.get_data()
    except Exception:
        return 0
    if not isinstance(data, (bytes, bytearray)):
        return 0
    sample = bytes(data[:512_000])
    return min(500, len(_VECTOR_OPERATOR_RE.findall(sample)))


def _visual_priority(signal: PdfPageSignal) -> int:
    text_length = len(signal.text)
    has_graphics = signal.image_count > 0 or signal.vector_score >= 8
    likely_visual = (
        (text_length < 120 and has_graphics)
        or signal.image_count >= 2
        or signal.vector_score >= 20
        or (signal.image_count >= 1 and text_length < 1200)
    )
    if not likely_visual:
        return 0

    score = 0
    if text_length < 120:
        score += 1000
    elif text_length < 500:
        score += 180
    elif text_length < 1200:
        score += 60
    score += min(signal.image_count, 6) * 240
    score += min(signal.vector_score, 160) * 3
    return score


def select_visual_page_numbers(
    signals: list[PdfPageSignal],
    *,
    max_pages: int = MAX_PDF_VISUAL_PAGES,
) -> tuple[int, ...]:
    if max_pages <= 0:
        return ()
    ranked = [
        (_visual_priority(signal), signal.page_number)
        for signal in signals
        if _visual_priority(signal) > 0
    ]
    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected = sorted(page_number for _, page_number in ranked[:max_pages])
    return tuple(selected)


def _open_reader(payload: bytes) -> PdfReader:
    try:
        reader = PdfReader(BytesIO(payload), strict=False)
    except Exception as exc:
        raise PdfContextError("Forma could not read that PDF. Try exporting it again.") from exc

    if reader.is_encrypted:
        try:
            unlocked = reader.decrypt("")
        except Exception as exc:
            raise PdfContextError("Password-protected PDFs are not supported yet.") from exc
        if not unlocked:
            raise PdfContextError("Password-protected PDFs are not supported yet.")
    return reader


def _analyze_pdf(
    payload: bytes,
    *,
    max_pages: int,
    max_chars: int,
) -> tuple[str | None, list[PdfPageSignal], bool]:
    reader = _open_reader(payload)
    page_count = len(reader.pages)
    if page_count > max_pages:
        raise PdfContextError(
            f"PDF attachments are limited to {max_pages} pages for context ingestion."
        )

    chunks: list[str] = []
    signals: list[PdfPageSignal] = []
    remaining = max_chars
    truncated = False

    for index, page in enumerate(reader.pages, start=1):
        try:
            page_text = _normalize_page_text(page.extract_text() or "")
        except Exception:
            page_text = ""

        signals.append(
            PdfPageSignal(
                page_number=index,
                text=page_text,
                image_count=_page_image_count(page),
                vector_score=_page_vector_score(page),
            )
        )

        if not page_text or remaining <= 0:
            if page_text and remaining <= 0:
                truncated = True
            continue

        chunk = f"[PDF page {index}]\n{page_text}"
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining].rstrip())
            remaining = 0
            truncated = True
            continue

        chunks.append(chunk)
        remaining -= len(chunk)

    text = "\n\n".join(chunks).strip() or None
    if truncated and text:
        text = f"{text}\n\n[PDF text truncated to {max_chars} characters.]"
    return text, signals, truncated


def _render_pdf_page(document: Any, page_number: int) -> Image.Image | None:
    page = None
    bitmap = None
    try:
        page = document[page_number - 1]
        width, _ = page.get_size()
        scale = min(1.5, max(0.8, PDF_VISUAL_PAGE_WIDTH / max(float(width), 1.0)))
        bitmap = page.render(scale=scale)
        return bitmap.to_pil().convert("RGB").copy()
    except Exception:
        return None
    finally:
        for item in (bitmap, page):
            close = getattr(item, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def _contact_sheet(rendered_pages: list[tuple[int, Image.Image]]) -> Image.Image:
    columns = 1 if len(rendered_pages) == 1 else 2
    gap = 12
    label_height = 32
    cell_width = PDF_VISUAL_PAGE_WIDTH

    normalized: list[tuple[int, Image.Image]] = []
    for page_number, image in rendered_pages:
        if image.width > cell_width:
            ratio = cell_width / image.width
            image = image.resize(
                (cell_width, max(1, round(image.height * ratio))),
                Image.Resampling.LANCZOS,
            )
        normalized.append((page_number, image))

    rows = math.ceil(len(normalized) / columns)
    row_heights: list[int] = []
    for row in range(rows):
        items = normalized[row * columns : (row + 1) * columns]
        row_heights.append(max(image.height for _, image in items) + label_height)

    width = columns * cell_width + gap * (columns + 1)
    height = sum(row_heights) + gap * (rows + 1)
    sheet = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(sheet)

    y = gap
    for row in range(rows):
        items = normalized[row * columns : (row + 1) * columns]
        row_height = row_heights[row]
        for column, (page_number, image) in enumerate(items):
            x = gap + column * (cell_width + gap)
            draw.text((x + 4, y + 7), f"PDF page {page_number}", fill="black")
            sheet.paste(image, (x, y + label_height))
        y += row_height + gap
    return sheet


def _encode_visual_sheet(sheet: Image.Image, *, max_bytes: int) -> str:
    candidate = sheet
    for resize_round in range(4):
        for quality in (82, 72, 60, 48):
            output = BytesIO()
            candidate.save(output, format="JPEG", quality=quality, optimize=True)
            payload = output.getvalue()
            if len(payload) <= max_bytes:
                return f"data:image/jpeg;base64,{base64.b64encode(payload).decode('ascii')}"

        if resize_round == 3:
            break
        candidate = candidate.resize(
            (
                max(320, round(candidate.width * 0.78)),
                max(320, round(candidate.height * 0.78)),
            ),
            Image.Resampling.LANCZOS,
        )

    raise PdfContextError("PDF visual pages could not be compacted within the image context limit.")


def _render_visual_contact_sheet(
    payload: bytes,
    page_numbers: tuple[int, ...],
    *,
    max_bytes: int,
) -> tuple[str | None, tuple[int, ...]]:
    if not page_numbers:
        return None, ()

    try:
        document = pdfium.PdfDocument(payload)
    except Exception:
        return None, ()

    rendered: list[tuple[int, Image.Image]] = []
    try:
        for page_number in page_numbers:
            image = _render_pdf_page(document, page_number)
            if image is not None:
                rendered.append((page_number, image))
    finally:
        close = getattr(document, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    if not rendered:
        return None, ()

    sheet = _contact_sheet(rendered)
    try:
        data_url = _encode_visual_sheet(sheet, max_bytes=max_bytes)
    finally:
        sheet.close()
        for _, image in rendered:
            image.close()
    return data_url, tuple(page_number for page_number, _ in rendered)


def ingest_pdf_data_url(
    data_url: str,
    *,
    max_bytes: int = MAX_PDF_BYTES,
    max_pages: int = MAX_PDF_PAGES,
    max_chars: int = MAX_PDF_TEXT_CHARS,
    max_visual_pages: int = MAX_PDF_VISUAL_PAGES,
    max_visual_bytes: int = MAX_PDF_VISUAL_BYTES,
) -> PdfContextResult:
    """Extract text plus a bounded visual contact sheet from a PDF data URL."""

    payload = _decode_pdf_data_url(data_url, max_bytes=max_bytes)
    text, signals, truncated = _analyze_pdf(
        payload,
        max_pages=max_pages,
        max_chars=max_chars,
    )

    visual_candidates = select_visual_page_numbers(signals, max_pages=max_visual_pages)
    visual_data_url, rendered_pages = _render_visual_contact_sheet(
        payload,
        visual_candidates,
        max_bytes=max_visual_bytes,
    )

    if not text and not visual_data_url:
        raise PdfContextError(
            "This PDF has no extractable text or renderable visual pages."
        )

    return PdfContextResult(
        text=text,
        visual_data_url=visual_data_url,
        visual_page_numbers=rendered_pages,
        source_digest=hashlib.sha256(payload).hexdigest()[:20],
        page_count=len(signals),
        text_truncated=truncated,
    )


def extract_pdf_text_from_data_url(
    data_url: str,
    *,
    max_bytes: int = MAX_PDF_BYTES,
    max_pages: int = MAX_PDF_PAGES,
    max_chars: int = MAX_PDF_TEXT_CHARS,
) -> str:
    """Backward-compatible text-only extraction helper."""

    payload = _decode_pdf_data_url(data_url, max_bytes=max_bytes)
    text, _, _ = _analyze_pdf(payload, max_pages=max_pages, max_chars=max_chars)
    if not text:
        raise PdfContextError(
            "This PDF has no extractable text. Use multimodal PDF ingestion for scanned or image-only pages."
        )
    return text
