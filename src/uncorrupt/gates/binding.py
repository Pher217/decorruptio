"""Freeze-state binding for gate artifacts (spec A2.4.5, ADR-008).

`run_gold_benchmark.py` (owned elsewhere, read-only from here) already
defines the binding contract a gate report is checked against --
`GateBinding(code_commit, graph_hash, manifest_hash)` and
`compute_graph_hash()`/`current_code_commit()`/`compute_manifest_hash()`.
This module does NOT reimplement any of those: the CLIs in `scripts/
measure_coverage_gate.py` / `scripts/measure_stratum_gates.py` import them
directly from `scripts.run_gold_benchmark` so the values this package writes
are guaranteed identical to what the scorer will independently recompute and
compare against -- two independent hash implementations that must agree bit
for bit is a drift risk neither side can afford.

THE ATTESTATION-INCLUSIVE HASH -- the gap this module exists to close.
`compute_graph_hash()` hashes only `(edge_type, source_entity_id,
target_entity_id, valid_from)` tuples -- **edges**, never attestations. An
ingest that adds ONLY `Attestation` rows to edges that already exist changes
nothing that hash can see. This is not a hypothetical: the Lords Wayback
snapshot ingest (spec v2.9) added roughly 6,000 attestations against
already-existing `declared_interest` edges and created zero new edges. A
coverage or stratum gate measured BEFORE that ingest would still "bind", by
`GateBinding.matches()`'s own three-field check, to the graph state AFTER
it -- silently authorising the scorer to treat evidence the gate never
actually measured as already covered by a passing gate.

This package cannot change `GateBinding.matches()` (out of scope). Instead,
every gate artifact this package writes ALSO records
`attestation_inclusive_hash` (`compute_attestation_inclusive_hash()` below),
and `GateFreezeState.matches_recorded()` -- used by this package's own
`certificate` module, never by `run_gold_benchmark.py` -- checks all four
fields, not three. A caller that only trusts `run_gold_benchmark.py`'s own
binding check will still miss attestation-only drift; a caller that also
checks `attestation_inclusive_hash` will not. **Flagged, not silently
worked around**: `run_gold_benchmark.py`'s own binding remains blind to this
class of drift until that file is amended by whoever owns it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from uncorrupt.graph.models import Attestation


def compute_attestation_inclusive_hash() -> str:
    """Canonical, order-independent hash over every Edge AND every Attestation.

    Same discipline as `run_gold_benchmark.compute_graph_hash` (sha256 over
    sorted tuples, so insertion order never changes the hash) extended to
    cover the evidence layer that function cannot see. Attestation rows are
    hashed on `(edge_id, source_name, source_reference, observed_at,
    snapshot_ref)` -- the fields that identify *which* evidence exists and
    what it claims to observe, deliberately excluding `match_confidence` /
    `match_method` (resolution-quality metadata, not new evidence) and
    `created_at` (a write-time artifact, not part of what was observed).

    A drift detector, not a cryptographic commitment -- same caveat as
    `compute_graph_hash`.
    """
    from scripts.run_gold_benchmark import compute_graph_hash

    edge_component = compute_graph_hash()

    attestation_rows = sorted(
        Attestation.objects.values_list(
            "edge_id", "source_name", "source_reference", "observed_at", "snapshot_ref"
        )
    )
    h = hashlib.sha256()
    h.update(edge_component.encode("utf-8"))
    h.update(b"\n")
    for row in attestation_rows:
        h.update("|".join(str(x) for x in row).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


@dataclass(frozen=True)
class GateFreezeState:
    """The full freeze-state a gate artifact is bound to (spec A2.4.5).

    `code_commit`/`graph_hash`/`manifest_hash` are exactly the three fields
    `run_gold_benchmark.GateBinding` checks -- same names, so
    `to_binding_dict()` round-trips through that class unchanged.
    `attestation_inclusive_hash` is the extra field this package's own
    `certificate` module additionally verifies (see module docstring).
    """

    code_commit: str
    graph_hash: str
    attestation_inclusive_hash: str
    manifest_hash: str
    measured_at: str

    def to_binding_dict(self) -> dict[str, str]:
        """Fields to merge into any gate JSON this package writes.

        Field names match `run_gold_benchmark.GateBinding.matches()`'s own
        `data.get(...)` calls exactly (`code_commit`, `graph_hash`,
        `manifest_hash`) plus the extra `attestation_inclusive_hash` and a
        human-auditable `measured_at` timestamp neither `GateBinding` nor
        the scorer reads, but every write-back trigger in this project
        requires ("bind... UTC timestamp").
        """
        return {
            "code_commit": self.code_commit,
            "graph_hash": self.graph_hash,
            "attestation_inclusive_hash": self.attestation_inclusive_hash,
            "manifest_hash": self.manifest_hash,
            "measured_at": self.measured_at,
        }

    def matches_recorded(self, data: dict) -> bool:
        """Stricter than `run_gold_benchmark.GateBinding.matches()`: also
        requires `attestation_inclusive_hash` to match, so an attestation-
        only ingest since this state was recorded is caught even though the
        scorer's own three-field check would miss it (see module docstring).
        """
        return (
            data.get("code_commit") == self.code_commit
            and data.get("graph_hash") == self.graph_hash
            and data.get("attestation_inclusive_hash") == self.attestation_inclusive_hash
            and data.get("manifest_hash") == self.manifest_hash
        )


def utc_now_iso() -> str:
    """UTC timestamp in the format every frozen-state record in this
    package uses -- spec A2.4.5 requires one on every frozen state."""
    return datetime.now(UTC).isoformat()
