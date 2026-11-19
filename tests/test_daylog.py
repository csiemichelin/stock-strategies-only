"""回測逐日紀錄：算過的交易日不重算、不重抓資料；每日排程補最新一天。"""
import numpy as np
import pandas as pd
import pytest

from stock_strategies import daylog, sim

STRATEGY = {"id": "default", "name": "預設"}


def _synthetic(n=900, seed=1):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, n)))
    o = c * (1 + rng.normal(0, 0.006, n))
    return pd.DataFrame({"date": pd.bdate_range("2023-01-02", periods=n).strftime("%Y-%m-%d"),
                         "open": o, "high": np.maximum(o, c) * 1.01, "low": np.minimum(o, c) * 0.99,
                         "close": c, "volume": rng.integers(1_000_000, 5_000_000, n).astype(float)})


class World:
    """4 檔合成股票＋大盤；upto 控制「目前資料到第幾天」，calls 記錄抓了幾次歷史資料。"""

    def __init__(self, monkeypatch):
        from stock_strategies import cache, data, datasources, sheet

        self.full = {f"S{i}": _synthetic(seed=10 + i) for i in range(4)}
        self.index = _synthetic(seed=99)
        self.dates = list(self.index["date"])
        self.upto = len(self.dates)
        self.calls = []
        self.index_calls = 0
        self.lag = set()  # 這些股票最新一天還沒更新
        monkeypatch.setattr(sheet, "read_watchlist", lambda: [{"stock_id": k, "name": k} for k in self.full])
        monkeypatch.setattr(sim, "_full_history", self._history)
        monkeypatch.setattr(data, "get_fundamental", lambda sid: {"eps": {2023: 6, 2024: 7}, "roe": {2023: 16, 2024: 18}})
        monkeypatch.setattr(datasources, "get_index_history", self._index)
        monkeypatch.setattr(cache, "_is_fresh", lambda *a, **k: True)  # 大盤在快取裡（讀它不花額度）
        monkeypatch.setattr(daylog, "latest_expected_day", lambda now=None: self.dates[self.upto - 1])

    def _history(self, sid, years):
        self.calls.append(sid)
        return self.full[sid].iloc[:self.upto - (1 if sid in self.lag else 0)].reset_index(drop=True)

    def _index(self, *a, **k):
        self.index_calls += 1
        return self.index.iloc[:self.upto].reset_index(drop=True)

    def day(self, offset):
        return self.dates[self.upto - 1 + offset]


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def test_second_backtest_reuses_log_and_fetches_nothing(world):
    start = world.day(-150)
    fresh = sim.replay(10_000, start, STRATEGY, use_log=False)
    first = sim.replay(10_000, start, STRATEGY)
    assert first["log_info"]["computed"] == 151 and first["log_info"]["reused"] == 0
    world.calls.clear()
    second = sim.replay(10_000, start, STRATEGY)
    assert world.calls == []  # 完全沒抓歷史資料
    assert second["log_info"]["computed"] == 0 and second["log_info"]["reused"] == 151
    assert first["trades"] == fresh["trades"] == second["trades"]
    assert first["equity"] == fresh["equity"] == second["equity"]
    assert fresh["trades"], "合成資料應該要有交易，否則測試沒意義"


def test_new_trading_day_only_computes_that_day(world):
    start = world.day(-150)
    world.upto -= 1
    sim.replay(10_000, start, STRATEGY)
    world.upto += 1
    again = sim.replay(10_000, start, STRATEGY)
    assert again["log_info"]["computed"] == 1 and again["log_info"]["reused"] == 150
    fresh = sim.replay(10_000, start, STRATEGY, use_log=False)
    assert again["trades"] == fresh["trades"] and again["equity"] == fresh["equity"]


def test_other_start_date_and_capital_share_the_log(world):
    sim.replay(10_000, world.day(-100), STRATEGY)
    world.calls.clear()
    later = sim.replay(50_000, world.day(-40), STRATEGY)  # 換開始日、換本金：全部沿用
    assert world.calls == [] and later["log_info"]["computed"] == 0 and later["log_info"]["reused"] == 41
    earlier = sim.replay(10_000, world.day(-120), STRATEGY)  # 往前多 20 天：只算那 20 天
    assert earlier["log_info"]["computed"] == 20 and earlier["log_info"]["reused"] == 101
    fresh = sim.replay(10_000, world.day(-120), STRATEGY, use_log=False)
    assert earlier["trades"] == fresh["trades"] and earlier["equity"] == fresh["equity"]


def test_other_strategy_params_do_not_share_signals(world):
    start = world.day(-60)
    sim.replay(10_000, start, STRATEGY)
    other = {"id": "fast", "name": "短線", "params": {"hold_days": 5}}
    assert daylog.strategy_key(other) != daylog.strategy_key(STRATEGY)
    assert daylog.strategy_key({"params": {"hold_days": 20.0}}) == daylog.strategy_key({"params": {"hold_days": 20}})
    state = sim.replay(10_000, start, other)
    assert state["log_info"]["computed"] == 61 and state["log_info"]["reused"] == 0


