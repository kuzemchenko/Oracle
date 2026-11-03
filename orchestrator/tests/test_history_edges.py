# -*- coding: utf-8 -*-
"""Исторические каскады → библиотека и пары под форвард-счёт (orchestrator/history_edges.py)."""
import datetime
import json
import math
import pathlib
import random
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from orchestrator import edge_forward as EFW     # noqa: E402
from orchestrator import history_edges as HE     # noqa: E402

TODAY = datetime.date(2026, 11, 3)


def _dates(n, start="2001-01-01"):
    d, out = datetime.date.fromisoformat(start), []
    while len(out) < n:
        if d.isoweekday() <= 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def _series_bank(n=5200):
    """OIL — блуждание со скачками; TNK догоняет скачки OIL через 3 дня; RND — независимое."""
    rnd = random.Random(42)
    ds = _dates(n)
    oil, tnk, rndm = [80.0], [20.0], [30.0]
    jumps, pend = set(), {}
    for i in range(1, n):
        r = rnd.gauss(0, 0.01)
        if i > 300 and i % 61 == 0:
            r += 0.08 * (1 if rnd.random() < 0.5 else -1)
            jumps.add(i)
        oil.append(oil[-1] * math.exp(r))
        rt = rnd.gauss(0, 0.01)
        if (i - 3) in jumps:
            pend[i] = 0.06 if math.log(oil[i - 3] / oil[i - 4]) > 0 else -0.06
        tnk.append(tnk[-1] * math.exp(rt + pend.pop(i, 0.0)))
        rndm.append(rndm[-1] * math.exp(rnd.gauss(0, 0.01)))
    spy = [100.0]
    for i in range(1, n):
        spy.append(spy[-1] * math.exp(rnd.gauss(0, 0.008)))
    bank = {"OIL": (ds, oil), "TNK": (ds, tnk), "RND": (ds, rndm), "OLDOIL": (ds, oil), "SPY": (ds, spy)}
    return lambda s: bank.get(s)


def _ep(**kw):
    e = {"id": "2019_test", "trigger_date": "2019-06-13", "date_precision": "day", "title_ru": "атака на танкеры",
         "what_ru": "атаки в Оманском заливе", "domain": "нефть", "mechanism_id": "chokepoint_disruption",
         "shock_type": "route_blocked", "resources": ["oil", "tanker_capacity"], "region": "Персидский залив",
         "sources": [{"url": "https://en.wikipedia.org/wiki/June_2019_Gulf_of_Oman_incident", "confirms_ru": "дата"}],
         "links": [
             {"order": 0, "role_ru": "нефть", "role_tag": "crude_oil", "then_symbol": "OLDOIL", "today_symbol": "OIL",
              "direction": "up", "lag_days_guess": 0, "why_ru": "риск поставок", "non_obvious": False},
             {"order": 1, "role_ru": "танкерщик", "role_tag": "crude_tanker_owner", "then_symbol": "TNK",
              "today_symbol": "TNK", "direction": "up", "lag_days_guess": 3, "why_ru": "ставки фрахта",
              "non_obvious": False},
             {"order": 2, "role_ru": "случайная бумага", "role_tag": "random", "then_symbol": "RND",
              "today_symbol": "RND", "direction": "down", "lag_days_guess": 40, "why_ru": "проверка",
              "non_obvious": True}],
         "counter_case_ru": "не знаю", "analog_rule_ru": "когда… смотри на…",
         "verified": {"source_ok": True, "checked_url": "x", "date_ok": True, "notes_ru": ""}}
    e.update(kw)
    return e


def test_validate_rejects_bad_episodes():
    ok, why = HE.validate_episode(_ep(), TODAY, {"chokepoint_disruption"})
    assert ok and not why and ok["mechanism_known"]
    assert HE.validate_episode(_ep(trigger_date="2027-01-01"), TODAY)[1] == ["дата повода в будущем"]
    assert "нет источника-URL" in HE.validate_episode(_ep(sources=[{"url": "wiki"}]), TODAY)[1]
    one = _ep()
    one["links"] = [dict(one["links"][0]), dict(one["links"][1], then_symbol="нет")]
    assert "меньше двух звеньев с торгуемым тикером того времени" in HE.validate_episode(one, TODAY)[1]
    no0 = _ep()
    no0["links"] = [dict(L, order=L["order"] + 1) for L in no0["links"]]
    assert "нет звена order=0 (сам шок)" in HE.validate_episode(no0, TODAY)[1]
    bad = _ep(verified={"source_ok": False})
    assert "скептик: источник не подтвердил событие" in HE.validate_episode(bad, TODAY)[1]


