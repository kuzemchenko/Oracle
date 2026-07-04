#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""data/news_common.py — общий слой новостного сборщика (MASTER_SPEC §4 «Сборщик новостей», П1).

Единая нормализация, тегирование и дедупликация суточного потока из GDELT и NewsAPI.ai.
Здесь нет сетевых вызовов и нет LLM — только детерминированный код, который коннекторы
data/gdelt.py и data/newsapi_ai.py вызывают, отдав сырую статью.

Что делает (П1 «Теги: язык, страна, тип источника, время»):
  • lang        → ISO 639-1 (из полного имени GDELT или ISO 639-3 NewsAPI.ai), data/_maps.py
  • country     → ISO 3166-1 alpha-2 (если источник дал; иначе None — «нет данных», П8)
  • source_type → media | social | forum | official (детерминированные правила по домену + dataType)
  • published_at→ ISO 8601 UTC (время события/публикации)
  • дедуп       → точный по каноническому URL + near-dup по отпечатку заголовка в пределах суток

Все строки кладутся в storage/oracle.db, таблица news. dup_of IS NULL = канонический представитель
кластера; уникальный поток = SELECT ... WHERE dup_of IS NULL. Ничего не удаляется (CLAUDE.md «стиль»).
"""
import re
import json
import hashlib
import sqlite3
import datetime
import pathlib

from _maps import ISO3_TO_ISO1, NAME_TO_ISO1, COUNTRY_NAME_TO_ISO2

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "storage" / "oracle.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id            TEXT PRIMARY KEY,   -- sha1 канонического URL (или домен|отпечаток|дата)
    source        TEXT NOT NULL,      -- 'gdelt' | 'newsapi_ai'
    url           TEXT,
    canonical_url TEXT,
    domain        TEXT,
    title         TEXT,
    body          TEXT,               -- GDELT тела не даёт → NULL
    lang          TEXT,               -- ISO 639-1
    country       TEXT,               -- ISO 3166-1 alpha-2 или NULL
    source_type   TEXT,               -- media|social|forum|official
    published_at  TEXT,               -- ISO 8601 UTC (время новости)
    title_fp      TEXT,               -- отпечаток заголовка для near-dup
    event_uri     TEXT,               -- кластер события NewsAPI.ai (для будущей §6), не для дедупа
    dup_of        TEXT,               -- id канонического представителя; NULL = сам канонический
    dup_reason    TEXT,               -- 'url' | 'title' | 'provider' — почему помечен дублем
    raw           TEXT,               -- исходный JSON источника (провенанс, П8)
    fetched_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_pubdate  ON news(substr(published_at,1,10));
CREATE INDEX IF NOT EXISTS idx_news_dupof    ON news(dup_of);
CREATE INDEX IF NOT EXISTS idx_news_country  ON news(country);
CREATE INDEX IF NOT EXISTS idx_news_lang     ON news(lang);
CREATE INDEX IF NOT EXISTS idx_news_type     ON news(source_type);
CREATE INDEX IF NOT EXISTS idx_news_titlefp  ON news(title_fp);

CREATE TABLE IF NOT EXISTS trends (
    keyword     TEXT NOT NULL,
    geo         TEXT NOT NULL,        -- '' = весь мир
    date        TEXT NOT NULL,        -- ISO date точки ряда
    interest    INTEGER,              -- 0..100 Google Trends
    is_partial  INTEGER DEFAULT 0,
    source      TEXT DEFAULT 'google_trends',
    fetched_at  TEXT,
    timeframe   TEXT,                  -- окно фетча Trends; ТОЛЬКО явная запись store() (NULL = неизвестно)
    PRIMARY KEY (keyword, geo, date)
);
CREATE TABLE IF NOT EXISTS trends_related (
    keyword     TEXT NOT NULL,
    geo         TEXT NOT NULL,
    rank_kind   TEXT NOT NULL,        -- 'top' | 'rising'
    query       TEXT NOT NULL,
    value       INTEGER,              -- индекс популярности / breakout
    fetched_at  TEXT,
    PRIMARY KEY (keyword, geo, rank_kind, query)
);
"""

