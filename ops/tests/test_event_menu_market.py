# -*- coding: utf-8 -*-
"""Q-меню-рынок: раздел меню «Рынок: резкие движения» (ops/event_menu.py) и рыночные поводы M… для утреннего
«что покопать» (ops/dig_offer.py) за флагом event_menu_market. Герметично: цены и новости подставляются, база —
временный sqlite, модели и сети нет."""
import datetime
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ops"))
sys.path.insert(0, str(ROOT))

import event_menu as EM      # noqa: E402
import dig_offer as D        # noqa: E402

TODAY = datetime.date(2026, 11, 22)
LAST = datetime.date(2026, 11, 20)


def _series(tail, n=300, step=0.01, start=100.0, end=LAST):
    """Спокойный ряд (день +step, день −step) и хвост из заданных дневных ходов; даты подряд до end."""
    v = [start]
    for i in range(n):
        v.append(v[-1] * (1 + (step if i % 2 == 0 else -step)))
    for r in tail:
        v.append(v[-1] * (1 + r))
    return [((end - datetime.timedelta(days=len(v) - 1 - i)).isoformat(), c) for i, c in enumerate(v)]


CLOSES = {
    "SPY.US": _series([-0.01, -0.012, -0.015, -0.02, -0.036]),
    "QQQ.US": _series([0.002]),
    "XLK.US": _series([-0.07]),
    "XLE.US": _series([0.004]),
    "NVDA.US": _series([-0.14], step=0.02),
    "STEM.US": _series([-0.3], step=0.03),
    "AAA.US": _series([0.09]),
    "RY.US": _series([-0.045]),
    "UVXY.US": _series([0.25], step=0.04),
    "BITO.US": _series([0.98, 0.01, -0.01]),
    "OLD.US": _series([-0.2], end=LAST - datetime.timedelta(days=3)),
    "LMT": _series([-0.2]),
}
PAGES = {"NVDA.US": {"name": "NVIDIA Corporation", "page": "NVIDIA", "type": "Common Stock"},
         "STEM.US": {"name": "Stem Inc", "page": "Stem", "type": "Common Stock"},
         "RY.US": {"name": "Royal Bank of Canada", "page": "Royal_Bank_of_Canada", "type": "Common Stock"},
         "UVXY.US": {"name": "ProShares Ultra VIX ETF", "page": None, "почему": "тип ETF — у фонда нет «страницы толпы»"},
         "BITO.US": {"name": "ProShares Bitcoin ETF", "page": None, "почему": "тип ETF — у фонда нет «страницы толпы»"}}
RULES = {"карта": ["XLK.US", "XLE.US", "ITA.US"]}
FUND_NAMES = {"XLK.US": "технологии", "XLE.US": "нефть и газ", "ITA.US": "оборона и авиакосмос"}
NEWS = [  # (published_at, заголовок, url, домен, отпечаток) — по убыванию времени, как отдаёт база
    ("2026-11-22T05:30:00+00:00", "US financial watchdog probing Nvidia accounting", "https://a.example/1", "a.example", "f1"),
    ("2026-11-22T05:21:00+00:00", "US financial watchdog probing Nvidia accounting", "https://b.example/1", "b.example", "f1"),
    ("2026-11-22T05:00:00+00:00", "Where Will Nvidia Stock Be in 20 Years?", "https://a.example/2", "a.example", "f2"),
    ("2026-11-22T04:00:00+00:00", "Israeli envoy aims to stem damage after remarks", "https://c.example/1", "c.example", "f3"),
    ("2026-11-22T03:00:00+00:00", "Stem Cells Breakthrough Announced", "https://c.example/2", "c.example", "f4"),
    ("2026-11-22T02:00:00+00:00", "S&P 500 suffers longest losing streak since the pandemic", "https://d.example/1", "d.example", "f5"),
    ("2026-11-21T23:00:00+00:00", "Pagaya (NASDAQ:PGY) and TeraWulf (NASDAQ: WULF) Head to Head", "https://e.example/1", "e.example", "f6"),
    ("2026-11-21T22:00:00+00:00", "Stocks enter correction territory, driven by a deepening tech rout", "https://f.example/1", "f.example", "f7"),
    ("2026-11-21T07:15:00+00:00", "NVIDIA (NVDA) earnings strength collides with a sharp reset", "https://g.example/1", "g.example", "f8"),
    ("2026-11-20T07:00:00+00:00", "Nvidia, AMD got licenses for chips to China", "https://h.example/1", "h.example", "f9"),
]


