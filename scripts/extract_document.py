"""Extract text from a PDF or HTML document on disk.

Text-layer first, OCR as a quality-gated fallback (see
``uncorrupt.extraction`` for the full design). No network calls — this
takes a local file path and prints the extracted text plus its status.

Usage:
    uv run python scripts/extract_document.py path/to/document.pdf
    uv run python scripts/extract_document.py path/to/register.html --quiet
"""

from __future__ import annotations

import argparse
import sys

from uncorrupt.extraction import extract_document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Path to a .pdf, .html or .htm file.")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only the extracted text, not the status/quality summary.",
    )
    args = parser.parse_args()

    result = extract_document(args.path)

    if not args.quiet:
        print(
            f"status={result.status.value} method={result.method} "
            f"pages={result.page_count} chars={result.char_count} "
            f"reliable={result.is_reliable}",
            file=sys.stderr,
        )
        if result.error:
            print(f"error: {result.error}", file=sys.stderr)

    print(result.text)

    if not result.is_reliable:
        sys.exit(1)


if __name__ == "__main__":
    main()
