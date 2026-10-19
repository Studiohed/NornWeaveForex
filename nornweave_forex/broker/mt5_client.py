"""RPyC client for the MT5 server, running on the Linux VPS.

The client owns reconnection, timeouts and the shared-secret handshake. It
returns plain dicts; the layers above (:mod:`~nornweave_forex.broker.mt5_broker`,
:mod:`~nornweave_forex.live.feed`) translate them into typed objects.

Retry policy (money-critical)
-----------------------------
A transport failure is ambiguous: the request may or may not have reached the
server. Retrying a read is free, retrying a *mutation* can duplicate a live
order. Therefore only :data:`READ_ONLY_METHODS` are retried after a transport
error. ``order_send``/``order_modify_sltp``/``initialize``/``shutdown`` are
"at most once": a lost reply raises :class:`MT5ConnectionLost` with an explicit
"state unknown, reconcile before retrying" message and lets the caller decide.
The one exception is a failed connect/re-auth, where the request provably never
left this process.
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass
from typing import Any

import rpyc

__all__ = ["MT5Client", "MT5ClientError", "MT5ConnectionLost", "READ_ONLY_METHODS"]

log = logging.getLogger(__name__)


class MT5ClientError(RuntimeError):
    """Raised for any client-side failure (auth, timeout, remote error)."""


class MT5ConnectionLost(MT5ClientError):
    """The connection dropped mid-call; the caller should reconcile, not retry.

    For read-only calls the client has already exhausted its bounded retries.
    For mutating calls it is raised on the first transport failure because
    retrying could duplicate an order whose reply was lost.
    """


#: Service methods with no side effects: safe to retry after a transport error.
READ_ONLY_METHODS = frozenset(
    {
        "ping",
        "account_info",
        "symbol_info",
        "symbol_select",  # Market Watch visibility toggle; idempotent
        "symbol_tick",
        "copy_rates_from_pos",
        "positions_get",
        "history_deals_get",
        "order_check",  # server-side validation only; places nothing
        "terminal_info",
    }
)


@dataclass(frozen=True, slots=True)
class MT5Endpoint:
    host: str
    port: int = 18813


class MT5Client:
    """Authenticated, auto-reconnecting RPyC client.

    Public methods return plain dicts. Transport failures become
    :class:`MT5ConnectionLost`; everything else becomes :class:`MT5ClientError`.
    """

    def __init__(
        self,
        endpoint: MT5Endpoint,
        secret: str,
        *,
        connect_timeout: float = 10.0,
        sync_timeout: float = 30.0,
        reconnect_attempts: int = 3,
        reconnect_backoff_s: float = 1.0,
    ) -> None:
        if not secret:
            raise ValueError("MT5Client needs a non-empty shared secret")
        self._endpoint = endpoint
        self._secret = secret
        self._connect_timeout = float(connect_timeout)
        self._sync_timeout = sync_timeout
        self._reconnect_attempts = max(1, int(reconnect_attempts))
        self._reconnect_backoff = max(0.0, float(reconnect_backoff_s))
        self._conn: Any = None

    # -- connection management --------------------------------------------- #
    def _open(self) -> Any:
        if self._conn is not None:
            return self._conn
        host, port = self._endpoint.host, self._endpoint.port
        log.info("connecting to MT5 server at %s:%d", host, port)
        # rpyc.connect has no timeout argument; probe the socket first so an
        # unreachable VPS/appliance fails in connect_timeout, not the OS TCP
        # timeout (which can be minutes).
        try:
            with socket.create_connection((host, port), timeout=self._connect_timeout):
                pass
        except OSError as exc:
            raise MT5ConnectionLost(
                f"cannot reach MT5 server at {host}:{port} within "
                f"{self._connect_timeout:.1f}s: {exc}"
            ) from exc
        conn = rpyc.connect(
            host,
            port,
            config={
                "sync_request_timeout": self._sync_timeout,
                "allow_public_attrs": False,
                "allow_all_attrs": False,
            },
        )
        try:
            conn.root.auth(self._secret)
        except Exception as exc:
            try:
                conn.close()
            finally:
                raise MT5ClientError(f"MT5 auth failed: {exc}") from exc
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
            finally:
                self._conn = None

    def __enter__(self) -> "MT5Client":
        self._open()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # -- remote call wrapper ------------------------------------------------ #
    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke ``root.<name>(*args)`` honouring the retry policy above."""
        read_only = name in READ_ONLY_METHODS
        attempts = self._reconnect_attempts if read_only else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            sent = False
            try:
                conn = self._open()
                fn = getattr(conn.root, name)
                sent = True  # from here on, the server may have executed it
                return fn(*args, **kwargs)
            except (EOFError, socket.error, ConnectionError, BrokenPipeError,
                    TimeoutError) as exc:
                last_exc = exc
                log.warning(
                    "MT5 transport failure on %s (attempt %d/%d, sent=%s): %s",
                    name, attempt + 1, attempts, sent, exc,
                )
                self.close()
                if sent and not read_only:
                    # Never resend a mutation whose reply was lost: the order may
                    # already be live. The caller must reconcile before retrying.
                    raise MT5ConnectionLost(
                        f"MT5 call {name} lost its reply after being sent ({exc}); "
                        "not retried because retrying a mutating call can duplicate it. "
                        "Reconcile positions/deals against the server before retrying."
                    ) from exc
                if attempt + 1 < attempts:
                    time.sleep(self._reconnect_backoff * (attempt + 1))
            except MT5ConnectionLost:
                # Raised by _open(): the request never left this process, so even
                # a mutating call cannot have executed. Reads may retry.
                self.close()
                if read_only and attempt + 1 < attempts:
                    time.sleep(self._reconnect_backoff * (attempt + 1))
                    continue
                raise
            except MT5ClientError:
                raise
            except Exception as exc:
                # Remote exceptions arrive wrapped by rpyc; surface the message
                # but don't retry — the server answered, the call just failed.
                raise MT5ClientError(f"MT5 call {name} failed: {exc}") from exc
        raise MT5ConnectionLost(
            f"MT5 connection lost after {attempts} attempt(s) on {name}: {last_exc}"
        ) from last_exc

    # -- public surface (mirrors MT5Service) -------------------------------- #
    def ping(self) -> int:
        return int(self._call("ping"))

    def initialize(self, login: int | None = None, password: str | None = None,
                   server: str | None = None, path: str | None = None) -> dict[str, Any]:
        # Mutating/lifecycle: at most one attempt (see module docstring).
        return dict(self._call("initialize", login=login, password=password,
                               server=server, path=path))

    def shutdown(self) -> None:
        try:
            self._call("shutdown")
        finally:
            self.close()

    def account_info(self) -> dict[str, Any]:
        return dict(self._call("account_info"))

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        return dict(self._call("symbol_info", symbol))

    def symbol_select(self, symbol: str, enable: bool = True) -> bool:
        return bool(self._call("symbol_select", symbol, enable))

    def symbol_tick(self, symbol: str) -> dict[str, Any]:
        return dict(self._call("symbol_tick", symbol))

    def copy_rates_from_pos(self, symbol: str, timeframe_minutes: int,
                            start_pos: int, count: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self._call("copy_rates_from_pos", symbol,
                                             timeframe_minutes, start_pos, count)]

    def positions_get(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return [dict(p) for p in self._call("positions_get", symbol)]

    def order_send(self, request: dict[str, Any]) -> dict[str, Any]:
        # At most once: a lost reply raises MT5ConnectionLost, never a duplicate.
        return dict(self._call("order_send", dict(request)))

    def order_check(self, request: dict[str, Any]) -> dict[str, Any]:
        """Server-side preflight (``mt5.order_check``); places nothing."""
        return dict(self._call("order_check", dict(request)))

    def order_modify_sltp(self, ticket: int, sl: float, tp: float) -> dict[str, Any]:
        # At most once: modifying SL/TP twice is usually idempotent, but a lost
        # reply on a close-adjacent operation is still ambiguous; reconcile first.
        return dict(self._call("order_modify_sltp", int(ticket), float(sl), float(tp)))

    def history_deals_get(self, since_ms: int, until_ms: int) -> list[dict[str, Any]]:
        return [dict(d) for d in self._call("history_deals_get", int(since_ms), int(until_ms))]
