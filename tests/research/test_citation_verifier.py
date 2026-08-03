"""Tests for the deterministic citation verifier.

Verifies the core invariants the tool exists for (see
`uncorrupt.research.citation_verifier` module docstring for the motivating
26%-defect-rate benchmark run):

- An exact quote (modulo whitespace/quote/dash rendering) is VERBATIM
- A quote differing only in curly-vs-straight quotes and whitespace is
  still VERBATIM
- A paraphrase is NEAR, with the best-matching source sentence returned
- A quote genuinely absent from a successfully-fetched document is ABSENT
  -- the exact defect class the overnight benchmark run caught by hand
- A blocked/challenge/error response is UNFETCHABLE, and NEVER ABSENT --
  the tool must not conflate "couldn't check" with "checked and it's wrong"
- No code path (including the optional LLM adjudication hook) can promote
  an ABSENT result into a pass
"""

import json
from datetime import UTC, datetime, timedelta

import httpx

from uncorrupt.research.citation_verifier import (
    CitationStatus,
    verify_citation,
)

ARTICLE_TEXT = (
    "The committee published its findings today. "
    "Mr Example has been a paid consultant to Acme Corp, a clinical diagnostics "
    "company, since August 2015. "
    "The report goes on to describe the nature of his advocacy on the company’s "
    "behalf. "
    "A spokesperson for Acme Corp declined to comment on the report findings."
)

VERBATIM_QUOTE = (
    "Mr Example has been a paid consultant to Acme Corp, a clinical diagnostics "
    "company, since August 2015."
)

# Same substring as in ARTICLE_TEXT, but with curly quote + irregular
# whitespace/newlines standing in for how a curator might retype a quote.
CURLY_WHITESPACE_QUOTE = "the nature   of his advocacy\non the company’s  behalf"

PARAPHRASE_QUOTE = "Mr Example has been paid as an adviser to Acme Corp since 2015."

ABSENT_QUOTE = "The minister resigned after admitting he had lied to Parliament about the contract."

CHALLENGE_HTML = """
<html><head><title>Just a moment...</title></head>
<body>
<div id="cf-challenge-stage">Checking your browser before accessing the site.</div>
<div>This process is automatic. Your browser will redirect once the check is complete.</div>
</body></html>
"""


class TestVerbatimMatch:
    def test_exact_quote_is_verbatim(self):
        """GIVEN a claimed quote that appears exactly in the fetched text WHEN
        verified THEN the status is VERBATIM."""
        result = verify_citation(
            "https://example.test/article",
            VERBATIM_QUOTE,
            fetched_text=ARTICLE_TEXT,
        )

        assert result.status == CitationStatus.VERBATIM

    def test_curly_quotes_and_whitespace_differences_are_still_verbatim(self):
        """GIVEN a claimed quote differing from the source only in curly-vs-straight
        apostrophes and irregular whitespace/newlines WHEN verified THEN the status
        is still VERBATIM -- rendering differences are not a sourcing defect."""
        result = verify_citation(
            "https://example.test/article",
            CURLY_WHITESPACE_QUOTE,
            fetched_text=ARTICLE_TEXT,
        )

        assert result.status == CitationStatus.VERBATIM


class TestNearMatch:
    def test_paraphrase_is_near_with_best_matching_sentence_returned(self):
        """GIVEN a claimed quote that paraphrases (rather than quotes) the source
        WHEN verified THEN the status is NEAR and the actual best-matching source
        sentence is returned so a human can judge paraphrase vs fabrication."""
        result = verify_citation(
            "https://example.test/article",
            PARAPHRASE_QUOTE,
            fetched_text=ARTICLE_TEXT,
        )

        assert result.status == CitationStatus.NEAR
        assert result.best_match == VERBATIM_QUOTE
        assert result.similarity is not None
        assert 0.0 < result.similarity < 1.0


class TestAbsentMatch:
    def test_quote_genuinely_absent_from_fetched_article_is_absent(self):
        """GIVEN a claimed quote that does not appear anywhere in the fetched
        article, in any form WHEN verified THEN the status is ABSENT -- the exact
        defect class ('quote in quotation marks that isn't in the article') the
        overnight benchmark run caught only by manual re-fetching."""
        result = verify_citation(
            "https://example.test/article",
            ABSENT_QUOTE,
            fetched_text=ARTICLE_TEXT,
        )

        assert result.status == CitationStatus.ABSENT