def _market(**kw):
    args = dict(today=TODAY, data=(CLOSES, NEWS), rules=RULES, fund_name=lambda f: FUND_NAMES.get(f, f),
                pages=PAGES, curated={"NVDA.US": "Nvidia"})
    args.update(kw)
    return EM.market_menu(**args)


def _by(mk, key):
    return {it["бумага"]: it for it in mk[key]}


# ── раздел меню ──────────────────────────────────────────────────────────────────────────
def test_market_section_finds_sharp_moves_by_code():
    mk = _market()
    assert mk["цены_на"] == "2026-11-20" and "3 обычных колебаний" in mk["правило"] and "2%" in mk["правило"]
    idx = _by(mk, "рынок")
    assert list(idx) == ["SPY.US", "QQQ.US"]                                  # широкий рынок показан всегда
    assert idx["SPY.US"]["резкое"] and idx["SPY.US"]["вниз"] and not idx["QQQ.US"]["резкое"]
    assert idx["SPY.US"]["подпись"] == "фонд широкого рынка США S&P 500 (SPY.US)"
    assert "за день −3.6%" in idx["SPY.US"]["словами"] and "за 5 торговых дней −9.0%" in idx["SPY.US"]["словами"]
    assert "заголовки" not in idx["QQQ.US"]                                   # спокойной бумаге причину не ищем
    assert [it["бумага"] for it in mk["секторы"]] == ["XLK.US"]
    assert mk["секторы"][0]["подпись"] == "фонд сектора «технологии» (XLK.US)"
    assert mk["секторы_спокойные"] == ["нефть и газ"] and mk["секторы_не_измерены"] == ["оборона и авиакосмос"]
    comp = _by(mk, "компании")
    assert {"NVDA.US", "STEM.US", "AAA.US", "RY.US"} == set(comp)
    forces = [it["сила"] for it in mk["компании"]]
    assert forces == sorted(forces, reverse=True)                             # самые резкие — первыми
    assert comp["NVDA.US"]["подпись"] == "Nvidia (NVDA.US)" and comp["RY.US"]["подпись"] == "Royal Bank of Canada (RY.US)"
    assert comp["AAA.US"]["подпись"] == "AAA.US — компанию не опознал"         # имя не выдумываем (П8)
    assert [it["подпись"] for it in mk["фонды"]] == ["фонд ProShares Ultra VIX ETF (UVXY.US)"]
    assert mk["фонды"][0]["вниз"] is False
    # OLD.US отстал; «LMT» без биржи — не бумага; у BITO.US в окне разрыв ряда (+98% за день — не рынок)
    assert mk["цены_отстали"] == 1 and mk["ряд_с_разрывом"] == ["фонд ProShares Bitcoin ETF (BITO.US)"]
    assert all("LMT" != it["бумага"] and "OLD.US" != it["бумага"] for k in ("компании", "фонды") for it in mk[k])
    assert mk["счёт"]["компании"] == {"всего": 4, "резких": 4, "вниз": 3, "вверх": 1}
    assert mk["счёт"]["секторы"]["всего"] == 2 and mk["счёт"]["секторы"]["резких"] == 1


def test_headlines_are_fresh_unique_and_honest_when_absent():
    mk = _market()
    comp = _by(mk, "компании")
    heads = comp["NVDA.US"]["заголовки"]
    assert [h["url"] for h in heads] == ["https://a.example/1", "https://g.example/1", "https://h.example/1"]
    assert "новости" not in comp["NVDA.US"]                                   # повтор заголовка и второй с того же домена — мимо
    # «aims to stem damage» — не про компанию Stem; «Stem Cells…» без слова о рынке — тоже
    assert comp["STEM.US"].get("заголовки") == [] and comp["STEM.US"]["новости"] == EM.NO_NEWS
    assert comp["RY.US"]["новости"] == "причина в базе новостей не найдена"
    spy = _by(mk, "рынок")["SPY.US"]["заголовки"]
    assert [h["url"] for h in spy] == ["https://d.example/1"]
    assert [h["url"] for h in mk["секторы"][0]["заголовки"]] == ["https://f.example/1"]   # «tech rout» — сектор технологий


