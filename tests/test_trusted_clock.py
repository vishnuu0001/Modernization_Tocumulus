from datetime import datetime, timezone

import pytest

from services.trusted_clock import TrustedClock, TrustedTimeError


def _at(second):
    return datetime(2026, 11, 1, 12, 0, second, tzinfo=timezone.utc)


def test_trusted_clock_uses_quorum_and_ignores_failed_source():
    def unavailable():
        raise OSError("offline")

    clock = TrustedClock(sources=[
        ("one", lambda: _at(1)), ("two", lambda: _at(2)), ("failed", unavailable),
    ], cache_seconds=0)
    assert clock.now().second in {1, 2}
    assert clock.last_sources == ["one", "two"]


def test_trusted_clock_fails_closed_without_two_sources():
    clock = TrustedClock(sources=[("one", lambda: _at(1))], cache_seconds=0)
    with pytest.raises(TrustedTimeError, match="quorum unavailable"):
        clock.now()


def test_trusted_clock_fails_closed_when_sources_disagree():
    clock = TrustedClock(sources=[
        ("one", lambda: _at(1)),
        ("two", lambda: datetime(2030, 1, 1, tzinfo=timezone.utc)),
    ], cache_seconds=0, max_spread_seconds=30)
    with pytest.raises(TrustedTimeError, match="disagree"):
        clock.now()
