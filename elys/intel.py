"""HTTP client for the public Elysium intel API (api.elysium.eco)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from .config import Settings
from .util import norm_addr

log = logging.getLogger("elys.intel")


class IntelError(RuntimeError):
    """Upstream returned an error or stayed unreachable."""


class _TokenBucket:
    """Adaptive global request budget.

    The public API 429s under sustained bursts; crossing that line is expensive
    because every in-flight retry inherits the cooldown. The bucket starts
    conservative, halves its rate on a throttle, and slowly recovers.
    """

    def __init__(self, rate: float = 4.0, burst: float = 6.0, max_rate: float = 6.0) -> None:
        self.rate = rate
        self.max_rate = max_rate
        self.capacity = burst
        self.tokens = burst
        self.updated = time.monotonic()
        self.successes = 0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.rate
            await asyncio.sleep(wait)

    def penalize(self) -> None:
        self.rate = max(1.5, self.rate * 0.5)
        self.tokens = 0.0
        self.updated = time.monotonic()

    def reward(self) -> None:
        self.successes += 1
        if self.successes % 50 == 0:
            self.rate = min(self.max_rate, self.rate + 0.25)


class Intel:
    """Thin, cached, rate-limited wrapper over the explorer API.

    Endpoints used (all public, no key):

    * ``GET /api/tokens/traders?win=1d|7d|30d|all&sort=&limit=`` leaderboard rows
    * ``GET /api/tokens/hunters`` early-launch hunters with hit rates
    * ``GET /api/tokens/w/<addr>?win=`` wallet profile, PnL record and positions
    * ``GET /api/tokens/w/<addr>/trades`` decoded per-wallet swaps, newest first
    * ``GET /api/tokens/<token>`` token snapshot (price, liquidity, honeypot)
    * ``GET /api/tokens/<token>/trades`` decoded token trade tape
    * ``GET /api/stats`` chain head, block time, HYPE price
    """

    def __init__(self, settings: Settings, *, concurrency: int = 3, rate: float = 4.0) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.intel_base.rstrip("/"),
            headers={"user-agent": settings.user_agent, "accept": "application/json"},
            timeout=httpx.Timeout(20.0, connect=10.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )
        self._sem = asyncio.Semaphore(concurrency)
        self._bucket = _TokenBucket(rate=rate)
        self._cooldown_until = 0.0
        self._cache: dict[str, tuple[float, Any]] = {}
        self.rate_limited = 0

    async def close(self) -> None:
        await self._client.aclose()

    # ── plumbing ──────────────────────────────────────────────────────────────

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        ttl: float = 0.0,
        retries: int = 5,
    ) -> Any:
        key = path
        if params:
            key += "?" + json.dumps(params, sort_keys=True)
        if ttl:
            hit = self._cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
        last: Exception | None = None
        for attempt in range(retries):
            try:
                await self._bucket.acquire()
                cooldown = self._cooldown_until - time.monotonic()
                if cooldown > 0:
                    await asyncio.sleep(cooldown)
                async with self._sem:
                    response = await self._client.get(path, params=params)
                if response.status_code == 429:
                    self.rate_limited += 1
                    self._bucket.penalize()
                    retry_after = float(response.headers.get("retry-after") or 0) or (0.75 * (2**attempt))
                    self._cooldown_until = max(self._cooldown_until, time.monotonic() + retry_after)
                    raise IntelError(f"HTTP 429 for {path}")
                if response.status_code in (500, 502, 503, 504):
                    raise IntelError(f"HTTP {response.status_code} for {path}")
                if response.status_code >= 400:
                    raise IntelError(f"HTTP {response.status_code} for {path}")
                self._bucket.reward()
                data = response.json()
                if data is None:
                    raise IntelError(f"empty JSON body for {path}")
                if ttl:
                    self._cache[key] = (time.monotonic() + ttl, data)
                    if len(self._cache) > 4096:
                        cutoff = time.monotonic()
                        self._cache = {k: v for k, v in self._cache.items() if v[0] > cutoff}
                return data
            except (httpx.RequestError, ValueError, IntelError) as exc:
                last = exc
                if attempt < retries - 1:
                    await asyncio.sleep(0.4 * (2**attempt))
        raise IntelError(f"{path}: {last}")

    # ── endpoints ─────────────────────────────────────────────────────────────

    async def traders(self, win: str = "1d", sort: str = "realized", limit: int = 300) -> tuple[list[dict], dict]:
        data = await self._get(
            "/api/tokens/traders", {"win": win, "sort": sort, "limit": limit}, ttl=30
        )
        return list(data.get("rows") or []), dict(data.get("meta") or {})

    async def hunters(self, limit: int = 100) -> list[dict]:
        data = await self._get("/api/tokens/hunters", {"limit": limit}, ttl=60)
        return list(data or [])

    async def wallet(self, address: str, win: str = "7d") -> dict:
        return await self._get(f"/api/tokens/w/{norm_addr(address)}", {"win": win}, ttl=5)

    async def wallet_trades(self, address: str, limit: int = 50) -> list[dict]:
        data = await self._get(
            f"/api/tokens/w/{norm_addr(address)}/trades", {"limit": max(1, min(limit, 200))}, ttl=0.8
        )
        return list(data or [])

    async def token(self, address: str, *, ttl: float = 4.0) -> dict:
        return await self._get(f"/api/tokens/{norm_addr(address)}", ttl=ttl)

    async def token_trades(self, address: str, limit: int = 100) -> list[dict]:
        data = await self._get(
            f"/api/tokens/{norm_addr(address)}/trades", {"limit": max(1, min(limit, 200))}, ttl=1.0
        )
        return list(data or [])

    async def stats(self) -> dict:
        return await self._get("/api/stats", ttl=10)

    async def universe(self, limit: int = 200) -> list[dict]:
        return list(await self._get("/api/tokens/universe", {"sort": "vol", "limit": limit}, ttl=30) or [])

    async def launches(self, limit: int = 200) -> list[dict]:
        return list(await self._get("/api/tokens/launches", {"limit": limit}, ttl=30) or [])

    async def rpc(self, method: str, params: list[Any] | None = None) -> Any:
        async with self._sem:
            response = await self._client.post(
                "/api/rpc", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
            )
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise IntelError(f"rpc {method}: {body['error']}")
        return body.get("result")

    # ── helpers ───────────────────────────────────────────────────────────────

    async def trades_many(self, addresses: list[str], limit: int = 100) -> dict[str, list[dict]]:
        """Fetch recent trades for many wallets concurrently; failures are skipped."""
        out: dict[str, list[dict]] = {}
        pending = list(dict.fromkeys(norm_addr(a) for a in addresses))

        async def one(addr: str) -> None:
            try:
                out[addr] = await self.wallet_trades(addr, limit=limit)
            except IntelError as exc:
                log.debug("trades %s failed: %s", addr, exc)

        await asyncio.gather(*(one(a) for a in pending))
        return out

    async def wallets(self, addresses: list[str], win: str = "7d") -> dict[str, dict]:
        """Fetch many wallet profiles concurrently; failures are skipped."""
        out: dict[str, dict] = {}
        pending = list(dict.fromkeys(norm_addr(a) for a in addresses))

        async def one(addr: str) -> None:
            try:
                out[addr] = await self.wallet(addr, win)
            except IntelError as exc:
                log.debug("wallet %s failed: %s", addr, exc)

        await asyncio.gather(*(one(a) for a in pending))
        return out
