# -*- coding: utf-8 -*-
"""ops/event_menu.py — «меню событий» для режима «Круги на воде» (решение владельца 04.11).

Владелец: «я говорю „найдём идею“ — ты спрашиваешь тему или показываешь, о чём сейчас говорят,
что растёт в поиске, что произошло; дальше мы обсуждаем событие и смотрим, куда расходятся круги».
Этот модуль даёт ДАННЫЕ для меню (код, без LLM — Инв#6); выбрать 3–5 событий и сказать «почему
горячее» по-человечески — работа сессии, которая читает меню.

Источники (04.11: GDELT-API забанил нас и облако, Google Trends отказывает всем — 429):
  • GDELT по ФАЙЛАМ: каждые 15 минут data.gdeltproject.org/gdeltv2/<время>.export.CSV.zip — все
    новостные события мира без лимита запросов. Код суммирует число статей по участникам и местам
    за последние сутки и сравнивает с предыдущими: «о чём пишут больше всего» и «что резко выросло».
  • Просмотры Википедии (официальный открытый API Wikimedia): самые читаемые статьи вчера против
    позавчера — к чему вырос интерес людей (замена Google Trends). Отвечает серверу; облачный
    общий адрес часто получает отказ — тогда честно «нет данных».
  • NewsAPI.ai (уже оплачен): крупнейшие события суток по числу статей (event/getEvents,
    eventsSortBy=size). Нужен NEWSAPI_AI_KEY (только на сервере). Fail-soft.

  • РЫНОК (Q-меню-рынок, 22.11): резкие движения самого рынка по ценам закрытия из нашей базы — широкий
    рынок, крупные технологии, фонды секторов, самые резкие отдельные бумаги; к каждому — свежие заголовки
    из базы новостей. 22.11 меню предложило «атаку на судно в Балтике», а падение рынка на 9% за неделю
    не увидело: оно смотрело только новости. Расчёт — mathlib/market_moves.py, моделей нет.

  python3 ops/event_menu.py gdelt [--hours 24]   — только GDELT, живьём (облако и сервер)
  python3 ops/event_menu.py write                — сервер: все источники → reports/events/menu_latest.{json,md}
  python3 ops/event_menu.py show                 — показать последнее меню (облако)
  python3 ops/event_menu.py market [--today ГГГГ-ММ-ДД] [--db путь]  — только раздел «Рынок», живьём по базе
Журналы честности не трогает.
"""
import argparse
import collections
import csv
import datetime
import io
import json
import math
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "events"
MARKET_DB = ROOT / "storage" / "oracle.db"
MARKET_PAGES = ROOT / "reports" / "attention" / "pages.json"   # тикер → имя компании (EODHD), ведёт датчик внимания
GDELT_BASE = "http://data.gdeltproject.org/gdeltv2/"
UA = "OracleEventMenu/1.0 (https://github.com/kuzemchenko/Oracle; research, low volume)"
STEP_MIN = 15

# CAMEO: корневые коды событий GDELT → по-русски (для «что за событие»)
CAMEO_RU = {"01": "заявления", "02": "призывы", "03": "намерения сотрудничать", "04": "переговоры",
            "05": "дипломатическое сотрудничество", "06": "материальное сотрудничество", "07": "помощь",
            "08": "уступки", "09": "расследования", "10": "требования", "11": "неодобрение",
            "12": "отказ", "13": "угрозы", "14": "протесты", "15": "демонстрация силы",
            "16": "ухудшение отношений (санкции, разрыв)", "17": "принуждение", "18": "нападения",
            "19": "боевые действия", "20": "массовое насилие"}
# участники-роли без имени собственного — шум для меню
GENERIC = {"", "UNITED STATES", "PRESIDENT", "GOVERNMENT", "POLICE", "COMPANY", "SCHOOL", "MILITARY",
           "BUSINESS", "MEDIA", "COMMUNITY", "STUDENT", "UNIVERSITY", "COURT", "CITIZEN", "JUDGE",
           "PROSECUTOR", "LAWMAKER", "OFFICIALS", "AUTHORITIES", "PRIME MINISTER", "MINISTER",
           "CONGRESS", "SENATE", "CHILDREN", "WOMEN", "PEOPLE", "EMPLOYEE", "WORKER", "RESIDENTS",
           "HOSPITAL", "DOCTOR", "LAWYER", "ATTORNEY", "SOLDIER", "TEACHER", "PARENT", "VOTER",
           "PARLIAMENT", "CABINET", "INDUSTRY", "BANK", "CRIMINAL", "THIEF", "SUSPECT", "PRISON",
           "MAYOR", "GOVERNOR", "LEGISLATOR", "OPPOSITION", "PARTY", "REBEL", "NEWSPAPER"}
