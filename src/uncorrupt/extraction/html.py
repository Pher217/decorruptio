"""HTML text extraction.

Reuses the BeautifulSoup ``get_text()`` approach already used by
``uncorrupt.graph.lords_interests`` for register pages, rather than
inventing a second HTML-parsing path.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from uncorrupt.extraction.types import ExtractionResult, ExtractionStatus


def extract_html(html: str | bytes) -> ExtractionResult:
    """Extract visible text from an HTML document.

    HTML has no "scanned" failure mode the way a PDF does — BeautifulSoup
    either parses the markup or the input itself is invalid (not a
    str/bytes/file-like object). A document that parses but has no text
    content (e.g. an empty ``<body>``) is a legitimate zero-length result —
    status TEXT_LAYER with ``char_count`` 0 — never conflated with FAILED,
    which is reserved for input that could not be parsed at all.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        return ExtractionResult(
            status=ExtractionStatus.FAILED,
            text="",
            page_count=None,
            char_count=0,
            source_format="html",
            method=None,
            quality=None,
            error=f"could not parse HTML: {exc}",
        )

    text = soup.get_text(separator=" ", strip=True)
    return ExtractionResult(
        status=ExtractionStatus.TEXT_LAYER,
        text=text,
        page_count=1,
        char_count=len(text),
        source_format="html",
        method="beautifulsoup",
        quality=None,
        error=None,
    )
