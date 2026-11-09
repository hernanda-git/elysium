from __future__ import annotations

from elys.config import Settings
from elys.scoring import WalletScore
from elys.store import Store
from elys.util import now_ts
from elys.web import WebDB, page_alerts, page_overview, page_whales


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
    assert "Elysium Copy Desk" in html
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
