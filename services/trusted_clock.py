"""Fail-closed UTC authority independent of the host wall clock."""
from __future__ import annotations

import statistics
import threading
import time
from datetime import datetime, timedelta, UTC
from email.utils import parsedate_to_datetime
from collections.abc import Callable

import httpx


class TrustedTimeError(RuntimeError):
    pass


def _world_time_api(client: httpx.Client) -> datetime:
    data = client.get("https://gateway.timeapi.world/timezone/UTC").raise_for_status().json()
    return datetime.fromisoformat(str(data["utc_datetime"]))


def _timeapi_io(client: httpx.Client) -> datetime:
    data = client.get("https://timeapi.io/api/Time/current/zone?timeZone=UTC").raise_for_status().json()
    value = data.get("dateTime") or data.get("dateTimeString")
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC)


def _https_date(client: httpx.Client, url: str) -> datetime:
    response = client.get(url)
    response.raise_for_status()
    value = response.headers.get("date")
    if not value:
        raise ValueError("HTTPS response has no Date header")
    return parsedate_to_datetime(value).astimezone(UTC)


class TrustedClock:
    """Use a quorum of TLS-protected internet clocks and monotonic caching."""

    def __init__(self, sources: list[tuple[str, Callable[[], datetime]]] | None = None,
                 cache_seconds: int = 60, max_spread_seconds: int = 180):
        self.cache_seconds = cache_seconds
        self.max_spread_seconds = max_spread_seconds
        self._lock = threading.Lock()
        self._trusted_at: datetime | None = None
        self._monotonic_at = 0.0
        self.last_sources: list[str] = []
        if sources is None:
            client = httpx.Client(timeout=5, follow_redirects=True)
            sources = [
                ("timeapi.world", lambda: _world_time_api(client)),
                ("timeapi.io", lambda: _timeapi_io(client)),
                ("cloudflare-date", lambda: _https_date(client, "https://www.cloudflare.com/cdn-cgi/trace")),
                ("google-date", lambda: _https_date(client, "https://www.google.com/generate_204")),
            ]
        self.sources = sources

    def now(self, *, force: bool = False) -> datetime:
        with self._lock:
            monotonic_now = time.monotonic()
            if not force and self._trusted_at is not None and monotonic_now - self._monotonic_at < self.cache_seconds:
                return self._trusted_at + timedelta(seconds=monotonic_now - self._monotonic_at)

            observations: list[tuple[str, datetime]] = []
            for name, source in self.sources:
                try:
                    value = source().astimezone(UTC)
                    observations.append((name, value))
                except Exception:
                    continue
            if len(observations) < 2:
                raise TrustedTimeError("Trusted UTC quorum unavailable; access is denied")
            stamps = [value.timestamp() for _, value in observations]
            median = statistics.median(stamps)
            agreeing = [(name, value) for name, value in observations
                        if abs(value.timestamp() - median) <= self.max_spread_seconds]
            if len(agreeing) < 2:
                raise TrustedTimeError("Trusted UTC sources disagree; access is denied")
            trusted_stamp = statistics.median([value.timestamp() for _, value in agreeing])
            self._trusted_at = datetime.fromtimestamp(trusted_stamp, UTC)
            self._monotonic_at = monotonic_now
            self.last_sources = [name for name, _ in agreeing]
            return self._trusted_at
