"""回測比較：策略（用模擬交易同一套買賣規則逐日回放） vs 同期買進持有 0050。

- 策略：sim.replay() 從開始日逐日跑到最新交易日；期末持股以最後收盤價結算（扣手續費、證交稅）
- 0050：開始日開盤價用全部資金買零股，持有到最後一天收盤賣出（同樣扣費稅）
- 指標：總報酬、最大回撤、勝率（已結算交易）、交易次數、平均賺／賠、獲利因子、平均持股比例
"""

from __future__ import annotations

import copy
import math

from . import sim

BENCHMARK_ID = "0050"
BENCHMARK_NAME = "元大台灣50"


def max_drawdown(values: list[float]) -> float:
    peak, mdd = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, (v / peak - 1) * 100)
    return round(mdd, 2)


def _metrics(curve: list[dict], capital: float, final: float) -> dict:
    values = [p["total"] for p in curve] or [capital]
    return {
        "final": round(final, 2),
        "pnl": round(final - capital, 2),
        "return_pct": round((final / capital - 1) * 100, 2),
        "max_drawdown_pct": max_drawdown([capital, *values, final]),
    }


def trade_stats(trades: list[dict]) -> dict:
    sells = [t for t in trades if t.get("side") == "SELL"]
    wins = [t for t in sells if (t.get("pnl") or 0) > 0]
    losses = [t for t in sells if (t.get("pnl") or 0) <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t.get("pnl") or 0 for t in losses)
    return {
        "trades": len(sells),
        "win_rate": round(len(wins) / len(sells) * 100, 1) if sells else None,
        "avg_win_pct": round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "fees": round(sum(t.get("fee") or 0 for t in trades), 2),
    }


def benchmark(capital: float, dates: list[str], px) -> dict:
    """dates：策略回放的交易日；px：0050 日 K（date/open/close）。"""
    bars = {str(r["date"])[:10]: r for r in px.to_dict("records")}
    days = [d for d in dates if d in bars]
    if not days:
        return {"available": False}
    entry = float(bars[days[0]]["open"])
    shares = int(capital // (entry * (1 + sim.FEE_RATE)))
    while shares > 0 and sim.buy_cost(entry, shares)[0] > capital:
        shares -= 1
    cost, buy_fee = sim.buy_cost(entry, shares) if shares else (0.0, 0)
    cash = capital - cost
    curve = [{"date": d, "total": round(cash + float(bars[d]["close"]) * shares, 2)} for d in days]
    last_close = float(bars[days[-1]]["close"])
    proceeds, sell_costs = sim.sell_proceeds(BENCHMARK_ID, last_close, shares) if shares else (0.0, 0)
    final = cash + proceeds
    return {"available": True, "shares": shares, "entry_price": entry, "exit_price": last_close,
            "curve": curve, **_metrics(curve, capital, final),
            "trades": 1 if shares else 0, "win_rate": None, "fees": round(buy_fee + sell_costs, 2)}


def run(capital: float, start_date: str, strategy: dict, *, progress=None, cancel=None) -> dict | None:
    """跑完整比較。被取消回傳 None。"""
    from .data import get_price_history

    state = sim.replay(capital, start_date, strategy, progress=progress, cancel=cancel)
    if cancel is not None and cancel.is_set():
        return None
    if progress:
        progress(phase="計算 0050 對照", current=BENCHMARK_ID)

    final_state = sim.stop(copy.deepcopy(state))  # 期末持股以最後收盤價結算
    for t in final_state["trades"][len(state["trades"]):]:
        t["reason"] = "回測期末結算"
    curve = [{"date": p["date"], "total": p["total"]} for p in state["equity"]]
    final = final_state["cash"]
    days = [p["date"] for p in curve]
    exposure = [p["holdings"] / p["total"] * 100 for p in state["equity"] if p["total"] > 0]

    strat = {**_metrics(curve, capital, final), **trade_stats(final_state["trades"]),
             "avg_exposure_pct": round(sum(exposure) / len(exposure), 1) if exposure else 0.0, "curve": curve}
    try:
        bench = benchmark(capital, days, get_price_history(BENCHMARK_ID, 2))
    except Exception as exc:  # 0050 抓不到不影響策略結果
        bench = {"available": False, "error": str(exc)[:200]}

    return {
        "strategy": {"id": strategy.get("id", "default"), "name": strategy.get("name", "預設策略")},
        "capital": capital,
        "start_date": days[0] if days else start_date,
        "end_date": days[-1] if days else start_date,
        "trading_days": len(days),
        "result": strat,
        "benchmark": {"stock_id": BENCHMARK_ID, "name": BENCHMARK_NAME, **bench},
        "trades": final_state["trades"],
    }
