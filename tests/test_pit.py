"""當時名單版實驗室：名單只能用當時的資料、下市的股票要留著、還原要對、兩個對照都要贏才算數。"""

import numpy as np
import pandas as pd
import pytest

from stock_strategies import lab, pit, twse

N, STOCKS = 1500, 12


def market(n=N, stocks=STOCKS, seed=3):
    """合成的全市場行情（長表）：2301 成交金額最大、依序遞減；外加 0050。"""
    rng = np.random.default_rng(seed)
    days = list(pd.bdate_range("2019-01-01", periods=n).strftime("%Y-%m-%d"))
    frames = []
    for i, sid in enumerate([str(2301 + k) for k in range(stocks)] + ["0050"]):
        c = 50 * np.exp(np.cumsum(rng.normal(0.0004 + 0.0001 * (i % 3), 0.012 if sid == "0050" else 0.018, n)))
        o = c * (1 + rng.normal(0, 0.004, n))
        value = (stocks + 1 - i) * 1e8 * (1 + 0.2 * rng.random(n))
        frames.append(pd.DataFrame({"date": days, "id": sid, "open": o, "high": np.maximum(o, c),
                                    "low": np.minimum(o, c), "close": c, "volume": (value / c).astype("int64"),
                                    "value": value, "flag": 0}))
    index = pd.Series(10000 * np.exp(np.cumsum(rng.normal(0.0004, 0.01, n))), index=days)
    return days, pd.concat(frames, ignore_index=True), index


def revenues(seed=7, companies=320):
    """全市場月營收：前 STOCKS 家是行情裡的股票，其餘是名單外的公司（月營收彙總表本來就有全體上市公司）。"""
    rng = np.random.default_rng(seed)
    months = [str(m) for m in pd.period_range("2017-06", "2024-12", freq="M")]
    return {str(2301 + k): dict(zip(months, 1000 * np.exp(np.cumsum(rng.normal(0.01, 0.08, len(months))))))
            for k in range(companies)}


@pytest.fixture
def source(monkeypatch):
    """假的證交所：測試可以改 state 裡的行情、除權息再呼叫 pit.build()。"""
    days, quotes, index = market()
    state = {"days": days, "quotes": quotes, "index": index, "ex": {}, "revenue": revenues()}
    monkeypatch.setattr(twse, "calendar", lambda start, end=None: [(d, float(index[d])) for d in state["days"]])
    monkeypatch.setattr(twse, "load_quotes", lambda want: state["quotes"][state["quotes"]["date"].isin(set(want))])
    monkeypatch.setattr(twse, "exrights", lambda start, end=None: state["ex"])
    monkeypatch.setattr(twse, "revenue", lambda start, end=None: state["revenue"])
    monkeypatch.setattr(twse, "names", lambda: {"2301": "甲公司"})
    monkeypatch.setattr(pit, "TOP", 6)
    monkeypatch.setattr(pit, "OTHER_TOPS", (4, 8))
    return state


def test_revenue_series_uses_the_legal_deadline_and_same_month_last_year():
    months = {str(m): 100.0 for m in pd.period_range("2022-01", "2023-06", freq="M")}
    months["2023-02"] = 150.0
    del months["2022-05"]
    r = pit.revenue_series(months)
    i = r["avail"].index("2023-03-10")                      # 2 月營收：3 月 10 日才看得到
    assert r["yoy"][i] == pytest.approx(0.5)
    assert np.isnan(r["yoy"][r["avail"].index("2023-06-10")])  # 去年 5 月沒資料 → 算不出年增率，不會亂配
    assert np.isnan(r["accel"][i])                              # 近 12 個月缺一個月 → 不算
    assert r["accel"][-1] == pytest.approx(100 / (1250 / 12))  # 2022-07～2023-06 都有：近 3 月平均 ÷ 近 12 月平均
    assert pit.revenue_series({"2023-01": 1.0}) is None


def test_ranking_only_uses_data_up_to_that_month_end():
    days, quotes, _ = market()
    hot = (quotes["id"] == "2312") & (quotes["date"] >= days[900])
    quotes.loc[hot, "value"] *= 100                        # 最冷門的 2312 在第 900 天之後才爆量
    full = pit.rank_universe(quotes, days, 6)
    early = pit.rank_universe(quotes[quotes["date"] <= days[880]], days[:881], 6)
    assert early and all(full[d] == early[d] for d in early)        # 後來的資料不會改到以前的名單
    before, after = [d for d in full if d < days[900]], [d for d in full if d > days[980]]
    assert all("2312" not in full[d] for d in before)
    assert all(full[d][0] == "2312" for d in after)
    assert min(full) >= days[pit.LIQUIDITY_DAYS - 1]                # 資料不滿 60 天不排名


