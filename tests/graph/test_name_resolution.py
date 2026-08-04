"""Tests for peerage name resolution in the Phase C matcher.

These exist because a positive-control run found that 14 of 15 resolution
failures came from one bug: taking the last token of a peerage name yields the
territorial designation, not the family name.

The `normalise_name` / `prefer_companies_house` tests below exist because a
later positive-control run (28/30) found the two remaining failures were
resolver ambiguity, not retrieval failures: "CLOSE BROTHERS GROUP PLC" matched
3 company nodes (over-aggressive suffix stripping collapsed a parent and two
subsidiaries onto one key) and "EDGE FOUNDATION" matched 2 (a genuine
Companies House + GLEIF-LEI duplicate for one real organisation).
"""

import pytest
from scripts.phase_c_paths import normalise_name, prefer_companies_house, surname

from uncorrupt.graph.models import Entity


def test_territorial_designation_is_not_the_surname():
    """GIVEN a peerage name with a territorial designation
    WHEN the surname is extracted
    THEN it is the family name, not the place.

    "Baroness Mone of Mayfair" must resolve to "mone" — an external table
    writes "Baroness Mone", so matching on "mayfair" can never succeed.
    """
    assert surname("Baroness Mone of Mayfair") == "mone"


def test_territorial_designation_stripped_for_lords():
    """GIVEN other peers written with 'of <place>'
    WHEN the surname is extracted
    THEN the place is discarded."""
    assert surname("Lord Agnew of Oulton") == "agnew"
    assert surname("Lord Allan of Hallam") == "allan"


def test_honorific_is_not_the_surname():
    """GIVEN a knighthood honorific
    WHEN the surname is extracted
    THEN the family name is returned, not the title."""
    assert surname("Sir Gavin Williamson") == "williamson"


def test_plain_name_is_unchanged():
    """GIVEN an MP written without any title
    WHEN the surname is extracted
    THEN the last name is returned."""
    assert surname("Kim Leadbeater") == "leadbeater"


def test_empty_name_returns_empty_string():
    """GIVEN no name
    WHEN the surname is extracted
    THEN the result is empty rather than an exception."""
    assert surname("") == ""
    assert surname(None) == ""


def test_single_token_name_survives():
    """GIVEN a name that is already just a surname
    WHEN extracted
    THEN it is returned lowercased."""
    assert surname("Adebowale") == "adebowale"


def test_post_nominal_is_not_the_surname():
    """GIVEN an MP written with a post-nominal
    WHEN the surname is extracted
    THEN it is the family name, not "mp".

    Every MP in a cohort otherwise collapses onto the same key — this produced
    four phantom matches in Phase C, all pointing at one unrelated officer.
    """
    assert surname("Matt Hancock MP") == "hancock"
    assert surname("Dr Julian Lewis MP") == "lewis"


def test_companies_house_comma_format_uses_the_leading_surname():
    """GIVEN the CH "SURNAME, Forenames, Title" format
    WHEN the surname is extracted
    THEN it is the part before the first comma, not the last token.

    "LEWIS, John Patrick, Sir" indexed under "patrick" before this fix, so all
    21,082 officer entities were keyed by a forename.
    """
    assert surname("LEWIS, John Patrick, Sir") == "lewis"
    assert surname("AGNEW, Theodore Thomas More, Lord") == "agnew"


def test_group_and_holdings_are_not_stripped_as_suffixes():
    """GIVEN three real, separately-registered companies in one corporate group
    WHEN their names are normalised
    THEN "GROUP" and "HOLDINGS" survive as distinguishing tokens, so the three
    normalise to three different keys instead of colliding on "CLOSE BROTHERS".
    """
    parent = normalise_name("CLOSE BROTHERS GROUP PLC")
    subsidiary = normalise_name("CLOSE BROTHERS LIMITED")
    other_subsidiary = normalise_name("CLOSE BROTHERS HOLDINGS LIMITED")
    assert len({parent, subsidiary, other_subsidiary}) == 3


def test_legal_form_suffixes_are_still_stripped():
    """GIVEN a name that differs only by a generic legal-form suffix
    WHEN normalised
    THEN the suffix is stripped so the two forms match.
    """
    assert normalise_name("Acme Widgets Limited") == normalise_name("Acme Widgets Ltd")


