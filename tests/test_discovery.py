from __future__ import annotations

import asyncio
import json

from elys.config import Settings
from elys.discovery import discover
from elys.util import now_ts

WALLET_A = "0x" + "a1" * 20
WALLET_B = "0x" + "b2" * 20
TOKEN = "0x" + "c3" * 20


class FakeIntel:
    """Deterministic stand-in for the explorer client."""

    rate_limited = 0

    def __init__(self, *, leaderboard, seed_trades, profile):
        self._leaderboard = leaderboard
        self._seed_trades = seed_trades
        self._profile = profile

    async def traders(self, win="1d", sort="realized", limit=300):
        return list(self._leaderboard), {}

    async def hunters(self, limit=100):
        return []

    async def trades_many(self, addresses, limit=100):
        return {addr: self._seed_trades.get(addr, []) for addr in addresses}

    async def wallets(self, addresses, win="7d"):
        return {addr: self._profile for addr in addresses if addr == WALLET_B}


def _row(addr: str) -> dict:
    now = now_ts()
    return {
        "wallet": addr,
        "realized": 12_000.0,
        "volume": 90_000.0,
        "trades": 240,
        "tokens": 12,
        "wins": 70,
        "losses": 30,
        "last_ts": now,
    }


def _tape() -> list[dict]:
    return [{"token": TOKEN, "symbol": "TST", "ts": now_ts(), "side": "buy", "usd": 900.0, "px": 0.01}]


def test_seeded_wallet_outside_leaderboard_is_scored_and_returned(tmp_path):
    seed_file = tmp_path / "seed.json"
    seed_file.write_text(json.dumps([WALLET_B]), encoding="utf-8")
    settings = Settings(
        seed_path=seed_file,
        discover_windows=("7d",),
        discover_limit=10,
        discover_probe_trades=1,  # only WALLET_A is probed from the board
    )
    intel = FakeIntel(
        leaderboard=[_row(WALLET_A)],
        seed_trades={WALLET_A: _tape(), WALLET_B: _tape()},
        profile={
            "record": {"wins": 60, "losses": 20},
            "board": {"realized": 15_000.0, "trades": 180, "tokens": 9},
            "book": {"last_ts": now_ts()},
        },
    )

    passed = asyncio.run(discover(settings, intel, None, persist=False))
    by_addr = {row.address: row for row in passed}

    assert set(by_addr) == {WALLET_A, WALLET_B}
    assert by_addr[WALLET_B].realized == 15_000.0
    assert by_addr[WALLET_B].recent_max_buy_usd == 900.0


def test_seeded_wallet_without_tape_is_rejected(tmp_path):
    seed_file = tmp_path / "seed.json"
    seed_file.write_text(json.dumps([WALLET_B]), encoding="utf-8")
    settings = Settings(
        seed_path=seed_file,
        discover_windows=("7d",),
        discover_limit=10,
        discover_probe_trades=1,
    )
    intel = FakeIntel(leaderboard=[_row(WALLET_A)], seed_trades={WALLET_A: _tape()}, profile={})

    passed = asyncio.run(discover(settings, intel, None, persist=False))
    assert [row.address for row in passed] == [WALLET_A]
