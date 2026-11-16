"""Whale monitor: SSE fast path plus round-robin polling fallback.

Detection is decoupled from execution. The monitor only turns observed whale
swaps into ``Signal`` objects and hands them to the engine queue; sizing,
safety checks and fills live in ``strategy``/``paper``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from .config import Settings
from .intel import Intel, IntelError
from .store import Store
from .stream import BlockStream
from .util import norm_addr, now_ts

log = logging.getLogger("elys.monitor")


@dataclass(slots=True)
class Signal:
    ts: int
    wallet: str
    token: str
    symbol: str
    side: str
    px: float
    usd: float
    tx: str
    block: int
    source: str
    latency_ms: int
    whale_score: float
    whale_tier: str
    whale_trades: int = 0
    whale_tokens: int = 0
    detected_ts: int = field(default_factory=now_ts)


class Monitor:
    def __init__(
        self,
        settings: Settings,
        intel: Intel,
        store: Store,
        queue: asyncio.Queue[Signal],
    ) -> None:
        self.settings = settings
        self.intel = intel
        self.store = store
        self.queue = queue
        self.whales: dict[str, dict] = {}
        self.watch: set[str] = set()
        self.stream = BlockStream(settings)
        self._stop = asyncio.Event()
        self._seen: dict[str, int] = {}
        self._last_ts: dict[str, int] = {}
        self._inflight: set[str] = set()
        self._decode_after: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()
        self._started = now_ts()
        self.lookback_s = 900
        self.signals = 0
        self.hits = 0
        self.errors = 0

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def load_whales(self, limit: int | None = None) -> int:
        rows = self.store.load_whales(active_only=True, limit=limit)
        self.whales = {w["address"]: w for w in rows}
        self.watch = set(self.whales)
        start = now_ts()
        for addr in self.whales:
            self._last_ts[addr] = start - 2
        return len(self.whales)

    def stop(self) -> None:
        self._stop.set()
        self.stream.stop()
        for task in list(self._tasks):
            task.cancel()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def run(self) -> None:
        if not self.whales:
            raise RuntimeError("no active whales; run discovery first")
        log.info("monitor watching %d wallets (stream=%s)", len(self.whales), self.settings.stream_enabled)
        tasks = [asyncio.create_task(self._poll_loop(), name="poll")]
        if self.settings.stream_enabled:
            tasks.append(asyncio.create_task(self._stream_loop(), name="stream"))
        stop_task = asyncio.create_task(self._stop.wait(), name="stop")
        try:
            done, _ = await asyncio.wait(
                [*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is stop_task:
                    continue
                exc = task.exception()
                if exc is not None:
                    raise exc
                raise RuntimeError(f"monitor task {task.get_name()} exited")
        finally:
            stop_task.cancel()
            for task in tasks:
                task.cancel()
            for task in list(self._tasks):
                task.cancel()
            await asyncio.gather(*tasks, stop_task, return_exceptions=True)

    # ── fast path ─────────────────────────────────────────────────────────────

    async def _stream_loop(self) -> None:
        async for block in self.stream.blocks():
            if self._stop.is_set():
                break
            txs = block.get("txs") or []
            hits = BlockStream.touched(txs, self.watch)
            if not hits:
                continue
            block_ts = int((block.get("block") or {}).get("ts") or now_ts())
            block_no = int((block.get("block") or {}).get("number") or 0)
            for addr, wallet_txs in hits.items():
                if addr in self._inflight:
                    continue
                hashes = {str(tx.get("hash") or "").lower().lstrip("0x") for tx in wallet_txs}
                self._inflight.add(addr)
                self.hits += 1
                self._spawn(
                    self._decode(addr, block_ts - 2, source="stream", hashes=hashes, block=block_no)
                )

    # ── polling fallback ──────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        cursor = 0
        while not self._stop.is_set():
            addresses = list(self.whales)
            if not addresses:
                await asyncio.sleep(self.settings.poll_interval_s)
                continue
            batch = max(1, min(len(addresses), 12))
            chunk = [addresses[(cursor + i) % len(addresses)] for i in range(batch)]
            cursor = (cursor + batch) % len(addresses)
            await asyncio.gather(
                *(self._decode(addr, source="poll") for addr in chunk), return_exceptions=True
            )
            # Spread wallets evenly over the interval so the API is not hammered.
            delay = self.settings.poll_interval_s / max(1, len(addresses) / batch)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    # ── decode ────────────────────────────────────────────────────────────────

    async def _decode(
        self,
        address: str,
        min_ts: int | None = None,
        *,
        source: str,
        hashes: set[str] | None = None,
        block: int = 0,
    ) -> None:
        whale = self.whales.get(address)
        if whale is None:
            self._inflight.discard(address)
            return
        now = time.monotonic()
        if now < self._decode_after.get(address, 0.0):
            self._inflight.discard(address)
            return
        self._decode_after[address] = now + 1.2
        floor = min_ts if min_ts is not None else max(self._started - 2, now_ts() - int(self.lookback_s))
        try:
            trades = await self.intel.wallet_trades(address, limit=40)
        except IntelError as exc:
            self.errors += 1
            log.debug("trades %s failed: %s", address, exc)
            self._inflight.discard(address)
            return

        newest = max((int(t.get("ts") or 0) for t in trades), default=0)
        if newest > self._last_ts.get(address, 0):
            self._last_ts[address] = newest

        emitted = 0
        for trade in sorted(
            trades, key=lambda t: (int(t.get("ts") or 0), str(t.get("hash") or ""))
        ):
            ts = int(trade.get("ts") or 0)
            if ts < floor:
                continue
            tx = str(trade.get("hash") or "").lstrip("0x").lower()
            if hashes is not None and tx and tx not in hashes:
                continue
            if self._emit(whale, trade, source):
                emitted += 1

        # The explorer indexes a beat after the block lands: one cheap retry.
        if emitted == 0 and source == "stream" and hashes:
            await asyncio.sleep(1.2)
            try:
                trades = await self.intel.wallet_trades(address, limit=40)
            except IntelError:
                trades = []
            for trade in sorted(
                trades, key=lambda t: (int(t.get("ts") or 0), str(t.get("hash") or ""))
            ):
                if int(trade.get("ts") or 0) < floor:
                    continue
                tx = str(trade.get("hash") or "").lstrip("0x").lower()
                if tx and tx not in hashes:
                    continue
                self._emit(whale, trade, source)

        self._inflight.discard(address)

    def _emit(self, whale: dict, trade: dict, source: str) -> bool:
        token = norm_addr(trade.get("token"))
        symbol = str(trade.get("symbol") or "")
        side = str(trade.get("side") or "").lower()
        px = float(trade.get("px") or 0.0)
        usd = float(trade.get("usd") or 0.0)
        tx = str(trade.get("hash") or "")
        ts = int(trade.get("ts") or 0)

        if px <= 0 or usd <= 0 or side not in ("buy", "sell"):
            return False
        if symbol.upper() in {s.upper() for s in self.settings.ignore_symbols}:
            return False
        if side == "buy" and usd < self.settings.min_whale_usd:
            return False
        if side == "sell" and (
            self.store.position_for_token(token) is None
            and self.store.pending_for_token(token) is None
        ):
            return False

        key = f"{tx}:{token}:{side}"
        if key in self._seen:
            return False

        signal = Signal(
            ts=ts,
            wallet=whale["address"],
            token=token,
            symbol=symbol,
            side=side,
            px=px,
            usd=usd,
            tx=tx,
            block=int(trade.get("block") or 0),
            source=source,
            latency_ms=max(0, (now_ts() - ts) * 1000),
            whale_score=float(whale.get("score") or 0.0),
            whale_tier=str(whale.get("tier") or ""),
            whale_trades=int(whale.get("trades") or 0),
            whale_tokens=int(whale.get("tokens") or 0),
        )
        try:
            self.queue.put_nowait(signal)
        except asyncio.QueueFull:
            # Do not dedupe a trade that was never delivered: the next poll may
            # still have it in the tape, and an exit must not be lost forever.
            log.warning("signal queue full; dropping %s %s", symbol, side)
            return False
        self._seen[key] = now_ts()
        if len(self._seen) > 50_000:
            cutoff = now_ts() - 7200
            self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
        self.signals += 1
        return True

    # ── introspection ─────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "wallets": len(self.whales),
            "signals": self.signals,
            "block_hits": self.hits,
            "errors": self.errors,
            "stream_connected": self.stream.connected,
            "blocks_seen": self.stream.blocks_seen,
            "last_block_ts": self.stream.last_block_ts,
        }
