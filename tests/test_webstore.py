from stock_strategies import webstore


class FakeWS:
    def __init__(self):
        self.rows, self.col_count, self.row_count = [], 30, 20

    def col_values(self, c):
        return [r[c - 1] if len(r) >= c else "" for r in self.rows]

    def row_values(self, i):
        row = list(self.rows[i - 1])
        while row and row[-1] == "":
            row.pop()
        return row

    def add_cols(self, n):
        self.col_count += n

    def add_rows(self, n):
        self.row_count += n

    def update(self, range_name, values, **_):
        i = int("".join(ch for ch in range_name.split(":")[0] if ch.isdigit()))
        while len(self.rows) < i:
            self.rows.append([])
        self.rows[i - 1] = list(values[0])


def test_local_roundtrip():
    assert webstore.load("x") is None
    webstore.save("x", {"a": [1, 2, "三"]})
    webstore._memory.clear()
    assert webstore.load("x") == {"a": [1, 2, "三"]}


def test_sheet_chunks_and_overwrite(monkeypatch):
    ws = FakeWS()
    monkeypatch.setenv("GOOGLE_SHEET_ID", "x")
    monkeypatch.setenv("GOOGLE_CREDS_JSON", "{}")
    monkeypatch.setattr(webstore, "_worksheet", lambda: ws)
    monkeypatch.setattr(webstore, "CHUNK", 10)
    big = {"rows": ["股票" * 20] * 5}
    webstore.save("other", {"k": 1})
    webstore.save("last_run", big)
    assert len(ws.rows[1]) > 10  # 分段存成很多格
    webstore._memory.clear()
    assert webstore.load("last_run") == big and webstore.load("other") == {"k": 1}
    webstore.save("last_run", {"small": True})  # 變短：舊的尾巴要清掉
    webstore._memory.clear()
    assert webstore.load("last_run") == {"small": True}