def test_incomplete_latest_day_is_not_logged(world):
    world.lag = {"S0", "S1", "S2"}  # 大盤已有最新一天，但多數股票還沒更新
    start = world.day(-30)
    state = sim.replay(10_000, start, STRATEGY)
    assert state["log_info"]["pending"] == [world.day(0)] and state["last_date"] == world.day(-1)
    key = daylog.strategy_key(STRATEGY)
    assert world.day(0) not in daylog.load(fresh=True).done(key)
    world.lag = set()
    state = sim.replay(10_000, start, STRATEGY)  # 資料齊了再補
    assert state["log_info"]["computed"] == 1 and state["last_date"] == world.day(0)


def test_lazy_tail_waits_for_daily_job_instead_of_spending_quota(world, monkeypatch):
    from stock_strategies import cache

    start = world.day(-50)
    world.upto -= 1
    sim.replay(10_000, start, STRATEGY)
    world.upto += 1
    world.calls.clear()
    monkeypatch.setattr(sim, "estimate_finmind_calls", lambda *a, **k: {"total": 131})  # 快取是冷的
    lazy = sim.replay(10_000, start, STRATEGY, lazy_tail=True)
    assert world.calls == [] and lazy["log_info"]["deferred"] == [world.day(0)]
    assert lazy["log_info"]["computed"] == 0 and lazy["last_date"] == world.day(-1)

    monkeypatch.setattr(cache, "_is_fresh", lambda *a, **k: False)  # 連大盤都不在快取：整個不碰 FinMind
    world.index_calls = 0
    lazy = sim.replay(10_000, start, STRATEGY, lazy_tail=True)
    assert world.index_calls == 0 and world.calls == [] and lazy["log_info"]["reused"] == 50

    monkeypatch.setattr(sim, "estimate_finmind_calls", lambda *a, **k: {"total": 0})  # 快取是熱的就順手補
    monkeypatch.setattr(cache, "_is_fresh", lambda *a, **k: True)
    warm = sim.replay(10_000, start, STRATEGY, lazy_tail=True)
    assert warm["log_info"]["computed"] == 1 and warm["log_info"]["deferred"] == []


def test_recompute_ignores_old_log(world):
    start = world.day(-20)
    sim.replay(10_000, start, STRATEGY)
    world.calls.clear()
    state = sim.replay(10_000, start, STRATEGY, recompute=True)
    assert state["log_info"]["computed"] == 21 and len(world.calls) == 4


def test_daily_job_extends_recently_used_strategies(world, monkeypatch):
    start = world.day(-30)
    world.upto -= 1
    sim.replay(10_000, start, STRATEGY)  # 網頁回測過 → 記下最近用過這組參數
    world.upto += 1
    monkeypatch.setattr(sim, "estimate_finmind_calls", lambda *a, **k: {"total": 300})
    assert "留給網頁回測" in sim.extend_recent_logs()[0]  # 要花額度就不做
    monkeypatch.setattr(sim, "estimate_finmind_calls", lambda *a, **k: {"total": 0})
    assert "補了 1 天" in sim.extend_recent_logs()[0]
    assert world.day(0) in daylog.load(fresh=True).done(daylog.strategy_key(STRATEGY))
    assert "補了 0 天" in sim.extend_recent_logs()[0]


def test_daily_job_seeds_default_strategy_so_first_backtest_is_free(world, monkeypatch):
    monkeypatch.setattr(sim, "estimate_finmind_calls", lambda *a, **k: {"total": 0})
    span = (pd.Timestamp.now() - pd.Timestamp(world.day(-60))).days  # 合成資料停在過去，換算成「最近幾天」
    msg = sim.extend_recent_logs(seed=[STRATEGY], seed_days=span)[0]  # 還沒人回測過：先建好最近這段
    assert "補了" in msg and "補了 0 天" not in msg
    world.calls.clear()
    state = sim.replay(10_000, world.day(-20), STRATEGY, lazy_tail=True)
    assert world.calls == [] and state["log_info"]["computed"] == 0 and state["log_info"]["reused"] == 21
    assert "補了 0 天" in sim.extend_recent_logs(seed=[STRATEGY], seed_days=span)[0]


def test_todo_order_keeps_logged_days_contiguous():
    done = {"2026-01-07": {}, "2026-01-08": {}}
    todo = ["2026-01-05", "2026-01-06", "2026-01-09", "2026-01-12"]
    assert sim._todo_order(todo, done) == ["2026-01-09", "2026-01-12", "2026-01-06", "2026-01-05"]
    assert sim._todo_order(todo, {}) == todo


