"""SQLite persistence: whales, signals, paper positions, equity, TG posts."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

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
    strategy    TEXT NOT NULL DEFAULT 'copy',
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
    strategy      TEXT NOT NULL DEFAULT 'copy',
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

CREATE TABLE IF NOT EXISTS pending_entries (
    token       TEXT PRIMARY KEY,
    symbol      TEXT,
    limit_px    REAL NOT NULL,
    expires_ts  INTEGER NOT NULL,
    whale       TEXT,
    whale_score REAL,
    created_ts  INTEGER
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit mode: every write is visible immediately unless it is wrapped
        # in ``transaction()``. That makes ledger transitions explicit rather than
        # relying on sqlite3's implicit-transaction quirks.
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        """Add columns introduced after the first deployed schema."""
        pcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(positions)")}
        if "strategy" not in pcols:
            self.conn.execute(
                "ALTER TABLE positions ADD COLUMN strategy TEXT NOT NULL DEFAULT 'copy'"
            )
        scols = {r["name"] for r in self.conn.execute("PRAGMA table_info(signals)")}
        if "strategy" not in scols:
            self.conn.execute(
                "ALTER TABLE signals ADD COLUMN strategy TEXT NOT NULL DEFAULT 'copy'"
            )
        # Rows written before the split used a synthetic wallet name for screen
        # entries; retag the legacy rows so the two feeds stay distinguishable.
        self.conn.execute(
            "UPDATE signals SET strategy='momentum' "
            "WHERE strategy='copy' AND wallet IN ('momentum','screen')"
        )
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(pending_entries)")}
        for name, decl in (
            ("signal_ts", "INTEGER"),
            ("signal_usd", "REAL"),
            ("signal_liq", "REAL"),
            ("conviction", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if name not in cols:
                self.conn.execute(f"ALTER TABLE pending_entries ADD COLUMN {name} {decl}")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One atomic ledger transition: all writes commit or none do."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

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
        signal = {"strategy": "copy", **signal}
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO signals
              (ts,wallet,strategy,token,symbol,side,px,usd,tx,block,source,latency_ms,
               whale_score,whale_tier,decision,reason,token_meta)
            VALUES (:ts,:wallet,:strategy,:token,:symbol,:side,:px,:usd,:tx,:block,:source,:latency_ms,
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
        pos = {**{"strategy": "copy"}, **pos}
        cur = self.conn.execute(
            """
            INSERT INTO positions (token,symbol,entry_px,qty,usd,entry_ts,last_ts,
                                   source_wallet,source_signal,strategy,conviction,peak_px,last_px,fee_usd)
            VALUES (:token,:symbol,:entry_px,:qty,:usd,:entry_ts,:last_ts,
                    :source_wallet,:source_signal,:strategy,:conviction,:peak_px,:peak_px,:fee_usd)
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

    # ── atomic ledger transitions ────────────────────────────────────────────

    def open_with_fill(self, pos: dict, fill: dict, cash: float) -> int:
        """Insert a position, its buy fill and the new cash balance atomically."""
        pos = {**{"strategy": "copy"}, **pos}
        with self.transaction():
            cur = self.conn.execute(
                """
                INSERT INTO positions (token,symbol,entry_px,qty,usd,entry_ts,last_ts,
                                       source_wallet,source_signal,strategy,conviction,peak_px,last_px,fee_usd)
                VALUES (:token,:symbol,:entry_px,:qty,:usd,:entry_ts,:last_ts,
                        :source_wallet,:source_signal,:strategy,:conviction,:peak_px,:peak_px,:fee_usd)
                """,
                pos,
            )
            position_id = int(cur.lastrowid or 0)
            self._insert_fill({**fill, "position_id": position_id})
            self._set_meta("cash", round(cash, 8))
        return position_id

    def add_with_fill(self, position_id: int, qty: float, usd: float, px: float, fee: float,
                      ts: int, fill: dict, cash: float) -> bool:
        """Increase an open position and append the fill atomically."""
        with self.transaction():
            cur = self.conn.execute(
                """
                UPDATE positions
                   SET qty = qty + ?, usd = usd + ?, entry_px = (usd + ?) / (qty + ?),
                       conviction = conviction + 1, last_ts = ?, fee_usd = fee_usd + ?
                 WHERE id = ? AND status = 'open'
                """,
                (qty, usd, usd, qty, ts, fee, position_id),
            )
            if cur.rowcount != 1:
                return False
            self._insert_fill({**fill, "position_id": position_id})
            self._set_meta("cash", round(cash, 8))
        return True

    def close_with_fill(self, position_id: int, fields: dict, fill: dict, cash: float) -> bool:
        """Close an open position exactly once and credit its fill atomically."""
        with self.transaction():
            assignments = ", ".join(f"{k}=?" for k in fields)
            cur = self.conn.execute(
                f"UPDATE positions SET {assignments}, status='closed' "
                "WHERE id=? AND status='open'",
                (*fields.values(), position_id),
            )
            if cur.rowcount != 1:
                return False
            self._insert_fill({**fill, "position_id": position_id})
            self._set_meta("cash", round(cash, 8))
        return True

    def deactivate_all(self) -> None:
        self.conn.execute("UPDATE whales SET active=0 WHERE active=1")

    def claim_pending(self, token: str) -> dict | None:
        """Remove and return a resting order atomically, or None if it vanished."""
        with self.transaction():
            row = self.conn.execute(
                "SELECT * FROM pending_entries WHERE token=?", (token,)
            ).fetchone()
            if row is None:
                return None
            self.conn.execute("DELETE FROM pending_entries WHERE token=?", (token,))
            return dict(row)

    def _insert_fill(self, fill: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO fills (ts,position_id,side,px,qty,usd,fee_usd,kind,tx)
            VALUES (:ts,:position_id,:side,:px,:qty,:usd,:fee_usd,:kind,:tx)
            """,
            fill,
        )

    def _set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

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

    def last_closed(self, token: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM positions WHERE token=? AND status='closed' ORDER BY exit_ts DESC LIMIT 1",
            (token,),
        ).fetchone()
        return dict(row) if row else None

    # ── resting entries (retrace mode) ───────────────────────────────────────

    def upsert_pending(self, order: dict) -> None:
        order = {
            "signal_ts": 0,
            "signal_usd": 0.0,
            "signal_liq": 0.0,
            "conviction": 1,
            **order,
        }
        self.conn.execute(
            """
            INSERT INTO pending_entries
              (token,symbol,limit_px,expires_ts,whale,whale_score,created_ts,
               signal_ts,signal_usd,signal_liq,conviction)
            VALUES (:token,:symbol,:limit_px,:expires_ts,:whale,:whale_score,:created_ts,
                    :signal_ts,:signal_usd,:signal_liq,:conviction)
            ON CONFLICT(token) DO UPDATE SET
                symbol=excluded.symbol, limit_px=MIN(limit_px, excluded.limit_px),
                expires_ts=MAX(expires_ts, excluded.expires_ts),
                whale=excluded.whale, whale_score=excluded.whale_score,
                signal_ts=excluded.signal_ts, signal_usd=excluded.signal_usd,
                signal_liq=excluded.signal_liq, conviction=excluded.conviction
            """,
            order,
        )
        self.conn.commit()

    def pending_entries(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM pending_entries ORDER BY created_ts")
        return [dict(r) for r in rows]

    def pending_for_token(self, token: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM pending_entries WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None

    def remove_pending(self, token: str) -> None:
        self.conn.execute("DELETE FROM pending_entries WHERE token=?", (token,))
        self.conn.commit()

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

    def equity_bounds(self) -> dict:
        """Lifetime peak and maximum drawdown, without loading the curve."""
        row = self.conn.execute(
            """
            WITH curve AS (
                SELECT equity_usd, MAX(equity_usd) OVER (ORDER BY id) AS peak
                  FROM equity
            )
            SELECT COUNT(*) AS n,
                   MIN(equity_usd) AS low,
                   MAX(peak) AS high,
                   COALESCE(MAX(CASE WHEN peak > 0 THEN (peak - equity_usd) / peak END), 0.0) AS max_dd
              FROM curve
            """
        ).fetchone()
        return {
            "n": int(row["n"] or 0),
            "low": float(row["low"] or 0.0),
            "high": float(row["high"] or 0.0),
            "max_dd": float(row["max_dd"] or 0.0),
        }

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
        with self.transaction():
            self.conn.execute("DELETE FROM pending_entries")
            self.conn.execute("DELETE FROM positions")
            self.conn.execute("DELETE FROM fills")
            self.conn.execute("DELETE FROM equity")
            self.conn.execute("DELETE FROM signals")
            self.conn.execute(
                "DELETE FROM meta WHERE key IN "
                "('halt_day','halt_reason','halt_global','halt_global_reason')"
            )
            self._set_meta("cash", round(start_equity, 8))
            self._set_meta("paper_started", now_ts())

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
