"""Uncorrupt error hierarchy."""


class UncorruptError(Exception):
    """Base class for all Uncorrupt errors."""


class RegisterError(UncorruptError):
    """A source/locale register entry is missing or invalid."""


class TierViolation(UncorruptError):
    """An attempt to move data to a tier its classification forbids (ADR-000 G2)."""


class RedistributionViolation(UncorruptError):
    """An attempt to bulk-export data whose source license forbids it (ADR-001 D5-bis)."""


class VaultError(UncorruptError):
    """The tokenized-ID vault was misused or misconfigured (ADR-000 G3)."""
