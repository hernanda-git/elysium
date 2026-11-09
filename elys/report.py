"""Performance report from the paper ledger."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Settings
from .store import Store
from .util import fmt_secs, fmt_usd


@dataclass(slots=True)
class Report:
    closed: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    pnl: float = 0.0
    fees: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float | None = None
    expectancy: float = 0.0
    avg_hold_s: float = 0.0
    best: dict | None = None
    worst: dict | None = None
    open_count: int = 0
    open_cost: float = 0.0
    open_value: float = 0.0
    open_pnl: float = 0.0
    cash: float = 0.0
    equity: float = 0.0
    start_equity: float = 0.0
    peak_equity: float = 0.0
    max_drawdown: float = 0.0
    signals_total: int = 0
    signals_by_decision: dict[str, int] = field(default_factory=dict)
    skip_reasons: list[tuple[str, int]] = field(default_factory=list)
    whales_by_tier: dict[str, int] = field(default_factory=dict)
    per_whale: list[dict] = field(default_factory=list)
    exit_reasons: list[tuple[str, int]] = field(default_factory=list)


def build_report(store: Store, settings: Settings) -> Report:
    report = Report(start_equity=settings.start_equity)

    closed = store.query("SELECT * FROM positions WHERE status='closed' ORDER BY exit_ts")
    pnls = [float(p["pnl_usd"] or 0.0) for p in closed]
    report.closed = len(closed)
    report.pnl = sum(pnls)
    report.wins = sum(1 for p in pnls if p > 0)
    report.losses = sum(1 for p in pnls if p <= 0)
    report.win_rate = report.wins / report.closed if report.closed else 0.0
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    report.avg_win = sum(wins) / len(wins) if wins else 0.0
    report.avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_win, gross_loss = sum(wins), sum(losses)
    report.profit_factor = gross_win / gross_loss if gross_loss > 0 else None
    report.expectancy = report.pnl / report.closed if report.closed else 0.0
    if closed:
        report.best = max(closed, key=lambda p: float(p["pnl_usd"] or 0.0))
        report.worst = min(closed, key=lambda p: float(p["pnl_usd"] or 0.0))
        holds = [
            int(p["exit_ts"] or 0) - int(p["entry_ts"] or 0)
            for p in closed
            if p.get("exit_ts") and p.get("entry_ts")
        ]
        report.avg_hold_s = sum(holds) / len(holds) if holds else 0.0

    fees = store.query("SELECT COALESCE(SUM(fee_usd),0) f FROM fills")
    report.fees = float(fees[0]["f"] or 0.0) if fees else 0.0

    open_positions = store.load_open_positions()
    report.open_count = len(open_positions)
    report.open_cost = sum(float(p["usd"]) for p in open_positions)
    report.open_value = sum(float(p["qty"]) * float(p.get("last_px") or p["entry_px"]) for p in open_positions)
    report.open_pnl = report.open_value - report.open_cost

    report.cash = float(store.cash(settings.start_equity))
    report.equity = report.cash + report.open_value

    curve = store.equity_curve(limit=20_000)
    peak = report.start_equity
    max_dd = 0.0
    for point in curve:
        value = float(point["equity_usd"])
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    report.peak_equity = peak
    report.max_drawdown = max_dd

    signals = store.query("SELECT decision, reason FROM signals")
    report.signals_total = len(signals)
    for row in signals:
        decision = row["decision"] or "pending"
        report.signals_by_decision[decision] = report.signals_by_decision.get(decision, 0) + 1
    reasons: dict[str, int] = {}
    for row in signals:
        if (row["decision"] or "") != "skip" or not row["reason"]:
            continue
        key = (row["reason"].split(":")[0])[:48]
        reasons[key] = reasons.get(key, 0) + 1
    report.skip_reasons = sorted(reasons.items(), key=lambda kv: kv[1], reverse=True)[:8]

    for row in store.query("SELECT tier, COUNT(*) c FROM whales WHERE active=1 GROUP BY tier"):
        report.whales_by_tier[row["tier"] or "?"] = int(row["c"])

    report.exit_reasons = [
        (row["exit_reason"] or "?", int(row["c"]))
        for row in store.query(
            "SELECT exit_reason, COUNT(*) c FROM positions WHERE status='closed' "
            "GROUP BY exit_reason ORDER BY c DESC LIMIT 8"
        )
    ]

    report.per_whale = [
        {
            "wallet": row["source_wallet"],
            "trades": int(row["n"]),
            "pnl": float(row["pnl"] or 0.0),
            "wins": int(row["wins"] or 0),
        }
        for row in store.query(
            """
            SELECT source_wallet AS source_wallet, COUNT(*) n, SUM(pnl_usd) pnl,
                   SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins
              FROM positions
             WHERE status='closed' AND source_wallet IS NOT NULL
             GROUP BY source_wallet
             ORDER BY pnl DESC
             LIMIT 15
            """
        )
    ]
    return report


def render(report: Report) -> str:
    lines: list[str] = []
    add = lines.append

    add("ELYSIUM PAPER DESK")
    add("=" * 62)
    total_pct = (report.equity / report.start_equity - 1) if report.start_equity else 0.0
    add(
        f"equity {fmt_usd(report.equity)}  ({total_pct:+.2%})   "
        f"cash {fmt_usd(report.cash)}   open {report.open_count} ({fmt_usd(report.open_value)})"
    )
    add(f"peak {fmt_usd(report.peak_equity)}   max drawdown {report.max_drawdown:.2%}   fees {fmt_usd(report.fees)}")
    add("")
    add("CLOSED TRADES")
    add(f"  n={report.closed}  win rate {report.win_rate:.1%}  ({report.wins}W/{report.losses}L)")
    add(
        f"  realised {fmt_usd(report.pnl)}   expectancy {fmt_usd(report.expectancy)}/trade   "
        f"profit factor {('%.2f' % report.profit_factor) if report.profit_factor else '–'}"
    )
    add(
        f"  avg win {fmt_usd(report.avg_win)}   avg loss {fmt_usd(-report.avg_loss)}   "
        f"avg hold {fmt_secs(report.avg_hold_s)}"
    )
    if report.best:
        add(f"  best  {report.best.get('symbol') or report.best['token'][:10]}  {fmt_usd(float(report.best['pnl_usd'] or 0))}")
    if report.worst:
        add(f"  worst {report.worst.get('symbol') or report.worst['token'][:10]}  {fmt_usd(float(report.worst['pnl_usd'] or 0))}")
    if report.exit_reasons:
        add("  exits: " + "  ".join(f"{name} {count}" for name, count in report.exit_reasons))
    add("")
    add("OPEN POSITIONS")
    if report.open_count == 0:
        add("  none")
    else:
        add(f"  cost {fmt_usd(report.open_cost)}   mark {fmt_usd(report.open_value)}   unrealised {fmt_usd(report.open_pnl)}")
    add("")
    add("SIGNALS")
    add(f"  seen {report.signals_total}   " + "  ".join(f"{k}={v}" for k, v in sorted(report.signals_by_decision.items())))
    for reason, count in report.skip_reasons:
        add(f"  skip {count:>4}  {reason}")
    add("")
    add("WATCHLIST")
    add("  " + "  ".join(f"{tier}={count}" for tier, count in sorted(report.whales_by_tier.items())))
    if report.per_whale:
        add("")
        add("ATTRIBUTION (closed)")
        for row in report.per_whale:
            wr = row["wins"] / row["trades"] if row["trades"] else 0.0
            add(
                f"  {row['wallet']}  n={row['trades']:>3}  wr {wr:>5.0%}  pnl {fmt_usd(row['pnl'])}"
            )
    return "\n".join(lines)
