"""Web dashboard for the paper desk.

Zero framework, zero build step: a threaded stdlib HTTP server renders HTML from
SQLite and exposes one JSON endpoint for the live status strip. The engine runs in
the main asyncio loop and writes to the same WAL database; the web thread only reads.

Design constraints (from the operator's earlier deployments): no gradients, no
oversized greeting cards, no invented charts, no icon-only actions, no card per
scalar. Dense tables, tabular numbers, one accent, semantic colour paired with text.
"""

from __future__ import annotations

import base64
import hmac
import html
import json
import logging
import sqlite3
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Settings
from .util import fmt_px, fmt_secs, fmt_usd, now_ts, short_addr

log = logging.getLogger("elys.web")

PAGES = ("/", "/positions", "/signals", "/whales", "/alerts")
NAV = (
    ("/", "Overview"),
    ("/positions", "Positions"),
    ("/signals", "Signals"),
    ("/whales", "Watchlist"),
    ("/alerts", "Alerts"),
)

CSS = """
:root{
  color-scheme: light dark;
  --bg:#ffffff; --fg:#16181d; --muted:#6b7280; --line:#e3e5e9; --surface:#f7f8f9;
  --accent:#1d4ed8; --pos:#15803d; --neg:#b91c1c; --warn:#a16207;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#0e1013; --fg:#e6e8ec; --muted:#9aa1ab; --line:#242a31; --surface:#15191e;
         --accent:#7aa2f7; --pos:#4ade80; --neg:#f87171; --warn:#eab308; }
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--fg)}
body{font:14px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
     font-variant-numeric:tabular-nums}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px 64px}
header.top{border-bottom:1px solid var(--line)}
header.top .inner{max-width:1180px;margin:0 auto;padding:14px 20px 0;display:flex;
     align-items:baseline;justify-content:space-between;gap:16px;flex-wrap:wrap}
.brand{font-weight:600;letter-spacing:.01em}
.brand .paper{font-weight:500;color:var(--muted);margin-left:8px;font-size:12px;
     text-transform:uppercase;letter-spacing:.08em}
nav.tabs{display:flex;gap:18px;padding:10px 0 0}
nav.tabs a{padding:0 0 9px;color:var(--muted);font-size:13px;border-bottom:2px solid transparent}
nav.tabs a.on{color:var(--fg);border-bottom-color:var(--fg)}
nav.tabs a:hover{color:var(--fg);text-decoration:none}
.statusline{max-width:1180px;margin:0 auto;padding:8px 20px;color:var(--muted);font-size:12px;
     border-bottom:1px solid var(--line);display:flex;gap:14px;flex-wrap:wrap}
.statusline b{font-weight:500;color:var(--fg)}
h1{font-size:24px;font-weight:600;margin:26px 0 4px}
h2{font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;
   color:var(--muted);margin:30px 0 8px}
.sub{color:var(--muted);font-size:13px;margin:0 0 18px}
.figures{display:flex;border:1px solid var(--line);margin-top:18px;flex-wrap:wrap}
.fig{flex:1 1 150px;padding:11px 14px;border-right:1px solid var(--line)}
.fig:last-child{border-right:0}
.fig .k{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
.fig .v{font-size:19px;font-weight:600;margin-top:2px}
.fig .n{font-size:11px;color:var(--muted);margin-top:1px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
   color:var(--muted);padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr:hover td{background:var(--surface)}
.num{text-align:right;white-space:nowrap}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
.muted{color:var(--muted)}
.pos{color:var(--pos)}
.neg{color:var(--neg)}
.warn{color:var(--warn)}
.tag{display:inline-block;font-size:10px;letter-spacing:.06em;text-transform:uppercase;
     border:1px solid var(--line);border-radius:2px;padding:0 5px;color:var(--muted);line-height:16px}
.tag.pos{color:var(--pos);border-color:currentColor}
.tag.neg{color:var(--neg);border-color:currentColor}
.tag.warn{color:var(--warn);border-color:currentColor}
.empty{padding:14px 0;color:var(--muted);font-size:13px}
.kv{display:grid;grid-template-columns:190px 1fr;gap:4px 16px;font-size:13px;max-width:720px}
.kv .k{color:var(--muted)}
footer{border-top:1px solid var(--line);margin-top:36px;padding-top:12px;color:var(--muted);font-size:12px}
.note{font-size:12px;color:var(--muted);margin-top:8px}
.scroll{overflow-x:auto}
.alerts{list-style:none;margin:0;padding:0}
.alerts li{padding:10px 0;border-bottom:1px solid var(--line)}
.alerts .meta{font-size:11px;color:var(--muted);margin-bottom:3px}
.alerts .text{white-space:pre-wrap;font-size:13px;max-width:900px}
.alerts .links{margin-top:4px;font-size:12px;display:flex;gap:12px}
@media (max-width:760px){
  .figures{display:grid;grid-template-columns:1fr 1fr}
  .fig{border-bottom:1px solid var(--line)}
  h1{font-size:20px}
  .hide-sm{display:none}
  td,th{padding:6px 5px}
}
"""

