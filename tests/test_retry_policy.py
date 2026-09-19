"""Unit tests for the retry policy: classification and backoff.

This is the logic most easily gotten wrong (which statuses retry), so it is
tested exhaustively and in isolation from I/O and time.
"""

from __future__ import annotations

import pytest

from app.deliverer import Outcome, OutcomeKind
from app.retry import Classification, RetryPolicy


def _http(status: int) -> Outcome:
    return Outcome(OutcomeKind.HTTP_RESPONSE, status_code=status)


policy = RetryPolicy(max_attempts=5, base_backoff_seconds=2.0, max_backoff_seconds=60.0)


@pytest.mark.parametrize("status", [200, 201, 202, 204])
def test_2xx_is_success(status):
    assert policy.classify(_http(status)) is Classification.SUCCESS


@pytest.mark.parametrize("status", [500, 502, 503, 504, 408, 425, 429])
def test_retryable_statuses(status):
    assert policy.classify(_http(status)) is Classification.RETRYABLE


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_terminal_4xx(status):
    assert policy.classify(_http(status)) is Classification.TERMINAL


def test_transport_error_is_retryable():
    outcome = Outcome(OutcomeKind.TRANSPORT_ERROR, detail="timeout")
    assert policy.classify(outcome) is Classification.RETRYABLE


def test_backoff_is_exponential():
    # attempts_made = 1 -> base, 2 -> base*2, 3 -> base*4 ...
    assert policy.backoff_seconds(1) == 2.0
    assert policy.backoff_seconds(2) == 4.0
    assert policy.backoff_seconds(3) == 8.0


def test_backoff_is_capped():
    assert policy.backoff_seconds(20) == 60.0  # would be huge; capped


def test_attempts_left_boundary():
    assert policy.has_attempts_left(4) is True   # 4 < 5
    assert policy.has_attempts_left(5) is False  # limit reached
