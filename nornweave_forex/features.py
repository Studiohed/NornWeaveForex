"""The feature vector at every entry decision, so a model can be trained — and watched.

Why this exists before any model does.

Everything the strategy decides is thrown away the moment it decides. The backtest reports
totals, the live runner logs a line, and neither keeps the vector that produced the
decision. That makes three things impossible:

* training anything on the entries, because there is nothing to train on;
* checking that live sees the market the way the backtest did, which is the failure that
  makes a validated strategy behave differently with real money;
* noticing drift later, because there is no record of what "normal" looked like.

So this module records, for EVERY bar the engine evaluates: the indicator values, the
per-check verdicts, the market regime around it, the clock, and — for the entries that
were actually taken — the outcome. Blocked signals are recorded too, and matter just as
much: a model that only ever sees taken entries cannot learn what was avoided, and a
backtest whose blocks drift cannot be compared with live.

Nothing here changes a trading decision. It is a side channel, and it is written to disk
as JSONL so a run of any length appends without rewriting.

The honest ordering, from the analysis that prompted this: record first, train second,
and only wire a model into live trading if it beats taking every entry OUT OF SAMPLE on a
holdout nobody looked at. Recording is worth doing even if no model ever follows.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

__all__ = ["FeatureRecord", "FeatureLog", "build_record", "SCHEMA"]

#: Bumped when the meaning of a field changes, so an old and a new log are never pooled
#: silently. The state file learned this the hard way; the lesson is applied up front.
SCHEMA = 1

#: How many bars back the market-regime measures look. 360 bars of 2H is 30 days.
REGIME_WINDOW = 360


@dataclass(slots=True)
class FeatureRecord:
    """One entry decision, plus its outcome if it became a trade."""

    # -- identity ---------------------------------------------------------- #
    symbol: str
    interval: str
    bar_index: int
    bar_ts: int
    side: str
    allowed: bool
    blockers: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)

    # -- what the engine saw at the close of that bar ---------------------- #
    close: float = 0.0
    ema: float = 0.0
    ema_slope_pct: float = 0.0
    adx: float = 0.0
    adx_threshold: float | None = None
    adx_ratio: float | None = None
    atr: float = 0.0
    atr_pct: float = 0.0
    dow: str = ""
    daily_trend: str = ""
    daily_ema_slope: int = 0
    daily_zone: str = ""
    daily_depth: float | None = None
    gap_to_shallow_pct: float | None = None
    gap_to_deep_pct: float | None = None

    # -- the market regime around it --------------------------------------- #
    atr_ratio: float | None = None
    max_bar_pct: float | None = None
    top3_share_pct: float | None = None
    off_low_pct: float | None = None
    off_high_pct: float | None = None
    range_pos_pct: float | None = None
    trend_efficiency: float | None = None

    # -- clock ------------------------------------------------------------- #
    hour_utc: int = 0
    weekday: int = 0

    # -- the outcome, filled in when the trade closes ---------------------- #
    taken: bool = False
    entry_price: float | None = None
    stop_price: float | None = None
    stop_pct: float | None = None
    qty: float | None = None
    exit_ts: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    net_pnl: float | None = None
    bars_held: int | None = None
    max_favourable: float | None = None
    max_adverse: float | None = None

    @property
    def labelled(self) -> bool:
        """True when this record can be used as a training example."""
        return self.taken and self.net_pnl is not None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _regime(high, low, close, index: int, window: int = REGIME_WINDOW) -> dict[str, Any]:
    """Volatility concentration and where price sits — the same measures the screener uses.

    Distance from the low cannot tell a pump from a trend, so both are recorded and the
    model is left to decide rather than being handed a conclusion.
    """
    out: dict[str, Any] = {
        "atr_ratio": None, "max_bar_pct": None, "top3_share_pct": None,
        "off_low_pct": None, "off_high_pct": None, "range_pos_pct": None,
        "trend_efficiency": None,
    }
    if close is None or index < 40:
        return out

    lo = max(0, index - window + 1)
    w_high = np.asarray(high[lo:index + 1], dtype=float)
    w_low = np.asarray(low[lo:index + 1], dtype=float)
    w_close = np.asarray(close[lo:index + 1], dtype=float)
    if len(w_close) < 20:
        return out

    price = float(w_close[-1])
    hi, lo_ = float(w_high.max()), float(w_low.min())
    if lo_ > 0:
        out["off_low_pct"] = (price / lo_ - 1) * 100
    if hi > 0:
        out["off_high_pct"] = (price / hi - 1) * 100
    if hi > lo_:
        out["range_pos_pct"] = (price - lo_) / (hi - lo_) * 100

    # ATR now against its own recent median, i.e. is volatility abnormal right now.
    end = index + 1
    beg = max(1, end - 400)
    h = np.asarray(high[beg:end], dtype=float)
    l = np.asarray(low[beg:end], dtype=float)
    c = np.asarray(close[beg:end], dtype=float)
    if len(c) > 40:
        prev = c[:-1]
        tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - prev), np.abs(l[1:] - prev)))
        period = 14
        atr = float(np.mean(tr[:period]))
        series = []
        for value in tr[period:]:
            atr = (atr * (period - 1) + float(value)) / period
            series.append(atr)
        series = np.array(series)
        median = float(np.median(series)) if len(series) else 0.0
        if median > 0:
            out["atr_ratio"] = float(series[-1] / median)

    # How concentrated the movement is: a pump is one or two bars, a trend is many.
    returns = np.abs(np.diff(w_close) / w_close[:-1]) if len(w_close) > 1 else np.array([])
    if len(returns) >= 3:
        total = float(returns.sum())
        out["max_bar_pct"] = float(returns.max() * 100)
        if total > 0:
            out["top3_share_pct"] = float(np.sort(returns)[-3:].sum() / total * 100)
        churn = float(total * 100)
        net_move = float((w_close[-1] / w_close[0] - 1) * 100) if w_close[0] else 0.0
        out["trend_efficiency"] = net_move / churn if churn else None

    return out


def build_record(
    *,
    symbol: str,
    interval: str,
    index: int,
    context,
    signal,
    candles=None,
    window: int = REGIME_WINDOW,
) -> FeatureRecord:
    """Turn one engine decision into a record. Pure: it never touches trading state."""
    import datetime

    close = float(context.close) if context.close else 0.0
    ema = float(context.ema) if np.isfinite(context.ema) else 0.0
    ema_ref = float(context.ema_ref) if context.ema_ref and np.isfinite(context.ema_ref) else 0.0
    atr = float(context.atr) if context.atr and np.isfinite(context.atr) else 0.0

    threshold = getattr(context, "adx_threshold", None)
    adx = float(context.adx) if np.isfinite(context.adx) else 0.0

    daily = getattr(context, "daily", None)
    daily_trend = daily.dow.value if daily is not None else ""
    daily_ema_slope = int(daily.ema_slope) if daily is not None else 0
    leg = getattr(daily, "leg", None) if daily is not None else None

    gap_shallow = gap_deep = None
    if leg is not None and close:
        gap_shallow = (close / leg.level_shallow - 1) * 100 if leg.level_shallow else None
        gap_deep = (close / leg.level_deep - 1) * 100 if leg.level_deep else None

    when = datetime.datetime.fromtimestamp(int(context.ts) / 1000, datetime.UTC)

    record = FeatureRecord(
        symbol=symbol,
        interval=interval,
        bar_index=int(index),
        bar_ts=int(context.ts),
        side=signal.direction.label,
        allowed=bool(signal.allowed),
        blockers=list(signal.blockers),
        checks=dict(signal.checks),
        close=close,
        ema=ema,
        ema_slope_pct=((ema / ema_ref - 1) * 100) if ema_ref else 0.0,
        adx=adx,
        adx_threshold=float(threshold) if threshold is not None else None,
        adx_ratio=(adx / float(threshold)) if threshold else None,
        atr=atr,
        atr_pct=(atr / close * 100) if close else 0.0,
        dow=context.dow.value if context.dow is not None else "",
        daily_trend=daily_trend,
        daily_ema_slope=daily_ema_slope,
        daily_zone=getattr(signal, "daily_zone", None).value if getattr(signal, "daily_zone", None) else "",
        daily_depth=getattr(signal, "daily_depth", None),
        gap_to_shallow_pct=gap_shallow,
        gap_to_deep_pct=gap_deep,
        hour_utc=when.hour,
        weekday=when.weekday(),
    )

    if candles is not None:
        for key, value in _regime(candles.high, candles.low, candles.close, index, window).items():
            setattr(record, key, value)

    return record


class FeatureLog:
    """Collection of records, written as JSONL.

    Two modes, because the two callers need different things:

    * **Backtest** — no path. Records accumulate in memory and are written once at the
      end, outcomes included. A run is a single process and nothing can crash between
      the entry and its exit.
    * **Live** — a path. Every record is appended the moment it is made, so a restart
      mid-position does not lose the entry that is still open. Memory stays flat, and
      the outcome is appended separately when the position closes.

    Kept deliberately dumb: a list and a file. Anything cleverer would be a place for a
    bug to hide, and the whole point of the file is to be trusted later.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.records: list[FeatureRecord] = []
        self.path = Path(path) if path else None
        #: In live mode nothing is retained, so ``len`` counts what was written.
        self._written = 0
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def __len__(self) -> int:
        return self._written if self.path is not None else len(self.records)

    def _append_line(self, payload: dict[str, Any]) -> None:
        assert self.path is not None
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(payload, separators=(",", ":"), default=_default) + "\n")

    def record(self, item: FeatureRecord) -> int:
        if self.path is not None:
            # Append and forget: a live run has no end to write at.
            self._append_line({"kind": "feature", "schema": SCHEMA, **item.to_dict()})
            self._written += 1
            return self._written - 1
        self.records.append(item)
        return len(self.records) - 1

    def observe(self, *, symbol, interval, index, context, signal, candles=None) -> int:
        """Build and store a record in one call, for use from an engine loop."""
        return self.record(
            build_record(
                symbol=symbol, interval=interval, index=index,
                context=context, signal=signal, candles=candles,
            )
        )

    def append_outcome(self, symbol: str, bar_ts: int, trade) -> None:
        """Live mode: append the outcome of a closed trade as its own line.

        A separate line rather than a rewrite, because the file is append-only and a
        rewrite is the one operation that can corrupt everything written before it. A
        reader joins by ``(symbol, bar_ts)``.

        Written for ``ClosedTrade``, which is what the runner has at that point — and it
        carries LESS than the backtest's ``Trade``: no stop, no excursion, no bar
        indices, only ``bars_held``. Reading those attributes anyway is how this method
        first failed, silently, once per close. ``getattr`` with a default keeps it
        working for both types instead of assuming the richer one.
        """
        if self.path is None:
            self.attach_outcome(trade)
            return
        self._append_line({
            "kind": "outcome",
            "schema": SCHEMA,
            "symbol": symbol,
            "bar_ts": int(bar_ts),
            "taken": True,
            "entry_price": _num(getattr(trade, "entry_price", None)),
            "stop_price": _num(getattr(trade, "initial_stop", None)),
            "qty": _num(getattr(trade, "initial_qty", None)) or _num(getattr(trade, "qty", None)),
            "exit_ts": int(trade.exit_ts) if getattr(trade, "exit_ts", None) else None,
            "exit_price": _num(getattr(trade, "exit_price", None)) or _exit_price_of(trade),
            "exit_reason": _reason_of(trade),
            "net_pnl": _num(getattr(trade, "net_pnl", None)),
            "bars_held": _int(getattr(trade, "bars_held", None)),
            "max_favourable": _num(getattr(trade, "max_favorable_excursion", None)),
            "max_adverse": _num(getattr(trade, "max_adverse_excursion", None)),
        })

    def attach_outcome(self, trade) -> bool:
        """Fill in the outcome of a trade on the record that proposed it.

        The signal is evaluated on bar ``i`` and filled at bar ``i + 1``, so the record is
        the one whose ``bar_index`` is exactly one before the trade's entry bar. Matching
        on that is deterministic; matching on time would not be, because several symbols
        share timestamps.
        """
        target = int(trade.entry_bar) - 1
        for rec in reversed(self.records):
            if rec.bar_index == target and rec.side == trade.direction.label:
                rec.taken = True
                rec.entry_price = float(trade.entry_price)
                rec.stop_price = float(trade.initial_stop)
                rec.stop_pct = (
                    (trade.initial_stop / trade.entry_price - 1) * 100
                    if trade.entry_price else None
                )
                rec.qty = float(trade.initial_qty)
                rec.exit_ts = int(trade.exit_ts)
                rec.exit_price = _exit_price_of(trade)
                rec.exit_reason = _reason_of(trade)
                rec.net_pnl = float(trade.net_pnl)
                rec.bars_held = int(trade.exit_bar - trade.entry_bar) if trade.exit_bar >= 0 else None
                rec.max_favourable = float(trade.max_favorable_excursion)
                rec.max_adverse = float(trade.max_adverse_excursion)
                return True
        return False

    @property
    def labelled(self) -> list[FeatureRecord]:
        return [r for r in self.records if r.labelled]

    def summary(self) -> dict[str, Any]:
        labelled = self.labelled
        by_side: dict[str, int] = {}
        for r in labelled:
            by_side[r.side] = by_side.get(r.side, 0) + 1
        return {
            "schema": SCHEMA,
            "decisions": len(self.records),
            "taken": sum(1 for r in self.records if r.taken),
            "labelled": len(labelled),
            "blocked": sum(1 for r in self.records if not r.allowed),
            "by_side": by_side,
            "wins": sum(1 for r in labelled if (r.net_pnl or 0) > 0),
        }

    def write(self, path: str | Path) -> Path:
        """Write every record as one JSON object per line.

        JSONL rather than a JSON array so a long run appends and a partial file is still
        readable — a crash halfway through must not cost the whole dataset.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as fh:
            for rec in self.records:
                fh.write(json.dumps(rec.to_dict(), separators=(",", ":"), default=_default) + "\n")
        return target

    @classmethod
    def read(cls, path: str | Path) -> list[dict[str, Any]]:
        """Read a log back, skipping lines that a crash left half-written."""
        rows: list[dict[str, Any]] = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    @classmethod
    def joined(cls, path: str | Path) -> list[dict[str, Any]]:
        """Read a live log and merge each outcome line onto its feature line.

        The backtest writes one complete line per record; live writes the feature when
        the decision is made and the outcome when the trade closes, because a live
        process can be restarted in between. This gives both callers the same shape.
        """
        features: dict[tuple[str, int], dict[str, Any]] = {}
        outcomes: dict[tuple[str, int], dict[str, Any]] = {}
        order: list[tuple[str, int]] = []
        for row in cls.read(path):
            key = (str(row.get("symbol") or ""), int(row.get("bar_ts") or 0))
            if row.get("kind") == "outcome":
                outcomes[key] = row
            else:
                if key not in features:
                    order.append(key)
                features[key] = row
        out = []
        for key in order:
            merged = dict(features[key])
            merged.update(outcomes.get(key, {}))
            out.append(merged)
        return out


def _num(value) -> float | None:
    """Coerce to float, or None. Never raises: this runs inside a trading loop."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _exit_price_of(trade) -> float | None:
    """Volume-weighted price of the exits.

    ``Trade`` has no ``exit_price``: a position can leave through several fills — a take
    profit partly, the stop for the rest — so the price is derived from the fills rather
    than stored. Weighted, not the last one, or a small final stop-out would be reported
    as the price of the whole exit.
    """
    fills = list(getattr(trade, "fills", []) or [])
    if not fills:
        return None
    total = sum(float(f.qty) for f in fills)
    if total <= 0:
        return None
    return sum(float(f.price) * float(f.qty) for f in fills) / total


def _reason_of(trade) -> str:
    fills: Sequence[Any] = getattr(trade, "fills", []) or []
    if not fills:
        return ""
    reason = getattr(fills[-1], "reason", "")
    return getattr(reason, "value", reason) or ""


def _default(obj: Any) -> Any:
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return str(obj)