JS = """
(function(){
  function money(n){ if(n==null) return "\\u2013";
    var a=Math.abs(n);
    if(a>=1e6) return (n<0?"-":"")+"$"+(a/1e6).toFixed(2)+"M";
    if(a>=1e3) return (n<0?"-":"")+"$"+(a/1e3).toFixed(1)+"K";
    return (n<0?"-":"")+"$"+a.toFixed(2); }
  function pct(n){ return n==null ? "\\u2013" : (n*100).toFixed(1)+"%"; }
  function esc(v){ return v==null ? "\\u2013" : String(v); }
  function fmt(key,v){
    if(key.indexOf("usd")>=0||key==="equity"||key==="cash") return money(v);
    if(key==="win_rate") return pct(v);
    if(key==="heartbeat_age") return v==null ? "\\u2013" : v+"s ago";
    if(key==="stream") return v ? "up" : "down";
    if(typeof v==="number" && Math.abs(v)>=1000) return v.toLocaleString();
    return esc(v);
  }
  function paint(){
    fetch("/api/summary",{cache:"no-store"}).then(function(r){return r.ok?r.json():null;})
    .then(function(s){
      if(!s) return;
      document.querySelectorAll("[data-live]").forEach(function(el){
        var k=el.getAttribute("data-live"); if(!(k in s)) return;
        el.textContent=fmt(k,s[k]);
        if(k==="stream") el.className = s[k] ? "pos" : "neg";
       });
      var u=document.getElementById("updated");
      if(u) u.textContent=new Date().toLocaleTimeString();
    }).catch(function(){});
  }
  paint(); setInterval(paint, 10000);
})();
"""


# ── data access ───────────────────────────────────────────────────────────────