def test_build_keeps_delisted_stocks_and_restores_reference_price_changes(source):
    days, q = source["days"], source["quotes"]
    plain = pit.build()
    base = plain.px["2302"]["close"].pct_change().to_numpy()
    k, j = 700, 1000
    ex_div = (q["id"] == "2302") & (q["date"] >= days[k])
    q.loc[ex_div, ["open", "high", "low", "close"]] *= 0.95          # 第 700 天除息 5%
    reduce_ = (q["id"] == "2302") & (q["date"] >= days[j])
    q.loc[reduce_, ["open", "high", "low", "close"]] *= 1.08         # 第 1000 天減資恢復買賣，參考價調高 8%
    q.loc[(q["id"] == "2302") & q["date"].isin([days[k], days[j]]), "flag"] = twse.FLAG_X
    source["ex"] = {"2302": [(days[k], 0.95)]}
    source["quotes"] = q[~((q["id"] == "2303") & (q["date"] > days[800]))]  # 2303 在第 800 天之後下市
    data = pit.build()
    got = data.px["2302"]["close"].pct_change().to_numpy()
    same = np.ones(len(got), dtype=bool)
    same[[0, j]] = False
    assert np.allclose(got[same], base[same])                        # 除息日的報酬還原回來了
    assert abs(got[j]) < 1e-12                                       # 查不到幅度的那天不算報酬
    assert data.dividend_events == 1 and data.other_adjustments == 1
    assert data.px["2303"]["date"].iloc[-1] == days[800]             # 下市的股票留在歷史裡
    assert any("2303" in ids[:3] for d, ids in data.ranked.items() if d <= days[800])
    assert all("2303" not in ids for d, ids in data.ranked.items() if d > days[880])
    assert data.names["2301"] == "甲公司" and "0050" in data.px and "0050" not in data.stocks


def test_strategies_can_only_pick_from_the_list_of_that_time(source):
    data = pit.build()
    data.top = 4
    plans = [lab.plan_trend(data, 60, 5), lab.plan_revenue(data, "yoy", 5), lab.plan_combo(data, "yoy", 5),
             lab.plan_universe(data)]
    for plan in plans:
        assert any(plan.values())
        for d, picks in plan.items():
            assert set(picks) <= data.eligible(d)
    last = max(data.ranked)
    assert data.eligible(last) == set(data.ranked[last][:4]) == {"2301", "2302", "2303", "2304"}
    assert data.eligible("2018-01-01") == set()                      # 還沒有名單的時候什麼都不能買
    assert lab.Data(data.px, data.index, data.names).eligible(last) is None  # 原本的實驗室（事後名單）不受影響


def test_verdict_needs_both_the_index_and_the_universe_control():
    folds = [{}] * 10
    strong = {"excess_cagr_pct": 10.0, "t_stat": 3.5, "win_folds": 8, "folds": folds}
    weak = {"excess_cagr_pct": 4.0, "t_stat": 2.2, "win_folds": 6, "folds": folds}
    none = {"excess_cagr_pct": 4.0, "t_stat": 1.2, "win_folds": 6, "folds": folds}
    assert pit.verdict(strong, strong) == "strong"
    assert pit.verdict(strong, weak) == "tentative"
    assert pit.verdict(strong, none) == "none"                      # 贏 0050 但沒贏「等權重持有當時名單」不算選股能力
    assert pit.verdict(none, strong) == "none"


def test_run_reports_every_candidate_with_controls_and_current_picks(source, monkeypatch):
    saved = {}
    from stock_strategies import webstore

    monkeypatch.setattr(webstore, "save", lambda name, obj: saved.update({name: obj}))
    data = pit.build()
    report = pit.run(data)
    assert saved[pit.REPORT] is report
    assert [c["id"] for c in report["candidates"]] == ["revenue", "combo", "trend"]
    assert report["top"] == 6 and report["universe"]["stocks"] == 6
    assert [o["top"] for o in report["universe"]["other_tops"]] == [4, 8]
    assert report["period"]["start"] >= "2022-04-30" and len(report["benchmark"]["folds"]) >= 4
    for c in report["candidates"]:
        assert c["verdict"] in pit.ORDER and set(c["vs_universe"]) == {"excess_cagr_pct", "t_stat", "win_folds"}
        assert c["with_slippage"]["cagr_pct"] <= c["cagr_pct"] + 0.5    # 加滑價不會變比較好（容許換參數的雜訊）
        assert [o["top"] for o in c["other_tops"]] == [4, 8]
        picks = c["picks"]
        assert picks["config"] == c["current"] and picks["decided"] <= report["period"]["end"]
        assert len(picks["stocks"]) <= 10 and all(s["id"] in data.eligible(picks["decided"]) for s in picks["stocks"])
    assert report["passed"] == [c["id"] for c in report["candidates"] if c["verdict"] != "none"]
    assert any("當時" in n for n in report["notes"]) and report["data"]["stocks_ever"] == 8  # 進過前 8 名的
    assert pit.summary(report)[0].startswith("樣本外")


