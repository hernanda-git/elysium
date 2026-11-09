"""Wallet scoring: turn explorer rows, profiles and recent trades into a ranked,
actually-copyable whale list.

The score is deliberately transparent and testable. Six bounded components plus a
sharp recency term and a copyability term, because a wallet that has been idle for
three hours cannot be copied no matter how good its record is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import Settings
from .util import clamp, norm_addr, now_ts

# Sample-size shrinkage for win rate: a wallet with 9 wins / 12 losses must not
# outrank one with 180 wins / 120 losses just because its raw ratio is similar.
PRIOR_WR = 0.5
PRIOR_N = 12.0

WEIGHTS = {
    "wr": 30.0,
    "pnl": 20.0,
    "diversity": 10.0,
    "activity": 10.0,
    "recency": 10.0,
    "hunter": 10.0,
    "copy": 10.0,
}

# Idle time at which the recency component is fully zero.
RECENCY_HORIZON_S = 3600
# A recent buy is "copyable" when it is at least this multiple of the copy floor.
COPY_FULL_MULTIPLE = 4.0

WHALE_REALIZED = 50_000.0
SMART_REALIZED = 10_000.0
CONVICTION_WR = 0.55


@dataclass(slots=True)
class WalletScore:
    address: str
    score: float
    tier: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    win_rate: float | None = None
    shrunk_wr: float = PRIOR_WR
    realized: float = 0.0
    unreal: float = 0.0
    volume: float = 0.0
    trades: int = 0
    tokens: int = 0
    wins: int = 0
    losses: int = 0
    hunter: int = 0
    early_rate: float | None = None
    early_smoothed: float = 0.0
    early_entries: int = 0
    early_hits: int = 0
    last_ts: int = 0
    idle_s: int | None = None
    recent_buys: int = 0
    recent_sells: int = 0
    recent_max_buy_usd: float = 0.0
    copyable: bool = False
    label: str = ""


def win_rate(wins: int, losses: int) -> float | None:
    total = wins + losses
    return wins / total if total > 0 else None


def shrunk_win_rate(wins: int, losses: int, prior: float = PRIOR_WR, n: float = PRIOR_N) -> float:
    return (wins + prior * n) / (wins + losses + n)


def _stats(row: dict, profile: dict | None) -> dict:
    """Merge the leaderboard row with the richer wallet profile."""
    profile = profile or {}
    record = profile.get("record") or {}
    board = profile.get("board") or {}
    early = profile.get("early") or {}
    book = profile.get("book") or {}

    wins = int(record.get("wins") or row.get("wins") or 0)
    losses = int(record.get("losses") or row.get("losses") or 0)
    if wins + losses == 0:
        wins = int(row.get("wins") or 0)
        losses = int(row.get("losses") or 0)

    realized = board.get("realized")
    if realized is None:
        realized = row.get("realized") or 0.0
    unreal = board.get("unreal")
    if unreal is None:
        unreal = row.get("unreal") or 0.0
    trades = int(board.get("trades") or row.get("trades") or 0)
    tokens = int(board.get("tokens") or row.get("tokens") or 0)
    last_ts = int(book.get("last_ts") or row.get("last_ts") or 0)

    return {
        "wins": wins,
        "losses": losses,
        "realized": float(realized or 0.0),
        "unreal": float(unreal or 0.0),
        "volume": float(row.get("volume") or 0.0),
        "trades": trades,
        "tokens": tokens,
        "last_ts": last_ts,
        "hunter": int(early.get("hunter") or row.get("hunter") or 0),
        "early_rate": early.get("rate"),
        "early_smoothed": float(early.get("smoothed") or 0.0),
        "early_entries": int(early.get("entries") or 0),
        "early_hits": int(early.get("hits") or 0),
    }


def recent_stats(
    trades: list[dict] | None,
    *,
    now: int | None = None,
    min_usd: float = 250.0,
    lookback_s: int = 86_400,
) -> dict:
    """Buy/sell counts and the largest copyable buy in the recent trade tape."""
    now = now or now_ts()
    buys = sells = 0
    max_buy = 0.0
    last_ts = 0
    for trade in trades or []:
        ts = int(trade.get("ts") or 0)
        if ts > last_ts:
            last_ts = ts
        if now - ts > lookback_s:
            continue
        side = str(trade.get("side") or "").lower()
        usd = float(trade.get("usd") or 0.0)
        if side == "buy":
            buys += 1
            if usd >= min_usd:
                max_buy = max(max_buy, usd)
        elif side == "sell":
            sells += 1
    return {
        "last_ts": last_ts,
        "recent_buys": buys,
        "recent_sells": sells,
        "recent_max_buy_usd": max_buy,
    }


def score_wallet(
    row: dict,
    profile: dict | None,
    settings: Settings,
    *,
    now: int | None = None,
    recent: list[dict] | None = None,
) -> WalletScore:
    now = now or now_ts()
    address = norm_addr(row.get("wallet") or row.get("address") or (profile or {}).get("address"))
    st = _stats(row, profile)

    stats = recent_stats(recent, now=now, min_usd=settings.min_whale_usd) if recent is not None else None
    if stats is not None:
        last_ts = max(st["last_ts"], stats["last_ts"])
    else:
        last_ts = st["last_ts"]
    idle_s = (now - last_ts) if last_ts else None

    raw_wr = win_rate(st["wins"], st["losses"])
    swr = shrunk_win_rate(st["wins"], st["losses"])

    # 1. win rate after shrinkage, full marks at 0.70, zero at 0.45
    wr_pts = WEIGHTS["wr"] * clamp((swr - 0.45) / 0.25)
    # 2. realised profit, full marks at $100k
    pnl_pts = WEIGHTS["pnl"] * clamp(math.log10(1 + max(st["realized"], 0.0)) / math.log10(1 + 100_000))
    # 3. spread across tokens: one lucky launch is not a strategy
    diversity_pts = WEIGHTS["diversity"] * clamp(min(st["tokens"], 10) / 10)
    # 4. sample size
    activity_pts = WEIGHTS["activity"] * clamp(st["trades"] / 200)
    # 5. recency: a wallet idle for an hour is not copyable right now
    recency_pts = WEIGHTS["recency"] * clamp(1 - (idle_s or 0) / RECENCY_HORIZON_S) if idle_s is not None else 0.0
    # 6. launch-hunter record
    hunter_pts = WEIGHTS["hunter"] * clamp(st["early_smoothed"] / 0.70) if st["hunter"] else 0.0
    # 7. copyability: can this wallet's recent buys actually be followed at our floor?
    max_buy = float((stats or {}).get("recent_max_buy_usd") or 0.0)
    copy_pts = WEIGHTS["copy"] * clamp(max_buy / (settings.min_whale_usd * COPY_FULL_MULTIPLE))

    score = min(100.0, wr_pts + pnl_pts + diversity_pts + activity_pts + recency_pts + hunter_pts + copy_pts)

    reasons: list[str] = []
    if st["trades"] < settings.discover_min_trades:
        reasons.append(f"thin ({st['trades']} trades < {settings.discover_min_trades})")
    if st["tokens"] < settings.discover_min_tokens:
        reasons.append(f"narrow ({st['tokens']} tokens < {settings.discover_min_tokens})")
    if st["realized"] < settings.discover_min_realized:
        reasons.append(f"pnl {st['realized']:.0f} < {settings.discover_min_realized:.0f}")
    if st["wins"] + st["losses"] == 0:
        reasons.append("no closed record")
    elif swr < settings.discover_min_wr:
        reasons.append(f"shrunk wr {swr:.2f} < {settings.discover_min_wr:.2f}")
    if stats is not None:
        if idle_s is None:
            reasons.append("no activity")
        elif idle_s > settings.discover_max_idle_s:
            reasons.append(f"idle {idle_s}s > {settings.discover_max_idle_s}s")
        if stats["recent_buys"] < settings.discover_min_recent_buys:
            reasons.append(f"no recent buys ({stats['recent_buys']})")
        if stats["recent_max_buy_usd"] < settings.min_whale_usd:
            reasons.append(
                f"largest recent buy {stats['recent_max_buy_usd']:.0f} < floor {settings.min_whale_usd:.0f}"
            )

    if st["hunter"] and st["early_entries"] >= 3 and st["early_hits"] >= 2:
        tier = "hunter"
    elif st["realized"] >= WHALE_REALIZED and swr >= CONVICTION_WR:
        tier = "whale"
    elif st["realized"] >= SMART_REALIZED and swr >= CONVICTION_WR:
        tier = "smart"
    else:
        tier = "watch"

    return WalletScore(
        address=address,
        score=round(score, 2),
        tier=tier,
        passed=not reasons,
        reasons=reasons,
        win_rate=raw_wr,
        shrunk_wr=swr,
        realized=st["realized"],
        unreal=st["unreal"],
        volume=st["volume"],
        trades=st["trades"],
        tokens=st["tokens"],
        wins=st["wins"],
        losses=st["losses"],
        hunter=st["hunter"],
        early_rate=st["early_rate"],
        early_smoothed=st["early_smoothed"],
        early_entries=st["early_entries"],
        early_hits=st["early_hits"],
        last_ts=last_ts,
        idle_s=idle_s,
        recent_buys=int((stats or {}).get("recent_buys") or 0),
        recent_sells=int((stats or {}).get("recent_sells") or 0),
        recent_max_buy_usd=max_buy,
        copyable=bool(stats is not None and max_buy >= settings.min_whale_usd),
    )


def copy_weight(score: float) -> float:
    """Size multiplier for a copied signal: 0.6x at score 30, 1.5x at score 90+."""
    return round(clamp(0.6 + (score - 30.0) / 60.0 * 0.9, 0.6, 1.5), 2)
