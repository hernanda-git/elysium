from __future__ import annotations

import asyncio

import pytest

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
        "liq_usd": 500_000.0,
        "top10_pct": 20.0,
        "mcap": 1_000_000.0,
        "underwater_pct": 50.0,
        "clusters": 1,
        "insider_pct": 5.0,
        "buys_1h": 60,
        "sells_1h": 30,
        "vol_5m": 1_000.0,
        "vol_1h": 6_000.0,
        "net_1h": 500.0,
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
        whale_trades=500,
        whale_tokens=20,
    )
    base.update(over)
    return Signal(**base)


def _setup(tmp_path, meta=None, *, fail=False, **over):
    settings = Settings(db_path=tmp_path / "t.db", log_path=tmp_path / "t.log", slippage_bps=0, fee_bps=0)
    settings.entry_mode = "market"  # existing tests exercise the market path
    settings.min_conviction_wallets = 1
    settings.copy_enabled = True
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


def test_missing_token_metadata_is_skipped(tmp_path):
    strategy, *_ = _setup(tmp_path, meta={})
    decision = asyncio.run(strategy.on_signal(_signal()))
    assert decision.action == "skip"
    assert "metadata" in decision.reason


def test_market_price_is_used_for_the_fill(tmp_path):
    strategy, broker, store, _ = _setup(tmp_path, meta=_meta(px=0.0102))
    decision = asyncio.run(strategy.on_signal(_signal(px=0.01)))
    assert decision.action == "open", decision.reason
    assert store.position_for_token(TOKEN)["entry_px"] == pytest.approx(0.0102, rel=0.01)


def test_chase_above_whale_price_is_skipped(tmp_path):
    strategy, *_ = _setup(tmp_path, meta=_meta(px=0.012))  # 20 % above the whale
    decision = asyncio.run(strategy.on_signal(_signal(px=0.01)))
    assert decision.action == "skip" and "chase" in decision.reason


def test_small_chase_is_allowed(tmp_path):
    strategy, *_ = _setup(tmp_path, meta=_meta(px=0.0102))  # 2 % above the whale
    decision = asyncio.run(strategy.on_signal(_signal(px=0.01)))
    assert decision.action == "open", decision.reason


def test_stale_signal_is_skipped(tmp_path):
    strategy, *_ = _setup(tmp_path)
    decision = asyncio.run(strategy.on_signal(_signal(ts=now_ts() - 400)))
    assert decision.action == "skip" and "signal age" in decision.reason


def test_detection_latency_alone_does_not_stale_a_fresh_fill(tmp_path):
    strategy, *_ = _setup(tmp_path)
    decision = asyncio.run(strategy.on_signal(_signal(latency_ms=400_000)))
    assert decision.action == "open", decision.reason


def test_whale_trade_too_large_for_the_pool_is_skipped(tmp_path):
    strategy, *_ = _setup(tmp_path, meta=_meta(liq_usd=20_000.0))
    decision = asyncio.run(strategy.on_signal(_signal(usd=1_000.0)))  # 5 % of the pool
    assert decision.action == "skip" and "liquidity" in decision.reason


def test_reentry_guard_blocks_immediate_reopen(tmp_path):
    strategy, broker, store, _ = _setup(tmp_path)
    assert asyncio.run(strategy.on_signal(_signal())).action == "open"
    position = store.position_for_token(TOKEN)
    broker.close(position, px=0.01, reason="whale_exit:TST")
    decision = asyncio.run(strategy.on_signal(_signal()))
    assert decision.action == "skip" and "re-entry guard" in decision.reason


def test_stop_cooldown_blocks_reopen_after_stop_loss(tmp_path):
    strategy, broker, store, _ = _setup(tmp_path)
    assert asyncio.run(strategy.on_signal(_signal())).action == "open"
    position = store.position_for_token(TOKEN)
    broker.close(position, px=0.01, reason="stop_loss")
    decision = asyncio.run(strategy.on_signal(_signal()))
    assert decision.action == "skip" and "stop cooldown" in decision.reason


def test_sell_uses_market_price_not_the_whale_price(tmp_path):
    strategy, broker, store, intel = _setup(tmp_path)
    assert asyncio.run(strategy.on_signal(_signal())).action == "open"
    intel.meta = _meta(px=0.015)
    decision = asyncio.run(strategy.on_signal(_signal(side="sell", px=0.02)))
    assert decision.action == "close"
    assert store.positions("closed")[0]["exit_px"] == pytest.approx(0.015, rel=0.01)


# ── retrace mode ─────────────────────────────────────────────────────────────


