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

  python3 ops/event_menu.py gdelt [--hours 24]   — только GDELT, живьём (облако и сервер)
  python3 ops/event_menu.py write                — сервер: все источники → reports/events/menu_latest.{json,md}
  python3 ops/event_menu.py show                 — показать последнее меню (облако)
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
import subprocess
import sys
import time
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "reports" / "events"
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


# ── сборка ───────────────────────────────────────────────────────────────────────────────────────
def render_md(m):
    L = [f"# Меню событий — {m.get('собрано')}", "",
         "Что это: сырьё для режима «Круги на воде» — о чём больше всего пишет мировая пресса, что резко "
         "выросло за сутки, к чему вырос интерес людей. Выбор 3–5 событий и «почему горячее» — в разговоре.", ""]
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


def build(hours=24, sources=("gdelt", "wiki", "newsapi")):
    m = {"собрано": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
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
    a = ap.parse_args(argv)
    if a.cmd == "show":
        p = OUT_DIR / "menu_latest.md"
        print(p.read_text(encoding="utf-8") if p.exists() else "Меню ещё не собрано (сервер собирает вечером).")
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
