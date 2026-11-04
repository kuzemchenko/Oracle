# -*- coding: utf-8 -*-
"""Агент свободного диалога с инструментами (ops/bot_agent.py, решение владельца 04.11 «без рельсов»).

Сеть замокана: модель — фейковый post, HTTP — фейковый http_get, NewsAPI — фейковый post.
Закрепляют: цикл инструментов, выгрузку файлом, вычистку секретов, базу только на чтение, закрытые
файлы, запрет внутренней сети, потолок трат, интеграцию с ботом (файл уходит документом)."""
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))

import bot_agent as A    # noqa: E402

SECRET = "EODHDsecret123456789"


def _msg(content=None, calls=None):
    m = {"role": "assistant", "content": content}
    if calls:
        m["tool_calls"] = [{"id": f"c{i}", "type": "function",
                            "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
    return {"choices": [{"message": m}], "usage": {"cost": 0.01}}


class FakeLLM:
    def __init__(self, script):
        self.script = list(script)
        self.bodies = []

    def __call__(self, body):
        self.bodies.append(json.loads(json.dumps(body)))
        return self.script.pop(0)


def _agent(script, tools=None):
    llm = FakeLLM(script)
    ag = A.Agent(post=llm, tools=tools or A.Tools(), api_key="x", log_cost=lambda *a, **k: None,
                 sleep=lambda s: None)
    return ag, llm


def _eod_http(calls):
    def http_get(url, params=None, timeout=None, headers=None, max_bytes=None):
        calls.append((url, dict(params or {})))
        if "/eod/" in url:
            rows = [{"date": "2026-11-02", "close": 13.1, "adjusted_close": 12.9},
                    {"date": "2026-11-03", "close": 13.4, "adjusted_close": 13.2}]
            return 200, "application/json", json.dumps(rows).encode()
        return 404, "text/plain", f"not found api_token={SECRET}".encode()
    return http_get


def test_agent_fetches_eodhd_and_sends_csv(monkeypatch):
    monkeypatch.setenv("EODHD_API_KEY", SECRET)
    calls = []
    tools = A.Tools(http_get=_eod_http(calls))
    ag, llm = _agent([
        _msg(calls=[("eodhd", {"endpoint": "eod/DHT.US", "params": {"from": "2026-01-01"}})]),
        _msg(calls=[("send_file", {"result_id": "r1", "filename": "DHT prices", "fmt": "csv",
                                   "note": "USD за акцию"})]),
        _msg("Готово: цены DHT в файле."),
    ], tools)
    text, files, cost = ag.run("выгрузи цены DHT", history=[], brief="")
    assert text == "Готово: цены DHT в файле."
    assert calls[0][0].endswith("/eod/DHT.US") and calls[0][1]["api_token"] == SECRET
    assert calls[0][1]["from"] == "2026-01-01"
    assert len(files) == 1 and files[0][0] == "DHT_prices.csv"
    body = files[0][1].decode()
    assert body.splitlines()[0] == "date,close,adjusted_close" and "13.4" in body
    assert "USD за акцию" in files[0][2] and SECRET not in files[0][2]
    # ключ не попадает в то, что видит модель
    assert all(SECRET not in json.dumps(b) for b in llm.bodies)
    assert abs(cost - 0.03) < 1e-9
    # инструменты объявлены модели
    assert {t["function"]["name"] for t in llm.bodies[0]["tools"]} >= {"eodhd", "newsapi", "send_file", "sql"}


def test_errors_are_redacted(monkeypatch):
    monkeypatch.setenv("EODHD_API_KEY", SECRET)
    tools = A.Tools(http_get=_eod_http([]))
    out = tools.eodhd("div/XXX.US")
    assert "HTTP 404" in out and SECRET not in out
    assert A.redact("https://x/?api_token=abc123&fmt=json") == "https://x/?api_token=***&fmt=json"


def test_no_key_is_honest(monkeypatch):
    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    monkeypatch.delenv("EODHD_KEY", raising=False)
    assert A.Tools().eodhd("eod/DHT.US").startswith("нет данных")


def test_sql_read_only(tmp_path):
    db = tmp_path / "o.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE quotes(symbol TEXT, date TEXT, close REAL)")
    con.execute("INSERT INTO quotes VALUES('DHT.US','2026-11-03',13.4)")
    con.commit()
    con.close()
    t = A.Tools(db_path=db)
    out = t.sql("SELECT * FROM quotes")
    assert '"close": 13.4' in out
    assert t.sql("DELETE FROM quotes").startswith("ошибка")
    assert t.sql("SELECT 1; DROP TABLE quotes").startswith("ошибка")
    # даже если обойти проверку текста — соединение только на чтение
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        con.execute("INSERT INTO quotes VALUES('X','d',1)")
        raise AssertionError("запись прошла")
    except sqlite3.OperationalError:
        pass
    finally:
        con.close()


def test_secret_files_closed():
    t = A.Tools()
    for p in (".env", "ops/crontab.local.txt", ".git/config", "../etc/passwd", "storage/oracle.db",
              "keys/id_ed25519"):
        assert t.read_file(p).startswith(("отказ", "нет такого")), p
    assert "CLAUDE.md" in t.read_file("CLAUDE.md", limit=2)
    assert ".env" not in t.list_files(".*")


def test_web_get_blocks_internal_network():
    t = A.Tools(http_get=lambda *a, **k: (200, "text/html", b"<b>x</b>"))
    for u in ("http://127.0.0.1:8080/", "http://localhost/", "file:///etc/passwd", "http://10.0.0.1/"):
        assert t.web_get(u).startswith("отказ"), u


def test_send_file_dict_of_dicts_and_json():
    t = A.Tools()
    rid = t._store({"Earnings": {"Trend": {"2026-12-31": {"period": "+1q", "earningsEstimateAvg": 0.5},
                                           "2027-12-31": {"period": "+1y", "earningsEstimateAvg": 2.1}}}},
                   "EODHD fundamentals/DHT.US")
    assert "будет отправлен" in t.send_file(rid, "trend", "csv", path="Earnings.Trend")
    rows = t.files[0][1].decode().splitlines()
    assert rows[0].startswith("key,period,earningsEstimateAvg") and len(rows) == 3
    t.send_file(rid, "trend", "json")
    j = json.loads(t.files[1][1])
    assert j["meta"]["источник"].startswith("EODHD") and "получено_utc" in j["meta"]
    assert t.send_file("r99", "x").startswith("нет результата")


def test_newsapi_compacts(monkeypatch):
    monkeypatch.setenv("NEWSAPI_AI_KEY", "newsKEY12345678")
    seen = {}

    def post(path, payload):
        seen["path"], seen["payload"] = path, payload
        return {"articles": {"results": [{"dateTime": "2026-11-04T10:00:00Z", "title": "Tankers rally",
                                          "source": {"title": "Reuters"}, "url": "https://r/x", "body": "b" * 900}]}}
    t = A.Tools(newsapi_post=post)
    out = t.newsapi(keyword="DHT Holdings", date_start="2026-11-01")
    assert seen["path"] == "article/getArticles" and "Tankers rally" in out and "newsKEY12345678" not in out
    assert len(t.results["r1"]["data"][0]["text"]) == 500


def test_turn_cost_cap_forces_answer(monkeypatch):
    monkeypatch.setattr(A, "MAX_TURN_COST_USD", 0.015)
    t = A.Tools(http_get=lambda *a, **k: (200, "application/json", b"[]"))
    monkeypatch.setenv("EODHD_API_KEY", SECRET)
    ag, llm = _agent([
        {"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "a", "function": {"name": "eodhd", "arguments": '{"endpoint":"eod/X.US"}'}}]}}],
         "usage": {"cost": 0.02}},
        _msg("по собранному: данных нет"),
    ], t)
    text, files, cost = ag.run("q")
    assert text == "по собранному: данных нет"
    assert llm.bodies[-1]["tool_choice"] == "none"


def test_unknown_tool_and_bad_args():
    t = A.Tools()
    assert t.call("rm_rf", {}).startswith("нет инструмента")
    assert t.call("sql", {"nope": 1}).startswith("неверные аргументы")


def test_court_check_goes_through_bot_action():
    got = []
    t = A.Tools(actions={"court_check": got.append})
    assert "суд запущен" in t.court_check("DHT — ставки фрахта")
    assert got == ["DHT — ставки фрахта"]


# ── интеграция с ботом ─────────────────────────────────────────────────────────────
def test_bot_routes_free_text_to_agent_and_sends_files(tmp_path, monkeypatch):
    import bot_state as S
    import bot as BOT
    monkeypatch.setattr(S, "STATE_PATH", tmp_path / "bot_state.json")

    class TG:
        def __init__(self):
            self.sent, self.docs = [], []

        def send_message(self, chat, text, reply_markup=None):
            self.sent.append((chat, text))
            return 1

        def send_document(self, chat, name, data, caption=None):
            self.docs.append((name, data, caption))
            return 2

    tg = TG()
    b = BOT.Bot(tg, "555", state=S._default_state())
    monkeypatch.setattr(b, "_budget_status", lambda: {"exit_code": 0})
    seen = {}

    def fake_answer(text, history=None, actions=None):
        seen["text"], seen["actions"] = text, actions
        return "Вот выгрузка DHT.", [("DHT.csv", b"date,close\n", "DHT.csv: EODHD")], 0.05
    monkeypatch.setattr(A, "answer", fake_answer)
    b._on_message({"chat": {"id": 555}, "text": "разбери DHT и выгрузи цены с января"})
    assert seen["text"].startswith("разбери DHT")           # «разбери ТИКЕР» больше не уходит в тяжёлый контур
    assert "court_check" in seen["actions"]
    assert tg.docs and tg.docs[0][0] == "DHT.csv"
    assert any("Вот выгрузка DHT." in t for _c, t in tg.sent)
    assert b.state["chat_history"][-1]["text"] == "Вот выгрузка DHT."


def test_resolve_summary_names_waiting_hunter_ideas():
    """04.11 владелец: «/resolve опять пишет, что не запущен» — сводка называет, ЧТО ждёт и до какой даты."""
    import bot as BOT
    s = {"сверено_сейчас": 0, "ещё_pending": 222, "всего_исходов": 3676, "hunter_трек": {"исходов": 0},
         "hunter_ждут": [{"asset": "MP.US", "resolve_by": "2026-11-25", "threshold": 47.5, "direction": "above"}]}
    t = BOT.resolve_summary_text(s)
    assert "MP — цена выше $47.5, проверка 25.11" in t and "срок ещё не наступил" in t
    t0 = BOT.resolve_summary_text({"hunter_трек": {"исходов": 0}})
    assert "для обсуждения" in t0


def test_answer_logs_turn_with_tools(monkeypatch, tmp_path):
    """Журнал разговора: видно с GitHub, какие инструменты агент реально вызвал; ключей в нём нет."""
    monkeypatch.setenv("EODHD_API_KEY", SECRET)
    log = tmp_path / "chat.jsonl"
    monkeypatch.setattr(A, "CHAT_LOG", log)
    ag, _llm = _agent([_msg(calls=[("eodhd", {"endpoint": "eod/DHT.US", "params": {"api_token": SECRET}})]),
                       _msg("ок")], A.Tools(http_get=_eod_http([])))
    text, files, cost = A.answer("цены DHT", history=[], agent=ag)
    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["ok"] and rec["tools"][0]["tool"] == "eodhd" and SECRET not in log.read_text()
    assert rec["question"] == "цены DHT" and rec["reply"] == "ок"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    try:
        A.answer("без ключа")
        raise AssertionError("должно упасть")
    except RuntimeError:
        pass
    assert json.loads(log.read_text().splitlines()[-1])["ok"] is False