def test_retrace_mode_arms_a_limit_instead_of_opening(tmp_path):
    strategy, broker, store, _ = _setup(tmp_path, entry_mode="retrace", meta=_meta(px=0.0105))
    decision = asyncio.run(strategy.on_signal(_signal(px=0.01)))
    assert decision.action == "arm"
    assert store.load_open_positions() == []
    pending = store.pending_entries()
    assert len(pending) == 1
    assert pending[0]["limit_px"] == pytest.approx(0.0102)


def test_retrace_fills_when_the_market_returns_to_the_limit(tmp_path):
    strategy, broker, store, intel = _setup(tmp_path, entry_mode="retrace", meta=_meta(px=0.0105))
    asyncio.run(strategy.on_signal(_signal(px=0.01)))
    intel.meta = _meta(px=0.0100)
    decisions = asyncio.run(strategy.check_pending())
    assert decisions and decisions[0].action == "open"
    position = store.position_for_token(TOKEN)
    assert position is not None
    assert position["entry_px"] == pytest.approx(0.0100, rel=0.01)
    assert store.pending_entries() == []


def test_retrace_expires_without_a_fill(tmp_path):
    strategy, broker, store, intel = _setup(
        tmp_path, entry_mode="retrace", retrace_ttl_s=0, meta=_meta(px=0.0105)
    )
    asyncio.run(strategy.on_signal(_signal(px=0.01)))
    intel.meta = _meta(px=0.0100)
    decisions = asyncio.run(strategy.check_pending())
    assert decisions[0].action == "skip" and "expired" in decisions[0].reason
    assert store.pending_entries() == []
    assert store.load_open_positions() == []


def test_next_buy_exits_a_retrace_position(tmp_path):
    strategy, broker, store, intel = _setup(tmp_path, entry_mode="retrace", meta=_meta(px=0.0105))
    asyncio.run(strategy.on_signal(_signal(px=0.01)))
    intel.meta = _meta(px=0.0100)
    asyncio.run(strategy.check_pending())
    assert store.position_for_token(TOKEN) is not None

    intel.meta = _meta(px=0.0120)  # the next buy's pop
    decision = asyncio.run(strategy.on_signal(_signal(px=0.0115)))
    assert decision.action == "close"
    assert store.positions("closed")[0]["exit_reason"].startswith("pop_exit")


def test_whale_sell_cancels_a_resting_entry(tmp_path):
    strategy, broker, store, intel = _setup(tmp_path, entry_mode="retrace", meta=_meta(px=0.0105))
    asyncio.run(strategy.on_signal(_signal(px=0.01)))
    assert store.pending_for_token(TOKEN) is not None
    decision = asyncio.run(strategy.on_signal(_signal(side="sell")))
    assert decision.action == "skip" and "pending cancelled" in decision.reason
    assert store.pending_for_token(TOKEN) is None


def test_missing_safety_field_is_vetoed(tmp_path):
    strategy, _, store, _ = _setup(tmp_path, meta=_meta(mcap=None))
    decision = asyncio.run(strategy.on_signal(_signal()))
    assert decision.action == "skip" and "unknown mcap" in decision.reason
    assert store.load_open_positions() == []


def test_untested_honeypot_verdict_is_vetoed(tmp_path):
    strategy, *_ = _setup(tmp_path, meta=_meta(hp_verdict="untested"))
    decision = asyncio.run(strategy.on_signal(_signal()))
    assert decision.action == "skip" and "honeypot verdict" in decision.reason


def test_two_distinct_wallets_are_required_for_conviction(tmp_path):
    strategy, store, *_ = _setup(
        tmp_path, min_conviction_wallets=2, meta=_meta()
    )
    first = asyncio.run(strategy.on_signal(_signal(wallet="0x" + "aa" * 20, tx="0x" + "01" * 32)))
    assert first.action == "skip" and "conviction" in first.reason
    second = asyncio.run(strategy.on_signal(_signal(wallet="0x" + "bb" * 20, tx="0x" + "02" * 32)))
    assert second.action == "open", second.reason


def test_high_frequency_wallet_is_rejected(tmp_path):
    strategy, *_ = _setup(tmp_path)
    decision = asyncio.run(strategy.on_signal(_signal(whale_trades=1_000, whale_tokens=2)))
    assert decision.action == "skip" and "trades/token" in decision.reason


def test_copy_disabled_logs_flow_without_opening(tmp_path):
    strategy, _, store, _ = _setup(tmp_path, copy_enabled=False)
    decision = asyncio.run(strategy.on_signal(_signal()))
    assert decision.action == "skip" and "copy disabled" in decision.reason
    assert store.load_open_positions() == []
