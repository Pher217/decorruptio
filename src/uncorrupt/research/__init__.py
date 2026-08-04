"""Research-quality tooling: sourcing verification for gold-manifest curation.

Distinct from `uncorrupt.connectors` / `uncorrupt.graph` ingest modules --
this package never writes graph or staging data. It checks whether a claim
already curated into a manifest (a `label_source_url` / `label_source_quote`
pair) is actually supported by the document it cites.
"""