def test_prefer_companies_house_singleton_list_is_unchanged():
    """GIVEN a candidate list with 0 or 1 entries
    WHEN disambiguated
    THEN the list is returned unchanged (nothing to disambiguate)."""
    assert prefer_companies_house([]) == []
    one = [Entity(entity_type="company", name="X", registry_scheme="GB-COH")]
    assert prefer_companies_house(one) == one


def test_prefer_companies_house_picks_the_unique_gb_coh_candidate():
    """GIVEN a name match with one GB-COH candidate and one GLEIF-LEI candidate
    WHEN disambiguated
    THEN the GB-COH candidate wins — Companies House is the authoritative
    national register, and the GLEIF-LEI row is not merged or deleted."""
    gb_coh = Entity(entity_type="company", name="EDGE FOUNDATION", registry_scheme="GB-COH")
    gleif = Entity(entity_type="company", name="EDGE FOUNDATION", registry_scheme="GLEIF-LEI")
    assert prefer_companies_house([gleif, gb_coh]) == [gb_coh]


def test_prefer_companies_house_leaves_two_gb_coh_candidates_ambiguous():
    """GIVEN two GB-COH candidates for the same name
    WHEN disambiguated
    THEN nothing is preferred — the ambiguity is real and stays unresolved."""
    first = Entity(entity_type="company", name="X", registry_scheme="GB-COH")
    second = Entity(entity_type="company", name="X", registry_scheme="GB-COH")
    assert prefer_companies_house([first, second]) == [first, second]


def test_prefer_companies_house_leaves_no_gb_coh_candidates_ambiguous():
    """GIVEN candidates that are all non-GB-COH (e.g. two GLEIF-LEI records)
    WHEN disambiguated
    THEN nothing is preferred — there is no authoritative tie-break to apply."""
    first = Entity(entity_type="company", name="X", registry_scheme="GLEIF-LEI")
    second = Entity(entity_type="company", name="X", registry_scheme="GLEIF-LEI")
    assert prefer_companies_house([first, second]) == [first, second]


@pytest.mark.django_db
def test_resolve_supplier_disambiguates_gb_coh_and_gleif_duplicate():
    """GIVEN a Companies House Entity and a GLEIF-LEI Entity with the identical
    name (a real organisation registered in both registers)
    WHEN resolve_supplier is asked to resolve that name with no company_number
    and no cache hit
    THEN it returns the Companies House Entity, not None — the ambiguity is
    resolved without merging or deleting the GLEIF-LEI row."""
    from scripts.phase_c_paths import resolve_supplier

    gb_coh = Entity.objects.create(
        entity_type="company",
        name="EDGE FOUNDATION",
        registry_scheme="GB-COH",
        registry_id="01686164",
        company_number="01686164",
    )
    Entity.objects.create(
        entity_type="company",
        name="EDGE FOUNDATION",
        registry_scheme="GLEIF-LEI",
        registry_id="2138006GI3MOCJ9RRR23",
    )

    resolved = resolve_supplier("EDGE FOUNDATION", ch_cache={})

    assert resolved == gb_coh


@pytest.mark.django_db
def test_resolve_supplier_by_company_number_prefers_gb_coh_over_gleif_twins():
    """GIVEN a company number held by two GLEIF-LEI Entities (created first,
    so they hold the lowest database ids) and one GB-COH Entity (created
    last) -- mirroring the real "SC214564" case, where GLEIF cross-links a
    UK company's LEI record with its Companies House twin's company_number
    WHEN resolve_supplier is given that company_number
    THEN it returns the GB-COH Entity, not either GLEIF-LEI twin -- a plain
    `.first()` on `company_number=cn` implicitly orders by id and would have
    returned the first-created (GLEIF) row, and `officer_of` edges only
    attach to the GB-COH node, so returning a GLEIF twin silently yields a
    company with no officers."""
    from scripts.phase_c_paths import resolve_supplier

    Entity.objects.create(
        entity_type="company",
        name="N F U OF SCOTLAND",
        registry_scheme="GLEIF-LEI",
        registry_id="LEI0000000000000NFU1",
        company_number="SC214564",
    )
    Entity.objects.create(
        entity_type="company",
        name="NFU SCOTLAND",
        registry_scheme="GLEIF-LEI",
        registry_id="LEI0000000000000NFU2",
        company_number="SC214564",
    )
    gb_coh = Entity.objects.create(
        entity_type="company",
        name="NFU SCOTLAND",
        registry_scheme="GB-COH",
        registry_id="SC214564",
        company_number="SC214564",
    )

    resolved = resolve_supplier("NFU SCOTLAND", ch_cache={}, company_number="SC214564")

    assert resolved == gb_coh


