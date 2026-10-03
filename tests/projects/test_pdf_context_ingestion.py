from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

from forma_core.workspaces.context.pdf import PdfContextError, extract_pdf_text_from_data_url


def _pdf_data_url(payload: bytes = b"%PDF-fake") -> str:
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:application/pdf;base64,{encoded}"


class _Page:
    def __init__(self, text: str) -> None:
        self.text = text

    def extract_text(self) -> str:
        return self.text


class _Reader:
    is_encrypted = False

    def __init__(self, pages: list[_Page]) -> None:
        self.pages = pages


class PdfContextIngestionTests(unittest.TestCase):
    def test_extracts_page_text_with_page_provenance_markers(self) -> None:
        reader = _Reader([
            _Page("Motor voltage: 12 V.\nUse an M3 mount."),
            _Page("Maximum enclosure width: 80 mm."),
        ])
        with patch("forma_core.workspaces.context.pdf.PdfReader", return_value=reader):
            text = extract_pdf_text_from_data_url(_pdf_data_url())

        self.assertIn("[PDF page 1]", text)
        self.assertIn("Motor voltage: 12 V.", text)
        self.assertIn("[PDF page 2]", text)
        self.assertIn("Maximum enclosure width: 80 mm.", text)

    def test_rejects_image_only_pdf_without_extractable_text(self) -> None:
        with patch(
            "forma_core.workspaces.context.pdf.PdfReader",
            return_value=_Reader([_Page(""), _Page("   ")]),
        ):
            with self.assertRaisesRegex(PdfContextError, "no extractable text"):
                extract_pdf_text_from_data_url(_pdf_data_url())

    def test_rejects_pdf_larger_than_configured_limit(self) -> None:
        with self.assertRaisesRegex(PdfContextError, "limited to"):
            extract_pdf_text_from_data_url(_pdf_data_url(b"%PDF" + b"x" * 64), max_bytes=16)

    def test_bounds_extracted_text(self) -> None:
        with patch(
            "forma_core.workspaces.context.pdf.PdfReader",
            return_value=_Reader([_Page("A" * 200)]),
        ):
            text = extract_pdf_text_from_data_url(_pdf_data_url(), max_chars=40)

        self.assertIn("[PDF text truncated to 40 characters.]", text)
        self.assertLess(len(text), 120)


if __name__ == "__main__":
    unittest.main()