def test_headline_matcher_rules():
    nv = EM.headline_matcher("NVDA.US", names=["NVIDIA", "Nvidia"])
    assert nv("Why NVIDIA (NASDAQ:NVDA) faces pressure") and nv("nvda, ANT -- NVDA") and nv("Акции Nvidia упали")
    assert not nv("nvidia in lowercase is not a headline name") and not nv("NVDAX fund")
    al = EM.headline_matcher("ALL.US", names=["Allstate"])
    assert not al("ALL EYES ON THE FED") and al("Allstate (NYSE: ALL) raises dividend") and al("Insurer $ALL jumps")
    qq = EM.headline_matcher("QQQ.US", words=EM.FUND_WORDS["QQQ.US"])
    assert qq("Nasdaq plunges 5.3% as AI selloff snowballs") and qq("Nasdaq 100 ends worst week since 2020")
    assert not qq("Pagaya (NASDAQ:PGY) and TeraWulf (NASDAQ: WULF) Head to Head")
    long_title = "​Nvidia stock crash " + "очень длинный хвост " * 12 + "-"
    got = EM.find_headlines([("2026-11-21T10:00", long_title, "https://x.example/1", "x.example", None),
                             ("2026-11-21T09:00", "Nvidia без ссылки", None, "y.example", None)], nv)
    assert len(got) == 1 and got[0]["заголовок"].startswith("Nvidia stock crash") and got[0]["заголовок"].endswith("…")
    assert len(got[0]["заголовок"]) <= EM.MARKET_TITLE_MAX + 1 and got[0]["когда"] == "2026-11-21T10:00"
    assert EM.short_name("Mitsubishi UFJ Financial Group Inc ADR") == "Mitsubishi UFJ Financial"
    assert EM.short_name("NVIDIA Corporation") == "NVIDIA" and EM.short_name("AT&T Inc.") == "AT&T"


def test_stale_or_missing_prices_are_honest_no_data(tmp_path):
    old = _market(today=TODAY + datetime.timedelta(days=10))
    assert "ошибка" in old and "старше 5 дней" in old["ошибка"] and "рынок" not in old
    none = EM.market_menu(today=TODAY, db_path=tmp_path / "нет.db", rules=RULES, pages={}, curated={})
    assert none["ошибка"].startswith("нет данных") and "базы котировок" in none["ошибка"]
    assert "нет данных" in "\n".join(EM.render_market(none))
    assert EM.market_leads(old) == [] and EM.market_leads(None) == [] and EM.render_market(None) == []
    empty = EM.market_menu(today=TODAY, data=({}, []), rules=RULES, pages={}, curated={})
    assert "ошибка" in empty


def test_reads_prices_and_news_from_sqlite_read_only(tmp_path):
    db = tmp_path / "oracle.db"
    con = sqlite3.connect(db)
    con.execute("create table quotes (symbol text, date text, close real, adjusted_close real)")
    con.execute("create table news (published_at text, title text, url text, domain text, title_fp text, dup_of text)")
    for sym in ("SPY.US", "NVDA.US"):
        con.executemany("insert into quotes values (?,?,?,?)", [(sym, d, c, c if sym == "SPY.US" else None)
                                                               for d, c in CLOSES[sym]])
    con.executemany("insert into news values (?,?,?,?,?,?)", [
        ("2026-11-21T10:00:00+00:00", "Nvidia shares slide", "https://a.example/n", "a.example", "x1", None),
        ("2026-11-21T09:00:00+00:00", "Nvidia shares slide again", "https://b.example/n", "b.example", "x2", "x1"),
        ("2026-11-10T09:00:00+00:00", "Nvidia old story", "https://c.example/n", "c.example", "x3", None)])
    con.commit()
    con.close()
    before = db.read_bytes()
    mk = EM.market_menu(today=TODAY, db_path=db, rules={}, pages=PAGES, curated={})
    assert db.read_bytes() == before                                          # база только читается
    assert mk["цены_на"] == "2026-11-20" and [it["бумага"] for it in mk["рынок"]] == ["SPY.US"]
    nv = _by(mk, "компании")["NVDA.US"]                                       # цена без поправки тоже читается
    assert [h["url"] for h in nv["заголовки"]] == ["https://a.example/n"]     # дубль и новость старше 5 дней — мимо


