"""Paper execution: fills, mark-to-market, exits and account equity.

No venue keys, no signing, no order routing. Every fill is simulated at the
observed pool price with modelled slippage and fees, then tracked to a real
exit rule. The same interface can later be backed by a live venue adapter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Settings
from .intel import Intel, IntelError
from .store import Store
from .util import now_ts

log = logging.getLogger("elys.paper")

EQUITY_SNAPSHOT_S = 15


@dataclass(slots=True)
class FillResult:
    ok: bool
    reason: str
    position_id: int | None = None
    px: float = 0.0
    usd: float = 0.0
    qty: float = 0.0


class PaperBroker:
    def __init__(self, settings: Settings, intel: Intel, store: Store) -> None:
        self.settings = settings
        self.intel = intel
        self.store = store
        self.cash = store.cash(settings.start_equity)
        self._last_snapshot = 0
        self.halt_reason: str | None = None
        self.closed = 0
        self.opened = 0

    # ── accounting helpers ────────────────────────────────────────────────────

    def _entry_px(self, px: float) -> float:
        return px * (1 + self.settings.slippage_bps / 10_000)

    def _exit_px(self, px: float) -> float:
        return px * (1 - self.settings.slippage_bps / 10_000)

    def _fee(self, usd: float) -> float:
        return usd * self.settings.fee_bps / 10_000

    def last_px(self, token: str) -> float | None:
        position = self.store.position_for_token(token)
        if position is None:
            return None
        px = float(position.get("last_px") or position.get("entry_px") or 0.0)
        return px or None

    def positions_usd(self) -> float:
        total = 0.0
        for pos in self.store.load_open_positions():
            px = float(pos.get("last_px") or pos["entry_px"])
            total += float(pos["qty"]) * px
        return total

    def equity(self) -> tuple[float, float, float]:
        open_positions = self.store.load_open_positions()
        positions_usd = sum(float(p["qty"]) * float(p["last_px"] or p["entry_px"]) for p in open_positions)
        return self.cash, positions_usd, self.cash + positions_usd

    # ── risk ──────────────────────────────────────────────────────────────────

    def day_open_equity(self) -> float:
        midnight = now_ts() - (now_ts() % 86_400)
        rows = self.store.query(
            "SELECT equity_usd FROM equity WHERE ts >= ? ORDER BY id LIMIT 1", (midnight,)
        )
        if rows:
            return float(rows[0]["equity_usd"])
        return self.settings.start_equity

    def check_halt(self) -> str | None:
        _cash, _pos, equity = self.equity()
        baseline = self.day_open_equity()
        if baseline <= 0:
            return None
        drawdown = (equity - baseline) / baseline
        if drawdown <= -self.settings.max_daily_loss_pct:
            return f"daily loss {drawdown:.1%} <= -{self.settings.max_daily_loss_pct:.1%}"
        if equity <= self.settings.start_equity * (1 - self.settings.max_daily_loss_pct * 3):
            return f"equity {equity:.2f} below kill-switch"
        return None

    # ── fills ─────────────────────────────────────────────────────────────────

    def open(
        self,
        *,
        token: str,
        symbol: str,
        px: float,
        usd: float,
        source_wallet: str,
        signal_id: int | None,
        whale_score: float,
    ) -> FillResult:
        if px <= 0 or usd <= 0:
            return FillResult(False, "bad price/size")
        exec_px = self._entry_px(px)
        qty = usd / exec_px
        fee = self._fee(usd)
        if self.cash < usd + fee:
            return FillResult(False, f"insufficient cash ({self.cash:.2f})")

        ts = now_ts()
        position_id = self.store.open_position(
            {
                "token": token,
                "symbol": symbol,
                "entry_px": exec_px,
                "qty": qty,
                "usd": usd,
                "entry_ts": ts,
                "last_ts": ts,
                "source_wallet": source_wallet,
                "source_signal": signal_id,
                "conviction": 1,
                "peak_px": exec_px,
                "fee_usd": fee,
            }
        )
        self.store.update_position(position_id, last_px=exec_px)
        self.store.add_fill(
            {
                "ts": ts,
                "position_id": position_id,
                "side": "buy",
                "px": exec_px,
                "qty": qty,
                "usd": usd,
                "fee_usd": fee,
                "kind": "copy",
                "tx": None,
            }
        )
        self.cash -= usd + fee
        self.store.set_meta("cash", round(self.cash, 8))
        self.opened += 1
        self._snapshot(force=True)
        return FillResult(True, "opened", position_id, exec_px, usd, qty)

    def add(self, position: dict, *, px: float, usd: float) -> FillResult:
        if px <= 0 or usd <= 0:
            return FillResult(False, "bad price/size")
        exec_px = self._entry_px(px)
        qty = usd / exec_px
        fee = self._fee(usd)
        if self.cash < usd + fee:
            return FillResult(False, f"insufficient cash ({self.cash:.2f})")
        ts = now_ts()
        self.store.add_to_position(int(position["id"]), qty, usd, exec_px, fee, ts)
        self.store.update_position(int(position["id"]), last_px=exec_px)
        self.store.add_fill(
            {
                "ts": ts,
                "position_id": int(position["id"]),
                "side": "buy",
                "px": exec_px,
                "qty": qty,
                "usd": usd,
                "fee_usd": fee,
                "kind": "add",
                "tx": None,
            }
        )
        self.cash -= usd + fee
        self.store.set_meta("cash", round(self.cash, 8))
        return FillResult(True, "added", int(position["id"]), exec_px, usd, qty)

    def close(self, position: dict, *, px: float, reason: str) -> FillResult:
        qty = float(position["qty"])
        cost = float(position["usd"])
        if px <= 0 or qty <= 0:
            return FillResult(False, "bad price/qty")
        exec_px = self._exit_px(px)
        proceeds = qty * exec_px
        fee = self._fee(proceeds)
        pnl = proceeds - fee - cost
        ts = now_ts()
        self.store.close_position(
            int(position["id"]),
            exit_px=exec_px,
            exit_ts=ts,
            exit_reason=reason,
            pnl_usd=round(pnl, 8),
            pnl_pct=round(pnl / cost, 8) if cost else 0.0,
            last_px=exec_px,
        )
        self.store.add_fill(
            {
                "ts": ts,
                "position_id": int(position["id"]),
                "side": "sell",
                "px": exec_px,
                "qty": qty,
                "usd": proceeds,
                "fee_usd": fee,
                "kind": reason,
                "tx": None,
            }
        )
        self.cash += proceeds - fee
        self.store.set_meta("cash", round(self.cash, 8))
        self.closed += 1
        self._snapshot(force=True)
        return FillResult(True, reason, int(position["id"]), exec_px, proceeds, qty)

    def close_token(self, token: str, *, px: float, reason: str) -> FillResult | None:
        position = self.store.position_for_token(token)
        if position is None:
            return None
        return self.close(position, px=px, reason=reason)

    # ── mark to market / exits ────────────────────────────────────────────────

    async def mark(self) -> None:
        positions = self.store.load_open_positions()
        if not positions:
            self._snapshot(force=True)
            return
        # One token request per unique token per cycle (intel caches ~4s).
        tokens: dict[str, dict] = {}
        for pos in positions:
            token = pos["token"]
            if token in tokens:
                continue
            try:
                tokens[token] = await self.intel.token(token, ttl=3.0)
            except IntelError as exc:
                log.debug("token %s mark failed: %s", token, exc)

        now = now_ts()
        for pos in positions:
            meta = tokens.get(pos["token"])
            if not meta:
                continue
            px = float(meta.get("px") or 0.0)
            if px <= 0:
                continue
            peak = max(float(pos.get("peak_px") or 0.0), px)
            self.store.update_position(int(pos["id"]), peak_px=peak, last_px=px, last_ts=now)

            if int(meta.get("rug") or 0) or str(meta.get("hp_verdict") or "").lower() == "honeypot":
                self.close(pos, px=px, reason="rug")
                continue

            entry = float(pos["entry_px"])
            change = px / entry - 1 if entry else 0.0
            peak_change = peak / entry - 1 if entry else 0.0

            if change <= -self.settings.stop_loss_pct:
                self.close(pos, px=px, reason="stop_loss")
                continue
            if change >= self.settings.take_profit_pct:
                self.close(pos, px=px, reason="take_profit")
                continue
            if peak_change >= self.settings.trail_arm_pct:
                giveback = (peak - px) / peak if peak else 0.0
                if giveback >= self.settings.trail_giveback_pct:
                    self.close(pos, px=px, reason="trail_stop")
                    continue
            if now - int(pos["entry_ts"]) >= self.settings.max_hold_min * 60:
                self.close(pos, px=px, reason="time_stop")

        self._snapshot()

    def _snapshot(self, *, force: bool = False) -> None:
        now = now_ts()
        if not force and now - self._last_snapshot < EQUITY_SNAPSHOT_S:
            return
        cash, positions_usd, _equity = self.equity()
        self.store.add_equity(now, round(cash, 8), round(positions_usd, 8))
        self._last_snapshot = now

    # ── introspection ─────────────────────────────────────────────────────────

    def status(self) -> dict:
        cash, positions_usd, equity = self.equity()
        return {
            "cash": round(cash, 2),
            "positions_usd": round(positions_usd, 2),
            "equity": round(equity, 2),
            "open": len(self.store.load_open_positions()),
            "opened": self.opened,
            "closed": self.closed,
            "halt": self.halt_reason,
        }
