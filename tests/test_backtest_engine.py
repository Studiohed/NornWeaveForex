"""Forex backtest-engine tests: money, timing, spread, swap, marks, settlement.

These are the permanent form of the probes used to verify the offline-stack fixes.
They pin the five things that were wrong before the port was fixed:

* PnL is in account currency through ``InstrumentSpec.pnl_money`` (a 10-pip move
  on 0.10 lots of EURUSD is $10, not $0.001).
* A signal at bar ``k`` fills at the **open of bar k+1**, never at ``open[k]``.
* The spread is paid on entry **and** exit.
* Swap is credited to cash and tripled on the spec's Python weekday (Wednesday).
* Every position is marked at its own symbol's close; an open position is settled
  with an ``END_OF_DATA`` fill and cash reconciles with ``sum(trade.net_pnl)``.

The series are small (150-600 bars) and the signal engine is stubbed where the
test needs a deterministic entry, so the file runs in well under a second.
"""

from __future__ import annotations

import copy
import datetime as dt
from dataclasses import replace

import numpy as np
import pytest

from nornweave_forex.backtest.engine import PortfolioBacktester, _OpenPosition
from nornweave_forex.config import Config
from nornweave_forex.data.synthetic import synthetic_candles
from nornweave_forex.instrument import default_instrument
from nornweave_forex.strategy.engine import EntrySignal
from nornweave_forex.types import CandleArrays, Direction, DowTrend, ExitReason, Trade

