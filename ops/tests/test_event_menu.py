# -*- coding: utf-8 -*-
"""Меню событий (ops/event_menu.py): разбор файлов GDELT, «что выросло», Википедия — кодом."""
import io
import pathlib
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from ops import event_menu as EM   # noqa: E402


def _row(a1="", a2="", root="19", place="Tehran, Tehran, Iran", arts=10, url="https://x.org/a"):
    r = [""] * 61
    r[6], r[16], r[28], r[52], r[33], r[30], r[60] = a1, a2, root, place, str(arts), "-5", url
    return "\t".join(r)


def _zip(lines):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("x.export.CSV", "\n".join(lines))
    return b.getvalue()


def test_parse_and_aggregate_merge_country_and_skip_roles():
    raw = _zip([_row("IRAN", "UNITED STATES", arts=30), _row("HEALTH MINIST", "", arts=50, place="Paris, France"),
                _row("FIRST SOLAR", "", root="06", arts=40, place="Ohio, United States", url="https://y.org/b"),
                "битая строка"])
    rows = EM.parse_export(raw)
    assert len(rows) == 3
    agg = EM.aggregate(rows)
    assert agg["страна: Iran"]["статей"] == 30                      # участник IRAN и место Iran — одно
    assert "IRAN" not in agg and "HEALTH MINIST" not in agg and "UNITED STATES" not in agg
    assert agg["FIRST SOLAR"]["примеры"] == ["https://y.org/b"]
    assert agg["страна: Iran"]["по_типам"].most_common(1)[0][0] == "19"


def test_heat_table_rising_needs_volume_and_ratio():
    now = {"A": {"статей": 600, "по_типам": EM.collections.Counter({"19": 600}), "примеры": []},
           "B": {"статей": 100, "по_типам": EM.collections.Counter({"04": 100}), "примеры": []},
           "C": {"статей": 900, "по_типам": EM.collections.Counter({"01": 900}), "примеры": []}}
    prev = {"A": {"статей": 50}, "B": {"статей": 0}, "C": {"статей": 850}}
    t = EM.heat_table(now, prev, min_articles=150, min_ratio=2.0)
    assert [r["что"] for r in t["резко_выросло"]] == ["A"]          # B мал по объёму, C не вырос
    assert t["больше_всего_пишут"][0]["что"] == "C" and "боевые действия" in t["резко_выросло"][0]["типы"]


def test_wiki_rising_skips_service_pages_and_needs_growth():
    cur = {"Main_Page": 5_000_000, "First_Solar": 120_000, "Taylor_Swift": 300_000, "Special:Search": 900_000}
    prev = {"First_Solar": 8_000, "Taylor_Swift": 290_000}
    out = EM.wiki_rising(cur, prev)
    assert [r["статья"] for r in out] == ["First Solar"]


def test_newsapi_without_key_is_honest_and_menu_renders(monkeypatch):
    monkeypatch.delenv("NEWSAPI_AI_KEY", raising=False)
    assert "нет ключа" in EM.newsapi_menu()["ошибка"]
    md = EM.render_md({"собрано": "2026-11-04", "gdelt": {"ошибка": "нет данных: x"},
                       "wiki": {"ошибка": "нет данных: 429"}, "newsapi": {"ошибка": "нет ключа"}})
    assert "GDELT — нет данных" in md and "Википедия: нет данных" in md
