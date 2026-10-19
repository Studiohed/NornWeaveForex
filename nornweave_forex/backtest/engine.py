"""Event-driven, look-ahead-free, multi-symbol bar-by-bar backtester.

Forex money model
-----------------
Quantity is **lots** and every monetary result is in the account currency:

* PnL per fill ``instrument.pnl_money(entry, exit, lots, sign)`` — i.e.
  ``(exit - entry) / pip * pip_value_per_lot * lots``, never
  ``(exit - entry) * lots`` (that would be off by ``contract_size`` ≈ 100,000x).
* Fees are ``instrument.notional_account(price, lots) * backtest.taker_fee``.
* Swap is ``instrument.swap_money(sign=..., lots=..., tripled=...)`` and is
  credited/debited to cash at each 21:00 UTC rollover. ``Trade.funding`` is
  repurposed as the swap *cost* (positive = paid), which keeps
  ``Trade.net_pnl = gross_pnl - fees - funding`` correct: a long paying
  ``-5/lot`` adds ``+5`` of funding. The signed cash credit is ``-funding``.

Execution model
---------------
* Indicators are built **once** per symbol over the full series and read with
  ``IndicatorSet.context_at(idx)``; trading starts at
  ``max(backtest.warmup_bars, IndicatorSet.ready_index)``.
* A signal produced at the close of bar ``i`` becomes a pending entry and is
  filled at the **open of bar ``i + 1``** for that symbol. It is never filled on
  the bar that generated it.
* Every fill pays half of ``typical_spread_points * point`` adversely (entry and
  exit, including resting target fills), plus ``slippage_bps`` on market fills.
* Intrabar stop/target resolution is delegated to
  :mod:`nornweave_forex.execution`, exactly like the live runner: intrabar plans
  first, then a close-triggered stop, then break-even arming, then the
  trend-reversal exit, then swap.
* Each open position is marked at **its own symbol's** last close, so a
  multi-symbol portfolio is not priced off whichever pair happened to tick last.
* Realised PnL and swap are credited to cash on every fill; equity is
  ``cash + unrealised``. Positions still open at the end of the data are closed
  with an ``END_OF_DATA`` fill and the final equity point is settled to cash.

A position is always single-instrument. The union timeline keeps multi-symbol
up to ``portfolio.max_positions`` simultaneous positions and enforces
``risk.portfolio_risk_pct`` at entry.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from nornweave_forex.config import Config, interval_ms
from nornweave_forex.execution import (
    ExitPlan,
    PositionView,
    break_even_should_arm,
    effective_stop_trigger,
    plan_intrabar_exits,
    stop_breached_on_close,
)
from nornweave_forex.instrument import InstrumentSpec
from nornweave_forex.risk.sizing import position_size
from nornweave_forex.strategy.context import IndicatorSet, build_indicators
from nornweave_forex.strategy.engine import SignalEngine
from nornweave_forex.strategy.exits import build_exit_ladder
from nornweave_forex.types import CandleArrays, Direction, ExitReason, Fill, Trade

from nornweave_forex.backtest.metrics import Metrics, compute_metrics

__all__ = ["BacktestEngine", "BacktestResult", "PortfolioBacktester"]

#: Cash quantities below this are treated as flat (lots are multiples of 0.01).
_EPS = 1e-12


@dataclass(slots=True)
class _OpenPosition:
    trade: Trade
    symbol: str
    ladder: Any
    qty: float
    tps_hit: list[bool]
    be_active: bool = False
    swap_accrued: float = 0.0


@dataclass(slots=True)
class _PendingEntry:
    direction: Direction
    atr: float
    tp1_override: float | None
    signal_close: float
    signal_bar: int


@dataclass(slots=True)
class BacktestResult:
    trades: list[Trade]
    equity_curve: np.ndarray
    timestamps: np.ndarray
    metrics: Metrics
    signals_evaluated: int = 0
    entries_allowed: int = 0
    entries_taken: int = 0
    entries_rejected_by_sizing: int = 0
    bars_in_market: int = 0
    blocked_by: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    start_index: int = 0
    config_summary: str = ""

    def summary(self) -> str:
        head = [
            self.config_summary,
            "",
            f"portfolio bars simulated: {self.equity_curve.size}",
            f"signals evaluated: {self.signals_evaluated} | allowed: {self.entries_allowed} "
            f"| taken: {self.entries_taken} | rejected by sizing: {self.entries_rejected_by_sizing}",
        ]
        if self.blocked_by:
            top = sorted(self.blocked_by.items(), key=lambda kv: -kv[1])[:8]
            head.append("top blockers: " + ", ".join(f"{k}={v}" for k, v in top))
        if self.warnings:
            head.append("warnings:")
            head.extend(f"  ! {w}" for w in self.warnings)
        return "\n".join(head)


class PortfolioBacktester:
    """Run NORN WEAVE over historical candle series for a list of pairs."""

    def __init__(
        self,
        config: Config,
        series: dict[str, CandleArrays],
        *,
        instruments: dict[str, InstrumentSpec],
    ) -> None:
        config.validate()
        self.config = config
        self.series = dict(series)
        self.instruments = instruments
        self.engine = SignalEngine(config.strategy)
        self._interval_ms = interval_ms(config.interval)

    # -- execution price helpers ------------------------------------------- #
    def _fill_price(
        self,
        instrument: InstrumentSpec,
        reference: float,
        direction: Direction,
        *,
        is_exit: bool,
        slippage: bool,
    ) -> float:
        """Adverse fill price: half-spread always, slippage on market orders."""
        half_spread = instrument.typical_spread_points * instrument.point / 2.0
        # A long buys on entry and sells on exit; a short does the opposite.
        buying = (direction is Direction.LONG) != bool(is_exit)
        price = float(reference) + half_spread if buying else float(reference) - half_spread
        bps = self.config.backtest.slippage_bps / 10_000.0
        if slippage and bps:
            price *= 1.0 + bps if buying else 1.0 - bps
        return instrument.round_price(price)

    def _fee(self, instrument: InstrumentSpec, price: float, lots: float) -> float:
        return instrument.notional_account(price, lots) * self.config.backtest.taker_fee

    def _swap_pnl(
        self, instrument: InstrumentSpec, direction: Direction, lots: float, bar_start: _dt.datetime
    ) -> float:
        """Signed swap for one rollover, or 0 for a bar that does not cross 21:00 UTC."""
        if not self.config.backtest.swap_enabled:
            return 0.0
        bar_end = bar_start + _dt.timedelta(milliseconds=self._interval_ms)
        rollover = bar_start.replace(hour=21, minute=0, second=0, microsecond=0)
        if not (bar_start < rollover <= bar_end):
            return 0.0
        tripled = bar_start.weekday() == instrument.swap_triple_day
        return instrument.swap_money(sign=direction.sign, lots=lots, tripled=tripled)

    def _unrealised(self, positions: dict[str, _OpenPosition], last_close: dict[str, float]) -> float:
        total = 0.0
        for symbol, pos in positions.items():
            instrument = self.instruments[symbol]
            mark = last_close.get(symbol, pos.trade.entry_price)
            total += instrument.pnl_money(
                pos.trade.entry_price, mark, pos.qty, pos.trade.direction.sign
            )
        return total

    def _open_risk(self, positions: dict[str, _OpenPosition]) -> float:
        """Account-currency risk still open, at each position's *current* stop."""
        total = 0.0
        for symbol, pos in positions.items():
            instrument = self.instruments[symbol]
            total += abs(
                instrument.pnl_money(
                    pos.trade.entry_price, pos.ladder.stop, pos.qty, pos.trade.direction.sign
                )
            )
        return total

    # -- setup ------------------------------------------------------------- #
    def _prepare(self) -> tuple[dict[str, IndicatorSet], dict[str, int], list[tuple[int, str, int]]]:
        cfg = self.config
        indicators: dict[str, IndicatorSet] = {}
        starts: dict[str, int] = {}
        events: list[tuple[int, str, int]] = []
        for symbol, candles in self.series.items():
            if symbol not in self.instruments:
                raise ValueError(f"{symbol}: no InstrumentSpec supplied")
            if len(candles) == 0:
                raise ValueError(f"{symbol}: empty candle series")
            ind = build_indicators(candles, cfg.interval, cfg.strategy, tuning=cfg.tuning)
            indicators[symbol] = ind
            start = max(int(cfg.backtest.warmup_bars), int(ind.ready_index))
            starts[symbol] = start
            for idx in range(start, len(candles)):
                events.append((int(candles.ts[idx]), symbol, idx))
        if not events:
            raise ValueError("no bars after warmup")
        events.sort(key=lambda e: (e[0], e[1]))
        return indicators, starts, events

    # -- main loop --------------------------------------------------------- #
    def run(self) -> BacktestResult:
        cfg = self.config
        indicators, starts, events = self._prepare()
        cash = float(cfg.backtest.initial_equity)
        positions: dict[str, _OpenPosition] = {}
        pending: dict[str, _PendingEntry] = {}
        trades: list[Trade] = []
        blocked_by: dict[str, int] = {}
        warnings: list[str] = []
        last_close = {symbol: float(candles.close[-1]) for symbol, candles in self.series.items()}
        signals_evaluated = entries_allowed = entries_taken = entries_rejected = 0
        bars_in_market = 0
        portfolio_bars_open: set[int] = set()
        timestamps: list[int] = []
        equity_curve: list[float] = []

        for symbol, candles in self.series.items():
            for problem in candles.validate():
                warnings.append(f"{symbol}: data: {problem}")

        def fill_exit(pos: _OpenPosition, plan: ExitPlan, bar_index: int, bar_ts: int) -> None:
            nonlocal cash
            instrument = self.instruments[pos.symbol]
            portion = min(float(plan.qty), pos.qty)
            if portion <= _EPS:
                return
            price = self._fill_price(
                instrument, plan.price, pos.trade.direction,
                is_exit=True, slippage=bool(plan.slippage),
            )
            fee = self._fee(instrument, price, portion)
            pnl = instrument.pnl_money(
                pos.trade.entry_price, price, portion, pos.trade.direction.sign
            )
            cash += pnl - fee
            pos.trade.gross_pnl += pnl
            pos.trade.fees += fee
            pos.trade.fills.append(
                Fill(
                    ts=bar_ts,
                    bar_index=bar_index,
                    price=price,
                    qty=portion,
                    fee=fee,
                    reason=plan.reason,
                    pnl=pnl - fee,
                )
            )
            pos.qty -= portion
            if plan.level is not None and plan.level < len(pos.tps_hit):
                pos.tps_hit[plan.level] = True
            if pos.qty <= _EPS:
                pos.qty = 0.0
                pos.trade.exit_bar = bar_index
                pos.trade.exit_ts = bar_ts
                trades.append(pos.trade)
                positions.pop(pos.symbol, None)

        def open_position(pend: _PendingEntry, symbol: str, idx: int, bar_ts: int,
                           bar_open: float) -> None:
            nonlocal cash, entries_taken, entries_rejected
            instrument = self.instruments[symbol]
            entry_price = self._fill_price(
                instrument, bar_open, pend.direction, is_exit=False, slippage=True
            )
            ladder = build_exit_ladder(
                pend.direction,
                entry_price,
                pend.atr,
                cfg.risk,
                tp1_override=pend.tp1_override,
                atr_factor=None,
                stop_loss_pct=None,
            )
            decision = position_size(
                equity=cash,
                entry=entry_price,
                stop=ladder.stop,
                instrument=instrument,
                risk=cfg.risk,
                open_risk=self._open_risk(positions),
            )
            if not decision.ok or not instrument.accepts(decision.lots, entry_price)[0]:
                entries_rejected += 1
                blocked_by["sizing"] = blocked_by.get("sizing", 0) + 1
                return
            fee = self._fee(instrument, entry_price, decision.lots)
            risk_per_unit = abs(entry_price - ladder.stop) / instrument.pip * instrument.pip_value_per_lot
            trade = Trade(
                direction=pend.direction,
                entry_bar=idx,
                entry_ts=bar_ts,
                entry_price=entry_price,
                initial_qty=decision.lots,
                initial_stop=ladder.stop,
                initial_risk_per_unit=risk_per_unit,
                tp_prices=list(ladder.tp_prices),
                fees=fee,
            )
            cash -= fee
            positions[symbol] = _OpenPosition(
                trade=trade,
                symbol=symbol,
                ladder=ladder,
                qty=decision.lots,
                tps_hit=[False] * len(ladder.tp_prices),
            )
            entries_taken += 1

        for event_index, (bar_ts, symbol, idx) in enumerate(events):
            candles = self.series[symbol]
            ind = indicators[symbol]
            bar_open = float(candles.open[idx])
            bar_high = float(candles.high[idx])
            bar_low = float(candles.low[idx])
            bar_close = float(candles.close[idx])
            last_close[symbol] = bar_close
            bar_start = _dt.datetime.fromtimestamp(bar_ts / 1000.0, tz=_dt.timezone.utc)

            # 1) A signal from the previous bar fills at THIS bar's open.
            pend = pending.pop(symbol, None)
            if pend is not None and symbol not in positions:
                if len(positions) >= cfg.portfolio.max_positions:
                    blocked_by["max_positions"] = blocked_by.get("max_positions", 0) + 1
                elif not np.isfinite(pend.atr) or pend.atr <= 0:
                    entries_rejected += 1
                    blocked_by["invalid_atr"] = blocked_by.get("invalid_atr", 0) + 1
                else:
                    open_position(pend, symbol, idx, bar_ts, bar_open)

            # 2) Resolve an existing position against this bar, in live-runner order.
            pos = positions.get(symbol)
            if pos is not None:
                portfolio_bars_open.add(bar_ts)
                plans = plan_intrabar_exits(
                    PositionView(
                        direction=pos.trade.direction,
                        entry_price=pos.trade.entry_price,
                        qty=pos.qty,
                        initial_qty=pos.trade.initial_qty,
                        stop=pos.ladder.stop,
                        tp_prices=tuple(pos.ladder.tp_prices),
                        tp_weights=tuple(pos.ladder.tp_weights),
                        tps_hit=tuple(pos.tps_hit),
                        be_active=pos.be_active,
                    ),
                    bar_open=bar_open,
                    bar_high=bar_high,
                    bar_low=bar_low,
                    bar_close=bar_close,
                    intrabar_priority=cfg.backtest.intrabar_priority,
                    stop_trigger=cfg.risk.stop_trigger,
                    exit_fill=cfg.risk.exit_fill,
                    be_exit_trigger=cfg.risk.be_exit_trigger,
                    tp_weight_basis=cfg.risk.tp_weight_basis,
                )
                for plan in plans:
                    fill_exit(pos, plan, idx, bar_ts)
                    if symbol not in positions:
                        break

                pos = positions.get(symbol)
                if pos is not None and pos.qty > 0:
                    trigger = effective_stop_trigger(
                        be_active=pos.be_active,
                        stop_trigger=cfg.risk.stop_trigger,
                        be_exit_trigger=cfg.risk.be_exit_trigger,
                    )
                    if trigger == "close" and stop_breached_on_close(
                        pos.trade.direction, pos.ladder.stop, bar_close
                    ):
                        fill_exit(pos, ExitPlan(
                            reason=ExitReason.BREAK_EVEN_STOP if pos.be_active else ExitReason.STOP_LOSS,
                            price=bar_close,
                            qty=pos.qty,
                            slippage=True,
                        ), idx, bar_ts)

                pos = positions.get(symbol)
                if pos is not None and pos.qty > 0 and not pos.be_active:
                    if break_even_should_arm(
                        be_mode=cfg.risk.be_mode,
                        direction=pos.trade.direction,
                        bar_high=bar_high,
                        bar_low=bar_low,
                        be_trigger_price=pos.ladder.be_trigger_price,
                        tps_hit=tuple(pos.tps_hit),
                        arm_on=cfg.risk.be_arm_on,
                        bar_close=bar_close,
                    ):
                        pos.be_active = True
                        pos.ladder = replace(pos.ladder, stop=pos.ladder.be_stop)

                # Trend-reversal exit, decided at the close like the live runner.
                pos = positions.get(symbol)
                if pos is not None and pos.qty > 0 and cfg.strategy.exit_on_dow_reversal:
                    ctx = ind.context_at(idx)
                    reason = self.engine.reversal_exit(
                        pos.trade.direction, ctx.dow, close=bar_close, ema=ctx.ema
                    )
                    if reason is not None:
                        fill_exit(pos, ExitPlan(
                            reason=ExitReason.TREND_REVERSAL,
                            price=bar_close,
                            qty=pos.qty,
                            slippage=True,
                        ), idx, bar_ts)

                # Swap: one rollover per bar that crosses 21:00 UTC.
                # ``Trade.funding`` keeps the crypto funding-field convention (a
                # positive number is a cost), so ``net_pnl = gross - fees -
                # funding`` stays correct. The signed cash credit is the negative
                # of that: a long paying -5/lot is +5 of funding cost.
                pos = positions.get(symbol)
                if pos is not None and pos.qty > 0:
                    swap_credit = self._swap_pnl(
                        self.instruments[symbol], pos.trade.direction, pos.qty, bar_start
                    )
                    if swap_credit:
                        cash += swap_credit
                        pos.swap_accrued += swap_credit
                        pos.trade.funding -= swap_credit

            # 3) End-of-bar signal evaluation: it becomes a pending fill for next bar.
            if symbol not in positions and symbol not in pending:
                signals_evaluated += 1
                ctx = ind.context_at(idx)
                signal = self.engine.evaluate(ctx)
                if signal.allowed:
                    entries_allowed += 1
                    if len(positions) >= cfg.portfolio.max_positions:
                        blocked_by["max_positions"] = blocked_by.get("max_positions", 0) + 1
                    elif not np.isfinite(signal.atr) or signal.atr <= 0:
                        blocked_by["invalid_atr"] = blocked_by.get("invalid_atr", 0) + 1
                    else:
                        pending[symbol] = _PendingEntry(
                            direction=signal.direction,
                            atr=float(signal.atr),
                            tp1_override=signal.tp1_override,
                            signal_close=bar_close,
                            signal_bar=idx,
                        )
                else:
                    for blocker in signal.blockers:
                        blocked_by[blocker] = blocked_by.get(blocker, 0) + 1

            # 4) One portfolio equity point per timestamp (not per symbol event).
            if event_index + 1 == len(events) or events[event_index + 1][0] != bar_ts:
                timestamps.append(bar_ts)
                equity_curve.append(cash + self._unrealised(positions, last_close))

        # -- settle anything still open at the end of the data ---------------- #
        for symbol in list(positions):
            pos = positions[symbol]
            candles = self.series[symbol]
            final_idx = len(candles) - 1
            final_ts = int(candles.ts[final_idx])
            fill_exit(pos, ExitPlan(
                reason=ExitReason.END_OF_DATA,
                price=float(candles.close[final_idx]),
                qty=pos.qty,
                slippage=True,
            ), final_idx, final_ts)
            warnings.append(
                f"{symbol}: position was still open at the end of the data and was "
                "closed at the final close; treat the last trade as truncated"
            )

        eq = np.asarray(equity_curve, dtype=np.float64)
        ts = np.asarray(timestamps, dtype=np.int64)
        # Exposure is measured over return intervals, so the first snapshot (which
        # has no preceding bar) does not count.
        bars_in_market = len(portfolio_bars_open)
        if timestamps and portfolio_bars_open and min(portfolio_bars_open) == timestamps[0]:
            bars_in_market -= 1
        if eq.size:
            # The last snapshot was taken before end-of-data settlement.
            eq[-1] = cash
        metrics = compute_metrics(
            trades,
            eq,
            ts,
            float(cfg.backtest.initial_equity),
            cfg.interval,
            bars_in_market=bars_in_market,
        )
        if entries_allowed > 0 and entries_taken == 0:
            warnings.append(
                "signals fired but every entry was rejected by position sizing "
                "(check risk.risk_per_trade_pct, leverage and instrument minimums)"
            )

        return BacktestResult(
            trades=trades,
            equity_curve=eq,
            timestamps=ts,
            metrics=metrics,
            signals_evaluated=signals_evaluated,
            entries_allowed=entries_allowed,
            entries_taken=entries_taken,
            entries_rejected_by_sizing=entries_rejected,
            bars_in_market=bars_in_market,
            blocked_by=blocked_by,
            warnings=warnings,
            start_index=min(starts.values()),
            config_summary=cfg.describe(),
        )


# --------------------------------------------------------------------------- #
# Compatibility alias
# --------------------------------------------------------------------------- #
BacktestEngine = PortfolioBacktester