_START_TS = 1_600_000_000_000
#: Monday 2024-01-01 00:00 UTC — a clean FX session start for swap tests.
_MONDAY_TS = int(dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class _StubSignalEngine:
    """Replaces :class:`SignalEngine` so a test controls exactly where it enters.

    ``reversal_exit`` is used by the engine's trend-reversal branch; returning
    ``None`` keeps positions open for swap / end-of-data tests.
    """

    def __init__(self, evaluate) -> None:
        self._evaluate = evaluate

    def evaluate(self, ctx) -> EntrySignal:
        return self._evaluate(ctx)

    def reversal_exit(self, *args, **kwargs) -> str | None:
        return None


def _allowed(atr: float = 0.0005, direction: Direction = Direction.LONG) -> EntrySignal:
    return EntrySignal(
        direction=direction,
        allowed=True,
        checks={"stub": True},
        blockers=(),
        tp1_override=None,
        atr=atr,
        dow=DowTrend.BULLISH,
    )


def _blocked() -> EntrySignal:
    return EntrySignal(
        direction=Direction.FLAT,
        allowed=False,
        checks={},
        blockers=("stub",),
        tp1_override=None,
        atr=0.0005,
        dow=DowTrend.NEUTRAL,
    )


def _oscillating_prices(n: int, base: float = 1.10, amplitude: float = 0.0006,
                        period: int = 7) -> np.ndarray:
    """Non-flat prices so ADX/ATR have finite values from an early index.

    A perfectly flat series makes ADX NaN everywhere and the engine refuses to
    build indicators; a gentle sine gives valid indicators while staying far
    from any stop/target level.
    """
    t = np.arange(n, dtype=np.float64)
    return base + amplitude * np.sin(2.0 * np.pi * t / period)


def _crafted_candles(
    prices: np.ndarray,
    *,
    start_ts: int = _START_TS,
    half_range: float = 0.0004,
    interval_minutes: int = 120,
) -> CandleArrays:
    """OHLC around a price path: open = previous close, high/low = body ± wick."""
    prices = np.asarray(prices, dtype=np.float64)
    n = len(prices)
    ts = np.arange(n, dtype=np.int64) * interval_minutes * 60_000 + start_ts
    open_ = np.empty(n, dtype=np.float64)
    open_[0] = prices[0]
    open_[1:] = prices[:-1]
    high = np.maximum(open_, prices) + half_range
    low = np.minimum(open_, prices) - half_range
    return CandleArrays(
        ts=ts,
        open=open_,
        high=high,
        low=low,
        close=prices.copy(),
        volume=np.full(n, 1_000.0),
    )


def _run_forced(
    cfg: Config,
    candles: CandleArrays,
    instrument,
    signal_index: int,
    *,
    atr: float = 0.0005,
    direction: Direction = Direction.LONG,
):
    """Run the backtester with a signal forced at exactly ``signal_index``."""
    bt = PortfolioBacktester(cfg, {"EURUSD": candles}, instruments={"EURUSD": instrument})
    bt.engine = _StubSignalEngine(
        lambda ctx: _allowed(atr=atr, direction=direction)
        if ctx.index == signal_index
        else _blocked()
    )
    return bt.run()


def _small_cfg(forex_config: Config) -> Config:
    """Test config: tiny warmup, zero slippage/fee, deterministic money.

    Returns a deep copy so a test that needs two variants (e.g. swap on/off)
    does not mutate the other one through the shared fixture.
    """
    cfg = copy.deepcopy(forex_config)
    cfg.backtest.warmup_bars = 10
    cfg.backtest.slippage_bps = 0.0
    cfg.backtest.taker_fee = 0.0
    cfg.backtest.swap_enabled = True
    cfg.portfolio.max_positions = 2
    return cfg


def _half_spread(instrument) -> float:
    return instrument.typical_spread_points * instrument.point / 2.0


def _backtester(cfg: Config, candles: CandleArrays, instruments: dict):
    return PortfolioBacktester(cfg, {"EURUSD": candles}, instruments=instruments)


# --------------------------------------------------------------------------- #
# 1. Money
# --------------------------------------------------------------------------- #
def test_ten_pips_on_010_lots_is_ten_dollars(eurusd_spec, usdjpy_spec) -> None:
    # EURUSD is USD-quoted: $10 per pip per standard lot.
    assert eurusd_spec.pnl_money(1.1000, 1.1010, 0.10, +1) == pytest.approx(10.0)
    assert eurusd_spec.pnl_money(1.1010, 1.1000, 0.10, -1) == pytest.approx(10.0)
    # USDJPY's pip value floats with the pair: ~$6.67 per pip per lot at 150.
    assert usdjpy_spec.pnl_money(110.00, 110.10, 0.10, +1) == pytest.approx(6.667, rel=1e-3)
    # Shorts are the sign mirror of longs.
    assert usdjpy_spec.pnl_money(110.10, 110.00, 0.10, -1) == pytest.approx(6.667, rel=1e-3)


def test_run_reconciles_net_pnl_with_equity_and_charges_no_fees(forex_config) -> None:
    cfg = _small_cfg(forex_config)
    cfg.backtest.warmup_bars = 200
    candles = synthetic_candles("EURUSD", bars=600, interval_minutes=120)
    result = _backtester(
        cfg, candles, {"EURUSD": default_instrument("EURUSD", price=float(candles.close[-1]))}
    ).run()

    assert len(result.trades) > 0, "the synthetic run should trade"
    closed = sum(trade.net_pnl for trade in result.trades)
    assert closed == pytest.approx(
        float(result.equity_curve[-1]) - cfg.backtest.initial_equity, abs=1e-6
    )
    assert result.metrics.net_profit == pytest.approx(closed, abs=1e-9)
    assert all(trade.fees == 0.0 for trade in result.trades)  # taker_fee == 0
    assert result.metrics.total_fees == 0.0


# --------------------------------------------------------------------------- #
# 2. No look-ahead
# --------------------------------------------------------------------------- #
def test_signal_fills_at_next_bar_open_not_signal_bar_open(forex_config, eurusd_spec) -> None:
    cfg = _small_cfg(forex_config)
    candles = _crafted_candles(_oscillating_prices(200))
    signal_index = 100
    result = _run_forced(cfg, candles, eurusd_spec, signal_index)

    assert len(result.trades) == 1
    trade = result.trades[0]
    half = _half_spread(eurusd_spec)
    next_open = float(candles.open[signal_index + 1])
    signal_open = float(candles.open[signal_index])
    assert next_open != signal_open, "crafted series must distinguish the two opens"

    assert trade.entry_bar == signal_index + 1
    assert trade.entry_ts == int(candles.ts[signal_index + 1])
    assert trade.entry_price == eurusd_spec.round_price(next_open + half)
    # The look-ahead fill — the signal bar's own open — must never be used.
    assert trade.entry_price != eurusd_spec.round_price(signal_open + half)


# --------------------------------------------------------------------------- #
# 3. Spread on both sides
# --------------------------------------------------------------------------- #
def test_spread_is_paid_on_entry_and_exit(forex_config, eurusd_spec) -> None:
    prices = _oscillating_prices(150)
    prices[71] += 0.004  # next bar's high blows through TP1 and TP2
    candles = _crafted_candles(prices)
    cfg = _small_cfg(forex_config)

    spread_result = _run_forced(
        cfg, candles, replace(eurusd_spec, typical_spread_points=10.0), 70
    )
    free_result = _run_forced(
        cfg, candles, replace(eurusd_spec, typical_spread_points=0.0), 70
    )
    assert len(spread_result.trades) == 1
    assert len(free_result.trades) == 1

    half = _half_spread(eurusd_spec)  # 0.5 pip on a 5-digit EURUSD
    spread_trade = spread_result.trades[0]
    free_trade = free_result.trades[0]

    # Entry pays half a spread above the bar open ...
    assert spread_trade.entry_price == eurusd_spec.round_price(float(candles.open[71]) + half)
    assert free_trade.entry_price == eurusd_spec.round_price(float(candles.open[71]))
    # ... and the target exit gives up half a spread below its level.
    tp1 = spread_trade.tp_prices[0]
    assert spread_trade.fills[0].reason is ExitReason.TAKE_PROFIT_1
    assert spread_trade.fills[0].price == eurusd_spec.round_price(tp1 - half)
    assert free_trade.fills[0].price == eurusd_spec.round_price(free_trade.tp_prices[0])

    # A round trip at an unchanged mid price costs one full spread.
    round_trip = eurusd_spec.pnl_money(
        float(candles.open[71]) + half, float(candles.open[71]) - half, 0.10, +1
    )
    assert round_trip == pytest.approx(-1.0)  # 1 pip on 0.10 lots
    assert spread_result.metrics.net_profit < free_result.metrics.net_profit


# --------------------------------------------------------------------------- #
# 4./5. Swap: credit, triple weekday, toggle
# --------------------------------------------------------------------------- #
def test_triple_swap_is_booked_on_the_spec_python_weekday(forex_config, eurusd_spec) -> None:
    # The fixture carries the legacy crypto day index 3; the engine compares
    # Python weekdays, so 2 must mean Wednesday and 3 must mean Thursday.
    wednesday_spec = replace(eurusd_spec, swap_triple_day=2)
    assert default_instrument("EURUSD").swap_triple_day == 2
    candles = _crafted_candles(_oscillating_prices(120))
    bt = _backtester(_small_cfg(forex_config), candles, {"EURUSD": wednesday_spec})

    wed_2000 = dt.datetime(2024, 1, 3, 20, 0, tzinfo=dt.timezone.utc)
    thu_2000 = dt.datetime(2024, 1, 4, 20, 0, tzinfo=dt.timezone.utc)
    # A bar starting at 22:00 does not cross 21:00, so no swap is due.
    wed_2200 = dt.datetime(2024, 1, 3, 22, 0, tzinfo=dt.timezone.utc)

    assert bt._swap_pnl(wednesday_spec, Direction.LONG, 1.0, wed_2000) == pytest.approx(-15.0)
    assert bt._swap_pnl(wednesday_spec, Direction.LONG, 1.0, thu_2000) == pytest.approx(-5.0)
    assert bt._swap_pnl(wednesday_spec, Direction.SHORT, 1.0, wed_2000) == pytest.approx(+6.0)
    assert bt._swap_pnl(wednesday_spec, Direction.LONG, 1.0, wed_2200) == pytest.approx(0.0)

    # swap_enabled=False switches the whole model off.
    off_cfg = _small_cfg(forex_config)
    off_cfg.backtest.swap_enabled = False
    bt_off = _backtester(off_cfg, candles, {"EURUSD": wednesday_spec})
    assert bt_off._swap_pnl(wednesday_spec, Direction.LONG, 1.0, wed_2000) == pytest.approx(0.0)


def test_swap_is_credited_to_cash_and_makes_reported_pnl_match_equity(
    forex_config, eurusd_spec
) -> None:
    candles = _crafted_candles(
        _oscillating_prices(200, amplitude=0.0003), start_ts=_MONDAY_TS, half_range=0.0001
    )
    cfg_on = _small_cfg(forex_config)
    cfg_off = _small_cfg(forex_config)
    cfg_off.backtest.swap_enabled = False

    on = _run_forced(cfg_on, candles, eurusd_spec, 100, atr=0.0005)
    off = _run_forced(cfg_off, candles, eurusd_spec, 100, atr=0.0005)
    assert len(on.trades) == 1 and len(off.trades) == 1
    long_trade = on.trades[0]
    assert long_trade.funding > 0.0, "a long paying swap records a positive funding cost"
    assert off.trades[0].funding == 0.0

    # Equity differs by exactly the swap effect; sign is the cash credit.
    assert float(on.equity_curve[-1]) - float(off.equity_curve[-1]) == pytest.approx(
        -long_trade.funding, abs=1e-9
    )
    # And the reported net PnL stays consistent with the equity curve.
    assert long_trade.net_pnl == pytest.approx(
        long_trade.gross_pnl - long_trade.fees - long_trade.funding, abs=1e-12
    )


# --------------------------------------------------------------------------- #
# 6. Multi-symbol marking
# --------------------------------------------------------------------------- #
def test_each_position_is_marked_at_its_own_symbols_close(forex_config, eurusd_spec,
                                                           usdjpy_spec) -> None:
    candles = _crafted_candles(_oscillating_prices(120))
    bt = _backtester(forex_config, candles, {"EURUSD": eurusd_spec, "USDJPY": usdjpy_spec})

    def position(symbol: str, entry: float) -> _OpenPosition:
        trade = Trade(
            direction=Direction.LONG,
            entry_bar=0,
            entry_ts=0,
            entry_price=entry,
            initial_qty=0.10,
            initial_stop=entry * 0.99,
            initial_risk_per_unit=100.0,
        )
        return _OpenPosition(trade=trade, symbol=symbol, ladder=None, qty=0.10, tps_hit=[])

    positions = {"EURUSD": position("EURUSD", 1.1000), "USDJPY": position("USDJPY", 110.00)}
    # +10 pips on 0.10 lots: $10 for EURUSD, $6.667 for USDJPY.
    marks = {"EURUSD": 1.1010, "USDJPY": 110.10}
    unrealised = bt._unrealised(positions, marks)
    assert unrealised == pytest.approx(10.0 + 6.667, rel=1e-6)

    # Feeding the other symbol's price must not reproduce the same mark.
    swapped = bt._unrealised(positions, {"EURUSD": 110.10, "USDJPY": 1.1010})
    assert abs(swapped - unrealised) > 100.0


# --------------------------------------------------------------------------- #
# 7. End-of-data settlement
# --------------------------------------------------------------------------- #
def test_open_position_is_settled_with_end_of_data_and_a_warning(forex_config,
                                                                  eurusd_spec) -> None:
    cfg = _small_cfg(forex_config)
    candles = _crafted_candles(
        _oscillating_prices(150, amplitude=0.0003), half_range=0.0001
    )
    result = _run_forced(cfg, candles, eurusd_spec, 70)
    assert len(result.trades) == 1
    trade = result.trades[0]

    assert trade.fills, "the truncated trade must be closed with a real fill"
    final_fill = trade.fills[-1]
    assert final_fill.reason is ExitReason.END_OF_DATA
    assert trade.exit_bar == len(candles) - 1
    assert trade.exit_ts == int(candles.ts[-1])
    assert any("still open at the end" in warning for warning in result.warnings)
    # The last equity point is settled to realised cash.
    assert float(result.equity_curve[-1]) == pytest.approx(
        cfg.backtest.initial_equity + trade.net_pnl, abs=1e-6
    )


# --------------------------------------------------------------------------- #
# 8. Portfolio curve shape
# --------------------------------------------------------------------------- #
def test_portfolio_curve_has_one_point_per_timestamp_and_sane_exposure(forex_config) -> None:
    cfg = _small_cfg(forex_config)
    candles = synthetic_candles("EURUSD", bars=600, interval_minutes=120)
    result = _backtester(
        cfg, candles, {"EURUSD": default_instrument("EURUSD", price=float(candles.close[-1]))}
    ).run()

    assert len(result.equity_curve) == len(result.timestamps)
    assert np.all(np.isfinite(result.equity_curve))
    assert np.all(np.diff(result.timestamps) > 0), "one point per portfolio timestamp"
    assert len(np.unique(result.timestamps)) == len(result.timestamps)
    assert 0.0 <= result.metrics.exposure_pct <= 1.0
    assert result.metrics.final_equity == pytest.approx(float(result.equity_curve[-1]), abs=1e-9)
