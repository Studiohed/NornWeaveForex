"""Position sizing for a multi-symbol Forex account.

Differences from the crypto sizing module:

* **Quantity is lots, not units.** A position of "0.10" means 0.10 standard
  lots — 10,000 units of the base currency at the usual ``contract_size`` of
  100,000.
* **Risk is measured in pips.** The size of the position is
  ``risk_amount / (stop_distance_pips * pip_value_per_lot)``. The pip value
  comes from :class:`~nornweave_forex.instrument.InstrumentSpec` and is the
  broker's own number — never a hardcoded $10.
* **Margin is account-currency notional, not base-currency notional.** One lot of
  USDJPY is 100,000 *USD* of notional; one lot of EURJPY is 100,000 *EUR*
  converted through the quote currency. The conversion is delegated to
  :meth:`InstrumentSpec.notional_account`, which derives it from the pip value
  MT5 reported (``price * lots * pip_value / pip``). There is deliberately no
  ``entry < 10`` guesswork: that heuristic is exact for EURUSD/USDJPY and
  understates margin for every JPY cross.
* **The margin cap always runs.** Live callers pass the broker's
  ``free_margin``; when they do not, the documented fallback is
  ``max_notional = equity * risk.leverage`` (the same shape the crypto sizing
  used). A cap can only ever reduce the risk-sized lot amount, and if it reduces
  it below the broker's ``min_lot`` the trade is refused instead of being
  silently bumped back over the account's margin.
* **Portfolio cap.** With up to N positions open at once, each new entry is
  capped so the sum of all open risks does not exceed
  ``risk.portfolio_risk_pct`` of equity.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from nornweave_forex.config import RiskParams
from nornweave_forex.instrument import InstrumentSpec

__all__ = ["SizeDecision", "position_size"]


def _dec(value: float) -> Decimal:
    """Decimal from the shortest repr, so 1.10 - 1.09 is exactly 0.01."""
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class SizeDecision:
    lots: float
    notional_base: float  # in base currency units (lots * contract_size)
    risk_amount: float  # in account currency
    margin_required: float  # in account currency
    rejected: str | None = None

    @property
    def ok(self) -> bool:
        return self.rejected is None and self.lots > 0


def position_size(
    equity: float,
    entry: float,
    stop: float,
    instrument: InstrumentSpec,
    risk: RiskParams,
    *,
    open_risk: float = 0.0,
    free_margin: float | None = None,
) -> SizeDecision:
    """Size a Forex position.

    ``equity`` and ``open_risk`` are in the account currency. ``open_risk`` is
    the sum of ``|entry - stop| * lots * pip_value / pip`` over every other
    open position — the number the portfolio cap is enforced against.

    ``free_margin`` is the broker's available margin in account currency (from
    ``account_info()['margin_free']``) and is the authoritative cap. When it is
    ``None`` the fallback cap is ``equity * risk.leverage`` of account-currency
    notional, which keeps the function usable offline; the margin actually
    reserved is then ``notional_account(entry, lots) / leverage``.
    """
    if equity <= 0:
        return SizeDecision(0.0, 0.0, 0.0, 0.0, "non_positive_equity")
    if not entry > 0:
        return SizeDecision(0.0, 0.0, 0.0, 0.0, "non_positive_entry")
    if instrument.pip <= 0:
        return SizeDecision(0.0, 0.0, 0.0, 0.0, "invalid_pip")
    if instrument.pip_value_per_lot <= 0:
        return SizeDecision(0.0, 0.0, 0.0, 0.0, "invalid_pip_value")

    # Compute pips in Decimal from the string forms of the prices. In binary
    # floats an exact 100-pip stop is 100.0000000000012 pips, and flooring that
    # target 0.10 lots against a 0.01 lot step yields 0.09 (or 0.01 -> 0.00 at
    # smaller accounts): a silent one-step under-size on boundary inputs.
    stop_distance = _dec(entry) - _dec(stop)
    if stop_distance < 0:
        stop_distance = -stop_distance
    if stop_distance <= 0:
        return SizeDecision(0.0, 0.0, 0.0, 0.0, "zero_stop_distance")
    stop_pips = stop_distance / _dec(instrument.pip)

    # Per-trade risk, then portfolio cap.
    per_trade_risk = equity * risk.risk_per_trade_pct
    portfolio_cap = equity * getattr(risk, "portfolio_risk_pct", risk.risk_per_trade_pct)
    remaining_portfolio = max(0.0, portfolio_cap - open_risk)
    allowed_risk = min(per_trade_risk, remaining_portfolio)
    if allowed_risk <= 0:
        return SizeDecision(0.0, 0.0, 0.0, 0.0, "portfolio_risk_exhausted")

    # lots = risk / (pips_at_risk * pip_value_per_lot)
    risk_per_lot_dec = stop_pips * _dec(instrument.pip_value_per_lot)
    if risk_per_lot_dec <= 0:
        return SizeDecision(0.0, 0.0, 0.0, 0.0, "invalid_risk_per_lot")
    risk_per_lot = float(risk_per_lot_dec)
    lots = float(_dec(allowed_risk) / risk_per_lot_dec)

    leverage = max(1e-9, float(risk.leverage))

    def margin_for(lot_amount: float) -> float:
        """Margin in account currency for ``lot_amount`` at ``entry``."""
        return instrument.notional_account(entry, lot_amount) / leverage

    margin_capped = False

    def apply_margin_cap(lot_amount: float) -> float:
        """Scale ``lot_amount`` down until it fits the margin budget."""
        nonlocal margin_capped
        if lot_amount <= 0:
            return 0.0
        if free_margin is not None:
            available = max(0.0, float(free_margin))
            required = margin_for(lot_amount)
            if required > 0 and required > available:
                lot_amount *= available / required
                margin_capped = True
        else:
            max_notional = max(0.0, equity * leverage)
            notional = instrument.notional_account(entry, lot_amount)
            if notional > 0 and notional > max_notional:
                lot_amount *= max_notional / notional
                margin_capped = True
        return lot_amount

    lots = apply_margin_cap(lots)
    if margin_capped and lots < instrument.min_lot:
        # The account cannot afford the smallest legal order at this stop; refuse
        # rather than bumping to min_lot (which would exceed the margin budget).
        return SizeDecision(0.0, 0.0, 0.0, 0.0, "insufficient_margin")

    # Broker minimum lot: same martingale warning as the crypto version.
    if lots < instrument.min_lot:
        floor_lots = instrument.min_lot
        floor_risk = floor_lots * risk_per_lot
        cap = risk.max_risk_per_trade_pct
        if cap > 0 and floor_risk > equity * cap:
            return SizeDecision(0.0, 0.0, 0.0, 0.0, "min_lot_risk_above_cap")
        # The portfolio cap still applies: bumping to the minimum must not
        # exceed the portfolio budget either.
        if floor_risk > remaining_portfolio:
            return SizeDecision(0.0, 0.0, 0.0, 0.0, "min_lot_risk_above_portfolio")
        lots = floor_lots
        # A minimum-size order must still fit the margin budget.
        if free_margin is not None:
            if margin_for(lots) > max(0.0, float(free_margin)):
                return SizeDecision(0.0, 0.0, 0.0, 0.0, "insufficient_margin")
        elif instrument.notional_account(entry, lots) > equity * leverage:
            return SizeDecision(0.0, 0.0, 0.0, 0.0, "insufficient_margin")

    lots = instrument.round_lots(lots)
    if lots <= 0:
        return SizeDecision(0.0, 0.0, 0.0, 0.0, "zero_lots_after_rounding")
    if lots > instrument.max_lot:
        lots = instrument.round_lots(instrument.max_lot)

    # Recompute after max_lot clamp/rounding: rounding can only reduce, so this
    # is a no-op in the normal path, but a broker with a coarse lot step could
    # otherwise return a size that does not fit the cap.
    margin_required = margin_for(lots)
    if free_margin is not None:
        available = max(0.0, float(free_margin))
        if margin_required > available and margin_required > 0:
            lots *= available / margin_required
            lots = instrument.round_lots(lots)
            if lots < instrument.min_lot:
                return SizeDecision(0.0, 0.0, 0.0, 0.0, "insufficient_margin")
            margin_required = margin_for(lots)
    else:
        max_notional = max(0.0, equity * leverage)
        notional = instrument.notional_account(entry, lots)
        if notional > max_notional and notional > 0:
            lots *= max_notional / notional
            lots = instrument.round_lots(lots)
            if lots < instrument.min_lot:
                return SizeDecision(0.0, 0.0, 0.0, 0.0, "insufficient_margin")
            margin_required = margin_for(lots)

    notional_base = lots * instrument.contract_size
    risk_amount = lots * risk_per_lot

    return SizeDecision(
        lots=float(lots),
        notional_base=float(notional_base),
        risk_amount=float(risk_amount),
        margin_required=float(margin_required),
    )