class TestUnfetchableIsNeverAbsent:
    def test_403_response_is_unfetchable_not_absent(self):
        """GIVEN the source URL returns HTTP 403 WHEN verified THEN the status is
        UNFETCHABLE, never ABSENT -- a blocked fetch is not evidence the quote is
        missing."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        result = verify_citation(
            "https://example.test/blocked",
            VERBATIM_QUOTE,
            client=client,
            max_retries=1,
        )

        assert result.status == CitationStatus.UNFETCHABLE
        assert result.status != CitationStatus.ABSENT

    def test_404_response_is_unfetchable_not_absent(self):
        """GIVEN the source URL returns HTTP 404 (e.g. a typo'd registry ID) WHEN
        verified THEN the status is UNFETCHABLE, never ABSENT."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        result = verify_citation(
            "https://example.test/does-not-exist",
            VERBATIM_QUOTE,
            client=client,
            max_retries=1,
        )

        assert result.status == CitationStatus.UNFETCHABLE
        assert result.status != CitationStatus.ABSENT

    def test_cloudflare_challenge_page_served_with_200_is_unfetchable_not_absent(self):
        """GIVEN a URL returns HTTP 200 but the body is a Cloudflare/JS interstitial
        challenge page (real behaviour of parliament.uk domains against plain
        fetchers) WHEN verified THEN the status is UNFETCHABLE, never ABSENT -- a
        quote is not 'missing' just because the fetcher was served an interstitial
        instead of the real document."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=CHALLENGE_HTML)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        result = verify_citation(
            "https://committees.parliament.uk/some-report",
            VERBATIM_QUOTE,
            client=client,
            max_retries=1,
        )

        assert result.status == CitationStatus.UNFETCHABLE
        assert result.status != CitationStatus.ABSENT

    def test_known_blocked_domain_is_unfetchable_without_any_network_call(self):
        """GIVEN a URL on a domain known to be unreachable from this environment
        (e.g. theguardian.com) WHEN verified THEN the status is UNFETCHABLE and no
        HTTP request is made at all."""
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, text=ARTICLE_TEXT)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        result = verify_citation(
            "https://www.theguardian.com/some-article",
            VERBATIM_QUOTE,
            client=client,
        )

        assert result.status == CitationStatus.UNFETCHABLE
        assert calls == 0

    def test_network_error_after_exhausting_retries_is_unfetchable(self):
        """GIVEN every request raises a transport error WHEN verified THEN the
        status is UNFETCHABLE after retries are exhausted, never ABSENT."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        result = verify_citation(
            "https://example.test/unreachable",
            VERBATIM_QUOTE,
            client=client,
            max_retries=2,
            polite_delay_seconds=0,
        )

        assert result.status == CitationStatus.UNFETCHABLE


class TestLlmAdjudicationCannotPromoteAbsent:
    def test_absent_result_never_invokes_the_adjudicator(self):
        """GIVEN a genuinely ABSENT quote WHEN verify_citation is called with
        adjudicate_near_with_llm=True and an adjudicator callback THEN the
        adjudicator is NEVER called and the status remains ABSENT -- the
        deterministic ABSENT classification cannot be overridden by an LLM."""
        calls: list[tuple[str, str]] = []

        def spy_adjudicator(quote: str, best_match: str) -> str:
            calls.append((quote, best_match))
            return "PARAPHRASE"  # would be wrong if ever consulted here

        result = verify_citation(
            "https://example.test/article",
            ABSENT_QUOTE,
            fetched_text=ARTICLE_TEXT,
            adjudicate_near_with_llm=True,
            llm_adjudicator=spy_adjudicator,
        )

        assert result.status == CitationStatus.ABSENT
        assert result.llm_adjudication is None
        assert calls == []

    def test_unfetchable_result_never_invokes_the_adjudicator(self):
        """GIVEN an UNFETCHABLE result (403) WHEN verify_citation is called with
        adjudicate_near_with_llm=True THEN the adjudicator is never called."""
        calls: list[tuple[str, str]] = []

        def spy_adjudicator(quote: str, best_match: str) -> str:
            calls.append((quote, best_match))
            return "PARAPHRASE"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        result = verify_citation(
            "https://example.test/blocked",
            VERBATIM_QUOTE,
            client=client,
            max_retries=1,
            adjudicate_near_with_llm=True,
            llm_adjudicator=spy_adjudicator,
        )

        assert result.status == CitationStatus.UNFETCHABLE
        assert calls == []

    def test_near_result_invokes_adjudicator_and_only_annotates_never_changes_status(self):
        """GIVEN a NEAR (paraphrase) result WHEN verify_citation is called with
        adjudicate_near_with_llm=True THEN the adjudicator IS called exactly once
        and its return value is stored in llm_adjudication, but result.status stays
        NEAR regardless of what the adjudicator returns."""
        calls: list[tuple[str, str]] = []

        def adjudicator(quote: str, best_match: str) -> str:
            calls.append((quote, best_match))
            return "FABRICATION"

        result = verify_citation(
            "https://example.test/article",
            PARAPHRASE_QUOTE,
            fetched_text=ARTICLE_TEXT,
            adjudicate_near_with_llm=True,
            llm_adjudicator=adjudicator,
        )

        assert len(calls) == 1
        assert result.status == CitationStatus.NEAR
        assert result.llm_adjudication == "FABRICATION"

    def test_adjudicator_not_invoked_when_flag_is_off_by_default(self):
        """GIVEN a NEAR result WHEN verify_citation is called WITHOUT setting
        adjudicate_near_with_llm THEN the adjudicator is never invoked, confirming
        the LLM hook defaults off."""
        calls: list[tuple[str, str]] = []

        def adjudicator(quote: str, best_match: str) -> str:
            calls.append((quote, best_match))
            return "FABRICATION"

        result = verify_citation(
            "https://example.test/article",
            PARAPHRASE_QUOTE,
            fetched_text=ARTICLE_TEXT,
            llm_adjudicator=adjudicator,
        )

        assert calls == []
        assert result.llm_adjudication is None
        assert result.status == CitationStatus.NEAR


