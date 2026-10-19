"""MT5-backed feed for H2 candles on the watched major pairs.

Differences from the crypto feed:

* Polls ``copy_rates_from_pos`` every N seconds instead of consuming a WebSocket.
  The MT5 Python API has no subscription channel, so polling is the only option;
  the interval is generous because the bot works on *closed* H2 bars and a
  couple of seconds of latency is irrelevant.
* Returns bars only for pairs whose newest closed bar is strictly newer than
  the last one the runner has seen. Weekend gaps simply produce no new bars.
* A pair that MT5 cannot serve (no history, symbol disabled) is skipped with a
  log line — one dead pair never stalls the other seven.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from nornweave_forex.broker.mt5_client import MT5Client, MT5ClientError
from nornweave_forex.types import Candle

__all__ = ["MT5Feed", "FeedError", "feed_is_stale", "bars_from_mt5"]

log = logging.getLogger(__name__)


class FeedError(RuntimeError):
    """The feed could not deliver a usable bar."""


def bars_from_mt5(rows: list[dict[str, Any]]) -> list[Candle]:
    """Translate the MT5 server dict rows into :class:`Candle` objects."""
    out: list[Candle] = []
    for r in rows:
        out.append(
            Candle(
                ts=int(r["ts"]),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r.get("volume", 0.0)),
            )
        )
    return out


def feed_is_stale(now_ms: int, last_bar_ts: int, interval_minutes: int, *, max_stale_mult: int = 3) -> bool:
    """Whether a feed has gone silent.

    ``last_bar_ts`` is the open time of the newest closed bar we have seen.
    A bar is due ``interval_minutes`` after its open; we allow
    ``max_stale_mult`` intervals before calling it stale. Crypto never sleeps,
    Forex does — this check only fires when a *weekday* bar is overdue.
    """
    if last_bar_ts <= 0:
        return False  # nothing seen yet; cannot be stale
    due_ms = last_bar_ts + interval_minutes * 60_000
    return now_ms > due_ms + max_stale_mult * interval_minutes * 60_000


@dataclass(slots=True)
class MT5Feed:
    """Poll MT5 for newly closed bars on a fixed list of symbols."""

    client: MT5Client
    symbols: tuple[str, ...]
    interval_minutes: int
    #: How many bars to request per poll. The strategy needs several hundred
    # bars of warmup history at startup, so the first poll asks for many.
    lookback: int = 500

    _last_ts: dict[str, int] | None = None

    def _ensure_state(self) -> None:
        if self._last_ts is None:
            self._last_ts = {s: 0 for s in self.symbols}

    def poll_new_bars(self) -> dict[str, list[Candle]]:
        """Newly closed bars per symbol since the previous poll.

        Returns ``{symbol: [Candle, ...]}`` where the list contains every closed
        bar strictly newer than what we last returned for that symbol. The
        rightmost bar from MT5 is *forming* and is always discarded.
        """
        self._ensure_state()
        assert self._last_ts is not None
        out: dict[str, list[Candle]] = {}
        for symbol in self.symbols:
            try:
                rows = self.client.copy_rates_from_pos(
                    symbol, self.interval_minutes, start_pos=0, count=self.lookback
                )
            except MT5ClientError as exc:
                log.warning("feed: %s unavailable: %s", symbol, exc)
                continue
            if not rows:
                continue
            # The rightmost row is the still-forming bar; drop it.
            closed = rows[:-1]
            if not closed:
                continue
            candles = bars_from_mt5(closed)
            last = self._last_ts[symbol]
            new = [c for c in candles if c.ts > last]
            if new:
                self._last_ts[symbol] = int(new[-1].ts)
                out[symbol] = new
        return out

    def mark_seen(self, symbol: str, ts: int) -> None:
        """Record a bar timestamp without needing a poll (used after a crash resume)."""
        self._ensure_state()
        assert self._last_ts is not None
        self._last_ts[symbol] = max(self._last_ts[symbol], int(ts))

    def last_seen(self, symbol: str) -> int:
        self._ensure_state()
        assert self._last_ts is not None
        return self._last_ts[symbol]
