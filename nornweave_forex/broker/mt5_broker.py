"""Real order placement against MetaTrader 5 via the RPyC client.

Safety layers, outermost first
------------------------------
1. **Deterministic order ids.** Every order carries a comment derived from
   ``(symbol, bar timestamp, purpose)``. ``live.state.client_id`` keeps it within
   MT5's 31-character comment limit; the broker never slices it (a sliced tail is
   exactly what silently collided exit ids in the reference).
2. **Magic number.** Every order sets MT5's ``magic`` field, so a position
   opened by hand in the same terminal is never confused with one the bot owns.
3. **Ticket binding.** The entry's position ticket comes from ``order_send``'s
   result (``result["order"]``) and every later SL/TP modify or partial close
   targets that ticket. If it is unknown, exactly one magic-matching position
   must exist or the order is refused — never "first position wins".
4. **Reconciliation hooks.** ``positions_snapshot``/``closed_since``/``actual_size``
   expose broker truth for the runner's startup reconciliation and deal booking.
5. **Disaster stop rests on the broker.** The strategy's own stop is
   close-triggered, so it cannot be a resting order without changing behaviour.
   Instead we park a wide stop (``disaster_stop_multiple`` × the strategy's
   stop distance) in the position's ``sl`` field so a dead bot never leaves a
   naked position. It is tightened to break-even once the strategy arms it.

Differences from the Bybit broker
---------------------------------
* **Lots, not coins.** Order quantity is a multiple of ``lot_step``.
* **SL/TP are native.** MT5 positions carry ``sl`` and ``tp``; there are no
  separate reduce-only orders. The ladder is implemented by *modifying* ``tp``
  as each rung fills, and ``sl`` is the current protective stop.
* **Closing is not ``ORDER_TYPE_CLOSE_BY``.** A close is a normal
  ``TRADE_ACTION_DEAL`` in the opposite direction with the ``position`` ticket
  in the request and ``volume`` equal to the amount being reduced. That model
  requires a hedging account; :meth:`MT5Broker.initialize` refuses
  netting/exchange accounts.
* **A market order still sends ``price``.** MT5 documents ``price`` as optional
  only for ``SYMBOL_TRADE_EXECUTION_MARKET`` symbols; on Instant-execution
  accounts omitting it is rejected with ``10015``. The broker always sends the
  current ask/bid rounded to the symbol's digits.
* **No funding.** Overnight rollover is booked by the broker as ``swap`` on the
  position; we surface it in ``closed_since`` so the runner can accrue it.
* **Filling mode is negotiated.** IOC is tried first; on ``10030``
  (``TRADE_RETCODE_INVALID_FILL``) the broker retries with FOK, then RETURN, and
  caches the working mode per symbol.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from nornweave_forex.broker.mt5_client import MT5Client, MT5ClientError, MT5ConnectionLost
from nornweave_forex.instrument import InstrumentSpec
from nornweave_forex.live.broker import Broker, OrderRejected, OrderRequest
from nornweave_forex.live.state import LiveFill, OpenPositionState
from nornweave_forex.types import Direction

__all__ = ["MT5Broker", "MT5Credentials", "MT5Position", "parse_position"]

log = logging.getLogger(__name__)

#: A unique magic number identifying this bot's orders in the MT5 terminal.
MAGIC = 20261018

# Trade retcodes (MetaTrader 5 ``ENUM_TRADE_RETCODE``).
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_DONE_PARTIAL = 10010
TRADE_RETCODE_REQUOTE = 10004
TRADE_RETCODE_REJECT = 10006
TRADE_RETCODE_INVALID = 10013
TRADE_RETCODE_INVALID_VOLUME = 10014
TRADE_RETCODE_INVALID_PRICE = 10015
TRADE_RETCODE_INVALID_STOPS = 10016
TRADE_RETCODE_TRADE_DISABLED = 10017
TRADE_RETCODE_MARKET_CLOSED = 10018
TRADE_RETCODE_NO_MONEY = 10019
TRADE_RETCODE_PRICE_CHANGED = 10020
TRADE_RETCODE_PRICE_OFF = 10021
TRADE_RETCODE_NO_CHANGES = 10025
TRADE_RETCODE_INVALID_FILL = 10030
TRADE_RETCODE_POSITION_CLOSED = 10036
TRADE_RETCODE_INVALID_CLOSE_VOLUME = 10037
TRADE_RETCODE_HEDGE_PROHIBITED = 10045

#: Retcodes after which resending the *same intent* with a refreshed price is
#: safe: the server explicitly rejected the previous request, so it did not
#: execute. (A transport failure is different — see :mod:`mt5_client`.)
RETRY_WITH_FRESH_PRICE = frozenset({TRADE_RETCODE_PRICE_CHANGED, TRADE_RETCODE_PRICE_OFF})

# ORDER_FILLING_*: try the most common mode first, then the documented fallbacks.
_ORDER_FILLING_IOC = 1
_ORDER_FILLING_FOK = 0
_ORDER_FILLING_RETURN = 2
_FILLING_SEQUENCE = (_ORDER_FILLING_IOC, _ORDER_FILLING_FOK, _ORDER_FILLING_RETURN)

# ACCOUNT_MARGIN_MODE_*: the ladder model (one position per symbol, partial
# closes by position ticket) needs hedging.
ACCOUNT_MARGIN_MODE_NETTING = 0
ACCOUNT_MARGIN_MODE_EXCHANGE = 1
ACCOUNT_MARGIN_MODE_HEDGING = 2

_RETCODE_TEXT = {
    TRADE_RETCODE_REQUOTE: "requote",
    TRADE_RETCODE_REJECT: "rejected",
    TRADE_RETCODE_INVALID: "invalid request",
    TRADE_RETCODE_INVALID_VOLUME: "invalid volume",
    TRADE_RETCODE_INVALID_PRICE: "invalid price",
    TRADE_RETCODE_INVALID_STOPS: "invalid stops",
    TRADE_RETCODE_TRADE_DISABLED: "trading disabled",
    TRADE_RETCODE_MARKET_CLOSED: "market closed",
    TRADE_RETCODE_NO_MONEY: "no money",
    TRADE_RETCODE_PRICE_CHANGED: "price changed",
    TRADE_RETCODE_PRICE_OFF: "price off",
    TRADE_RETCODE_NO_CHANGES: "no changes",
    TRADE_RETCODE_INVALID_FILL: "invalid filling mode",
    TRADE_RETCODE_POSITION_CLOSED: "position closed",
    TRADE_RETCODE_INVALID_CLOSE_VOLUME: "invalid close volume",
    TRADE_RETCODE_HEDGE_PROHIBITED: "hedging prohibited",
}


def _retcode_message(retcode: int, comment: str = "") -> str:
    text = _RETCODE_TEXT.get(retcode, "unknown retcode")
    return f"retcode={retcode} ({text})" + (f" comment={comment!r}" if comment else "")


@dataclass(frozen=True, slots=True)
class MT5Credentials:
    login: int
    password: str
    server: str
    path: str | None = None  # terminal path override, rarely needed

    def __post_init__(self) -> None:
        if self.login <= 0 or not self.password or not self.server:
            raise ValueError("login, password and server are all required")


@dataclass(frozen=True, slots=True)
class MT5Position:
    """One open position as reported by MT5."""

    ticket: int
    symbol: str
    direction: Direction
    lots: float
    entry_price: float
    current_price: float
    sl: float
    tp: float
    profit: float
    swap: float
    magic: int
    time_ms: int

    @property
    def is_open(self) -> bool:
        return self.lots > 0

    def matches(self, position: OpenPositionState, lot_tolerance: float = 1e-6) -> bool:
        if not self.is_open:
            return False
        if self.direction is not position.direction:
            return False
        return abs(self.lots - position.qty) <= lot_tolerance


def parse_position(payload: Mapping[str, Any]) -> MT5Position:
    raw_type = int(payload["type"])
    return MT5Position(
        ticket=int(payload["ticket"]),
        symbol=str(payload["symbol"]),
        direction=Direction.LONG if raw_type == 0 else Direction.SHORT,
        lots=float(payload["volume"]),
        entry_price=float(payload["price_open"]),
        current_price=float(payload["price_current"]),
        sl=float(payload.get("sl", 0.0)),
        tp=float(payload.get("tp", 0.0)),
        profit=float(payload.get("profit", 0.0)),
        swap=float(payload.get("swap", 0.0)),
        magic=int(payload.get("magic", 0)),
        time_ms=int(payload.get("time_ms", 0)),
    )


def _round_price(price: float, instrument: InstrumentSpec) -> float:
    return instrument.round_price(price)


class MT5Broker(Broker):
    """Broker implementation backed by the MT5 RPyC client."""

    def __init__(
        self,
        client: MT5Client,
        instruments: Mapping[str, InstrumentSpec],
        credentials: MT5Credentials | None = None,
        *,
        magic: int = MAGIC,
        disaster_stop_multiple: float = 3.0,
        max_slippage_points: float = 30.0,
        max_price_retries: int = 2,
    ) -> None:
        self._client = client
        self._instruments = dict(instruments)
        self._credentials = credentials
        self._magic = magic
        self._disaster_stop_multiple = disaster_stop_multiple
        self._max_slippage_points = max_slippage_points
        self._max_price_retries = max(1, int(max_price_retries))
        self._initialized = False
        #: symbol -> MT5 position ticket of the position this bot manages.
        self._tickets: dict[str, int] = {}
        #: symbol -> ORDER_FILLING_* mode the broker accepted last time.
        self._filling: dict[str, int] = {}
        self._margin_mode: int | None = None

    # -- lifecycle ---------------------------------------------------------- #
    def initialize(self) -> dict[str, Any]:
        """Log in to MT5 (if credentials given) and verify the account model."""
        creds = self._credentials
        info = self._client.initialize(
            login=creds.login if creds else None,
            password=creds.password if creds else None,
            server=creds.server if creds else None,
            path=creds.path if creds else None,
        )
        mode = info.get("margin_mode")
        if mode is None:
            # Older server payload; cannot verify the account model.
            log.warning("MT5 account did not report margin_mode; assuming hedging")
        else:
            self._margin_mode = int(mode)
            if self._margin_mode != ACCOUNT_MARGIN_MODE_HEDGING:
                raise RuntimeError(
                    "MT5 account is not a hedging account "
                    f"(margin_mode={self._margin_mode}; netting=0, exchange=1, hedging=2). "
                    "The ladder model opens one position per symbol and partially closes "
                    "it by position ticket, which netting accounts do not support."
                )
        self._initialized = True
        return info

    def shutdown(self) -> None:
        try:
            self._client.shutdown()
        finally:
            self._initialized = False

    # -- instrument helpers --------------------------------------------------- #
    def _spec(self, symbol: str) -> InstrumentSpec:
        if symbol not in self._instruments:
            raise OrderRejected(f"unknown symbol {symbol!r}")
        return self._instruments[symbol]

    # -- pricing ------------------------------------------------------------- #
    def expected_price(self, *, reference_price: float, side: str, slippage: bool) -> float:
        # For a market order we cannot know the fill before it happens; the
        # reference price IS the best ask/bid the caller captured just before
        # calling, so the expectation is the reference itself. Slippage is
        # applied by the caller's sizing math, not baked in here.
        return float(reference_price)

    def current_price(self, symbol: str, side: str) -> float:
        """The price an order on ``side`` would execute at right now."""
        tick = self._client.symbol_tick(symbol)
        # Buy fills at ask, sell fills at bid.
        return float(tick["ask"] if side == "Buy" else tick["bid"])

    # -- order execution ------------------------------------------------------ #
    def _tick_price(self, order: OrderRequest, spec: InstrumentSpec) -> float:
        """Fresh executable, digits-rounded price: ask for buys, bid for sells."""
        try:
            tick = self._client.symbol_tick(order.symbol)
        except MT5ConnectionLost:
            raise
        except MT5ClientError as exc:
            raise OrderRejected(f"{order.client_id}: no tick for {order.symbol}: {exc}") from exc
        raw = float(tick["ask"] if order.side == "Buy" else tick["bid"])
        return spec.round_price(raw)

    def _magic_positions(self, symbol: str) -> list[MT5Position]:
        try:
            payloads = self._client.positions_get(symbol=symbol)
        except MT5ConnectionLost:
            raise
        except MT5ClientError as exc:
            raise OrderRejected(f"positions_get({symbol!r}) failed: {exc}") from exc
        return [
            parse_position(p)
            for p in payloads
            if int(p.get("magic", 0)) == self._magic
        ]

    def _resolve_close_target(self, symbol: str, client_id: str) -> MT5Position:
        """The position a reduce-only order must close.

        Prefers the ticket bound at entry. If that ticket is gone (closed
        manually, or a resumed process), exactly one magic-matching position
        must exist; anything else is refused rather than guessed.
        """
        positions = self._magic_positions(symbol)
        if not positions:
            raise OrderRejected(f"{client_id}: no open position on {symbol}")
        known = self._tickets.get(symbol)
        if known:
            for position in positions:
                if position.ticket == known:
                    return position
            log.warning("bound ticket %s for %s is gone; falling back to unique match",
                        known, symbol)
            self._tickets.pop(symbol, None)
        if len(positions) == 1:
            return positions[0]
        raise OrderRejected(
            f"{client_id}: cannot identify the position to close on {symbol} "
            f"({len(positions)} magic-matching positions, no bound ticket); refusing to guess"
        )

    def _filling_for(self, symbol: str) -> int:
        return self._filling.get(symbol, _ORDER_FILLING_IOC)

    def _send_order(self, request: dict[str, Any], order: OrderRequest,
                    spec: InstrumentSpec) -> dict[str, Any]:
        """Send a market order, retrying only explicit rejections.

        Safe-to-resend cases:

        * ``TRADE_RETCODE_PRICE_CHANGED``/``PRICE_OFF`` — the server rejected the
          request, so a refresh+resend cannot duplicate a fill;
        * ``TRADE_RETCODE_INVALID_FILL`` — the order was not accepted; try the
          next filling mode for this symbol and cache the working one.

        A transport failure (:class:`MT5ConnectionLost`) is deliberately NOT
        retried: the order may be live and the caller must reconcile.
        """
        attempt = 0
        filling_index = 0
        while True:
            try:
                result = self._client.order_send(dict(request))
            except MT5ConnectionLost:
                raise
            except MT5ClientError as exc:
                raise OrderRejected(
                    f"{order.client_id}: order_send failed: {exc}"
                ) from exc
            retcode = int(result.get("retcode", -1))
            comment = str(result.get("comment", ""))

            if retcode == TRADE_RETCODE_INVALID_FILL:
                filling_index += 1
                if filling_index < len(_FILLING_SEQUENCE):
                    request["type_filling"] = _FILLING_SEQUENCE[filling_index]
                    self._filling[order.symbol] = request["type_filling"]
                    log.warning("%s: filling mode rejected; retrying with %s",
                                order.client_id, request["type_filling"])
                    continue
                raise OrderRejected(
                    f"{order.client_id}: broker rejected every filling mode "
                    f"({_retcode_message(retcode, comment)})"
                )

            if retcode in RETRY_WITH_FRESH_PRICE and attempt < self._max_price_retries:
                attempt += 1
                request["price"] = self._tick_price(order, spec)
                log.warning("%s: %s; refreshing price and retrying (%d/%d)",
                            order.client_id, _retcode_message(retcode, comment),
                            attempt, self._max_price_retries)
                continue

            if retcode in (TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL):
                self._filling[order.symbol] = int(request["type_filling"])
            return result

    def execute(self, order: OrderRequest) -> LiveFill:
        spec = self._spec(order.symbol)

        # Resolve/clamp a reduce-only quantity BEFORE the request is built: the
        # server must never receive more than the position holds.
        target: MT5Position | None = None
        qty = spec.round_lots(order.qty)
        if order.reduce_only:
            target = self._resolve_close_target(order.symbol, order.client_id)
            qty = min(qty, target.lots)
        if qty <= 0:
            raise OrderRejected(f"{order.client_id}: qty {order.qty} rounds to zero")

        ok, why = spec.accepts(qty, order.reference_price, reduce_only=order.reduce_only)
        if not ok:
            raise OrderRejected(f"{order.client_id}: {why}")

        price = self._tick_price(order, spec)
        if order.slippage:
            # Slippage guard: refuse the fill if the spread blew out beyond the
            # configured cap. Rejection is safer than filling at a wild price.
            try:
                tick = self._client.symbol_tick(order.symbol)
            except MT5ConnectionLost:
                raise
            except MT5ClientError as exc:
                raise OrderRejected(
                    f"{order.client_id}: no tick for {order.symbol}: {exc}"
                ) from exc
            spread_points = (float(tick["ask"]) - float(tick["bid"])) / spec.point
            if spread_points > self._max_slippage_points:
                raise OrderRejected(
                    f"{order.client_id}: spread {spread_points:.1f} points "
                    f"exceeds cap {self._max_slippage_points:.1f}"
                )

        request: dict[str, Any] = {
            "action": 1,  # TRADE_ACTION_DEAL
            "symbol": order.symbol,
            "volume": qty,
            "type": 0 if order.side == "Buy" else 1,  # ORDER_TYPE_BUY / SELL
            # MT5 docs: price is only optional for SYMBOL_TRADE_EXECUTION_MARKET
            # symbols. Always sending it is correct for both execution models.
            "price": price,
            "sl": 0.0,
            "tp": 0.0,
            "deviation": int(self._max_slippage_points),
            "magic": self._magic,
            # client_id is already <= 31 chars (live.state._LINK_MAX); slicing
            # here is what silently collided ids in the reference. Never slice.
            "comment": order.client_id,
            "type_time": 0,  # ORDER_TIME_GTC
            "type_filling": self._filling_for(order.symbol),
        }
        if target is not None:
            request["position"] = int(target.ticket)

        result = self._send_order(request, order, spec)
        retcode = int(result.get("retcode", -1))
        if retcode not in (TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL):
            raise OrderRejected(
                f"{order.client_id}: order_send "
                f"{_retcode_message(retcode, str(result.get('comment', '')))}"
            )

        # Book the broker's executed volume (partial fills are DONE_PARTIAL).
        result_volume = float(result.get("volume") or 0.0)
        fill_qty = result_volume if result_volume > 0 else qty
        if target is not None:
            fill_qty = min(fill_qty, target.lots)
        fill_price = float(result.get("price") or 0.0) or price

        # Bind the ticket returned by MT5. For a market entry, result["order"] is
        # the position ticket (MQL5 docs: "opened position with
        # POSITION_TICKET=result.order").
        ticket = int(result.get("order") or 0)
        if not order.reduce_only and ticket:
            self._tickets[order.symbol] = ticket

        # Commission: MT5 reports it per deal in history; we approximate the entry
        # fee at zero here because most MT5 Forex brokers bake commission into the
        # spread. The runner can read the real commission from history_deals_get
        # when it reconciles.
        return LiveFill(
            ts=order.ts,
            client_id=order.client_id,
            price=fill_price,
            qty=float(fill_qty),
            fee=0.0,
            reason=order.reason,
            pnl=0.0,
        )

    # -- position lifecycle hooks -------------------------------------------- #
    def _bind_ticket(self, position: OpenPositionState) -> int:
        """Find and remember the MT5 position ticket for ``position``."""
        ticket = int(position.mt5_ticket or self._tickets.get(position.symbol) or 0)
        if ticket:
            position.mt5_ticket = ticket
            self._tickets[position.symbol] = ticket
            return ticket
        positions = self._magic_positions(position.symbol)
        if len(positions) != 1:
            raise OrderRejected(
                f"on_position_opened: cannot bind MT5 ticket for {position.symbol} "
                f"({len(positions)} magic-matching positions)"
            )
        ticket = positions[0].ticket
        position.mt5_ticket = ticket
        self._tickets[position.symbol] = ticket
        return ticket

    def on_position_opened(self, position: OpenPositionState) -> None:
        """Set the disaster stop and the first ladder rung as the TP."""
        spec = self._spec(position.symbol)
        direction = position.direction

        # Disaster stop: multiple of the strategy stop distance, on the broker.
        stop_distance = abs(position.entry_price - position.stop)
        if direction is Direction.LONG:
            disaster_stop = position.entry_price - stop_distance * self._disaster_stop_multiple
        else:
            disaster_stop = position.entry_price + stop_distance * self._disaster_stop_multiple
        disaster_stop = _round_price(disaster_stop, spec)

        # TP: the deepest unhit target. MT5 carries ONE tp per position, so the
        # ladder is realised by moving tp down to the next rung as each fills.
        tp = 0.0
        for i, hit in enumerate(position.tps_hit):
            if not hit and i < len(position.tp_prices):
                tp = position.tp_prices[i]
                break
        tp = _round_price(tp, spec) if tp else 0.0

        ticket = self._bind_ticket(position)
        try:
            result = self._client.order_modify_sltp(ticket, disaster_stop, tp)
        except MT5ConnectionLost:
            raise
        except MT5ClientError as exc:
            raise OrderRejected(
                f"on_position_opened: SL/TP modify failed for ticket {ticket}: {exc}"
            ) from exc
        retcode = int(result.get("retcode", -1))
        if retcode not in (TRADE_RETCODE_DONE, TRADE_RETCODE_NO_CHANGES):
            if retcode == TRADE_RETCODE_INVALID_STOPS:
                # Do not leave the position without a broker-side stop.
                raise OrderRejected(
                    f"on_position_opened: broker rejected disaster stop {disaster_stop} "
                    f"for ticket {ticket} ({_retcode_message(retcode)}); position is naked"
                )
            raise OrderRejected(
                f"on_position_opened: SLTP modify "
                f"{_retcode_message(retcode, str(result.get('comment', '')))}"
            )

    def on_position_updated(self, position: OpenPositionState) -> None:
        """Refresh SL/TP after a partial exit or a break-even arm."""
        spec = self._spec(position.symbol)
        ticket = int(position.mt5_ticket or self._tickets.get(position.symbol) or 0)
        if not ticket:
            return

        sl = position.stop
        if position.be_active and position.be_stop:
            sl = position.be_stop
        sl = _round_price(sl, spec)

        # TP = next unhit rung; zero when all hit (position runs to final exit).
        tp = 0.0
        for i, hit in enumerate(position.tps_hit):
            if not hit and i < len(position.tp_prices):
                tp = position.tp_prices[i]
                break
        tp = _round_price(tp, spec) if tp else 0.0

        try:
            result = self._client.order_modify_sltp(ticket, sl, tp)
        except MT5ConnectionLost:
            raise
        except MT5ClientError as exc:
            raise OrderRejected(
                f"on_position_updated: SL/TP modify failed for ticket {ticket}: {exc}"
            ) from exc
        retcode = int(result.get("retcode", -1))
        if retcode not in (TRADE_RETCODE_DONE, TRADE_RETCODE_NO_CHANGES):
            raise OrderRejected(
                f"on_position_updated: SLTP modify "
                f"{_retcode_message(retcode, str(result.get('comment', '')))}"
            )

    def on_position_closed(self, position: OpenPositionState) -> None:
        """Nothing to cancel — MT5 SL/TP live on the position, which is now closed."""
        known = self._tickets.get(position.symbol)
        if known is None or not position.mt5_ticket or known == int(position.mt5_ticket):
            self._tickets.pop(position.symbol, None)
        return None

    # -- emergency and reconciliation ---------------------------------------- #
    def emergency_flatten(self) -> None:
        """Close every position this bot owns, immediately."""
        for symbol in list(self._instruments):
            positions = self._magic_positions(symbol)
            for pos in positions:
                close_side = "Sell" if pos.direction is Direction.LONG else "Buy"
                try:
                    tick = self._client.symbol_tick(symbol)
                except MT5ConnectionLost:
                    raise
                except MT5ClientError as exc:
                    raise OrderRejected(
                        f"emergency_flatten: no tick for {symbol}: {exc}"
                    ) from exc
                ref = float(tick["ask" if close_side == "Buy" else "bid"])
                req = OrderRequest(
                    client_id=f"emergency-{pos.ticket}",
                    symbol=symbol,
                    direction=pos.direction,
                    qty=pos.lots,
                    reference_price=ref,
                    slippage=True,
                    reduce_only=True,
                    reason="emergency_flatten",
                    ts=pos.time_ms,
                )
                # Bind the ticket first so the close cannot target another position.
                self._tickets[symbol] = pos.ticket
                try:
                    self.execute(req)
                except OrderRejected as exc:
                    log.error("emergency_flatten failed for %s ticket=%d: %s",
                              symbol, pos.ticket, exc)
                    raise

    def actual_size(self) -> float | None:
        """Total lots this bot holds across all watched symbols."""
        try:
            positions = self._client.positions_get()
        except MT5ClientError:
            return None
        total = 0.0
        for p in positions:
            if int(p.get("magic", 0)) == self._magic:
                total += float(p.get("volume", 0.0))
        return total

    def protective_stop_price(self, position: OpenPositionState) -> float | None:
        if not position.mt5_ticket:
            return None
        positions = self._client.positions_get(symbol=position.symbol)
        for p in positions:
            if int(p.get("ticket", 0)) == position.mt5_ticket:
                return float(p.get("sl", 0.0)) or None
        return None

    def closed_since(self, since_ms: int) -> list[dict[str, Any]] | None:
        """Out deals since ``since_ms``, for booking."""
        try:
            deals = self._client.history_deals_get(since_ms, since_ms + 86_400_000 * 30)
        except MT5ClientError:
            return None
        out: list[dict[str, Any]] = []
        for d in deals:
            # entry == 1 means an OUT deal (closing).
            if int(d.get("entry", 0)) != 1:
                continue
            if int(d.get("magic", 0)) != self._magic:
                continue
            out.append({
                "ticket": int(d.get("ticket", 0)),
                "position_id": int(d.get("position_id", 0)),
                "qty": float(d.get("volume", 0.0)),
                "pnl": float(d.get("profit", 0.0)),
                "swap": float(d.get("swap", 0.0)),
                "commission": float(d.get("commission", 0.0)),
                "exit_price": float(d.get("price", 0.0)),
                "ts": int(d.get("time_ms", 0)),
            })
        return out

    def positions_snapshot(self) -> list[dict[str, Any]]:
        """All open positions owned by this bot, for reconciliation."""
        positions = self._client.positions_get()
        return [
            {
                "ticket": int(p["ticket"]),
                "symbol": str(p["symbol"]),
                "direction": int(p["type"]),
                "lots": float(p["volume"]),
                "entry_price": float(p["price_open"]),
                "sl": float(p.get("sl", 0.0)),
                "tp": float(p.get("tp", 0.0)),
                "magic": int(p.get("magic", 0)),
            }
            for p in positions
            if int(p.get("magic", 0)) == self._magic
        ]

    def describe(self) -> str:
        return "MT5Broker"
