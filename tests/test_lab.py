"""策略實驗室：樣本外驗證不能偷看未來；資料處理（含息還原、as-of 財報、成本）要正確。"""
import json

import numpy as np
import pandas as pd
import pytest

from stock_strategies import lab


def _px(n=1500, seed=1, drift=0.0004, vol=0.018, end=None):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    o = c * (1 + rng.normal(0, 0.005, n))
    dates = pd.bdate_range(end=end, periods=n) if end is not None else pd.bdate_range("2019-01-01", periods=n)
    return pd.DataFrame({"date": dates.strftime("%Y-%m-%d"), "open": o, "high": np.maximum(o, c) * 1.01,
                         "low": np.minimum(o, c) * 0.99, "close": c,
                         "volume": rng.integers(1_000_000, 5_000_000, n).astype(float)})


def _data(n=1500, stocks=6):
    """合成市場：幾檔股票＋0050＋大盤，附月營收與歷年財報。"""
    px = {str(2300 + i): _px(n, i) for i in range(stocks)}
    px["0050"] = _px(n, 500, 0.0005, 0.011)
    idx = _px(n, 999, 0.0004, 0.01)
    rng = np.random.default_rng(7)
    months = pd.period_range("2017-01", periods=n // 21 + 30, freq="M")
    avail = [(m.to_timestamp(how="end").normalize() + pd.Timedelta(days=10)).strftime("%Y-%m-%d") for m in months]
    revenue, funds = {}, {}
    for sid in px:
        if sid.startswith("00"):
            continue
        rev = pd.Series(100 * np.exp(np.cumsum(rng.normal(0.01, 0.08, len(months)))))
        revenue[sid] = {"avail": avail, "yoy": [float(v) for v in rev / rev.shift(12) - 1],
                        "accel": [float(v) for v in rev.rolling(3).mean() / rev.rolling(12).mean()]}
        funds[sid] = {"eps": {str(y): 5.0 for y in range(2016, 2027)}, "roe": {str(y): 20.0 for y in range(2016, 2027)}}
    return lab.Data(px, pd.Series(idx["close"].to_numpy(), index=idx["date"]), {s: s for s in px}, revenue, funds)


def test_adjust_removes_dividend_and_split_gaps():
    days = pd.bdate_range("2026-01-05", periods=6).strftime("%Y-%m-%d")
    close = [100.0, 100.0, 95.0, 95.0, 23.75, 23.75]  # 第 3 天除息 5 元，第 5 天 1 拆 4
    px = pd.DataFrame({"date": days, "open": close, "high": close, "low": close, "close": close, "volume": 1.0})
    out, n = lab.adjust(px, [(days[2], 0.95)])
    assert n == 2
    assert out["close"].pct_change().abs().max() < 1e-9  # 還原後沒有任何一天出現假的漲跌
    assert out["close"].iloc[-1] == 23.75                # 最新價格不變


def test_fund_asof_only_uses_published_annual_reports():
    hist = {"eps": {str(y): float(y) for y in range(2018, 2026)}, "roe": {"2024": 20.0}}
    assert list(lab.fund_asof(hist, "2025-03-31")["eps"]) == [2021, 2022, 2023]  # 2024 年報還沒到期限
    assert list(lab.fund_asof(hist, "2025-04-01")["eps"]) == [2022, 2023, 2024]
    assert lab.fund_asof(hist, "2025-04-01")["roe"] == {2024: 20.0}
    assert lab.fund_asof(None, "2025-04-01") == {"eps": {}, "roe": {}}


def test_annual_history_keeps_only_complete_years(monkeypatch):
    from stock_strategies import cache, data as data_mod

    def fetch(dataset, sid, start, **kw):
        if dataset == "TaiwanStockFinancialStatements":
            rows = []
            for year, quarters in ((2023, 4), (2024, 4), (2025, 2)):  # 2025 只公布了兩季
                for q in ("03-31", "06-30", "09-30", "12-31")[:quarters]:
                    rows += [{"date": f"{year}-{q}", "type": "EPS", "value": 2.5},
                             {"date": f"{year}-{q}", "type": "TotalConsolidatedProfitForThePeriod", "value": 100.0}]
            return pd.DataFrame(rows)
        return pd.DataFrame([{"date": f"{y}-12-31", "type": "EquityAttributableToOwnersOfParent", "value": 2000.0}
                             for y in (2023, 2024)])

    monkeypatch.setattr(cache, "fetch_finmind_cached", fetch)
    monkeypatch.setattr(data_mod, "fetch_finmind_cached", fetch)
    assert lab.annual_history("2330") == {"eps": {"2023": 10.0, "2024": 10.0}, "roe": {"2023": 20.0, "2024": 20.0}}


def test_folds_are_half_years_and_short_tail_is_merged():
    days = list(pd.bdate_range("2024-01-01", "2025-07-31").strftime("%Y-%m-%d"))
    parts = lab.folds(days, "2024-01-01")
    assert [p[0][:7] for p in parts] == ["2024-01", "2024-07", "2025-01"]  # 2025-07 只有 23 天，併進前一段
    assert parts[-1][-1] == days[-1] and sum(len(p) for p in parts) == len(days)


def test_walk_forward_picks_parameters_from_the_past_only():
    days = list(pd.bdate_range("2020-01-01", periods=900).strftime("%Y-%m-%d"))
    noise = np.tile([0.0005, -0.0005], 450)
    a = pd.Series(np.r_[np.full(600, 0.002), np.full(300, -0.002)] + noise, index=days)   # 以前很好、之後很差
    b = pd.Series(np.r_[np.full(600, -0.001), np.full(300, 0.003)] + noise, index=days)   # 以前很差、之後很好
    fold_days = [days[600:750], days[750:]]
    ret, chosen = lab.walk_forward({"A": a, "B": b}, fold_days, days)
    assert chosen == ["A", "A"]  # 只看得到過去：一定選 A，即使它在樣本外很差
    assert ret.tolist() == a.iloc[600:].tolist() and float(ret.sum()) < 0
    ret, chosen = lab.walk_forward({"A": a, "B": b}, fold_days, days, train=250)  # 訓練期短：第二段看得到 A 變差了
    assert chosen == ["A", "B"]
    assert ret.iloc[150] == pytest.approx(b.iloc[750] - lab.SWITCH_COST)  # 換參數多扣一次來回成本
    assert ret.iloc[151:].tolist() == b.iloc[751:].tolist()


def test_simulate_trades_next_open_and_charges_costs():
    days = list(pd.bdate_range("2026-01-05", periods=5).strftime("%Y-%m-%d"))
    flat = pd.DataFrame({"date": days, "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0})
    data = lab.Data({"2330": flat, "0050": flat.copy()}, pd.Series(100.0, index=days), {})
    res = lab.simulate(data, {days[0]: ["2330"], days[2]: []}, days[0], days[-1], slots=1, capital=100_000)
    assert res["exposure"].iloc[0] == 0 and res["exposure"].iloc[1] > 0.99  # 第 1 天收盤決定，第 2 天開盤才買
    assert res["exposure"].iloc[2] > 0.99 and res["exposure"].iloc[3] == 0   # 第 3 天決定賣，第 4 天開盤賣
    final = float((1 + res["ret"]).prod()) * 100_000
    assert final == pytest.approx(100_000 * (1 - 0.00585), rel=2e-4)         # 價格沒動，只虧手續費×2＋證交稅


def test_verdict_rules():
    base = {"folds": [{}] * 10}
    assert lab._verdict({**base, "excess_cagr_pct": 5, "t_stat": 3.2, "win_folds": 7}) == "strong"
    assert lab._verdict({**base, "excess_cagr_pct": 5, "t_stat": 2.4, "win_folds": 6}) == "tentative"
    assert lab._verdict({**base, "excess_cagr_pct": 5, "t_stat": 1.9, "win_folds": 9}) == "none"   # 不顯著
    assert lab._verdict({**base, "excess_cagr_pct": -1, "t_stat": 3.5, "win_folds": 9}) == "none"  # 沒贏
    assert lab._verdict({**base, "excess_cagr_pct": 5, "t_stat": 3.5, "win_folds": 5}) == "none"   # 只贏一半的區段


def test_report_end_to_end_on_synthetic_market(monkeypatch):
    from stock_strategies import webstore

    saved = {}
    monkeypatch.setattr(webstore, "save", lambda name, obj: saved.update({name: obj}))
    report = lab.run(_data(), eval_floor="2000-01-01")
    assert saved[lab.REPORT] is report and json.dumps(report)  # 純 JSON，存得進 webstore
    assert [c["id"] for c in report["candidates"]] == ["v32", "v32_nobt", "revenue", "trend", "timing"]
    n_folds = len(report["benchmark"]["folds"])
    assert report["trials"] == 15 and n_folds >= 2
    for c in report["candidates"]:
        assert c["verdict"] in ("strong", "tentative", "none") and len(c["folds"]) == n_folds
        assert c["curve"][0][0] >= report["period"]["start"] and 0 <= c["exposure_pct"] <= 100
    assert any(c["trades_per_year"] > 0 for c in report["candidates"])
    timing = next(c for c in report["candidates"] if c["id"] == "timing")
    assert all("config" in f for f in timing["folds"]) and timing["current"].endswith("日均線")
    assert report["passed"] == [c["id"] for c in report["candidates"] if c["verdict"] != "none"]


def test_load_data_adjusts_prices_and_keeps_fund_history(monkeypatch):
    """抓資料：0050 一定納入、有除權息的還原、月營收抓不到不影響其他；歷年財報存起來下次不用再抓。"""
    from stock_strategies import cache, data as data_mod, datasources, webstore

    raw = _px(600, end=pd.Timestamp.now().normalize())
    as_api = raw.assign(date=pd.to_datetime(raw["date"]))
    asked = []

    def fetch(dataset, sid, start, **kw):
        if dataset == "TaiwanStockDividendResult" and sid == "0050":
            return pd.DataFrame({"date": [raw["date"].iloc[300]], "before_price": [100.0], "after_price": [96.0]})
        return pd.DataFrame()

    def history(sid):
        asked.append(sid)
        return {"eps": {"2024": 5.0}, "roe": {"2024": 20.0}}

    monkeypatch.setattr(lab, "_quota_left", lambda: None)
    monkeypatch.setattr(data_mod, "get_price_history", lambda sid, years: as_api)
    monkeypatch.setattr(datasources, "get_index_history", lambda *a, **k: as_api[["date", "close"]])
    monkeypatch.setattr(cache, "fetch_finmind_cached", fetch)
    monkeypatch.setattr(datasources, "fetch_finmind_cached", fetch)
    monkeypatch.setattr(lab, "annual_history", history)
    watch = [{"stock_id": "2330", "name": "台積電"}]
    data = lab.load_data(years=2, watch=watch)
    assert set(data.px) == {"2330", "0050"} and data.stocks == ["2330"] and data.dividend_events == 1
    assert data.px["0050"]["close"].iloc[0] == pytest.approx(raw["close"].iloc[0] * 0.96)  # 除息前的價格往下還原
    assert data.px["2330"]["close"].iloc[0] == pytest.approx(raw["close"].iloc[0])
    assert data.funds["2330"]["eps"] == {"2024": 5.0} and data.revenue == {} and len(data.days) > 400
    assert webstore.load(lab.FUND_HISTORY)["stocks"]["2330"]["roe"] == {"2024": 20.0}
    lab.load_data(years=2, watch=watch)
    assert asked == ["2330"]  # 第二次直接用存好的歷年財報


def test_if_due_skips_when_last_report_is_recent(monkeypatch):
    from stock_strategies import webstore

    webstore.save(lab.REPORT, {"generated_at": pd.Timestamp.now(tz="Asia/Taipei").isoformat()})
    monkeypatch.setattr(lab, "run", lambda **kw: pytest.fail("不該重跑"))
    assert lab.main(["--if-due", "25"]) == 0
