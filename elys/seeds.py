"""Curated smart-money wallets that discovery always re-verifies.

The public leaderboard exposes 300 rows per window, so a wallet that drifts down
the board silently falls out of the candidate set even though its on-chain record
is unchanged. The seed file keeps a named list in the discovery pool: every
address is still re-scored against live trades on each pass — the regular gates
apply — it just cannot fall out of the candidate set while the file lists it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .util import norm_addr

log = logging.getLogger("elys.seeds")

ZERO_ADDRESS = "0x" + "0" * 40


def load_seed_addresses(path: Path | None) -> list[str]:
    """Read a curated seed file: a JSON array of addresses or ``{"address": …}``.

    Accepts either the bare array or an object with ``wallets``/``addresses``.
    Invalid entries are dropped; duplicates keep their first position.
    """
    if path is None or not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("seed file %s unreadable: %s", path, exc)
        return []

    if isinstance(payload, dict):
        payload = payload.get("wallets") or payload.get("addresses") or []
    if not isinstance(payload, list):
        log.warning("seed file %s: expected a JSON array, got %s", path, type(payload).__name__)
        return []

    out: list[str] = []
    for item in payload:
        raw = item.get("address") if isinstance(item, dict) else item
        addr = norm_addr(raw)
        if addr == ZERO_ADDRESS or addr in out:
            continue
        out.append(addr)
    return out
