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
core VERBATIM/NEAR/ABSENT/UNFETCHABLE decision requires no LLM at all.

Four outcomes, deliberately not collapsed into pass/fail:

  * VERBATIM      -- the quote appears in the fetched text after normalising
                      whitespace, quote-mark style (curly vs straight) and
                      dash style. This is the "no defect" case.
  * NEAR          -- not verbatim, but a high-similarity source sentence (or
                      short run of sentences) exists. Returned together with
                      that best-matching text so a human can judge paraphrase
                      vs fabrication.
  * ABSENT        -- the document fetched fine, but no sentence resembles the
                      claimed quote. This is the defect the whole tool exists
                      to catch (the "quote appears in quotation marks but
                      isn't in the article" failure mode from the overnight
                      run).
  * UNFETCHABLE   -- the document could not be retrieved as real content:
                      blocked domain, HTTP error, network failure, a PDF with
                      no `pdftotext` available, or a Cloudflare/JS challenge
                      page served with a 200. This is explicitly NOT the same
                      as ABSENT -- a quote is not "missing" just because the
                      fetcher was blocked, and conflating the two would erase
                      the exact distinction this tool exists to preserve.

An optional, off-by-default LLM adjudication step (`adjudicate_near_with_llm`)
may relabel a NEAR match's *interpretation* (paraphrase vs likely fabrication)
for a human reviewer -- see `verify_citation`'s docstring for the guarantee
that it can only ever fire on a NEAR result and can never turn ABSENT (or
UNFETCHABLE) into anything else. ADR-004 permits LLMs in research/extraction;
this measurement path is not that -- the VERBATIM/NEAR/ABSENT/UNFETCHABLE
classification itself is always decided by the deterministic checks above.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
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

# Bound on how much of a document is fuzzy-matched against, so one huge
# article/PDF cannot make verification pathologically slow.
_MAX_SENTENCES_CONSIDERED = 800


class CitationStatus(StrEnum):
    """The four outcomes of `verify_citation`. Never collapsed to pass/fail."""

    VERBATIM = "VERBATIM"
    NEAR = "NEAR"
    ABSENT = "ABSENT"
    UNFETCHABLE = "UNFETCHABLE"


@dataclass(frozen=True)
class CitationVerification:
    """Result of checking one (url, claimed_quote) pair."""

    status: CitationStatus
    url: str
    claimed_quote: str
    similarity: float | None = None
    best_match: str | None = None
    detail: str | None = None
    fetched_content_length: int | None = None
    llm_adjudication: str | None = None


@dataclass(frozen=True)
class _FetchOutcome:
    """Internal: result of retrieving and extracting text from a URL."""

    ok: bool
    text: str | None = None
    content_type: str | None = None
    status_code: int | None = None
    reason: str | None = None


def _normalize(text: str) -> str:
    """Normalise whitespace, quote-mark style, and dash style for comparison.

    Deliberately does NOT lowercase -- VERBATIM is defined as an exact match
    modulo whitespace/quote/dash rendering only, not modulo case.
    """
    text = unicodedata.normalize("NFKC", text)
    for src, dst in _QUOTE_CHARS.items():
        text = text.replace(src, dst)
    for src, dst in _DASH_CHARS.items():
        text = text.replace(src, dst)
    text = text.replace(" ", " ")
    return _WHITESPACE_RE.sub(" ", text).strip()


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


def _extract_pdf_text(raw_bytes: bytes) -> str | None:
    """Shell out to `pdftotext`. Returns None if the binary is not installed."""
    if shutil.which("pdftotext") is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(raw_bytes)
        tmp.flush()
        try:
            result = subprocess.run(
                ["pdftotext", "-layout", tmp.name, "-"],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def _extract_outcome(
    raw_bytes: bytes, content_type: str, url: str, status_code: int
) -> _FetchOutcome:
    content_type_lower = (content_type or "").lower()
    is_pdf = "application/pdf" in content_type_lower or url.lower().split("?")[0].endswith(".pdf")

    if is_pdf:
        text = _extract_pdf_text(raw_bytes)
        if text is None:
            return _FetchOutcome(
                ok=False,
                status_code=status_code,
                reason="pdftotext_not_available: install poppler (pdftotext) to verify PDF sources",
            )
        if _looks_like_challenge_page(text, text):
            return _FetchOutcome(
                ok=False, status_code=status_code, reason="challenge_page_detected"
            )
        return _FetchOutcome(
            ok=True, text=text, content_type="application/pdf", status_code=status_code
        )

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
        ok=True, text=text, content_type=content_type or "text/plain", status_code=status_code
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


def _split_sentence_windows(text: str, max_sentences: int = _MAX_SENTENCES_CONSIDERED) -> list[str]:
    """Sentence-and-short-run candidates for fuzzy matching against a quote.

    Windows of 1, 2 and 3 consecutive sentences so a quote spanning a
    sentence boundary can still be matched, without doing a full quadratic
    scan of the whole document.
    """
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()][:max_sentences]
    candidates: list[str] = []
    for window in (1, 2, 3):
        for i in range(max(len(sentences) - window + 1, 0)):
            candidates.append(" ".join(sentences[i : i + window]))
    return candidates


def _best_fuzzy_match(normalized_quote: str, normalized_text: str) -> tuple[str | None, float]:
    """Return the best-matching candidate sentence/run and its similarity ratio."""
    if not normalized_quote or not normalized_text:
        return None, 0.0
    quote_cf = normalized_quote.casefold()
    best_sentence: str | None = None
    best_score = 0.0
    for candidate in _split_sentence_windows(normalized_text):
        score = difflib.SequenceMatcher(None, quote_cf, candidate.casefold()).ratio()
        if score > best_score:
            best_score = score
            best_sentence = candidate
    return best_sentence, best_score


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
    extracts text (HTML via BeautifulSoup; PDF via `pdftotext` if
    installed), normalises whitespace/quote-style/dash-style on both sides,
    and classifies the result as VERBATIM, NEAR, ABSENT, or UNFETCHABLE (see
    module docstring for the exact meaning of each).

    `adjudicate_near_with_llm` + `llm_adjudicator` is the one optional,
    off-by-default LLM hook (ADR-004 allows LLMs in research/extraction,
    never in this measurement path): when set, `llm_adjudicator(claimed_quote,
    best_match)` is called ONLY when the deterministic result is already NEAR,
    and its return value is stored in `result.llm_adjudication` for a human
    to read -- it can only annotate an existing NEAR result, never change
    `result.status`. The adjudicator is never invoked on VERBATIM, ABSENT, or
    UNFETCHABLE results, so there is no code path by which an LLM call can
    promote an ABSENT (or UNFETCHABLE) citation into anything else.
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

    best_sentence, best_score = _best_fuzzy_match(normalized_quote, normalized_text)

    if best_score >= near_threshold:
        result = CitationVerification(
            status=CitationStatus.NEAR,
            url=url,
            claimed_quote=claimed_quote,
            similarity=best_score,
            best_match=best_sentence,
            fetched_content_length=len(fetched),
        )
        if adjudicate_near_with_llm and llm_adjudicator is not None:
            adjudication = llm_adjudicator(claimed_quote, best_sentence or "")
            result = replace(result, llm_adjudication=adjudication)
        return result

    return CitationVerification(
        status=CitationStatus.ABSENT,
        url=url,
        claimed_quote=claimed_quote,
        similarity=best_score,
        best_match=best_sentence,
        fetched_content_length=len(fetched),
    )
