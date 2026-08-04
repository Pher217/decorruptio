"""Deterministic citation verifier — checks a claimed quote against the real document.

Motivation: an overnight benchmark-sourcing pass (11 research agents, 34
gold-manifest rows) was adversarially checked by hand and found 9/34 rows
(26%) defective, including two quotes presented in quotation marks that do
not appear in the cited article at all, and a registry ID that 404s. Every
one of those was caught only by re-fetching the primary document -- never by
re-reading a search summary. That verification does not scale to a new gold
manifest per country, so this module makes it a deterministic, repeatable
check instead of a one-off manual pass.

Anti-pattern this deliberately avoids (found in a sibling project): a
research agent whose only anti-hallucination control is a prompt line
("no hallucinated quotes") -- self-attestation by the same model that would
fabricate, with no ground truth involved, and a content tool that only
re-serves already-scraped text rather than re-fetching. `verify_citation`
instead re-fetches the URL itself and tests the claim against that fetched
text -- the same discipline as `officelabs-agent`'s `VerifierMiddleware`
(checking real document state, not the generator's self-report) -- and the
core classification decision requires no LLM at all.

Five outcomes, deliberately not collapsed into pass/fail:

  * VERBATIM      -- the quote appears in the fetched text after normalising
                      whitespace, quote-mark style (curly vs straight) and
                      dash style. This is the "no defect" case.
  * NEAR          -- not verbatim, but a high-similarity source sentence (or
                      short run of sentences) exists. Returned together with
                      that best-matching text so a human can judge paraphrase
                      vs fabrication.
  * ABSENT        -- the document fetched fine, real substantive text was
                      extracted, and no sentence in it resembles the claimed
                      quote. This is the defect the whole tool exists to
                      catch (the "quote appears in quotation marks but isn't
                      in the article" failure mode from the overnight run).
  * EXTRACTION_UNRELIABLE -- the fetch succeeded (HTTP 200, no challenge
                      page) but the extracted content is not plausibly the
                      document's substance: a JavaScript-rendered results
                      page whose real content never appears in the static
                      HTML, a scanned/image-only PDF, or a PDF whose text
                      layer decoded for some pages but not others (see
                      `uncorrupt.extraction.pdf.extract_pdf_bytes`, which
                      does the actual PDF quality-vs-OCR decision for this
                      module -- see "PDF extraction" below). Also covers a
                      fuzzy-match scan that was truncated by
                      `_MAX_CHARS_CONSIDERED` before it could cover the
                      whole document -- a scan that stopped early cannot
                      prove a quote's absence either. Same principle as
                      ADR-008's document case ("an image-only PDF or failed
                      text layer must become EXTRACTION_UNRELIABLE, not
                      QUOTE_ABSENT") applied to every source type this tool
                      fetches -- a fetch succeeding is not the same as the
                      document's real content having been read. Detected on
                      measurable signals (see `_extraction_is_unreliable`
                      and the `scan_truncated` check in `verify_citation`),
                      never a guess, and only ever checked in place of what
                      would otherwise be ABSENT -- it can never swallow a
                      VERBATIM or NEAR result, and a genuinely absent quote
                      in a properly-extracted, fully-scanned document still
                      reports ABSENT.
  * UNFETCHABLE   -- the document could not be retrieved as real content:
                      blocked domain, HTTP error, network failure, a PDF that
                      could not be parsed or whose text layer failed the
                      quality gate with no OCR backend available, or a
                      Cloudflare/JS challenge page served with a 200. This is
                      explicitly NOT the same as ABSENT -- a quote is not
                      "missing" just because the fetcher was blocked, and
                      conflating the two would erase the exact distinction
                      this tool exists to preserve.

PDF extraction: delegated entirely to `uncorrupt.extraction.pdf.extract_pdf_bytes`
(pypdf text layer, OCR quality-gated fallback -- see that module and
`uncorrupt.extraction.quality`) rather than a second, bespoke PDF-reliability
heuristic maintained in parallel here. An earlier version of this module
shelled out to `pdftotext` and judged partial extraction from raw per-page
form-feed counts; that duplicated (and, on inspection, disagreed with) the
extraction layer's own measured chars-per-page/alnum-ratio/word-shape-ratio
gate, so it was removed in favour of the one canonical decision.

Known limitations, not fixed by this module:

  * Two-column PDF layouts. `pypdf`'s reading order can interleave the two
    columns' text on one logical line, so a quote's characters are present
    and in the right order but padded by the neighbouring column's text --
    this can still read as ABSENT even though the quote is genuinely in the
    document. No column-detection or layout-aware extraction is attempted
    here; a false ABSENT from a two-column source should be spot-checked by
    hand before being treated as a confirmed defect.
  * Fabricated values inside an otherwise-real table row. The sliding-window
    fallback that finds a near-identical row for a genuine table quote (see
    `_sliding_char_windows`) will also find a high-similarity match for a
    quote that reproduces a real row's *shape* with a fabricated *value* --
    e.g. a real company/date/amount row with one figure changed reads as
    NEAR, not ABSENT, because the surrounding characters still match. Fuzzy
    matching on tabular data cannot distinguish "this exact row exists" from
    "a row shaped like this exists" -- a human must still read `best_match`
    against `claimed_quote` for table-sourced NEAR results, the same as any
    other NEAR result.

Keep EXTRACTION_UNRELIABLE and UNFETCHABLE straight: UNFETCHABLE means the
request itself did not succeed (or was never attempted, e.g. a known-blocked
domain, or was served a bot-challenge page instead of the document).
EXTRACTION_UNRELIABLE means the request succeeded and no challenge page was
served, but what came back does not plausibly contain the document's real
content (a JS app shell, an image-only PDF). Both are conservative escape
hatches from ABSENT: when a signal is ambiguous, this module always prefers
the reading that does not impugn a real person's sourcing -- "when in doubt
between ABSENT and EXTRACTION_UNRELIABLE, choose EXTRACTION_UNRELIABLE" (a
false EXTRACTION_UNRELIABLE costs a human a look; a false ABSENT looks like
proof a citation is fabricated when it might not be).

An optional, off-by-default LLM adjudication step (`adjudicate_near_with_llm`)
may relabel a NEAR match's *interpretation* (paraphrase vs likely fabrication)
for a human reviewer -- see `verify_citation`'s docstring for the guarantee
that it can only ever fire on a NEAR result and can never turn ABSENT (or
EXTRACTION_UNRELIABLE, or UNFETCHABLE) into anything else. ADR-004 permits
LLMs in research/extraction; this measurement path is not that -- the
classification itself is always decided by the deterministic checks above.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from uncorrupt.extraction.pdf import extract_pdf_bytes
from uncorrupt.extraction.types import ExtractionStatus

logger = logging.getLogger(__name__)

# Cache entries older than this are refetched rather than trusted forever --
# same discipline as `uncorrupt.graph.ch_officers.DEFAULT_MAX_CACHE_AGE_DAYS`.
DEFAULT_MAX_CACHE_AGE_DAYS = 30

# Below this SequenceMatcher ratio a candidate sentence is not "near" the
# claimed quote -- it is reported ABSENT. Chosen so a same-facts paraphrase
# (reworded clause, different tense/connective) clears the bar while
# unrelated sentences from the same article do not.
DEFAULT_NEAR_THRESHOLD = 0.55

# Domains observed to be unreachable from this environment (hard network
# block or bot-detection that never returns real content to a plain
# fetcher) -- short-circuited to UNFETCHABLE instead of spending retries on
# a call that cannot succeed. This is a fast-path only: any other domain
# that turns out to serve a JS/Cloudflare challenge is still caught by the
# content-based `_looks_like_challenge_page` check below, e.g.
# `parliament.uk`, which serves a 200 carrying challenge HTML rather than
# blocking the connection outright.
KNOWN_BLOCKED_DOMAINS: frozenset[str] = frozenset(
    {
        "theguardian.com",
        "www.theguardian.com",
        "ft.com",
        "www.ft.com",
        "web.archive.org",
    }
)

USER_AGENT = "Mozilla/5.0 (compatible; decorruptio-citation-verifier/1.0)"

_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "checking if the site connection is secure",
    "cf-chl",
    "cf_chl",
    "__cf_chl",
    "enable javascript and cookies to continue",
    "attention required! | cloudflare",
    "ddos protection by",
    "cf-browser-verification",
    "verifying you are human",
    "please wait while we verify",
)

# -- EXTRACTION_UNRELIABLE thresholds (see module docstring / ADR-008) ------
#
# Below this many normalised characters, extracted "content" is too short to
# plausibly be a real article/letter/report body -- a scanned/image-only PDF
# with no text layer, or a page that is almost entirely chrome. This is the
# universal backstop signal: it applies even when no raw markup is available
# (e.g. a caller passing `fetched_text` directly, or a PDF, which has no
# "markup" in the HTML sense). Deliberately low -- the real JS-shell case
# that motivated this status (search.electoralcommission.org.uk) still had
# ~1.3KB of nav/footer text, so it is caught by the ratio signal below, not
# this one; this backstop exists for near-empty extractions (an image-only
# PDF, a bare stub), so it must not fire on a genuinely short-but-real
# article body.
EXTRACTION_UNRELIABLE_MIN_TEXT_LENGTH = 200

# Extracted-text-characters / raw-markup-characters. A JS-rendered results
# page typically ships a large bundle of markup/script to render almost no
# server-side body text -- confirmed against the real
# search.electoralcommission.org.uk donations search (raw HTML 56KB,
# extracted+normalised text 1.3KB, ratio ~0.023) that motivated this status.
EXTRACTION_UNRELIABLE_MIN_TEXT_TO_MARKUP_RATIO = 0.05

# If the best fuzzy-match candidate scores below this AND looks like
# nav/footer boilerplate (see `_BOILERPLATE_MARKERS`), the "nearest" thing in
# the document to the claimed quote is chrome, not prose -- a second,
# independent signal that the real content was never retrieved.
EXTRACTION_UNRELIABLE_BOILERPLATE_SIMILARITY_CEILING = 0.35

# A looser text-length cap used only when a JS-app-root marker is present --
# some minimal server-rendered chrome/nav text is normal even for a genuine
# SPA shell, so this is intentionally more permissive than
# EXTRACTION_UNRELIABLE_MIN_TEXT_LENGTH on its own.
_JS_APP_ROOT_TEXT_LENGTH_CAP = 600

_JS_APP_ROOT_MARKERS = (
    'id="root"',
    "id='root'",
    'id="app"',
    "id='app'",
    "<app-root",
    "ng-app",
    "data-reactroot",
    "you need to enable javascript to run this app",
    "doesn't work properly without javascript enabled",
    "please enable javascript",
)

_BOILERPLATE_MARKERS = (
    "accessibility",
    "site map",
    "sitemap",
    "privacy notice",
    "privacy policy",
    "cookie",
    "follow us",
    "all rights reserved",
    "terms of use",
    "terms & conditions",
    "skip to main content",
    "skip to content",
    "back to top",
    "no results were returned",
)

_QUOTE_CHARS: dict[str, str] = {
    "‘": "'",
    "’": "'",
    "‚": "'",
    "‛": "'",
    "′": "'",
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
    "″": '"',
    "«": '"',
    "»": '"',
}
_DASH_CHARS: dict[str, str] = {
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",
    "—": "-",
    "―": "-",
}
_WHITESPACE_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# A space immediately before one of these marks has no valid reading in
# English prose (nobody writes "the cat 's toy" or "hello , world") -- it
# is a PDF text-extraction kerning artefact, the same family of rendering
# noise as the curly-quote/dash normalisation above. Confirmed on real
# manifest rows: pypdf inserting a spurious space before a possessive
# apostrophe ("Geidt 's") and before a comma ("point , in"). Deliberately
# narrow to marks with NO valid preceding-space reading -- quote marks and
# dashes are excluded because a space before an opening quote or a spaced
# em-dash is often intentional prose, not an artefact. This does NOT
# address the opposite artefact (pypdf sometimes drops a space between two
# words entirely, e.g. "toldme") -- that direction cannot be safely
# reversed without guessing where the missing word boundary was, so it is
# deliberately left alone; a false VERBATIM is worse than a human glancing
# at an accurate NEAR.
_SPACE_BEFORE_TERMINAL_PUNCTUATION_RE = re.compile(r" (?=['.,;:!?)\]])")

# Bound on how many characters of a document are fuzzy-matched against, so
# one huge document cannot make verification pathologically slow. A
# character bound, not a sentence-count bound: a sentence-count cap made
# ABSENT unreachable above a fixed page count regardless of how many
# characters that represented (measured: ~150-200 pages), which is exactly
# the size of Hansard volumes, inquiry reports and NAO reports with
# appendices -- this project's primary sources. Measured on real ~400-1100KB
# government documents: a full scan (sentence-window candidates plus the
# whole-text sliding window in `_best_fuzzy_match`) costs roughly 2-7s, so
# this bound is headroom for a single large real source, not a practical
# ceiling reached by ordinary documents. Exceeding it is still safe:
# `verify_citation` never returns ABSENT when the scan was truncated (see
# `truncated` on `CitationVerification`) -- it reports EXTRACTION_UNRELIABLE
# instead, because a scan that stopped early cannot prove a quote's absence.
_MAX_CHARS_CONSIDERED = 2_000_000


class CitationStatus(StrEnum):
    """The five outcomes of `verify_citation`. Never collapsed to pass/fail."""

    VERBATIM = "VERBATIM"
    NEAR = "NEAR"
    ABSENT = "ABSENT"
    EXTRACTION_UNRELIABLE = "EXTRACTION_UNRELIABLE"
    UNFETCHABLE = "UNFETCHABLE"


@dataclass(frozen=True)
class CitationVerification:
    """Result of checking one (url, claimed_quote) pair.

    `truncated` is True when the fuzzy-match scan only covered the first
    `_MAX_CHARS_CONSIDERED` characters of a longer document -- see
    `_best_fuzzy_match`. It is never True together with ABSENT: a
    truncated scan that found no match reports EXTRACTION_UNRELIABLE
    instead, since it cannot prove the quote is absent from the part of
    the document it never saw.
    """

    status: CitationStatus
    url: str
    claimed_quote: str
    similarity: float | None = None
    best_match: str | None = None
    detail: str | None = None
    fetched_content_length: int | None = None
    llm_adjudication: str | None = None
    truncated: bool = False


@dataclass(frozen=True)
class _FetchOutcome:
    """Internal: result of retrieving and extracting text from a URL.

    `raw_markup` is the undecoded/pre-extraction document (raw HTML) when
    one exists, used only for the text-to-markup ratio signal in
    `_extraction_is_unreliable`. It is None for PDFs (no markup concept --
    the extracted-text-length backstop covers image-only PDFs instead) and
    for plain text.

    `pdf_unreliable_reason` is set only when a PDF's text came back through
    `uncorrupt.extraction.pdf.extract_pdf_bytes` with
    `ExtractionStatus.EXTRACTION_UNRELIABLE` (text layer AND OCR both failed
    the quality gate) -- the extraction layer's own verdict, not a second
    heuristic computed here.
    """

    ok: bool
    text: str | None = None
    content_type: str | None = None
    status_code: int | None = None
    reason: str | None = None
    raw_markup: str | None = None
    pdf_unreliable_reason: str | None = None


def _normalize(text: str) -> str:
    """Normalise whitespace, quote-mark style, dash style, and space-before-
    terminal-punctuation rendering artefacts for comparison.

    Deliberately does NOT lowercase -- VERBATIM is defined as an exact match
    modulo rendering differences only, not modulo case or wording.
    """
    text = unicodedata.normalize("NFKC", text)
    for src, dst in _QUOTE_CHARS.items():
        text = text.replace(src, dst)
    for src, dst in _DASH_CHARS.items():
        text = text.replace(src, dst)
    text = text.replace(" ", " ")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return _SPACE_BEFORE_TERMINAL_PUNCTUATION_RE.sub("", text)


def _domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


def _looks_like_html(text: str) -> bool:
    stripped = text.lstrip()[:512].lower()
    return "<html" in stripped or "<!doctype html" in stripped or "<body" in stripped


def _looks_like_challenge_page(extracted_text: str, raw: str) -> bool:
    haystack = f"{extracted_text}\n{raw}"[:8000].casefold()
    return any(marker in haystack for marker in _CHALLENGE_MARKERS)


def _extract_html_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(separator=" ")


def _extract_pdf_outcome(raw_bytes: bytes, status_code: int) -> _FetchOutcome:
    """Extract PDF text via the shared `uncorrupt.extraction.pdf` layer
    (pypdf text layer, OCR quality-gated fallback) rather than a second,
    bespoke PDF-reliability heuristic maintained in parallel here -- see the
    "PDF extraction" section of the module docstring.

    BACKEND_UNAVAILABLE (no pypdf, or the text layer failed the quality gate
    and no OCR backend is available) and FAILED (the bytes could not be
    parsed as a PDF at all, e.g. a challenge page mislabelled as one) both
    map to `ok=False` -- the tool could not obtain any text to check at all,
    which is the UNFETCHABLE case, not "checked and unreliable". TEXT_LAYER
    and OCR (`result.is_reliable`) map straight through. EXTRACTION_UNRELIABLE
    (text layer AND OCR both failed the quality gate) still returns the
    (unreliable) text with `ok=True` plus `pdf_unreliable_reason` set -- the
    same "only ever escalates what would otherwise be ABSENT" contract as
    every other EXTRACTION_UNRELIABLE signal in this module, so a quote that
    genuinely is verbatim/near in that text still classifies correctly.
    """
    result = extract_pdf_bytes(raw_bytes)

    if result.status in (ExtractionStatus.BACKEND_UNAVAILABLE, ExtractionStatus.FAILED):
        return _FetchOutcome(
            ok=False,
            status_code=status_code,
            reason=f"pdf_extraction_{result.status.value}:{result.error}",
        )

    text = result.text
    if _looks_like_challenge_page(text, text):
        return _FetchOutcome(ok=False, status_code=status_code, reason="challenge_page_detected")

    pdf_unreliable_reason = None
    if result.status == ExtractionStatus.EXTRACTION_UNRELIABLE:
        quality_reason = result.quality.reason if result.quality else result.error
        pdf_unreliable_reason = f"pdf_extraction_unreliable:{quality_reason}"

    return _FetchOutcome(
        ok=True,
        text=text,
        content_type="application/pdf",
        status_code=status_code,
        pdf_unreliable_reason=pdf_unreliable_reason,
    )


def _extract_outcome(
    raw_bytes: bytes, content_type: str, url: str, status_code: int
) -> _FetchOutcome:
    content_type_lower = (content_type or "").lower()
    is_pdf = "application/pdf" in content_type_lower or url.lower().split("?")[0].endswith(".pdf")

    if is_pdf:
        return _extract_pdf_outcome(raw_bytes, status_code)

    try:
        decoded = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        decoded = raw_bytes.decode("latin-1", errors="replace")

    text = (
        _extract_html_text(decoded)
        if ("html" in content_type_lower or _looks_like_html(decoded))
        else decoded
    )

    if _looks_like_challenge_page(text, decoded):
        return _FetchOutcome(ok=False, status_code=status_code, reason="challenge_page_detected")

    return _FetchOutcome(
        ok=True,
        text=text,
        content_type=content_type or "text/plain",
        status_code=status_code,
        raw_markup=decoded,
    )


def _cache_paths(cache_dir: Path, url: str) -> tuple[Path, Path]:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return cache_dir / f"{key}.raw", cache_dir / f"{key}.provenance.json"


def _cache_is_valid(raw_path: Path, provenance: dict, max_age_days: int) -> bool:
    """A cache entry is trusted only if it is fresh and its content hash matches.

    Mirrors `uncorrupt.graph.ch_officers._cache_is_valid`.
    """
    retrieved_at = datetime.fromisoformat(provenance["retrieved_at"])
    age_days = (datetime.now(UTC) - retrieved_at).days
    if age_days > max_age_days:
        return False
    actual_hash = f"sha256:{hashlib.sha256(raw_path.read_bytes()).hexdigest()}"
    return actual_hash == provenance["content_hash"]


def _get_with_backoff(
    client: httpx.Client, url: str, max_retries: int, polite_delay_seconds: float
) -> httpx.Response:
    delay = 2.0
    last_exc: Exception | None = None
    for _attempt in range(max_retries):
        try:
            response = client.get(url, headers={"User-Agent": USER_AGENT})
        except httpx.TransportError as exc:
            last_exc = exc
            time.sleep(delay)
            delay *= 2
            continue
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = response.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else delay)
            delay *= 2
            continue
        return response
    raise RuntimeError(f"citation fetch failed after {max_retries} retries: {url}") from last_exc


def _fetch_url(
    url: str,
    *,
    client: httpx.Client | None = None,
    cache_dir: str | Path | None = None,
    max_cache_age_days: int = DEFAULT_MAX_CACHE_AGE_DAYS,
    max_retries: int = 3,
    polite_delay_seconds: float = 0.0,
    known_unfetchable_domains: frozenset[str] = KNOWN_BLOCKED_DOMAINS,
) -> _FetchOutcome:
    domain = _domain(url)
    if domain in known_unfetchable_domains:
        return _FetchOutcome(ok=False, reason=f"known_blocked_domain:{domain}")

    cache_dir_path = Path(cache_dir) if cache_dir else None
    if cache_dir_path is not None:
        cache_dir_path.mkdir(parents=True, exist_ok=True)
        raw_path, provenance_path = _cache_paths(cache_dir_path, url)
        if raw_path.exists() and provenance_path.exists():
            provenance = json.loads(provenance_path.read_text())
            if _cache_is_valid(raw_path, provenance, max_cache_age_days):
                return _extract_outcome(
                    raw_path.read_bytes(),
                    provenance.get("content_type", ""),
                    url,
                    provenance.get("status_code", 200),
                )

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        response = _get_with_backoff(client, url, max_retries, polite_delay_seconds)
    except RuntimeError as exc:
        return _FetchOutcome(ok=False, reason=f"network_error:{exc}")
    finally:
        if owns_client:
            client.close()

    if response.status_code == 404:
        return _FetchOutcome(ok=False, status_code=404, reason="http_404")
    if response.status_code >= 400:
        return _FetchOutcome(
            ok=False, status_code=response.status_code, reason=f"http_{response.status_code}"
        )

    content_type = response.headers.get("content-type", "")
    raw_bytes = response.content

    if cache_dir_path is not None:
        raw_path, provenance_path = _cache_paths(cache_dir_path, url)
        raw_path.write_bytes(raw_bytes)
        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        provenance_path.write_text(
            json.dumps(
                {
                    "url": url,
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "content_hash": f"sha256:{content_hash}",
                    "status_code": response.status_code,
                    "content_type": content_type,
                },
                indent=2,
            )
        )

    return _extract_outcome(raw_bytes, content_type, url, response.status_code)


def _split_sentence_windows(text: str) -> list[str]:
    """Sentence-and-short-run candidates for fuzzy matching against a quote.

    Windows of 1, 2 and 3 consecutive sentences so a quote spanning a
    sentence boundary can still be matched, and so the winning candidate is
    usually a clean, human-readable sentence for `result.best_match` rather
    than an arbitrary character offset.

    This is ADDITIONAL to, never a substitute for, the whole-text sliding
    window in `_best_fuzzy_match`: gating candidate generation on sentence
    *shape* alone silently defeats matching in two real shapes this project's
    sources take -- an unpunctuated table/list/heading run that never hits a
    `[.!?]` (one giant "sentence" whose SequenceMatcher ratio against a short
    quote collapses toward zero regardless of content), and a block quote
    spanning more than 3 sentences (beyond this function's window). Both were
    confirmed to produce false ABSENT verdicts before the sliding window was
    added as the primary defence and this function became a secondary,
    readability-oriented source of candidates.
    """
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    candidates: list[str] = []
    for window in (1, 2, 3):
        for i in range(max(len(sentences) - window + 1, 0)):
            candidates.append(" ".join(sentences[i : i + window]))
    return candidates


def _sliding_char_windows(text: str, window_len: int, step: int) -> list[str]:
    """Quote-length substrings of `text`, stepped by `step` characters.

    Runs against the WHOLE text, not any particular segment -- earlier this
    only fired on segments judged to "look like" an unpunctuated block by
    length, which was non-monotone (a small table could score worse than a
    huge one) and missed block quotes spanning many sentences entirely.
    Matching against the raw text directly, regardless of its punctuation or
    sentence shape, is what the shape-based gate was trying to approximate
    and failed to.
    """
    if window_len <= 0 or not text:
        return []
    if len(text) <= window_len:
        return [text]
    return [text[i : i + window_len] for i in range(0, len(text) - window_len + 1, step)]


def _best_of(quote_cf: str, candidates: list[str]) -> tuple[str | None, float]:
    """The candidate with the highest SequenceMatcher ratio against `quote_cf`."""
    best_candidate: str | None = None
    best_score = 0.0
    for candidate in candidates:
        score = difflib.SequenceMatcher(None, quote_cf, candidate.casefold()).ratio()
        if score > best_score:
            best_score = score
            best_candidate = candidate
    return best_candidate, best_score


def _best_fuzzy_match(
    normalized_quote: str, normalized_text: str, near_threshold: float
) -> tuple[str | None, float, bool]:
    """Return the best-matching candidate, its similarity ratio, and whether
    the scan was truncated to `_MAX_CHARS_CONSIDERED` characters of the text.

    Two independent candidate sources exist: sentence-window runs
    (`_split_sentence_windows`, 1-3 sentences, giving a readable
    `best_match` for ordinary prose) and a quote-length window slid across
    the whole (possibly truncated) text at a quarter-quote-length step
    (`_sliding_char_windows`) -- the latter is what actually defends against
    false ABSENT on unpunctuated tables and multi-sentence block quotes,
    since it does not depend on sentence segmentation succeeding at all.

    The sentence-window candidates are tried FIRST and returned directly if
    the best one already clears `near_threshold`: a short quote-length
    substring frequently scores *higher* than a full matching sentence
    purely because it is closer in length to the quote (SequenceMatcher's
    ratio is sensitive to combined length), so pooling both sets and taking
    the single best score would silently prefer an arbitrary character
    offset over a coherent, human-readable sentence whenever they are
    close. The whole-text sliding window is only computed -- and only wins
    -- when sentence segmentation alone did not already prove a match, which
    is exactly the unpunctuated-table / long-block-quote case it exists for.
    This also means the more expensive whole-text scan is skipped entirely
    for the common case of an ordinary prose match.
    """
    if not normalized_quote or not normalized_text:
        return None, 0.0, False

    truncated = len(normalized_text) > _MAX_CHARS_CONSIDERED
    scan_text = normalized_text[:_MAX_CHARS_CONSIDERED] if truncated else normalized_text

    quote_cf = normalized_quote.casefold()
    quote_len = len(normalized_quote)

    best_sentence, best_sentence_score = _best_of(quote_cf, _split_sentence_windows(scan_text))
    if best_sentence_score >= near_threshold:
        return best_sentence, best_sentence_score, truncated

    step = max(quote_len // 4, 1)
    best_sliding, best_sliding_score = _best_of(
        quote_cf, _sliding_char_windows(scan_text, quote_len, step)
    )

    if best_sliding_score > best_sentence_score:
        return best_sliding, best_sliding_score, truncated
    return best_sentence, best_sentence_score, truncated


def _extraction_is_unreliable(
    *,
    normalized_text: str,
    raw_markup: str | None,
    best_sentence: str | None,
    best_score: float,
    pdf_unreliable_reason: str | None = None,
) -> str | None:
    """Return a reason string if the extracted text is not plausibly the
    document's real substance, else None.

    Only ever consulted for what would otherwise be an ABSENT result (see
    `verify_citation`) -- a VERBATIM or NEAR match already proves extraction
    worked, so this function is never given the chance to touch those.
    Conservative by construction: any one signal firing is enough to
    escalate from ABSENT to EXTRACTION_UNRELIABLE, per ADR-008's principle
    that a false "unreliable" costs a human a look while a false "absent"
    impugns a real case.
    """
    if pdf_unreliable_reason is not None:
        return pdf_unreliable_reason

    text_length = len(normalized_text)

    if best_score < EXTRACTION_UNRELIABLE_BOILERPLATE_SIMILARITY_CEILING and best_sentence:
        best_lower = best_sentence.casefold()
        if any(marker in best_lower for marker in _BOILERPLATE_MARKERS):
            return "best_match_is_nav_or_footer_boilerplate"

    if raw_markup:
        markup_length = len(raw_markup)
        ratio = text_length / markup_length if markup_length else 0.0
        if ratio < EXTRACTION_UNRELIABLE_MIN_TEXT_TO_MARKUP_RATIO:
            return f"low_text_to_markup_ratio:{ratio:.4f}"

        raw_lower = raw_markup.casefold()
        if (
            any(marker in raw_lower for marker in _JS_APP_ROOT_MARKERS)
            and text_length < _JS_APP_ROOT_TEXT_LENGTH_CAP
        ):
            return "js_app_root_detected_with_negligible_body_text"

    if text_length < EXTRACTION_UNRELIABLE_MIN_TEXT_LENGTH:
        return f"extracted_text_implausibly_short:{text_length}_chars"

    return None


def verify_citation(
    url: str,
    claimed_quote: str,
    *,
    fetched_text: str | None = None,
    client: httpx.Client | None = None,
    cache_dir: str | Path | None = None,
    max_cache_age_days: int = DEFAULT_MAX_CACHE_AGE_DAYS,
    max_retries: int = 3,
    polite_delay_seconds: float = 0.0,
    known_unfetchable_domains: frozenset[str] = KNOWN_BLOCKED_DOMAINS,
    near_threshold: float = DEFAULT_NEAR_THRESHOLD,
    adjudicate_near_with_llm: bool = False,
    llm_adjudicator: Callable[[str, str], str] | None = None,
) -> CitationVerification:
    """Check whether `claimed_quote` actually appears in the document at `url`.

    Deterministic core, no LLM required: fetches `url` (or uses
    `fetched_text` directly, e.g. for a test or an already-scraped pass),
    extracts text (HTML via BeautifulSoup; PDF via
    `uncorrupt.extraction.pdf.extract_pdf_bytes`), normalises whitespace/
    quote-style/dash-style on both sides, and classifies the result as
    VERBATIM, NEAR, ABSENT, EXTRACTION_UNRELIABLE, or UNFETCHABLE (see module
    docstring for the exact meaning of each).

    EXTRACTION_UNRELIABLE is only ever considered in place of what would
    otherwise be ABSENT (see `_extraction_is_unreliable`) -- it can never
    override a VERBATIM or NEAR result, since either already proves real
    document content was read. This tool never renders JavaScript; a page
    whose real content only exists after JS execution reports
    EXTRACTION_UNRELIABLE, not ABSENT -- that is the signal to fall back to
    a browser-based capture path, not a claim about the citation itself.

    `adjudicate_near_with_llm` + `llm_adjudicator` is the one optional,
    off-by-default LLM hook (ADR-004 allows LLMs in research/extraction,
    never in this measurement path): when set, `llm_adjudicator(claimed_quote,
    best_match)` is called ONLY when the deterministic result is already NEAR,
    and its return value is stored in `result.llm_adjudication` for a human
    to read -- it can only annotate an existing NEAR result, never change
    `result.status`. The adjudicator is never invoked on VERBATIM, ABSENT,
    EXTRACTION_UNRELIABLE, or UNFETCHABLE results, so there is no code path by
    which an LLM call can promote an ABSENT (or EXTRACTION_UNRELIABLE, or
    UNFETCHABLE) citation into anything else.
    """
    if fetched_text is not None:
        outcome = _FetchOutcome(
            ok=True, text=fetched_text, content_type="text/plain", status_code=200
        )
    else:
        outcome = _fetch_url(
            url,
            client=client,
            cache_dir=cache_dir,
            max_cache_age_days=max_cache_age_days,
            max_retries=max_retries,
            polite_delay_seconds=polite_delay_seconds,
            known_unfetchable_domains=known_unfetchable_domains,
        )

    if not outcome.ok:
        return CitationVerification(
            status=CitationStatus.UNFETCHABLE,
            url=url,
            claimed_quote=claimed_quote,
            detail=outcome.reason,
        )

    fetched = outcome.text or ""
    normalized_quote = _normalize(claimed_quote)
    normalized_text = _normalize(fetched)

    if normalized_quote and normalized_quote in normalized_text:
        return CitationVerification(
            status=CitationStatus.VERBATIM,
            url=url,
            claimed_quote=claimed_quote,
            fetched_content_length=len(fetched),
        )

    best_sentence, best_score, scan_truncated = _best_fuzzy_match(
        normalized_quote, normalized_text, near_threshold
    )

    if best_score >= near_threshold:
        result = CitationVerification(
            status=CitationStatus.NEAR,
            url=url,
            claimed_quote=claimed_quote,
            similarity=best_score,
            best_match=best_sentence,
            fetched_content_length=len(fetched),
            truncated=scan_truncated,
        )
        if adjudicate_near_with_llm and llm_adjudicator is not None:
            adjudication = llm_adjudicator(claimed_quote, best_sentence or "")
            result = replace(result, llm_adjudication=adjudication)
        return result

    if scan_truncated:
        # The fuzzy scan only covered the first `_MAX_CHARS_CONSIDERED`
        # characters and found nothing there -- that is not proof the quote
        # is absent from the rest of the document, so this can never
        # classify as ABSENT.
        return CitationVerification(
            status=CitationStatus.EXTRACTION_UNRELIABLE,
            url=url,
            claimed_quote=claimed_quote,
            similarity=best_score,
            best_match=best_sentence,
            detail=f"document_scan_truncated_after_{_MAX_CHARS_CONSIDERED}_characters",
            fetched_content_length=len(fetched),
            truncated=True,
        )

    unreliable_reason = _extraction_is_unreliable(
        normalized_text=normalized_text,
        raw_markup=outcome.raw_markup,
        best_sentence=best_sentence,
        best_score=best_score,
        pdf_unreliable_reason=outcome.pdf_unreliable_reason,
    )
    if unreliable_reason is not None:
        return CitationVerification(
            status=CitationStatus.EXTRACTION_UNRELIABLE,
            url=url,
            claimed_quote=claimed_quote,
            similarity=best_score,
            best_match=best_sentence,
            detail=unreliable_reason,
            fetched_content_length=len(fetched),
        )

    return CitationVerification(
        status=CitationStatus.ABSENT,
        url=url,
        claimed_quote=claimed_quote,
        similarity=best_score,
        best_match=best_sentence,
        fetched_content_length=len(fetched),
    )
