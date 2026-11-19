"""共用測試 fixture。FinMind 一律 mock，不打真 API。"""
import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """每個測試用獨立快取目錄，避免互相污染。"""
    cache_dir = tmp_path / "finmind_cache"
    monkeypatch.setenv("FINMIND_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("FINMIND_TOKEN", "test-token")
    monkeypatch.setenv("PREWARM", "0")          # 不在測試裡背景打真 API
    monkeypatch.setenv("WEB_CACHE_TTL", "0")    # 回應快取另有專門測試
    monkeypatch.setenv("WEBSTORE_DIR", str(tmp_path / "webstore"))
    from stock_strategies import webstore
    webstore._memory.clear()
    from stock_strategies import cache, daylog, fundstore, sim
    monkeypatch.setattr(sim, "QUOTA_WAITER", None)      # 網頁回測會設成「等額度」，測試間不要互相影響
    monkeypatch.setattr(cache, "_quota_block_until", 0.0)
    daylog.forget()      # 回測紀錄、基本面快照都是行程內快取，每個測試從空的開始
    fundstore.reset()
    yield


def make_price_df(n=120, start="2023-01-02", base=100.0):
    """造一段遞增日 K，欄位符合 add_indicators 契約。"""
    dates = pd.bdate_range(start=start, periods=n)
    close = [base + i * 0.5 for i in range(n)]
    return pd.DataFrame({
        "date": dates,
        "open": [c - 0.3 for c in close],
        "high": [c + 0.6 for c in close],
        "low": [c - 0.6 for c in close],
        "close": close,
        "volume": [1000 + i for i in range(n)],
    })