ROLE_PARTS = ("MINIST", "BARRISTER", "SPOKES", "OFFICIAL", "ADMINISTRATION", "AUTHORIT", "COMMISSION",
              "DEPARTMENT", "AGENCY", "COUNCIL", "COMMITTEE", "SECRETARY", "DIRECTOR", "CHIEF", "EXECUTIVE",
              "CHAIRMAN", "LEADER", "OFFICER", "INVESTIGATOR", "PROTESTER", "ACTIVIST", "REFUGEE", "MIGRANT",
              "PATIENT", "VICTIM", "FARMER", "TRADER", "INVESTOR", "ANALYST", "JOURNALIST", "REPORTER")
WIKI_SKIP = ("Main_Page", "Special:", "Wikipedia:", "File:", "Portal:", "Help:", "Talk:", "Category:",
             "Template:", "User:", "-", "Cleopatra", "XXX", "Pornhub", "Xvideos", "Bible")


# ── сеть ─────────────────────────────────────────────────────────────────────────────────────────
def _get(url, timeout=40, binary=False):
    p = subprocess.run(["curl", "-sS", "-L", "--http1.1", "-m", str(timeout), "-A", UA, url],
                       capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise OSError((p.stderr or b"").decode("utf-8", "replace").strip()[:200] or f"curl {p.returncode}")
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


# ── GDELT ────────────────────────────────────────────────────────────────────────────────────────
def gdelt_last_stamp(get=_get):
    """Метка последнего 15-минутного файла (ГГГГММДДЧЧММСС) из lastupdate.txt."""
    for ln in get(GDELT_BASE + "lastupdate.txt").splitlines():
        if ".export.CSV.zip" in ln:
            return ln.rsplit("/", 1)[-1].split(".")[0]
    raise LookupError("GDELT: в lastupdate.txt нет файла событий")


def stamps_back(last, n):
    t = datetime.datetime.strptime(last, "%Y%m%d%H%M%S")
    return [(t - datetime.timedelta(minutes=STEP_MIN * i)).strftime("%Y%m%d%H%M%S") for i in range(n)]


def parse_export(raw_zip):
    """Байты .export.CSV.zip → строки событий (dict с нужными полями). Битые строки — пропуск."""
    z = zipfile.ZipFile(io.BytesIO(raw_zip))
    text = z.read(z.namelist()[0]).decode("utf-8", "replace")
    out = []
    for r in csv.reader(io.StringIO(text), delimiter="\t"):
        if len(r) < 61:
            continue
        try:
            arts = int(r[33] or 0)
        except ValueError:
            continue
        out.append({"a1": r[6].strip().upper(), "a2": r[16].strip().upper(), "root": r[28][:2],
                    "place": r[52].strip(), "country": r[53].strip(), "articles": arts,
                    "goldstein": float(r[30] or 0), "url": r[60].strip()})
    return out


def _place_key(place):
    """«Tehran, Tehran, Iran» → «Iran»; страна — уровень меню (города дробят тему)."""
    return place.rsplit(",", 1)[-1].strip() if place else ""


def aggregate(rows):
    """Строки событий → {сущность: {"статей", "по_типам": Counter, "примеры": [(статей, url)]}}.
    Сущность — участник с именем собственным (не роль из GENERIC) или страна места события."""
    agg = {}
    countries = {_place_key(e["place"]).upper(): _place_key(e["place"]) for e in rows if e.get("place")}
    for e in rows:
        ents = set()
        for x in (e["a1"], e["a2"]):
            if x in GENERIC or len(x) <= 2 or any(part in x for part in ROLE_PARTS):
                continue
            # участник-страна и место-страна — одна сущность («GUYANA» и «Guyana» не двоятся)
            ents.add("страна: " + countries[x] if x in countries else x)
        pk = _place_key(e["place"])
        if pk:
            ents.add("страна: " + pk)
        for k in ents:
            a = agg.setdefault(k, {"статей": 0, "по_типам": collections.Counter(), "примеры": []})
            a["статей"] += e["articles"]
            a["по_типам"][e["root"]] += e["articles"]
            if e["url"]:
                a["примеры"].append((e["articles"], e["url"]))
    for a in agg.values():
        seen, ex = set(), []
        for n, u in sorted(a["примеры"], key=lambda x: -x[0]):
            if u not in seen:
                seen.add(u)
                ex.append(u)
            if len(ex) >= 3:
                break
        a["примеры"] = ex
    return agg


def heat_table(now_agg, prev_agg, top=25, min_articles=150, min_ratio=2.0):
    """Сравнение суток: «больше всего пишут» (по числу статей) и «резко выросло» (рост ≥ min_ratio при
    объёме ≥ min_articles). Рост со сглаживанием (+20) — редкая сущность не выглядит взрывом."""
    def row(k, a, b):
        types = [CAMEO_RU.get(c, c) for c, _n in a["по_типам"].most_common(2)]
        return {"что": k, "статей_сутки": a["статей"], "статей_прошлые_сутки": b,
                "рост_раз": round((a["статей"] + 20) / (b + 20), 2), "типы": types, "примеры": a["примеры"]}
    rows = [row(k, a, (prev_agg.get(k) or {}).get("статей", 0)) for k, a in now_agg.items()]
    biggest = sorted(rows, key=lambda r: -r["статей_сутки"])[:top]
    rising = sorted([r for r in rows if r["статей_сутки"] >= min_articles and r["рост_раз"] >= min_ratio],
                    key=lambda r: -(r["статей_сутки"] * math.log(r["рост_раз"])))[:top]
    return {"больше_всего_пишут": biggest, "резко_выросло": rising}


def gdelt_menu(hours=24, get=_get, pause=0.2):
    """Последние `hours` часов против предыдущих `hours` (файлы по 15 минут). Пропавший файл — пропуск
    с подсчётом (П8: сколько файлов реально прочитано — в выдаче)."""
    last = gdelt_last_stamp(get)
    n = int(hours * 60 / STEP_MIN)
    all_st = stamps_back(last, 2 * n)
    now_rows, prev_rows, got, miss = [], [], 0, 0
    for i, st in enumerate(all_st):
        try:
            rows = parse_export(get(GDELT_BASE + st + ".export.CSV.zip", binary=True))
            got += 1
        except (OSError, zipfile.BadZipFile, KeyError, IndexError):
            miss += 1
            continue
        (now_rows if i < n else prev_rows).extend(rows)
        if pause:
            time.sleep(pause)
    t = heat_table(aggregate(now_rows), aggregate(prev_rows))
    return {"источник": "GDELT 2.0, файлы событий каждые 15 минут (data.gdeltproject.org)", "до": last,
            "часов": hours, "файлов_прочитано": got, "файлов_нет": miss, **t}


# ── Википедия ────────────────────────────────────────────────────────────────────────────────────
def _wiki_top(day, project="en.wikipedia", get=_get):
    url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/{project}/all-access/"
           f"{day.year}/{day.month:02d}/{day.day:02d}")
    d = json.loads(get(url))
    return {a["article"]: int(a["views"]) for a in (d.get("items") or [{}])[0].get("articles") or []}


