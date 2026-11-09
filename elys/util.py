"""Small shared helpers: address normalisation, time and console formatting."""

from __future__ import annotations

import math
import os
import re
import time

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_ZERO = "0x" + "0" * 40


def norm_addr(value: object) -> str:
    """Normalise any Elysium address form to lowercase 0x + 40 hex chars.

    Handles ABI-padded 32-byte words, 40-hex without prefix, and the odd
    upper-case checksum form the explorer emits in some feeds.
    """
    if value is None:
        return _ZERO
    text = str(value).strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if len(text) == 64:
        text = text[-40:]
    if len(text) < 40:
        text = text.zfill(40)
    if not _HEX40.match(text):
        return _ZERO
    return "0x" + text


def short_addr(value: object) -> str:
    addr = norm_addr(value)
    return f"{addr[:6]}…{addr[-4:]}"


def now_ts() -> int:
    return int(time.time())


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def fmt_usd(value: float | None) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "–"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1_000_000:
        return f"{sign}${value / 1_000_000:,.2f}M"
    if value >= 1_000:
        return f"{sign}${value / 1_000:,.1f}K"
    if value >= 1:
        return f"{sign}${value:,.2f}"
    return f"{sign}${value:,.4f}"


def fmt_px(value: float | None) -> str:
    if value is None or value <= 0:
        return "–"
    if value >= 1:
        return f"${value:,.4f}"
    return f"${value:.8f}".rstrip("0").rstrip(".")


def fmt_num(value: float | None) -> str:
    if value is None:
        return "–"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:,.2f}M"
    if abs(value) >= 1_000:
        return f"{value / 1_000:,.1f}K"
    return f"{value:,.0f}" if abs(value) >= 10 else f"{value:,.2f}"


def fmt_secs(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def hhmmss(ts: int | None = None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts if ts is not None else time.time()))


_COLOR = os.environ.get("NO_COLOR") is None


def paint(text: str, code: str) -> str:
    if not _COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def dim(text: str) -> str:
    return paint(text, "2")


def green(text: str) -> str:
    return paint(text, "32")


def red(text: str) -> str:
    return paint(text, "31")


def yellow(text: str) -> str:
    return paint(text, "33")


def cyan(text: str) -> str:
    return paint(text, "36")


def bold(text: str) -> str:
    return paint(text, "1")
