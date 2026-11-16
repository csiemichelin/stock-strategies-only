"""FinMind 額度：財報快取別每次重抓、空資料也要記住、額度用完快速失敗。"""
import json

import pytest

from stock_strategies import cache


class Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload

    def raise_for_status(self):
        pass


@pytest.fixture
def calls(monkeypatch):
    log = []

    def fake_get(url, params=None, timeout=None):
        log.append(params["dataset"])
        if params["dataset"] == "TaiwanStockFinancialStatements" and params["data_id"] == "0050":
            return Resp({"status": 200, "data": []})  # ETF 沒有財報
        return Resp({"status": 200, "data": [{"date": "2026-06-30", "type": "EPS", "value": 5}]})

    monkeypatch.setattr(cache.requests, "get", fake_get)
    monkeypatch.setattr(cache, "_quota_block_until", 0.0)
    return log


def test_quarterly_data_recently_fetched_is_fresh(calls):
    for _ in range(3):
        df = cache.fetch_finmind_cached("TaiwanStockFinancialStatements", "2330", "2022-01-01")
    assert len(df) == 1 and calls == ["TaiwanStockFinancialStatements"]  # 季末日很舊，但剛抓過 → 不重抓


def test_quarterly_data_refetched_after_a_few_days(calls):
    cache.fetch_finmind_cached("TaiwanStockFinancialStatements", "2330", "2022-01-01")
    meta = cache._meta_path("TaiwanStockFinancialStatements", "2330")
    m = json.loads(meta.read_text())
    m["fetched_at"] = "2026-01-01T00:00:00+00:00"
    meta.write_text(json.dumps(m))
    cache.fetch_finmind_cached("TaiwanStockFinancialStatements", "2330", "2022-01-01")
    assert len(calls) == 2


def test_empty_result_is_remembered(calls):
    for _ in range(3):
        df = cache.fetch_finmind_cached("TaiwanStockFinancialStatements", "0050", "2022-01-01")
    assert df.empty and calls == ["TaiwanStockFinancialStatements"]


def test_quota_exhausted_fails_fast(monkeypatch):
    import time

    monkeypatch.setattr(cache, "_quota_block_until", 0.0)
    monkeypatch.setattr(cache.time, "sleep", lambda s: None)
    hits = []

    def limited(url, params=None, timeout=None):
        hits.append(1)
        return Resp({"status": 402, "msg": "Requests reach the upper limit"}, 402)

    monkeypatch.setattr(cache.requests, "get", limited)
    with pytest.raises(cache.FinMindRateLimitError, match="額度"):
        cache.fetch_finmind_cached("TaiwanStockPrice", "2330", "2025-01-01")
    n = len(hits)
    t = time.monotonic()
    with pytest.raises(cache.FinMindRateLimitError, match="分鐘後再試"):
        cache.fetch_finmind_cached("TaiwanStockPrice", "2317", "2025-01-01")
    assert len(hits) == n and time.monotonic() - t < 0.5  # 第二檔不再打 API、不再等
