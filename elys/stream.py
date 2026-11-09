"""Real-time Elysium block stream (server-sent events).

``GET /api/stream`` emits one ``head`` event per block with the full tx list.
Block timestamps track wall clock to the second, which is what makes sub-second
whale detection possible without a paid RPC websocket.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Callable

import httpx

from .config import Settings
from .util import norm_addr

log = logging.getLogger("elys.stream")


class BlockStream:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop = asyncio.Event()
        self.connected = False
        self.blocks_seen = 0
        self.txs_seen = 0
        self.last_block_ts = 0
        self.last_error: str | None = None

    def stop(self) -> None:
        self._stop.set()

    async def blocks(self) -> AsyncIterator[dict]:
        """Yield blocks forever, reconnecting with capped backoff."""
        backoff = 1.0
        headers = {"accept": "text/event-stream", "user-agent": self.settings.user_agent}
        timeout = httpx.Timeout(None, connect=10.0, read=30.0)
        async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
            while not self._stop.is_set():
                try:
                    async with client.stream("GET", self.settings.stream_url) as response:
                        response.raise_for_status()
                        self.connected = True
                        self.last_error = None
                        backoff = 1.0
                        log.info("stream connected")
                        event = "message"
                        data: list[str] = []
                        async for line in response.aiter_lines():
                            if self._stop.is_set():
                                break
                            if line.startswith("event:"):
                                event = line[6:].strip()
                            elif line.startswith("data:"):
                                data.append(line[5:].strip())
                            elif not line:
                                if data and event == "head":
                                    try:
                                        block = json.loads("\n".join(data))
                                    except ValueError:
                                        block = None
                                    if block:
                                        self.blocks_seen += 1
                                        self.txs_seen += len(block.get("txs") or [])
                                        self.last_block_ts = int((block.get("block") or {}).get("ts") or 0)
                                        yield block
                                event, data = "message", []
                except (httpx.HTTPError, OSError) as exc:
                    self.connected = False
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log.warning("stream error (%s); reconnecting in %.1fs", self.last_error, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 15.0)
                except asyncio.CancelledError:
                    raise
        self.connected = False

    @staticmethod
    def touched(txs: list[dict], watch: set[str]) -> dict[str, list[dict]]:
        """Map watched address -> txs that it initiated in this block."""
        hits: dict[str, list[dict]] = {}
        if not watch:
            return hits
        for tx in txs:
            sender = norm_addr(tx.get("from_a"))
            if sender in watch:
                hits.setdefault(sender, []).append(tx)
        return hits
