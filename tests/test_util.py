from __future__ import annotations

import pytest

from elys.util import clamp, fmt_px, fmt_usd, norm_addr, short_addr

ZERO = "0x" + "0" * 40


def test_norm_addr_padded_word():
    padded = "00000000000000000000000000000000000a4b05"
    assert norm_addr(padded) == "0x" + padded


def test_norm_addr_checksum_and_prefix():
    assert norm_addr("0x9041A814DED6D207F8EA264024D32A41E23C8B43") == (
        "0x9041a814ded6d207f8ea264024d32a41e23c8b43"
    )


def test_norm_addr_short_and_empty():
    assert norm_addr("a4b05") == "0x" + "0" * 35 + "a4b05"
    assert norm_addr(None) == ZERO
    assert norm_addr("") == ZERO
    assert norm_addr("not-an-address") == ZERO


def test_short_addr():
    assert short_addr("0x9041a814ded6d207f8ea264024d32a41e23c8b43") == "0x9041…8b43"


def test_clamp():
    assert clamp(1.5) == 1.0
    assert clamp(-2) == 0.0
    assert clamp(0.4) == 0.4
    assert clamp(5, 0, 3) == 3


def test_fmt_usd():
    assert fmt_usd(1_250_000) == "$1.25M"
    assert fmt_usd(1_500) == "$1,500.00"
    assert fmt_usd(10_000) == "$10.0K"
    assert fmt_usd(-12.5) == "-$12.50"
    assert fmt_usd(0.0004) == "$0.0004"


def test_fmt_px():
    assert fmt_px(0.0005809).startswith("$0.00058")
    assert fmt_px(2.5) == "$2.5000"
    assert fmt_px(0) == "–"
