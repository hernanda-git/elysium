from __future__ import annotations

import asyncio

from elys.config import Settings
from elys.intel import IntelError
from elys.monitor import Signal
from elys.paper import PaperBroker
from elys.store import Store
from elys.strategy import CopyStrategy
from elys.util import now_ts

TOKEN = "0x" + "33" * 20
WALLET = "0x" + "44" * 20


class FakeIntel:
    def __init__(self, meta: dict | None = None, *, fail: bool = False) -> None:
        self.meta = meta or {}
        self.fail = fail
        self.calls = 0

    async def token(self, address: str, ttl: float = 4.0) -> dict:
        self.calls += 1
        if self.fail:
            raise IntelError("boom")
        return self.meta


def _meta(**over) -> dict:
    base = {
        "px": 0.01,
        "liq_usd": 50_000.0,
        "top10_pct": 20.0,
        "hp": 0,
        "hp_verdict": "clean",
        "rug": 0,
        "rug_by_creator": 0,
        "hp_wiped": 0,
        "launch_ts": now_ts() - 3600,
    }
    base.update(over)
    return base


def _signal(**over) -> Signal:
    base = dict(
        ts=now_ts(),
        wallet=WALLET,
        token=TOKEN,
        symbol="TST",
        side="buy",
        px=0.01,
        usd=1000.0,
        tx="0x" + "ab" * 32,
        block=1,
        source="test",
        latency_ms=120,
        whale_score=80.0,
        whale_tier="smart",
    )
    base.update(over)
    return Signal(**base)


def _setup(tmp_path, meta=None, *, fail=False, **over):
    settings = Settings(db_path=tmp_path / "t.db", log_path=tmp_path / "t.log", slippage_bps=0, fee_bps=0)
    for key, value in over.items():
        setattr(settings, key, value)
    store = Store(settings.db_path)
    intel = FakeIntel(meta if meta is not None else _meta(), fail=fail)
    broker = PaperBroker(settings, intel, store)
    return CopyStrategy(settings, intel, store, broker), broker, store, intel


def test_buy_opens_position(tmp_path):
    strategy, broker, store, _ = _setup(tmp_path)
    decision = asyncio.run(strategy.on_signal(_signal()))
    assert decision.action == "open", decision.reason
    position = store.position_for_token(TOKEN)
    assert position is not None
    assert 0 < position["usd"] <= 50.0


def test_honeypot_is_vetoed(tmp_path):
    strategy, broker, store, _ = _setup(tmp_path, meta=_meta(hp_verdict="honeypot"))
    decision = asyncio.run(strategy.on_signal(_signal()))
    assert decision.action == "skip"
    assert "honeypot" in decision.reason
    assert store.load_open_positions() == []


def test_thin_liquidity_is_vetoed(tmp_path):
    strategy, *_ = _setup(tmp_path, meta=_meta(liq_usd=100.0))
    decision = asyncio.run(strategy.on_signal(_signal()))
    assert decision.action == "skip" and "liquidity" in decision.reason


def test_young_token_is_vetoed(tmp_path):
    strategy, *_ = _setup(tmp_path, meta=_meta(launch_ts=now_ts() - 5))
    decision = asyncio.run(strategy.on_signal(_signal()))
    assert decision.action == "skip" and "young" in decision.reason


def test_low_score_whale_ignored(tmp_path):
    strategy, _, _, intel = _setup(tmp_path)
    decision = asyncio.run(strategy.on_signal(_signal(whale_score=10.0)))
    assert decision.action == "skip" and "score" in decision.reason
    assert intel.calls == 0  # never even prices the token


def test_intel_failure_skips_cleanly(tmp_path):
    strategy, *_ = _setup(tmp_path, fail=True)
    decision = asyncio.run(strategy.on_signal(_signal()))
    assert decision.action == "skip" and "lookup failed" in decision.reason


def test_second_buy_adds_within_token_cap(tmp_path):
    strategy, broker, store, _ = _setup(tmp_path, per_trade_usd=15.0, max_token_usd=50.0)
    first = asyncio.run(strategy.on_signal(_signal(tx="0x" + "01" * 32)))
    second = asyncio.run(strategy.on_signal(_signal(tx="0x" + "02" * 32)))
    assert first.action == "open"
    assert second.action == "add", second.reason
    position = store.position_for_token(TOKEN)
    assert position["conviction"] == 2
    assert position["usd"] <= 50.0


def test_third_buy_hits_max_adds(tmp_path):
    strategy, _, store, _ = _setup(tmp_path, per_trade_usd=10.0, max_token_usd=100.0, max_adds_per_token=2)
    for i in range(4):
        decision = asyncio.run(strategy.on_signal(_signal(tx=f"0x0{i}" + "ab" * 31)))
    assert decision.action == "skip"
    assert decision.reason == "max_adds"


def test_max_positions_blocks_new_token(tmp_path):
    strategy, *_ = _setup(tmp_path, max_positions=0)
    decision = asyncio.run(strategy.on_signal(_signal()))
    assert decision.action == "skip" and decision.reason == "max_positions"


def test_sell_without_position_is_skipped(tmp_path):
    strategy, *_ = _setup(tmp_path)
    decision = asyncio.run(strategy.on_signal(_signal(side="sell")))
    assert decision.action == "skip" and decision.reason == "no_position"


def test_sell_closes_copied_position(tmp_path):
    strategy, broker, store, _ = _setup(tmp_path)
    asyncio.run(strategy.on_signal(_signal()))
    assert store.position_for_token(TOKEN) is not None
    decision = asyncio.run(strategy.on_signal(_signal(side="sell", px=0.02)))
    assert decision.action == "close", decision.reason
    assert store.position_for_token(TOKEN) is None
    assert store.positions("closed")[0]["exit_reason"].startswith("whale_exit")


def test_rug_alert_closes_position(tmp_path):
    strategy, broker, store, _ = _setup(tmp_path)
    asyncio.run(strategy.on_signal(_signal()))
    decision = asyncio.run(strategy.on_tg_event({"kind": "rug_warning", "token": TOKEN}))
    assert decision is not None and decision.action == "close"
    assert store.positions("closed")[0]["exit_reason"] == "rug_alert"
