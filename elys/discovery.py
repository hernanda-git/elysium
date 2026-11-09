"""Whale discovery: build the ranked watchlist from live explorer data."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from .config import Settings
from .intel import Intel, IntelError
from .scoring import WalletScore, score_wallet
from .store import Store
from .util import norm_addr, now_ts

log = logging.getLogger("elys.discovery")


async def discover(
    settings: Settings,
    intel: Intel,
    store: Store | None = None,
    *,
    limit: int | None = None,
    top: int | None = None,
    persist: bool = True,
    prune: bool = True,
    dump: Path | None = None,
) -> list[WalletScore]:
    """Pull leaderboard + hunters, probe recent trade tapes, score and rank."""
    limit = limit or settings.discover_limit

    rows: dict[str, dict] = {}
    windows: dict[str, list[str]] = {}

    results = await asyncio.gather(
        *(intel.traders(win=win, sort="realized", limit=limit) for win in settings.discover_windows),
        return_exceptions=True,
    )
    for win, result in zip(settings.discover_windows, results):
        if isinstance(result, Exception):
            log.warning("traders win=%s failed: %s", win, result)
            continue
        traded, _meta = result
        for row in traded:
            addr = norm_addr(row.get("wallet"))
            if addr == norm_addr(None):
                continue
            merged = rows.get(addr)
            if merged is None:
                rows[addr] = dict(row, wallet=addr)
            else:
                merged["realized"] = max(merged.get("realized") or 0.0, row.get("realized") or 0.0)
                merged["volume"] = max(merged.get("volume") or 0.0, row.get("volume") or 0.0)
                merged["trades"] = max(merged.get("trades") or 0, row.get("trades") or 0)
                merged["tokens"] = max(merged.get("tokens") or 0, row.get("tokens") or 0)
            windows.setdefault(addr, []).append(win)

    try:
        hunters = await intel.hunters(limit=100)
    except IntelError as exc:
        hunters = []
        log.warning("hunters failed: %s", exc)
    for hunter in hunters:
        addr = norm_addr(hunter.get("wallet"))
        if addr == norm_addr(None):
            continue
        merged = rows.setdefault(addr, {"wallet": addr, "realized": 0.0, "volume": 0.0, "trades": 0, "tokens": 0})
        merged["hunter"] = 1
        merged["early"] = hunter
        windows.setdefault(addr, []).append("hunters")

    # Cheap pre-screen before spending profile requests.
    candidates: list[tuple[str, dict]] = []
    for addr, row in rows.items():
        if row.get("hunter"):
            candidates.append((addr, row))
            continue
        wins, losses = int(row.get("wins") or 0), int(row.get("losses") or 0)
        wr = wins / (wins + losses) if wins + losses else 0.0
        last_ts = int(row.get("last_ts") or 0)
        # A dormant wallet cannot be copied right now; re-discovery picks it up later.
        if last_ts and now_ts() - last_ts > settings.discover_max_idle_s:
            continue
        if (
            int(row.get("trades") or 0) >= settings.discover_min_trades
            and int(row.get("tokens") or 0) >= settings.discover_min_tokens
            and float(row.get("realized") or 0.0) >= settings.discover_min_realized
            and wr >= 0.40
        ):
            candidates.append((addr, row))

    # The leaderboard row already carries the scoring inputs (realised, trades,
    # tokens, wins/losses, last_ts); only the trade tape needs a second request.
    prelim = sorted(
        ((score_wallet(row, None, settings).score, addr, row) for addr, row in candidates),
        key=lambda item: item[0],
        reverse=True,
    )
    probe = prelim[: max(1, settings.discover_probe_trades)]
    recent_trades = await intel.trades_many([addr for _, addr, _ in probe], limit=100)
    missing = [addr for _, addr, _ in probe if addr not in recent_trades]
    if missing:
        await asyncio.sleep(2.0)
        recent_trades.update(await intel.trades_many(missing, limit=100))
    log.info(
        "discovery: %d candidates, probing %d trade tapes (429s so far: %d)",
        len(candidates),
        len(probe),
        intel.rate_limited,
    )

    scored: list[WalletScore] = [
        score_wallet(row, None, settings, recent=recent_trades.get(addr, []))
        for _, addr, row in probe
    ]

    passed = [s for s in scored if s.passed]
    passed.sort(key=lambda s: s.score, reverse=True)
    if top:
        passed = passed[:top]

    rejected = sorted((s for s in scored if not s.passed), key=lambda s: s.score, reverse=True)
    for item in rejected[:10]:
        log.debug("rejected %s: %s", item.address, "; ".join(item.reasons))

    if persist and store is not None:
        for item in passed:
            store.upsert_whale(item, window="screen")
        if prune:
            store.deactivate_whales([s.address for s in passed])
        store.set_meta("last_discovery", now_ts())
        store.set_meta("last_discovery_count", len(passed))

    if dump:
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(
            json.dumps([_as_dict(s) for s in passed], indent=1, default=str),
            encoding="utf-8",
        )

    return passed


def _as_dict(score: WalletScore) -> dict:
    return {
        "address": score.address,
        "score": score.score,
        "tier": score.tier,
        "win_rate": round(score.win_rate, 4) if score.win_rate is not None else None,
        "shrunk_wr": round(score.shrunk_wr, 4),
        "realized": round(score.realized, 2),
        "volume": round(score.volume, 2),
        "trades": score.trades,
        "tokens": score.tokens,
        "wins": score.wins,
        "losses": score.losses,
        "hunter": score.hunter,
        "early_rate": round(score.early_rate, 4) if score.early_rate is not None else None,
        "last_ts": score.last_ts,
        "idle_s": score.idle_s,
        "recent_buys": score.recent_buys,
        "recent_sells": score.recent_sells,
        "recent_max_buy_usd": round(score.recent_max_buy_usd, 2),
        "copyable": score.copyable,
    }
