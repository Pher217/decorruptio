"""Phase-1 connectors are conformant (register entry exists, discover/fetch present)."""

from uncorrupt.connectors.conformance import check_connector
from uncorrupt.connectors.eu_ted.connector import EuTedConnector
from uncorrupt.connectors.gleif.connector import GleifConnector


def test_eu_ted_conformant():
    assert check_connector(EuTedConnector()) == []


def test_gleif_conformant():
    assert check_connector(GleifConnector()) == []