def test_live_plan_follows_the_parameters_chosen_for_each_period():
    days = [f"d{i:02d}" for i in range(40)]
    plans = {"A": ({"d10": ["x"], "d30": ["y"]}, 5), "B": ({"d10": ["p"], "d30": ["q"]}, 10)}
    plan, sizes = pit.live_plan(plans, ["A", "B"], [days[:20], days[20:]], days)
    assert plan == {"d10": ["x"], "d19": ["p"], "d30": ["q"]}        # 第二段開始前一天換成 B 最近一次決定的名單
    assert sizes == {"d10": 5, "d19": 10, "d30": 10}
    same, _ = pit.live_plan(plans, ["A", "A"], [days[:20], days[20:]], days)
    assert same == {"d10": ["x"], "d30": ["y"]}                      # 沒換參數就不會多一次換股
    eve = {"A": ({"d19": ["x"]}, 5), "B": ({"d19": ["p"]}, 10)}
    plan, sizes = pit.live_plan(eve, ["A", "B"], [days[:20], days[20:]], days)
    assert plan == {"d19": ["p"]} and sizes == {"d19": 10}           # 決定日隔天就是新的一段：直接用新參數


def test_paper_accounts_start_the_day_after_and_are_recomputed_from_scratch(source):
    data = pit.build()
    days = data.days
    report = pit.run(data, save=False, track_after=days[-31])
    acc = {a["id"]: a for a in report["tracking"]["accounts"]}
    assert report["tracking"] == pit.run(data, save=False, track_after=days[-31])["tracking"]  # 重算結果一樣
    for a in acc.values():
        assert a["since"] == days[-30] and a["days"] == 30
        assert sum(h["weight_pct"] for h in a["holdings"]) + a["cash_pct"] == pytest.approx(100, abs=0.5)
        bought = data.o["0050"][days[-30]]
        assert a["bench_pct"] == pytest.approx((data.c["0050"][days[-1]] / bought - 1) * 100, abs=0.3)
    rev = acc["revenue"]
    assert rev["holdings"] and {h["id"] for h in rev["holdings"]} <= set(data.stocks)
    fresh = pit.run(data, save=False, track_after=days[-1])["tracking"]["accounts"]
    assert all(a["since"] is None and a["days"] == 0 and not a["holdings"] for a in fresh)
    assert fresh[0]["next"]["buy"] and not fresh[0]["next"]["sell"]  # 還沒開始：列出下個交易日開盤要買的


def _report(verdict="tentative", nxt=None, as_of="2026-11-11", changed=None):
    account = {"id": "revenue", "name": "月營收動能", "verdict": verdict, "config": "營收年增率前 5 檔", "since": "2026-11-02",
               "days": 8, "return_pct": 1.5, "bench_pct": 0.7, "cash_pct": 2.0, "decided": as_of, "next": nxt, "changed": changed,
               "holdings": [{"id": "2330", "name": "台積電", "shares": 10, "price": 1000.0, "weight_pct": 98.0}]}
    cand = {"id": "revenue", "name": "月營收動能", "verdict": verdict, "cagr_pct": 30.0, "excess_cagr_pct": 5.0,
            "t_stat": 2.3 if verdict != "none" else 0.8, "win_folds": 9 if verdict != "none" else 7, "mdd_pct": -58.9,
            "folds": [{}] * 14, "vs_universe": {"excess_cagr_pct": 4.0, "t_stat": 2.1, "win_folds": 9}}
    return {"period": {"years": 6.8, "end": as_of}, "benchmark": {"cagr_pct": 25.0}, "universe": {"cagr_pct": 26.0},
            "candidates": [cand], "tracking": {"after": "2026-10-30", "as_of": as_of, "accounts": [account]}}