def test_build_writes_library_with_code_prices(tmp_path):
    f = tmp_path / "ver_00.json"
    f.write_text(json.dumps([_ep(), _ep(id="2019_test"), _ep(id="x", trigger_date="bad")]), encoding="utf-8")
    out = tmp_path / "lib.yaml"
    s = HE.build([str(f)], out=out, series_fn=_series_bank(), today=TODAY, mechs={"chokepoint_disruption"})
    assert s["эпизодов"] == 1 and len(s["отклонено"]) == 1 and s["склеено_дублей"] == 1   # тот же id — склейка
    doc = yaml.safe_load(out.read_text(encoding="utf-8"))
    L0 = doc["эпизоды"][0]["links"][0]
    assert L0["prices"]["есть"] and L0["prices"]["тикер"] == "OLDOIL"
    assert L0["prices"]["база_дата"] < "2019-06-13" and "ход_20" in L0["prices"]
    assert doc["эпизоды"][0]["звеньев_с_ценой"] == 3


def test_episode_pairs_parents_and_signs():
    ep, _ = HE.validate_episode(_ep(), TODAY)
    prs = HE.episode_pairs(ep)
    keys = {(p["from"], p["to"]): p for p in prs}
    assert set(keys) == {("OIL", "TNK"), ("TNK", "RND"), ("OIL", "RND")}
    assert keys[("OIL", "TNK")]["sign"] == 1 and keys[("OIL", "TNK")]["horizon"] == 5
    assert keys[("TNK", "RND")]["sign"] == -1 and keys[("OIL", "RND")]["horizon"] == 60


def test_test_pairs_scores_and_conflicts():
    ep, _ = HE.validate_episode(_ep(), TODAY)
    ep2, _ = HE.validate_episode(_ep(id="2020_other"), TODAY)
    ep2["links"][1]["direction"] = "down"                # OIL↑→TNK↓ — противоположный знак тому же ребру
    res = HE.test_pairs([ep], series_fn=_series_bank(), server={"OIL", "TNK", "RND"})
    r = {(x["from"], x["to"]): x for x in res}
    assert r[("OIL", "TNK")]["вердикт"] == "подтверждена" and r[("OIL", "TNK")]["q"] <= HE.Q_FDR
    assert r[("OIL", "TNK")]["шоки_источника"] == "вверх"
    assert r[("OIL", "TNK")]["сверх_рынка"]["вердикт"] == "подтверждена"    # не общий рынок, а своя волна
    res2 = HE.test_pairs([ep, ep2], series_fn=_series_bank(), server={"OIL", "TNK", "RND"})
    c = [x for x in res2 if (x["from"], x["to"]) == ("OIL", "TNK")][0]
    assert c["вердикт"] == "конфликт знаков" and c["эпизоды"] == ["2019_test", "2020_other"]


def test_eligible_rules():
    base = {"вердикт": "намёк", "на_сервере": True, "знак": 1, "знак_корреляции": 1, "ρ": 0.3}
    assert HE.eligible(base)[0]
    assert not HE.eligible({**base, "вердикт": "история против"})[0]
    assert not HE.eligible({**base, "на_сервере": False})[0]
    ok, why = HE.eligible({**base, "знак_корреляции": -1, "ρ": -0.2})
    assert not ok and "против корреляции" in why
    assert HE.eligible({**base, "знак_корреляции": -1, "ρ": -0.02})[0]     # ρ≈0 — спора нет
    assert HE.eligible({**base, "вердикт": "мало данных", "знак_корреляции": 0})[0]


