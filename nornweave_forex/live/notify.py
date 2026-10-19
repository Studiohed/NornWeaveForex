"""Tell somebody when the bot does something, in words a person reads.

Same rules as the crypto notifier:

* **Never affects trading.** Every failure is swallowed and logged.
* **Never delays trading.** Sending is on a background thread with a bounded queue.
* **Never drops a critical event.** A flooded queue discards the routine and
  makes room for the one that matters.
* **Says when it sends, and says when it fails.** Both are logged.
* **Notifications only.** Nothing here can place, cancel or close anything.
"""

from __future__ import annotations

import logging
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

__all__ = [
    "Event",
    "Notifier",
    "NullNotifier",
    "RecordingNotifier",
    "TelegramNotifier",
    "build_notifier",
    "entry_event",
    "exit_event",
    "halt_event",
]

log = logging.getLogger(__name__)

FIRE = "\U0001f6a8"
UP = "\U0001f7e2"
DOWN = "\U0001f534"
WIN = "\U0001f7e0"
FLAT = "\u26aa"
INFO = "\u2139\ufe0f"


def _money(value: float) -> str:
    return f"{value:+.2f}".replace("-", "\u2212")


def _price(value: float) -> str:
    return f"{value:.5f}".rstrip("0").rstrip(".")


@dataclass(slots=True)
class Event:
    kind: str   # "entry" | "exit" | "halt" | "info"
    symbol: str
    direction: str
    qty: float
    price: float
    pnl: float = 0.0
    reason: str = ""
    be_active: bool = False


def entry_event(*, symbol: str, direction: str, qty: float, price: float) -> Event:
    return Event(kind="entry", symbol=symbol, direction=direction, qty=qty, price=price)


def exit_event(*, symbol: str, direction: str, qty: float, price: float,
               pnl: float, reason: str, be_active: bool = False) -> Event:
    return Event(kind="exit", symbol=symbol, direction=direction, qty=qty,
                 price=price, pnl=pnl, reason=reason, be_active=be_active)


def halt_event(reason: str) -> Event:
    return Event(kind="halt", symbol="", direction="", qty=0, price=0.0, reason=reason)


class Notifier:
    """Base class with a bounded-queue background sender."""

    def __init__(self, *, max_queue: int = 100) -> None:
        self._queue: queue.Queue[Event] = queue.Queue(maxsize=max_queue)
        self._thread: threading.Thread | None = None
        self._sent = 0
        self._failed = 0

    # -- producer side ------------------------------------------------------ #
    def notify(self, event: Event) -> None:
        """Enqueue an event; never raises, never blocks the caller."""
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            # Drop the oldest routine event to make room; a halt always gets through.
            try:
                if event.kind == "halt":
                    self._queue.get_nowait()
                    self._queue.put_nowait(event)
                else:
                    dropped = self._queue.get_nowait()
                    log.warning("notifier queue full; dropped %s event for %s",
                                dropped.kind, dropped.symbol)
                    self._queue.put_nowait(event)
            except queue.Empty:  # pragma: no cover - race
                pass
        self._ensure_thread()

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                event = self._queue.get(timeout=1.0)
            except queue.Empty:
                return
            try:
                self._send(event)
                self._sent += 1
                log.info("notifier sent %s for %s", event.kind, event.symbol or "-")
            except Exception as exc:  # noqa: BLE001 - a notification is never fatal
                self._failed += 1
                log.warning("notifier failed for %s: %s", event.kind, exc)

    # -- consumer side ------------------------------------------------------- #
    def _send(self, event: Event) -> None:
        raise NotImplementedError

    def close(self, wait: float = 3.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout=wait)

    def stats(self) -> dict[str, int]:
        return {"sent": self._sent, "failed": self._failed}


class NullNotifier(Notifier):
    """Discards everything. The default when nobody asked to be told."""

    def __init__(self) -> None:
        super().__init__()

    def notify(self, event: Event) -> None:  # pragma: no cover - trivial
        return

    def _send(self, event: Event) -> None:  # pragma: no cover
        return


class RecordingNotifier(Notifier):
    """Used in tests to assert that events were raised."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[Event] = []

    def _send(self, event: Event) -> None:
        self.events.append(event)


class TelegramNotifier(Notifier):
    """Posts to a Telegram chat via the Bot API."""

    def __init__(self, *, token: str, chat_id: str, max_queue: int = 100) -> None:
        super().__init__(max_queue=max_queue)
        self._token = token
        self._chat_id = chat_id

    def _render(self, event: Event) -> str:
        if event.kind == "halt":
            return f"{FIRE} NORN WEAVE FOREX HALTED\n{event.reason}"
        if event.kind == "entry":
            arrow = UP if event.direction == "long" else DOWN
            return (
                f"{arrow} {event.symbol} {event.direction.upper()} "
                f"{event.qty:.2f} lots @ {_price(event.price)}"
            )
        if event.kind == "exit":
            emoji = WIN if event.pnl > 0 else (DOWN if event.pnl < 0 else FLAT)
            be = " (BE)" if event.be_active else ""
            return (
                f"{emoji} {event.symbol} closed{be} {event.direction.upper()} "
                f"@ {_price(event.price)}\n"
                f"PnL {_money(event.pnl)}  reason: {event.reason}"
            )
        return f"{INFO} {event.symbol} {event.reason}"

    def _send(self, event: Event) -> None:
        text = self._render(event)
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": self._chat_id,
            "text": text,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                raise RuntimeError(f"telegram HTTP {resp.status}")


def build_notifier(settings: Mapping[str, Any] | None) -> Notifier:
    """From a config dict, build the right notifier."""
    if not settings:
        return NullNotifier()
    if settings.get("type") == "telegram":
        return TelegramNotifier(
            token=str(settings["token"]),
            chat_id=str(settings["chat_id"]),
        )
    return NullNotifier()
