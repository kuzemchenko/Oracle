# -*- coding: utf-8 -*-
"""data/tests/test_news_common.py — детерминизм нормализации, тегирования и дедупа (MASTER_SPEC §4, П1).

Сеть не нужна: проверяем чистый код news_common на синтетических статьях. Эти тесты — страховка
gate Недели 2: «суточный поток нормализуется и тегируется (язык, страна, тип, время) БЕЗ ручной
правки, дедупликация работает».
"""
import sys
import pathlib
import tempfile

import pytest

DATA = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DATA))
import news_common as nc  # noqa: E402


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(nc, "DB", tmp_path / "t.db")
    c = nc.db_connect()
    yield c
    c.close()


# --- Тег: язык (П1) → ISO 639-1 -------------------------------------------
@pytest.mark.parametrize("raw,expect", [
    ("English", "en"), ("english", "en"), ("Russian", "ru"), ("Arabic", "ar"),
    ("Chinese", "zh"), ("eng", "en"), ("rus", "ru"), ("zho", "zh"), ("spa", "es"),
    ("fr", "fr"), ("", None), (None, None), ("Klingon", None),
])
def test_normalize_lang(raw, expect):
    assert nc.normalize_lang(raw) == expect


# --- Тег: страна (П1) → ISO 3166-1 alpha-2 --------------------------------
@pytest.mark.parametrize("raw,expect", [
    ("United States", "US"), ("united states", "US"), ("Saudi Arabia", "SA"),
    ("United Kingdom", "GB"), ("Russia", "RU"), ("China", "CN"), ("Chile", "CL"),
    ("US", "US"), ("", None), (None, None), ("Atlantis", None),
])
def test_normalize_country(raw, expect):
    assert nc.normalize_country(raw) == expect


# --- Тег: тип источника (П1) media|social|forum|official ------------------
@pytest.mark.parametrize("domain,dtype,expect", [
    ("reuters.com", None, "media"),
    ("newsradio1410.iheart.com", None, "media"),
    ("twitter.com", None, "social"),
    ("x.com", "news", "social"),
    ("t.me", None, "social"),
    ("reddit.com", None, "forum"),
    ("news.ycombinator.com", None, "forum"),
    ("eia.gov", None, "official"),
    ("foo.gov.uk", None, "official"),
    ("ecb.europa.eu", None, "official"),
    ("someblog.com", "blog", "media"),
    ("neftegaz.ru", "blog", "media"),
    ("prnewswire.com", "pr", "official"),
    ("unknownsite.io", "news", "media"),
    # граница домена: подстрока НЕ должна срабатывать (регресс бага instaforex→social)
    ("instaforex.com", "blog", "media"),
    ("forex.com", None, "media"),
    ("fortune.org", None, "media"),       # не должно ловиться 'un.org'
    ("m.twitter.com", None, "social"),    # поддомен реальной площадки — должно
    ("forum.hotcopper.com.au", None, "forum"),  # forum.* поддомен
])
def test_classify_source_type(domain, dtype, expect):
    assert nc.classify_source_type(domain, dtype) == expect


def test_source_type_always_filled():
    # Даже при пустом домене и без dataType тип не None (gate: тип 100%).
    assert nc.classify_source_type(None, None) == "media"
    assert nc.classify_source_type("", None) == "media"


# --- Тег: время (П1) → ISO 8601 UTC ---------------------------------------
def test_time_parsing():
    assert nc.parse_gdelt_time("20260611T043000Z") == "2026-06-11T04:30:00+00:00"
    assert nc.parse_gdelt_time("bad") is None
    assert nc.parse_iso_time("2026-06-11T04:53:55Z") == "2026-06-11T04:53:55+00:00"
    assert nc.parse_iso_time(None) is None


# --- Канонизация URL (основа точного дедупа) ------------------------------
def test_canonicalize_url():
    a = nc.canonicalize_url("https://WWW.Example.com/Path/?utm_source=x&b=2&a=1#frag")
    assert a == "https://example.com/Path?a=1&b=2"
    # тот же материал с трекинг-мусором и www → тот же канон
    b = nc.canonicalize_url("https://example.com/Path?a=1&b=2&fbclid=Z")
    assert a == b


