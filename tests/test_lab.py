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


def test_days_without_trades_do_not_fake_momentum():
    days = list(pd.bdate_range("2026-01-05", periods=4).strftime("%Y-%m-%d"))
    quiet = pd.DataFrame({"date": days, "open": [50.0, 0.0, 51.0, 52.0], "high": 52.0, "low": 50.0,
                          "close": [50.0, 0.0, 51.0, 52.0], "volume": 1.0})  # 第 2 天沒成交，價格是 0
    data = lab.Data({"2330": quiet, "0050": quiet.copy()}, pd.Series(100.0, index=days), {})
    assert data.close["2330"].tolist() == [50.0, 50.0, 51.0, 52.0]  # 沿用前一天收盤，不會算出無限大的漲幅
    res = lab.simulate(data, {days[0]: ["2330"]}, days[0], days[-1], slots=1, capital=10_000)
    assert res["exposure"].iloc[1] == 0 and res["exposure"].iloc[2] == 0  # 開盤價 0 的那天買不到（這次換股就不買了）


def test_verdict_rules():
    base = {"folds": [{}] * 10}
    assert lab._verdict({**base, "excess_cagr_pct": 5, "t_stat": 3.2, "win_folds": 7}) == "strong"
    assert lab._verdict({**base, "excess_cagr_pct": 5, "t_stat": 2.4, "win_folds": 6}) == "tentative"
    assert lab._verdict({**base, "excess_cagr_pct": 5, "t_stat": 1.9, "win_folds": 9}) == "none"   # 不顯著
    assert lab._verdict({**base, "excess_cagr_pct": -1, "t_stat": 3.5, "win_folds": 9}) == "none"  # 沒贏
    assert lab._verdict({**base, "excess_cagr_pct": 5, "t_stat": 3.5, "win_folds": 5}) == "none"   # 只贏一半的區段
    # 從事後名單選股的策略：還要贏過「等權重持有同一份名單」，而且最多只算「有跡象」
    good = {**base, "excess_cagr_pct": 30, "t_stat": 3.5, "win_folds": 9}
    assert lab._verdict(good, {"excess_cagr_pct": 8, "t_stat": 2.1, "win_folds": 6}) == "promising"
    assert lab._verdict(good, {"excess_cagr_pct": 8, "t_stat": 1.2, "win_folds": 6}) == "none"  # 贏 0050 只是因為名單虛胖


def test_recently_fetched_data_is_reused_without_asking_finmind(tmp_path, monkeypatch):
    """實驗室同一兩天內重跑：快取裡剛抓過的資料直接用，不再花額度。"""
    from stock_strategies import cache

    monkeypatch.setattr(cache, "_cache_dir", lambda: tmp_path)
    asked = []

    def fake_get(params, timeout, max_retries):
        asked.append(params["dataset"])
        return {"data": [{"date": "2025-07-15", "stock_id": "2330", "before_price": 100, "after_price": 96}]}

    monkeypatch.setattr(cache, "_rate_limited_get", fake_get)
    cache.fetch_finmind_cached("TaiwanStockDividendResult", "2330", "2016-01-01")
    meta_path = cache._meta_path("TaiwanStockDividendResult", "2330")
    meta = json.loads(meta_path.read_text())
    meta["fetched_at"] = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=30)).isoformat()  # 當成 30 小時前抓的
    meta_path.write_text(json.dumps(meta))
    assert not cache._is_fresh("TaiwanStockDividendResult", "2330", None)  # 已經是「昨天以前」：平常會再抓一次
    with lab._reuse_recent(36):
        again = cache.fetch_finmind_cached("TaiwanStockDividendResult", "2330", "2016-01-01")
    assert len(again) == 1 and asked == ["TaiwanStockDividendResult"]  # 36 小時內：沒有再問 FinMind
    with lab._reuse_recent(12):
        cache.fetch_finmind_cached("TaiwanStockDividendResult", "2330", "2016-01-01")
    assert len(asked) == 2  # 超過重用時限才重抓


