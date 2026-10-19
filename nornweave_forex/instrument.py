"""Forex instrument metadata: digits, point, lot step, contract size, swap.

A Forex pair is *not* a crypto perpetual. The differences that matter to this bot:

* **Lots, not coins.** Order quantity is a multiple of ``lot_step``, between
  ``min_lot`` and ``max_lot``. One lot is ``contract_size`` units of the base
  currency — almost always 100,000.
* **Pips, not ticks.** The price moves in ``point`` increments; a "pip" is
  conventionally ``10 * point`` on 5-digit brokers and ``point`` on 3-digit
  (JPY) brokers. We expose both so callers do not guess.
* **Pip value is pair-dependent.** For ``XXXUSD`` it is fixed at $10 per
  standard lot per pip. For ``USDJPY`` it floats with USDJPY itself. For
  crosses it depends on both legs. We pull ``SYMBOL_TRADE_TICK_VALUE`` from
  MT5 at startup and never hardcode it.
* **Swap instead of funding.** Holding past the rollover books ``swap_long``
  or ``swap_short`` per lot per night, tripled on the broker's rollover day.
* **Digits.** All prices are rounded to ``digits`` decimals; ``round_price``
  is the only correct way to do that.

The MT5 side returns every numeric field as a native Python float via RPyC, so
no Decimal-string parsing is needed here (unlike Bybit).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any, Mapping

__all__ = ["InstrumentSpec", "pip_size", "default_instrument", "quote_currency"]

#: Reference USD rates used only by :func:`default_instrument` (offline demo/tests).
#: Live mode never uses these: ``pip_value_per_lot`` comes from MT5's
#: ``SYMBOL_TRADE_TICK_VALUE``, which is already denominated in account currency.
_REFERENCE_USD_RATES: dict[str, float] = {
    "USD": 1.0,
    "JPY": 1.0 / 150.0,
    "CAD": 1.0 / 1.36,
    "CHF": 1.0 / 0.88,
    "GBP": 1.27,
    "AUD": 0.66,
    "NZD": 0.61,
    "EUR": 1.09,
}

#: MT5 ``ENUM_SYMBOL_SWAP_MODE``: only POINTS needs a unit conversion; the other
#: modes are already account currency per lot.
SWAP_MODE_POINTS = 1


def quote_currency(symbol: str) -> str:
    """The quote (profit) currency of a 6-letter FX symbol, e.g. ``JPY`` for USDJPY."""
    symbol = symbol.upper()
    return symbol[3:6] if len(symbol) >= 6 else ""


def _mt5_weekday_to_python(value: Any) -> int:
    """Convert MT5's ``ENUM_DAY_OF_WEEK`` (Sunday=0) to Python's weekday (Monday=0).

    The two conventions differ by one, and getting it wrong books the triple swap
    on Thursday instead of Wednesday. Missing metadata falls back to Wednesday.
    """
    if value is None:
        return 2
    try:
        day = int(value)
    except (TypeError, ValueError):
        return 2
    if not 0 <= day <= 6:
        return 2
    return (day + 6) % 7


def default_instrument(
    symbol: str,
    *,
    price: float | None = None,
    reference_rates: Mapping[str, float] | None = None,
) -> InstrumentSpec:
    """A realistic spec for the 8 majors when MT5 metadata is unavailable.

    ``pip_value_per_lot`` must never be hardcoded: for a USD-quoted pair it is
    $10/lot, for USDJPY it is ``1000 / USDJPY``, for USDCAD ``10 / USDCAD`` and for
    EURJPY it is ``1000 / USDJPY`` as well. This function derives that from the
    pair's quote currency and a reference rate table, so offline backtests and the
    demo are internally consistent. Live mode uses :meth:`InstrumentSpec.from_mt5`.
    """
    symbol = symbol.upper()
    quote = quote_currency(symbol)
    rates = dict(_REFERENCE_USD_RATES)
    if reference_rates:
        rates.update({k.upper(): float(v) for k, v in reference_rates.items()})
    jpy_quote = quote == "JPY"
    digits = 3 if jpy_quote else 5
    pip = 0.01 if jpy_quote else 0.0001
    point = pip / 10.0
    contract = 100_000.0
    # USD per one unit of the quote currency (the table is already in that form).
    usd_per_quote = 1.0 if quote == "USD" else rates.get(quote, 1.0)
    pip_value = contract * pip * usd_per_quote
    return InstrumentSpec(
        symbol=symbol,
        digits=digits,
        point=point,
        pip=pip,
        pip_value_per_lot=pip_value,
        contract_size=contract,
        lot_step=0.01,
        min_lot=0.01,
        max_lot=50.0,
        # Typical Alfa-Forex swap, per lot per night, in account currency.
        swap_long=-5.0 if not jpy_quote else -3.0,
        swap_short=2.0 if not jpy_quote else 1.0,
        swap_triple_day=2,  # Python weekday: 2 = Wednesday
        typical_spread_points=10.0 if not jpy_quote else 18.0,
        quote_currency=quote,
    )


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _places(step: Decimal) -> int:
    exponent = step.normalize().as_tuple().exponent
    return max(0, -int(exponent))


def pip_size(digits: int, point: float) -> float:
    """Return the size of one *pip* in price units.

    Convention: on a 5-digit (or 3-digit JPY) broker a pip is ``10 * point``.
    On a 4-digit (or 2-digit JPY) broker a pip equals ``point``.
    """
    if digits in (3, 5):
        return float(point) * 10.0
    return float(point)


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Symbol metadata for one Forex pair."""

    symbol: str
    #: Number of decimal places in a quoted price (5 for EURUSD on most brokers).
    digits: int
    #: Smallest price increment, e.g. 0.00001 for EURUSD on a 5-digit broker.
    point: float
    #: Size of one pip in price units (10 * point on 5/3-digit brokers).
    pip: float
    #: Value of one pip for one standard lot, in the account currency.
    pip_value_per_lot: float
    #: One lot = ``contract_size`` units of the base currency (almost always 100_000).
    contract_size: float
    #: Order quantity is a multiple of this (usually 0.01).
    lot_step: float
    min_lot: float
    max_lot: float
    #: Swap per lot per night, in the account currency.
    swap_long: float
    swap_short: float
    #: Day of week when swap is booked 3x (3 = Wednesday is the most common).
    swap_triple_day: int = 3
    #: Typical spread in points, used by the backtester. The live side reads
    #: the actual spread from the market on every tick.
    typical_spread_points: float = 0.0
    #: ISO code of the profit currency (``JPY`` for USDJPY), used only for
    #: diagnostics and for the offline fallback pip value.
    quote_currency: str = ""
    #: True when MT5 reports swap in *points* (``SYMBOL_SWAP_MODE_POINTS``),
    #: in which case :meth:`swap_money` converts to account currency.
    swap_in_points: bool = False

    # -- rounding ----------------------------------------------------------- #
    def round_lots(self, lots: float) -> float:
        """Floor ``lots`` to the broker's lot step.

        Flooring is right for anything that *reduces* a position. For an opening
        order that must clear ``min_lot``, see :meth:`round_lots_to_meet`.
        """
        if lots <= 0:
            return 0.0
        step = _dec(self.lot_step)
        value = (Decimal(str(lots)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        return float(round(value, _places(step)))

    def round_lots_to_meet(self, lots: float) -> float:
        """Snap ``lots`` to the lot grid, rounding *up* if that is what it takes
        to clear ``min_lot``. Mirrors the crypto ``round_qty_to_meet`` for the
        same reason: a floor can produce an order the broker rejects.
        """
        lots = self.round_lots(lots)
        if lots >= self.min_lot:
            return lots
        step = _dec(self.lot_step)
        candidate = lots
        for _ in range(10_000):
            candidate = float(round(Decimal(str(candidate)) + step, _places(step)))
            if candidate >= self.min_lot:
                return candidate
        return candidate

    def round_price(self, price: float) -> float:
        """Snap ``price`` to the broker's ``digits``."""
        if price <= 0:
            return 0.0
        return round(float(price), self.digits)

    def pips_between(self, a: float, b: float) -> float:
        """How many pips separate two prices (sign-aware)."""
        if self.pip <= 0:
            return 0.0
        return (b - a) / self.pip

    def accepts(self, lots: float, price: float, *, reduce_only: bool = False) -> tuple[bool, str | None]:
        """Whether ``lots`` at ``price`` is a legal order.

        The ``reduce_only`` exemption mirrors the crypto version: closing a
        position must never be blocked locally, even if the remainder fell
        below ``min_lot`` after partial exits.
        """
        if lots <= 0:
            return False, "lots_below_zero"
        if lots > self.max_lot:
            return False, "lots_above_max"
        if not reduce_only and lots < self.min_lot:
            return False, "lots_below_min"
        return True, None

    def notional_in_base(self, lots: float) -> float:
        """How many units of the base currency ``lots`` controls."""
        return lots * self.contract_size

    # -- money -------------------------------------------------------------- #
    def pnl_money(self, entry: float, exit: float, lots: float, sign: int) -> float:
        """Realised PnL in **account currency**.

        ``(exit - entry) / pip * pip_value_per_lot * lots`` with ``sign`` = +1 for
        a long and -1 for a short. ``pip_value_per_lot`` is MT5's
        ``trade_tick_value`` rescaled to one pip, so the result is already in the
        account currency and needs no further FX conversion.

        This is the single conversion the whole stack must use: after the Forex
        port ``qty`` is lots, so ``(exit - entry) * qty`` alone is off by
        ``contract_size`` (~100,000x).
        """
        if self.pip <= 0:
            raise ValueError(f"{self.symbol}: pip must be > 0")
        return (float(exit) - float(entry)) / self.pip * self.pip_value_per_lot * float(lots) * sign

    def notional_account(self, price: float, lots: float) -> float:
        """Notional value of ``lots`` at ``price`` in account currency.

        ``lots * contract_size * price`` is expressed in the *quote* currency;
        dividing by the quote currency's USD rate turns it into account currency.
        The pip value already carries that rate: ``pip_value = contract * pip *
        usd_per_quote``, hence ``notional = price * lots * pip_value / pip``.
        """
        if self.pip <= 0:
            raise ValueError(f"{self.symbol}: pip must be > 0")
        return abs(float(price)) * float(lots) * self.pip_value_per_lot / self.pip

    def swap_money(self, *, sign: int, lots: float, tripled: bool = False) -> float:
        """Overnight swap in account currency for ``lots``, one rollover.

        ``swap_long``/``swap_short`` are per lot per night as MT5 reports them.
        When the broker uses ``SYMBOL_SWAP_MODE_POINTS`` they are in points, not
        account currency, so they are converted through the point value.
        Positive numbers are a credit, negative a debit, for both sides.
        """
        raw = self.swap_long if sign > 0 else self.swap_short
        per_lot = raw * self.point * self.pip_value_per_lot / self.pip if self.swap_in_points else raw
        return per_lot * float(lots) * (3.0 if tripled else 1.0)

    # -- MT5 parsing -------------------------------------------------------- #
    @classmethod
    def from_mt5(cls, payload: Mapping[str, Any]) -> "InstrumentSpec":
        """Build from an ``MT5SymbolInfo`` dict produced by the RPyC server.

        The server is responsible for filling every key; this class refuses to
        guess. ``pip_value_per_lot`` is derived from ``trade_tick_value`` and
        ``trade_tick_size`` because that is the only broker-truth source: the
        value of one point move for one lot, denominated in the account currency.
        """
        digits = int(payload["digits"])
        point = float(payload["point"])
        pip = pip_size(digits, point)
        tick_value = float(payload["trade_tick_value"])
        tick_size = float(payload["trade_tick_size"])
        if tick_size <= 0:
            raise ValueError(f"{payload.get('symbol')}: trade_tick_size must be > 0")
        # pip / tick_size tells how many ticks fit in one pip; multiply by the
        # tick value to get the pip value.
        pip_value = tick_value * (pip / tick_size)
        symbol = str(payload["symbol"])
        return cls(
            symbol=symbol,
            digits=digits,
            point=point,
            pip=pip,
            pip_value_per_lot=pip_value,
            contract_size=float(payload.get("trade_contract_size", 100_000.0)),
            lot_step=float(payload.get("volume_step", 0.01)),
            min_lot=float(payload.get("volume_min", 0.01)),
            max_lot=float(payload.get("volume_max", 100.0)),
            swap_long=float(payload.get("swap_long", 0.0)),
            swap_short=float(payload.get("swap_short", 0.0)),
            swap_triple_day=_mt5_weekday_to_python(payload.get("swap_rollover3days", payload.get("swap_triple_day"))),
            typical_spread_points=float(payload.get("spread", 0.0)),
            quote_currency=str(payload.get("currency_profit") or quote_currency(symbol)),
            swap_in_points=int(payload.get("swap_mode") or 0) == SWAP_MODE_POINTS,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "digits": self.digits,
            "point": self.point,
            "pip": self.pip,
            "pip_value_per_lot": self.pip_value_per_lot,
            "contract_size": self.contract_size,
            "lot_step": self.lot_step,
            "min_lot": self.min_lot,
            "max_lot": self.max_lot,
            "swap_long": self.swap_long,
            "swap_short": self.swap_short,
            "swap_triple_day": self.swap_triple_day,
            "typical_spread_points": self.typical_spread_points,
            "quote_currency": self.quote_currency,
            "swap_in_points": self.swap_in_points,
        }