def test_menu_file_gets_the_section_and_old_sections_do_not_change():
    base = {"собрано": "2026-11-22T21:00:00+00:00", "gdelt": {"ошибка": "нет данных: x"},
            "wiki": {"ошибка": "нет данных: 429"}, "newsapi": {"ошибка": "нет ключа"}}
    mk = _market()
    md0, md1 = EM.render_md(base), EM.render_md({**base, "рынок": mk})
    block = "\n".join(EM.render_market(mk)) + "\n"
    assert "Рынок" not in md0 and md1.replace(block, "") == md0               # прежние разделы — байт в байт
    assert "## Рынок: резкие движения (цены закрытия на 2026-11-20)" in md1
    assert "- Nvidia (NVDA.US) — РЕЗКОЕ, вниз: за день −14.0%" in md1
    assert "  - «US financial watchdog probing Nvidia accounting» — https://a.example/1" in md1
    assert "  - причина в базе новостей не найдена" in md1
    assert "- без резких движений: нефть и газ" in md1 and "BITO.US" in md1
    assert "без резких движений: за день +0.2%" in md1                        # спокойный Nasdaq показан числом
    json.dumps(mk, ensure_ascii=False)                                        # раздел пишется в menu_latest.json


def test_build_puts_market_first_and_survives_its_failure(monkeypatch):
    monkeypatch.setattr(EM, "market_menu", lambda: (_ for _ in ()).throw(RuntimeError("сбой")))
    m = EM.build(sources=("market",))
    assert list(m) == ["собрано", "рынок"] and m["рынок"]["ошибка"].startswith("нет данных: RuntimeError")
    assert "рынок" not in EM.build(sources=())


# ── поводы для «что покопать» за флагом ──────────────────────────────────────────────────
NOW = datetime.datetime(2026, 11, 23, 5, 0, tzinfo=datetime.timezone.utc)
MENU = {"собрано": "2026-11-22T21:00:19+00:00",
        "newsapi": {"события": [{"событие": "EU digital tax", "статей": 900, "дата": "2026-11-22", "uri": "eng-1"}]},
        "gdelt": {"резко_выросло": [{"что": "BALTIC", "статей_сутки": 700, "рост_раз": 4.0,
                                      "примеры": ["https://example.com/baltic"]}]},
        "wiki": {"резко_выросло": []}}


def _menu_with_market():
    return {**MENU, "рынок": _market()}


def _offer(**kw):
    o = {"тема": "Обвал производителей чипов после проверки Nvidia", "повод_ids": ["M4"],
         "что_случилось": "Бумаги Nvidia (NVDA.US) за день потеряли 14 процентов.",
         "круги": ["1-й круг: сама Nvidia и её поставщики"],
         "почему_дальше": "Проверка бухгалтерии может затянуться на кварталы.",
         "тип": "масштаб или срок", "почему_рынок_не_видит": "Срок проверки рынок закладывает коротким.",
         "что_опровергнет": "Если проверка закроется без претензий до конца года.", "аналог_id": None}
    o.update(kw)
    return o


class FakeClient:
    mode = "mock"

    def __init__(self, payload):
        self.payload, self.user = payload, None

    def complete(self, role, system, user, **kw):
        self.user = user
        return {"text": json.dumps(self.payload, ensure_ascii=False), "model": "fake", "cost": 0.0}


def _run(tmp_path, menu, payload, sub="a", **kw):
    client = FakeClient(payload)
    p, pr = D.run(mode="mock", now=NOW, client=client, menu=menu, attention_daily={}, attention_fast={},
                  out_dir=tmp_path / sub, decisions=tmp_path / "dec.jsonl", search_dir=tmp_path / "s",
                  episodes=[], **kw)
    return pr, client


def test_flag_reads_presentation_yaml_directly(tmp_path):
    assert D.MARKET_FLAG == "event_menu_market"
    assert D.market_enabled(tmp_path) is False                                # файла нет → выключено
    (tmp_path / "config").mkdir()
    cfg = tmp_path / "config" / "presentation.yaml"
    cfg.write_text("dig_offer_push: true\n", encoding="utf-8")
    assert D.market_enabled(tmp_path) is False                                # ключа нет → выключено
    cfg.write_text("event_menu_market: false\n", encoding="utf-8")
    assert D.market_enabled(tmp_path) is False
    cfg.write_text("event_menu_market: true\n", encoding="utf-8")
    assert D.market_enabled(tmp_path) is True
    cfg.write_text("event_menu_market: [не закрыто\n", encoding="utf-8")
    assert D.market_enabled(tmp_path) is False                                # битый файл → выключено


