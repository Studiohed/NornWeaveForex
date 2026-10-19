"""Symbol screener for Forex majors.

The crypto version fetched a market list and ranked by volume. On Forex there is
no equivalent public ranking — majors are majors because the market says so.
So this screener takes the fixed list from ``portfolio.symbols`` and filters it
down to the pairs whose *current* spread is tight enough to trade.

A pair whose typical spread exceeds ``max_spread_points`` is excluded: paying
more than a couple of pips of spread on entry and again on exit is a drag the
strategy cannot recover from on H2 bars.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from nornweave_forex.broker.mt5_client import MT5Client, MT5ClientError

__all__ = ["ScreenerResult", "screen_symbols"]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScreenerResult:
    symbol: str
    spread_points: float
    digits: int
    tradeable: bool
    reason: str | None = None


def screen_symbols(
    client: MT5Client,
    symbols: tuple[str, ...],
    *,
    max_spread_points: float = 30.0,
) -> list[ScreenerResult]:
    """One round of screening: fetch symbol info and current spread per pair."""
    results: list[ScreenerResult] = []
    for symbol in symbols:
        try:
            info = client.symbol_info(symbol)
            tick = client.symbol_tick(symbol)
        except MT5ClientError as exc:
            log.warning("screener: cannot inspect %s: %s", symbol, exc)
            results.append(ScreenerResult(
                symbol=symbol, spread_points=float("inf"), digits=0,
                tradeable=False, reason=str(exc),
            ))
            continue

        digits = int(info.get("digits", 0))
        point = float(info.get("point", 0.0))
        bid = float(tick.get("bid", 0.0))
        ask = float(tick.get("ask", 0.0))
        spread = (ask - bid) / point if point > 0 else float("inf")

        tradeable = spread <= max_spread_points
        reason = None if tradeable else f"spread {spread:.1f} > cap {max_spread_points}"
        results.append(ScreenerResult(
            symbol=symbol, spread_points=spread, digits=digits,
            tradeable=tradeable, reason=reason,
        ))
    return results
