"""Performance metrics computed from a closed-trade list and an equity curve."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Sequence

import numpy as np

from nornweave_forex.config import interval_ms
from nornweave_forex.types import ExitReason, Trade

__all__ = ["Metrics", "compute_metrics", "max_drawdown"]

_MINUTES_PER_YEAR = 365.0 * 24.0 * 60.0


def max_drawdown(equity: np.ndarray) -> tuple[float, float]:
    """Return ``(absolute, fraction)`` peak-to-trough drawdown of an equity curve."""
    if equity.size == 0:
        return 0.0, 0.0
    peak = np.maximum.accumulate(equity)
    dd = peak - equity
    abs_dd = float(dd.max())
    with np.errstate(divide="ignore", invalid="ignore"):
        frac = np.where(peak > 0, dd / peak, 0.0)
    return abs_dd, float(frac.max())


@dataclass(slots=True)
class Metrics:
    initial_equity: float = 0.0
    final_equity: float = 0.0
    net_profit: float = 0.0
    net_profit_pct: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    winrate: float = 0.0
    profit_factor: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    expectancy: float = 0.0
    expectancy_r: float = 0.0
    payoff_ratio: float = 0.0
    total_fees: float = 0.0
    total_funding: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    max_consecutive_losses: int = 0
    exposure_pct: float = 0.0
    avg_bars_held: float = 0.0
    yearly_returns: dict[str, float] = field(default_factory=dict)
    exit_breakdown: dict[str, int] = field(default_factory=dict)
    fill_breakdown: dict[str, int] = field(default_factory=dict)
    trades_reaching_tp1: int = 0
    trades_reaching_tp2: int = 0
    trades_reaching_tp3: int = 0
    trades_stopped_out: int = 0
    fills: int = 0
    fill_wins: int = 0
    fill_losses: int = 0
    fill_winrate: float = 0.0
    fill_profit_factor: float = 0.0
    fill_expectancy: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)

    def format_report(self) -> str:
        def pct(x: float) -> str:
            return f"{x:+.2%}"

        lines = [
            "=" * 62,
            "  NORN WEAVE FOREX — backtest performance",
            "=" * 62,
            f"  Starting equity        {self.initial_equity:>14,.2f}",
            f"  Final equity           {self.final_equity:>14,.2f}",
            f"  Net profit             {self.net_profit:>14,.2f}  ({pct(self.net_profit_pct)})",
            f"  Max drawdown           {self.max_drawdown:>14,.2f}  ({self.max_drawdown_pct:.2%})",
            "-" * 62,
            f"  Trades                 {self.trades:>14d}",
            f"  Wins / losses / flat   {self.wins:>6d} / {self.losses:<6d} / {self.breakeven}",
            f"  Winrate                {self.winrate:>13.2%}",
            f"  Profit factor          {self.profit_factor:>14.2f}",
            f"  Gross profit / loss    {self.gross_profit:>14,.2f} / {self.gross_loss:,.2f}",
            f"  Avg win / avg loss     {self.avg_win:>14,.2f} / {self.avg_loss:,.2f}",
            f"  Payoff ratio           {self.payoff_ratio:>14.2f}",
            f"  Expectancy / trade     {self.expectancy:>14,.2f}  ({self.expectancy_r:+.3f} R)",
            f"  Max consecutive losses {self.max_consecutive_losses:>14d}",
            "-" * 62,
            f"  Fees paid              {self.total_fees:>14,.2f}",
            f"  Swap paid              {self.total_funding:>14,.2f}",
            f"  Sharpe (annualised)    {self.sharpe:>14.2f}",
            f"  Sortino (annualised)   {self.sortino:>14.2f}",
            f"  Calmar                 {self.calmar:>14.2f}",
            f"  Time in market         {self.exposure_pct:>13.2%}",
            f"  Avg bars held          {self.avg_bars_held:>14.1f}",
        ]
        if self.fill_breakdown:
            lines.append("-" * 62)
            lines.append("  Fills by reason (partial exits included)")
            for reason, count in sorted(self.fill_breakdown.items(), key=lambda kv: -kv[1]):
                lines.append(f"    {reason:<22}{count:>8d}")
            reached = (
                f"    TP1 {self.trades_reaching_tp1} / TP2 {self.trades_reaching_tp2} "
                f"/ TP3 {self.trades_reaching_tp3} of {self.trades} trades"
            )
            lines.append(reached)
        if self.fills:
            lines.append("-" * 62)
            lines.append("  Per-fill view (every partial exit is a closed trade)")
            lines.append(f"    fills                  {self.fills:>8d}")
            lines.append(f"    fill winrate           {self.fill_winrate:>8.2%}")
            lines.append(f"    fill profit factor     {self.fill_profit_factor:>8.2f}")
            lines.append(f"    fill expectancy        {self.fill_expectancy:>8.2f}")
        if self.exit_breakdown:
            lines.append("-" * 62)
            lines.append("  Terminal exit reason (last fill of each trade)")
            for reason, count in sorted(self.exit_breakdown.items(), key=lambda kv: -kv[1]):
                lines.append(f"    {reason:<22}{count:>8d}")
        if self.yearly_returns:
            lines.append("-" * 62)
            lines.append("  Calendar year PnL (on starting equity)")
            for year, ret in sorted(self.yearly_returns.items()):
                lines.append(f"    {year:<8}{ret:>+12.2%}")
        lines.append("=" * 62)
        return "\n".join(lines)


def compute_metrics(
    trades: Sequence[Trade],
    equity_curve: np.ndarray,
    timestamps: np.ndarray,
    initial_equity: float,
    interval: str,
    bars_in_market: int = 0,
) -> Metrics:
    """Aggregate raw results into the report the brief cares about."""
    m = Metrics(initial_equity=float(initial_equity))
    equity_curve = np.asarray(equity_curve, dtype=np.float64)
    m.final_equity = float(equity_curve[-1]) if equity_curve.size else float(initial_equity)
    m.net_profit = m.final_equity - m.initial_equity
    m.net_profit_pct = m.net_profit / m.initial_equity if m.initial_equity else 0.0
    m.max_drawdown, m.max_drawdown_pct = max_drawdown(equity_curve)

    m.trades = len(trades)
    if trades:
        pnls = np.asarray([t.net_pnl for t in trades], dtype=np.float64)
        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]
        m.wins = int(wins.size)
        m.losses = int(losses.size)
        m.breakeven = int(m.trades - m.wins - m.losses)
        m.winrate = m.wins / m.trades
        m.gross_profit = float(wins.sum())
        m.gross_loss = float(-losses.sum())
        m.profit_factor = (
            m.gross_profit / m.gross_loss if m.gross_loss > 0 else float("inf") if m.gross_profit > 0 else 0.0
        )
        m.avg_win = float(wins.mean()) if wins.size else 0.0
        m.avg_loss = float(losses.mean()) if losses.size else 0.0
        m.largest_win = float(pnls.max())
        m.largest_loss = float(pnls.min())
        m.expectancy = float(pnls.mean())
        m.expectancy_r = float(np.mean([t.r_multiple for t in trades]))
        m.payoff_ratio = abs(m.avg_win / m.avg_loss) if m.avg_loss else float("inf")
        m.total_fees = float(sum(t.fees for t in trades))
        m.total_funding = float(sum(t.funding for t in trades))
        m.avg_bars_held = float(np.mean([t.bars_held for t in trades]))

        streak = best = 0
        for pnl in pnls:
            if pnl < 0:
                streak += 1
                best = max(best, streak)
            else:
                streak = 0
        m.max_consecutive_losses = best

        breakdown: dict[str, int] = {}
        fill_breakdown: dict[str, int] = {}
        for t in trades:
            key = t.fills[-1].reason.value if t.fills else "unknown"
            breakdown[key] = breakdown.get(key, 0) + 1
            for fill in t.fills:
                fill_breakdown[fill.reason.value] = fill_breakdown.get(fill.reason.value, 0) + 1
        m.exit_breakdown = breakdown
        m.fill_breakdown = fill_breakdown

        def reached(reason: ExitReason) -> int:
            return sum(1 for t in trades if any(f.reason is reason for f in t.fills))

        m.trades_reaching_tp1 = reached(ExitReason.TAKE_PROFIT_1)
        m.trades_reaching_tp2 = reached(ExitReason.TAKE_PROFIT_2)
        m.trades_reaching_tp3 = reached(ExitReason.TAKE_PROFIT_3)
        m.trades_stopped_out = reached(ExitReason.STOP_LOSS) + reached(ExitReason.BREAK_EVEN_STOP)

        fill_pnls: list[float] = []
        for trade in trades:
            exit_fees = sum(f.fee for f in trade.fills)
            entry_fee = max(0.0, trade.fees - exit_fees)
            for fill in trade.fills:
                share = fill.qty / trade.initial_qty if trade.initial_qty > 0 else 0.0
                fill_pnls.append(fill.pnl - entry_fee * share)
        if fill_pnls:
            arr = np.asarray(fill_pnls, dtype=np.float64)
            fwins = arr[arr > 0]
            flosses = arr[arr < 0]
            m.fills = int(arr.size)
            m.fill_wins = int(fwins.size)
            m.fill_losses = int(flosses.size)
            m.fill_winrate = m.fill_wins / m.fills
            gross_win = float(fwins.sum())
            gross_los = float(-flosses.sum())
            m.fill_profit_factor = (
                gross_win / gross_los
                if gross_los > 0
                else (float("inf") if gross_win > 0 else 0.0)
            )
            m.fill_expectancy = float(arr.mean())

    if equity_curve.size > 1:
        rets = np.diff(equity_curve) / np.maximum(equity_curve[:-1], 1e-12)
        bars_per_year = _MINUTES_PER_YEAR / (interval_ms(interval) / 60_000.0)
        std = float(rets.std(ddof=1)) if rets.size > 1 else 0.0
        m.sharpe = float(rets.mean() / std * np.sqrt(bars_per_year)) if std > 0 else 0.0
        downside = rets[rets < 0]
        dstd = float(downside.std(ddof=1)) if downside.size > 1 else 0.0
        m.sortino = float(rets.mean() / dstd * np.sqrt(bars_per_year)) if dstd > 0 else 0.0
        m.calmar = (
            float(m.net_profit_pct / m.max_drawdown_pct) if m.max_drawdown_pct > 1e-12 else 0.0
        )
        m.exposure_pct = bars_in_market / max(1, equity_curve.size - 1)

    if trades and timestamps.size:
        by_year: dict[str, float] = {}
        for t in trades:
            year = str(_year_utc(t.entry_ts))
            by_year[year] = by_year.get(year, 0.0) + t.net_pnl
        m.yearly_returns = {
            year: value / m.initial_equity if m.initial_equity else 0.0
            for year, value in by_year.items()
        }

    return m


def _year_utc(ts_ms: int) -> int:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts_ms / 1000.0, tz=_dt.timezone.utc).year