# ---------------------------------------------------------------------------
# Время
# ---------------------------------------------------------------------------

def now_utc_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def parse_gdelt_time(seendate):
    """GDELT seendate 'YYYYMMDDTHHMMSSZ' → ISO 8601 UTC. None при сбое."""
    if not seendate:
        return None
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z?$", seendate.strip())
    if not m:
        return None
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}+00:00"


def parse_iso_time(value):
    """NewsAPI.ai dateTimePub/dateTime '...Z' → ISO 8601 UTC. None при сбое."""
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Язык / страна (П1)
# ---------------------------------------------------------------------------

def normalize_lang(value):
    """Полное имя GDELT ('English') или ISO 639-3 ('eng') → ISO 639-1 ('en'). None если неизвестно."""
    if not value:
        return None
    v = value.strip().lower()
    if v in NAME_TO_ISO1:
        return NAME_TO_ISO1[v]
    if v in ISO3_TO_ISO1:
        return ISO3_TO_ISO1[v]
    if len(v) == 2 and v.isalpha():   # уже ISO 639-1
        return v
    return None


def normalize_country(value):
    """Полное имя страны → ISO 3166-1 alpha-2. None если неизвестно/не дано (П8)."""
    if not value:
        return None
    v = value.strip().lower()
    if v in COUNTRY_NAME_TO_ISO2:
        return COUNTRY_NAME_TO_ISO2[v]
    if len(value.strip()) == 2 and value.strip().isalpha():  # уже alpha-2
        return value.strip().upper()
    return None


# ---------------------------------------------------------------------------
# Тип источника (П1: СМИ / соцсеть / форум / официоз)
# ---------------------------------------------------------------------------

SOCIAL_DOMAINS = (
    "twitter.com", "x.com", "facebook.com", "fb.com", "instagram.com",
    "t.me", "telegram.me", "telegram.org", "vk.com", "ok.ru", "youtube.com",
    "youtu.be", "tiktok.com", "threads.net", "bsky.app",
    "linkedin.com", "weibo.com", "tumblr.com", "snapchat.com",
)
FORUM_DOMAINS = (
    "reddit.com", "redd.it", "news.ycombinator.com", "ycombinator.com",
    "stackexchange.com", "stackoverflow.com", "quora.com", "stocktwits.com",
    "4chan.org", "discord.com", "disqus.com",
)
# Поддомены-форумы: домен, у которого первый ярлык — forum/forums/board/community.
FORUM_SUBDOMAIN_RE = re.compile(r"^(forum|forums|board|boards|community)\.")
# Официоз: государственные/межгосударственные/регуляторы/центробанки.
OFFICIAL_DOMAINS = (
    "europa.eu", "federalreserve.gov", "ecb.europa.eu", "imf.org",
    "worldbank.org", "un.org", "who.int", "opec.org", "iea.org", "eia.gov",
    "bis.org", "wto.org", "kremlin.ru", "mid.ru", "cbr.ru", "gov.uk",
    "bankofengland.co.uk", "treasury.gov", "sec.gov", "whitehouse.gov",
)
OFFICIAL_TLD_RE = re.compile(
    r"(\.gov$|\.gov\.[a-z]{2}$|\.gob\.[a-z]{2}$|\.go\.[a-z]{2}$|\.mil$|\.gouv\.[a-z]{2}$)"
)
# Тип данных NewsAPI.ai → наш тип, если домен ничего не сказал.
# ВАЖНО: dataType='blog' у EventRegistry — это НЕ соцсеть, а немейнстримный веб-источник
# (отраслевые сайты вроде neftegaz.ru, metalinfo.ru). Соцсеть/форум определяются ТОЛЬКО по
# домену реальной площадки (twitter/telegram/reddit). Поэтому blog → media (СМИ), pr → официоз.
DATATYPE_TO_TYPE = {"news": "media", "pr": "official", "blog": "media"}


