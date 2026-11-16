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

    def _impact_bps(self, usd: float, liq_usd: float) -> float:
        """Constant-product price impact for one side, capped.

        Filling ``$25`` into a ``$15k`` pool costs ``usd / liq`` (≈17 bps); the
        flat slippage setting is only the execution floor on top of that.
        """
        if liq_usd <= 0 or usd <= 0:
            return 0.0
        return min(10_000.0 * usd / liq_usd, self.settings.max_impact_bps)

    def _entry_px(self, px: float, *, usd: float = 0.0, liq_usd: float = 0.0) -> float:
        bps = self.settings.slippage_bps + self._impact_bps(usd, liq_usd)
        return px * (1 + bps / 10_000)

    def _exit_px(self, px: float, *, usd: float = 0.0, liq_usd: float = 0.0) -> float:
        bps = self.settings.slippage_bps + self._impact_bps(usd, liq_usd)
        return px * (1 - bps / 10_000)

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
        today = now_ts() // 86_400
        if self.store.get_meta("halt_global") == "1":
            return self.store.get_meta("halt_global_reason") or "global kill switch"
        if self.store.get_meta("halt_day") == str(today):
            return self.store.get_meta("halt_reason") or "daily loss halt"
        _cash, _pos, equity = self.equity()
        baseline = self.day_open_equity()
        if baseline > 0:
            drawdown = (equity - baseline) / baseline
            if drawdown <= -self.settings.max_daily_loss_pct:
                reason = f"daily loss {drawdown:.1%} <= -{self.settings.max_daily_loss_pct:.1%}"
                self.store.set_meta("halt_day", today)
                self.store.set_meta("halt_reason", reason)
                return reason
        floor = self.settings.start_equity * (1 - self.settings.max_daily_loss_pct * 3)
        if equity <= floor:
            reason = f"equity {equity:.2f} below kill-switch {floor:.2f}"
            self.store.set_meta("halt_global", "1")
            self.store.set_meta("halt_global_reason", reason)
            return reason
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
        liq_usd: float = 0.0,
        strategy: str = "copy",
    ) -> FillResult:
        if px <= 0 or usd <= 0:
            return FillResult(False, "bad price/size")
        exec_px = self._entry_px(px, usd=usd, liq_usd=liq_usd)
        qty = usd / exec_px
        fee = self._fee(usd)
        if self.cash < usd + fee:
            return FillResult(False, f"insufficient cash ({self.cash:.2f})")

        ts = now_ts()
        position = {
            "token": token,
            "symbol": symbol,
            "entry_px": exec_px,
            "qty": qty,
            "usd": usd,
            "entry_ts": ts,
            "last_ts": ts,
            "source_wallet": source_wallet,
            "source_signal": signal_id,
            "strategy": strategy,
            "conviction": 1,
            "peak_px": exec_px,
            "fee_usd": fee,
        }
        fill = {
            "ts": ts,
            "side": "buy",
            "px": exec_px,
            "qty": qty,
            "usd": usd,
            "fee_usd": fee,
            "kind": "copy",
            "tx": None,
        }
        cash_after = self.cash - usd - fee
        position_id = self.store.open_with_fill(position, fill, cash_after)
        self.cash = cash_after
        self.opened += 1
        self._snapshot(force=True)
        return FillResult(True, "opened", position_id, exec_px, usd, qty)

    def add(self, position: dict, *, px: float, usd: float, liq_usd: float = 0.0) -> FillResult:
        if px <= 0 or usd <= 0:
            return FillResult(False, "bad price/size")
        exec_px = self._entry_px(px, usd=usd, liq_usd=liq_usd)
        qty = usd / exec_px
        fee = self._fee(usd)
        if self.cash < usd + fee:
            return FillResult(False, f"insufficient cash ({self.cash:.2f})")
        ts = now_ts()
        fill = {
            "ts": ts,
            "side": "buy",
            "px": exec_px,
            "qty": qty,
            "usd": usd,
            "fee_usd": fee,
            "kind": "add",
            "tx": None,
        }
        cash_after = self.cash - usd - fee
        ok = self.store.add_with_fill(
            int(position["id"]), qty, usd, exec_px, fee, ts, fill, cash_after
        )
        if not ok:
            return FillResult(False, "position already closed")
        self.cash = cash_after
        return FillResult(True, "added", int(position["id"]), exec_px, usd, qty)

    def close(self, position: dict, *, px: float, reason: str, liq_usd: float = 0.0) -> FillResult:
        qty = float(position["qty"])
        cost = float(position["usd"])
        entry_fees = float(position.get("fee_usd") or 0.0)
        if px <= 0 or qty <= 0:
            return FillResult(False, "bad price/qty")
        exec_px = self._exit_px(px, usd=qty * px, liq_usd=liq_usd)
        proceeds = qty * exec_px
        fee = self._fee(proceeds)
        # Entry fees are part of the cost basis. Cash already paid them at entry,
        # so realised PnL must deduct them too or the report overstates profit.
        cost_basis = cost + entry_fees
        pnl = proceeds - fee - cost_basis
        ts = now_ts()
        fields = {
            "exit_px": exec_px,
            "exit_ts": ts,
            "exit_reason": reason,
            "pnl_usd": round(pnl, 8),
            "pnl_pct": round(pnl / cost_basis, 8) if cost_basis else 0.0,
            "last_px": exec_px,
        }
        fill = {
            "ts": ts,
            "side": "sell",
            "px": exec_px,
            "qty": qty,
            "usd": proceeds,
            "fee_usd": fee,
            "kind": reason,
            "tx": None,
        }
        cash_after = self.cash + proceeds - fee
        if not self.store.close_with_fill(int(position["id"]), fields, fill, cash_after):
            return FillResult(False, "position already closed")
        self.cash = cash_after
        self.closed += 1
        self._snapshot(force=True)
        return FillResult(True, reason, int(position["id"]), exec_px, proceeds, qty)

    def close_token(self, token: str, *, px: float, reason: str, liq_usd: float = 0.0) -> FillResult | None:
        position = self.store.position_for_token(token)
        if position is None:
            return None
        return self.close(position, px=px, reason=reason, liq_usd=liq_usd)

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
            liq = float(meta.get("liq_usd") or 0.0)
            rug = (
                int(meta.get("rug") or 0)
                or int(meta.get("rug_by_creator") or 0)
                or int(meta.get("hp_wiped") or 0)
                or str(meta.get("hp_verdict") or "").lower() == "honeypot"
            )
            if rug and px <= 0:
                # Nothing is executable at the old quote. Impair the position
                # instead of marking it at a price that cannot be realised.
                stale = float(pos.get("last_px") or pos["entry_px"])
                self.close(
                    pos,
                    px=stale * (1 - self.settings.impaired_haircut_pct),
                    reason="rug_impaired",
                )
                continue
            if px <= 0:
                # Unknown quote: keep the last mark, but do not fabricate a price.
                continue
            peak = max(float(pos.get("peak_px") or 0.0), px)
            self.store.update_position(int(pos["id"]), peak_px=peak, last_px=px, last_ts=now)

            if rug:
                self.close(pos, px=px, reason="rug", liq_usd=liq)
                continue

            entry = float(pos["entry_px"])
            change = px / entry - 1 if entry else 0.0
            peak_change = peak / entry - 1 if entry else 0.0
            if str(pos.get("strategy") or "copy") == "momentum":
                stop_loss = self.settings.momentum_stop_loss_pct
                take_profit = self.settings.momentum_take_profit_pct
                trail_arm = self.settings.momentum_trail_arm_pct
                trail_give = self.settings.momentum_trail_giveback_pct
                max_hold = self.settings.momentum_max_hold_min * 60
            else:
                stop_loss = self.settings.stop_loss_pct
                take_profit = self.settings.take_profit_pct
                trail_arm = self.settings.trail_arm_pct
                trail_give = self.settings.trail_giveback_pct
                max_hold = self.settings.max_hold_min * 60

            if change <= -stop_loss:
                self.close(pos, px=px, reason="stop_loss", liq_usd=liq)
                continue
            if change >= take_profit:
                self.close(pos, px=px, reason="take_profit", liq_usd=liq)
                continue
            if peak_change >= trail_arm:
                giveback = (peak - px) / peak if peak else 0.0
                if giveback >= trail_give:
                    self.close(pos, px=px, reason="trail_stop", liq_usd=liq)
                    continue
            if now - int(pos["entry_ts"]) >= max_hold:
                self.close(pos, px=px, reason="time_stop", liq_usd=liq)

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
