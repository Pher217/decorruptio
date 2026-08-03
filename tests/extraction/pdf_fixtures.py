"""Synthetic PDF byte builders for extraction tests.

Built with pypdf's own writer/generic objects (already a project dependency
for this layer) rather than a second PDF-authoring dependency or a
hand-rolled xref table.
"""

from __future__ import annotations

from io import BytesIO

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def build_text_pdf(pages: list[str]) -> bytes:
    """A valid PDF whose pages carry a real embedded text layer."""
    writer = PdfWriter()

    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    font_ref = writer._add_object(font)

    for page_text in pages:
        page = writer.add_blank_page(width=612, height=792)
        if "/Resources" not in page:
            page[NameObject("/Resources")] = DictionaryObject()
        fonts_dict = DictionaryObject()
        fonts_dict[NameObject("/F1")] = font_ref
        page["/Resources"][NameObject("/Font")] = fonts_dict

        content = f"BT /F1 12 Tf 72 700 Td ({page_text}) Tj ET".encode("latin-1")
        stream_obj = DecodedStreamObject()
        stream_obj.set_data(content)
        page.replace_contents(stream_obj)

    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def build_blank_pdf(page_count: int) -> bytes:
    """A valid PDF whose pages carry no content stream at all — the text
    layer a scanned/image-only PDF produces (structurally sound, zero
    embedded text)."""
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=612, height=792)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()