def _host_matches(d, suffixes):
    """Совпадение по границе домена: d == s ИЛИ d оканчивается на '.'+s.
    Так 'x.com' матчит 'x.com' и 'm.x.com', но НЕ 'instaforex.com' (была подстрочная ошибка)."""
    return any(d == s or d.endswith("." + s) for s in suffixes)


def classify_source_type(domain, datatype=None):
    """Детерминированный тип источника. Приоритет: домен > dataType > media.

    media   — СМИ (по умолчанию: новостной/веб-источник, в т.ч. EventRegistry dataType='blog')
    social  — соцсеть (twitter/telegram/reddit-как-платформа и т.п. — только по домену площадки)
    forum   — форум/агрегатор обсуждений (только по домену площадки)
    official— официоз: гос/межгос/регуляторы/центробанки + пресс-релизы (dataType='pr')

    Сопоставление доменов — строго по границе ярлыка (см. _host_matches), не подстрокой,
    иначе 'x.com' ловит 'instaforex.com', а 'un.org' — 'fortune.org'.
    """
    d = (domain or "").strip().lower().lstrip(".")
    if d.startswith("www."):
        d = d[4:]
    if d:
        if OFFICIAL_TLD_RE.search(d) or _host_matches(d, OFFICIAL_DOMAINS):
            return "official"
        if _host_matches(d, SOCIAL_DOMAINS) or "mastodon." in d:
            return "social"
        if _host_matches(d, FORUM_DOMAINS) or FORUM_SUBDOMAIN_RE.match(d):
            return "forum"
    if datatype:
        t = DATATYPE_TO_TYPE.get(datatype.strip().lower())
        if t:
            return t
    return "media"


# ---------------------------------------------------------------------------
# URL и отпечаток заголовка (дедуп)
# ---------------------------------------------------------------------------

_TRACKING_PARAMS = re.compile(
    r"^(utm_|fbclid$|gclid$|gbraid$|wbraid$|mc_|ref$|ref_|source$|cmpid$|"
    r"icid$|ito$|xtor$|spm$|yclid$|_hsenc$|_hsmi$|mkt_tok$|igshid$)", re.I)


def canonicalize_url(url):
    """Канонизация URL для точного дедупа: схема/хост в нижний регистр, убрать www,
    фрагмент, трекинг-параметры, дефолтные порты и хвостовой слэш. None → None."""
    if not url:
        return None
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    try:
        sp = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    scheme = (sp.scheme or "http").lower()
    host = (sp.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    if sp.port and not ((scheme == "http" and sp.port == 80) or (scheme == "https" and sp.port == 443)):
        netloc = f"{host}:{sp.port}"
    q = [(k, v) for k, v in parse_qsl(sp.query, keep_blank_values=False)
         if not _TRACKING_PARAMS.match(k)]
    q.sort()
    path = sp.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, urlencode(q), ""))


def domain_of(url):
    if not url:
        return None
    from urllib.parse import urlsplit
    try:
        host = (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return None
    return host[4:] if host.startswith("www.") else (host or None)


_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)
# Минимальные стоп-слова основных языков — снижают ложные совпадения отпечатка.
_STOP = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "as", "at",
    "by", "is", "are", "be", "with", "from", "that", "this", "it", "its",
    "после", "как", "что", "для", "над", "под", "при", "the", "los", "las",
    "el", "la", "de", "del", "y", "en", "para", "con", "und", "der", "die",
    "das", "le", "les", "des", "du", "et", "à",
}


def title_fingerprint(title):
    """Отпечаток заголовка для near-dup: нижний регистр, выкинуть пунктуацию и стоп-слова,
    отсортировать значимые токены. Один и тот же сюжет из разных источников → совпадает."""
    if not title:
        return ""
    toks = [t for t in _WORD_RE.split(title.lower()) if t and t not in _STOP and len(t) > 1]
    return " ".join(sorted(toks))


