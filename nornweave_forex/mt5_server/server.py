"""RPyC service wrapping the MetaTrader5 Python API.

Runs **on the Windows machine** where the MT5 terminal is installed and logged
in. The Linux VPS talks to this service over TCP.

Security model
--------------
The RPyC protocol itself is not authenticated, so we gate every call behind a
shared-secret check that runs at connect time. A wrong secret closes the
connection. This is a speed bump, not a vault — run this on a private network
or behind a VPN/SSH tunnel, never on the open internet.

Exposed surface
---------------
Every ``exposed_*`` method is callable remotely. Anything else on the service
is private. We deliberately do *not* expose the raw ``MetaTrader5`` module —
only the operations the bot needs, with deterministic return shapes the Linux
side can validate.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import logging
import os
import sys
import time
from typing import Any, Sequence

from rpyc.core.service import Service
from rpyc.utils.server import ThreadedServer

__all__ = ["MT5Service", "build_server", "main"]

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Payload types returned over RPyC
# --------------------------------------------------------------------------- #
# These are deliberately plain dicts/lists — RPyC marshals them across the wire
# without needing the class on the client side. The client rebuilds typed
# objects from them.


def _load_mt5():
    """Import MetaTrader5 lazily so the module can be loaded on Linux for tests."""
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as exc:  # pragma: no cover - Linux
        raise RuntimeError(
            "MetaTrader5 is not importable here. This service must run on the "
            "Windows machine where the MT5 terminal is installed."
        ) from exc
    return mt5


def _now_ms() -> int:
    return int(time.time() * 1000)


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _optional_int(obj: Any, attr: str) -> int | None:
    """Read an optional integer attribute, preserving ``None`` when absent."""
    value = getattr(obj, attr, None)
    return None if value is None else int(value)


def _optional_str(obj: Any, attr: str) -> str | None:
    value = getattr(obj, attr, None)
    return None if value is None else str(value)


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class MT5Service(Service):
    """RPyC service exposing a minimal MT5 surface."""

    def __init__(self, *args: Any, mt5_secret_hash: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._secret_hash = mt5_secret_hash
        self._mt5 = None  # lazily loaded
        self._initialized = False
        self._authed = False  # per-connection instance

    # -- auth --------------------------------------------------------------- #
    def on_connect(self, conn: Any) -> None:  # noqa: D401 - rpyc hook
        # Each connection gets its own service instance (rpyc's default), so
        # ``self._authed`` is per-connection state.
        self._authed = False

    def on_disconnect(self, conn: Any) -> None:  # noqa: D401 - rpyc hook
        self._authed = False

    def _require_auth(self) -> None:
        if not self._authed:
            raise PermissionError("call auth() first")

    def exposed_auth(self, secret: str) -> bool:
        """Validate the shared secret; required before any other call."""
        candidate = _hash_secret(secret)
        if hmac.compare_digest(candidate, self._secret_hash):
            self._authed = True
            log.info("client authenticated")
            return True
        log.warning("rejected client: bad secret")
        raise PermissionError("invalid secret")

    # -- lifecycle ---------------------------------------------------------- #
    def _ensure_mt5(self) -> Any:
        self._require_auth()
        if self._mt5 is None:
            self._mt5 = _load_mt5()
        return self._mt5

    def exposed_initialize(self, login: int | None = None, password: str | None = None,
                           server: str | None = None, path: str | None = None) -> dict[str, Any]:
        """Bring up the MT5 connection; returns the account summary."""
        self._require_auth()
        mt5 = self._ensure_mt5()
        kwargs: dict[str, Any] = {}
        if path:
            kwargs["path"] = path
        if login and password and server:
            kwargs.update(login=int(login), password=password, server=server)
        if not mt5.initialize(**kwargs):
            code, msg = mt5.last_error()
            raise RuntimeError(f"mt5.initialize failed: {code} {msg}")
        info = mt5.account_info()
        if info is None:
            raise RuntimeError("mt5.account_info returned None — not logged in?")
        self._initialized = True
        return {
            "login": int(info.login),
            "server": str(info.server),
            "name": str(info.name),
            "currency": str(info.currency),
            "balance": float(info.balance),
            "equity": float(info.equity),
            "margin": float(info.margin),
            "margin_free": float(info.margin_free),
            "leverage": int(info.leverage),
            # ACCOUNT_MARGIN_MODE_RETAIL_NETTING=0, EXCHANGE=1, RETAIL_HEDGING=2.
            # The broker refuses non-hedging accounts because the ladder model
            # (one ticket per symbol, partial closes) assumes hedging.
            "margin_mode": _optional_int(info, "margin_mode"),
        }

    def exposed_shutdown(self) -> None:
        self._require_auth()
        if self._mt5 is not None and self._initialized:
            self._mt5.shutdown()
        self._initialized = False

    def exposed_ping(self) -> int:
        self._require_auth()
        return _now_ms()

    # -- account ------------------------------------------------------------ #
    def exposed_account_info(self) -> dict[str, Any]:
        self._require_auth()
        mt5 = self._ensure_mt5()
        info = mt5.account_info()
        if info is None:
            raise RuntimeError("mt5.account_info returned None")
        return {
            "login": int(info.login),
            "balance": float(info.balance),
            "equity": float(info.equity),
            "margin": float(info.margin),
            "margin_free": float(info.margin_free),
            "currency": str(info.currency),
            "leverage": int(info.leverage),
            "margin_mode": _optional_int(info, "margin_mode"),
        }

    # -- symbols ------------------------------------------------------------ #
    def exposed_symbol_info(self, symbol: str) -> dict[str, Any]:
        """Return the symbol metadata needed by ``InstrumentSpec.from_mt5``."""
        self._require_auth()
        mt5 = self._ensure_mt5()
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"mt5.symbol_info({symbol!r}) returned None")
        return {
            "symbol": str(info.name),
            "digits": int(info.digits),
            "point": float(info.point),
            "trade_contract_size": float(info.trade_contract_size),
            "trade_tick_value": float(info.trade_tick_value),
            "trade_tick_size": float(info.trade_tick_size),
            "volume_min": float(info.volume_min),
            "volume_max": float(info.volume_max),
            "volume_step": float(info.volume_step),
            "swap_long": float(info.swap_long),
            "swap_short": float(info.swap_short),
            # MT5 ENUM_DAY_OF_WEEK: Sunday=0 .. Saturday=6. Sent as MT5 reports
            # it; InstrumentSpec.from_mt5 converts to Python's Monday=0 weekday.
            "swap_rollover3days": _optional_int(info, "swap_rollover3days"),
            # MT5 ENUM_SYMBOL_SWAP_MODE (1 == POINTS). Without it swap_long/short
            # cannot be interpreted: POINTS is not account currency.
            "swap_mode": _optional_int(info, "swap_mode"),
            # Profit/margin currency, needed to convert notional to account ccy.
            "currency_profit": _optional_str(info, "currency_profit"),
            "currency_margin": _optional_str(info, "currency_margin"),
            # Broker stop constraints: sizing/validation must respect these.
            "trade_stops_level": _optional_int(info, "trade_stops_level"),
            "trade_freeze_level": _optional_int(info, "trade_freeze_level"),
            # Fill/execution mode: decides whether order_send needs a price and
            # which ORDER_FILLING_* values the broker accepts.
            "filling_mode": _optional_int(info, "filling_mode"),
            "trade_exemode": _optional_int(info, "trade_exemode"),
            "spread": float(info.spread),
        }

    def exposed_symbol_select(self, symbol: str, enable: bool = True) -> bool:
        self._require_auth()
        mt5 = self._ensure_mt5()
        return bool(mt5.symbol_select(symbol, enable))

    # -- quotes ------------------------------------------------------------- #
    def exposed_symbol_tick(self, symbol: str) -> dict[str, Any]:
        self._require_auth()
        mt5 = self._ensure_mt5()
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"mt5.symbol_info_tick({symbol!r}) returned None")
        return {
            "symbol": symbol,
            "time_ms": int(tick.time_msc),
            "bid": float(tick.bid),
            "ask": float(tick.ask),
            "last": float(tick.last),
            "volume": float(tick.volume),
        }

    # -- candles ------------------------------------------------------------ #
    def exposed_copy_rates_from_pos(self, symbol: str, timeframe_minutes: int,
                                    start_pos: int, count: int) -> list[dict[str, Any]]:
        """Return ``count`` closed bars ending at ``start_pos``.

        The bot works on closed H2 bars; the caller is responsible for
        discarding the still-forming rightmost bar.
        """
        self._require_auth()
        mt5 = self._ensure_mt5()
        tf = self._timeframe(mt5, timeframe_minutes)
        rates = mt5.copy_rates_from_pos(symbol, tf, start_pos, count)
        if rates is None:
            return []
        return [
            {
                "ts": int(r["time"]) * 1000,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["tick_volume"]),
            }
            for r in rates
        ]

    @staticmethod
    def _timeframe(mt5: Any, minutes: int) -> Any:
        mapping = {
            1: mt5.TIMEFRAME_M1, 3: mt5.TIMEFRAME_M3, 5: mt5.TIMEFRAME_M5,
            15: mt5.TIMEFRAME_M15, 30: mt5.TIMEFRAME_M30,
            60: mt5.TIMEFRAME_H1, 120: mt5.TIMEFRAME_H2, 240: mt5.TIMEFRAME_H4,
            1440: mt5.TIMEFRAME_D1, 10080: mt5.TIMEFRAME_W1,
        }
        if minutes not in mapping:
            raise ValueError(f"unsupported timeframe: {minutes} minutes")
        return mapping[minutes]

    # -- positions ---------------------------------------------------------- #
    def exposed_positions_get(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """All open positions, optionally filtered to one symbol."""
        self._require_auth()
        mt5 = self._ensure_mt5()
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if positions is None:
            return []
        return [
            {
                "ticket": int(p.ticket),
                "symbol": str(p.symbol),
                "type": int(p.type),  # 0 = buy, 1 = sell
                "volume": float(p.volume),
                "price_open": float(p.price_open),
                "price_current": float(p.price_current),
                "sl": float(p.sl),
                "tp": float(p.tp),
                "profit": float(p.profit),
                "swap": float(p.swap),
                "magic": int(p.magic),
                "comment": str(p.comment),
                "time_ms": int(p.time) * 1000,
            }
            for p in positions
        ]

    # -- orders ------------------------------------------------------------- #
    def exposed_order_check(self, request: dict[str, Any]) -> dict[str, Any]:
        """Preflight a request with ``mt5.order_check``; places nothing.

        Used by the broker before the first orders on a symbol so an invalid
        price/volume/stop is caught with a readable retcode and margin numbers
        instead of a live rejection.
        """
        self._require_auth()
        mt5 = self._ensure_mt5()
        result = mt5.order_check(request)
        if result is None:
            code, msg = mt5.last_error()
            raise RuntimeError(f"order_check returned None: {code} {msg}")
        return {
            "retcode": int(result.retcode),
            "balance": float(result.balance),
            "equity": float(result.equity),
            "profit": float(result.profit),
            "margin": float(result.margin),
            "margin_free": float(result.margin_free),
            "margin_level": float(result.margin_level),
            "comment": str(result.comment),
        }

    def exposed_order_send(self, request: dict[str, Any]) -> dict[str, Any]:
        """Place an order. The request shape mirrors MT5's ``order_send`` dict.

        The broker always sets:

          - ``price``       — required whenever the symbol is *not*
                              ``SYMBOL_TRADE_EXECUTION_MARKET``; MT5 documents
                              it as "not set" only for Market-execution symbols.
          - ``magic``       — the bot's magic number, so manual trades are ignored
          - ``type_time``   — ``ORDER_TIME_GTC``
          - ``type_filling`` — chosen from ``filling_mode``; IOC is the default,
                              with a FOK/RETURN fallback on retcode 10030.
          - ``sl``/``tp``   — 0 on entry; the protective stop and ladder target
                              are attached right after the fill.
        """
        self._require_auth()
        mt5 = self._ensure_mt5()
        result = mt5.order_send(request)
        if result is None:
            code, msg = mt5.last_error()
            raise RuntimeError(f"order_send returned None: {code} {msg}")
        return {
            "retcode": int(result.retcode),
            "deal": int(result.deal),
            "order": int(result.order),
            "volume": float(result.volume),
            "price": float(result.price),
            "request_id": int(getattr(result, "request_id", 0)),
            "comment": str(result.comment),
        }

    def exposed_order_modify_sltp(self, ticket: int, sl: float, tp: float) -> dict[str, Any]:
        """Adjust a position's SL/TP. Both must be given; pass 0 to clear."""
        self._require_auth()
        mt5 = self._ensure_mt5()
        position = next((p for p in mt5.positions_get(ticket=ticket) or []), None)
        if position is None:
            raise RuntimeError(f"position {ticket} not found")
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": int(ticket),
            "symbol": str(position.symbol),
            "sl": float(sl),
            "tp": float(tp),
        }
        result = mt5.order_send(request)
        if result is None:
            code, msg = mt5.last_error()
            raise RuntimeError(f"order_send(SLTP) returned None: {code} {msg}")
        return {
            "retcode": int(result.retcode),
            "deal": int(result.deal),
            "order": int(result.order),
            "comment": str(result.comment),
        }

    # -- history ------------------------------------------------------------ #
    def exposed_history_deals_get(self, since_ms: int, until_ms: int) -> list[dict[str, Any]]:
        """All deals in a time window, used for reconciliation and booking."""
        self._require_auth()
        mt5 = self._ensure_mt5()
        import datetime as dt
        since = dt.datetime.fromtimestamp(since_ms / 1000, tz=dt.timezone.utc)
        until = dt.datetime.fromtimestamp(until_ms / 1000, tz=dt.timezone.utc)
        deals = mt5.history_deals_get(since, until)
        if deals is None:
            return []
        return [
            {
                "ticket": int(d.ticket),
                "order": int(d.order),
                "position_id": int(d.position_id),
                "symbol": str(d.symbol),
                "type": int(d.type),
                "entry": int(d.entry),  # 0 = in, 1 = out, 2 = in/out
                "volume": float(d.volume),
                "price": float(d.price),
                "profit": float(d.profit),
                "swap": float(d.swap),
                "commission": float(d.commission),
                "magic": int(d.magic),
                "time_ms": int(d.time) * 1000,
                "comment": str(d.comment),
            }
            for d in deals
        ]


