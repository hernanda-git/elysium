from __future__ import annotations

from elys.config import Settings
from elys.scoring import WalletScore
from elys.store import Store
from elys.util import now_ts


def _signal(**over) -> dict:
    base = {
        "ts": now_ts(),
        "wallet": "0x" + "55" * 20,
        "token": "0x" + "66" * 20,
        "symbol": "TST",
        "side": "buy",
        "px": 0.01,
        "usd": 500.0,
        "tx": "0x" + "77" * 32,
        "block": 1,
        "source": "test",
        "latency_ms": 100,
        "whale_score": 70.0,
        "whale_tier": "smart",
        "decision": None,
        "reason": None,
        "token_meta": None,
    }
    base.update(over)
    return base


def test_signal_dedup_by_tx_token_side(tmp_path):
    store = Store(tmp_path / "t.db")
    first = store.log_signal(_signal())
    assert first is not None
    assert store.log_signal(_signal()) is None
    # different tx is a different trade
    assert store.log_signal(_signal(tx="0x" + "88" * 32)) is not None


def test_signal_decision_update(tmp_path):
    store = Store(tmp_path / "t.db")
    signal_id = store.log_signal(_signal())
    store.set_signal_decision(signal_id, "open", "ok")
    row = store.recent_signals(1)[0]
    assert row["decision"] == "open" and row["reason"] == "ok"


def test_whale_upsert_and_deactivate(tmp_path):
    store = Store(tmp_path / "t.db")
    score = WalletScore(
        address="0x" + "aa" * 20,
        score=80.0,
        tier="whale",
        passed=True,
        win_rate=0.7,
        shrunk_wr=0.66,
        realized=100_000.0,
        volume=500_000.0,
        trades=300,
        tokens=12,
        wins=70,
        losses=30,
        last_ts=now_ts(),
    )
    store.upsert_whale(score, raw={"ok": True})
    store.upsert_whale(score)
    rows = store.load_whales()
    assert len(rows) == 1
    assert rows[0]["tier"] == "whale"
    assert store.whale_count() == 1

    other = WalletScore(address="0x" + "bb" * 20, score=50.0, tier="smart", passed=True)
    store.upsert_whale(other)
    assert store.whale_count() == 2
    store.deactivate_whales([other.address])
    assert store.whale_count() == 1


def test_reset_paper_restores_equity(tmp_path):
    settings = Settings(db_path=tmp_path / "t.db", log_path=tmp_path / "t.log", start_equity=500.0)
    store = Store(settings.db_path)
    store.set_meta("cash", 123.0)
    store.upsert_pending({
        "token": "0x" + "ee" * 20, "symbol": "T", "limit_px": 1.0,
        "expires_ts": now_ts() + 60, "whale": "0x" + "ff" * 20,
        "whale_score": 80.0, "created_ts": now_ts(),
    })
    store.reset_paper(settings.start_equity)
    assert store.cash(settings.start_equity) == 500.0
    assert store.load_open_positions() == []
    assert store.pending_entries() == []


def test_pending_entry_upsert_and_remove(tmp_path):
    store = Store(tmp_path / "t.db")
    token = "0x" + "cc" * 20
    base = {
        "token": token,
        "symbol": "T",
        "limit_px": 1.0,
        "expires_ts": now_ts() + 60,
        "whale": "0x" + "dd" * 20,
        "whale_score": 80.0,
        "created_ts": now_ts(),
    }
    store.upsert_pending(base)
    store.upsert_pending({**base, "limit_px": 0.9, "expires_ts": now_ts() + 120})
    row = store.pending_for_token(token)
    assert row["limit_px"] == 0.9
    assert row["expires_ts"] > now_ts() + 60
    store.remove_pending(token)
    assert store.pending_for_token(token) is None
