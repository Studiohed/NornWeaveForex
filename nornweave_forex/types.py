"""Value objects shared by the strategy, the backtester and the live executor."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Iterable, Sequence

import numpy as np

__all__ = [
    "Candle",
    "CandleArrays",
    "Direction",
    "DowTrend",
    "ExitReason",
    "Zone",
    "Fill",
    "Trade",
    "TradeTick",
]


class Direction(IntEnum):
    """Position direction. The integer value doubles as the sign of the trade."""

    FLAT = 0
    LONG = 1
    SHORT = -1

    @property
    def sign(self) -> int:
        return int(self)

    @property
    def opposite(self) -> "Direction":
        return Direction(-int(self))

    @property
    def label(self) -> str:
        return {0: "flat", 1: "long", -1: "short"}[int(self)]


class DowTrend(Enum):
    """Result of the Dow-theory structure read."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"

    @property
    def direction(self) -> Direction:
        if self is DowTrend.BULLISH:
            return Direction.LONG
        if self is DowTrend.BEARISH:
            return Direction.SHORT
        return Direction.FLAT


class Zone(Enum):
    """Daily-Fibonacci retracement zone of the current daily swing leg."""

    SHALLOW = "shallow"  # retracement < 0.382 -> entry allowed, TP1 pulled to the 0.382 level
    MID = "mid"  # 0.382 .. 0.618 -> "landing zone", entry forbidden
    DEEP = "deep"  # > 0.618 -> possible reversal area, entry allowed with standard targets
    UNKNOWN = "unknown"  # not enough daily history to decide


class ExitReason(Enum):
    STOP_LOSS = "stop_loss"
    BREAK_EVEN_STOP = "break_even_stop"
    TAKE_PROFIT_1 = "take_profit_1"
    TAKE_PROFIT_2 = "take_profit_2"
    TAKE_PROFIT_3 = "take_profit_3"
    TREND_REVERSAL = "trend_reversal"
    FINAL = "final"
    END_OF_DATA = "end_of_data"


@dataclass(frozen=True, slots=True)
class Candle:
    """A single closed OHLCV bar. ``ts`` is the bar's *open* time in ms UTC."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class TradeTick:
    """One public trade, as Bybit's ``publicTrade`` stream or ``recent-trade`` reports it.

    ``side`` is the **taker** side, which is what makes a footprint delta possible:
    aggressive buying lifts the offer, aggressive selling hits the bid.
    """

    ts: int  # fill time, ms UTC
    price: float
    size: float  # base-asset quantity
    side: str  # "Buy" | "Sell"
    is_block_trade: bool = False
    is_rpi: bool = False

    @property
    def signed_size(self) -> float:
        """Positive when the taker bought, negative when the taker sold."""
        if self.side.lower() == "buy":
            return self.size
        if self.side.lower() == "sell":
            return -self.size
        raise ValueError(f"unknown taker side {self.side!r}")


@dataclass(slots=True)
class CandleArrays:
    """Column-oriented OHLCV store, ordered oldest -> newest, all arrays equal length."""

    ts: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray

    def __post_init__(self) -> None:
        n = int(self.ts.shape[0])
        for name in ("open", "high", "low", "close", "volume"):
            arr = getattr(self, name)
            if arr.shape[0] != n:
                raise ValueError(
                    f"CandleArrays field {name!r} has length {arr.shape[0]}, expected {n}"
                )

    def __len__(self) -> int:
        return int(self.ts.shape[0])

    @classmethod
    def from_candles(cls, candles: Iterable[Candle]) -> "CandleArrays":
        items = list(candles)
        return cls(
            ts=np.asarray([c.ts for c in items], dtype=np.int64),
            open=np.asarray([c.open for c in items], dtype=np.float64),
            high=np.asarray([c.high for c in items], dtype=np.float64),
            low=np.asarray([c.low for c in items], dtype=np.float64),
            close=np.asarray([c.close for c in items], dtype=np.float64),
            volume=np.asarray([c.volume for c in items], dtype=np.float64),
        )

    def to_candles(self) -> list[Candle]:
        return [
            Candle(
                ts=int(self.ts[i]),
                open=float(self.open[i]),
                high=float(self.high[i]),
                low=float(self.low[i]),
                close=float(self.close[i]),
                volume=float(self.volume[i]),
            )
            for i in range(len(self))
        ]

    def slice(self, start: int, stop: int | None = None) -> "CandleArrays":
        sl = slice(start, stop)
        return CandleArrays(
            ts=self.ts[sl],
            open=self.open[sl],
            high=self.high[sl],
            low=self.low[sl],
            close=self.close[sl],
            volume=self.volume[sl],
        )

    def validate(self) -> list[str]:
        """Return a list of human readable data problems (empty list == clean)."""
        problems: list[str] = []
        n = len(self)
        if n == 0:
            return ["series is empty"]
        if not np.all(np.diff(self.ts) > 0):
            bad = int(np.argmin(np.diff(self.ts))) + 1
            problems.append(f"timestamps are not strictly increasing (first offender at index {bad})")
        if not np.all(np.isfinite(self.high)) or not np.all(np.isfinite(self.low)):
            problems.append("non-finite high/low values present")
        bad_hl = np.where(self.high < self.low)[0]
        if bad_hl.size:
            problems.append(f"high < low on {bad_hl.size} bar(s), first at index {int(bad_hl[0])}")
        outside = np.where((self.close > self.high) | (self.close < self.low))[0]
        if outside.size:
            problems.append(
                f"close outside [low, high] on {outside.size} bar(s), first at index {int(outside[0])}"
            )
        return problems


@dataclass(frozen=True, slots=True)
class Fill:
    """A single execution against a position."""

    ts: int
    bar_index: int
    price: float
    qty: float
    fee: float
    reason: ExitReason
    pnl: float = 0.0


@dataclass(slots=True)
class Trade:
    """A complete position lifecycle: one entry and one or more exits."""

    direction: Direction
    entry_bar: int
    entry_ts: int
    entry_price: float
    initial_qty: float
    initial_stop: float
    initial_risk_per_unit: float
    tp_prices: list[float] = field(default_factory=list)
    tp1_overridden: bool = False
    fills: list[Fill] = field(default_factory=list)
    fees: float = 0.0
    funding: float = 0.0
    gross_pnl: float = 0.0
    exit_bar: int = -1
    exit_ts: int = -1
    max_favorable_excursion: float = 0.0
    max_adverse_excursion: float = 0.0

    @property
    def qty(self) -> float:
        return self.initial_qty

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees - self.funding

    @property
    def r_multiple(self) -> float:
        """Net PnL expressed in units of the risk taken at entry."""
        risk_amount = self.initial_risk_per_unit * self.initial_qty
        if risk_amount <= 0:
            return 0.0
        return self.net_pnl / risk_amount

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0

    @property
    def bars_held(self) -> int:
        if self.exit_bar < 0:
            return 0
        return self.exit_bar - self.entry_bar

    @property
    def exit_reasons(self) -> list[str]:
        return [f.reason.value for f in self.fills if f.reason is not ExitReason.FINAL]


def derive_exit_reason(fills: Sequence[Fill]) -> ExitReason:
    """Condense the fills of a position into a single reason for reporting."""
    if not fills:
        return ExitReason.FINAL
    terminal = fills[-1].reason
    if terminal in (ExitReason.STOP_LOSS, ExitReason.BREAK_EVEN_STOP, ExitReason.TREND_REVERSAL):
        return terminal
    return terminal
