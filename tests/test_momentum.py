from __future__ import annotations

import asyncio

from elys.config import Settings
from elys.momentum import MomentumScanner, evaluate
from elys.paper import PaperBroker
from elys.store import Store
from elys.util import now_ts

TOKEN = "0x" + "55" * 20


def _settings(tmp_path, **over) -> Settings:
    settings = Settings(db_path=tmp_path / "t.db", log_path=tmp_path / "t.log")
    for key, value in over.items():
        setattr(settings, key, value)
    return settings


def _row(**over) -> dict:
    now = now_ts()
    base = {
        "token": TOKEN,
        "symbol": "MOMO",
        "px": 1.0,
        "mcap": 1_000_000.0,
        "liq_usd": 100_000.0,
        "vol_1h": 10_000.0,
        "vol_5m": 2_000.0,
        "vol_6h": 30_000.0,
        "buys_1h": 60,
        "sells_1h": 40,
        "net_1h": 500.0,
        "chg_1h": 0.05,
        "chg_5m": 1.0,
        "hp_verdict": "clean",
        "rug": 0,
        "rug_by_creator": 0,
        "hp_wiped": 0,
        "top10_pct": 20.0,
        "underwater_pct": 50.0,
        "clusters": 1,
        "insider_pct": 5.0,
        "launch_ts": now - 3_600,
    }
    base.update(over)
    return base


class FakeIntel:
    def __init__(self, rows=None, meta=None) -> None:
        self.rows = rows or [_row()]
        self.meta = meta or {"px": 1.0, "liq_usd": 100_000.0, "chg_5m": 1.0}

    async def universe(self, limit: int = 200) -> list[dict]:
        return self.rows

    async def launches(self, limit: int = 50) -> list[dict]:
        return []

    async def token(self, address: str, ttl: float = 4.0) -> dict:
        return self.meta


def test_candidate_passes_all_gates(tmp_path):
    cands = evaluate([_row()], _settings(tmp_path))
    assert len(cands) == 1
    cand = cands[0]
    assert cand.token == TOKEN
    assert cand.accel == 2.5
    assert 0.59 < cand.buy_ratio < 0.61


def test_low_volume_acceleration_is_rejected(tmp_path):
    assert evaluate([_row(vol_6h=80_000.0)], _settings(tmp_path)) == []


def test_negative_net_flow_is_rejected(tmp_path):
    assert evaluate([_row(net_1h=-1.0)], _settings(tmp_path)) == []


def test_small_mcap_is_rejected(tmp_path):
    assert evaluate([_row(mcap=50_000.0)], _settings(tmp_path)) == []


def test_missing_mcap_is_recomputed_from_price_and_supply(tmp_path):
    row = _row(px=2.0, supply_circ=600_000.0)
    row.pop("mcap")
    cands = evaluate([row], _settings(tmp_path))
    assert len(cands) == 1
    assert cands[0].mcap == 1_200_000.0


def test_missing_mcap_without_supply_is_rejected(tmp_path):
    row = _row()
    row.pop("mcap")
    assert evaluate([row], _settings(tmp_path)) == []


def test_untested_honeypot_is_rejected(tmp_path):
    assert evaluate([_row(hp_verdict="untested")], _settings(tmp_path)) == []


def test_young_token_is_rejected(tmp_path):
    assert evaluate([_row(launch_ts=now_ts() - 60)], _settings(tmp_path)) == []


def test_five_minute_spike_is_rejected(tmp_path):
    assert evaluate([_row(chg_5m=16.0)], _settings(tmp_path)) == []


def test_candidates_are_ranked_by_score(tmp_path):
    strong = _row(vol_6h=20_000.0, buys_1h=80)
    weak = _row(token="0x" + "66" * 20, symbol="WEAK", vol_6h=40_000.0, buys_1h=55)
    cands = evaluate([weak, strong], _settings(tmp_path))
    assert [c.token for c in cands] == [TOKEN, "0x" + "66" * 20]


def _scanner(tmp_path, rows=None, **over):
    settings = _settings(tmp_path, slippage_bps=0, fee_bps=0, **over)
    store = Store(settings.db_path)
    intel = FakeIntel(rows=rows)
    broker = PaperBroker(settings, intel, store)
    return MomentumScanner(settings, intel, store, broker), store, broker


def test_launch_probes_run_concurrently(tmp_path):
    scanner, _store, _broker = _scanner(tmp_path, momentum_launch_probe=20)

    class ProbeIntel(FakeIntel):
        def __init__(self) -> None:
            super().__init__()
            self.inflight = 0
            self.max_inflight = 0

        async def launches(self, limit: int = 50) -> list[dict]:
            return [
                {"token": "0x" + f"{i:040x}", "launch_ts": now_ts() - 60}
                for i in range(20)
            ]

        async def token(self, address: str, ttl: float = 4.0) -> dict:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            await asyncio.sleep(0.02)
            self.inflight -= 1
            return {"token": address, "px": 1.0}

    probe = ProbeIntel()
    scanner.intel = probe
    rows = asyncio.run(scanner._launch_rows(now_ts()))
    assert len(rows) == 20
    assert probe.max_inflight > 1


def test_scan_opens_a_momentum_position(tmp_path):
    scanner, store, _broker = _scanner(tmp_path)
    opened = asyncio.run(scanner.scan_once())
    assert opened == 1
    position = store.position_for_token(TOKEN)
    assert position is not None
    assert position["strategy"] == "momentum"
    assert store.query("SELECT decision FROM signals")[0]["decision"] == "open"


def test_scan_respects_position_cap(tmp_path):
    rows = [_row(), _row(token="0x" + "77" * 20, symbol="TWO")]
    scanner, store, _broker = _scanner(tmp_path, rows=rows, momentum_max_positions=1)
    opened = asyncio.run(scanner.scan_once())
    assert opened == 1
    assert len(store.load_open_positions()) == 1


def test_momentum_and_copy_use_different_stops(tmp_path):
    settings = _settings(tmp_path, slippage_bps=0, fee_bps=0)
    store = Store(settings.db_path)
    broker = PaperBroker(settings, FakeIntel(), store)
    broker.open(
        token=TOKEN, symbol="M", px=1.0, usd=100.0, source_wallet="m",
        signal_id=None, whale_score=0, strategy="momentum",
    )
    broker.open(
        token="0x" + "88" * 20, symbol="C", px=1.0, usd=100.0, source_wallet="c",
        signal_id=None, whale_score=0, strategy="copy",
    )
    intel = FakeIntel(meta={"px": 0.88, "liq_usd": 100_000.0})
    broker.intel = intel
    asyncio.run(broker.mark())
    momentum = [p for p in store.positions() if p["token"] == TOKEN][0]
    copied = [p for p in store.positions() if p["token"] == "0x" + "88" * 20][0]
    assert momentum["status"] == "closed" and momentum["exit_reason"] == "stop_loss"
    assert copied["status"] == "open"
