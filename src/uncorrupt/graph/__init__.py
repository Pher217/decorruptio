"""Relationship-recovery graph — FtM-shaped entities, edges, aliases, provenance.

Models a supplier↔referrer relationship graph in PostgreSQL. Every edge carries
valid_from / valid_to and a source citation. Resolution by registry ID, never
by person name string (ADR-004 D2). Company-level + public-function officials
only — no PSC, no private-individual profiling (ADR-004 D1).
"""