def wiki_rising(cur, prev, top=25, min_views=50000, min_ratio=2.0):
    """Статьи, которые вчера читали в разы больше, чем позавчера (служебные — пропуск)."""
    out = []
    for art, v in cur.items():
        if v < min_views or any(art.startswith(s) or art == s for s in WIKI_SKIP):
            continue
        b = prev.get(art, 0)
        ratio = (v + 1000) / (b + 1000)
        if ratio >= min_ratio:
            out.append({"статья": art.replace("_", " "), "просмотров_вчера": v, "позавчера": b,
                        "рост_раз": round(ratio, 2)})
    return sorted(out, key=lambda r: -(r["просмотров_вчера"] * math.log(r["рост_раз"])))[:top]


def wiki_menu(today=None, get=_get):
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    d1, d2 = today - datetime.timedelta(days=1), today - datetime.timedelta(days=2)
    try:
        cur, prev = _wiki_top(d1, get=get), _wiki_top(d2, get=get)
    except (OSError, ValueError, KeyError, IndexError) as e:
        return {"источник": "просмотры Википедии (Wikimedia)", "ошибка": f"нет данных: {str(e)[:120]}"}
    return {"источник": "просмотры Википедии (Wikimedia), en", "день": d1.isoformat(),
            "резко_выросло": wiki_rising(cur, prev)}


