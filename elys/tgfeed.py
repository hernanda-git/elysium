"""Telegram feeds for the public ``elysiumecoalerts`` channel.

Two transports, same parser:

* web  — ``https://t.me/s/<channel>``, no credentials, ~1s latency, default;
* user — Telethon with the existing TG_API_ID/TG_API_HASH, real push, optional.

Posts are structured by the channel: Launch, Smart money, Whale, Bridge, Rug
warning. We parse the token, wallet set and tx hash out of each post so the
engine can use rug warnings as an emergency exit and the wallet alerts as
confirmation for our own watchlist.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx

from .config import Settings
from .store import Store
from .util import norm_addr, now_ts

log = logging.getLogger("elys.tg")

EmitFn = Callable[[dict], Awaitable[None]]

_MSG_SPLIT = re.compile(r'<div class="tgme_widget_message_wrap js-widget_message_wrap">')
_DATA_POST = re.compile(r'data-post="([^/"]+)/(\d+)"')
_TEXT_DIV = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TIME = re.compile(r'<time[^>]*datetime="([^"]+)"')
_HREF = re.compile(r'href="([^"]+)"')
_TOKEN_URL = re.compile(r"elysium\.eco/screener/(0x[0-9a-fA-F]{40})")
_WALLET_URL = re.compile(r"elysium\.eco/screener/w/(0x[0-9a-fA-F]{40})")
_TX_URL = re.compile(r"elysium\.eco/tx/(0x[0-9a-fA-F]{64})")

INTERESTING = {"whale", "smart_money", "rug_warning"}


def strip_html(fragment: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", fragment)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def classify(text: str) -> str:
    lower = text.lower()
    if lower.startswith("whale"):
        return "whale"
    if lower.startswith("smart money"):
        return "smart_money"
    if lower.startswith("rug warning") or lower.startswith("honeypot"):
        return "rug_warning"
    if lower.startswith("launch"):
        return "launch"
    if lower.startswith("bridge"):
        return "bridge"
    return "other"


def parse_symbol(text: str, kind: str) -> str:
    patterns = {
        "whale": r"\b(?:buy|sell) of ([^\s]+)",
        "smart_money": r"bought ([^\s]+)",
        "launch": r"Launch · ([^\s]+) drew",
        "rug_warning": r"Rug warning · ([^:]+):",
    }
    pattern = patterns.get(kind)
    if not pattern:
        return ""
    match = re.search(pattern, text, re.I)
    return match.group(1).strip() if match else ""


def parse_post(channel: str, post_id: int, when: datetime, text: str, links: list[str]) -> dict:
    kind = classify(text)
    token = ""
    wallets: list[str] = []
    tx = ""
    for link in links:
        if (m := _TOKEN_URL.search(link)) and not token:
            token = norm_addr(m.group(1))
        if (m := _WALLET_URL.search(link)):
            addr = norm_addr(m.group(1))
            if addr not in wallets:
                wallets.append(addr)
        if (m := _TX_URL.search(link)) and not tx:
            tx = m.group(1)
    return {
        "id": post_id,
        "channel": channel,
        "ts": int(when.timestamp()) if when else now_ts(),
        "kind": kind,
        "text": text,
        "token": token,
        "symbol": parse_symbol(text, kind),
        "wallets": wallets,
        "tx": tx,
    }


def parse_page(channel: str, page: str) -> list[dict]:
    chunks = _MSG_SPLIT.split(page)
    posts: list[dict] = []
    for chunk in chunks[1:]:
        # keep the block small: stop before the next message's buttons wrapper
        post = _DATA_POST.search(chunk)
        if not post or post.group(1) != channel:
            continue
        text_block = _TEXT_DIV.search(chunk)
        if not text_block:
            continue
        text = strip_html(text_block.group(1))
        if not text:
            continue
        time_match = _TIME.search(chunk)
        when = None
        if time_match:
            try:
                when = datetime.fromisoformat(time_match.group(1))
            except ValueError:
                when = None
        posts.append(parse_post(channel, int(post.group(2)), when, text, _HREF.findall(chunk)))
    posts.sort(key=lambda p: p["id"])
    return posts


class TelegramWebFeed:
    """Polls the public preview page; no login, no session file."""

    def __init__(self, settings: Settings, store: Store, on_event: EmitFn | None = None) -> None:
        self.settings = settings
        self.store = store
        self.on_event = on_event
        self._last_id: int | None = None
        self._stop = asyncio.Event()
        self.fetched = 0

    def stop(self) -> None:
        self._stop.set()

    async def sync_once(self) -> list[dict]:
        url = f"https://t.me/s/{self.settings.tg_channel}"
        headers = {"user-agent": "Mozilla/5.0 (compatible; elysium-copytrader/0.1)"}
        async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            posts = parse_page(self.settings.tg_channel, response.text)
        self.fetched += 1
        new_posts: list[dict] = []
        for post in posts:
            fresh = self.store.add_tg_post(
                {
                    "id": post["id"],
                    "channel": post["channel"],
                    "ts": post["ts"],
                    "kind": post["kind"],
                    "text": post["text"],
                    "token": post["token"] or None,
                    "wallets": ",".join(post["wallets"]),
                    "tx": post["tx"],
                    "raw": None,
                }
            )
            if fresh:
                new_posts.append(post)
        if self._last_id is None:
            self._last_id = max((p["id"] for p in posts), default=0)
            return []
        events = [p for p in new_posts if p["id"] > self._last_id]
        if posts:
            self._last_id = max(self._last_id, max(p["id"] for p in posts))
        return events

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                events = await self.sync_once()
                for event in events:
                    if event["kind"] in INTERESTING and self.on_event:
                        try:
                            await self.on_event(event)
                        except Exception:
                            log.exception("telegram handler failed for post %s", event.get("id"))
            except (httpx.HTTPError, ValueError) as exc:
                log.debug("tg web fetch failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.tg_interval_s)
            except asyncio.TimeoutError:
                pass

    async def backfill(self, pages: int = 3) -> int:
        """Walk ``?before=`` pages backwards to seed history."""
        total = 0
        before: int | None = None
        headers = {"user-agent": "Mozilla/5.0 (compatible; elysium-copytrader/0.1)"}
        async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
            for _ in range(max(1, pages)):
                url = f"https://t.me/s/{self.settings.tg_channel}"
                if before:
                    url += f"?before={before}"
                response = await client.get(url)
                response.raise_for_status()
                posts = parse_page(self.settings.tg_channel, response.text)
                if not posts:
                    break
                for post in posts:
                    if self.store.add_tg_post(
                        {
                            "id": post["id"],
                            "channel": post["channel"],
                            "ts": post["ts"],
                            "kind": post["kind"],
                            "text": post["text"],
                            "token": post["token"] or None,
                            "wallets": ",".join(post["wallets"]),
                            "tx": post["tx"],
                            "raw": None,
                        }
                    ):
                        total += 1
                before = min(p["id"] for p in posts)
        return total


class TelegramUserFeed:
    """Optional Telethon push transport; reuses the machine's TG_API_ID/HASH."""

    def __init__(self, settings: Settings, store: Store, on_event: EmitFn | None = None) -> None:
        self.settings = settings
        self.store = store
        self.on_event = on_event
        self._stop = asyncio.Event()
        self.client = None

    def stop(self) -> None:
        self._stop.set()

    async def _client(self):
        if self.client is not None:
            return self.client
        try:
            from telethon import TelegramClient
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError("telethon not installed: pip install telethon") from exc
        if not self.settings.tg_api_id or not self.settings.tg_api_hash:
            raise RuntimeError("TG_API_ID / TG_API_HASH not set")
        self.settings.telegram_session.parent.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(
            str(self.settings.telegram_session),
            self.settings.tg_api_id,
            self.settings.tg_api_hash,
        )
        await self.client.start(phone=self.settings.tg_phone or None)
        return self.client

    async def run(self) -> None:
        from telethon import events
        from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

        client = await self._client()
        channel = self.settings.tg_channel

        @client.on(events.NewMessage(chats=channel))
        async def handler(message):  # pragma: no cover - live push
            text = message.message or ""
            links: list[str] = []
            for entity, _fragment in message.get_entities_text():
                if isinstance(entity, MessageEntityTextUrl):
                    links.append(entity.url)
                elif isinstance(entity, MessageEntityUrl):
                    links.append(_fragment)
            links.extend(re.findall(r"https?://\S+", text))
            post_id = int(getattr(message, "id", 0) or 0)
            when = getattr(message, "date", None)
            post = parse_post(channel, post_id, when, text, links)
            if self.store.add_tg_post(
                {
                    "id": post["id"],
                    "channel": channel,
                    "ts": post["ts"],
                    "kind": post["kind"],
                    "text": post["text"],
                    "token": post["token"] or None,
                    "wallets": ",".join(post["wallets"]),
                    "tx": post["tx"],
                    "raw": None,
                }
            ):
                if post["kind"] in INTERESTING and self.on_event:
                    await self.on_event(post)

        log.info("telegram user feed listening on %s", channel)
        me = await client.get_me()
        log.info("telegram authenticated as %s", getattr(me, "username", None) or getattr(me, "id", None))
        try:
            await self._stop.wait()
        finally:
            await client.disconnect()

    async def backfill(self, limit: int = 200) -> int:
        from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

        client = await self._client()
        count = 0
        async for message in client.iter_messages(self.settings.tg_channel, limit=limit):
            text = message.message or ""
            links: list[str] = []
            for entity, fragment in message.get_entities_text():
                if isinstance(entity, MessageEntityTextUrl):
                    links.append(entity.url)
                elif isinstance(entity, MessageEntityUrl):
                    links.append(fragment)
            post = parse_post(self.settings.tg_channel, int(message.id), message.date, text, links)
            if self.store.add_tg_post(
                {
                    "id": post["id"],
                    "channel": self.settings.tg_channel,
                    "ts": post["ts"],
                    "kind": post["kind"],
                    "text": post["text"],
                    "token": post["token"] or None,
                    "wallets": ",".join(post["wallets"]),
                    "tx": post["tx"],
                    "raw": None,
                }
            ):
                count += 1
        return count
