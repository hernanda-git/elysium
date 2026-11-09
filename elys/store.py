"""SQLite persistence: whales, signals, paper positions, equity, TG posts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .util import now_ts

SCHEMA = """
CREATE TABLE IF NOT EXISTS whales (
    address     TEXT PRIMARY KEY,
    label       TEXT,
    tier        TEXT,
    score       REAL,
    win_rate    REAL,
    shrunk_wr   REAL,
    realized    REAL,
    volume      REAL,
    trades      INTEGER,
    tokens      INTEGER,
    hunter      INTEGER,
    early_rate  REAL,
    last_ts     INTEGER,
    first_seen  INTEGER,
    updated_ts  INTEGER,
    active      INTEGER NOT NULL DEFAULT 1,
    raw         TEXT
);

CREATE TABLE IF NOT EXISTS whale_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    address     TEXT NOT NULL,
    window      TEXT,
    ts          INTEGER,
    realized    REAL,
    volume      REAL,
    trades      INTEGER,
    wins        INTEGER,
    losses      INTEGER,
    tokens      INTEGER,
    score       REAL,
    raw         TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    wallet      TEXT NOT NULL,
    token       TEXT NOT NULL,
    symbol      TEXT,
    side        TEXT NOT NULL,
    px          REAL,
    usd         REAL,
    tx          TEXT,
    block       INTEGER,
    source      TEXT,
    latency_ms  INTEGER,
    whale_score REAL,
    whale_tier  TEXT,
    decision    TEXT,
    reason      TEXT,
    token_meta  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS signals_dedup ON signals (wallet, tx, token, side);
CREATE INDEX IF NOT EXISTS signals_ts ON signals (ts DESC);

CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token         TEXT NOT NULL,
    symbol        TEXT,
    entry_px      REAL NOT NULL,
    qty           REAL NOT NULL,
    usd           REAL NOT NULL,
    entry_ts      INTEGER NOT NULL,
    last_ts       INTEGER,
    source_wallet TEXT,
    source_signal INTEGER,
    conviction    INTEGER NOT NULL DEFAULT 1,
    peak_px       REAL,
    last_px       REAL,
    status        TEXT NOT NULL DEFAULT 'open',
    exit_px       REAL,
    exit_ts       INTEGER,
    exit_reason   TEXT,
    pnl_usd       REAL,
    pnl_pct       REAL,
    fee_usd       REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS positions_status ON positions (status);
CREATE INDEX IF NOT EXISTS positions_token ON positions (token);

CREATE TABLE IF NOT EXISTS fills (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    position_id INTEGER,
    side        TEXT NOT NULL,
    px          REAL,
    qty         REAL,
    usd         REAL,
    fee_usd     REAL,
    kind        TEXT,
    tx          TEXT
);

