from __future__ import annotations

from elys.config import Settings
from elys.scoring import WalletScore
from elys.store import Store
from elys.util import now_ts
from elys.web import WebDB, page_alerts, page_overview, page_signals, page_whales


def _fixture(tmp_path):
    settings = Settings(db_path=tmp_path / "web.db", log_path=tmp_path / "web.log")
    store = Store(settings.db_path)
    store.set_meta("cash", 1000.0)
    store.set_meta("heartbeat_ts", now_ts())
    store.set_meta("stream_connected", "1")

    pid = store.open_position(
        {
            "token": "0x" + "11" * 20,
            "symbol": "<b>X</b>",
            "entry_px": 1.0,
            "qty": 10.0,
            "usd": 10.0,
            "entry_ts": now_ts() - 60,
            "last_ts": now_ts(),
            "source_wallet": "0x" + "22" * 20,
            "source_signal": None,
            "conviction": 1,
            "peak_px": 1.0,
            "fee_usd": 0.0,
        }
    )
    store.update_position(pid, last_px=2.0)
    store.upsert_whale(
        WalletScore(
            address="0x" + "aa" * 20,
            score=77.0,
            tier="smart",
            passed=True,
            win_rate=0.7,
            realized=5000.0,
            trades=40,
            tokens=8,
        )
    )
    store.add_tg_post(
        {
            "id": 1,
            "channel": "elysiumecoalerts",
            "ts": now_ts(),
            "kind": "rug_warning",
            "text": "Rug warning · TEST",
            "token": "0x" + "11" * 20,
            "wallets": "",
            "tx": "",
            "raw": None,
        }
    )
    return settings, WebDB(settings.db_path)


def test_summary_math(tmp_path):
    settings, db = _fixture(tmp_path)
    summary = db.summary(settings)
    assert summary["cash"] == 1000.0
    assert summary["positions_usd"] == 20.0
    assert summary["equity"] == 1020.0
    assert summary["open_count"] == 1
    assert summary["whales"] == 1
    assert summary["stream"] is True
    assert summary["win_rate"] is None


def test_overview_escapes_untrusted_symbols(tmp_path):
    settings, db = _fixture(tmp_path)
    html = page_overview(db, settings)
    assert "Elysium Desk" in html
    assert "&lt;b&gt;X&lt;/b&gt;" in html
    assert "<b>X</b>" not in html
    assert "-$0.00" not in html and "$0.00" in html


def test_whales_and_alerts_render(tmp_path):
    settings, db = _fixture(tmp_path)
    whales = page_whales(db, settings)
    assert "0x" + "aa" * 20 in whales
    assert "smart" in whales
    alerts = page_alerts(db, settings)
    assert "rug warning" in alerts
    assert "elysium.eco/screener/" in alerts


def _signal(**over) -> dict:
    base = {
        "ts": now_ts(),
        "wallet": "0x" + "aa" * 20,
        "token": "0x" + "66" * 20,
        "symbol": "TST",
        "side": "buy",
        "px": 0.01,
        "usd": 25.0,
        "tx": "0x" + "77" * 32,
        "block": 1,
        "source": "test",
        "latency_ms": 1000,
        "whale_score": 0.0,
        "whale_tier": "",
        "decision": "open",
        "reason": "",
        "token_meta": None,
    }
    base.update(over)
    return base


def test_signals_page_separates_screen_entries_from_copy_signals(tmp_path):
    settings = Settings(db_path=tmp_path / "split.db", log_path=tmp_path / "split.log")
    store = Store(settings.db_path)
    store.log_signal(
        _signal(
            wallet="screen",
            strategy="momentum",
            symbol="MOMO",
            tx="0x" + "11" * 32,
            reason="accel 4.2 buy 0.61",
        )
    )
    store.log_signal(
        _signal(
            wallet="0x" + "bb" * 20,
            symbol="WHALE",
            tx="0x" + "22" * 32,
            reason="copied",
        )
    )

    html = page_signals(WebDB(settings.db_path), settings)
    screen_part, copy_part = html.split("Copy signals", 1)
    assert "Screen entries" in screen_part
    assert "MOMO" in screen_part and "accel 4.2 buy 0.61" in screen_part
    assert "WHALE" in copy_part
    assert "0x0000" not in html
    assert "Whale size" not in html