def test_flag_off_keeps_items_prompt_and_message_byte_identical(tmp_path, monkeypatch):
    with_mk = _menu_with_market()
    assert D.menu_items(with_mk) == D.menu_items(MENU)                         # по умолчанию рынка в поводах нет
    assert all(not it["id"].startswith("M") for it in D.menu_items(with_mk))
    items = D.menu_items(with_mk)
    assert D.build_user_prompt(items, [], NOW) == D.build_user_prompt(D.menu_items(MENU), [], NOW)
    assert "[M" not in D.build_user_prompt(items, [], NOW)
    payload = {"предложения": [_offer(повод_ids=["N1", "G1"])]}
    monkeypatch.setattr(D, "market_enabled", lambda root=None: False)          # флаг выключен в настройке
    pr_a, cl_a = _run(tmp_path, with_mk, payload, "a")
    pr_b, cl_b = _run(tmp_path, MENU, payload, "b")
    assert pr_a["текст"] and pr_a["текст"] == pr_b["текст"] and cl_a.user == cl_b.user
    assert pr_a["предложения"] == pr_b["предложения"] and "рыночных_поводов" not in pr_a
    bad, _ = _run(tmp_path, with_mk, {"предложения": [_offer()]}, "c")         # повод M при выключенном флаге — брак
    assert not bad["предложения"] and "не из меню дня" in bad["брак"][0]["причина"]


def test_flag_on_market_moves_become_leads_with_sources_from_code(tmp_path):
    items = D.menu_items(_menu_with_market(), market=True)
    ids = [it["id"] for it in items]
    m_ids = [i for i in ids if i.startswith("M")]
    assert ids[:len(m_ids)] == m_ids and m_ids[0] == "M1" and ids[len(m_ids):] == ["N1", "G1"]
    by = {it["id"]: it for it in items}
    assert "фонд широкого рынка США S&P 500 (SPY.US)" in by["M1"]["текст"] and "цены закрытия на 2026-11-20" in by["M1"]["текст"]
    assert by["M1"]["url"] == "https://d.example/1"
    nv = next(it for it in items if "Nvidia (NVDA.US)" in it["текст"])
    assert nv["urls"] == ["https://a.example/1", "https://g.example/1", "https://h.example/1"]
    assert "что пишут: «US financial watchdog probing Nvidia accounting»" in nv["текст"]
    quiet = [it for it in items if "QQQ.US" in it["текст"]]
    assert quiet == []                                                         # спокойный Nasdaq — не повод
    prompt = D.build_user_prompt(items, [], NOW)
    assert prompt.index("[M1]") < prompt.index("[N1]") and "[M…] — резкие движения самого рынка" in prompt

    pr, client = _run(tmp_path, _menu_with_market(), {"предложения": [_offer(повод_ids=[nv["id"]])]}, market=True)
    assert pr["рыночных_поводов"] == len(m_ids) and f"[{nv['id']}]" in client.user
    src = pr["предложения"][0]["источники"][0]
    assert src["url"] == "https://a.example/1" and src["urls"] == nv["urls"] and src["цены_на"] == "2026-11-20"
    assert "Источник: https://a.example/1" in pr["текст"] and pr.get("линт") is None
    fake, _ = _run(tmp_path, _menu_with_market(), {"предложения": [_offer(повод_ids=["M99"])]}, "b", market=True)
    assert not fake["предложения"] and "M99" in fake["брак"][0]["причина"]     # выдуманный рыночный повод — брак (П8)


