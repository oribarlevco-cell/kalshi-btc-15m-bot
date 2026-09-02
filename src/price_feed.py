from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

COINBASE_BTC_TICKER_URL = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"

# Keep roughly 15 minutes of samples at a typical ~20s poll interval.
DEFAULT_MAX_SAMPLES = 60


@dataclass(frozen=True)
class PriceSample:
    ts: datetime
    price: float


class PriceFeed:
    """Rolling window of BTC-USD spot price samples, used to approximate the
    short-term volatility Kalshi's KXBTC15M markets actually settle on
    (a 60s average of CF Benchmarks' BRTI, which isn't freely available)."""

    def __init__(self, url: str = COINBASE_BTC_TICKER_URL, max_samples: int = DEFAULT_MAX_SAMPLES):
        self._url = url
        self._samples: deque[PriceSample] = deque(maxlen=max_samples)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(initial=1, max=10), reraise=True)
    def fetch_and_record(self, session: requests.Session | None = None) -> PriceSample:
        session = session or requests
        response = session.get(self._url, timeout=10)
        response.raise_for_status()
        payload = response.json()
        sample = PriceSample(ts=datetime.now(timezone.utc), price=float(payload["price"]))
        self._samples.append(sample)
        return sample

    def latest_price(self) -> float | None:
        return self._samples[-1].price if self._samples else None

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def volatility_per_second(self) -> float | None:
        """Stdev of log-returns between consecutive samples, divided by the
        average time between them (i.e. volatility per second of BTC price)."""
        if len(self._samples) < 2:
            return None

        log_returns = []
        dts = []
        prev = self._samples[0]
        for sample in list(self._samples)[1:]:
            dt = (sample.ts - prev.ts).total_seconds()
            if dt > 0 and prev.price > 0 and sample.price > 0:
                log_returns.append(math.log(sample.price / prev.price))
                dts.append(dt)
            prev = sample

        if len(log_returns) < 2:
            return None

        mean = sum(log_returns) / len(log_returns)
        variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
        stdev = math.sqrt(variance)
        avg_dt = sum(dts) / len(dts)
        if avg_dt <= 0:
            return None

        return stdev / math.sqrt(avg_dt)
