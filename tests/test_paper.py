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


def test_impact_scales_with_size_and_liquidity(tmp_path):
    broker, store = _broker(tmp_path, slippage_bps=0, fee_bps=0)
    result = broker.open(
        token=TOKEN, symbol="T", px=1.0, usd=100.0, source_wallet=WALLET,
        signal_id=None, whale_score=80, liq_usd=10_000.0,
    )
    assert result.ok
    assert result.px == pytest.approx(1.01)  # 100 / 10_000 = 100 bps
    position = store.position_for_token(TOKEN)
    exit_result = broker.close(position, px=1.0, reason="take_profit", liq_usd=10_000.0)
    assert exit_result.px == pytest.approx(1 - 99.0099 / 10_000, rel=1e-4)


def test_impact_is_zero_without_liquidity(tmp_path):
    broker, _store = _broker(tmp_path, slippage_bps=0, fee_bps=0)
    result = broker.open(
        token=TOKEN, symbol="T", px=1.0, usd=100.0, source_wallet=WALLET, signal_id=None, whale_score=80,
    )
    assert result.px == pytest.approx(1.0)


def test_entry_fee_is_part_of_realised_pnl(tmp_path):
    broker, store = _broker(tmp_path, slippage_bps=0, fee_bps=50)
    opened = broker.open(
        token=TOKEN, symbol="T", px=1.0, usd=100.0, source_wallet=WALLET, signal_id=None, whale_score=80
    )
    assert opened.ok and broker.cash == pytest.approx(899.5)
    position = store.position_for_token(TOKEN)
    result = broker.close(position, px=1.0, reason="time_stop")
    assert result.ok
    closed = store.positions("closed")[0]
    # 100 in, 100 out, 0.50 entry fee, 0.50 exit fee.
    assert closed["pnl_usd"] == pytest.approx(-1.0)
    assert broker.cash == pytest.approx(999.0)


def test_close_is_exactly_once(tmp_path):
    broker, store = _broker(tmp_path, slippage_bps=0, fee_bps=0)
    broker.open(
        token=TOKEN, symbol="T", px=1.0, usd=100.0, source_wallet=WALLET, signal_id=None, whale_score=80
    )
    position = store.position_for_token(TOKEN)
    assert broker.close(position, px=1.2, reason="whale_exit:T").ok
    cash = broker.cash
    second = broker.close(position, px=1.2, reason="whale_exit:T")
    assert not second.ok and second.reason == "position already closed"
    assert broker.cash == pytest.approx(cash)
    assert len(store.positions("closed")) == 1
    sells = [f for f in store.query("SELECT * FROM fills WHERE side='sell'")]
    assert len(sells) == 1


def test_daily_halt_is_latched_for_the_day(tmp_path):
    broker, store = _broker(tmp_path, max_daily_loss_pct=0.10)
    store.add_equity(now_ts(), 1000.0, 0.0)
    broker.cash = 850.0
    assert broker.check_halt() is not None
    broker.cash = 1_050.0  # equity recovers the same UTC day
    assert broker.check_halt() is not None
