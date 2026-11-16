"""Copy strategy: decide whether an observed whale swap becomes a paper fill."""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass

from .config import Settings
from .intel import Intel, IntelError
from .monitor import Signal
from .paper import PaperBroker
from .scoring import copy_weight
from .store import Store
from .util import now_ts

log = logging.getLogger("elys.strategy")


@dataclass(slots=True)
class Decision:
    action: str  # open | add | close | skip | arm
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
        self._recent_buys: dict[str, deque[tuple[int, str]]] = defaultdict(deque)
        self._seed_conviction()

    def _seed_conviction(self) -> None:
        """Recover the rolling buy window across a process restart."""
        cutoff = now_ts() - self.settings.conviction_window_s
        for row in reversed(self.store.recent_signals(1_000)):
            if (row.get("side") or "") != "buy" or int(row.get("ts") or 0) < cutoff:
                continue
            if float(row.get("whale_score") or 0) < self.settings.min_copy_score:
                continue
            self._recent_buys[row["token"]].append((int(row["ts"]), row["wallet"]))

    def _record_buy(self, signal: Signal) -> None:
        if signal.whale_score < self.settings.min_copy_score:
            return
        window = self.settings.conviction_window_s
        queue = self._recent_buys[signal.token]
        queue.append((signal.ts, signal.wallet))
        cutoff = signal.ts - window
        while queue and queue[0][0] < cutoff:
            queue.popleft()

    def _conviction(self, signal: Signal) -> int:
        cutoff = now_ts() - self.settings.conviction_window_s
        wallets = {
            wallet for ts, wallet in self._recent_buys.get(signal.token, ()) if ts >= cutoff
        }
        wallets.add(signal.wallet)
        return len(wallets)

    # ── signals ───────────────────────────────────────────────────────────────

    async def on_signal(self, signal: Signal) -> Decision:
        if signal.side == "sell":
            return await self._on_sell(signal)
        return await self._on_buy(signal)

    async def _on_sell(self, signal: Signal) -> Decision:
        position = self.store.position_for_token(signal.token)
        if position is None:
            if self.store.pending_for_token(signal.token) is not None:
                self.store.remove_pending(signal.token)
                return self._skip("pending cancelled")
            return self._skip("no_position")
        if not self.settings.copy_sells:
            return self._skip("sells_disabled")
        # The sale is triggered by the whale's fill, but the desk sells into the
        # market it can actually reach now — not at the whale's stale price.
        px, liq = signal.px, 0.0
        try:
            meta = await self.intel.token(signal.token)
            if isinstance(meta, dict) and float(meta.get("px") or 0) > 0:
                px = float(meta["px"]) if self.settings.fill_at_market else signal.px
                liq = float(meta.get("liq_usd") or 0.0)
        except IntelError as exc:
            log.debug("sell price lookup failed for %s: %s", signal.symbol, exc)
        result = self.paper.close(
            position, px=px, reason=f"whale_exit:{signal.symbol}", liq_usd=liq
        )
        if not result.ok:
            return self._skip(result.reason)
        return Decision("close", f"whale sold {signal.symbol}", result.position_id, result.usd)

    def _wallet_veto(self, signal: Signal) -> str:
        """Reject wallets whose tape is too fast to copy at retail latency."""
        if signal.whale_trades > self.settings.max_wallet_trades:
            return (
                f"wallet {signal.whale_trades} trades > "
                f"{self.settings.max_wallet_trades}"
            )
        if signal.whale_tokens > 0:
            per_token = signal.whale_trades / signal.whale_tokens
            if per_token > self.settings.max_wallet_trades_per_token:
                return (
                    f"wallet {per_token:.0f} trades/token > "
                    f"{self.settings.max_wallet_trades_per_token}"
                )
        return ""

    async def _on_buy(self, signal: Signal) -> Decision:
        if signal.whale_score < self.settings.min_copy_score:
            return self._skip(
                f"whale score {signal.whale_score:.0f} < {self.settings.min_copy_score:.0f}"
            )
        wallet_block = self._wallet_veto(signal)
        if wallet_block:
            return self._skip(wallet_block)
        self._record_buy(signal)
        if not self.settings.copy_enabled:
            return self._skip("copy disabled")
        if signal.usd < self.settings.min_whale_usd:
            return self._skip("whale trade below floor")

        halt = self.paper.check_halt()
        if halt:
            return self._skip(f"halted: {halt}")

        try:
            meta = await self.intel.token(signal.token)
        except IntelError as exc:
            return self._skip(f"token lookup failed: {exc}")
        if not isinstance(meta, dict) or not meta:
            return self._skip("token metadata missing")

        veto = self._token_veto(meta)
        if veto:
            return self._skip(f"token veto: {veto}")

        # Detection latency is diagnostic; what matters is how old the whale's
        # fill is when this decision is actually made.
        age = now_ts() - int(signal.ts)
        if age > self.settings.max_signal_latency_s:
            return self._skip(
                f"signal age {age}s > {self.settings.max_signal_latency_s}s"
            )

        liq = float(meta.get("liq_usd") or 0.0)
        market_px = float(meta.get("px") or 0.0)
        if market_px <= 0:
            return self._skip("no price")
        if liq > 0 and signal.usd / liq > self.settings.max_trade_liq_pct:
            return self._skip(
                f"whale trade {signal.usd / liq:.1%} of liquidity > {self.settings.max_trade_liq_pct:.1%}"
            )
        # In retrace mode the resting limit enforces the no-chase rule, so the
        # market may already be above the whale's fill when the order is armed.
        if (
            self.settings.entry_mode != "retrace"
            and market_px > signal.px * (1 + self.settings.max_chase_pct)
        ):
            return self._skip(
                f"chase {market_px / signal.px - 1:+.1%} > {self.settings.max_chase_pct:.0%}"
            )
        fill_px = market_px if self.settings.fill_at_market else signal.px
        conviction = self._conviction(signal)

        if self.settings.entry_mode == "retrace":
            return self._arm_or_exit(signal, market_px, liq, conviction)

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
            result = self.paper.add(existing, px=fill_px, usd=size, liq_usd=liq)
            return Decision("add" if result.ok else "skip", result.reason, result.position_id, size)

        if conviction < self.settings.min_conviction_wallets:
            return self._skip(
                f"conviction {conviction}/{self.settings.min_conviction_wallets}"
            )

        blocked = self._open_block(signal.token)
        if blocked:
            return self._skip(blocked)

        result = self.paper.open(
            token=signal.token,
            symbol=signal.symbol,
            px=fill_px,
            usd=size,
            source_wallet=signal.wallet,
            signal_id=None,
            whale_score=signal.whale_score,
            liq_usd=liq,
        )
        return Decision("open" if result.ok else "skip", result.reason, result.position_id, size)

    # ── retrace entries ───────────────────────────────────────────────────────

    def _open_block(self, token: str) -> str:
        """Reasons a new position in ``token`` must not be opened right now."""
        last = self.store.last_closed(token)
        if last is not None:
            closed_ago = now_ts() - int(last.get("exit_ts") or 0)
            if str(last.get("exit_reason") or "").startswith("stop_loss") and closed_ago < self.settings.stop_cooldown_s:
                return f"stop cooldown {closed_ago}s"
            if closed_ago < self.settings.reentry_guard_s:
                return f"re-entry guard {closed_ago}s"
        exposure = len(self.store.load_open_positions()) + len(self.store.pending_entries())
        if exposure >= self.settings.max_positions:
            return "max_positions"
        halt = self.paper.check_halt()
        if halt:
            self.paper.halt_reason = halt
            return f"halted: {halt}"
        return ""

    def _arm_or_exit(
        self, signal: Signal, market_px: float, liq: float, conviction: int
    ) -> Decision:
        """Retrace mode: exit into the next buy, otherwise rest a limit bid.

        The leader's fill is a trigger, not an execution price. We only buy if the
        market comes back to (or below) the leader's price, and we sell into the
        next buy's pop instead of waiting for the leader's exit.
        """
        existing = self.store.position_for_token(signal.token)
        if existing is not None:
            if not self.settings.pop_exit:
                return self._skip("position open; pop exit disabled")
            result = self.paper.close(
                existing, px=market_px, reason=f"pop_exit:{signal.symbol}", liq_usd=liq
            )
            if not result.ok:
                return self._skip(result.reason)
            return Decision("close", f"next buy {signal.symbol}", result.position_id, result.usd)

        if self.store.pending_for_token(signal.token) is not None:
            return self._skip("pending entry")

        if conviction < self.settings.min_conviction_wallets:
            return self._skip(
                f"conviction {conviction}/{self.settings.min_conviction_wallets}"
            )

        blocked = self._open_block(signal.token)
        if blocked:
            return self._skip(blocked)

        limit = signal.px * (1 + self.settings.retrace_limit_pct)
        now = now_ts()
        self.store.upsert_pending(
            {
                "token": signal.token,
                "symbol": signal.symbol,
                "limit_px": limit,
                "expires_ts": now + self.settings.retrace_ttl_s,
                "whale": signal.wallet,
                "whale_score": signal.whale_score,
                "created_ts": now,
                "signal_ts": signal.ts,
                "signal_usd": signal.usd,
                "signal_liq": liq,
                "conviction": conviction,
            }
        )
        size = min(
            self.settings.per_trade_usd * copy_weight(signal.whale_score),
            self.settings.max_token_usd,
        )
        return Decision("arm", f"limit {limit:.8g} ttl {self.settings.retrace_ttl_s}s", None, size)

    async def check_pending(self) -> list[Decision]:
        """Fill resting limits whose market has come back to the limit."""
        out: list[Decision] = []
        max_age = self.settings.max_signal_latency_s + self.settings.retrace_ttl_s + 60
        for order in self.store.pending_entries():
            token = order["token"]
            now = now_ts()
            if now >= int(order["expires_ts"]):
                self.store.remove_pending(token)
                out.append(Decision("skip", f"retrace expired: {order.get('symbol') or token}"))
                continue
            try:
                meta = await self.intel.token(token, ttl=3.0)
            except IntelError as exc:
                log.debug("pending %s price failed: %s", token, exc)
                continue
            if not isinstance(meta, dict) or not meta:
                continue
            px = float(meta.get("px") or 0.0)
            if px <= 0 or px > float(order["limit_px"]):
                continue
            veto = self._token_veto(meta)
            if veto:
                self.store.remove_pending(token)
                out.append(Decision("skip", f"retrace veto: {veto}"))
                continue
            blocked = self._open_block(token)
            if blocked:
                continue
            # Claim immediately before the fill: a concurrent sell/cancel or an
            # expired TTL must not leave a stale order able to open later.
            claimed = self.store.claim_pending(token)
            if claimed is None:
                continue
            if now_ts() >= int(claimed["expires_ts"]):
                out.append(Decision("skip", f"retrace expired: {claimed.get('symbol') or token}"))
                continue
            signal_ts = int(claimed.get("signal_ts") or 0)
            if signal_ts and now_ts() - signal_ts > max_age:
                out.append(Decision("skip", f"retrace stale signal: {claimed.get('symbol') or token}"))
                continue
            signal_usd = float(claimed.get("signal_usd") or 0.0)
            liq = float(meta.get("liq_usd") or 0.0)
            if signal_usd and liq > 0 and signal_usd / liq > self.settings.max_trade_liq_pct:
                out.append(Decision("skip", f"retrace trade/liq: {claimed.get('symbol') or token}"))
                continue
            size = min(
                self.settings.per_trade_usd * copy_weight(float(claimed.get("whale_score") or 0.0)),
                self.settings.max_token_usd,
            )
            result = self.paper.open(
                token=token,
                symbol=str(claimed.get("symbol") or ""),
                px=px,
                usd=size,
                source_wallet=str(claimed.get("whale") or ""),
                signal_id=None,
                whale_score=float(claimed.get("whale_score") or 0.0),
                liq_usd=liq,
            )
            if result.ok:
                out.append(Decision("open", f"retrace fill {claimed.get('symbol')}", result.position_id, size))
            else:
                out.append(Decision("skip", result.reason))
        return out

    def _token_veto(self, meta: dict) -> str:
        verdict = str(meta.get("hp_verdict") or "").lower()
        if verdict != "clean":
            return f"honeypot verdict {verdict or 'unknown'}"
        if int(meta.get("hp") or 0) != 0:
            return "honeypot flag"
        if int(meta.get("rug") or 0) or int(meta.get("rug_by_creator") or 0):
            return "rugged"
        if int(meta.get("hp_wiped") or 0):
            return "holders wiped"
        liq = meta.get("liq_usd")
        if liq is None:
            return "unknown liquidity"
        liq = float(liq)
        if liq < self.settings.min_liq_usd:
            return f"liquidity {liq:,.0f} < {self.settings.min_liq_usd:,.0f}"
        top10 = meta.get("top10_pct")
        if top10 is None:
            return "unknown top10"
        if float(top10) > self.settings.max_top10_pct:
            return f"top10 {float(top10):.0f}% > {self.settings.max_top10_pct:.0f}%"
        mcap = meta.get("mcap")
        if mcap is None:
            return "unknown mcap"
        if float(mcap) < self.settings.min_mcap_usd:
            return f"mcap {float(mcap):,.0f} < {self.settings.min_mcap_usd:,.0f}"
        underwater = meta.get("underwater_pct")
        if underwater is None:
            return "unknown underwater"
        if float(underwater) > self.settings.max_underwater_pct:
            return f"underwater {float(underwater):.0f}% > {self.settings.max_underwater_pct:.0f}%"
        clusters = meta.get("clusters")
        if clusters is not None and int(clusters) > self.settings.max_clusters:
            return f"clusters {int(clusters)} > {self.settings.max_clusters}"
        insider = meta.get("insider_pct")
        if insider is not None and float(insider) > self.settings.max_insider_pct:
            return f"insider {float(insider):.0f}% > {self.settings.max_insider_pct:.0f}%"
        if float(meta.get("px") or 0.0) <= 0:
            return "no price"
        launch_ts = meta.get("launch_ts")
        if not launch_ts:
            return "unknown launch"
        if now_ts() - int(launch_ts) < self.settings.min_token_age_s:
            return "too young"
        buys = int(meta.get("buys_1h") or 0)
        sells = int(meta.get("sells_1h") or 0)
        if buys + sells < 5:
            return "thin hourly tape"
        if buys / (buys + sells) < self.settings.min_buy_ratio:
            return f"buy ratio {buys / (buys + sells):.2f} < {self.settings.min_buy_ratio:.2f}"
        vol_1h = float(meta.get("vol_1h") or 0.0)
        if vol_1h <= 0:
            return "no hourly volume"
        vol_5m = float(meta.get("vol_5m") or 0.0)
        accel = vol_5m * 12.0 / vol_1h
        if accel < self.settings.min_vol_accel:
            return f"volume accel {accel:.2f} < {self.settings.min_vol_accel:.2f}"
        if float(meta.get("net_1h") or 0.0) <= 0:
            return "net hourly outflow"
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
            px, liq = 0.0, 0.0
            try:
                meta = await self.intel.token(token)
                if isinstance(meta, dict):
                    px = float(meta.get("px") or 0.0)
                    liq = float(meta.get("liq_usd") or 0.0)
            except IntelError as exc:
                log.debug("rug alert price lookup failed for %s: %s", token, exc)
            reason = "rug_alert"
            if px <= 0:
                # No executable quote: impair rather than pretend the last mark
                # was still sellable.
                stale = self.paper.last_px(token) or float(
                    position["last_px"] or position["entry_px"]
                )
                px = stale * (1 - self.settings.impaired_haircut_pct)
                reason = "rug_impaired"
            result = self.paper.close(position, px=px, reason=reason, liq_usd=liq)
            return Decision("close", "telegram rug warning", result.position_id, result.usd)
        return None

    # ── helpers ───────────────────────────────────────────────────────────────

    def _skip(self, reason: str) -> Decision:
        key = reason.split(":")[0][:60]
        self.skipped[key] = self.skipped.get(key, 0) + 1
        return Decision("skip", reason)
