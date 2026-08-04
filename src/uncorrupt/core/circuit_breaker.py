"""Session-level circuit breaker for long ingest loops.

Per-request retry (each connector's own `_fetch_page_with_backoff` /
`_fetch_all_officer_pages`) already handles a single flaky request. This is
the layer above it: a sweep over thousands of independent items (e.g.
12,227+ companies) currently has no way to give up when the failure rate
turns systemic (a revoked API key, an outage) -- it grinds through the rest
at the same failure rate, producing no more evidence than the first few
failures already did. This is a tiny, explicit counter + threshold, not a
retry/backoff framework -- that stays exactly where it already is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A sweep this size (see ch_officers.py's coverage-expansion docstring) is
# exactly the case this exists for -- unattended, many independent items,
# no operator watching it fail request-by-request.
DEFAULT_MAX_CONSECUTIVE_FAILURES = 25


class CircuitOpenError(Exception):
    """Raised by `CircuitBreaker.record_failure` once the threshold is reached."""


@dataclass
class CircuitBreaker:
    """Trips after `threshold` CONSECUTIVE failures; any success resets the count.

    Usage: call `record_success()` after each successful item (including a
    cache hit) and `record_failure()` after each failed one; catch
    `CircuitOpenError` around the loop body to abort early with whatever
    results were already gathered.
    """

    threshold: int
    consecutive_failures: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.threshold < 1:
            raise ValueError("threshold must be at least 1")

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.threshold:
            raise CircuitOpenError(
                f"{self.consecutive_failures} consecutive failures reached the "
                f"threshold of {self.threshold} -- aborting the sweep"
            )

    @property
    def is_open(self) -> bool:
        return self.consecutive_failures >= self.threshold