def title_tokens(title):
    return set(t for t in _WORD_RE.split((title or "").lower()) if t and t not in _STOP and len(t) > 1)


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# Запись «нормализованной» статьи
# ---------------------------------------------------------------------------

def make_record(source, url, title, published_at, lang, country, domain,
                body=None, datatype=None, event_uri=None, provider_duplicate=False,
                raw=None):
    """Собирает полностью тегированную запись (П1). id — sha1 канонического URL,
    либо домен|отпечаток|дата если URL нет. provider_duplicate помечает дубль,
    о котором сообщил сам источник (NewsAPI.ai isDuplicate)."""
    canon = canonicalize_url(url)
    dom = (domain or domain_of(url) or "").lower() or None
    fp = title_fingerprint(title)
    if canon:
        rec_id = hashlib.sha1(canon.encode("utf-8")).hexdigest()
    else:
        day = (published_at or "")[:10]
        rec_id = hashlib.sha1(f"{dom}|{fp}|{day}".encode("utf-8")).hexdigest()
    return {
        "id": rec_id,
        "source": source,
        "url": url,
        "canonical_url": canon,
        "domain": dom,
        "title": title,
        "body": body,
        "lang": normalize_lang(lang),
        "country": normalize_country(country),
        "source_type": classify_source_type(dom, datatype),
        "published_at": published_at,
        "title_fp": fp,
        "event_uri": event_uri,
        "dup_of": None,
        "dup_reason": "provider" if provider_duplicate else None,
        "raw": json.dumps(raw, ensure_ascii=False) if raw is not None else None,
        "fetched_at": now_utc_iso(),
    }


# ---------------------------------------------------------------------------
# БД
# ---------------------------------------------------------------------------