def test_title_fingerprint_stable_across_punctuation_and_case():
    f1 = nc.title_fingerprint("Oil prices surge on Iran tensions today")
    f2 = nc.title_fingerprint("Oil Prices Surge on Iran Tensions Today!")
    assert f1 == f2 and f1 != ""


# --- Запись полностью тегирована (П1) -------------------------------------
def test_make_record_fully_tagged():
    r = nc.make_record("gdelt", "https://a.com/x", "Oil up on OPEC cut",
                       "2026-06-11T04:00:00+00:00", "English", "United States", "a.com")
    assert r["lang"] == "en"
    assert r["country"] == "US"
    assert r["source_type"] == "media"
    assert r["published_at"] == "2026-06-11T04:00:00+00:00"
    assert r["id"] and r["title_fp"]


# --- Точный дедуп по каноническому URL (PRIMARY KEY) ----------------------
def test_exact_url_dedup_via_pk(con):
    recs = [
        nc.make_record("gdelt", "https://a.com/oil?utm_source=tw", "Oil up",
                       "2026-06-11T04:00:00+00:00", "English", "United States", "a.com"),
        nc.make_record("newsapi_ai", "https://www.a.com/oil", "Oil up (mirror)",
                       "2026-06-11T04:01:00+00:00", "eng", None, "a.com"),
    ]
    # одинаковый канонический URL → одинаковый id → одна строка
    assert recs[0]["id"] == recs[1]["id"]
    ins, tot = nc.upsert_news(con, recs)
    assert tot == 2 and ins == 1
    assert con.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 1


# --- Near-dup: один сюжет, разные URL/источники → один кластер -------------
def test_near_dup_collapses_cross_source(con):
    recs = [
        nc.make_record("gdelt", "https://a.com/oil-surge-iran",
                       "Oil prices surge on Iran tensions today",
                       "2026-06-11T04:00:00+00:00", "English", "United States", "a.com"),
        nc.make_record("newsapi_ai", "https://b.com/markets/oil-surge-iran-tensions",
                       "Oil Prices Surge on Iran Tensions Today!",
                       "2026-06-11T04:05:00+00:00", "eng", None, "b.com", datatype="news"),
        nc.make_record("gdelt", "https://c.com/copper-down",
                       "Copper falls as Chinese demand weakens",
                       "2026-06-11T05:00:00+00:00", "English", "Chile", "c.com"),
    ]
    nc.upsert_news(con, recs)
    marked = nc.dedupe_day(con, "2026-06-11")
    assert marked == 1
    uniq = con.execute("SELECT COUNT(*) FROM news WHERE dup_of IS NULL").fetchone()[0]
    assert uniq == 2
    # канонический представитель — самая ранняя по времени запись
    dup = con.execute("SELECT dup_of, dup_reason FROM news WHERE dup_of IS NOT NULL").fetchone()
    canon_pub = con.execute("SELECT published_at FROM news WHERE id=?", (dup[0],)).fetchone()[0]
    assert canon_pub == "2026-06-11T04:00:00+00:00"
    assert dup[1] == "title"


# --- Дедуп идемпотентен: повторный прогон не плодит и не теряет ------------
def test_dedupe_idempotent(con):
    recs = [
        nc.make_record("gdelt", "https://a.com/1", "Brent climbs above ninety dollars",
                       "2026-06-11T04:00:00+00:00", "English", "United States", "a.com"),
        nc.make_record("newsapi_ai", "https://b.com/2", "Brent Climbs Above Ninety Dollars",
                       "2026-06-11T04:10:00+00:00", "eng", None, "b.com"),
    ]
    nc.upsert_news(con, recs)
    m1 = nc.dedupe_day(con, "2026-06-11")
    m2 = nc.dedupe_day(con, "2026-06-11")
    assert m1 == 1 and m2 == 1
    assert con.execute("SELECT COUNT(*) FROM news WHERE dup_of IS NULL").fetchone()[0] == 1


# --- Идемпотентность вставки: повторный заход не дублирует -----------------
def test_upsert_idempotent(con):
    recs = [nc.make_record("gdelt", "https://a.com/x", "T", "2026-06-11T04:00:00+00:00",
                           "English", "United States", "a.com")]
    nc.upsert_news(con, recs)
    ins2, _ = nc.upsert_news(con, recs)
    assert ins2 == 0
    assert con.execute("SELECT COUNT(*) FROM news").fetchone()[0] == 1