@pytest.mark.django_db
def test_resolve_supplier_via_cache_company_number_is_deterministic_across_calls():
    """GIVEN a company number sourced from the Companies House name cache
    (not the cohort CSV's own company_number column) that is held by both a
    GLEIF-LEI Entity (created first, lowest id) and a GB-COH Entity (created
    second)
    WHEN resolve_supplier is called five times with the same input
    THEN every call returns the same GB-COH Entity -- the cache-sourced
    lookup shares the GB-COH-preferring resolution, not a second, separately
    broken `.first()`."""
    from scripts.phase_c_paths import resolve_supplier

    Entity.objects.create(
        entity_type="company",
        name="EXAMPLE GLEIF TWIN",
        registry_scheme="GLEIF-LEI",
        registry_id="LEI0000000000000EX01",
        company_number="00998877",
    )
    gb_coh = Entity.objects.create(
        entity_type="company",
        name="EXAMPLE LIMITED",
        registry_scheme="GB-COH",
        registry_id="00998877",
        company_number="00998877",
    )
    ch_cache = {"EXAMPLE LIMITED": {"company_number": "00998877"}}

    results = [resolve_supplier("EXAMPLE LIMITED", ch_cache) for _ in range(5)]

    assert results == [gb_coh] * 5


@pytest.mark.django_db
def test_resolve_supplier_two_genuine_namesakes_yield_no_match():
    """GIVEN two distinct real companies (different registry_id, different
    company_number) that share an exact normalised name, both GB-COH so
    prefer_companies_house cannot break the tie
    WHEN resolve_supplier is asked to resolve that name
    THEN it returns None -- the uniqueness guard still refuses to guess when
    genuine ambiguity remains after GB-COH preference."""
    from scripts.phase_c_paths import resolve_supplier

    Entity.objects.create(
        entity_type="company",
        name="TWIN TRADING LIMITED",
        registry_scheme="GB-COH",
        registry_id="11112222",
        company_number="11112222",
    )
    Entity.objects.create(
        entity_type="company",
        name="TWIN TRADING LIMITED",
        registry_scheme="GB-COH",
        registry_id="33334444",
        company_number="33334444",
    )

    resolved = resolve_supplier("TWIN TRADING LIMITED", ch_cache={})

    assert resolved is None


@pytest.mark.django_db
def test_resolve_supplier_capped_window_cannot_prove_uniqueness():
    """GIVEN 201 companies whose name contains the same 15-character search
    prefix: one genuine exact-name namesake created first (lowest id, so it
    falls inside an unordered 200-row window), 199 filler companies that
    match the substring but not the exact name, and a second genuine
    exact-name namesake created last (highest id, so it falls outside an
    unordered 200-row window)
    WHEN resolve_supplier resolves that name
    THEN it returns None -- a window that had to be capped can never be
    trusted to prove uniqueness. Before the fix, the second namesake's
    exclusion from the window was invisible to the guard, which saw a single
    candidate and returned it as if it were unique."""
    from scripts.phase_c_paths import resolve_supplier

    target_name = "CAPWATCH INDUSTRIES LIMITED"
    Entity.objects.create(
        entity_type="company",
        name=target_name,
        registry_scheme="GB-COH",
        registry_id="55550001",
        company_number="55550001",
    )
    for i in range(199):
        Entity.objects.create(
            entity_type="company",
            name=f"CAPWATCH INDUSTRIES FILLER {i} LIMITED",
            registry_scheme="GB-COH",
            registry_id=f"5555{i + 1000}",
            company_number=f"5555{i + 1000}",
        )
    Entity.objects.create(
        entity_type="company",
        name=target_name,
        registry_scheme="GB-COH",
        registry_id="55559999",
        company_number="55559999",
    )

    resolved = resolve_supplier(target_name, ch_cache={})

    assert resolved is None
