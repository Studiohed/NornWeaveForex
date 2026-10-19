"""Order execution abstractions, Forex edition.

The broker's whole job is to turn an :class:`OrderRequest` into a fill. Keeping it
that narrow means the paper broker and the real one share one interface and one set
of bar rules.

Differences from the crypto version:

* ``qty`` is **lots**, not base-currency units. A value of 0.10 means 0.10
  standard lots.
* ``reduce_only`` maps to MT5's ``ORDER_TYPE_CLOSE_BY``/position-close semantics,
  not a flag on the order.
* There is no leverage filter on the broker side — the account's own leverage
  (from ``account_info``) is what the broker enforces; the sizing layer already
  caps by margin.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

from nornweave_forex.instrument import InstrumentSpec
from nornweave_forex.live.state import LiveFill
from nornweave_forex.types import Direction

if TYPE_CHECKING:  # pragma: no cover
    from nornweave_forex.live.state import OpenPositionState

__all__ = ["OrderRequest", "Broker", "PaperBroker", "OrderRejected"]

log = logging.getLogger(__name__)


class OrderRejected(RuntimeError):
    """The order could not be executed as asked."""


@dataclass(frozen=True, slots=True)
class OrderRequest:
    client_id: str
    symbol: str
    direction: Direction  # direction of the POSITION this order serves
    qty: float            # in LOTS
    reference_price: float
    slippage: bool        # market orders slip; resting limit targets do not
    reduce_only: bool     # True when this order closes/reduces an existing position
    reason: str
    ts: int

    @property
    def side(self) -> str:
        """Whether this order buys or sells."""
        opening = not self.reduce_only
        if self.direction is Direction.LONG:
            return "Buy" if opening else "Sell"
        return "Sell" if opening else "Buy"


class Broker(ABC):
    """Turns order requests into fills."""

    @abstractmethod
    def expected_price(self, *, reference_price: float, side: str, slippage: bool) -> float:
        """The price an order would fill at, without placing it.

        The runner needs this to size the position and build the exit ladder
        *before* sending the entry, because both depend on the entry price.
        """

    @abstractmethod
    def execute(self, order: OrderRequest) -> LiveFill:
        """Execute ``order`` and return the resulting fill."""

    # -- optional hooks ------------------------------------------------------ #
    def on_position_opened(self, position: "OpenPositionState") -> None:
        """Called after a successful entry. Real brokers place the ladder here."""

    def on_position_updated(self, position: "OpenPositionState") -> None:
        """Called after break-even arms or a target fills."""

    def on_position_closed(self, position: "OpenPositionState") -> None:
        """Called once a position is flat. Real brokers cancel their resting orders."""

    def emergency_flatten(self) -> None:
        """Close everything now, by any means available."""
        return None

    def actual_size(self) -> float | None:
        """Total lots the broker currently holds across all our positions, or None."""
        return None

    def protective_stop_price(self, position: "OpenPositionState") -> float | None:
        """Where the broker's resting stop actually sits, if it rests one."""
        return None

    def closed_since(self, since_ms: int) -> list[dict] | None:
        """Deals closed by the broker since ``since_ms``.

        Each row is ``{"qty", "pnl", "swap", "commission", "exit_price", "ts"}``.
        Returns ``None`` when the broker cannot be asked (different from ``[]``).
        """
        return None

    def positions_snapshot(self) -> list[dict]:
        """All currently open positions keyed by ticket, for reconciliation."""
        return []

    def describe(self) -> str:
        return type(self).__name__


class PaperBroker(Broker):
    """Simulated fills at the reference price, with Forex lot rounding.

    It holds no position and no cash — the runner owns both. That keeps the
    accounting in exactly one place, so paper mode is a usable rehearsal for the
    real path.

    Slippage is applied in bps against the caller's reference price. Spread is
    *not* simulated here: in live mode it is already inside the ask/bid the
    runner reads, and in the backtester it is charged explicitly by the engine.
    """

    def __init__(
        self,
        *,
        fee_rate: float = 0.0,
        slippage_bps: float = 0.0,
        instruments: Mapping[str, InstrumentSpec] | None = None,
    ) -> None:
        self.fee_rate = float(fee_rate)
        self.slippage_bps = float(slippage_bps)
        self.instruments = dict(instruments or {})

    def spec(self, symbol: str) -> InstrumentSpec | None:
        return self.instruments.get(str(symbol).upper())

    def expected_price(self, *, reference_price: float, side: str, slippage: bool) -> float:
        price = float(reference_price)
        if slippage and self.slippage_bps:
            bps = self.slippage_bps / 10_000.0
            price *= 1.0 + bps if side == "Buy" else 1.0 - bps
        return price

    def fill_price(self, order: OrderRequest) -> float:
        return self.expected_price(
            reference_price=order.reference_price, side=order.side, slippage=order.slippage
        )

    def execute(self, order: OrderRequest) -> LiveFill:
        if order.qty <= 0:
            raise OrderRejected(f"{order.client_id}: qty must be positive, got {order.qty}")
        spec = self.spec(order.symbol)
        if spec is not None:
            qty = (
                spec.round_lots(order.qty)
                if order.reduce_only
                else spec.round_lots_to_meet(order.qty)
            )
            ok, why = spec.accepts(qty, order.reference_price, reduce_only=order.reduce_only)
            if not ok:
                raise OrderRejected(f"{order.client_id}: {why}")
        else:
            qty = float(order.qty)

        price = self.fill_price(order)
        if spec is not None:
            price = spec.round_price(price)
        notional = spec.notional_account(price, qty) if spec is not None else price * qty
        fee = notional * self.fee_rate
        return LiveFill(
            ts=order.ts,
            client_id=order.client_id,
            price=price,
            qty=qty,
            fee=fee,
            reason=order.reason,
            pnl=0.0,
        )
