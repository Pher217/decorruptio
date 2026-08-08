"""Cross-check the disqualified-directors register against live officer appointments.

Companies House publishes two registers that can contradict each other:

  * `/disqualified-officers/natural/{id}` — people barred from acting as a director,
    with `person_number`, full date of birth, dated `disqualified_from`/`disqualified_until`,
    the `company_names` the conduct related to, and a CDDA 1986 `reason.section`
    (s.9B is the competition/cartel provision; s.6/s.7 are the insolvency-driven majority).
  * `/company/{n}/officers` — serving officers, where a director who never had a TM01
    filed still shows `resigned_on: null`.

A person can therefore appear as a serving director of a company while simultaneously
carrying an in-force ban naming that same company. That is a statement about the
**public record**, never about the person's conduct: a register records appointment, not
activity, and CDDA s.17 leave may exist without surfacing in `permissions_to_act`.
Output wording must stay on the record, not the individual.

Two artifacts destroy this measurement if left uncontrolled, and both are handled here:

1. **Dissolved-company inflation.** When a company is dissolved nobody files TM01s, so
   every director stays "unresigned" for ever — and the company that collapsed is usually
   the very one that triggered the disqualification. Left in, this reported 50.3% on the
   first run against a true figure of 1.9%. `company_status` is required to be active.

2. **Namesake collision.** Officer records expose only birth *year and month*, so
   name + partial-DOB is NOT an identifier and matching on it reintroduces exactly the
   collision ADR-004 D2 forbids (it produced a "Lee Brown at PPG Industries (UK) Ltd"
   hit on the first run). This script only accepts a hit when Companies House itself
   links the person to the company, via the `company_names` on their own disqualification.

Usage:
    PYTHONPATH=.:src uv run python scripts/disqualified_director_cross_register.py \
        --seeds brown patel wilson --output experiments/disqualified_cross_register.json

Sampling note: `/search/disqualified-officers` requires a query term (an empty `q`
returns 0) and refuses `start_index` beyond ~5000, so the register cannot be enumerated
by this endpoint. `--seeds` draws a *convenience sample* by surname fragment; rates
computed from it are indicative and are labelled as such in the output. A population
figure needs the bulk snapshot, not this script.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

API_ROOT = "https://api.company-information.service.gov.uk"
DEFAULT_OUTPUT = Path("experiments/disqualified_cross_register.json")
DEFAULT_SEEDS = ("brown", "patel", "wilson", "ahmed", "murphy", "clark", "singh", "walsh")

# CDDA 1986 s.9B — disqualification for breach of competition law. The only section that
# labels a person as cartel-implicated rather than insolvency-implicated.
COMPETITION_SECTION = "9B"


def normalise_company_name(name: str | None) -> str:
    """Strip to comparable form for matching a register name against an appointment name."""
    return re.sub(r"[^A-Z0-9]", "", (name or "").upper())


def ban_in_force(disqualification: dict[str, Any], on: date) -> bool:
    """True when `on` falls inside the disqualification's dated window.

    Records missing either bound are treated as not-in-force: an undated ban cannot be
    asserted to cover a given day, and this measurement fails closed (ADR-008).
    """
    try:
        start = date.fromisoformat(disqualification["disqualified_from"])
        end = date.fromisoformat(disqualification["disqualified_until"])
    except (KeyError, TypeError, ValueError):
        return False
    return start <= on <= end


def corroborated_hits(
    disqualification: dict[str, Any], appointments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Appointments that Companies House itself ties to this disqualification.

    An appointment qualifies only when all three hold:
      * it is unresigned (`resigned_on` is falsy),
      * the company is still `active` — excludes the dissolved/liquidated artifact,
      * one of the company's names appears in the disqualification's own `company_names`,
        which is CH asserting the person-company link rather than us inferring it.

    Matching runs over `company_name_aliases` (current name plus every
    `previous_company_names` entry), NOT the current name alone. A disqualification
    records the name the company had at the time of the conduct, and companies rename —
    often straight after the decision that triggered the ban. The motivating case:
    CANTILLON LIMITED (00916538) became MORRISROE DEMOLITION LIMITED six weeks after the
    CMA decision, so a current-name-only match would miss precisely the cases this
    script exists to find.
    """
    named = {normalise_company_name(c) for c in (disqualification.get("company_names") or [])}
    hits = []
    for appointment in appointments:
        if appointment.get("resigned_on"):
            continue
        if appointment.get("company_status") != "active":
            continue
        aliases = appointment.get("company_name_aliases") or [appointment.get("company_name")]
        if not any(normalise_company_name(alias) in named for alias in aliases):
            continue
        hits.append(appointment)
    return hits


@dataclass
class Finding:
    person_number: str | None
    surname: str | None
    forename: str | None
    date_of_birth: str | None
    cdda_section: str | None
    disqualified_from: str | None
    disqualified_until: str | None
    has_permission_to_act: bool
    company_name: str | None
    company_number: str | None
    officer_role: str | None
    appointed_on: str | None