def test_report_end_to_end_on_synthetic_market(monkeypatch):
    from stock_strategies import webstore

    saved = {}
    monkeypatch.setattr(webstore, "save", lambda name, obj: saved.update({name: obj}))
    report = lab.run(_data(), eval_floor="2000-01-01")
    assert saved[lab.REPORT] is report and json.dumps(report)  # 純 JSON，存得進 webstore
    assert [c["id"] for c in report["candidates"]] == ["v32", "v32_nobt", "revenue", "combo", "trend", "timing"]
    n_folds = len(report["benchmark"]["folds"])
    assert report["trials"] == 19 and n_folds >= 2
    for c in report["candidates"]:
        assert c["verdict"] in ("strong", "tentative", "promising", "none") and len(c["folds"]) == n_folds
        assert c["curve"][0][0] >= report["period"]["start"] and 0 <= c["exposure_pct"] <= 100
    assert any(c["trades_per_year"] > 0 for c in report["candidates"])
    timing = next(c for c in report["candidates"] if c["id"] == "timing")
    assert all("config" in f for f in timing["folds"]) and timing["current"].endswith("日均線")
    assert report["passed"] == [c["id"] for c in report["candidates"] if c["verdict"] in ("strong", "tentative")]
    assert report["universe"]["stocks"] == 6 and len(report["universe"]["folds"]) == n_folds
    assert timing["vs_universe"] is None and timing["verdict"] in ("strong", "tentative", "none")
    for c in report["candidates"]:
        if c["universe"]:  # 從事後名單選股的：有對照組的比較，而且最多只會是「有跡象」
            assert set(c["vs_universe"]) == {"excess_cagr_pct", "t_stat", "win_folds"}
            assert c["verdict"] in ("promising", "none")
    assert "事後挑贏家" in report["notes"][0]


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


def test_api_serves_latest_report():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api import backtest_api
    from stock_strategies import webstore

    backtest_api._lab.update(at=0.0, report=None, pit=None)
    app = FastAPI()
    backtest_api.install(app)
    client = TestClient(app)
    assert client.get("/api/lab").json() == {"report": None, "pit": None}  # 還沒跑過
    webstore.save(lab.REPORT, {"generated_at": "2026-11-20T08:00:00+08:00", "candidates": []})
    backtest_api._lab["at"] = 0.0
    assert client.get("/api/lab").json()["report"]["generated_at"].startswith("2026-11-20")
    webstore.save("lab_pit_report", {"generated_at": "2026-11-21T20:10:00+08:00", "candidates": []})  # 當時名單版
    backtest_api._lab["at"] = 0.0
    body = client.get("/api/lab").json()
    assert body["pit"]["generated_at"].startswith("2026-11-21") and body["report"]["generated_at"].startswith("2026-11-20")


def test_fetch_waits_until_rolling_quota_recovers(monkeypatch):
    """FinMind 額度是滾動一小時：用完就每 5 分鐘看一次，恢復了才繼續，不會缺資料也不會空等。"""
    from stock_strategies import cache

    left = iter([600, 10, 60, 300, 500])  # 開始很多 → 快用完 → 還不夠 → 恢復 → （又用完後）再恢復
    naps, calls = [], []
    monkeypatch.setattr(lab, "_quota_left", lambda: next(left))
    monkeypatch.setattr(lab.time, "sleep", naps.append)
    monkeypatch.setattr(lab, "_calls", {"n": 0})

    def flaky(x):
        calls.append(x)
        if len(calls) == 21:
            raise cache.FinMindRateLimitError("用完")
        return x * 2

    assert [lab._fetch(flaky, i) for i in range(20)] == [i * 2 for i in range(20)] and naps == []
    assert lab._fetch(flaky, 7) == 14   # 第 21 次：先發現只剩 10 次 → 等到恢復；又碰到用完 → 再等一次後成功
    assert naps == [300, 300, 300] and len(calls) == 22


def test_monthly_decision_days_are_days_you_can_recognise_on_the_day_itself():
    # 2026 年 10 月：10/9（補假）、10/10（六）休市 → 申報期限順延到 10/12，10/13 收盤後才確定資料到齊
    october = ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
               "2026-10-08", "2026-10-12", "2026-10-13", "2026-10-14"]
    assert lab.mid_months(october) == ["2026-10-13"]         # 資料從 9/29 才開始：9 月看不出期限是哪天，不算
    assert lab.mid_months(october[:9]) == []                 # 只到 10/12：還不能決定
    assert lab.mid_months(["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"]) == ["2026-11-11"]  # 10 日有開盤：隔天
    assert lab.month_starts(october) == ["2026-10-01"]       # 月初第一個交易日（這時才確定 9/30 是上個月最後一天）
    assert lab.month_ends(october) == ["2026-09-30"]         # 最後一天是不是月底，要等下個月開盤才知道