def db_connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con):
    """Идемпотентные миграции схемы (stage-review П1-гейта, HIGH): CREATE TABLE IF NOT EXISTS
    не изменяет СУЩЕСТВУЮЩИЕ таблицы — восстановленная из бэкапа/чужого хоста БД без новых
    колонок тихо ронял бы store() («no column named ...»), а cron глотал бы это как пропуск."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(trends)")}
    if cols and "timeframe" not in cols:
        # окно фетча Trends (П1-гейт 04.07): нормировки окон несравнимы. Легаси-строки получают
        # NULL (кросс-ревью №2, BLOCKER: чем каким окном их реально тянули — знать нельзя, и
        # маркировать их каноном = сохранить timeframe-подмену). NULL-строки канонический расчёт
        # НЕ использует (rows_for_attention фильтрует по окну) — канон наполняется перефетчем.
        con.execute("ALTER TABLE trends ADD COLUMN timeframe TEXT")
        con.commit()


_COLS = ["id", "source", "url", "canonical_url", "domain", "title", "body",
         "lang", "country", "source_type", "published_at", "title_fp",
         "event_uri", "dup_of", "dup_reason", "raw", "fetched_at"]


def upsert_news(con, records):
    """Идемпотентная вставка по id (канонический URL). Повторный заход не плодит строки.
    Возвращает (вставлено_новых, всего_подано)."""
    if not records:
        return 0, 0
    before = con.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    con.executemany(
        f"INSERT OR IGNORE INTO news ({','.join(_COLS)}) "
        f"VALUES ({','.join('?' * len(_COLS))})",
        [tuple(r.get(c) for c in _COLS) for r in records],
    )
    con.commit()
    after = con.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    return after - before, len(records)


# ---------------------------------------------------------------------------
# Дедупликация (§4 «дедупликация»)
# ---------------------------------------------------------------------------

def dedupe_day(con, day, jaccard_threshold=0.82):
    """Near-dup проход по суткам `day` (YYYY-MM-DD по published_at).

    Точный дедуп уже обеспечен PRIMARY KEY id (один канонический URL = одна строка),
    поэтому здесь ловим один сюжет под разными URL/из разных источников: группируем по
    отпечатку заголовка, а близкие отпечатки сливаем по Жаккару токенов.

    Канонический представитель кластера — самая ранняя по published_at запись (при равенстве
    меньший id). Остальным проставляется dup_of=<id канонического> и dup_reason.
    Идемпотентно: пересчитывает кластеры суток с нуля (сначала сбрасывает прежние title/url-метки,
    сохраняя provider-метки источника). Возвращает число помеченных дублей.
    """
    rows = con.execute(
        "SELECT id, title_fp, published_at, dup_reason FROM news "
        "WHERE substr(published_at,1,10)=? ORDER BY published_at ASC, id ASC",
        (day,),
    ).fetchall()
    if not rows:
        return 0

    # Сброс наших прежних меток (url/title), provider-метки не трогаем — это факт источника.
    con.execute(
        "UPDATE news SET dup_of=NULL, dup_reason=NULL "
        "WHERE substr(published_at,1,10)=? AND dup_reason IN ('url','title')",
        (day,),
    )

    items = [{"id": r[0], "fp": r[1] or "", "tokset": set((r[1] or "").split())} for r in rows]
    canon_ids = []          # представители кластеров (порядок = по времени)
    canon_tok = []
    assign = {}             # id -> (canon_id, reason)

    for it in items:
        placed = False
        for cid, ctok in zip(canon_ids, canon_tok):
            if it["fp"] and it["fp"] == _fp_by_id(items, cid):
                assign[it["id"]] = (cid, "title")
                placed = True
                break
            if jaccard(it["tokset"], ctok) >= jaccard_threshold:
                assign[it["id"]] = (cid, "title")
                placed = True
                break
        if not placed:
            canon_ids.append(it["id"])
            canon_tok.append(it["tokset"])

    marked = 0
    for dup_id, (cid, reason) in assign.items():
        if dup_id == cid:
            continue
        con.execute("UPDATE news SET dup_of=?, dup_reason=COALESCE(dup_reason,?) WHERE id=?",
                    (cid, reason, dup_id))
        marked += 1
    con.commit()
    return marked


def _fp_by_id(items, target_id):
    for it in items:
        if it["id"] == target_id:
            return it["fp"]
    return ""


def dedupe_recent(con, days_back=2, **kw):
    """Прогнать near-dup по последним `days_back` суткам (включая сегодня, UTC)."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    total = 0
    for i in range(days_back + 1):
        day = (today - datetime.timedelta(days=i)).isoformat()
        total += dedupe_day(con, day, **kw)
    return total


# ---------------------------------------------------------------------------
# Сводка
# ---------------------------------------------------------------------------

def status(con):
    total = con.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    uniq = con.execute("SELECT COUNT(*) FROM news WHERE dup_of IS NULL").fetchone()[0]
    dups = total - uniq
    print(f"news: всего {total}, уникальных {uniq}, дублей {dups}")
    if total:
        print("  по источнику:",
              dict(con.execute("SELECT source, COUNT(*) FROM news GROUP BY source").fetchall()))
        print("  по типу:    ",
              dict(con.execute("SELECT source_type, COUNT(*) FROM news GROUP BY source_type").fetchall()))
        miss_lang = con.execute("SELECT COUNT(*) FROM news WHERE lang IS NULL").fetchone()[0]
        miss_time = con.execute("SELECT COUNT(*) FROM news WHERE published_at IS NULL").fetchone()[0]
        miss_type = con.execute("SELECT COUNT(*) FROM news WHERE source_type IS NULL").fetchone()[0]
        print(f"  без тега язык/время/тип: {miss_lang}/{miss_time}/{miss_type}")
    tr = con.execute("SELECT COUNT(*) FROM trends").fetchone()[0]
    print(f"trends: точек ряда {tr}")


if __name__ == "__main__":
    con = db_connect()
    status(con)