class WebDB:
    """Read-only connections, one per request; WAL keeps readers non-blocking."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        return con

    def meta(self, con: sqlite3.Connection) -> dict[str, str]:
        return {row["key"]: row["value"] for row in con.execute("SELECT key,value FROM meta")}

    def summary(self, settings: Settings) -> dict:
        con = self.connect()
        try:
            meta = self.meta(con)
            cash = float(meta.get("cash") or settings.start_equity)
            opens = [dict(r) for r in con.execute("SELECT * FROM positions WHERE status='open'")]
            pos_usd = sum(
                float(p["qty"]) * float(p.get("last_px") or p["entry_px"]) for p in opens
            )
            agg = dict(
                con.execute(
                    "SELECT COUNT(*) n, COALESCE(SUM(pnl_usd),0) pnl, "
                    "COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END),0) wins "
                    "FROM positions WHERE status='closed'"
                ).fetchone()
            )
            whales = con.execute("SELECT COUNT(*) c FROM whales WHERE active=1").fetchone()["c"]
            sig = dict(
                con.execute(
                    "SELECT COUNT(*) n, "
                    "COALESCE(SUM(CASE WHEN decision='open' THEN 1 ELSE 0 END),0) opens, "
                    "COALESCE(SUM(CASE WHEN decision='skip' THEN 1 ELSE 0 END),0) skips "
                    "FROM signals"
                ).fetchone()
            )
            heartbeat = int(meta.get("heartbeat_ts") or 0)
            now = now_ts()
            closed = int(agg["n"] or 0)
            return {
                "equity": round(cash + pos_usd, 2),
                "cash": round(cash, 2),
                "positions_usd": round(pos_usd, 2),
                "open_count": len(opens),
                "realised": round(float(agg["pnl"] or 0), 2),
                "closed_count": closed,
                "wins": int(agg["wins"] or 0),
                "win_rate": round(int(agg["wins"] or 0) / closed, 4) if closed else None,
                "whales": int(whales or 0),
                "signals_total": int(sig["n"] or 0),
                "signals_open": int(sig["opens"] or 0),
                "signals_skip": int(sig["skips"] or 0),
                "stream": (meta.get("stream_connected") or "0") in ("1", "True", "true"),
                "heartbeat_age": (now - heartbeat) if heartbeat else None,
                "chain_head": int(meta.get("chain_head") or 0) or None,
                "chain_block_ms": int(meta.get("chain_block_ms") or 0) or None,
                "chain_hype_usd": float(meta.get("chain_hype_usd") or 0) or None,
                "paper_started": int(meta.get("paper_started") or 0) or None,
            }
        finally:
            con.close()


# ── formatting helpers ────────────────────────────────────────────────────────


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def pnl_class(value: float | None) -> str:
    if value is None:
        return "muted"
    return "pos" if value > 0 else ("neg" if value < 0 else "muted")


def when(ts: int | float | None) -> str:
    if not ts:
        return "–"
    return time.strftime("%H:%M:%S", time.localtime(int(ts)))


def ago(ts: int | float | None) -> str:
    if not ts:
        return "–"
    return fmt_secs(max(0, now_ts() - int(ts)))


def tag(decision: str | None) -> str:
    d = (decision or "pending").lower()
    cls = "pos" if d in ("open", "add") else ("neg" if d == "close" else "")
    return f'<span class="tag {cls}">{esc(d)}</span>'


def layout(title: str, body: str, *, active: str, settings: Settings) -> str:
    body = body.replace("<table>", '<div class="scroll"><table>').replace("</table>", "</table></div>")
    tabs = "".join(
        f'<a href="{href}" class="{"on" if href == active else ""}">{label}</a>'
        for href, label in NAV
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · Elysium Copy Desk</title>
<style>{CSS}</style>
</head><body>
<header class="top"><div class="inner">
  <div class="brand">Elysium Copy Desk<span class="paper">paper</span></div>
  <nav class="tabs">{tabs}</nav>
</div></header>
<div class="statusline">
  <span>Updated <b id="updated">–</b></span>
  <span>Stream <b data-live="stream">–</b></span>
  <span>Worker <b data-live="heartbeat_age">–</b></span>
  <span>Chain <b data-live="chain_head">–</b></span>
  <span class="hide-sm">HYPE <b data-live="chain_hype_usd">–</b></span>
  <span class="hide-sm">Equity <b data-live="equity">–</b></span>
</div>
<div class="wrap">
{body}
<footer>
  Paper execution only. Whale discovery, monitoring and fills use the public Elysium
  intel API and {esc(settings.tg_channel)} alerts. No keys, no signing, no order routing.
</footer>
</div>
<script>{JS}</script>
</body></html>"""


# ── pages ─────────────────────────────────────────────────────────────────────


