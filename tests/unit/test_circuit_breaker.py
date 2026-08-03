"""Tests for the session-level circuit breaker (`uncorrupt.core.circuit_breaker`).

Verifies the core invariants a long ingest sweep relies on:
- the breaker trips at EXACTLY its threshold, never one failure early or late
- a success resets the consecutive-failure count, so alternating
  success/failure never trips it
- `threshold=1` trips on the very first failure (the boundary case)
- an invalid (non-positive) threshold is rejected up front
"""

import pytest

from uncorrupt.core.circuit_breaker import CircuitBreaker, CircuitOpenError


class TestCircuitBreaker:
    def test_does_not_trip_below_threshold(self):
        """GIVEN a threshold of 3 WHEN only 2 consecutive failures are recorded THEN
        no exception is raised and the circuit is not open."""
        breaker = CircuitBreaker(threshold=3)

        breaker.record_failure()
        breaker.record_failure()

        assert breaker.is_open is False

    def test_trips_at_exactly_the_threshold(self):
        """GIVEN a threshold of 3 WHEN the 3rd consecutive failure is recorded THEN
        CircuitOpenError is raised -- not before, not after."""
        breaker = CircuitBreaker(threshold=3)
        breaker.record_failure()
        breaker.record_failure()

        with pytest.raises(CircuitOpenError):
            breaker.record_failure()

    def test_is_open_true_after_tripping(self):
        """GIVEN a breaker that has just tripped WHEN is_open is checked THEN it is
        True."""
        breaker = CircuitBreaker(threshold=2)
        breaker.record_failure()
        with pytest.raises(CircuitOpenError):
            breaker.record_failure()

        assert breaker.is_open is True

    def test_threshold_of_one_trips_on_first_failure(self):
        """GIVEN threshold=1 (the boundary case) WHEN the first failure is recorded
        THEN it trips immediately."""
        breaker = CircuitBreaker(threshold=1)

        with pytest.raises(CircuitOpenError):
            breaker.record_failure()

    def test_success_resets_the_consecutive_count(self):
        """GIVEN 2 failures then a success WHEN 2 more failures are recorded THEN the
        circuit still has not tripped -- a success resets the streak, so it takes a
        fresh run of `threshold` failures to trip, not a cumulative count."""
        breaker = CircuitBreaker(threshold=3)
        breaker.record_failure()
        breaker.record_failure()

        breaker.record_success()

        breaker.record_failure()
        breaker.record_failure()
        assert breaker.is_open is False

    def test_alternating_success_and_failure_never_trips(self):
        """GIVEN failures that alternate with successes WHEN many are recorded THEN
        the circuit never trips, however many total failures accumulate."""
        breaker = CircuitBreaker(threshold=2)

        for _ in range(50):
            breaker.record_failure()
            breaker.record_success()

        assert breaker.is_open is False

    def test_non_positive_threshold_is_rejected(self):
        """GIVEN threshold=0 WHEN constructing a breaker THEN it raises ValueError up
        front rather than silently never tripping."""
        with pytest.raises(ValueError, match="threshold"):
            CircuitBreaker(threshold=0)

    def test_consecutive_failures_counts_the_current_streak(self):
        """GIVEN 2 recorded failures WHEN consecutive_failures is read THEN it is
        exactly 2 -- observable state for a caller wanting to log progress."""
        breaker = CircuitBreaker(threshold=5)
        breaker.record_failure()
        breaker.record_failure()

        assert breaker.consecutive_failures == 2