# ── NewsAPI.ai ───────────────────────────────────────────────────────────────────────────────────
def newsapi_menu(today=None, key=None):
    """Крупнейшие события суток по числу статей (EventRegistry event/getEvents, сортировка size).
    Без ключа — честно «нет данных». Формат ответа проверяется на первом запуске на сервере."""
    key = key or os.environ.get("NEWSAPI_AI_KEY")
    if not key:
        return {"источник": "NewsAPI.ai", "ошибка": "нет ключа NEWSAPI_AI_KEY (он только на сервере)"}
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    payload = {"apiKey": key, "resultType": "events", "eventsSortBy": "size", "eventsCount": 25,
               "dateStart": (today - datetime.timedelta(days=1)).isoformat(), "dateEnd": today.isoformat(),
               "lang": "eng", "includeEventSummary": False, "includeEventSocialScore": False}
    try:
        req = urllib.request.Request("https://eventregistry.org/api/v1/event/getEvents",
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:                                   # noqa: BLE001 — источник не роняет меню
        return {"источник": "NewsAPI.ai", "ошибка": f"нет данных: {type(e).__name__} {str(e)[:120]}"}
    res = ((d.get("events") or {}).get("results")) or []
    out = []
    for ev in res[:25]:
        title = ev.get("title") or {}
        out.append({"событие": title.get("eng") or next(iter(title.values()), None) if isinstance(title, dict) else title,
                    "статей": ev.get("totalArticleCount"), "дата": ev.get("eventDate"),
                    "uri": ev.get("uri")})
    return {"источник": "NewsAPI.ai (EventRegistry), крупнейшие события суток", "события": out,
            **({} if out else {"ошибка": "ответ без событий — проверить формат запроса"})}


# ── рынок: резкие движения (код, без моделей) ────────────────────────────────────────────────────
MARKET_INDEX = (("SPY.US", "фонд широкого рынка США S&P 500"),
                ("QQQ.US", "фонд крупных технологических компаний Nasdaq 100"))
MARKET_HISTORY_DAYS = 420        # календарных: год торгов для «обычного колебания» + запас
MARKET_MAX_PRICE_AGE = 5         # цены старше стольких дней — честное «нет данных» (длинные выходные — 4 дня)
MARKET_NEWS_DAYS = 5
MARKET_TOP_SECTORS = 6           # сколько резких секторов показывать подробно (остальные — одной строкой)
MARKET_TOP_STOCKS = 6
MARKET_TOP_FUNDS = 2
MARKET_LEADS_MAX = 15            # столько рыночных поводов максимум уходит разведчику «что покопать»
MARKET_TITLE_MAX = 140
NO_NEWS = "причина в базе новостей не найдена"
# слова в заголовках для фондов (у фонда нет «имени компании»); фонд без строки ищется только по тикеру
FUND_WORDS = {
    "SPY.US": (r"S&P ?500", r"Wall Street", r"U\.?S\.? stocks", r"stock market (?:correction|crash|rout|sell-?off|slump)",
               r"stocks (?:tumble|plunge|slide|sink|slump|rally|surge|soar)"),
    # «(NASDAQ: NVDA)» — это пометка биржи у чужой бумаги, а не новость об индексе
    "QQQ.US": (r"Nasdaq[- ]100", r"Nasdaq Composite", r"(?<!\()Nasdaq(?!\s*:)", r"tech (?:rout|sell-?off|wreck)",
               r"Big Tech"),
    "XLK.US": (r"tech(?:nology)? (?:stocks|shares|sector|rout|sell-?off)", r"Big Tech"),
    "SMH.US": (r"semiconductor (?:stocks|shares|index|sector|rout|sell-?off|makers)",
               r"chip (?:stocks|makers|sector|rout|shares)", r"chipmakers?"),
    "CIBR.US": (r"cyber ?security",),
    "XLE.US": (r"energy (?:stocks|shares|sector)", r"oil prices?", r"crude oil", r"oil majors"),
    "XOP.US": (r"shale", r"oil (?:and|&) gas producers", r"oil producers", r"drillers"),
    "XLU.US": (r"utilit(?:y|ies) (?:stocks|shares|sector)", r"power prices", r"electricity demand"),
    "XLI.US": (r"industrial (?:stocks|shares|sector)", r"industrials"),
    "ITA.US": (r"defen[cs]e (?:stocks|shares|contractors|sector)", r"aerospace (?:and|&) defen[cs]e"),
    "XLB.US": (r"materials (?:stocks|shares|sector)", r"mining (?:stocks|shares)", r"chemical makers"),
    "REMX.US": (r"rare[- ]earths?",),
    "URA.US": (r"uranium",),
    "XLF.US": (r"bank (?:stocks|shares)", r"big banks", r"banking sector", r"financial (?:stocks|shares|sector)"),
    "XLV.US": (r"health ?care (?:stocks|shares|sector)", r"drugmakers", r"pharma (?:stocks|shares)"),
    "XLY.US": (r"consumer discretionary", r"retail (?:stocks|shares)", r"retailers"),
    "XLP.US": (r"consumer staples",),
    "XLRE.US": (r"real estate (?:stocks|shares|sector)", r"REITs?"),
}
# короткое имя из одного слова («Stem», «Visa», «Gap») — заголовок засчитывается, только если в нём есть слово о
# рынке или тикер: иначе «…aims to stem damage» выглядел бы новостью о компании
_MARKET_WORDS = re.compile(
    r"\b(?:stocks?|shares?|earnings|investors?|sell-?off|rout|plunge[sd]?|slump[sd]?|tumble[sd]?|surge[sd]?|soar(?:s|ed)?|"
    r"rall(?:y|ies)|downgrade[sd]?|upgrade[sd]?|price target|CEO|SEC|Nasdaq|NYSE|guidance|revenue|profit)\b|акци|котиров",
    re.IGNORECASE)
_NAME_TAIL = re.compile(r"[\s,]+(?:Inc|Corp|Corporation|Ltd|Limited|PLC|LP|Holdings?|Group|Co|Company|SA|NV|AG|SE|ADR|"
                        r"Trust|Class [A-C]|Common Stock)\b.*$", re.IGNORECASE)


def _market_deps():
    for p in (str(ROOT), str(ROOT / "ops")):
        if p not in sys.path:
            sys.path.insert(0, p)
    from mathlib import market_moves as MM
    return MM


def _sector_rules():
    """Фонды секторов и их имена — из листа комитета (knowledge/idea_committee.yaml) функциями ops/sector_view.py.
    Сбой → пустые правила: раздел живёт без секторов, а не падает."""
    try:
        _market_deps()
        import sector_view as SV
        rules = SV.load_rules()
        return rules, (lambda f: SV.fund_name(f, rules))
    except Exception:                                        # noqa: BLE001 — нет правил = нет секторов
        return {}, (lambda f: f.split(".")[0])


def _curated_names():
    """Курируемые русские имена бумаг из словаря бота (только чтение); нет словаря — пусто."""
    try:
        _market_deps()
        import bot_reports as R
        return {k: v[0] for k, v in (getattr(R, "_ASSET", None) or {}).items() if v and v[0]}
    except Exception:                                        # noqa: BLE001
        return {}


def _read_pages(path=None):
    try:
        d = json.loads(pathlib.Path(path or MARKET_PAGES).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def short_name(name):
    """«NVIDIA Corporation» → «NVIDIA», «Mitsubishi UFJ Financial Group Inc ADR» → «Mitsubishi UFJ Financial»."""
    n = " ".join(str(name or "").replace("®", "").split())
    cut = _NAME_TAIL.sub("", n).strip(" .,&")
    return cut if len(cut) >= 2 else n.strip(" .,")


def read_market_data(today, db_path=None, news_days=MARKET_NEWS_DAYS):
    """Боевая база ТОЛЬКО на чтение → ({бумага: [(дата, цена)]}, [новость]) или (None, причина словами).
    Новость = (published_at, заголовок, url, домен, отпечаток заголовка); дубли и пустые заголовки — мимо."""
    p = pathlib.Path(db_path or MARKET_DB)
    if not p.exists() or p.stat().st_size == 0:
        return None, "базы котировок на этой машине нет (она только на сервере)"
    since = (today - datetime.timedelta(days=MARKET_HISTORY_DAYS)).isoformat()
    news_since = (today - datetime.timedelta(days=news_days)).isoformat()
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)
        try:
            closes = collections.defaultdict(list)
            for sym, d, c in con.execute(
                    "select symbol, date, coalesce(adjusted_close, close) from quotes "
                    "where date>=? and date<=? order by symbol, date", (since, today.isoformat())):
                if c is not None:
                    closes[sym].append((d, float(c)))
            news = con.execute(
                "select published_at, title, url, domain, title_fp from news "
                "where substr(published_at,1,10)>=? and substr(published_at,1,10)<=? and dup_of is null "
                "and title is not null and url is not null order by published_at desc",
                (news_since, (today + datetime.timedelta(days=1)).isoformat())).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        return None, f"база котировок не прочитана: {type(e).__name__}"
    return (dict(closes), news), None


def _ticker_rx(sym):
    """Тикер в заголовке: «(NVDA)», «NASDAQ: NVDA», «$NVDA»; голым словом — только от 4 букв (иначе «ALL», «NOW»)."""
    base = re.escape(sym.split(".")[0])
    pats = [rf"(?:\(|:\s?|\$){base}(?![\w.-])"]
    if len(sym.split(".")[0]) >= 4:
        pats.append(rf"(?<![\w$.-]){base}(?![\w.-])")
    return re.compile("|".join(pats))


def headline_matcher(sym, names=(), words=()):
    """→ функция(заголовок) → bool. names — имена компании (без учёта регистра, но слово в заголовке должно
    начинаться с заглавной; короткое имя из одного слова — только рядом со словом о рынке); words — готовые
    шаблоны для фондов; тикер — по _ticker_rx."""
    tick = _ticker_rx(sym)
    name_rx = [(re.compile(rf"(?<![\w-]){re.escape(n)}(?![\w-])", re.IGNORECASE), " " not in n and len(n) <= 4)
               for n in dict.fromkeys(x.strip() for x in names if x and len(x.strip()) >= 3)]
    word_rx = re.compile("|".join(words), re.IGNORECASE) if words else None

    def match(title):
        t = str(title or "")
        if tick.search(t) or (word_rx is not None and word_rx.search(t)):
            return True
        for rx, weak in name_rx:
            m = rx.search(t)
            if m and m.group(0)[:1].isupper() and (not weak or _MARKET_WORDS.search(t)):
                return True
        return False
    return match


def find_headlines(news, match, limit=3, used=None):
    """Самые свежие заголовки, где упомянута бумага: без повторов заголовка и не больше одного с домена.
    used — общий на раздел набор уже показанных заголовков: один и тот же не повторяется у соседних бумаг."""
    out, seen_fp, seen_dom = [], (used if used is not None else set()), set()
    for published, title, url, domain, fp in news or []:
        if not title or not url or not match(title):
            continue
        key = fp or " ".join(str(title).lower().split())[:80]
        if key in seen_fp or url in seen_fp or (domain and domain in seen_dom):
            continue
        seen_fp.update((key, url))
        seen_dom.add(domain)
        t = " ".join(str(title).replace("​", " ").split()).strip(" -–—|")
        out.append({"заголовок": t if len(t) <= MARKET_TITLE_MAX else t[:MARKET_TITLE_MAX].rsplit(" ", 1)[0] + "…",
                    "url": url, "когда": str(published)[:16]})
        if len(out) >= limit:
            break
    return out


def _pct1(x):
    n = round(x * 100, 1)
    return "0.0%" if n == 0 else (f"+{n:.1f}%" if n > 0 else f"−{abs(n):.1f}%")


def move_words(sm):
    """Сводка mathlib.market_moves.summary → фраза: «за день −3.6% — это 4.2 обычного дневного колебания; …»."""
    parts = []
    for key, label, adj in (("день", "за день", "дневного"), ("неделя", "за 5 торговых дней", "недельного")):
        m = sm.get(key)
        if m:
            parts.append(f"{label} {_pct1(m['ход'])} — это {abs(m['кратность']):.1f} обычного {adj} колебания")
        else:
            parts.append(f"{label} — не измерено")
    return "; ".join(parts)


def _market_item(sym, kind, label, sm):
    slim = {k: ({x: sm[k][x] for x in ("ход", "кратность")} if sm.get(k) else None) for k in ("день", "неделя")}
    return {"бумага": sym, "вид": kind, "подпись": label, **slim, "резкое": sm["резкое"], "окно": sm["окно"],
            "сила": sm["сила"], "вниз": sm["вниз"], "словами": move_words(sm)}


def market_menu(today=None, db_path=None, data=None, rules=None, fund_name=None, pages=None, curated=None):
    """Раздел «Рынок: резкие движения». Всё считает код: цены — база котировок, «резкое» — mathlib/market_moves,
    заголовки — база новостей за MARKET_NEWS_DAYS дней. data=(closes, news) подставляется в тестах.
    Нет базы / цены устарели → {'ошибка': …} (П8: честное «нет данных»)."""
    MM = _market_deps()
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    head = {"источник": "цены закрытия из нашей базы котировок (EODHD) и заголовки из нашей базы новостей; расчёт — код"}
    if data is None:
        data, err = read_market_data(today, db_path)
        if data is None:
            return {**head, "ошибка": f"нет данных: {err}"}
    closes, news = data
    if rules is None:
        rules, fund_name = _sector_rules()
    fund_name = fund_name or (lambda f: f.split(".")[0])
    pages = _read_pages() if pages is None else pages
    curated = _curated_names() if curated is None else curated
    # «сегодня» рынка — последняя дата у фонда широкого рынка; бумага без цены на эту дату отстала и не меряется
    if closes.get(MARKET_INDEX[0][0]):
        asof = closes[MARKET_INDEX[0][0]][-1][0]
    else:
        asof = max((c[-1][0] for c in closes.values() if c), default=None)
    if not asof:
        return {**head, "ошибка": "нет данных: в базе котировок нет цен за последний год"}
    try:
        age = (today - datetime.date.fromisoformat(str(asof)[:10])).days
    except ValueError:
        age = None
    if age is None or age > MARKET_MAX_PRICE_AGE:
        return {**head, "ошибка": f"нет данных: последние цены в базе — на {asof}, это старше {MARKET_MAX_PRICE_AGE} дней"}
    index_syms = [s for s, _ in MARKET_INDEX]
    sector_syms = [s for s in rules.get("карта") or [] if s not in index_syms]

    def kind_of(sym):
        if sym in index_syms:
            return "рынок"
        if sym in sector_syms:
            return "сектор"
        rec = pages.get(sym) or {}
        return "фонд" if str(rec.get("почему") or "").startswith("тип ") else "компания"

    def label_of(sym, kind):
        if kind == "рынок":
            return f"{dict(MARKET_INDEX)[sym]} ({sym})"
        if kind == "сектор":
            return f"фонд сектора «{fund_name(sym)}» ({sym})"
        name = curated.get(sym) or short_name((pages.get(sym) or {}).get("name"))
        if not name or name == sym:
            return f"{sym} — компанию не опознал"
        return f"{name} ({sym})" if (kind == "компания" or name.lower().startswith("фонд")) else f"фонд {name} ({sym})"

    def matcher_of(sym, kind):
        if kind == "компания":
            rec = pages.get(sym) or {}
            page = re.sub(r"\s*\(.*?\)\s*$", "", str(rec.get("page") or "").replace("_", " "))
            cur = curated.get(sym) or ""
            return headline_matcher(sym, names=[short_name(rec.get("name")), page, cur if cur.isascii() else ""])
        return headline_matcher(sym, words=FUND_WORDS.get(sym, ()))

    measured, stale, gaps, used = {}, [], [], set()
    for sym, cl in closes.items():
        if "." not in sym or not cl:                         # в базе есть обрывки без биржи («LMT») — не бумаги
            continue
        if cl[-1][0] != asof:
            stale.append(sym)
            continue
        sm = MM.summary(cl)
        if sm is None or sm["день"] is None or sm["неделя"] is None:
            if MM.window_has_gap(cl):
                gaps.append(sym)
            if sm is None:
                continue
        measured[sym] = sm

    def items(syms, limit=None, sharp_only=True):
        rows = [(s, measured[s]) for s in syms if s in measured and (measured[s]["резкое"] or not sharp_only)]
        if sharp_only:
            rows.sort(key=lambda r: -r[1]["сила"])
        out = []
        for s, sm in rows[:limit]:
            k = kind_of(s)
            it = _market_item(s, k, label_of(s, k), sm)
            if sm["резкое"]:
                it["заголовки"] = find_headlines(news, matcher_of(s, k), 3 if k in ("рынок", "компания") else 2, used)
                if not it["заголовки"]:
                    it["новости"] = NO_NEWS
            out.append(it)
        return out

    def count(syms):
        sh = [measured[s] for s in syms if s in measured and measured[s]["резкое"]]
        return {"всего": sum(1 for s in syms if s in measured), "резких": len(sh),
                "вниз": sum(1 for m in sh if m["вниз"]), "вверх": sum(1 for m in sh if not m["вниз"])}

    rest = [s for s in measured if s not in index_syms and s not in sector_syms]
    stocks = [s for s in rest if kind_of(s) == "компания"]
    funds = [s for s in rest if kind_of(s) == "фонд"]
    sharp_sectors = sorted((s for s in sector_syms if s in measured and measured[s]["резкое"]),
                           key=lambda s: -measured[s]["сила"])
    return {**head, "цены_на": asof,
            "правило": ("резкое движение — ход за день или за 5 торговых дней не меньше "
                        f"{MM.DEFAULTS['порог_кратности']:g} обычных колебаний этой же бумаги за последний год "
                        f"и не меньше {MM.DEFAULTS['мин_ход'] * 100:g}%"),
            "счёт": {"все": count(list(measured)), "секторы": count(sector_syms), "компании": count(stocks),
                     "фонды": count(funds)},
            "рынок": items(index_syms, sharp_only=False),
            "секторы": items(sharp_sectors, MARKET_TOP_SECTORS),
            "секторы_ещё_резкие": [f"«{fund_name(s)}» {_pct1(measured[s][measured[s]['окно']]['ход'])}"
                                   for s in sharp_sectors[MARKET_TOP_SECTORS:]],
            "секторы_спокойные": [fund_name(s) for s in sector_syms if s in measured and not measured[s]["резкое"]],
            "секторы_не_измерены": [fund_name(s) for s in sector_syms if s not in measured],
            "компании": items(stocks, MARKET_TOP_STOCKS),
            "фонды": items(funds, MARKET_TOP_FUNDS),
            "ряд_с_разрывом": [label_of(s, kind_of(s)) for s in sorted(gaps)[:20]], "цены_отстали": len(stale)}


def _market_line(it):
    tag = ("РЕЗКОЕ, " + ("вниз" if it["вниз"] else "вверх")) if it.get("резкое") else "без резких движений"
    L = [f"- {it['подпись']} — {tag}: {it['словами']}"]
    for h in it.get("заголовки") or []:
        L.append(f"  - «{h['заголовок']}» — {h['url']}")
    if it.get("новости"):
        L.append(f"  - {it['новости']}")
    return L


def render_market(mk):
    """Раздел меню словами (пишет код). Это измерение «что случилось с ценами», а не прогноз."""
    if not mk:
        return []
    if mk.get("ошибка"):
        return [f"## Рынок: резкие движения — {mk['ошибка']}", ""]
    c = mk.get("счёт") or {}
    a = c.get("все") or {}
    L = [f"## Рынок: резкие движения (цены закрытия на {mk.get('цены_на')})",
         f"Как читать: {mk.get('правило')}. Считает код по базе котировок. Заголовки — из базы новостей за "
         f"{MARKET_NEWS_DAYS} дней: они показывают, что пишут о бумаге, а причину движения не доказывают.",
         f"Измерено бумаг и фондов: {a.get('всего', 0)}; резкое движение у {a.get('резких', 0)} "
         f"(вниз {a.get('вниз', 0)}, вверх {a.get('вверх', 0)}).", "", "Широкий рынок:"]
    for it in mk.get("рынок") or []:
        L += _market_line(it)
    if not mk.get("рынок"):
        L.append("- не измерено (нет цен фондов широкого рынка)")
    s = c.get("секторы") or {}
    L += ["", f"Секторы (резкое движение у {s.get('резких', 0)} из {s.get('всего', 0)}):"]
    for it in mk.get("секторы") or []:
        L += _market_line(it)
    if mk.get("секторы_ещё_резкие"):
        L.append("- ещё резкие: " + ", ".join(mk["секторы_ещё_резкие"]))
    if mk.get("секторы_спокойные"):
        L.append("- без резких движений: " + ", ".join(mk["секторы_спокойные"]))
    if mk.get("секторы_не_измерены"):
        L.append("- не измерено (мало истории или разрыв в ряду цен): " + ", ".join(mk["секторы_не_измерены"]))
    for key, title in (("компании", "Отдельные компании"), ("фонды", "Другие фонды")):
        k = c.get(key) or {}
        shown = mk.get(key) or []
        L += ["", f"{title} (резкое движение у {k.get('резких', 0)} из {k.get('всего', 0)}"
                  + (f"; показаны {len(shown)} самых сильных" if k.get("резких", 0) > len(shown) else "") + "):"]
        for it in shown:
            L += _market_line(it)
        if not shown:
            L.append("- резких движений нет")
    if mk.get("ряд_с_разрывом"):
        L += ["", "Ход измерен не полностью из-за разрыва в ряду цен (похоже на дробление акций без поправки "
              "прежних цен): " + ", ".join(mk["ряд_с_разрывом"])]
    return L + [""]


def market_leads(mk, limit=MARKET_LEADS_MAX):
    """Рыночные поводы для разведчика «что покопать» (ops/dig_offer.menu_items, id M1, M2…): только резкие движения,
    порядок — широкий рынок, секторы, компании, другие фонды. Ссылки — url заголовков (ставит код)."""
    if not mk or mk.get("ошибка"):
        return []
    out = []
    for key in ("рынок", "секторы", "компании", "фонды"):
        for it in mk.get(key) or []:
            if not it.get("резкое"):
                continue
            heads = it.get("заголовки") or []
            news = ("что пишут: " + "; ".join(f"«{h['заголовок']}»" for h in heads)) if heads else NO_NEWS
            out.append({"текст": f"рынок, цены закрытия на {mk.get('цены_на')}: {it['подпись']} — {it['словами']}; {news}",
                        "url": heads[0]["url"] if heads else None, "urls": [h["url"] for h in heads],
                        "бумага": it["бумага"], "цены_на": mk.get("цены_на")})
    return out[:limit]


# ── сборка ───────────────────────────────────────────────────────────────────────────────────────
def render_md(m):
    L = [f"# Меню событий — {m.get('собрано')}", "",
         "Что это: сырьё для режима «Круги на воде» — о чём больше всего пишет мировая пресса, что резко "
         "выросло за сутки, к чему вырос интерес людей. Выбор 3–5 событий и «почему горячее» — в разговоре.", ""]
    L += render_market(m.get("рынок"))
    g = m.get("gdelt") or {}
    if g.get("ошибка"):
        L += [f"## GDELT — нет данных: {g['ошибка']}", ""]
    elif g:
        L += [f"## GDELT: резко выросло за {g.get('часов')} ч (файлов {g.get('файлов_прочитано')}, нет {g.get('файлов_нет')})"]
        for r in g.get("резко_выросло", [])[:15]:
            L.append(f"- {r['что']}: {r['статей_сутки']} статей (было {r['статей_прошлые_сутки']}, ×{r['рост_раз']}); "
                     f"{', '.join(r['типы'])}; {(r['примеры'] or [''])[0]}")
        L += ["", "## GDELT: больше всего пишут"]
        for r in g.get("больше_всего_пишут", [])[:15]:
            L.append(f"- {r['что']}: {r['статей_сутки']} статей; {', '.join(r['типы'])}")
        L.append("")
    w = m.get("wiki") or {}
    L.append(f"## Википедия: {w.get('ошибка') or 'интерес вырос'}")
    for r in w.get("резко_выросло", [])[:15]:
        L.append(f"- {r['статья']}: {r['просмотров_вчера']} просмотров (позавчера {r['позавчера']}, ×{r['рост_раз']})")
    n = m.get("newsapi") or {}
    L += ["", f"## NewsAPI.ai: {n.get('ошибка') or 'крупнейшие события'}"]
    for r in n.get("события", [])[:15]:
        L.append(f"- {r.get('событие')}: {r.get('статей')} статей")
    return "\n".join(L) + "\n"


def build(hours=24, sources=("market", "gdelt", "wiki", "newsapi")):
    m = {"собрано": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    if "market" in sources:
        try:
            m["рынок"] = market_menu()
        except Exception as e:                               # noqa: BLE001 — раздел рынка не роняет меню
            m["рынок"] = {"ошибка": f"нет данных: {type(e).__name__} {str(e)[:120]}"}
    if "gdelt" in sources:
        try:
            m["gdelt"] = gdelt_menu(hours)
        except (OSError, LookupError, ValueError) as e:
            m["gdelt"] = {"ошибка": f"нет данных: {str(e)[:160]}"}
    if "wiki" in sources:
        m["wiki"] = wiki_menu()
    if "newsapi" in sources:
        m["newsapi"] = newsapi_menu()
    return m


def main(argv=None):
    ap = argparse.ArgumentParser(description="меню событий для режима «Круги на воде»")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gdelt")
    g.add_argument("--hours", type=float, default=24)
    sub.add_parser("write")
    sub.add_parser("show")
    mk = sub.add_parser("market")
    mk.add_argument("--today", default=None)
    mk.add_argument("--db", default=None)
    mk.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "show":
        p = OUT_DIR / "menu_latest.md"
        print(p.read_text(encoding="utf-8") if p.exists() else "Меню ещё не собрано (сервер собирает вечером).")
        return 0
    if a.cmd == "market":                                    # живьём по базе, ничего не пишет
        res = market_menu(datetime.date.fromisoformat(a.today) if a.today else None, db_path=a.db)
        print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else "\n".join(render_market(res)))
        return 0
    if a.cmd == "gdelt":
        m = build(a.hours, sources=("gdelt",))
        print(render_md(m))
        return 0
    m = build()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "menu_latest.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT_DIR / "menu_latest.md").write_text(render_md(m), encoding="utf-8")
    print(render_md(m)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
