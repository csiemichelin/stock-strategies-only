"""證交所／公開資訊觀測站資料層：解析格式、只抓沒抓過的、中斷後接著抓。"""

import json

import pandas as pd
import pytest

from stock_strategies import twse

UP, DOWN, FLAT, X = "<p style= color:red>+</p>", "<p style= color:green>-</p>", "<p> </p>", "<p>X</p>"
FIELDS = ["證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價", "最高價", "最低價", "收盤價",
          "漲跌(+/-)", "漲跌價差", "最後揭示買價", "最後揭示買量", "最後揭示賣價", "最後揭示賣量", "本益比"]


def row(sid, name, close, sign=UP, value="1,000,000", open_=None):
    o = open_ if open_ is not None else close
    return [sid, name, "10,000", "5", value, o, close, o, close, sign, "0.50", close, "1", close, "1", "10.0"]


def quotes_body(rows):
    return {"stat": "OK", "tables": [
        {"title": "價格指數", "fields": ["指數", "收盤指數"], "data": [["發行量加權股價指數", "17,853.76"]]},
        {"title": "每日收盤行情", "fields": FIELDS, "data": rows}]}


class Reply:
    def __init__(self, body, status=200):
        self.status_code = status
        self.content = body if isinstance(body, bytes) else (
            body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)).encode("utf-8")


@pytest.fixture
def web(monkeypatch, tmp_path):
    """假的網站：routes 是 [(網址片段, 回應或函式)]，calls 記下每次請求的網址。"""
    monkeypatch.setenv("TWSE_CACHE_DIR", str(tmp_path / "twse"))
    monkeypatch.setattr(twse, "PAUSE", 0.0)
    monkeypatch.setattr(twse, "RETRY_WAIT", 0.0)
    monkeypatch.setattr(twse, "REUSE_MINUTES", 0.0)
    monkeypatch.setattr(twse.time, "sleep", lambda s: None)
    twse._last.clear()
    state = {"routes": [], "calls": []}

    def get(url, headers=None, timeout=None):
        state["calls"].append(url)
        for part, reply in state["routes"]:
            if part in url:
                out = reply(url) if callable(reply) else reply
                return out if isinstance(out, Reply) else Reply(out)
        return Reply("not found", 404)

    monkeypatch.setattr(twse.requests, "get", get)
    return state


def test_parse_quotes_keeps_traded_common_stocks_and_marks_reference_changes():
    body = quotes_body([
        row("2330", "台積電", "919.00", X, "54,980,784,829", "923.00"),
        row("0050", "元大台灣50", "178.95"),
        row("1101", "台泥", "33.45", DOWN),
        row("00878", "國泰永續高股息", "22.00"),
        row("9105", "泰金寶-DR", "5.00"),
        row("2881A", "富邦特", "62.00"),
        row("1234", "黑松", "--", FLAT),
    ])
    names = {}
    df = twse.parse_quotes(body, "2024-06-13", names)
    assert list(df["id"]) == ["2330", "0050", "1101"]
    tsmc = df.iloc[0]
    assert (tsmc["open"], tsmc["close"], tsmc["value"], tsmc["flag"]) == (923.0, 919.0, 54980784829.0, twse.FLAG_X)
    assert list(df["flag"]) == [twse.FLAG_X, 1, -1]
    assert names == {"2330": "台積電", "0050": "元大台灣50", "1101": "台泥"}


def test_parse_quotes_without_a_table_is_a_day_without_data():
    assert twse.parse_quotes({"stat": "很抱歉，沒有符合條件的資料!", "type": "ALLBUT0999"}, "2024-01-01") is None


def test_republic_of_china_dates():
    assert twse._roc_date("113/01/02") == "2024-01-02"
    assert twse._roc_date("105年09月01日") == "2016-09-01"
    assert twse._roc_date("合計") is None


def test_calendar_asks_once_for_finished_months(web, monkeypatch):
    monkeypatch.setattr(twse, "_now", lambda: pd.Timestamp("2024-02-15 18:00", tz="Asia/Taipei").to_pydatetime())
    web["routes"] = [
        ("FMTQIK?date=20240101", {"stat": "OK", "data": [["113/01/02", "1", "2", "3", "17,853.76", "-77.05"],
                                                           ["113/01/03", "1", "2", "3", "17,559.31", "-294.45"]]}),
        ("FMTQIK?date=20240201", {"stat": "OK", "data": [["113/02/15", "1", "2", "3", "18,644.57", "548.83"]]}),
    ]
    want = [("2024-01-02", 17853.76), ("2024-01-03", 17559.31), ("2024-02-15", 18644.57)]
    assert twse.calendar("2024-01-01") == want
    assert len(web["calls"]) == 2
    assert twse.calendar("2024-01-03", "2024-02-15") == want[1:]
    assert [u.split("date=")[1][:8] for u in web["calls"][2:]] == ["20240201"]  # 一月已經過完，不用再問