def test_combo_buys_revenue_leaders_only_when_price_is_above_its_half_year_average():
    data = _data()
    ranked = lab.plan_revenue(data, "yoy", 99)
    combo = lab.plan_combo(data, "yoy", 3)
    close = data.close[data.stocks]
    above = close > close.rolling(120, min_periods=96).mean()
    assert set(combo) == set(ranked) and any(combo.values())
    dropped = 0
    for d, picks in combo.items():
        assert picks == [s for s in ranked[d] if above.at[d, s]][:3]   # 營收排名的順序不變，只是把跌破均線的拿掉
        dropped += len([s for s in ranked[d][:3] if not above.at[d, s]])
    assert dropped > 0                                                    # 真的有被濾掉的
    assert lab._label("combo", ("accel", 10)) == "近3月÷近12月營收前 10 檔＋站上 120 日線"


def test_annualised_return_uses_calendar_time_not_252_trading_days():
    """台股一年大約 243 個交易日：一年賺 10% 就是年化 10%，不能因為除以 252 變成 10.4%。"""
    days = list(pd.date_range("2025-01-02", "2026-01-01", periods=243).strftime("%Y-%m-%d"))
    ret = pd.Series(1.10 ** (1 / 243) - 1, index=days)
    stats = lab._stats(ret, ret, [days])
    assert stats["cagr_pct"] == pytest.approx(10.0, abs=0.05) and stats["excess_cagr_pct"] == 0
    assert lab.span_years(days) == pytest.approx(1.0, abs=0.01) and lab.span_years(days[:1]) > 0


def test_rebalanced_control_really_is_equal_weight():
    """檔數多的對照組如果不調回等權重，賣出的錢不夠買新進的，久了只剩少數幾檔——那就不是「等權重持有名單」了。"""
    data = _data(stocks=12)
    names = [s for s in data.stocks]
    plan = {d: names[i % 3:] for i, d in enumerate(lab.month_starts(data.days))}  # 名單每個月有進有出
    start, end = data.days[300], data.days[-1]
    kept = lab.simulate(data, plan, start, end, len(names), initial=names, capital=1e8)
    even = lab.simulate(data, plan, start, end, len(names), initial=names, capital=1e8, rebalance=True)
    last_plan = plan[max(d for d in plan if d < end)]
    assert set(even["positions"]) == set(last_plan)                      # 名單裡的每一檔都有
    value = pd.Series({s: even["prices"][s] * n for s, n in even["positions"].items()})
    weights = value / (value.sum() + even["cash"])
    assert weights.max() / weights.min() < 1.6 and weights.max() < 0.14   # 調過之後大家差不多重（月中漲跌造成的差距而已）
    drift = pd.Series({s: kept["prices"][s] * n for s, n in kept["positions"].items()})
    assert drift.max() / drift.min() > weights.max() / weights.min()      # 不調的話比重差距比較大
    assert even["trades_per_year"] > kept["trades_per_year"] and even["cost_pct_per_year"] > 0  # 調整是有成本的，照算


def test_universe_control_is_fully_invested_in_whatever_is_listed():
    """對照組：每個月初等權重持有「當時已經掛牌」的名單個股（還沒掛牌的不佔位置），成本照算。"""
    data = _data(stocks=8)
    late = data.stocks[-1]
    cut = data.days[700]
    data.c[late] = {d: v for d, v in data.c[late].items() if d >= cut}   # 最後一檔在第 700 天才掛牌
    data.o[late] = {d: v for d, v in data.o[late].items() if d >= cut}
    ret = lab.universe_returns(data, data.days[300], data.days[-1])
    assert len(ret) == len(data.days) - 300 and abs(ret).max() < 0.2
    before = lab.simulate(data, lab.plan_universe(data), data.days[300], data.days[690], 7,
                          initial=data.stocks[:-1], capital=1e8, rebalance=True)
    assert late not in before["positions"] and before["exposure"].iloc[-1] > 0.97   # 沒掛牌的不留現金等它
    plan = lab.plan_universe(data)
    assert late not in plan[max(d for d in plan if d < cut)] and late in plan[min(d for d in plan if d > cut)]
