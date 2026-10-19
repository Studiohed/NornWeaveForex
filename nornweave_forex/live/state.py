"""Crash-safe persistent state for the multi-symbol live runner.

Mirrors the design of the crypto state module:

* **Atomicity.** Write to a temp file, move into place with ``os.replace``.
* **Idempotency.** Deterministic ``client_id`` from ``(symbol, bar_ts, purpose)``;
  a re-processed bar after a crash produces the same id and the executor refuses
  to double-execute.
* **Schema versioning.** A file from a newer build is *refused*, not quarantined —
  quarantining means starting fresh while the broker holds positions, which is the
  exact failure this module exists to prevent.

Multi-symbol differences from the crypto version:

* ``positions`` is a mapping ``symbol -> OpenPositionState`` rather than a single
  ``position``. Up to ``max_positions`` entries at once.
* ``symbol_last_bar_ts`` tracks the newest closed bar per symbol, so one pair's
  downtime never stalls the others.
* ``swap_accrued`` is tracked separately from ``funding`` — Forex has no funding,
  it has overnight rollover (triple on the broker's rollover day).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nornweave_forex.execution import PositionView
from nornweave_forex.types import Direction

__all__ = [
    "LiveFill",
    "OpenPositionState",
    "ClosedTrade",
    "LiveState",
    "StateStore",
    "StateSchemaError",
    "client_id",
    "SCHEMA_VERSION",
]

SCHEMA_VERSION = 2

_CLIENT_ID_MEMORY = 512
_TRADE_LOG_MEMORY = 2000
#: MT5 stores an order comment of at most 31 characters. Every id is kept inside
#: that budget so the broker's dedupe/audit comment is never silently truncated
#: into a different id (the reference had exactly that bug).
_LINK_MAX = 31


class StateSchemaError(RuntimeError):
    """The state file cannot be safely interpreted by this build."""


def client_id(symbol: str, bar_ts: int, purpose: str) -> str:
    """Deterministic order id for one intent on one bar.

    Same rationale as the crypto version: stability lets a re-processed bar dedupe
    itself; uniqueness means two different purposes on the same bar never collapse
    into one id (which used to happen when the tail was cut, making exits 2 and 3
    look like exit 1 and get dropped as duplicates).

    The id must also be unique **across pairs**: the runner keeps one global
    ``executed_client_ids`` list and every pair closes on the same bar timestamps,
    so truncating the symbol prefix (``USDCAD`` vs ``USDCHF`` -> ``USDC``) would
    silently drop one pair's exit. The full 6-letter symbol is therefore always
    kept, and only the purpose text plus a SHA-1 tail is shortened.
    """
    symbol = symbol.upper()
    stamp = f"-{bar_ts}"
    plain = f"{symbol}{stamp}-{purpose}"
    if len(plain) <= _LINK_MAX:
        return plain
    digest = hashlib.sha1(purpose.encode("utf-8")).hexdigest()[:6]
    suffix = f"-{digest}"
    sym = symbol if len(symbol) <= 6 else symbol[:6]
    room = _LINK_MAX - len(sym) - len(stamp) - 1 - len(suffix)
    if room < 0:
        # Pathological timestamp length (year > 30000): shorten the symbol, never
        # the digest, so two purposes on the same bar cannot collide.
        avail = _LINK_MAX - len(stamp) - 1 - len(suffix)
        if avail < 2:
            raise ValueError(f"cannot build a unique order id for {symbol!r}")
        return f"{symbol[:avail]}{stamp}{suffix}"
    stub = purpose[:room] if room else ""
    return f"{sym}{stamp}-{stub}{suffix}"


@dataclass(slots=True)
class LiveFill:
    ts: int
    client_id: str
    price: float
    qty: float      # lots
    fee: float
    reason: str
    pnl: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LiveFill":
        return cls(**data)


@dataclass(slots=True)
class OpenPositionState:
    """One open position, keyed by symbol in ``LiveState.positions``."""

    symbol: str
    direction: Direction
    entry_ts: int
    entry_price: float
    initial_qty: float  # initial lots
    qty: float          # current lots
    stop: float
    tp_prices: list[float]
    tp_weights: list[float]
    tps_hit: list[bool]
    be_trigger_price: float = 0.0
    be_stop: float = 0.0
    be_active: bool = False
    tp1_overridden: bool = False
    entry_fee: float = 0.0
    exit_fees: float = 0.0
    swap_accrued: float = 0.0   # overnight rollover, not funding
    gross_pnl: float = 0.0
    #: The MT5 position ticket, set once the broker confirms the entry. Without it
    #: reconciliation cannot match a local position to a broker one.
    mt5_ticket: int = 0
    #: What the broker has already closed on this position (deal-level).
    exchange_booked_qty: float = 0.0
    exchange_booked_pnl: float = 0.0
    exchange_booked_gross: float = 0.0
    #: Money-model metadata frozen at entry. Stored on the position (not looked up
    #: from the instrument map) so a resumed process can mark PnL before it has
    #: broker metadata, and a spec change cannot rewrite an open trade's history.
    pip: float = 0.0001
    pip_value_per_lot: float = 10.0
    contract_size: float = 100_000.0

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry_price - self.stop)

    def pnl_money(self, price: float, lots: float | None = None) -> float:
        """Realised/unrealised PnL in account currency at ``price``.

        ``lots`` defaults to the quantity still open; pass a partial fill quantity
        to book one exit.
        """
        qty = self.qty if lots is None else float(lots)
        if self.pip <= 0:
            return 0.0
        return (
            (float(price) - self.entry_price)
            / self.pip
            * self.pip_value_per_lot
            * qty
            * self.direction.sign
        )

    def notional_account(self, price: float | None = None) -> float:
        """Account-currency notional of the open position at ``price``."""
        if self.pip <= 0:
            return 0.0
        mark = self.entry_price if price is None else float(price)
        return abs(mark) * self.qty * self.pip_value_per_lot / self.pip

    def view(self) -> PositionView:
        return PositionView(
            direction=self.direction,
            entry_price=self.entry_price,
            qty=self.qty,
            initial_qty=self.initial_qty,
            stop=self.stop,
            tp_prices=tuple(self.tp_prices),
            tp_weights=tuple(self.tp_weights),
            tps_hit=tuple(self.tps_hit),
            be_active=self.be_active,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["direction"] = int(self.direction)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OpenPositionState":
        payload = dict(data)
        payload["direction"] = Direction(int(payload["direction"]))
        # Normalise the ladder lists the same way the crypto version does: lengths
        # must agree, and a missing entry gets a sentinel that can never fire.
        prices = list(payload.get("tp_prices") or [])
        weights = list(payload.get("tp_weights") or [])
        hits = list(payload.get("tps_hit") or [])
        levels = max(len(prices), len(weights), len(hits))
        if levels:
            entry = float(payload.get("entry_price") or 0.0)
            prices += [entry] * (levels - len(prices))
            weights += [0.0] * (levels - len(weights))
            hits += [False] * (levels - len(hits))
            payload["tp_prices"] = prices
            payload["tp_weights"] = weights
            payload["tps_hit"] = hits
        return cls(**payload)


@dataclass(slots=True)
class ClosedTrade:
    """One completed round trip on one symbol."""

    symbol: str
    direction: Direction
    entry_ts: int
    entry_price: float
    exit_ts: int
    exit_price: float
    qty: float       # lots
    gross_pnl: float
    fees: float
    swap: float
    net_pnl: float
    reason: str
    bars_held: int = 0
    #: Account-currency notional at entry (lots * contract * entry / quote rate,
    #: derived through the pip value). Used for the percentage-return column.
    notional_account: float = 0.0

    @property
    def net_pct_of_notional(self) -> float:
        notional = self.notional_account
        if notional <= 0:
            # Older state files (schema v1) did not persist the notional; fall back
            # to a rough proxy rather than dividing by zero.
            notional = self.entry_price * self.qty * 100_000.0
        return self.net_pnl / notional if notional > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": int(self.direction),
            "side": self.direction.label,
            "entry_ts": self.entry_ts,
            "entry_price": self.entry_price,
            "exit_ts": self.exit_ts,
            "exit_price": self.exit_price,
            "qty": self.qty,
            "gross_pnl": self.gross_pnl,
            "fees": self.fees,
            "swap": self.swap,
            "net_pnl": self.net_pnl,
            "reason": self.reason,
            "bars_held": self.bars_held,
            "notional_account": self.notional_account,
            "net_pct_of_notional": self.net_pct_of_notional,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClosedTrade":
        return cls(
            symbol=str(data["symbol"]),
            direction=Direction(int(data["direction"])),
            entry_ts=int(data["entry_ts"]),
            entry_price=float(data["entry_price"]),
            exit_ts=int(data["exit_ts"]),
            exit_price=float(data["exit_price"]),
            qty=float(data["qty"]),
            gross_pnl=float(data.get("gross_pnl", 0.0)),
            fees=float(data.get("fees", 0.0)),
            swap=float(data.get("swap", 0.0)),
            net_pnl=float(data.get("net_pnl", 0.0)),
            reason=str(data.get("reason", "")),
            bars_held=int(data.get("bars_held", 0)),
            notional_account=float(data.get("notional_account", 0.0)),
        )


@dataclass(slots=True)
class LiveState:
    """Everything the runner needs to resume after a crash."""

    symbols: list[str]
    interval: str
    cash: float
    positions: dict[str, OpenPositionState] = field(default_factory=dict)
    symbol_last_bar_ts: dict[str, int] = field(default_factory=dict)
    day: str = ""
    day_start_equity: float = 0.0
    day_realized: float = 0.0
    trades_closed: int = 0
    realized_pnl: float = 0.0
    halted: bool = False
    halt_reason: str | None = None
    executed_client_ids: list[str] = field(default_factory=list)
    fill_log: list[LiveFill] = field(default_factory=list)
    trade_log: list[ClosedTrade] = field(default_factory=list)

    # -- helpers ------------------------------------------------------------ #
    def remember_trade(self, trade: ClosedTrade) -> None:
        self.trade_log.append(trade)
        if len(self.trade_log) > _TRADE_LOG_MEMORY:
            del self.trade_log[: -_TRADE_LOG_MEMORY]

    def remember(self, cid: str) -> None:
        self.executed_client_ids.append(cid)
        if len(self.executed_client_ids) > _CLIENT_ID_MEMORY:
            del self.executed_client_ids[: -_CLIENT_ID_MEMORY]

    def already_executed(self, cid: str) -> bool:
        return cid in self.executed_client_ids

    def record(self, fill: LiveFill) -> None:
        self.fill_log.append(fill)
        self.remember(fill.client_id)

    def book_realized(self, pnl: float, fee: float = 0.0) -> float:
        """Credit one realised fill to cash and the running totals.

        Returns the net amount booked. Every partial exit must go through here:
        crediting only when the position finally closes leaves the equity curve
        blind to realised money for as long as a runner is open.
        """
        net = float(pnl) - float(fee)
        self.cash += net
        self.realized_pnl += net
        self.day_realized += net
        return net

    def open_symbols(self) -> list[str]:
        return sorted(self.positions.keys())

    def open_count(self) -> int:
        return len(self.positions)

    def equity(self, mark_prices: dict[str, float]) -> float:
        """Equity given a per-symbol mark price for open positions.

        Each position converts its own price difference through the pip value it
        froze at entry, so this is account currency — not price units.
        """
        unrealised = 0.0
        for sym, pos in self.positions.items():
            mark = mark_prices.get(sym)
            if mark is None:
                continue
            unrealised += pos.pnl_money(mark)
        return self.cash + unrealised

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "symbols": list(self.symbols),
            "interval": self.interval,
            "cash": self.cash,
            "positions": {s: p.to_dict() for s, p in self.positions.items()},
            "symbol_last_bar_ts": dict(self.symbol_last_bar_ts),
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "day_realized": self.day_realized,
            "trades_closed": self.trades_closed,
            "realized_pnl": self.realized_pnl,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "executed_client_ids": list(self.executed_client_ids),
            "fill_log": [f.to_dict() for f in self.fill_log[-_CLIENT_ID_MEMORY:]],
            "trade_log": [t.to_dict() for t in self.trade_log],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LiveState":
        payload = dict(data)
        # Files written before schema versioning have no ``schema`` key; they are
        # the v1 layout and every newer field has a default, so they load as-is.
        version = int(payload.pop("schema", 1) or 1)
        if version > SCHEMA_VERSION:
            raise StateSchemaError(
                f"state file is from schema v{version}, this build understands v{SCHEMA_VERSION}"
            )
        # positions arrive as {symbol: dict}
        raw_positions = payload.pop("positions", {}) or {}
        positions = {s: OpenPositionState.from_dict(p) for s, p in raw_positions.items()}
        fills = [LiveFill.from_dict(f) for f in payload.pop("fill_log", [])]
        trades = [ClosedTrade.from_dict(t) for t in payload.pop("trade_log", [])]
        payload["positions"] = positions
        payload["fill_log"] = fills
        payload["trade_log"] = trades
        return cls(**payload)


class StateStore:
    """Read/atomic-write/quarantine for the live state file.

    A save must survive a power cut: the payload goes to a temp file in the same
    directory, is flushed to disk, and is only then renamed over the real file.
    The parent directory is created on demand so the default ``state/live.json``
    path cannot turn the first save into a ``FileNotFoundError``.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> LiveState | None:
        if not self.path.exists():
            return None
        text = self.path.read_text(encoding="utf-8")
        if not text.strip():
            # A zero-length file is what a crashed `write_text` used to leave
            # behind. Treat it as "no state yet" rather than quarantining a file
            # that holds nothing, so a fresh start does not require operator action.
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Already a ValueError subclass; let the caller see the JSON position.
            raise
        if not isinstance(data, dict):
            raise TypeError(f"{self.path}: expected a JSON object at the top level")
        try:
            return LiveState.from_dict(data)
        except KeyError as exc:
            raise ValueError(f"{self.path}: missing required field {exc}") from exc

    def save(self, state: LiveState) -> None:
        payload = json.dumps(state.to_dict(), indent=2, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.chmod(tmp, 0o600)
            except OSError:  # pragma: no cover - Windows / restricted FS
                pass
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:  # pragma: no cover - best effort cleanup
                    pass

    def quarantine(self, reason: str) -> Path:
        """Move the current state aside so a corrupt/foreign file never blocks startup."""
        if not self.path.exists():
            return self.path
        safe_reason = "".join(c if c.isalnum() or c in "-_" else "-" for c in reason)
        target = self.path.with_suffix(f".quarantined-{safe_reason}{self.path.suffix}")
        os.replace(self.path, target)
        return target
