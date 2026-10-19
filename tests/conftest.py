# Fixture helpers shared by the Forex port's test suite.

from __future__ import annotations

import numpy as np
import pytest

from nornweave_forex.config import Config, RiskParams, StrategyParams, BacktestParams, PortfolioParams
from nornweave_forex.instrument import InstrumentSpec
from nornweave_forex.types import CandleArrays


@pytest.fixture()
def eurusd_spec() -> InstrumentSpec:
    """A realistic EURUSD spec as MT5 reports it on a 5-digit account."""
    return InstrumentSpec(
        symbol="EURUSD",
        digits=5,
        point=0.00001,
        pip=0.0001,
        pip_value_per_lot=10.0,
        contract_size=100_000.0,
        lot_step=0.01,
        min_lot=0.01,
        max_lot=50.0,
        swap_long=-5.0,
        swap_short=2.0,
        swap_triple_day=2,  # Python weekday: Wednesday
        typical_spread_points=10.0,
        quote_currency="USD",
    )


@pytest.fixture()
def usdjpy_spec() -> InstrumentSpec:
    """A 3-digit JPY pair — pip is 0.01, point is 0.001."""
    return InstrumentSpec(
        symbol="USDJPY",
        digits=3,
        point=0.001,
        pip=0.01,
        # 1000 / USDJPY at 150: JPY pip values float, they are never $10.
        pip_value_per_lot=6.667,
        contract_size=100_000.0,
        lot_step=0.01,
        min_lot=0.01,
        max_lot=50.0,
        swap_long=-4.0,
        swap_short=1.5,
        swap_triple_day=2,  # Python weekday: Wednesday
        typical_spread_points=18.0,
        quote_currency="JPY",
    )


@pytest.fixture()
def forex_config() -> Config:
    cfg = Config()
    cfg.symbol = "EURUSD"
    cfg.interval = "120"
    cfg.portfolio = PortfolioParams(
        symbols=("EURUSD", "GBPUSD", "USDJPY"),
        max_positions=2,
    )
    cfg.risk = RiskParams(
        stop_loss_pct=-0.02,
        be_trigger_pct=0.03,
        risk_per_trade_pct=0.01,
        portfolio_risk_pct=0.05,
        leverage=30.0,
    )
    cfg.backtest = BacktestParams(
        initial_equity=10_000.0,
        taker_fee=0.0,       # spread is modelled separately; commission is 0 on most MT5 Forex accounts
        slippage_bps=0.0,
        warmup_bars=200,
        preroll_bars=300,
    )
    return cfg
