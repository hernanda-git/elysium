"""Copy strategy: decide whether an observed whale swap becomes a paper fill."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Settings
from .intel import Intel, IntelError
from .monitor import Signal
from .paper import PaperBroker
from .scoring import copy_weight
from .store import Store
from .util import now_ts

log = logging.getLogger("elys.strategy")

MIN_COPY_SCORE = 40.0


@dataclass(slots=True)
class Decision:
    action: str  # open | add | close | skip
    reason: str
    position_id: int | None = None
    usd: float = 0.0


class CopyStrategy:
    def __init__(
        self,
        settings: Settings,
        intel: Intel,
        store: Store,
        paper: PaperBroker,
    ) -> None:
        self.settings = settings
        self.intel = intel
        self.store = store
        self.paper = paper
        self.skipped: dict[str, int] = {}

    # ── signals ───────────────────────────────────────────────────────────────

    async def on_signal(self, signal: Signal) -> Decision:
        if signal.side == "sell":
            return self._on_sell(signal)
        return await self._on_buy(signal)

    def _on_sell(self, signal: Signal) -> Decision:
        position = self.store.position_for_token(signal.token)
        if position is None:
            return self._skip("no_position")
        if not self.settings.copy_sells:
            return self._skip("sells_disabled")
        result = self.paper.close(position, px=signal.px, reason=f"whale_exit:{signal.symbol}")
        if not result.ok:
            return self._skip(result.reason)
        return Decision("close", f"whale sold {signal.symbol}", result.position_id, result.usd)

    async def _on_buy(self, signal: Signal) -> Decision:
        if signal.whale_score < MIN_COPY_SCORE:
            return self._skip(f"whale score {signal.whale_score:.0f} < {MIN_COPY_SCORE:.0f}")

        try:
            meta = await self.intel.token(signal.token)
        except IntelError as exc:
            return self._skip(f"token lookup failed: {exc}")

        veto = self._token_veto(meta)
        if veto:
            return self._skip(f"token veto: {veto}")

        size = min(
            self.settings.per_trade_usd * copy_weight(signal.whale_score),
            self.settings.max_token_usd,
        )

        existing = self.store.position_for_token(signal.token)
        if existing is not None:
            if int(existing["conviction"]) >= self.settings.max_adds_per_token:
                return self._skip("max_adds")
            if float(existing["usd"]) + size > self.settings.max_token_usd:
                return self._skip("token_cap")
            result = self.paper.add(existing, px=signal.px, usd=size)
            return Decision("add" if result.ok else "skip", result.reason, result.position_id, size)

        if len(self.store.load_open_positions()) >= self.settings.max_positions:
            return self._skip("max_positions")

        halt = self.paper.check_halt()
        if halt:
            self.paper.halt_reason = halt
            return self._skip(f"halted: {halt}")

        result = self.paper.open(
            token=signal.token,
            symbol=signal.symbol,
            px=signal.px,
            usd=size,
            source_wallet=signal.wallet,
            signal_id=None,
            whale_score=signal.whale_score,
        )
        return Decision("open" if result.ok else "skip", result.reason, result.position_id, size)

    def _token_veto(self, meta: dict) -> str:
        verdict = str(meta.get("hp_verdict") or "").lower()
        if verdict and verdict not in {"clean"}:
            return f"honeypot verdict {verdict}"
        if int(meta.get("hp") or 0) not in (0,):
            return "honeypot flag"
        if int(meta.get("rug") or 0) or int(meta.get("rug_by_creator") or 0):
            return "rugged"
        if int(meta.get("hp_wiped") or 0):
            return "holders wiped"
        liq = float(meta.get("liq_usd") or 0.0)
        if liq < self.settings.min_liq_usd:
            return f"liquidity {liq:,.0f} < {self.settings.min_liq_usd:,.0f}"
        top10 = float(meta.get("top10_pct") or 0.0)
        if top10 > self.settings.max_top10_pct:
            return f"top10 {top10:.0f}% > {self.settings.max_top10_pct:.0f}%"
        if float(meta.get("px") or 0.0) <= 0:
            return "no price"
        launch_ts = int(meta.get("launch_ts") or 0)
        if launch_ts and now_ts() - launch_ts < self.settings.min_token_age_s:
            return "too young"
        return ""

    # ── telegram events ───────────────────────────────────────────────────────

    async def on_tg_event(self, event: dict) -> Decision | None:
        kind = event.get("kind") or ""
        token = event.get("token") or ""
        if not token:
            return None
        if kind in {"rug_warning", "honeypot"}:
            position = self.store.position_for_token(token)
            if position is None:
                return None
            px = self.paper.last_px(token) or float(position["last_px"] or position["entry_px"])
            result = self.paper.close(position, px=px, reason="rug_alert")
            return Decision("close", "telegram rug warning", result.position_id, result.usd)
        return None

    # ── helpers ───────────────────────────────────────────────────────────────

    def _skip(self, reason: str) -> Decision:
        key = reason.split(":")[0][:60]
        self.skipped[key] = self.skipped.get(key, 0) + 1
        return Decision("skip", reason)