def test_servable_rules():
    log = daylog.Log()
    for d in ("2026-11-16", "2026-11-17", "2026-11-18"):
        log.add("bars", "", d, {"2330": [1, 2, 0.5, 1.5]})
        log.add("sig", "k", d, {"buys": []})
    now = pd.Timestamp("2026-11-19 20:00").to_pydatetime()  # 週四晚上，最新應有資料日 11/19
    assert log.servable("k", "2026-11-17", now) == ["2026-11-17", "2026-11-18"]  # 只落後一天：可以
    assert log.servable("k", "2026-11-14", now) == ["2026-11-16", "2026-11-17", "2026-11-18"]  # 開始日在週末
    assert log.servable("k", "2026-11-12", now) is None  # 開始日比紀錄早
    assert log.servable("other", "2026-11-17", now) is None
    late = pd.Timestamp("2026-11-23 20:00").to_pydatetime()
    assert log.servable("k", "2026-11-17", late) is None  # 紀錄落後好幾個交易日


def test_big_payload_is_split_and_merged(monkeypatch):
    monkeypatch.setattr(daylog, "CELL_LIMIT", 300)
    bars = {f"{1000 + i}": [1.0, 2.0, 0.5, 1.5] for i in range(60)}
    daylog.append([("bars", "", "2026-01-05", bars), ("sig", "k", "2026-01-05", {"buys": [], "n": 60})])
    daylog.forget()
    log = daylog.load()
    assert log.bars["2026-01-05"] == bars and "2026-01-05" in log.done("k")


def test_young_stock_is_not_refetched_every_replay(tmp_path, monkeypatch):
    """新上市的股票歷史很短，但快取記得「當初是從 5 年前抓起」→ 不必每次回放都重抓、也不算進預估次數。"""
    import json

    from stock_strategies import cache, data

    monkeypatch.setattr(cache, "_cache_dir", lambda: tmp_path)
    today = pd.Timestamp.now().normalize()
    dates = pd.bdate_range(end=today, periods=150)
    fetched = []

    def fake_get(params, timeout, max_retries):
        fetched.append(params["start_date"])
        return {"data": [{"date": d.strftime("%Y-%m-%d"), "stock_id": "00999", "open": 10, "max": 11, "min": 9,
                          "close": 10, "Trading_Volume": 1000} for d in dates]}

    monkeypatch.setattr(cache, "_rate_limited_get", fake_get)
    raw = sim._full_history("00999", 4)
    assert len(raw) == 150 and len(fetched) == 1  # 以前會因為「資料不夠長」再抓第二次
    meta = json.loads(cache._meta_path("TaiwanStockPrice", "00999").read_text())
    assert meta["since"] <= (today - pd.Timedelta(days=365 * 5)).strftime("%Y-%m-%d")
    sim._full_history("00999", 4)
    assert len(fetched) == 1
    monkeypatch.setattr(sim, "_fund_calls", lambda sid: 0)
    monkeypatch.setattr(cache, "_is_fresh", lambda *a, **k: True)
    assert sim.estimate_finmind_calls(4, ["00999"])["price"] == 0


def test_google_sheet_storage_roundtrip(monkeypatch):
    """有設 Google Sheet 時存在 BacktestLog 分頁：第一次自動建分頁，之後只往下加列。"""
    import gspread

    from stock_strategies import sheet

    class FakeWS:
        def __init__(self):
            self.rows = []

        def update(self, range_name=None, values=None, value_input_option=None):
            self.rows[:1] = values

        def append_rows(self, rows, value_input_option=None, insert_data_option=None):
            assert all(isinstance(c, str) for r in rows for c in r) and all(len(r[3]) < 50_000 for r in rows)
            self.rows += rows

        def get_all_values(self):
            return [list(r) for r in self.rows]

    class FakeSheet:
        def __init__(self):
            self.tabs = {}

        def worksheet(self, title):
            if title not in self.tabs:
                raise gspread.WorksheetNotFound(title)
            return self.tabs[title]

        def add_worksheet(self, title, rows, cols):
            self.tabs[title] = FakeWS()
            return self.tabs[title]

    fake = FakeSheet()
    monkeypatch.setenv("GOOGLE_SHEET_ID", "x")
    monkeypatch.setenv("GOOGLE_CREDS_JSON", "{}")
    monkeypatch.setattr(sheet, "get_gsheet", lambda: fake)
    daylog.append([("bars", "", "2026-01-05", {"2330": [1.0, 2.0, 0.5, 1.5]}),
                   ("sig", "k", "2026-01-05", {"buys": [{"stock_id": "2330", "name": "台積電"}], "n": 1})])
    daylog.touch(STRATEGY)
    ws = fake.tabs[daylog.TAB]
    assert ws.rows[0] == daylog.HEADER and len(ws.rows) == 4
    daylog.forget()
    log = daylog.load()
    assert log.done("k")["2026-01-05"]["buys"][0]["name"] == "台積電"
    assert daylog.recent_strategies(log) == []  # 這組參數還沒有任何紀錄 → 每日排程不用管
    daylog.append([("sig", daylog.strategy_key(STRATEGY), "2026-01-05", {"buys": [], "n": 1})])
    assert [s["id"] for s in daylog.recent_strategies(daylog.load(fresh=True))] == ["default"]