class TestPdfExtraction:
    def test_pdf_quote_verified_via_pdftotext_when_available(self, monkeypatch):
        """GIVEN a URL serving a PDF and pdftotext installed WHEN verified THEN the
        PDF bytes are shelled out to pdftotext and the extracted text is checked
        for the quote."""
        import uncorrupt.research.citation_verifier as verifier_module

        monkeypatch.setattr(verifier_module.shutil, "which", lambda _name: "/usr/bin/pdftotext")

        class FakeCompletedProcess:
            returncode = 0
            stdout = ARTICLE_TEXT.encode("utf-8")

        monkeypatch.setattr(
            verifier_module.subprocess, "run", lambda *a, **k: FakeCompletedProcess()
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"%PDF-fake-bytes", headers={"content-type": "application/pdf"}
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))

        result = verify_citation(
            "https://example.test/letter.pdf",
            VERBATIM_QUOTE,
            client=client,
        )

        assert result.status == CitationStatus.VERBATIM

    def test_pdf_without_pdftotext_installed_is_unfetchable_with_clear_reason(self, monkeypatch):
        """GIVEN a URL serving a PDF and pdftotext NOT installed WHEN verified THEN
        the status is UNFETCHABLE and the detail clearly names the missing tool,
        rather than silently reporting ABSENT."""
        import uncorrupt.research.citation_verifier as verifier_module

        monkeypatch.setattr(verifier_module.shutil, "which", lambda _name: None)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"%PDF-fake-bytes", headers={"content-type": "application/pdf"}
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))

        result = verify_citation(
            "https://example.test/letter.pdf",
            VERBATIM_QUOTE,
            client=client,
        )

        assert result.status == CitationStatus.UNFETCHABLE
        assert "pdftotext" in result.detail


class TestCaching:
    def test_second_verification_of_same_url_reuses_cache_without_a_network_call(self, tmp_path):
        """GIVEN a cache_dir WHEN the same URL is verified twice THEN only one HTTP
        request is made -- the second call is served from the on-disk cache."""
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, text=ARTICLE_TEXT)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        verify_citation(
            "https://example.test/cached-article",
            VERBATIM_QUOTE,
            client=client,
            cache_dir=tmp_path,
        )
        result = verify_citation(
            "https://example.test/cached-article",
            VERBATIM_QUOTE,
            client=client,
            cache_dir=tmp_path,
        )

        assert calls == 1
        assert result.status == CitationStatus.VERBATIM

    def test_stale_cache_entry_triggers_a_refetch(self, tmp_path):
        """GIVEN a cache entry older than max_cache_age_days WHEN the URL is
        verified again THEN it is refetched rather than trusted forever."""
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, text=ARTICLE_TEXT)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        verify_citation(
            "https://example.test/cached-article",
            VERBATIM_QUOTE,
            client=client,
            cache_dir=tmp_path,
            max_cache_age_days=30,
        )
        assert calls == 1

        # Age the provenance record artificially past the freshness window.
        provenance_paths = list(tmp_path.glob("*.provenance.json"))
        assert len(provenance_paths) == 1
        provenance = json.loads(provenance_paths[0].read_text())
        stale_time = datetime.now(UTC) - timedelta(days=31)
        provenance["retrieved_at"] = stale_time.isoformat()
        provenance_paths[0].write_text(json.dumps(provenance))

        verify_citation(
            "https://example.test/cached-article",
            VERBATIM_QUOTE,
            client=client,
            cache_dir=tmp_path,
            max_cache_age_days=30,
        )

        assert calls == 2