def test_line_message_is_sent_on_first_run_rebalance_days_and_fridays_only():
    quiet = _report(as_of="2026-11-11")                                # 週三、沒有換股
    moved = {"sell": [{"id": "2303", "name": "聯電"}], "buy": [{"id": "2330", "name": "台積電"}]}
    text, state = pit.line_message(_report(as_of="2026-11-11", changed=moved), {})
    assert "策略實驗室" in text and "初步有證據" in text and "2330 台積電" in text and "不是投資建議" in text
    assert text.splitlines()[0] == "🔬 策略實驗室" and "已照" not in text  # 第一則是介紹，不當成換股通知
    assert pit.line_message(quiet, state) == (None, state)            # 第二次就不重複推
    swap = {"sell": [{"id": "2330", "name": "台積電"}], "keep": [], "buy": [{"id": "2317", "name": "鴻海"}]}
    state = {**state, "told": {"revenue": "2026-10-12"}}
    text, told = pit.line_message(_report(nxt=swap), state, v32_excess=-18.9)
    assert "換股" in text.splitlines()[0] and "賣出：2330 台積電" in text and "買進：2317 鴻海" in text
    assert "V3.2" in text and "每年輸 0050 18.9 個百分點" in text
    assert "每年贏 0050 5.0 個百分點、贏當時名單 4.0；14 段贏 9 段、t 值 2.3" in text
    assert pit.line_message(_report(nxt=swap), told)[0] is None       # 同一次換股只通知一次
    text, weekly = pit.line_message(_report(as_of="2026-11-13"), told)  # 週五
    assert "每週摘要" in text.splitlines()[0] and "模擬帳戶 11/02 起 +1.5%（同期 0050 +0.7%）" in text
    assert pit.line_message(_report(as_of="2026-11-13"), weekly)[0] is None


def test_line_message_never_pushes_picks_of_an_unvalidated_strategy():
    swap = {"sell": [], "keep": [], "buy": [{"id": "2317", "name": "鴻海"}]}
    text, state = pit.line_message(_report("none", swap), {})
    assert "沒有任何策略有足夠的樣本外證據" in text and "未通過驗證，僅觀察" in text
    # 數字比 0050 高但沒通過：要講清楚為什麼不算數
    assert "比 0050 多 5.0 個百分點，但不穩定：14 段只贏 7 段、t 值 0.8（要 2 以上）、最大回撤 -58.9%" in text
    loser = _report("none")
    loser["candidates"][0]["excess_cagr_pct"] = -4.97
    assert "每年輸 0050 4.97 個百分點" in pit.line_message(loser, {})[0]
    assert "鴻海" not in text and "台積電" not in text               # 沒通過驗證：只報成績，不報持股與換股
    assert pit.line_message(_report("none", swap), state)[0] is None  # 也不會因為它換股就推播


def test_a_rebalance_that_was_not_announced_in_time_is_announced_afterwards():
    """決定那天晚上沒跑成（例如資料抓不到）：隔天資料已經過了執行日，還是要補一則通知。"""
    moved = {"sell": [{"id": "2303", "name": "聯電"}], "buy": [{"id": "2330", "name": "台積電"}]}
    state = {"intro": True, "told": {"revenue": "2026-10-12"}}
    late = _report(as_of="2026-11-12", changed=moved)                  # 資料已經到 11/12，決定日是 11/11
    late["tracking"]["accounts"][0]["decided"] = "2026-11-11"
    text, told = pit.line_message(late, state)
    assert text.splitlines()[0].endswith("換股通知")
    assert "已照 2026-11-11 的決定換股：賣出 2303 聯電；買進 2330 台積電" in text and "目前持股" in text
    assert told["told"]["revenue"] == "2026-11-11" and pit.line_message(late, told)[0] is None


def test_incomplete_revenue_stops_the_run_instead_of_ranking_on_partial_data(source):
    full = source["revenue"]
    source["revenue"] = {sid: {m: v for m, v in months.items() if m != "2021-03"} for sid, months in full.items()}
    with pytest.raises(RuntimeError, match="月營收還沒抓齊.*2021-03"):
        pit.build()
    # 最新一個月：全市場大多申報了，但名單裡的股票幾乎都還沒有 → 不能拿來排名
    late = {sid: dict(months) for sid, months in full.items()}
    for sid in [str(2301 + k) for k in range(1, STOCKS)]:
        del late[sid]["2024-08"]
    source["revenue"] = late
    with pytest.raises(RuntimeError, match="2024-08 的月營收只到 1/6 檔"):
        pit.build()
    source["revenue"] = full
    assert pit.build().revenue
