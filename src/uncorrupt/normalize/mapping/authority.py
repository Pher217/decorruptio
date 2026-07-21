"""Declares which store is authoritative for which field (OCDS vs FtM).

OCDS = analytical/indicator store; FtM = ER/graph store. The mapping is one-way
and lossy; this module documents authority so reconciliation tests can assert it.
"""

from __future__ import annotations

# field -> authoritative store ("ocds" | "ftm")
AUTHORITY: dict[str, str] = {
    "contract.value": "ocds",
    "contract.period": "ocds",
    "company.identity": "ftm",
    "company.ownership": "ftm",
}