def test_append_candidates_idempotent_and_visible_to_forward_test(tmp_path):
    sens = tmp_path / "sens.yaml"
    sens.write_text(yaml.safe_dump({"sensitivities": [{"источник": "USO.US", "узел": "BNO.US", "lag": 0}]}),
                    encoding="utf-8")
    reg = tmp_path / "cand.jsonl"
    reg.write_text(json.dumps({"edge_key": "OIL.US->RND.US@lag0", "origin": "thesis",
                               "from": "OIL.US", "to": "RND.US", "lag": 0}) + "\n", encoding="utf-8")
    ep, _ = HE.validate_episode(_ep(), TODAY)
    res = HE.test_pairs([ep], series_fn=_series_bank(), server={"OIL", "TNK", "RND"})
    s1 = HE.append_candidates(res, registry=reg, sens_path=sens)
    assert "OIL→TNK" in s1["добавлено"]
    s2 = HE.append_candidates(res, registry=reg, sens_path=sens)
    assert s2["добавлено"] == []                          # повторный прогон ничего не дублирует
    rows = [json.loads(x) for x in reg.read_text(encoding="utf-8").splitlines()]
    hist = [x for x in rows if x["origin"] == "history"]
    assert hist and all(x["lag"] == 0 and x["эпизоды"] and x["история"]["вердикт"] for x in hist)
    edges = EFW.edge_library(sens, candidates_path=reg)
    assert {"OIL.US", "TNK.US"} <= {e["from"] for e in edges} | {e["to"] for e in edges}
    assert any(e["origin"] == "history" for e in edges)


def test_edge_library_rotation_is_deterministic_and_keeps_library_first(tmp_path):
    sens = tmp_path / "sens.yaml"
    sens.write_text(yaml.safe_dump({"sensitivities": [{"источник": "ZZZ.US", "узел": "YYY.US", "lag": 0}]}),
                    encoding="utf-8")
    reg = tmp_path / "cand.jsonl"
    reg.write_text("".join(json.dumps({"origin": "history", "from": f"A{i}.US", "to": f"B{i}.US", "lag": 0}) + "\n"
                           for i in range(30)), encoding="utf-8")
    d1 = EFW.edge_library(sens, candidates_path=reg, rotate="2026-11-03")
    d1b = EFW.edge_library(sens, candidates_path=reg, rotate="2026-11-03")
    d2 = EFW.edge_library(sens, candidates_path=reg, rotate="2026-11-04")
    plain = EFW.edge_library(sens, candidates_path=reg)
    assert d1[0]["origin"] == "library" and d2[0]["origin"] == "library"
    assert d1 == d1b and [e["from"] for e in d1] != [e["from"] for e in d2]
    assert sorted(map(str, d1)) == sorted(map(str, plain))     # тот же набор, другой порядок
    assert [e["from"] for e in plain][1:4] == ["A0.US", "A1.US", "A10.US"]   # без ротации — как раньше


def test_merge_duplicates_across_collectors():
    a, _ = HE.validate_episode(_ep(id="2019_a"), TODAY)
    b, _ = HE.validate_episode(_ep(id="2019_b", trigger_date="2019-06-14"), TODAY)
    c, _ = HE.validate_episode(_ep(id="2019_c", trigger_date="2019-09-14"), TODAY)
    same_id, _ = HE.validate_episode(_ep(id="2019_c", trigger_date="2019-09-16"), TODAY)
    b["links"] = b["links"][:2]
    other, _ = HE.validate_episode(_ep(id="2019_other_chain", trigger_date="2019-06-14"), TODAY)
    other["links"] = [dict(other["links"][0]), dict(other["links"][2], then_symbol="ZZZ")]
    other["links"][0]["then_symbol"] = "QQQ1"
    out, n = HE.merge_duplicates([b, a, c, same_id, other])
    assert n == 2 and sorted(e["id"] for e in out) == ["2019_a", "2019_c", "2019_other_chain"]
    assert [e for e in out if e["id"] == "2019_a"][0]["merged_from"] == ["2019_b"]


def test_rank_for_forward_limits_and_order():
    def r(a, b, v, p, m="намёк"):
        return {"from": a, "to": b, "вердикт": v, "на_сервере": True, "знак": 1, "знак_корреляции": 1, "ρ": 0.2,
                "счёт": {"p": p}, "сверх_рынка": {"вердикт": m}}
    rows = [r("A", f"T{i}", "намёк", 0.1 + i / 100) for i in range(5)] + \
        [r("B", "T0", "подтверждена", 0.01), r("C", "T0", "намёк", 0.02), r("D", "X", "мало данных", None),
         r("E", "Y", "история против", 0.9)]
    pick, rest = HE.rank_for_forward(rows, top=5, per_from=3, per_to=2)
    keys = [(x["from"], x["to"]) for x in pick]
    assert keys[0] == ("B", "T0") and ("E", "Y") not in keys + [(x["from"], x["to"]) for x in rest]
    assert sum(1 for k in keys if k[0] == "A") <= 3 and sum(1 for k in keys if k[1] == "T0") <= 2
    assert len(pick) == 5 and ("D", "X") in [(x["from"], x["to"]) for x in rest + pick]