# --------------------------------------------------------------------------- #
# Server bootstrap
# --------------------------------------------------------------------------- #
def build_server(secret: str, *, port: int = 18813, host: str = "0.0.0.0") -> ThreadedServer:
    """Construct the threaded RPyC server. ``secret`` is the shared password."""
    if not secret:
        raise ValueError("a non-empty shared secret is required")
    secret_hash = _hash_secret(secret)

    class _BoundMT5Service(MT5Service):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, mt5_secret_hash=secret_hash, **kwargs)

    server = ThreadedServer(
        _BoundMT5Service,
        port=port,
        hostname=host,
        protocol_config={
            "allow_public_attrs": False,
            "allow_all_attrs": False,
            "sync_request_timeout": 30.0,
        },
    )
    return server


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MT5 RPyC server for NornWeaveForex")
    parser.add_argument("--port", type=int, default=18813)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--secret-env",
        default="NORNWEAVE_MT5_SECRET",
        help="name of the env var holding the shared secret",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    secret = os.environ.get(args.secret_env)
    if not secret:
        print(f"error: ${args.secret_env} is not set", file=sys.stderr)
        return 2
    server = build_server(secret, port=args.port, host=args.host)
    log.info("MT5 RPyC server listening on %s:%d", args.host, args.port)
    try:
        server.start()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