def test_market_lead_without_headline_names_price_base_as_source(tmp_path):
    items = D.menu_items(_menu_with_market(), market=True)
    ry = next(it for it in items if "RY.US" in it["текст"])
    assert ry["url"] is None and ry["urls"] == [] and "причина в базе новостей не найдена" in ry["текст"]
    offer = _offer(тема="Канадские банки падают без видимой причины", повод_ids=[ry["id"]],
                   что_случилось="Бумаги Royal Bank of Canada (RY.US) резко упали за день.",
                   круги=["1-й круг: канадские банки"])
    pr, _ = _run(tmp_path, _menu_with_market(), {"предложения": [offer]}, market=True)
    assert pr["текст"] and pr.get("линт") is None
    assert ("Источник: цены закрытия на 2026-11-20 из нашей базы котировок; заголовков о причине в базе новостей "
            "не нашёл.") in pr["текст"]
    # обычный повод без ссылки (флаг выключен) по-прежнему идёт без строки «Источник»
    plain = D.render({"предложения": [dict(_offer(), источники=[{"id": "F1", "url": None, "текст": "x"}], аналог=None)]})
    assert "Источник" not in plain


# ── находки ревью 22.11 ──────────────────────────────────────────────────────────────────
def test_names_are_cut_only_from_the_end_and_fund_keeps_its_name():
    assert EM.short_name("First Trust Global Wind Energy ETF") == "First Trust Global Wind Energy ETF"   # было «First»
    assert EM.short_name("Platinum Group Metals Ltd") == "Platinum Group Metals"                         # было «Platinum»
    assert EM.short_name("Reinsurance Group of America") == "Reinsurance Group of America"
    assert EM.short_name("Northern Trust Corporation") == "Northern Trust"
    assert EM.short_name("Alibaba Group Holding Ltd") == "Alibaba" and EM.short_name("KKR & Co. Inc.") == "KKR"
    assert EM.short_name("Arm Holdings plc American Depositary Shares") == "Arm"
    assert EM.short_name("Banco Macro SA B ADR") == "Banco Macro" and EM.short_name("Vitamin C") == "Vitamin C"
    pages = dict(PAGES, **{"UVXY.US": {"name": "First Trust Global Wind Energy ETF", "page": None,
                                       "почему": "тип ETF — у фонда нет «страницы толпы»"},
                           "RY.US": {"name": "Platinum Group Metals Ltd", "page": None, "type": "Common Stock"}})
    mk = _market(pages=pages)
    assert [it["подпись"] for it in mk["фонды"]] == ["фонд First Trust Global Wind Energy ETF (UVXY.US)"]
    assert _by(mk, "компании")["RY.US"]["подпись"] == "Platinum Group Metals (RY.US)"


def test_exchange_code_before_colon_is_not_the_ticker():
    asx = EM.headline_matcher("ASX.US", names=["ASE Industrial"])
    assert not asx("Woodside Energy (ASX:WDS) Shares Rise: Why Is the ASX 20 Stock Higher?")
    assert not asx("'One foot in recession': ASX slides again")
    assert asx("ASE Industrial (NYSE:ASX) cuts outlook") and asx("Chip packager (ASX) slides") and asx("$ASX drops")
    ms = EM.headline_matcher("MS.US", names=["Morgan Stanley"])
    assert not ms("Thala Forever : MS Dhoni CSK Story") and ms("Morgan Stanley sees gold higher") and ms("Bank (NYSE: MS) falls")
    wulf = EM.headline_matcher("WULF.US")
    assert wulf("Pagaya (NASDAQ:PGY) and TeraWulf (NASDAQ: WULF) Head to Head")


def test_paper_with_missing_trading_days_is_not_a_sharp_one_day_move():
    hole = _series([-0.2])                                   # последняя цена на дату рынка, но прежняя — три недели назад
    hole = [((datetime.date.fromisoformat(d) - datetime.timedelta(days=21)).isoformat(), c) for d, c in hole[:-1]] + [hole[-1]]
    mk = _market(data=({**CLOSES, "HOLE.US": hole}, NEWS))
    assert hole[-1][0] == mk["цены_на"]
    assert all(it["бумага"] != "HOLE.US" for k in ("компании", "фонды") for it in mk[k])
    assert mk["счёт"]["компании"]["всего"] == 4              # бумага с дырой в датах не измерена и в счёт не идёт
    assert all("HOLE.US" not in ld["текст"] for ld in EM.market_leads(mk))


def test_no_sector_list_is_said_plainly():
    md = "\n".join(EM.render_market(_market(rules={})))
    assert "Секторы (резкое движение у 0 из 0):\n- нет данных: список фондов секторов не прочитан" in md
    assert "список фондов секторов не прочитан" not in "\n".join(EM.render_market(_market()))