CREATE TABLE IF NOT EXISTS equity (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    cash          REAL NOT NULL,
    positions_usd REAL NOT NULL,
    equity_usd    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS equity_ts ON equity (ts DESC);

CREATE TABLE IF NOT EXISTS tg_posts (
    id      INTEGER PRIMARY KEY,
    channel TEXT,
    ts      INTEGER,
    kind    TEXT,
    text    TEXT,
    token   TEXT,
    wallets TEXT,
    tx      TEXT,
    raw     TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ── meta ─────────────────────────────────────────────────────────────────

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    # ── whales ───────────────────────────────────────────────────────────────

    def upsert_whale(self, score, *, raw: dict | None = None, window: str = "screen") -> None:
        now = now_ts()
        self.conn.execute(
            """
            INSERT INTO whales (address,label,tier,score,win_rate,shrunk_wr,realized,volume,
                                trades,tokens,hunter,early_rate,last_ts,first_seen,updated_ts,active,raw)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
            ON CONFLICT(address) DO UPDATE SET
                tier=excluded.tier, score=excluded.score, win_rate=excluded.win_rate,
                shrunk_wr=excluded.shrunk_wr, realized=excluded.realized, volume=excluded.volume,
                trades=excluded.trades, tokens=excluded.tokens, hunter=excluded.hunter,
                early_rate=excluded.early_rate, last_ts=excluded.last_ts,
                updated_ts=excluded.updated_ts, active=1, raw=excluded.raw
            """,
            (
                score.address,
                score.label,
                score.tier,
                score.score,
                score.win_rate,
                score.shrunk_wr,
                score.realized,
                score.volume,
                score.trades,
                score.tokens,
                score.hunter,
                score.early_rate,
                score.last_ts,
                now,
                now,
                json.dumps(raw or {}, separators=(",", ":"))[:8000],
            ),
        )
        self.conn.execute(
            """
            INSERT INTO whale_snapshots (address,window,ts,realized,volume,trades,wins,losses,tokens,score,raw)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                score.address,
                window,
                now,
                score.realized,
                score.volume,
                score.trades,
                score.wins,
                score.losses,
                score.tokens,
                score.score,
                json.dumps(raw or {}, separators=(",", ":"))[:8000],
            ),
        )
        self.conn.commit()

    def load_whales(self, *, active_only: bool = True, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM whales"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY score DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(sql)]

    def deactivate_whales(self, keep: Iterable[str]) -> None:
        keep = list(keep)
        if not keep:
            return
        marks = ",".join("?" * len(keep))
        self.conn.execute(f"UPDATE whales SET active=0 WHERE address NOT IN ({marks})", keep)
        self.conn.commit()

    def whale_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) c FROM whales WHERE active=1").fetchone()["c"])

    # ── signals ──────────────────────────────────────────────────────────────

    def log_signal(self, signal: dict) -> int | None:
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO signals
              (ts,wallet,token,symbol,side,px,usd,tx,block,source,latency_ms,
               whale_score,whale_tier,decision,reason,token_meta)
            VALUES (:ts,:wallet,:token,:symbol,:side,:px,:usd,:tx,:block,:source,:latency_ms,
                    :whale_score,:whale_tier,:decision,:reason,:token_meta)
            """,
            signal,
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return None
        return int(cur.lastrowid or 0)

    def set_signal_decision(self, signal_id: int, decision: str, reason: str) -> None:
        self.conn.execute(
            "UPDATE signals SET decision=?, reason=? WHERE id=?", (decision, reason, signal_id)
        )
        self.conn.commit()

    def recent_signals(self, limit: int = 50) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,))
        ]

    # ── positions ────────────────────────────────────────────────────────────

    def open_position(self, pos: dict) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO positions (token,symbol,entry_px,qty,usd,entry_ts,last_ts,
                                   source_wallet,source_signal,conviction,peak_px,last_px,fee_usd)
            VALUES (:token,:symbol,:entry_px,:qty,:usd,:entry_ts,:last_ts,
                    :source_wallet,:source_signal,:conviction,:peak_px,:peak_px,:fee_usd)
            """,
            pos,
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def add_to_position(self, position_id: int, qty: float, usd: float, px: float, fee: float, ts: int) -> None:
        self.conn.execute(
            """
            UPDATE positions
               SET qty = qty + ?, usd = usd + ?, entry_px = (usd + ?) / (qty + ?),
                   conviction = conviction + 1, last_ts = ?, fee_usd = fee_usd + ?
             WHERE id = ?
            """,
            (qty, usd, usd, qty, ts, fee, position_id),
        )
        self.conn.commit()

    def update_position(self, position_id: int, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE positions SET {assignments} WHERE id=?",
            (*fields.values(), position_id),
        )
        self.conn.commit()

    def close_position(self, position_id: int, **fields: Any) -> None:
        self.update_position(position_id, status="closed", **fields)

    def load_open_positions(self) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute("SELECT * FROM positions WHERE status='open' ORDER BY id")
        ]

    def position_for_token(self, token: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE token=? AND status='open' ORDER BY id LIMIT 1", (token,)
        ).fetchone()
        return dict(row) if row else None

    def positions(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status=? ORDER BY id DESC", (status,)
            )
        else:
            rows = self.conn.execute("SELECT * FROM positions ORDER BY id DESC")
        return [dict(r) for r in rows]

    # ── fills ────────────────────────────────────────────────────────────────

    def add_fill(self, fill: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO fills (ts,position_id,side,px,qty,usd,fee_usd,kind,tx)
            VALUES (:ts,:position_id,:side,:px,:qty,:usd,:fee_usd,:kind,:tx)
            """,
            fill,
        )
        self.conn.commit()

    # ── equity ───────────────────────────────────────────────────────────────

    def add_equity(self, ts: int, cash: float, positions_usd: float) -> None:
        self.conn.execute(
            "INSERT INTO equity (ts,cash,positions_usd,equity_usd) VALUES (?,?,?,?)",
            (ts, cash, positions_usd, cash + positions_usd),
        )
        self.conn.commit()

    def equity_curve(self, limit: int = 5000) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT * FROM equity ORDER BY id DESC LIMIT ?", (limit,)
            )
        ][::-1]

    # ── telegram ─────────────────────────────────────────────────────────────

    def add_tg_post(self, post: dict) -> bool:
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO tg_posts (id,channel,ts,kind,text,token,wallets,tx,raw)
            VALUES (:id,:channel,:ts,:kind,:text,:token,:wallets,:tx,:raw)
            """,
            post,
        )
        self.conn.commit()
        return cur.rowcount > 0

    def tg_posts(self, limit: int = 50) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute("SELECT * FROM tg_posts ORDER BY id DESC LIMIT ?", (limit,))
        ]

    # ── paper lifecycle ──────────────────────────────────────────────────────

    def reset_paper(self, start_equity: float) -> None:
        self.conn.execute("DELETE FROM positions")
        self.conn.execute("DELETE FROM fills")
        self.conn.execute("DELETE FROM equity")
        self.conn.execute("DELETE FROM signals")
        self.conn.commit()
        self.set_meta("cash", round(start_equity, 8))
        self.set_meta("paper_started", now_ts())

    def cash(self, start_equity: float) -> float:
        raw = self.get_meta("cash")
        if raw is None:
            self.set_meta("cash", round(start_equity, 8))
            self.set_meta("paper_started", now_ts())
            return start_equity
        return float(raw)

    # ── reporting helpers ────────────────────────────────────────────────────

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params)]
