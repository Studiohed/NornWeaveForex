"""Command line interface: ``nornweave-forex <command>``.

Commands
--------
``presets``  show the strategy's recommended parameter sets
``fetch``    download and cache H2 candles from MT5
``backtest`` run NORN WEAVE over historical data across all pairs
``live``     start the live runner against the MT5 RPyC server
``demo``     run end-to-end on synthetic data, no network required

The live/broker modules are imported lazily, so the offline commands
(``presets``/``backtest``/``demo``) work on a machine with no RPyC server and
even while the live runner is being changed.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from nornweave_forex.config import Config, ConfigError, interval_ms
from nornweave_forex.backtest.engine import BacktestResult, PortfolioBacktester
from nornweave_forex.instrument import InstrumentSpec, default_instrument
from nornweave_forex.types import CandleArrays

__all__ = ["main", "build_parser"]

log = logging.getLogger("nornweave_forex")

#: Bars generated for ``demo`` and for symbols missing from the candle cache.
DEFAULT_SYNTHETIC_BARS = 3000
#: Where ``fetch`` writes ``<SYMBOL>.npz`` and ``backtest`` reads them back.
DEFAULT_CACHE_DIR = "data/candles"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nornweave-forex")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    presets = sub.add_parser("presets", help="show the recommended parameter sets")
    presets.add_argument("--config", default="configs/forex.yaml")

    fetch = sub.add_parser("fetch", help="download and cache candles from MT5")
    fetch.add_argument("--config", default="configs/forex.yaml")
    fetch.add_argument("--output", default=DEFAULT_CACHE_DIR)

    bt = sub.add_parser("backtest", help="run the strategy over historical data")
    bt.add_argument("--config", default="configs/forex.yaml")
    bt.add_argument("--start", default=None, help="ISO date/time, inclusive (UTC)")
    bt.add_argument("--end", default=None, help="ISO date/time, exclusive (UTC)")
    bt.add_argument("--symbols", default=None, help="comma-separated pairs, default: all 8 majors")
    bt.add_argument("--cache", default=DEFAULT_CACHE_DIR,
                    help="directory of fetch'ed <SYMBOL>.npz candles")

    live = sub.add_parser("live", help="start the live runner")
    live.add_argument("--config", default="configs/forex.yaml")
    live.add_argument("--state", default="state/live.json")
    live.add_argument("--paper", action="store_true", help="paper mode: no real orders")
    live.add_argument("--once", action="store_true", help="run one step and exit")
    live.add_argument("--interval-seconds", type=int, default=30, help="poll interval")

    demo = sub.add_parser("demo", help="run on synthetic data, no network required")
    demo.add_argument("--config", default="configs/forex.yaml")
    return parser


def _load_config(path: str) -> Config:
    return Config.from_yaml(path)


def _make_mt5_client(args: Any) -> Any:
    from nornweave_forex.broker.mt5_client import MT5Client, MT5Endpoint

    host = os.environ.get("MT5_HOST", "127.0.0.1")
    port = int(os.environ.get("MT5_PORT", "18813"))
    secret = os.environ.get("NORNWEAVE_MT5_SECRET", "")
    return MT5Client(MT5Endpoint(host=host, port=port), secret=secret)


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #
def _parse_ts(value: str | None, *, exclusive_end: bool = False) -> int | None:
    """Parse an ISO date/time (UTC if naive); date-only end is exclusive next day."""
    if not value:
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError(f"invalid ISO date/time {value!r}: {exc}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    if exclusive_end and len(value.strip()) == 10:  # date only -> whole day included
        parsed += _dt.timedelta(days=1)
    return int(parsed.timestamp() * 1000)


def _slice_candles(candles: CandleArrays, start_ms: int | None, end_ms: int | None) -> CandleArrays:
    if start_ms is None and end_ms is None:
        return candles
    mask = np.ones(len(candles), dtype=bool)
    if start_ms is not None:
        mask &= candles.ts >= start_ms
    if end_ms is not None:
        mask &= candles.ts < end_ms
    return CandleArrays(
        ts=candles.ts[mask],
        open=candles.open[mask],
        high=candles.high[mask],
        low=candles.low[mask],
        close=candles.close[mask],
        volume=candles.volume[mask],
    )


def _load_cached_candles(symbol: str, cache_dir: str | Path) -> CandleArrays | None:
    """Load ``<cache>/<SYMBOL>.npz`` written by ``fetch``; None when absent."""
    path = Path(cache_dir) / f"{symbol.upper()}.npz"
    if not path.exists():
        return None
    with np.load(path) as data:
        return CandleArrays(
            ts=np.asarray(data["ts"], dtype=np.int64),
            open=np.asarray(data["open"], dtype=np.float64),
            high=np.asarray(data["high"], dtype=np.float64),
            low=np.asarray(data["low"], dtype=np.float64),
            close=np.asarray(data["close"], dtype=np.float64),
            volume=np.asarray(data["volume"], dtype=np.float64),
        )


def _build_series(cfg: Config, symbols: Sequence[str], *, cache_dir: str,
                  start: int | None, end: int | None) -> tuple[dict[str, CandleArrays],
                                                                dict[str, InstrumentSpec]]:
    """Cached MT5 candles when present, synthetic otherwise (with a warning)."""
    from nornweave_forex.data.synthetic import synthetic_candles

    series: dict[str, CandleArrays] = {}
    instruments: dict[str, InstrumentSpec] = {}
    synthetic_symbols: list[str] = []
    minutes = interval_ms(cfg.interval) // 60_000
    min_bars = cfg.backtest.warmup_bars + 2
    for symbol in symbols:
        candles = _load_cached_candles(symbol, cache_dir)
        if candles is None:
            synthetic_symbols.append(symbol)
            candles = synthetic_candles(symbol, bars=DEFAULT_SYNTHETIC_BARS, interval_minutes=minutes)
        candles = _slice_candles(candles, start, end)
        if len(candles) < min_bars:
            print(f"  {symbol}: only {len(candles)} bars after the window; skipped "
                  f"(need > {cfg.backtest.warmup_bars})", file=sys.stderr)
            continue
        series[symbol] = candles
        instruments[symbol] = default_instrument(symbol, price=float(candles.close[-1]))
    if synthetic_symbols:
        print(
            "  warning: no cached candles in "
            f"{Path(cache_dir) / '<SYMBOL>.npz'} for {', '.join(synthetic_symbols)}; "
            "using synthetic data. Run `nornweave-forex fetch` for real history.",
            file=sys.stderr,
        )
    if not series:
        raise ConfigError(f"no usable candle series for {list(symbols)}")
    return series, instruments


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def _fetch_symbols(client: Any, symbols: Sequence[str], interval_minutes: int,
                   count: int, output: str) -> None:
    from nornweave_forex.broker.mt5_client import MT5ClientError
    from nornweave_forex.live.feed import bars_from_mt5

    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    for symbol in symbols:
        try:
            rows = client.copy_rates_from_pos(symbol, interval_minutes, 0, count)
        except MT5ClientError as exc:
            print(f"  {symbol}: fetch failed: {exc}", file=sys.stderr)
            continue
        if not rows:
            print(f"  {symbol}: no data", file=sys.stderr)
            continue
        # The newest MT5 row is the still-forming bar; never cache it.
        closed = rows[:-1]
        if not closed:
            print(f"  {symbol}: only the forming bar was returned", file=sys.stderr)
            continue
        candles = bars_from_mt5(closed)
        arr = CandleArrays.from_candles(candles)
        dest = out / f"{symbol.upper()}.npz"
        np.savez(dest, **{k: getattr(arr, k) for k in ("ts", "open", "high", "low", "close", "volume")})
        print(f"  {symbol}: {len(candles)} closed bars -> {dest}")


def _print_result(result: BacktestResult) -> None:
    print(result.summary())
    print(result.metrics.format_report())


def _run_backtest(cfg: Config, symbols: list[str], *, cache_dir: str,
                  start: int | None = None, end: int | None = None) -> BacktestResult:
    series, instruments = _build_series(
        cfg, symbols, cache_dir=cache_dir, start=start, end=end
    )
    engine = PortfolioBacktester(cfg, series, instruments=instruments)
    result = engine.run()
    _print_result(result)
    return result


def _run_demo(cfg: Config) -> BacktestResult:
    from nornweave_forex.data.synthetic import synthetic_candles

    symbols = list(cfg.symbols)[:3]
    minutes = interval_ms(cfg.interval) // 60_000
    series = {
        symbol: synthetic_candles(symbol, bars=DEFAULT_SYNTHETIC_BARS, interval_minutes=minutes)
        for symbol in symbols
    }
    instruments = {
        symbol: default_instrument(symbol, price=float(candles.close[-1]))
        for symbol, candles in series.items()
    }
    print(f"demo: {len(symbols)} synthetic pair(s) {', '.join(symbols)}, "
          f"{DEFAULT_SYNTHETIC_BARS} bars each, interval {cfg.interval} min")
    engine = PortfolioBacktester(cfg, series, instruments=instruments)
    result = engine.run()
    _print_result(result)
    return result


def _run_live(args: Any, cfg: Config) -> None:
    from nornweave_forex.live.broker import PaperBroker
    from nornweave_forex.live.feed import MT5Feed
    from nornweave_forex.live.runner import PortfolioRunner, load_or_initialise
    from nornweave_forex.live.state import StateStore

    client = _make_mt5_client(args)
    symbols = tuple(cfg.symbols)
    instruments = {symbol: default_instrument(symbol) for symbol in symbols}

    broker: Any = None
    if args.paper:
        broker = PaperBroker(
            fee_rate=cfg.backtest.taker_fee,
            slippage_bps=cfg.backtest.slippage_bps,
            instruments=instruments,
        )
        log.info("paper mode: no orders will be sent")
    else:
        from nornweave_forex.broker.mt5_broker import MT5Broker, MT5Credentials

        creds = MT5Credentials(
            login=int(os.environ.get("MT5_DEMO_LOGIN", "0")),
            password=os.environ.get("MT5_PASSWORD", ""),
            server=os.environ.get("MT5_SERVER", ""),
        )
        broker = MT5Broker(client, instruments, credentials=creds)
        info = broker.initialize()
        log.info("MT5 connected: login=%s equity=%.2f %s",
                 info.get("login"), info.get("equity"), info.get("currency"))
        # Broker metadata is the only source of pip value for JPY crosses.
        instruments = {
            symbol: InstrumentSpec.from_mt5(client.symbol_info(symbol))
            for symbol in symbols
        }

    feed = MT5Feed(
        client=client,
        symbols=symbols,
        interval_minutes=interval_ms(cfg.interval) // 60_000,
        lookback=max(cfg.backtest.warmup_bars + 50, 500),
    )
    store = StateStore(Path(args.state))
    state, notes = load_or_initialise(cfg, store)
    for note in notes:
        log.info("init: %s", note)

    runner = PortfolioRunner(
        cfg,
        feed=feed,
        store=store,
        broker=broker,
        instruments=instruments,
        persist_every_bar=True,
    )

    if args.once:
        print(runner.step(state).summary())
        return

    _install_stop_handlers()
    import time as _time

    log.info("live loop started, polling every %ds", args.interval_seconds)
    try:
        while True:
            log.info(runner.step(state).summary())
            _time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        # SIGINT (Ctrl-C) and SIGTERM (systemd stop) both land here: persist the
        # last acted state before exiting so a restart never re-executes a bar.
        log.info("stop signal received, saving state")
        try:
            store.save(state)
        except OSError as exc:
            log.error("could not save state on shutdown: %s", exc)
            raise SystemExit(1) from exc


def _install_stop_handlers() -> None:
    """Turn SIGTERM (systemd/docker stop) into KeyboardInterrupt for the loop."""
    import signal

    def _stop(signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, _stop)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        if args.command == "presets":
            cfg = _load_config(args.config)
            print(cfg.describe())
            print(f"portfolio: {', '.join(cfg.symbols)} | max_positions={cfg.portfolio.max_positions}")
            return 0

        if args.command == "fetch":
            cfg = _load_config(args.config)
            client = _make_mt5_client(args)
            _fetch_symbols(
                client,
                cfg.symbols,
                interval_ms(cfg.interval) // 60_000,
                cfg.backtest.preroll_bars + cfg.backtest.warmup_bars,
                args.output,
            )
            return 0

        if args.command == "backtest":
            cfg = _load_config(args.config)
            symbols = (args.symbols or ",".join(cfg.symbols)).split(",")
            wanted = [s.strip().upper() for s in symbols if s.strip()]
            _run_backtest(
                cfg,
                wanted,
                cache_dir=args.cache,
                start=_parse_ts(args.start),
                end=_parse_ts(args.end, exclusive_end=True),
            )
            return 0

        if args.command == "live":
            cfg = _load_config(args.config)
            _run_live(args, cfg)
            return 0

        if args.command == "demo":
            cfg = _load_config(args.config)
            _run_demo(cfg)
            return 0

        parser.print_help()
        return 2
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - reported, then non-zero exit
        log.exception("fatal: %s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
