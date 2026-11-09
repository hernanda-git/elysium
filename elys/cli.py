"""CLI entry point: discover, watch, report, telegram, status."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .config import Settings
from .discovery import discover
from .intel import Intel
from .monitor import Monitor, Signal
from .paper import PaperBroker
from .report import build_report, render
from .store import Store
from .strategy import CopyStrategy
from .tgfeed import TelegramUserFeed, TelegramWebFeed
from .util import bold, cyan, dim, fmt_px, fmt_secs, fmt_usd, green, now_ts, red, short_addr, yellow

log = logging.getLogger("elys")


def configure_logging(settings: Settings, *, verbose: bool = False) -> None:
    settings.ensure_dirs()
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)-11s %(message)s", "%H:%M:%S"))
    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)-11s %(message)s"))
    root.addHandler(console)
    root.addHandler(file_handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)


# ── rendering ─────────────────────────────────────────────────────────────────


def render_whales(rows, *, title: str = "WATCHLIST") -> str:
    if not rows:
        return "no wallets"
    keys = ("address", "tier", "score", "win_rate", "trades", "tokens", "realized",
            "idle_s", "recent_buys", "recent_max_buy_usd", "hunter")

    def get(row, name):
        if isinstance(row, dict):
            return row.get(name)
        return getattr(row, name, None)

    lines = [bold(title), "-" * 132]
    lines.append(
        f"{'#':>3}  {'wallet':<44} {'tier':<7} {'score':>6} {'win%':>6} {'trades':>7} "
        f"{'tokens':>7} {'realised':>11} {'idle':>7} {'buys':>5} {'max buy':>10} {'hunter':>7}"
    )
    for i, row in enumerate(rows, 1):
        wr = get(row, "win_rate")
        idle = get(row, "idle_s")
        lines.append(
            f"{i:>3}  {get(row, 'address'):<44} {get(row, 'tier'):<7} {get(row, 'score'):>6.1f} "
            f"{(wr if wr is not None else 0) * 100:>6.1f} {int(get(row, 'trades') or 0):>7} "
            f"{int(get(row, 'tokens') or 0):>7} {fmt_usd(get(row, 'realized')):>11} "
            f"{fmt_secs(idle):>7} {int(get(row, 'recent_buys') or 0):>5} "
            f"{fmt_usd(get(row, 'recent_max_buy_usd')):>10} {('yes' if get(row, 'hunter') else ''):>7}"
        )
    return "\n".join(lines)


def render_signal(signal: Signal, decision) -> str:
    side = green("BUY ") if signal.side == "buy" else red("SELL")
    action = decision.action.upper()
    paint = green if decision.action in ("open", "add") else (red if decision.action == "close" else dim)
    tail = "" if decision.action == "skip" else f" {fmt_usd(decision.usd)}"
    stamp = time.strftime("%H:%M:%S", time.localtime(signal.detected_ts))
    return (
        f"{dim(stamp)} {side} {short_addr(signal.wallet)} {signal.symbol:<12} "
        f"{fmt_usd(signal.usd):>9} @ {fmt_px(signal.px):<13} "
        f"lat {signal.latency_ms:>5}ms  {paint(action)}{tail}"
        f"{dim('  ' + decision.reason) if decision.reason else ''}"
    )


# ── commands ──────────────────────────────────────────────────────────────────


async def cmd_discover(args) -> int:
    settings = Settings.from_env()
    configure_logging(settings, verbose=args.verbose)
    store = Store(settings.db_path)
    intel = Intel(settings)
    try:
        rows = await discover(
            settings,
            intel,
            store,
            limit=args.limit,
            top=args.top,
            persist=not args.no_persist,
            prune=not args.keep_stale,
            dump=settings.db_path.parent / "whales.json",
        )
    finally:
        await intel.close()
    print(render_whales(rows))
    print(dim(f"\n{len(rows)} wallets passed the screen (saved to {settings.db_path})"))
    if args.json:
        print(json.dumps([asdict(r) for r in rows], indent=1, default=str))
    return 0


async def cmd_list(args) -> int:
    settings = Settings.from_env()
    store = Store(settings.db_path)
    rows = store.load_whales(active_only=not args.all, limit=args.limit)
    print(render_whales(rows, title="SAVED WATCHLIST"))
    return 0


async def cmd_watch(args) -> int:
    settings = Settings.from_env()
    if args.no_stream:
        settings.stream_enabled = False
    configure_logging(settings, verbose=args.verbose)
    store = Store(settings.db_path)
    intel = Intel(settings)

    if store.whale_count() == 0:
        print(yellow("watchlist empty — running discovery first"))
        await discover(settings, intel, store, limit=args.limit, top=args.top)

    queue: asyncio.Queue[Signal] = asyncio.Queue(maxsize=2000)
    monitor = Monitor(settings, intel, store, queue)
    count = monitor.load_whales(limit=settings.max_watch)
    replay_minutes = int(getattr(args, "minutes", 0) or 0)
    if replay_minutes:
        floor = now_ts() - replay_minutes * 60
        for addr in monitor.whales:
            monitor._last_ts[addr] = floor
        print(cyan(f"replay: rewound detection floor by {replay_minutes} min"))
    if count == 0:
        print(red("no active whales; run `elys discover`"))
        return 1

    paper = PaperBroker(settings, intel, store)
    strategy = CopyStrategy(settings, intel, store, paper)
    tg = TelegramWebFeed(settings, store) if not args.no_tg else None

    stop = asyncio.Event()
    started = now_ts()

    async def engine() -> None:
        while not stop.is_set():
            try:
                signal = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                decision = await strategy.on_signal(signal)
            except Exception as exc:  # never kill the engine on one bad signal
                log.exception("strategy failed for %s: %s", signal.symbol, exc)
                queue.task_done()
                continue
            signal_id = store.log_signal(
                {
                    "ts": signal.ts,
                    "wallet": signal.wallet,
                    "token": signal.token,
                    "symbol": signal.symbol,
                    "side": signal.side,
                    "px": signal.px,
                    "usd": signal.usd,
                    "tx": signal.tx,
                    "block": signal.block,
                    "source": signal.source,
                    "latency_ms": signal.latency_ms,
                    "whale_score": signal.whale_score,
                    "whale_tier": signal.whale_tier,
                    "decision": decision.action,
                    "reason": decision.reason,
                    "token_meta": None,
                }
            )
            if signal_id and decision.position_id:
                store.update_position(decision.position_id, source_signal=signal_id)
            print(render_signal(signal, decision), flush=True)
            queue.task_done()

    async def marker() -> None:
        while not stop.is_set():
            try:
                await paper.mark()
            except Exception as exc:
                log.warning("mark failed: %s", exc)
            try:
                await asyncio.wait_for(stop.wait(), timeout=args.mark_interval)
            except asyncio.TimeoutError:
                pass

    async def ticker() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=args.status_interval)
                continue
            except asyncio.TimeoutError:
                pass
            status = monitor.status()
            desk = paper.status()
            cash, _pos, equity = paper.equity()
            print(
                dim(
                    f"-- {status['wallets']} whales | blocks {status['blocks_seen']} "
                    f"(stream {'up' if status['stream_connected'] else 'down'}) | "
                    f"signals {status['signals']} | hits {status['block_hits']} "
                    f"| 429s {intel.rate_limited} "
                    f"| errors {status['errors']} | equity {fmt_usd(equity)} "
                    f"| open {desk['open']} | t+{now_ts() - started}s"
                ),
                flush=True,
            )

    async def telegram_loop() -> None:
        if tg is None:
            return
        tg.on_event = lambda event: strategy.on_tg_event(event)
        await tg.run()

    tasks = [
        asyncio.create_task(monitor.run(), name="monitor"),
        asyncio.create_task(engine(), name="engine"),
        asyncio.create_task(marker(), name="marker"),
        asyncio.create_task(ticker(), name="ticker"),
    ]
    if tg is not None:
        tasks.append(asyncio.create_task(telegram_loop(), name="telegram"))

    print(
        bold("ELYSIUM PAPER DESK")
        + f"  whales={count}  equity={fmt_usd(paper.equity()[2])}  "
        + f"stream={'on' if settings.stream_enabled else 'off'}  tg={'on' if tg else 'off'}"
    )
    try:
        if args.seconds:
            await asyncio.wait_for(stop.wait(), timeout=args.seconds)
        else:
            await stop.wait()
    except asyncio.TimeoutError:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        monitor.stop()
        if tg is not None:
            tg.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await intel.close()

    desk = paper.status()
    print(dim(f"stopped: {desk}"))
    return 0


async def cmd_report(args) -> int:
    settings = Settings.from_env()
    store = Store(settings.db_path)
    report = build_report(store, settings)
    if args.json:
        print(json.dumps({k: v for k, v in report.__dict__.items()}, indent=1, default=str))
    else:
        print(render(report))
    return 0


async def cmd_status(args) -> int:
    settings = Settings.from_env()
    store = Store(settings.db_path)
    intel = Intel(settings)
    try:
        stats = await intel.stats()
    except Exception:
        stats = {}
    finally:
        await intel.close()
    paper = PaperBroker(settings, intel, store)
    cash, pos_usd, equity = paper.equity()
    print(bold("ELYSIUM STATUS"))
    print(f"  chain head     {stats.get('headBlock')}  block time {stats.get('avgBlockTimeMs')}ms")
    print(f"  watchers       {store.whale_count()}")
    print(f"  signals        {len(store.query('SELECT id FROM signals'))}")
    print(f"  open positions {len(store.load_open_positions())}")
    print(f"  equity         {fmt_usd(equity)}  (cash {fmt_usd(cash)}, positions {fmt_usd(pos_usd)})")
    count = store.query("SELECT COUNT(*) c FROM tg_posts")[0]["c"]
    print(f"  telegram posts {count}")
    return 0


async def cmd_tg(args) -> int:
    settings = Settings.from_env()
    store = Store(settings.db_path)
    if args.action == "sync":
        web = TelegramWebFeed(settings, store)
        added = await web.backfill(pages=args.pages)
        rows = store.tg_posts(limit=args.limit)
        print(f"stored {added} new posts; {len(rows)} shown")
        for row in rows:
            print(f"  #{row['id']} {dim(row['kind'] or '?'):<14} {row['text'][:100]}")
        return 0
    if args.action == "backfill":
        feed = TelegramUserFeed(settings, store)
        added = await feed.backfill(limit=args.limit)
        print(f"telethon backfill inserted {added} posts")
        return 0
    # listen
    if not settings.tg_api_id or not settings.tg_api_hash:
        print(red("set TG_API_ID and TG_API_HASH (see .env.example)"))
        return 1
    feed = TelegramUserFeed(settings, store)
    print(cyan(f"listening on t.me/{settings.tg_channel}; Ctrl-C to stop"))
    try:
        await feed.run()
    except KeyboardInterrupt:
        feed.stop()
    return 0


async def cmd_reset(args) -> int:
    settings = Settings.from_env()
    if not args.yes:
        print(red("refusing without --yes"))
        return 1
    store = Store(settings.db_path)
    store.reset_paper(settings.start_equity)
    print(green(f"paper account reset to {fmt_usd(settings.start_equity)}"))
    return 0


async def cmd_init(args) -> int:
    settings = Settings.from_env()
    settings.ensure_dirs()
    store = Store(settings.db_path)
    store.cash(settings.start_equity)
    print(green("initialised"))
    print(f"  db   {settings.db_path}")
    print(f"  log  {settings.log_path}")
    print(f"  env  {settings.intel_base}")
    return 0


# ── parser ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="elys", description="Elysium whale copy-trading desk (paper)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create database and directories")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("discover", help="rebuild the whale watchlist")
    p.add_argument("--limit", type=int, help="leaderboard rows per window (default 300)")
    p.add_argument("--top", type=int, help="keep only this many wallets")
    p.add_argument("--no-persist", action="store_true")
    p.add_argument("--keep-stale", action="store_true", help="do not deactivate wallets missing from this run")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("list", help="show the saved watchlist")
    p.add_argument("--all", action="store_true", help="include inactive wallets")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("watch", help="monitor whales and paper-copy their buys")
    p.add_argument("--seconds", type=int, default=0, help="run for N seconds then stop")
    p.add_argument("--no-stream", action="store_true", help="polling only")
    p.add_argument("--no-tg", action="store_true", help="do not read the telegram channel")
    p.add_argument("--limit", type=int, help="leaderboard rows if discovery is needed")
    p.add_argument("--top", type=int, help="max wallets if discovery is needed")
    p.add_argument("--mark-interval", type=float, default=3.0)
    p.add_argument("--status-interval", type=float, default=30.0)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("paper", help="alias for watch")
    p.add_argument("--seconds", type=int, default=0)
    p.add_argument("--no-stream", action="store_true")
    p.add_argument("--no-tg", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--top", type=int)
    p.add_argument("--mark-interval", type=float, default=3.0)
    p.add_argument("--status-interval", type=float, default=30.0)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("replay", help="replay recent whale trades through the paper engine")
    p.add_argument("--minutes", type=int, default=30, help="how far back to rewind the detection floor")
    p.add_argument("--seconds", type=int, default=90, help="wall-clock budget for the replay")
    p.add_argument("--no-stream", action="store_true")
    p.add_argument("--no-tg", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--top", type=int)
    p.add_argument("--mark-interval", type=float, default=3.0)
    p.add_argument("--status-interval", type=float, default=30.0)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("report", help="paper performance summary")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("status", help="one-shot account and chain status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("tg", help="telegram alerts")
    p.add_argument("action", choices=["sync", "listen", "backfill"])
    p.add_argument("--pages", type=int, default=3)
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_tg)

    p = sub.add_parser("reset-paper", help="wipe paper ledger")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