def test_quotes_are_downloaded_once_and_resume_after_a_failure(web):
    days = ["2023-12-28", "2023-12-29", "2024-01-02", "2024-01-03"]
    ok = quotes_body([row("2330", "台積電", "600.00"), row("1101", "台泥", "33.00")])
    broken = {"on": True}
    web["routes"] = [("MI_INDEX?date=20240102", lambda url: Reply("<html>blocked</html>") if broken["on"] else Reply(ok)),
                     ("MI_INDEX", ok)]
    with pytest.raises(RuntimeError):
        twse.load_quotes(days)
    broken["on"] = False
    web["calls"].clear()
    df = twse.load_quotes(days)
    assert sorted(df["date"].unique()) == days and len(df) == 8
    asked = [u.split("date=")[1][:8] for u in web["calls"]]
    assert asked == ["20240102", "20240103"]  # 去年那兩天已經存好了
    web["calls"].clear()
    assert len(twse.load_quotes(days[:2])) == 4 and web["calls"] == []
    assert twse.names()["2330"] == "台積電"


def test_a_day_that_is_not_published_yet_is_asked_again_next_time(web):
    web["routes"] = [("MI_INDEX?date=20240103", {"stat": "很抱歉，沒有符合條件的資料!"}),
                     ("MI_INDEX", quotes_body([row("2330", "台積電", "600.00")]))]
    assert list(twse.load_quotes(["2024-01-02", "2024-01-03"])["date"]) == ["2024-01-02"]
    web["calls"].clear()
    twse.load_quotes(["2024-01-02", "2024-01-03"])
    assert [u.split("date=")[1][:8] for u in web["calls"]] == ["20240103"]


def test_exrights_give_reference_over_previous_close(web, monkeypatch):
    monkeypatch.setattr(twse, "_now", lambda: pd.Timestamp("2025-03-01 09:00", tz="Asia/Taipei").to_pydatetime())
    web["routes"] = [
        ("TWT49U?startDate=20240101", {"stat": "OK", "data": [
            ["113年07月01日", "1101", "台泥", "34.20", "33.20", "1.000000", "息"],
            ["113年07月01日", "1101B", "台泥乙特", "48.40", "46.63", "1.763623", "息"],
            ["113年06月13日", "2330", "台積電", "923.00", "919.00", "4.000000", "息"]]}),
        ("TWT49U?startDate=20250101", {"stat": "OK", "data": []}),
    ]
    out = twse.exrights("2024-01-01")
    assert set(out) == {"1101", "2330"}
    assert out["1101"] == [("2024-07-01", pytest.approx(33.20 / 34.20))]
    assert "endDate=20250301" in web["calls"][1]  # 今年只問到今天
    web["calls"].clear()
    twse.exrights("2024-01-01")
    assert len(web["calls"]) == 1 and "20250101" in web["calls"][0]  # 去年的不會再變


REVENUE_HTML = (
    "<table><tr><th>公司代號</th><th>公司名稱</th><th>當月營收</th></tr>"
    "<tr align=right><td align=center>1101</td><td align=left>台泥</td><td nowrap>   7,237,930</td>"
    "<td nowrap>9,514,039</td><td nowrap>7,325,221</td><td nowrap>-23.92</td><Td nowrap>-1.19</td>"
    "<td nowrap>7,237,930</td><td nowrap>7,325,221</td><td nowrap>-1.19</td><td align=center>-</td></tr>"
    "<tr align=right><th colspan=2>合計</th><td>16,000,000</td><td>1</td><td>2</td><td>&nbsp;</td></tr>"
    "<tr align=right><td align=center>1102</td><td align=left>亞泥</td><td nowrap>6,490,566</td>"
    "<td nowrap>6,313,936</td><td nowrap>4,814,494</td><td nowrap>2.79</td><Td nowrap>34.81</td></tr></table>")


def test_parse_revenue_reads_this_month_column():
    assert twse.parse_revenue(REVENUE_HTML) == {"1101": 7237930.0, "1102": 6490566.0}


def test_revenue_merges_domestic_and_foreign_listings_and_keeps_settled_months(web, monkeypatch):
    monkeypatch.setattr(twse, "_now", lambda: pd.Timestamp("2024-03-12 09:00", tz="Asia/Taipei").to_pydatetime())
    ky = "<table><tr><td>1256</td><td>鮮活果汁-KY</td><td>426,241</td><td>273,921</td><td>178,183</td></tr></table>"
    web["routes"] = [("t21sc03_113_1_0", REVENUE_HTML.encode("big5")), ("t21sc03_113_1_1", ky.encode("big5")),
                     ("t21sc03_113_2_0", REVENUE_HTML.encode("big5")), ("t21sc03_113_2_1", ky.encode("big5"))]
    out = twse.revenue("2024-01")
    assert out["1256"] == {"2024-01": 426241.0, "2024-02": 426241.0}
    assert out["1101"]["2024-01"] == 7237930.0
    assert len(web["calls"]) == 4
    web["calls"].clear()
    twse.revenue("2024-01")
    assert all("113_2_" in u for u in web["calls"]) and len(web["calls"]) == 2  # 一月定案了；二月還可能有人晚申報，再抓一次


