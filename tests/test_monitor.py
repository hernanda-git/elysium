from __future__ import annotations

import asyncio

from elys.config import Settings
from elys.monitor import Monitor
from elys.scoring import WalletScore
from elys.store import Store
from elys.stream import BlockStream
from elys.util import now_ts

WALLET = "0x" + "aa" * 20
TOKEN = "0x" + "11" * 20


class FakeIntel:
    def __init__(self, trades: list[dict]) -> None:
        self.trades = trades
        self.calls = 0

    async def wallet_trades(self, address: str, limit: int = 50) -> list[dict]:
        self.calls += 1
        return list(self.trades)


def _setting(tmp_path, **over) -> Settings:
    settings = Settings(db_path=tmp_path / "t.db", log_path=tmp_path / "t.log")
    for key, value in over.items():
        setattr(settings, key, value)
    return settings


def _store_with_whale(settings) -> Store:
    store = Store(settings.db_path)
    store.upsert_whale(WalletScore(address=WALLET, score=80.0, tier="smart", passed=True))
    return store


def _trade(**over) -> dict:
    base = {
        "ts": now_ts() - 1,
        "token": TOKEN,
        "symbol": "ABC",
        "side": "buy",
        "usd": 500.0,
        "px": 0.01,
        "hash": "0x" + "bb" * 32,
        "block": 7,
    }
    base.update(over)
    return base


def test_decode_emits_buy_and_dedupes(tmp_path):
    settings = _setting(tmp_path, min_whale_usd=100.0)
    store = _store_with_whale(settings)
    queue: asyncio.Queue = asyncio.Queue()
    monitor = Monitor(settings, FakeIntel([_trade()]), store, queue)
    monitor.load_whales()

    asyncio.run(monitor._decode(WALLET, source="poll"))
    signal = queue.get_nowait()
    assert signal.token == TOKEN
    assert signal.side == "buy"
    assert signal.wallet == WALLET
    assert signal.whale_tier == "smart"
    assert signal.whale_score == 80.0
    assert signal.latency_ms >= 0

    monitor._decode_after.clear()
    asyncio.run(monitor._decode(WALLET, source="poll"))
    assert queue.empty()


def test_small_buys_are_ignored(tmp_path):
    settings = _setting(tmp_path, min_whale_usd=100.0)
    store = _store_with_whale(settings)
    queue: asyncio.Queue = asyncio.Queue()
    monitor = Monitor(settings, FakeIntel([_trade(usd=99.0)]), store, queue)
    monitor.load_whales()
    asyncio.run(monitor._decode(WALLET, source="poll"))
    assert queue.empty()


def test_sells_need_an_open_position(tmp_path):
    settings = _setting(tmp_path)
    store = _store_with_whale(settings)
    queue: asyncio.Queue = asyncio.Queue()
    monitor = Monitor(settings, FakeIntel([_trade(side="sell")]), store, queue)
    monitor.load_whales()
    asyncio.run(monitor._decode(WALLET, source="poll"))
    assert queue.empty()

    store.open_position(
        {
            "token": TOKEN,
            "symbol": "ABC",
            "entry_px": 0.01,
            "qty": 100.0,
            "usd": 1.0,
            "entry_ts": now_ts(),
            "last_ts": now_ts(),
            "source_wallet": WALLET,
            "source_signal": None,
            "conviction": 1,
            "peak_px": 0.01,
            "fee_usd": 0.0,
        }
    )
    monitor._decode_after.clear()
    asyncio.run(monitor._decode(WALLET, source="poll"))
    assert queue.get_nowait().side == "sell"


def test_stream_only_uses_matching_hashes(tmp_path):
    settings = _setting(tmp_path, min_whale_usd=100.0)
    store = _store_with_whale(settings)
    queue: asyncio.Queue = asyncio.Queue()
    trade = _trade()
    monitor = Monitor(settings, FakeIntel([trade]), store, queue)
    monitor.load_whales()

    other = "cc" * 32
    asyncio.run(monitor._decode(WALLET, trade["ts"] - 2, source="stream", hashes={other}))
    assert queue.empty()

    monitor._decode_after.clear()
    asyncio.run(
        monitor._decode(
            WALLET, trade["ts"] - 2, source="stream", hashes={"bb" * 32}
        )
    )
    assert queue.get_nowait().token == TOKEN


def test_block_stream_touched_normalises():
    txs = [
        {"from_a": "0" * 24 + "aa" * 20, "hash": "x"},
        {"from_a": "0" * 24 + "bb" * 20, "hash": "y"},
    ]
    hits = BlockStream.touched(txs, {WALLET})
    assert list(hits) == [WALLET]
    assert hits[WALLET][0]["hash"] == "x"