def page_overview(db: WebDB, settings: Settings) -> str:
    con = db.connect()
    try:
        meta = db.meta(con)
        cash = float(meta.get("cash") or settings.start_equity)
        opens = [dict(r) for r in con.execute("SELECT * FROM positions WHERE status='open' ORDER BY id")]
        pos_usd = sum(float(p["qty"]) * float(p.get("last_px") or p["entry_px"]) for p in opens)
        agg = dict(
            con.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(pnl_usd),0) pnl, "
                "COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END),0) wins "
                "FROM positions WHERE status='closed'"
            ).fetchone()
        )
        closed_n = int(agg["n"] or 0)
        wins = int(agg["wins"] or 0)
        signals = [dict(r) for r in con.execute("SELECT * FROM signals ORDER BY id DESC LIMIT 20")]
        tiers = [dict(r) for r in con.execute(
            "SELECT COALESCE(tier,'?') tier, COUNT(*) c FROM whales WHERE active=1 GROUP BY tier ORDER BY c DESC"
        )]
        whales = [dict(r) for r in con.execute(
            "SELECT * FROM whales WHERE active=1 ORDER BY score DESC LIMIT 5"
        )]
    finally:
        con.close()

    equity = cash + pos_usd
    figs = f"""
<section class="figures">
  <div class="fig"><div class="k">Paper equity</div><div class="v" data-live="equity">{esc(fmt_usd(equity))}</div>
    <div class="n">cash {esc(fmt_usd(cash))}</div></div>
  <div class="fig"><div class="k">Realised</div>
    <div class="v {pnl_class(float(agg["pnl"] or 0))}">{esc(fmt_usd(float(agg["pnl"] or 0)))}</div>
    <div class="n">{closed_n} closed</div></div>
  <div class="fig"><div class="k">Win rate</div>
    <div class="v">{(f"{wins / closed_n * 100:.1f}%" if closed_n else "–")}</div>
    <div class="n">{wins}W / {max(0, closed_n - wins)}L</div></div>
  <div class="fig"><div class="k">Open positions</div><div class="v">{len(opens)}</div>
    <div class="n">{esc(fmt_usd(pos_usd))} exposure</div></div>
  <div class="fig"><div class="k">Watchlist</div><div class="v">{sum(t["c"] for t in tiers)}</div>
    <div class="n">{" · ".join(f"{esc(t['tier'])} {t['c']}" for t in tiers) or "–"}</div></div>
</section>"""

    if opens:
        rows = "".join(
            f"<tr>"
            f"<td>{esc(p.get('symbol') or short_addr(p['token']))}</td>"
            f"<td class='num'>{esc(fmt_px(p['entry_px']))}</td>"
            f"<td class='num'>{esc(fmt_px(p.get('last_px') or p['entry_px']))}</td>"
            f"<td class='num {pnl_class((float(p.get('last_px') or p['entry_px']) / float(p['entry_px']) - 1) if p['entry_px'] else 0)}'>"
            f"{((float(p.get('last_px') or p['entry_px']) / float(p['entry_px']) - 1) * 100):+.1f}%</td>"
            f"<td class='num'>{esc(fmt_usd(p['usd']))}</td>"
            f"<td class='num {pnl_class(float(p['qty']) * float(p.get('last_px') or p['entry_px']) - float(p['usd']))}'>"
            f"{esc(fmt_usd(float(p['qty']) * float(p.get('last_px') or p['entry_px']) - float(p['usd'])))}</td>"
            f"<td class='num muted'>{esc(ago(p['entry_ts']))}</td>"
            f"<td class='mono hide-sm'>{esc(short_addr(p.get('source_wallet') or ''))}</td>"
            f"</tr>"
            for p in opens
        )
        positions_html = (
            "<table><thead><tr><th>Token</th><th class='num'>Entry</th><th class='num'>Mark</th>"
            "<th class='num'>Change</th><th class='num'>Cost</th><th class='num'>Unrealised</th>"
            "<th class='num'>Age</th><th class='hide-sm'>Copied from</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    else:
        positions_html = '<div class="empty">No open positions.</div>'

    if signals:
        rows = "".join(
            f"<tr>"
            f"<td class='muted'>{esc(when(s['ts']))}</td>"
            f"<td class='mono'>{esc(short_addr(s['wallet']))}</td>"
            f"<td>{esc(s.get('symbol') or short_addr(s['token']))}</td>"
            f"<td>{esc((s.get('side') or '').upper())}</td>"
            f"<td class='num'>{esc(fmt_usd(s.get('usd')))}</td>"
            f"<td class='num muted'>{esc(fmt_px(s.get('px')))}</td>"
            f"<td>{tag(s.get('decision'))}</td>"
            f"<td class='muted'>{esc(s.get('reason') or '')}</td>"
            f"<td class='num muted hide-sm'>{int((s.get('latency_ms') or 0) / 1000)}s</td>"
            f"</tr>"
            for s in signals
        )
        signals_html = (
            "<table><thead><tr><th>Time</th><th>Whale</th><th>Token</th><th>Side</th>"
            "<th class='num'>Whale size</th><th class='num'>Price</th><th>Decision</th>"
            "<th>Reason</th><th class='num hide-sm'>Latency</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    else:
        signals_html = '<div class="empty">No signals yet. The desk opens positions when a watched wallet buys above the floor.</div>'

    if whales:
        rows = "".join(
            f"<tr><td class='mono'>{esc(short_addr(w['address']))}</td>"
            f"<td>{esc(w.get('tier'))}</td>"
            f"<td class='num'>{float(w.get('score') or 0):.1f}</td>"
            f"<td class='num'>{((w.get('win_rate') or 0) * 100):.1f}%</td>"
            f"<td class='num'>{esc(fmt_usd(w.get('realized')))}</td></tr>"
            for w in whales
        )
        watch_html = (
            "<table><thead><tr><th>Wallet</th><th>Tier</th><th class='num'>Score</th>"
            "<th class='num'>Win rate</th><th class='num'>Realised</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    else:
        watch_html = '<div class="empty">No watchlist yet. Run discovery.</div>'

    body = f"""
<h1>Overview</h1>
<p class="sub">Paper copy desk. Positions open only when a watched wallet buys above
{esc(fmt_usd(settings.min_whale_usd))} and the token passes the safety gates.</p>
{figs}
<h2>Open positions</h2>
{positions_html}
<h2>Latest signals</h2>
{signals_html}
<h2>Watchlist</h2>
{watch_html}
<p class="note">Re-discovery every {settings.rebalance_hours:g}h ·
per trade {esc(fmt_usd(settings.per_trade_usd))} ·
stop {settings.stop_loss_pct * 100:.0f}% · target {settings.take_profit_pct * 100:.0f}% ·
trail {settings.trail_arm_pct * 100:.0f}%/{settings.trail_giveback_pct * 100:.0f}% ·
max hold {settings.max_hold_min}m</p>
"""
    return layout("Overview", body, active="/", settings=settings)


def page_positions(db: WebDB, settings: Settings) -> str:
    con = db.connect()
    try:
        opens = [dict(r) for r in con.execute("SELECT * FROM positions WHERE status='open' ORDER BY id")]
        closed = [dict(r) for r in con.execute(
            "SELECT * FROM positions WHERE status='closed' ORDER BY exit_ts DESC LIMIT 200"
        )]
    finally:
        con.close()

    def block(rows: list[dict], closed_view: bool) -> str:
        if not rows:
            return '<div class="empty">None.</div>'
        head = (
            "<tr><th>Token</th><th class='num'>Entry</th><th class='num'>"
            + ("Exit" if closed_view else "Mark")
            + "</th><th class='num'>PnL</th><th class='num'>PnL%</th><th class='num'>Cost</th>"
            + ("<th>Exit reason</th>" if closed_view else "")
            + "<th class='num'>Opened</th></tr>"
        )
        out = []
        for p in rows:
            mark = float(p.get("exit_px") or p.get("last_px") or p["entry_px"])
            pnl = float(p["pnl_usd"]) if p.get("pnl_usd") is not None else (
                float(p["qty"]) * mark - float(p["usd"])
            )
            pnl_pct = float(p["pnl_pct"]) if p.get("pnl_pct") is not None else (
                (pnl / float(p["usd"])) if float(p["usd"]) else 0.0
            )
            reason = f"<td class='muted'>{esc(p.get('exit_reason') or '')}</td>" if closed_view else ""
            out.append(
                f"<tr><td>{esc(p.get('symbol') or short_addr(p['token']))}</td>"
                f"<td class='num'>{esc(fmt_px(p['entry_px']))}</td>"
                f"<td class='num'>{esc(fmt_px(mark))}</td>"
                f"<td class='num {pnl_class(pnl)}'>{esc(fmt_usd(pnl))}</td>"
                f"<td class='num {pnl_class(pnl)}'>{pnl_pct * 100:+.1f}%</td>"
                f"<td class='num'>{esc(fmt_usd(p['usd']))}</td>"
                f"{reason}"
                f"<td class='num muted'>{esc(when(p.get('entry_ts')))}</td></tr>"
            )
        return f"<table><thead>{head}</thead><tbody>{''.join(out)}</tbody></table>"

    body = f"""
<h1>Positions</h1>
<p class="sub">Paper fills at the observed pool price plus slippage and fees.</p>
<h2>Open</h2>
{block(opens, False)}
<h2>Closed</h2>
{block(closed, True)}
"""
    return layout("Positions", body, active="/positions", settings=settings)


def page_signals(db: WebDB, settings: Settings) -> str:
    con = db.connect()
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM signals ORDER BY id DESC LIMIT 300")]
        reasons = [dict(r) for r in con.execute(
            "SELECT reason, COUNT(*) c FROM signals WHERE decision='skip' "
            "GROUP BY reason ORDER BY c DESC LIMIT 10"
        )]
    finally:
        con.close()

    if rows:
        body_rows = "".join(
            f"<tr><td class='muted'>{esc(when(s['ts']))}</td>"
            f"<td class='mono'>{esc(short_addr(s['wallet']))}</td>"
            f"<td>{esc(s.get('symbol') or short_addr(s['token']))}</td>"
            f"<td>{esc((s.get('side') or '').upper())}</td>"
            f"<td class='num'>{esc(fmt_usd(s.get('usd')))}</td>"
            f"<td class='num muted'>{esc(fmt_px(s.get('px')))}</td>"
            f"<td>{tag(s.get('decision'))}</td>"
            f"<td class='muted'>{esc(s.get('reason') or '')}</td>"
            f"<td class='num muted'>{int((s.get('latency_ms') or 0) / 1000)}s</td></tr>"
            for s in rows
        )
        table = (
            "<table><thead><tr><th>Time</th><th>Whale</th><th>Token</th><th>Side</th>"
            "<th class='num'>Whale size</th><th class='num'>Price</th><th>Decision</th>"
            "<th>Reason</th><th class='num'>Latency</th></tr></thead>"
            f"<tbody>{body_rows}</tbody></table>"
        )
    else:
        table = '<div class="empty">No signals recorded.</div>'

    skip = "".join(
        f"<tr><td>{esc(r['reason'] or '–')}</td><td class='num'>{r['c']}</td></tr>" for r in reasons
    )
    skip_html = (
        "<table><thead><tr><th>Skip reason</th><th class='num'>Count</th></tr></thead>"
        f"<tbody>{skip}</tbody></table>"
        if reasons
        else '<div class="empty">No skipped signals.</div>'
    )

    body = f"""
<h1>Signals</h1>
<p class="sub">Every detected whale swap, whether copied or refused. Latency is the gap
between the trade timestamp and detection.</p>
<h2>Recent</h2>
{table}
<h2>Why signals were skipped</h2>
{skip_html}
"""
    return layout("Signals", body, active="/signals", settings=settings)


def page_whales(db: WebDB, settings: Settings) -> str:
    con = db.connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM whales WHERE active=1 ORDER BY score DESC LIMIT 300"
        )]
    finally:
        con.close()

    if rows:
        body_rows = []
        for i, w in enumerate(rows, 1):
            wr = w.get("win_rate")
            body_rows.append(
                f"<tr><td class='num muted'>{i}</td>"
                f"<td class='mono'>{esc(w['address'])}</td>"
                f"<td>{esc(w.get('tier'))}</td>"
                f"<td class='num'>{float(w.get('score') or 0):.1f}</td>"
                f"<td class='num'>{(wr * 100 if wr is not None else 0):.1f}%</td>"
                f"<td class='num'>{int(w.get('trades') or 0)}</td>"
                f"<td class='num'>{int(w.get('tokens') or 0)}</td>"
                f"<td class='num'>{esc(fmt_usd(w.get('realized')))}</td>"
                f"<td class='num muted'>{esc(ago(w.get('last_ts')))}</td></tr>"
            )
        table = (
            "<table><thead><tr><th class='num'>#</th><th>Wallet</th><th>Tier</th>"
            "<th class='num'>Score</th><th class='num'>Win rate</th><th class='num'>Trades</th>"
            "<th class='num'>Tokens</th><th class='num'>Realised</th>"
            "<th class='num'>Last trade</th></tr></thead>"
            f"<tbody>{''.join(body_rows)}</tbody></table>"
        )
    else:
        table = '<div class="empty">No active wallets.</div>'

    body = f"""
<h1>Watchlist</h1>
<p class="sub">Wallets that passed the copyability screen: at least
{settings.discover_min_trades} trades, {settings.discover_min_tokens} tokens,
{esc(fmt_usd(settings.discover_min_realized))} realised, shrunk win rate ≥
{settings.discover_min_wr:.2f}, a trade in the last hour, and a recent buy of at least
{esc(fmt_usd(settings.min_whale_usd))}.</p>
{table}
<p class="note">Refreshed by discovery every {settings.rebalance_hours:g}h.</p>
"""
    return layout("Watchlist", body, active="/whales", settings=settings)


def page_alerts(db: WebDB, settings: Settings) -> str:
    con = db.connect()
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM tg_posts ORDER BY id DESC LIMIT 100")]
    finally:
        con.close()

    if rows:
        items = []
        for p in rows:
            links = []
            if p.get("token"):
                links.append(f'<a href="https://elysium.eco/screener/{esc(p["token"])}">Token</a>')
            if p.get("wallets"):
                first = str(p["wallets"]).split(",")[0]
                links.append(f'<a href="https://elysium.eco/screener/w/{esc(first)}">Wallet</a>')
            if p.get("tx"):
                links.append(f'<a href="https://elysium.eco/tx/{esc(p["tx"])}">Transaction</a>')
            kind = (p.get("kind") or "other").replace("_", " ")
            cls = "neg" if p.get("kind") == "rug_warning" else ("pos" if p.get("kind") in ("whale", "smart_money") else "")
            items.append(
                f'<li><div class="meta">{esc(when(p.get("ts")))} · '
                f'<span class="tag {cls}">{esc(kind)}</span></div>'
                f'<div class="text">{esc((p.get("text") or "")[:600])}</div>'
                f'<div class="links">{"".join(links)}</div></li>'
            )
        body_html = f'<ul class="alerts">{"".join(items)}</ul>'
    else:
        body_html = '<div class="empty">No alerts stored. The public channel is polled while the desk runs.</div>'

    body = f"""
<h1>Alerts</h1>
<p class="sub">{esc(settings.tg_channel)} — the public alert channel. Rug warnings close
a copied position immediately; the rest is context for the watchlist.</p>
{body_html}
"""
    return layout("Alerts", body, active="/alerts", settings=settings)


RENDERERS = {
    "/": page_overview,
    "/positions": page_positions,
    "/signals": page_signals,
    "/whales": page_whales,
    "/alerts": page_alerts,
}


# ── HTTP ──────────────────────────────────────────────────────────────────────


class HtmlHandler(BaseHTTPRequestHandler):
    server_version = "elysium/0.1"
    protocol_version = "HTTP/1.1"

    db: WebDB
    settings: Settings

    def log_message(self, fmt: str, *args) -> None:  # quieter than stdlib default
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _authorised(self) -> bool:
        if not self.settings.web_password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header[6:]).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return False
        user, _, password = raw.partition(":")
        return hmac.compare_digest(user, self.settings.web_user) and hmac.compare_digest(
            password, self.settings.web_password
        )

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.end_headers()

    def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self._headers(status, content_type, len(data))
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/healthz":
            summary = self.db.summary(self.settings)
            stale = summary["heartbeat_age"] is None or summary["heartbeat_age"] > 120
            self._send(
                200 if not stale else 503,
                json.dumps({"status": "stale" if stale else "ok", **summary}),
                "application/json",
            )
            return
        if not self._authorised():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Elysium Copy Desk", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/summary":
            self._send(200, json.dumps(self.db.summary(self.settings)), "application/json")
            return
        renderer = RENDERERS.get(path)
        if renderer is None:
            self._send(404, layout("Not found", "<h1>Not found</h1>", active="", settings=self.settings))
            return
        try:
            self._send(200, renderer(self.db, self.settings))
        except FileNotFoundError:
            self._send(503, "<h1>Database not initialised</h1>")


def run_http_server(settings: Settings, host: str = "127.0.0.1", port: int = 18086) -> None:
    HtmlHandler.db = WebDB(settings.db_path)
    HtmlHandler.settings = settings
    server = ThreadingHTTPServer((host, port), HtmlHandler)
    server.daemon_threads = True
    log.info("dashboard on http://%s:%d", host, port)
    server.serve_forever()
