from __future__ import annotations

import base64
from io import BytesIO
import unittest

from PIL import Image, ImageDraw
from pypdf import PdfWriter

from forma_core.workspaces.context.pdf import (
    MAX_PDF_VISUAL_BYTES,
    PdfContextError,
    PdfPageSignal,
    extract_pdf_text_from_data_url,
    ingest_pdf_data_url,
    select_visual_page_numbers,
)


def _blank_pdf_data_url(page_count: int = 1) -> str:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:application/pdf;base64,{encoded}"


def _image_pdf_data_url() -> str:
    image = Image.new("RGB", (640, 480), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 80, 560, 400), outline="black", width=8)
    draw.line((120, 240, 520, 240), fill="black", width=6)
    output = BytesIO()
    image.save(output, format="PDF")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:application/pdf;base64,{encoded}"


class PdfContextTests(unittest.TestCase):
    def test_visual_page_selection_prioritizes_scans_images_and_drawings(self) -> None:
        signals = [
            PdfPageSignal(page_number=1, text="A" * 1800, image_count=1, vector_score=2),
            PdfPageSignal(page_number=2, text="", image_count=1, vector_score=0),
            PdfPageSignal(page_number=3, text="Pinout " * 50, image_count=3, vector_score=8),
            PdfPageSignal(page_number=4, text="Mechanical drawing " * 40, image_count=0, vector_score=45),
            PdfPageSignal(page_number=5, text="Plain text " * 200, image_count=0, vector_score=0),
        ]

        selected = select_visual_page_numbers(signals, max_pages=3)

        self.assertEqual((2, 3, 4), selected)

    def test_visual_page_selection_caps_output_and_preserves_document_order(self) -> None:
        signals = [
            PdfPageSignal(page_number=index, text="", image_count=1, vector_score=index)
            for index in range(1, 9)
        ]

        selected = select_visual_page_numbers(signals, max_pages=3)

        self.assertEqual(3, len(selected))
        self.assertEqual(tuple(sorted(selected)), selected)

    def test_image_only_pdf_is_rendered_for_multimodal_context(self) -> None:
        result = ingest_pdf_data_url(_image_pdf_data_url())

        self.assertIsNone(result.text)
        self.assertEqual((1,), result.visual_page_numbers)
        self.assertEqual(1, result.page_count)
        self.assertTrue(result.visual_data_url)
        self.assertTrue(result.visual_data_url.startswith("data:image/jpeg;base64,"))
        encoded = result.visual_data_url.split(",", 1)[1]
        self.assertLessEqual(len(base64.b64decode(encoded)), MAX_PDF_VISUAL_BYTES)

    def test_text_only_helper_still_rejects_image_only_pdf(self) -> None:
        with self.assertRaisesRegex(PdfContextError, "no extractable text"):
            extract_pdf_text_from_data_url(_image_pdf_data_url())

    def test_blank_pdf_does_not_create_fake_visual_context(self) -> None:
        with self.assertRaisesRegex(PdfContextError, "no extractable text or renderable visual pages"):
            ingest_pdf_data_url(_blank_pdf_data_url())


if __name__ == "__main__":
    unittest.main()
