from __future__ import annotations

import pytest

from elys.config import Settings
from elys.scoring import copy_weight, score_wallet, shrunk_win_rate, win_rate
from elys.util import now_ts

WALLET = "0x" + "ab" * 20


def _row(**over):
    row = {
        "wallet": WALLET,
        "realized": 20_000.0,
        "unreal": 0.0,
        "volume": 50_000.0,
        "trades": 120,
        "tokens": 6,
        "wins": 40,
        "losses": 30,
        "last_ts": now_ts() - 3600,
    }
    row.update(over)
    return row


def test_win_rate_basics():
    assert win_rate(0, 0) is None
    assert win_rate(3, 1) == 0.75
    assert shrunk_win_rate(0, 0) == 0.5
    # shrinkage pulls small samples toward the prior, large ones barely move
    assert shrunk_win_rate(1, 0) < 0.65
    assert shrunk_win_rate(500, 300) == pytest.approx(506 / 812)


def test_strong_wallet_passes_and_tiers():
    profile = {
        "record": {"wins": 40, "losses": 20},
        "board": {"realized": 20_000.0, "trades": 120, "tokens": 6},
        "early": {"hunter": 0},
        "book": {"last_ts": now_ts() - 600},
    }
    score = score_wallet(_row(), profile, Settings())
    assert score.passed, score.reasons
    assert score.tier == "smart"
    assert 0 < score.score <= 100


def test_whale_tier_needs_conviction():
    profile = {
        "record": {"wins": 60, "losses": 40},
        "board": {"realized": 60_000.0, "trades": 200, "tokens": 9},
        "early": {"hunter": 0},
        "book": {"last_ts": now_ts() - 60},
    }
    score = score_wallet(_row(realized=60_000), profile, Settings())
    assert score.tier == "whale"


def test_thin_and_lucky_wallets_are_rejected():
    row = _row(trades=4, tokens=1, realized=80.0, wins=1, losses=0)
    score = score_wallet(row, None, Settings())
    assert not score.passed
    joined = " ".join(score.reasons)
    assert "thin" in joined and "narrow" in joined and "pnl" in joined


def test_losing_record_rejected_even_if_profitable():
    profile = {
        "record": {"wins": 12, "losses": 40},
        "board": {"realized": 9_000.0, "trades": 80, "tokens": 5},
        "early": {"hunter": 0},
        "book": {"last_ts": now_ts()},
    }
    score = score_wallet(_row(), profile, Settings())
    assert not score.passed
    assert any("wr" in reason for reason in score.reasons)


def test_hunter_tier_and_component():
    profile = {
        "record": {"wins": 10, "losses": 5},
        "board": {"realized": 1_500.0, "trades": 40, "tokens": 4},
        "early": {"hunter": 1, "entries": 8, "hits": 6, "smoothed": 0.7, "rate": 0.75},
        "book": {"last_ts": now_ts()},
    }
    score = score_wallet(_row(realized=1_500), profile, Settings())
    assert score.tier == "hunter"
    assert score.passed


def test_copy_weight_bounds_and_monotonicity():
    assert copy_weight(0) == 0.6
    assert copy_weight(30) == 0.6
    assert copy_weight(90) == 1.5
    assert copy_weight(200) == 1.5
    assert copy_weight(60) > copy_weight(50)


def test_recent_gate_rejects_small_buys():
    now = now_ts()
    recent = [
        {"ts": now - 10, "side": "sell", "usd": 900.0},
        {"ts": now - 20, "side": "buy", "usd": 10.0},
    ]
    score = score_wallet(_row(), None, Settings(), recent=recent)
    assert not score.passed
    assert any("largest recent buy" in reason for reason in score.reasons)
    assert score.copyable is False


def test_recent_gate_rejects_idle_wallet():
    now = now_ts()
    recent = [{"ts": now - 7200, "side": "buy", "usd": 1000.0}]
    score = score_wallet(_row(last_ts=now - 7200), None, Settings(), recent=recent)
    assert not score.passed
    assert any("idle" in reason for reason in score.reasons)


def test_active_copyable_wallet_passes_and_beats_idle():
    now = now_ts()
    active = [
        {"ts": now - 30, "side": "buy", "usd": 1200.0},
        {"ts": now - 90, "side": "sell", "usd": 1100.0},
    ]
    fresh = score_wallet(_row(), None, Settings(), recent=active)
    assert fresh.passed, fresh.reasons
    assert fresh.copyable is True
    idle = score_wallet(
        _row(last_ts=now - 1800), None, Settings(), recent=[{"ts": now - 1800, "side": "buy", "usd": 1200.0}]
    )
    assert fresh.score > idle.score
