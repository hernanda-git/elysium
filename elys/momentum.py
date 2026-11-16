"""Volume-climax screener.

The copy desk observes flow; it does not trade it. This module is the primary
paper strategy: scan the liquid token universe once per interval, detect a
short-window volume climax with positive net buying, then hold with a tight
stop and a trailing exit so the right tail can pay for the losers.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass

from .config import Settings
from .intel import Intel, IntelError
from .paper import PaperBroker
from .store import Store
from .util import norm_addr, now_ts

log = logging.getLogger("elys.momentum")


@dataclass(slots=True)
class Candidate:
    token: str
    symbol: str
    px: float
    mcap: float
    liq: float
    vol_1h: float
    vol_5m: float
    accel: float
    buy_ratio: float
    net_1h: float
    chg_1h: float
    score: float


def _f(row: dict, key: str) -> float | None:
    value = row.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate(rows: list[dict], settings: Settings, now: int | None = None) -> list[Candidate]:
    """Pure screen: return the candidates that pass every risk and momentum gate."""
    now = now or now_ts()
    ignore = {s.upper() for s in settings.ignore_symbols}
    out: list[Candidate] = []
    for row in rows:
        token = norm_addr(row.get("token"))
        symbol = str(row.get("symbol") or "")
        if not token or symbol.upper() in ignore:
            continue
        if str(row.get("hp_verdict") or "").lower() != "clean":
            continue
        if int(row.get("rug") or 0) or int(row.get("rug_by_creator") or 0):
            continue
        if int(row.get("hp_wiped") or 0):
            continue

        mcap = _f(row, "mcap")
        liq = _f(row, "liq_usd")
        vol_1h = _f(row, "vol_1h")
        vol_5m = _f(row, "vol_5m")
        chg_1h = _f(row, "chg_1h")
        chg_5m = _f(row, "chg_5m")
        net_1h = _f(row, "net_1h")
        top10 = _f(row, "top10_pct")
        underwater = _f(row, "underwater_pct")
        launch_ts = _f(row, "launch_ts")
        if None in (mcap, liq, vol_1h, vol_5m, chg_1h, net_1h, top10, underwater, launch_ts):
            continue
        assert mcap is not None and liq is not None and vol_1h is not None and vol_5m is not None
        assert chg_1h is not None and net_1h is not None and top10 is not None
        assert underwater is not None and launch_ts is not None

        if mcap < settings.momentum_min_mcap or liq < settings.momentum_min_liq:
            continue
        if vol_1h < settings.momentum_min_vol_1h or vol_1h <= 0:
            continue
        accel = vol_5m * 12.0 / vol_1h
        if accel < settings.momentum_min_accel:
            continue
        buys = int(row.get("buys_1h") or 0)
        sells = int(row.get("sells_1h") or 0)
        if buys + sells < 5:
            continue
        buy_ratio = buys / (buys + sells)
        if buy_ratio < settings.momentum_min_buy_ratio:
            continue
        if net_1h <= 0:
            continue
        if chg_1h < settings.momentum_min_chg_1h or chg_1h > settings.momentum_max_chg_1h:
            continue
        if chg_5m is not None and (
            chg_5m > settings.momentum_max_chg_5m or chg_5m < -5.0
        ):
            continue
        if top10 > settings.max_top10_pct or underwater > settings.max_underwater_pct:
            continue
        clusters = row.get("clusters")
        if clusters is not None and int(clusters) > settings.max_clusters:
            continue
        insider = _f(row, "insider_pct")
        if insider is not None and insider > settings.max_insider_pct:
            continue
        if now - int(launch_ts) < settings.momentum_min_age_s:
            continue
        px = _f(row, "px") or 0.0
        if px <= 0:
            continue
        score = accel * buy_ratio * math.log1p(vol_1h) * (1.0 + max(chg_1h, 0.0))
        out.append(
            Candidate(
                token=token,
                symbol=symbol,
                px=px,
                mcap=mcap,
                liq=liq,
                vol_1h=vol_1h,
                vol_5m=vol_5m,
                accel=accel,
                buy_ratio=buy_ratio,
                net_1h=net_1h,
                chg_1h=chg_1h,
                score=score,
            )
        )
    out.sort(key=lambda c: c.score, reverse=True)
    return out


class MomentumScanner:
    """Poll the universe, open climax candidates, let the broker manage exits."""

    def __init__(self, settings: Settings, intel: Intel, store: Store, paper: PaperBroker) -> None:
        self.settings = settings
        self.intel = intel
        self.store = store
        self.paper = paper
        self._stop = asyncio.Event()
        self.scans = 0
        self.candidates = 0
        self.opened = 0
        self.errors = 0
        self.last_scan_ts = 0

    def stop(self) -> None:
        self._stop.set()

    def _open_count(self) -> int:
        return sum(
            1
            for p in self.store.load_open_positions()
            if str(p.get("strategy") or "copy") == "momentum"
        )

    def _log(self, cand: Candidate, px: float, usd: float, decision: str, reason: str) -> None:
        ts = now_ts()
        try:
            self.store.log_signal(
                {
                    "ts": ts,
                    "wallet": "momentum",
                    "token": cand.token,
                    "symbol": cand.symbol,
                    "side": "buy",
                    "px": px,
                    "usd": usd,
                    "tx": f"scan:{ts}:{cand.token}",
                    "block": 0,
                    "source": "scan",
                    "latency_ms": 0,
                    "whale_score": 0.0,
                    "whale_tier": "momentum",
                    "decision": decision,
                    "reason": reason,
                    "token_meta": None,
                }
            )
        except Exception as exc:  # logging must never stop the scan
            log.warning("scan log failed for %s: %s", cand.symbol, exc)

    async def scan_once(self) -> int:
        try:
            rows = await asyncio.wait_for(
                self.intel.universe(limit=self.settings.momentum_universe), timeout=25.0
            )
        except asyncio.TimeoutError:
            self.errors += 1
            log.warning("universe scan timed out")
            return 0
        self.scans += 1
        self.last_scan_ts = now_ts()
        cands = evaluate(rows, self.settings, self.last_scan_ts)
        self.candidates = len(cands)
        new = 0
        for cand in cands:
            if new >= self.settings.momentum_max_new_per_scan:
                break
            if self._open_count() >= self.settings.momentum_max_positions:
                break
            if len(self.store.load_open_positions()) >= self.settings.max_positions:
                break
            if self.store.position_for_token(cand.token) is not None:
                continue
            if self.store.pending_for_token(cand.token) is not None:
                continue
            last = self.store.last_closed(cand.token)
            if last is not None:
                closed_ago = self.last_scan_ts - int(last.get("exit_ts") or 0)
                if closed_ago < self.settings.momentum_cooldown_s:
                    continue
            halt = self.paper.check_halt()
            if halt:
                log.warning("momentum halted: %s", halt)
                break
            try:
                meta = await asyncio.wait_for(self.intel.token(cand.token), timeout=10.0)
            except asyncio.TimeoutError:
                self.errors += 1
                log.debug("token %s confirmation timed out", cand.symbol)
                continue
            except IntelError as exc:
                self.errors += 1
                log.debug("token %s confirmation failed: %s", cand.symbol, exc)
                continue
            if not isinstance(meta, dict) or not meta:
                continue
            px = float(meta.get("px") or 0.0)
            liq = float(meta.get("liq_usd") or 0.0)
            if px <= 0 or liq < self.settings.momentum_min_liq:
                continue
            chg_5m = _f(meta, "chg_5m")
            if chg_5m is not None and chg_5m > self.settings.momentum_max_chg_5m:
                continue
            # Re-check existence after the awaited price lookup.
            if self.store.position_for_token(cand.token) is not None:
                continue
            size = min(
                self.settings.momentum_per_trade_usd, self.settings.max_token_usd
            )
            result = self.paper.open(
                token=cand.token,
                symbol=cand.symbol,
                px=px,
                usd=size,
                source_wallet="momentum",
                signal_id=None,
                whale_score=0.0,
                liq_usd=liq,
                strategy="momentum",
            )
            if result.ok:
                new += 1
                self.opened += 1
                self._log(cand, px, size, "open", f"accel {cand.accel:.1f} buy {cand.buy_ratio:.2f}")
                print(
                    f"SCAN BUY  {cand.symbol:10s} ${size:.2f} @ {px:.8g} "
                    f"accel {cand.accel:.1f}x buy {cand.buy_ratio:.0%} mcap ${cand.mcap:,.0f}",
                    flush=True,
                )
            else:
                self._log(cand, px, size, "skip", result.reason)
        self.store.set_meta("scan_last_ts", self.last_scan_ts)
        self.store.set_meta("scan_candidates", self.candidates)
        self.store.set_meta("scan_scans", self.scans)
        self.store.set_meta("scan_opened", self.opened)
        return new

    async def run(self) -> None:
        log.info(
            "momentum screen every %.0fs: accel>=%.1fx mcap>=%.0f liq>=%.0f",
            self.settings.momentum_interval_s,
            self.settings.momentum_min_accel,
            self.settings.momentum_min_mcap,
            self.settings.momentum_min_liq,
        )
        while not self._stop.is_set():
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors += 1
                log.warning("scan failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.momentum_interval_s
                )
            except asyncio.TimeoutError:
                pass

    def status(self) -> dict:
        return {
            "scans": self.scans,
            "candidates": self.candidates,
            "opened": self.opened,
            "errors": self.errors,
            "open": self._open_count(),
            "last_scan_ts": self.last_scan_ts,
        }
