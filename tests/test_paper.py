from __future__ import annotations

import asyncio

import pytest

from elys.config import Settings
from elys.paper import PaperBroker
from elys.store import Store
from elys.util import now_ts

TOKEN = "0x" + "11" * 20
WALLET = "0x" + "22" * 20


class FakeIntel:
    def __init__(self, price: float = 1.0, meta: dict | None = None) -> None:
        self.price = price
        self.meta = meta or {}

    async def token(self, address: str, ttl: float = 4.0) -> dict:
        return {"px": self.price, **self.meta}


def _settings(tmp_path, **over) -> Settings:
    settings = Settings(db_path=tmp_path / "t.db", log_path=tmp_path / "t.log")
    for key, value in over.items():
        setattr(settings, key, value)
    return settings


def _broker(tmp_path, intel=None, **over) -> tuple[PaperBroker, Store]:
    opts = {"slippage_bps": 100, "fee_bps": 0}
    opts.update(over)
    settings = _settings(tmp_path, **opts)
    store = Store(settings.db_path)
    return PaperBroker(settings, intel, store), store


def test_open_close_math(tmp_path):
    broker, store = _broker(tmp_path)
    result = broker.open(
        token=TOKEN, symbol="T", px=1.0, usd=100.0, source_wallet=WALLET, signal_id=None, whale_score=80
    )
    assert result.ok
    assert result.px == pytest.approx(1.01)  # 100 bps slippage
    assert result.qty == pytest.approx(100 / 1.01)
    assert broker.cash == pytest.approx(900.0)

    position = store.position_for_token(TOKEN)
    assert position is not None
    assert position["entry_px"] == pytest.approx(1.01)
    assert position["peak_px"] == pytest.approx(1.01)

    exit_result = broker.close(position, px=2.0, reason="take_profit")
    proceeds = (100 / 1.01) * (2.0 * 0.99)
    assert exit_result.ok
    assert broker.cash == pytest.approx(900.0 + proceeds)
    closed = store.positions("closed")[0]
    assert closed["pnl_usd"] == pytest.approx(proceeds - 100.0)
    assert closed["pnl_pct"] == pytest.approx((proceeds - 100.0) / 100.0)


def test_add_averages_entry(tmp_path):
    broker, store = _broker(tmp_path)
    broker.open(token=TOKEN, symbol="T", px=1.0, usd=100.0, source_wallet=WALLET, signal_id=None, whale_score=80)
    position = store.position_for_token(TOKEN)
    result = broker.add(position, px=2.0, usd=100.0)
    assert result.ok
    updated = store.position_for_token(TOKEN)
    assert updated["usd"] == pytest.approx(200.0)
    assert updated["qty"] == pytest.approx(100 / 1.01 + 100 / 2.02)
    assert updated["entry_px"] == pytest.approx(200.0 / (100 / 1.01 + 100 / 2.02))
    assert updated["conviction"] == 2


def test_stop_loss_exit(tmp_path):
    intel = FakeIntel(price=1.0)
    broker, store = _broker(tmp_path, intel=intel, slippage_bps=0)
    broker.open(token=TOKEN, symbol="T", px=1.0, usd=100.0, source_wallet=WALLET, signal_id=None, whale_score=80)
    intel.price = 0.5
    asyncio.run(broker.mark())
    closed = store.positions("closed")
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "stop_loss"


def test_take_profit_exit(tmp_path):
    intel = FakeIntel(price=1.0)
    broker, store = _broker(tmp_path, intel=intel, slippage_bps=0)
    broker.open(token=TOKEN, symbol="T", px=1.0, usd=100.0, source_wallet=WALLET, signal_id=None, whale_score=80)
    intel.price = 3.0
    asyncio.run(broker.mark())
    closed = store.positions("closed")
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "take_profit"


def test_trailing_stop(tmp_path):
    intel = FakeIntel(price=1.0)
    broker, store = _broker(tmp_path, intel=intel, slippage_bps=0)
    broker.open(token=TOKEN, symbol="T", px=1.0, usd=100.0, source_wallet=WALLET, signal_id=None, whale_score=80)
    intel.price = 1.7
    asyncio.run(broker.mark())  # arms the trail, no exit yet
    assert store.position_for_token(TOKEN) is not None
    intel.price = 1.05  # giveback (1.7-1.05)/1.7 = 38% > 30%
    asyncio.run(broker.mark())
    closed = store.positions("closed")
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "trail_stop"


def test_rug_meta_forces_exit(tmp_path):
    intel = FakeIntel(price=1.0)
    broker, store = _broker(tmp_path, intel=intel, slippage_bps=0)
    broker.open(token=TOKEN, symbol="T", px=1.0, usd=100.0, source_wallet=WALLET, signal_id=None, whale_score=80)
    intel.meta = {"rug": 1}
    asyncio.run(broker.mark())
    assert store.positions("closed")[0]["exit_reason"] == "rug"


def test_daily_loss_halt(tmp_path):
    broker, store = _broker(tmp_path, max_daily_loss_pct=0.10)
    store.add_equity(now_ts(), 1000.0, 0.0)
    broker.cash = 850.0
    halt = broker.check_halt()
    assert halt is not None
    assert "daily loss" in halt


def test_insufficient_cash_rejected(tmp_path):
    broker, store = _broker(tmp_path, start_equity=10.0)
    result = broker.open(
        token=TOKEN, symbol="T", px=1.0, usd=100.0, source_wallet=WALLET, signal_id=None, whale_score=80
    )
    assert not result.ok
    assert "insufficient" in result.reason
