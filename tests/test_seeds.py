from __future__ import annotations

import json

from elys.seeds import load_seed_addresses

A = "0x" + "ab" * 20
B = "0x" + "cd" * 20


def test_missing_or_disabled_seed_file_is_empty(tmp_path):
    assert load_seed_addresses(None) == []
    assert load_seed_addresses(tmp_path / "missing.json") == []


def test_seed_file_normalises_dedupes_and_drops_invalid(tmp_path):
    path = tmp_path / "whales.json"
    padded = "0x" + "0" * 24 + "ab" * 20  # ABI-padded 32-byte word
    path.write_text(
        json.dumps([A.upper().replace("0X", "0x"), padded, "0x" + "zz" * 20, B, None]),
        encoding="utf-8",
    )
    assert load_seed_addresses(path) == [A, B]


def test_seed_file_accepts_wallet_objects(tmp_path):
    path = tmp_path / "whales.json"
    path.write_text(json.dumps({"wallets": [{"address": A, "label": "curated"}, B]}), encoding="utf-8")
    assert load_seed_addresses(path) == [A, B]


def test_seed_file_ignores_malformed_json(tmp_path):
    path = tmp_path / "whales.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_seed_addresses(path) == []
