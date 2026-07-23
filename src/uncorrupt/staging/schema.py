"""DuckDB schema for the staging layer.

Unified OCDS-flattened schema: one row per tender, one per award, one per bid.
All three sources (ProZorro, SECOP II, Contracts Finder) map into this.
"""

from __future__ import annotations

import duckdb

SCHEMA_SQL = """
-- Tenders (procurement processes)
CREATE TABLE IF NOT EXISTS tenders (
    source_id       VARCHAR NOT NULL,
    tender_id       VARCHAR NOT NULL,
    ocid            VARCHAR,
    title           VARCHAR,
    description     VARCHAR,
    status          VARCHAR,
    procurement_method VARCHAR,
    procurement_method_details VARCHAR,
    award_criteria  VARCHAR,
    currency        VARCHAR,
    value_amount    DOUBLE,
    tender_start    TIMESTAMP,
    tender_end      TIMESTAMP,
    buyer_name      VARCHAR,
    buyer_id_scheme VARCHAR,
    buyer_id        VARCHAR,
    buyer_country   VARCHAR,
    item_count      INTEGER,
    raw_json        JSON,
    fetched_at      TIMESTAMP NOT NULL,
    source_url      VARCHAR NOT NULL,
    PRIMARY KEY (source_id, tender_id)
);

-- Awards (contract awards within a tender)
CREATE TABLE IF NOT EXISTS awards (
    source_id       VARCHAR NOT NULL,
    tender_id       VARCHAR NOT NULL,
    award_id        VARCHAR NOT NULL,
    supplier_name   VARCHAR,
    supplier_id_scheme VARCHAR,
    supplier_id     VARCHAR,
    currency        VARCHAR,
    value_amount    DOUBLE,
    status          VARCHAR,
    award_date      TIMESTAMP,
    raw_json        JSON,
    fetched_at      TIMESTAMP NOT NULL,
    PRIMARY KEY (source_id, tender_id, award_id)
);

-- Bids (proposals submitted for a tender)
CREATE TABLE IF NOT EXISTS bids (
    source_id       VARCHAR NOT NULL,
    tender_id       VARCHAR NOT NULL,
    bid_id          VARCHAR NOT NULL,
    bidder_name     VARCHAR,
    bidder_id       VARCHAR,
    currency        VARCHAR,
    value_amount    DOUBLE,
    status          VARCHAR,
    bid_date        TIMESTAMP,
    raw_json        JSON,
    fetched_at      TIMESTAMP NOT NULL,
    PRIMARY KEY (source_id, tender_id, bid_id)
);
"""


def create_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create all staging tables if they don't exist."""
    conn.execute(SCHEMA_SQL)


def drop_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Drop all staging tables."""
    conn.execute("DROP TABLE IF EXISTS bids")
    conn.execute("DROP TABLE IF EXISTS awards")
    conn.execute("DROP TABLE IF EXISTS tenders")