class CompaniesHouse:
    def __init__(self, api_key: str, pause: float = 0.35) -> None:
        self._auth = base64.b64encode(f"{api_key}:".encode()).decode()
        self._pause = pause

    def get(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(
            API_ROOT + path, headers={"Authorization": f"Basic {self._auth}"}
        )
        for _ in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    time.sleep(self._pause)
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    time.sleep(20)
                    continue
                return {"_error": exc.code}
            except OSError:
                time.sleep(2)
        return {"_error": "retries_exhausted"}

    def sample_disqualified(self, seeds: tuple[str, ...], per_seed: int) -> dict[str, str]:
        found: dict[str, str] = {}
        for seed in seeds:
            query = urllib.parse.urlencode({"q": seed, "items_per_page": per_seed})
            result = self.get(f"/search/disqualified-officers?{query}")
            for item in result.get("items") or []:
                link = (item.get("links") or {}).get("self") or ""
                if "natural" in link:
                    found[link] = item.get("title") or ""
        return found

    def appointments_for(self, forename: str, surname: str, year: int, month: int) -> list[dict]:
        """Officer appointments whose name AND birth year/month match.

        This is deliberately a *candidate* filter, not an identity decision — the identity
        assertion is made downstream by `corroborated_hits`, which requires CH's own
        person-company link. See the namesake note in the module docstring.
        """
        query = urllib.parse.urlencode({"q": f"{forename} {surname}", "items_per_page": 40})
        search = self.get(f"/search/officers?{query}")
        appointments: list[dict[str, Any]] = []
        for item in search.get("items") or []:
            dob = item.get("date_of_birth") or {}
            if dob.get("year") != year or dob.get("month") != month:
                continue
            title = (item.get("title") or "").upper()
            if surname.upper() not in title or forename.upper() not in title:
                continue
            detail = self.get((item.get("links") or {}).get("self") or "")
            for appointment in detail.get("items") or []:
                company = appointment.get("appointed_to") or {}
                appointments.append(
                    {
                        "company_name": company.get("company_name"),
                        "company_number": company.get("company_number"),
                        "company_status": company.get("company_status"),
                        "company_name_aliases": (
                            self.company_name_aliases(
                                company.get("company_number"), company.get("company_name")
                            )
                            if company.get("company_status") == "active"
                            else [company.get("company_name")]
                        ),
                        "officer_role": appointment.get("officer_role"),
                        "appointed_on": appointment.get("appointed_on"),
                        "resigned_on": appointment.get("resigned_on"),
                    }
                )
        return appointments

    def company_name_aliases(self, number: str | None, current: str | None) -> list[str]:
        """Current name plus every previous name, so a rename cannot break the match.

        Only fetched for still-active companies: dissolved and liquidated ones are
        discarded by `corroborated_hits` anyway, and skipping them avoids a profile
        call per dead shell (the dominant cost, since most hits are dissolved).
        """
        if not number:
            return [current] if current else []
        profile = self.get(f"/company/{number}")
        if "_error" in profile:
            return [current] if current else []
        names = [profile.get("company_name") or current]
        names += [p.get("name") for p in (profile.get("previous_company_names") or [])]
        return [n for n in names if n]


def run(client: CompaniesHouse, seeds: tuple[str, ...], per_seed: int, today: date) -> dict:
    people = client.sample_disqualified(seeds, per_seed)
    in_force: list[tuple[str, dict, dict]] = []
    undated = 0
    no_dob = 0

    for link in people:
        record = client.get(link)
        if "_error" in record:
            continue
        for disqualification in record.get("disqualifications") or []:
            if not ban_in_force(disqualification, today):
                undated += 1
                continue
            if not record.get("date_of_birth"):
                # Unmatchable under the identifier-only rule; counted, never name-matched.
                no_dob += 1
                break
            in_force.append((link, record, disqualification))
            break

    findings: list[Finding] = []
    for _link, record, disqualification in in_force:
        dob = record["date_of_birth"]
        appointments = client.appointments_for(
            record.get("forename") or "",
            record.get("surname") or "",
            int(dob[:4]),
            int(dob[5:7]),
        )
        for hit in corroborated_hits(disqualification, appointments):
            findings.append(
                Finding(
                    person_number=record.get("person_number"),
                    surname=record.get("surname"),
                    forename=record.get("forename"),
                    date_of_birth=dob,
                    cdda_section=(disqualification.get("reason") or {}).get("section"),
                    disqualified_from=disqualification.get("disqualified_from"),
                    disqualified_until=disqualification.get("disqualified_until"),
                    has_permission_to_act=bool(disqualification.get("permissions_to_act")),
                    company_name=hit.get("company_name"),
                    company_number=hit.get("company_number"),
                    officer_role=hit.get("officer_role"),
                    appointed_on=hit.get("appointed_on"),
                )
            )

    return {
        "measured_on": today.isoformat(),
        "sampling": "convenience sample by surname fragment; NOT a population estimate",
        "seeds": list(seeds),
        "persons_sampled": len(people),
        "persons_with_ban_in_force": len(in_force),
        "persons_unmatchable_no_dob": no_dob,
        "competition_section_count": sum(
            1
            for _l, _r, d in in_force
            if (d.get("reason") or {}).get("section") == COMPETITION_SECTION
        ),
        "findings": [asdict(f) for f in findings],
        "finding_count": len(findings),
        "interpretation": (
            "Each finding is an inconsistency between two Companies House registers. "
            "It is NOT evidence that any person acted in breach of a disqualification: "
            "a register records appointment, not conduct, and CDDA s.17 leave may exist "
            "without appearing in permissions_to_act."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", nargs="+", default=list(DEFAULT_SEEDS))
    parser.add_argument("--per-seed", type=int, default=20)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    if not api_key:
        raise SystemExit("COMPANIES_HOUSE_API_KEY is not set")

    result = run(CompaniesHouse(api_key), tuple(args.seeds), args.per_seed, date.today())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2))

    print(f"sampled {result['persons_sampled']} persons")
    print(f"  ban in force: {result['persons_with_ban_in_force']}")
    print(f"  unmatchable (no DOB): {result['persons_unmatchable_no_dob']}")
    print(f"  s.9B (competition): {result['competition_section_count']}")
    print(f"  CH-corroborated register inconsistencies: {result['finding_count']}")
    print(f"written to {args.output}")


if __name__ == "__main__":
    main()
