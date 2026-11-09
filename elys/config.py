"""Runtime configuration: .env loading plus typed settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    """Minimal .env reader; real environment always wins."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _str(key: str, default: str) -> str:
    return os.environ.get(key, default).strip()


def _float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return float(raw)


def _int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return int(raw)


def _bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(slots=True)
class Settings:
    # data plane
    intel_base: str = "https://api.elysium.eco"
    rpc_url: str = "https://rpc.elysium.kinetiq.xyz"
    db_path: Path = field(default_factory=lambda: ROOT / "data" / "elys.db")
    log_path: Path = field(default_factory=lambda: ROOT / "data" / "elys.log")

    # paper account
    start_equity: float = 1000.0
    per_trade_usd: float = 25.0
    max_positions: int = 8
    max_watch: int = 32
    max_token_usd: float = 50.0
    max_daily_loss_pct: float = 0.10
    max_adds_per_token: int = 2

    # execution model
    slippage_bps: float = 150.0
    fee_bps: float = 50.0

    # copy rules
    min_whale_usd: float = 250.0
    min_liq_usd: float = 15_000.0
    max_top10_pct: float = 45.0
    min_token_age_s: int = 60
    copy_sells: bool = True
    ignore_symbols: tuple[str, ...] = ("WHYPE", "HYPE", "USDC", "USDT", "USDe")

    # exits
    stop_loss_pct: float = 0.35
    take_profit_pct: float = 1.50
    trail_arm_pct: float = 0.60
    trail_giveback_pct: float = 0.30
    max_hold_min: int = 240

    # discovery screen
    discover_windows: tuple[str, ...] = ("1d", "7d", "30d")
    discover_limit: int = 300
    discover_min_trades: int = 20
    discover_min_tokens: int = 3
    discover_min_realized: float = 500.0
    discover_min_wr: float = 0.50
    discover_probe_trades: int = 80
    discover_max_idle_s: int = 3600
    discover_min_recent_buys: int = 1

    # monitoring
    poll_interval_s: float = 20.0
    stream_enabled: bool = True

    # telegram
    tg_channel: str = "elysiumecoalerts"
    tg_interval_s: float = 5.0
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_phone: str = ""

    # misc
    user_agent: str = "elysium-copytrader/0.1 (+https://elysium.eco)"

    @property
    def stream_url(self) -> str:
        return f"{self.intel_base.rstrip('/')}/api/stream"

    @property
    def telegram_session(self) -> Path:
        return ROOT / "data" / "telegram"

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        env_file = env_file or ROOT / ".env"
        load_dotenv(env_file)
        root = ROOT
        return cls(
            intel_base=_str("ELYS_INTEL_BASE", "https://api.elysium.eco"),
            rpc_url=_str("ELYS_RPC_URL", "https://rpc.elysium.kinetiq.xyz"),
            db_path=root / _str("ELYS_DB", "data/elys.db"),
            log_path=root / _str("ELYS_LOG", "data/elys.log"),
            start_equity=_float("ELYS_START_EQUITY", 1000.0),
            per_trade_usd=_float("ELYS_PER_TRADE_USD", 25.0),
            max_positions=_int("ELYS_MAX_POSITIONS", 8),
            max_watch=_int("ELYS_MAX_WATCH", 32),
            max_token_usd=_float("ELYS_MAX_TOKEN_USD", 50.0),
            max_daily_loss_pct=_float("ELYS_MAX_DAILY_LOSS_PCT", 0.10),
            max_adds_per_token=_int("ELYS_MAX_ADDS_PER_TOKEN", 2),
            slippage_bps=_float("ELYS_SLIPPAGE_BPS", 150.0),
            fee_bps=_float("ELYS_FEE_BPS", 50.0),
            min_whale_usd=_float("ELYS_MIN_WHALE_USD", 250.0),
            min_liq_usd=_float("ELYS_MIN_LIQ_USD", 15_000.0),
            max_top10_pct=_float("ELYS_MAX_TOP10_PCT", 45.0),
            min_token_age_s=_int("ELYS_MIN_TOKEN_AGE_S", 60),
            copy_sells=_bool("ELYS_COPY_SELLS", True),
            ignore_symbols=_list("ELYS_IGNORE_SYMBOLS", ("WHYPE", "HYPE", "USDC", "USDT", "USDe")),
            stop_loss_pct=_float("ELYS_STOP_LOSS_PCT", 0.35),
            take_profit_pct=_float("ELYS_TAKE_PROFIT_PCT", 1.50),
            trail_arm_pct=_float("ELYS_TRAIL_ARM_PCT", 0.60),
            trail_giveback_pct=_float("ELYS_TRAIL_GIVEBACK_PCT", 0.30),
            max_hold_min=_int("ELYS_MAX_HOLD_MIN", 240),
            discover_windows=_list("ELYS_DISCOVER_WINDOWS", ("1d", "7d", "30d")),
            discover_limit=_int("ELYS_DISCOVER_LIMIT", 300),
            discover_min_trades=_int("ELYS_DISCOVER_MIN_TRADES", 20),
            discover_min_tokens=_int("ELYS_DISCOVER_MIN_TOKENS", 3),
            discover_min_realized=_float("ELYS_DISCOVER_MIN_REALIZED", 500.0),
            discover_min_wr=_float("ELYS_DISCOVER_MIN_WR", 0.50),
            discover_probe_trades=_int("ELYS_DISCOVER_PROBE_TRADES", 80),
            discover_max_idle_s=_int("ELYS_DISCOVER_MAX_IDLE_S", 3600),
            discover_min_recent_buys=_int("ELYS_DISCOVER_MIN_RECENT_BUYS", 1),
            poll_interval_s=_float("ELYS_POLL_INTERVAL_S", 20.0),
            stream_enabled=_bool("ELYS_STREAM", True),
            tg_channel=_str("ELYS_TG_CHANNEL", "elysiumecoalerts"),
            tg_interval_s=_float("ELYS_TG_INTERVAL_S", 5.0),
            tg_api_id=_int("TG_API_ID", 0),
            tg_api_hash=_str("TG_API_HASH", ""),
            tg_phone=_str("TG_PHONE", ""),
        )
