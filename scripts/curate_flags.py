"""Curate top-N flags for blind journalist review (ADR-002 D4/D5).

Ranks the raw flag export from the kill experiment by:
1. Indicator diversity — ensure all fired indicators are represented
2. Jurisdiction diversity — ensure all three countries are represented
3. Value-at-stake — higher contract value = more newsworthy
4. Explanation specificity — longer, more detailed explanations rank higher

Outputs:
- experiments/flags_curated.json — the top-N flags with selection metadata
- experiments/blind_review_dossier.md — human-readable dossier for journalists

Usage:
    uv run python scripts/curate_flags.py [--input flags.json] [--top 10]
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path

INDICATOR_LABELS: dict[str, str] = {
    "i001_single_bidder": "Single-bidder award",
    "i002_short_bid_window": "Short bid window",
    "i003_repeat_winner_share": "Repeat-winner concentration",
    "i004_price_vs_estimate": "Price above estimate",
    "i005_direct_award_share": "Direct award share",
}

JURISDICTION_LABELS: dict[str, str] = {
    "UA": "Ukraine (ProZorro)",
    "GB": "United Kingdom (Contracts Finder)",
    "CO": "Colombia (SECOP II)",
}


def _score_flag(
    flag: dict, indicator_rarity: dict[str, float], jur_rarity: dict[str, float]
) -> float:
    """Score a flag for curation. Higher = more newsworthy."""
    jur = flag["evidence"][0]["jurisdiction"] if flag["evidence"] else "?"
    ind = flag["indicator_id"]

    # Rarity bonus: underrepresented indicators/jurisdictions get boosted
    rarity_score = indicator_rarity.get(ind, 0) + jur_rarity.get(jur, 0)

    # Value-at-stake: log-scale (1M → ~14, 100K → ~12, 1K → ~7)
    value = flag.get("tender_value")
    value_score = math.log10(value) if value and value > 0 else 0

    # Explanation specificity: longer = more context for a journalist
    expl_score = min(len(flag.get("explanation", "")) / 200.0, 2.0)

    # Weighted sum
    return rarity_score * 3.0 + value_score * 2.0 + expl_score * 1.0


def curate(flags: list[dict], top_n: int = 10) -> list[dict]:
    """Select the top-N flags maximizing indicator + jurisdiction diversity.

    Uses a quota-based approach:
    - At least 1 flag per fired indicator (if enough flags exist)
    - At least 1 flag per jurisdiction (if enough flags exist)
    - No single indicator gets more than 40% of the slots
    - No single jurisdiction gets more than 50% of the slots
    - Remaining slots filled by value-at-stake
    """
    ind_counts = Counter(f["indicator_id"] for f in flags)
    jur_counts = Counter(f["evidence"][0]["jurisdiction"] if f["evidence"] else "?" for f in flags)
    total = len(flags)

    indicator_rarity = {k: 1.0 - (v / total) for k, v in ind_counts.items()}
    jur_rarity = {k: 1.0 - (v / total) for k, v in jur_counts.items()}

    scored = []
    for flag in flags:
        score = _score_flag(flag, indicator_rarity, jur_rarity)
        scored.append((score, flag))
    scored.sort(key=lambda x: x[0], reverse=True)

    max_per_indicator = max(1, int(top_n * 0.4))
    max_per_jurisdiction = max(1, int(top_n * 0.5))

    selected: list[dict] = []
    ind_quota: Counter = Counter()
    jur_quota: Counter = Counter()

    # Pass 1: best flag per (indicator, jurisdiction) combo
    seen_combos: set[tuple[str, str]] = set()
    for score, flag in scored:
        jur = flag["evidence"][0]["jurisdiction"] if flag["evidence"] else "?"
        ind = flag["indicator_id"]
        combo = (ind, jur)
        if combo not in seen_combos:
            seen_combos.add(combo)
            flag_copy = dict(flag)
            flag_copy["_score"] = round(score, 3)
            flag_copy["_selection_reason"] = (
                f"Best example of {INDICATOR_LABELS.get(ind, ind)} "
                f"in {JURISDICTION_LABELS.get(jur, jur)}"
            )
            selected.append(flag_copy)
            ind_quota[ind] += 1
            jur_quota[jur] += 1
            if len(selected) >= top_n:
                break

    # Pass 2: fill remaining slots respecting diversity caps
    if len(selected) < top_n:
        for score, flag in scored:
            if any(s["subject_ref"] == flag["subject_ref"] for s in selected):
                continue
            jur = flag["evidence"][0]["jurisdiction"] if flag["evidence"] else "?"
            ind = flag["indicator_id"]
            if ind_quota[ind] >= max_per_indicator:
                continue
            if jur_quota[jur] >= max_per_jurisdiction:
                continue
            flag_copy = dict(flag)
            flag_copy["_score"] = round(score, 3)
            flag_copy["_selection_reason"] = "High value-at-stake"
            selected.append(flag_copy)
            ind_quota[ind] += 1
            jur_quota[jur] += 1
            if len(selected) >= top_n:
                break

    # Pass 3: relax caps if still under top_n
    if len(selected) < top_n:
        for score, flag in scored:
            if any(s["subject_ref"] == flag["subject_ref"] for s in selected):
                continue
            flag_copy = dict(flag)
            flag_copy["_score"] = round(score, 3)
            flag_copy["_selection_reason"] = "High score (relaxed caps)"
            selected.append(flag_copy)
            if len(selected) >= top_n:
                break

    return selected[:top_n]


def render_dossier(selected: list[dict], meta: dict) -> str:
    """Render a blind-review-ready markdown dossier."""
    lines: list[str] = []
    lines.append("# Decorruptio — Blind Review Dossier")
    lines.append("")
    lines.append(f"**Date:** {meta['experiment_date']}")
    lines.append(
        f"**Sample:** {meta['sample_size_per_source']} records per source "
        f"({meta['total_flags']} raw flags → top {len(selected)} curated)"
    )
    lines.append("")
    lines.append("## How to review")
    lines.append("")
    lines.append(
        "Each flag below is an automated anomaly signal from public procurement "
        "data. For each flag, answer:"
    )
    lines.append("")
    lines.append("1. **Is this novel?** (Would you not have found this otherwise?)")
    lines.append("2. **Is this defensible?** (Is the evidence reproducible from the source?)")
    lines.append("3. **Would you chase this?** (Is it worth your time to investigate?)")
    lines.append("")
    lines.append("---")
    lines.append("")

    for i, flag in enumerate(selected, 1):
        jur = flag["evidence"][0]["jurisdiction"] if flag["evidence"] else "?"
        ind = flag["indicator_id"]
        ind_label = INDICATOR_LABELS.get(ind, ind)
        jur_label = JURISDICTION_LABELS.get(jur, jur)

        lines.append(f"## Flag {i}: {ind_label} — {jur_label}")
        lines.append("")

        title = flag.get("tender_title") or "(no title)"
        buyer = flag.get("buyer_name") or "(unknown buyer)"
        value = flag.get("tender_value")
        currency = flag.get("tender_currency") or ""
        method = flag.get("procurement_method") or "—"

        lines.append(f"**Tender:** {title}")
        lines.append(f"**Buyer:** {buyer}")
        if value:
            lines.append(f"**Contract value:** {value:,.0f} {currency}")
        else:
            lines.append("**Contract value:** not available")
        lines.append(f"**Procurement method:** {method}")
        lines.append("")

        lines.append("### What the anomaly detector found")
        lines.append("")
        lines.append(f"> {flag['explanation']}")
        lines.append("")

        lines.append("### Evidence (reproducible from public source)")
        lines.append("")
        for ev in flag["evidence"]:
            lines.append(f"- **Source:** {ev['source_id']}")
            lines.append(f"- **URL:** {ev['source_url']}")
            lines.append(f"- **License:** {ev['license']}")
            lines.append(f"- **Jurisdiction:** {ev['jurisdiction']}")
        lines.append("")

        lines.append("### Technical metadata")
        lines.append("")
        lines.append(f"- Indicator: `{ind}`")
        lines.append(f"- Snapshot: {flag['stamp']['data_snapshot']}")
        lines.append(f"- Code version: {flag['stamp']['code_version']}")
        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("## Reviewer response")
    lines.append("")
    lines.append("| Flag # | Novel? | Defensible? | Would chase? | Notes |")
    lines.append("|--------|--------|-------------|--------------|-------|")
    for i in range(1, len(selected) + 1):
        lines.append(f"| {i} | ☐ Y ☐ N | ☐ Y ☐ N | ☐ Y ☐ N | |")
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Curate top flags for blind journalist review.")
    parser.add_argument(
        "--input",
        default="experiments/flags_raw.json",
        help="Input file (raw flags from kill experiment)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="Number of flags to select",
    )
    parser.add_argument(
        "--output-json",
        default="experiments/flags_curated.json",
        help="Output JSON file",
    )
    parser.add_argument(
        "--output-md",
        default="experiments/blind_review_dossier.md",
        help="Output markdown dossier file",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: {input_path} not found. Run kill_experiment.py first.")
        raise SystemExit(1)

    with open(input_path) as f:
        data = json.load(f)

    flags = data["flags"]
    selected = curate(flags, top_n=args.top)

    # Write curated JSON
    output = {
        "experiment_date": data["experiment_date"],
        "sample_size_per_source": data["sample_size_per_source"],
        "total_raw_flags": data["total_flags"],
        "curated_count": len(selected),
        "selection_criteria": (
            "Greedy diversity maximization: indicator diversity × "
            "jurisdiction diversity × value-at-stake × explanation specificity"
        ),
        "flags": selected,
    }
    Path(args.output_json).parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_json, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"Curated {len(selected)} flags → {args.output_json}")

    # Write blind-review dossier
    meta = {
        "experiment_date": data["experiment_date"],
        "sample_size_per_source": data["sample_size_per_source"],
        "total_flags": data["total_flags"],
    }
    dossier = render_dossier(selected, meta)
    with open(args.output_md, "w") as f:
        f.write(dossier)
    print(f"Blind-review dossier → {args.output_md}")

    # Summary
    ind_dist = Counter(f["indicator_id"] for f in selected)
    jur_dist = Counter(f["evidence"][0]["jurisdiction"] for f in selected)
    print("\nCurated flag distribution:")
    print(f"  Indicators: {dict(ind_dist)}")
    print(f"  Jurisdictions: {dict(jur_dist)}")


if __name__ == "__main__":
    main()
