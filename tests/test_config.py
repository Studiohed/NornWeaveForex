"""The config contract for the Forex port.

Two jobs:

* pin the shipped ``configs/forex.yaml`` (Forex cost model, majors list, risk
  defaults) so a crypto value cannot quietly reappear;
* lock the validation rules that make a bad config fail at load time instead of
  at 3 a.m. in the live runner — intraday-only intervals, a coherent watchlist
  and a portfolio budget that can actually host ``max_positions``.

Every rejection is asserted through ``Config.validate()`` (the same path
``Config.from_yaml`` takes), so these tests describe the contract, not an
implementation detail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nornweave_forex.config import (
    BacktestParams,
    Config,
    ConfigError,
    PortfolioParams,
    RiskParams,
    StrategyParams,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FOREX_YAML = REPO_ROOT / "configs" / "forex.yaml"

MAJORS = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD",
    "USDCHF",
    "NZDUSD",
    "EURJPY",
)


def valid_config(**overrides) -> Config:
    """A minimal config that validates, with the given top-level fields replaced."""
    cfg = Config()
    for name, value in overrides.items():
        setattr(cfg, name, value)
    return cfg


def expect_rejected(cfg: Config, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        cfg.validate()


# --------------------------------------------------------------------------- #
# the shipped file
# --------------------------------------------------------------------------- #
def test_the_shipped_config_loads_and_validates():
    cfg = Config.from_yaml(str(FOREX_YAML))
    assert isinstance(cfg, Config)


def test_shipped_config_is_the_eight_majors_with_two_slots():
    cfg = Config.from_yaml(str(FOREX_YAML))
    assert cfg.portfolio.symbols == MAJORS
    assert len(cfg.portfolio.symbols) == 8
    assert cfg.portfolio.max_positions == 2
    # the legacy single-symbol field must agree with the watchlist
    assert cfg.symbol == "EURUSD"
    assert cfg.symbol in cfg.portfolio.symbols
    assert cfg.symbols == cfg.portfolio.symbols


def test_shipped_config_has_the_forex_cost_model():
    cfg = Config.from_yaml(str(FOREX_YAML))
    # Alfa-Forex majors are spread-only: the engine charges the spread itself, so
    # any non-zero taker fee would double-count execution cost.
    assert cfg.backtest.taker_fee == 0.0
    assert cfg.backtest.maker_fee == 0.0
    assert cfg.backtest.swap_enabled is True
    assert cfg.backtest.slippage_bps == pytest.approx(1.0)
    assert cfg.backtest.intrabar_priority == "sl_first"


def test_shipped_config_has_forex_scaled_risk():
    cfg = Config.from_yaml(str(FOREX_YAML))
    assert cfg.interval == "120"
    # A 1% stop is ~100 pips on EURUSD; the crypto -5% default would be ~500.
    assert cfg.risk.stop_loss_pct == pytest.approx(-0.01)
    assert cfg.risk.be_trigger_pct == pytest.approx(0.015)
    assert cfg.risk.risk_per_trade_pct == pytest.approx(0.01)
    assert cfg.risk.portfolio_risk_pct == pytest.approx(0.05)
    assert cfg.risk.leverage == pytest.approx(30.0)
    assert cfg.strategy.daily_filter_enabled is True
    assert cfg.tuning.enabled is False


def test_the_built_in_defaults_are_valid():
    """A zero-argument ``Config()`` must be self-consistent, not just loadable."""
    Config().validate()


def test_a_missing_config_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        Config.from_yaml(str(tmp_path / "nope.yaml"))


# --------------------------------------------------------------------------- #
# intervals: the daily overlay needs an intraday series
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("interval", ["W", "M"])
def test_weekly_and_monthly_intervals_are_rejected(interval):
    expect_rejected(valid_config(interval=interval), "longer than a day")


@pytest.mark.parametrize("interval", ["1", "5", "15", "60", "120", "240"])
def test_intraday_intervals_are_accepted(interval):
    valid_config(interval=interval).validate()


def test_an_interval_of_exactly_one_day_is_still_allowed():
    # ``interval_ms("D") == DAY_MS`` is not *longer* than a day, and the daily
    # aggregation still has one bar per day to work with.
    valid_config(interval="D").validate()


def test_an_unsupported_interval_is_rejected():
    with pytest.raises(ConfigError, match="unsupported interval"):
        valid_config(interval="7").validate()


# --------------------------------------------------------------------------- #
# watchlist coherence
# --------------------------------------------------------------------------- #
def test_duplicate_symbols_are_rejected():
    expect_rejected(
        valid_config(portfolio=PortfolioParams(symbols=("EURUSD", "EURUSD"), max_positions=1)),
        "unique",
    )


def test_empty_symbols_are_rejected():
    expect_rejected(valid_config(portfolio=PortfolioParams(symbols=(), max_positions=1)), "empty")


def test_max_positions_above_the_watchlist_is_rejected():
    expect_rejected(
        valid_config(portfolio=PortfolioParams(symbols=("EURUSD",), max_positions=2)),
        "exceeds the number of watched symbols",
    )


def test_zero_max_positions_is_rejected():
    expect_rejected(
        valid_config(portfolio=PortfolioParams(symbols=MAJORS, max_positions=0)),
        "max_positions",
    )


def test_symbol_outside_the_watchlist_is_rejected():
    expect_rejected(valid_config(symbol="XAUUSD"), "not in portfolio.symbols")


def test_symbol_matching_is_case_insensitive():
    cfg = Config(symbol="eurusd")  # normalised in __post_init__
    assert cfg.symbol == "EURUSD"
    cfg.validate()


# --------------------------------------------------------------------------- #
# the portfolio budget must be able to host every slot
# --------------------------------------------------------------------------- #
def test_portfolio_budget_too_small_for_max_positions_is_rejected():
    cfg = valid_config(
        risk=RiskParams(
            stop_loss_pct=-0.01,
            be_trigger_pct=0.015,
            risk_per_trade_pct=0.01,
            portfolio_risk_pct=0.02,
        ),
        portfolio=PortfolioParams(symbols=MAJORS, max_positions=5),
    )
    expect_rejected(cfg, "cannot host")


def test_portfolio_budget_that_exactly_hosts_max_positions_is_accepted():
    cfg = valid_config(
        risk=RiskParams(
            stop_loss_pct=-0.01,
            be_trigger_pct=0.015,
            risk_per_trade_pct=0.01,
            portfolio_risk_pct=0.05,
        ),
        portfolio=PortfolioParams(symbols=MAJORS, max_positions=5),
    )
    cfg.validate()


def test_portfolio_risk_below_one_trade_is_rejected_even_for_one_slot():
    cfg = valid_config(
        risk=RiskParams(
            stop_loss_pct=-0.01,
            be_trigger_pct=0.015,
            risk_per_trade_pct=0.02,
            portfolio_risk_pct=0.01,
        )
    )
    expect_rejected(cfg, "portfolio_risk_pct")


# --------------------------------------------------------------------------- #
# enum knobs: a typo must not silently disable a filter
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "field,value",
    [
        ("stop_trigger", "sideways"),
        ("exit_fill", "mid"),
        ("exit_order", "random"),
        ("be_mode", "always"),
        ("be_arm_on", "never"),
        ("be_exit_trigger", "wick"),
        ("tp_anchor", "signal-bar"),
        ("tp_weight_basis", "remaining"),
        ("size_mode", "max"),
    ],
)
def test_bad_risk_enums_are_rejected(field, value):
    cfg = valid_config(
        risk=RiskParams(stop_loss_pct=-0.01, be_trigger_pct=0.015, **{field: value})
    )
    expect_rejected(cfg, field)


@pytest.mark.parametrize(
    "field,value",
    [
        ("dow_mode", "vibes"),
        ("dow_neutral_mode", "hold"),
        ("daily_fib_mode", "magic"),
        ("daily_trend_mode", "vibes"),
        ("daily_missing_policy", "ignore"),
        ("daily_neutral_policy", "ignore"),
        ("reversal_rule", "trailing"),
        ("ema_slope_mode", "both"),
    ],
)
def test_bad_strategy_enums_are_rejected(field, value):
    cfg = valid_config(strategy=StrategyParams(**{field: value}))
    expect_rejected(cfg, field)


def test_bad_intrabar_priority_is_rejected():
    cfg = valid_config(backtest=BacktestParams(intrabar_priority="coin_flip"))
    expect_rejected(cfg, "intrabar_priority")


def test_positive_stop_loss_is_rejected():
    cfg = valid_config(risk=RiskParams(stop_loss_pct=0.01, be_trigger_pct=0.015))
    expect_rejected(cfg, "stop_loss_pct")


def test_tuning_stop_bounds_must_be_ordered():
    from nornweave_forex.config import TuningParams

    # floor must sit below the cap: -0.02 is *above* -0.10.
    cfg = valid_config()
    cfg.tuning = TuningParams(stop_floor_pct=-0.02, stop_cap_pct=-0.10)
    expect_rejected(cfg, "stop bounds")


# --------------------------------------------------------------------------- #
# unknown YAML keys: a typo must fail, never be ignored
# --------------------------------------------------------------------------- #
def test_unknown_top_level_yaml_key_is_rejected(tmp_path):
    path = tmp_path / "typo.yaml"
    path.write_text('interval: "120"\nportoflio:\n  max_positions: 2\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown top-level key"):
        Config.from_yaml(str(path))


def test_unknown_nested_yaml_key_is_rejected(tmp_path):
    path = tmp_path / "typo.yaml"
    path.write_text("strategy:\n  adx_threshhold: 20.5\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_yaml(str(path))


def test_a_yaml_file_with_an_invalid_rule_fails_at_load(tmp_path):
    """Validation must run inside from_yaml, not only when the caller remembers."""
    path = tmp_path / "weekly.yaml"
    path.write_text('interval: "W"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="longer than a day"):
        Config.from_yaml(str(path))


def test_unknown_portfolio_key_is_rejected(tmp_path):
    path = tmp_path / "typo.yaml"
    path.write_text("portfolio:\n  symbols: [EURUSD]\n  max_position: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown key"):
        Config.from_yaml(str(path))


# --------------------------------------------------------------------------- #
# round-trip: describe() and to_dict() must not lose the Forex fields
# --------------------------------------------------------------------------- #
def test_to_dict_keeps_the_forex_fields():
    cfg = Config.from_yaml(str(FOREX_YAML))
    data = cfg.to_dict()
    assert tuple(data["portfolio"]["symbols"]) == MAJORS
    assert data["portfolio"]["max_positions"] == 2
    assert data["backtest"]["swap_enabled"] is True
    assert data["backtest"]["taker_fee"] == 0.0


def test_describe_reports_the_watchlist_not_a_single_crypto_symbol():
    text = Config.from_yaml(str(FOREX_YAML)).describe()
    assert "EURUSD" in text
    assert "DOGEUSDT" not in text
    assert "max_positions=2" in text
    assert "swap=on" in text
