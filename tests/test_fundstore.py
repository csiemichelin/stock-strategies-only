"""基本面快照：所有地方共用，7 天內不重抓 FinMind。"""
from datetime import timedelta

import pandas as pd

from stock_strategies import data, fundstore


def _fake_finmind(calls, empty=False):
    def fetch(dataset, stock_id, start, **kw):
        calls.append(dataset)
        if empty:
            return pd.DataFrame()
        if dataset == "TaiwanStockFinancialStatements":
            rows = []
            for y in (2024, 2025):
                for q in ("03-31", "06-30", "09-30", "12-31"):
                    rows += [{"date": f"{y}-{q}", "type": "EPS", "value": 2.5},
                             {"date": f"{y}-{q}", "type": "TotalConsolidatedProfitForThePeriod", "value": 100.0}]
            return pd.DataFrame(rows)
        return pd.DataFrame([{"date": f"{y}-12-31", "type": "EquityAttributableToOwnersOfParent", "value": 2000.0}
                             for y in (2024, 2025)])
    return fetch


def test_snapshot_is_shared_and_saves_finmind_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(data, "fetch_finmind_cached", _fake_finmind(calls))
    first = data.get_fundamental("2330")
    assert len(calls) == 2 and first["eps"]
    assert data.get_fundamental("2330") == first and len(calls) == 2  # 同一個行程：直接用
    assert fundstore.flush() == 1
    fundstore.reset()  # 換一台機器／重新部署：從共用快照讀
    again = data.get_fundamental("2330")
    assert len(calls) == 2
    assert again == {k: {int(y): v for y, v in first[k].items()} for k in ("eps", "roe")}


def test_snapshot_expires(monkeypatch):
    calls = []
    monkeypatch.setattr(data, "fetch_finmind_cached", _fake_finmind(calls))
    data.get_fundamental("2330")
    today = fundstore._today()
    monkeypatch.setattr(fundstore, "_today", lambda: today + timedelta(days=fundstore.MAX_AGE_DAYS + 1))
    data.get_fundamental("2330")
    assert len(calls) == 4  # 過期重抓


def test_empty_answer_does_not_overwrite_good_snapshot(monkeypatch):
    calls = []
    monkeypatch.setattr(data, "fetch_finmind_cached", _fake_finmind(calls))
    good = data.get_fundamental("2330")
    today = fundstore._today()
    monkeypatch.setattr(fundstore, "_today", lambda: today + timedelta(days=fundstore.MAX_AGE_DAYS + 1))
    monkeypatch.setattr(data, "fetch_finmind_cached", _fake_finmind(calls, empty=True))
    assert data.get_fundamental("2330") == {"eps": {}, "roe": {}}  # 這次照 FinMind 的回答
    monkeypatch.setattr(fundstore, "_today", lambda: today)
    assert data.get_fundamental("2330") == good  # 但沒有把好的快照蓋掉


def test_etf_without_statements_is_remembered(monkeypatch):
    calls = []
    monkeypatch.setattr(data, "fetch_finmind_cached", _fake_finmind(calls, empty=True))
    assert data.get_fundamental("0050") == {"eps": {}, "roe": {}}
    n = len(calls)
    assert data.get_fundamental("0050") == {"eps": {}, "roe": {}} and len(calls) == n


def test_can_be_disabled_and_nan_roundtrip(monkeypatch):
    fundstore.put("1101", {"eps": {2024: float("nan"), 2025: 1.5}, "roe": {}})
    got = fundstore.get("1101")
    assert got["eps"][2025] == 1.5 and got["eps"][2024] != got["eps"][2024]
    monkeypatch.setenv("FUND_SNAPSHOT", "0")
    assert fundstore.get("1101") is None