def test_a_blocked_response_is_retried(web):
    replies = iter(["<html>稍後再試</html>", "<html>稍後再試</html>", {"stat": "OK", "data": []}])
    web["routes"] = [("FMTQIK", lambda url: Reply(next(replies)))]
    assert twse._get_json(twse.BASE + "afterTrading/FMTQIK?date=20240101&response=json") == {"stat": "OK", "data": []}
    assert len(web["calls"]) == 3


def test_revenue_stops_when_the_site_blocks_us_and_uses_open_data_for_the_latest_month(web, monkeypatch):
    monkeypatch.setattr(twse, "_now", lambda: pd.Timestamp("2024-03-12 20:00", tz="Asia/Taipei").to_pydatetime())
    open_data = [{"資料年月": "11302", "公司代號": "1101", "營業收入-當月營收": "5000000"},
                 {"資料年月": "11302", "公司代號": "2330", "營業收入-當月營收": "181648270"},
                 {"資料年月": "11301", "公司代號": "9999", "營業收入-當月營收": "1"}]
    web["routes"] = [("t21sc03_112_12_0", REVENUE_HTML.encode("big5")), ("t21sc03_112_12_1", Reply("", 404)),
                     ("t21sc03", Reply("Bad Gateway", 502)), ("t187ap05_L", open_data)]
    out = twse.revenue("2023-12")
    assert out["1101"] == {"2023-12": 7237930.0, "2024-02": 5000000.0}   # 12 月抓到了（KY 那張不存在不算被擋）
    assert out["2330"] == {"2024-02": 181648270.0} and "9999" not in out  # 2 月用開放資料補；1 月被擋、開放資料不補舊月份
    assert twse.revenue_gaps(out, "2023-12", "2024-02", floor=1) == ["2024-01"]
    asked = [u for u in web["calls"] if "t21sc03" in u]
    assert sum("113_1_" in u for u in asked) == 2 and not any("113_2_" in u for u in asked)  # 被擋之後不再硬抓下個月


def test_patient_revenue_download_rests_and_then_continues(web, monkeypatch):
    monkeypatch.setattr(twse, "_now", lambda: pd.Timestamp("2024-03-12 20:00", tz="Asia/Taipei").to_pydatetime())
    rests = []
    monkeypatch.setattr(twse.time, "sleep", rests.append)
    blocked = {"left": 4}                                              # 前 4 次請求被擋（每個月重試 2 次 → 休息兩回）

    def mops(url):
        if blocked["left"] > 0:
            blocked["left"] -= 1
            return Reply("Bad Gateway", 502)
        return Reply(REVENUE_HTML.encode("big5"))

    web["routes"] = [("t21sc03", mops)]
    out = twse.revenue("2024-01", patient=True)
    assert out["1101"] == {"2024-01": 7237930.0, "2024-02": 7237930.0}
    assert rests.count(twse.MOPS_COOLDOWN) == 2


def test_data_fetched_minutes_ago_is_not_fetched_again_in_the_same_run(web, monkeypatch):
    clock = {"now": pd.Timestamp("2024-02-15 20:10", tz="Asia/Taipei")}
    monkeypatch.setattr(twse, "_now", lambda: clock["now"].to_pydatetime())
    monkeypatch.setattr(twse, "REUSE_MINUTES", 45.0)
    web["routes"] = [("FMTQIK", {"stat": "OK", "data": [["113/02/15", "1", "2", "3", "18,644.57", "548.83"]]})]
    twse.calendar("2024-02-01")
    clock["now"] += pd.Timedelta(minutes=20)
    twse.calendar("2024-02-01")
    assert len(web["calls"]) == 1                                      # 20 分鐘前才抓過
    clock["now"] += pd.Timedelta(hours=20)
    twse.calendar("2024-02-01")
    assert len(web["calls"]) == 2                                      # 隔天：這個月還沒過完，要再問一次


def test_stamp_changes_only_when_there_is_new_data(web, monkeypatch):
    monkeypatch.setattr(twse, "_now", lambda: pd.Timestamp("2024-03-12 20:00", tz="Asia/Taipei").to_pydatetime())
    assert twse.stamp() == "none-r0"
    web["routes"] = [("MI_INDEX", quotes_body([row("2330", "台積電", "600.00")])), ("t21sc03", REVENUE_HTML.encode("big5"))]
    twse.load_quotes(["2024-03-11", "2024-03-12"])
    twse.revenue("2024-01")
    assert twse.stamp() == "2024-03-12-r2"
    twse.load_quotes(["2024-03-11", "2024-03-12"])
    assert twse.stamp() == "2024-03-12-r2"
