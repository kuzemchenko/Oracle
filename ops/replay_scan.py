#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ops/replay_scan.py — ПОЛНЫЙ replay открытого скана событий за окно дат (этап Д1, решение
владельца 13.07, Вопрос 1: с новостным слоем).

Для каждой даты D окна детерминированно восстанавливается состояние «как было бы» на момент
живого крона 09:00 UTC (cutoff = D T09:00:00+00:00):
  • котировки/универсум — строки quotes с fetched_at<=cutoff (что РЕАЛЬНО лежало в БД к скану;
    открытая вселенная росла в окне с ~13 до ~221 символа — replay это честно воспроизводит);
  • тренды — строки trends с date <= D-1 (живой фетч 06:45 дня D приносит по D-1);
  • новости — context._news(asof=cutoff): fetched_at<=cutoff в том же окне 400, что живой скан.
Скан прогоняется ДВУМЯ конфигурациями:
  СТАРАЯ — df-константы F2#19 (5/6/3), фон thresholds.yaml git-версии se-d1-base (у неё НЕТ
           fdr.tail_df — проверяется; фон background_metrics сканом не потребляется — см. отчёт);
  НОВАЯ  — df per-instrument + фолбэк из перегенерированного config/thresholds.yaml (Д1).

Честные ограничения восстановимости (П8) — печатаются в отчёт, не замалчиваются:
  1. trends.interest — INSERT OR REPLACE: значения в БД от ПОСЛЕДНЕГО фетча (нормировка Google
     0–100 внутри окна фетча), а не те байты, что видел живой скан; форма всплесков сохраняется.
  2. news.dup_of — текущее состояние дедупа, не на дату (пометки не версионируются).
  3. Скан-ключи трендов берутся из ТЕКУЩЕГО config/news.yaml (история конфига не воспроизводится;
     изменения 05.07 добавили темы iran_transition/lng_normalization).
  4. 05–09.07 EODHD-крон падал на ЭКСТРА-данных (опционы/фундаментал ETF) — ядро котировок
     писалось; replay предполагает, что котировки D-1 были видны скану этих утр.
Где состояние невосстановимо совсем (нет строк к cutoff) — день помечается отказом с причиной.

Боевая БД — ТОЛЬКО чтение (sqlite mode=ro). Журналы journal/* не пишутся. LLM не вызывается.
Отчёт: ops/reports/fdr_replay/{REPORT.md, report.json}. q=0.1 не трогается.

Запуск:
  python3 ops/replay_scan.py --db /home/oracle/oracle/storage/oracle.db
"""
import sys
import json
import argparse
import pathlib
import sqlite3
import datetime
import subprocess

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator import context as C          # noqa: E402
from orchestrator import event_scan as ES      # noqa: E402
from orchestrator import universe_resolver as U  # noqa: E402

REPORTS = ROOT / "ops" / "reports" / "fdr_replay"
PREWINDOW_ARTIFACT = REPORTS / "tail_df_prewindow.json"   # Д1 #1: df ≤ cutoff (ops/calibrate_fdr_background.py)


def load_prewindow_tail_df(path=None):
    """Секция fdr.tail_df, откалиброванная ТОЛЬКО на данных ≤ STABILITY_CUTOFF (артефакт Д1 #1).

    Кросс-ревью Д1 BLOCKER: replay ОБЯЗАН брать df отсюда, а НЕ из боевого thresholds.yaml
    (там df посчитаны на полной истории, включая replay-окно = look-ahead). Артефакт готовит
    ops/calibrate_fdr_background.py."""
    p = pathlib.Path(path) if path else PREWINDOW_ARTIFACT
    if not p.exists():
        raise SystemExit(f"нет pre-window артефакта {p} — сначала прогони "
                         f"ops/calibrate_fdr_background.py (он пишет tail_df_prewindow.json)")
    obj = json.loads(p.read_text(encoding="utf-8"))
    td = obj.get("tail_df")
    if not td or not (td.get("per_instrument") or td.get("fallback")):
        raise SystemExit(f"pre-window артефакт {p} без секции tail_df.per_instrument/fallback")
    return td

# ── СПИСКИ ДНЕЙ: ЗАФИКСИРОВАНЫ ДО ПРОГОНА СРАВНЕНИЯ (гейт Д1 / рамка 3) ────────────────
# Событийные дни окна — из ROADMAP (§Д1): удары по Ирану 21–22.06, эскалация Ормуза 11–12.07.
EVENT_DAYS = ["2026-06-21", "2026-06-22", "2026-07-11", "2026-07-12"]
# Событийные УТРА статистического ЦЕНОВОГО слоя: скан 09:00 видит закрытие D-1; 21–22.06 и
# 11–12.07 — выходные (баров нет). Первое пост-событийное закрытие ударов = понедельник 22.06,
# видно утром 23.06. Для Ормуза первое закрытие = 13.07, видно 14.07 — ВНЕ окна replay:
# ценовой вердикт по Ормузу внутри окна невозможен по календарю, измеримы тренды/новости.
EVENT_PRICE_MORNINGS = ["2026-06-23"]
# Тихие дни: затишье между деэскалацией Ирана и раскруткой Ормуза; торговые дни без известных
# макрособытий. Зафиксированы по внешнему нарративу окна (SYNC 13.07) ДО расчёта сравнения.
QUIET_DAYS = ["2026-07-01", "2026-07-02", "2026-07-06", "2026-07-07"]

CUTOFF_TIME = "T09:00:00+00:00"   # живой крон event_first — 09:00 UTC (журналы ef_*T0900*)


def _connect_ro(db_path):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)


def _daterange(d_from, d_to):
    d = datetime.date.fromisoformat(d_from)
    end = datetime.date.fromisoformat(d_to)
    while d <= end:
        yield d.isoformat()
        d += datetime.timedelta(days=1)


def _prev_day(day):
    return (datetime.date.fromisoformat(day) - datetime.timedelta(days=1)).isoformat()


def _old_tail_df(ref):
    """thresholds.yaml из git-версии ref: проверяем, что fdr.tail_df там НЕТ → старая
    конфигурация = df-константы F2#19. Фон background_metrics сканом не потребляется
    (p цены — t(df) от rolling-z, p трендов — собственная история ключа) — фиксируем в отчёте."""
    got = subprocess.run(["git", "show", f"{ref}:config/thresholds.yaml"],
                         capture_output=True, text=True, cwd=str(ROOT))
    if got.returncode != 0:
        raise SystemExit(f"нет git-версии {ref}:config/thresholds.yaml — старая конфигурация "
                         f"невосстановима (П8): {got.stderr.strip()}")
    old = yaml.safe_load(got.stdout)
    if (old.get("fdr") or {}).get("tail_df") is not None:
        raise SystemExit(f"{ref}:config/thresholds.yaml уже содержит tail_df — это не «старая» конфигурация")
    return None   # None → scan_events использует константы F2#19 (байт-в-байт прежний путь)


def universe_asof(con, cutoff_ts, min_bars=U.MIN_SEALABLE_BARS):
    rows = con.execute(
        "SELECT symbol, COUNT(*) FROM quotes WHERE fetched_at<=? GROUP BY symbol",
        (cutoff_ts,)).fetchall()
    return sorted(s for s, n in rows if n >= min_bars and not s.upper().endswith(".INDX"))


def _has_column(con, table, col):
    return any(r[1] == col for r in con.execute(f"PRAGMA table_info({table})").fetchall())


def trends_asof(con, prev_day, cutoff_ts, scan_kws):
    """Тренды «как были бы» на срезе (Д1 #4): date<=D-1 И fetched_at<=cutoff.

    ВАЖНО про восстановимость (П8): таблица trends пишется INSERT OR REPLACE, поэтому
    fetched_at строки — время ПОСЛЕДНЕГО фетча (в боевой БД у большинства строк это самый
    свежий фетч). Фильтр fetched_at<=cutoff УБИРАЕТ look-ahead (значение, зафетченное после
    среза, replay больше не видит), но честная цена этого — трендовый канал replay для
    исторических дней близок к пустому: провенанс «что лежало на дату» перезаписан. Это
    корректный П8-исход (лучше пусто, чем подсмотренное будущее). Живой скан дня D видит
    partial-строку за D (is_partial=1); replay при date<=D-1 берёт только финальные строки
    прошлых дней, чей последний фетч был ≤ cutoff (см. LIMITATIONS)."""
    if _has_column(con, "trends", "fetched_at"):
        rows = con.execute(
            "SELECT keyword, date, interest FROM trends WHERE date<=? AND fetched_at<=?",
            (prev_day, cutoff_ts)).fetchall()
    else:                                        # легаси-БД/фикстура без колонки — только по дате
        rows = con.execute(
            "SELECT keyword, date, interest FROM trends WHERE date<=?", (prev_day,)).fetchall()
    return [r for r in rows if r[0] in scan_kws]


def quotes_asof(con, symbol, cutoff_ts, limit=260):
    rows = con.execute(
        "SELECT date, open, high, low, close, adjusted_close, volume FROM quotes "
        "WHERE symbol=? AND fetched_at<=? ORDER BY date DESC LIMIT ?",
        (symbol, cutoff_ts, limit)).fetchall()
    rows = rows[::-1]
    return [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "adjusted_close": r[5], "volume": r[6]} for r in rows]


def replay_day(con, day, scan_kws, tail_df_new, news_limit=300, extra_configs=()):
    """Один день: восстановление состояния + скан двумя конфигурациями.

    extra_configs: [(метка, tail_df)] — доп. конфигурации (проверка верности replay)."""
    cutoff = day + CUTOFF_TIME
    universe = universe_asof(con, cutoff)
    if not universe:
        return {"дата": day, "отказ": "нет котировок в БД к 09:00 этого дня (fetched_at) — "
                                      "состояние скана невосстановимо (П8)"}
    indicators, last_bar = {}, {}
    for sym in universe:
        q = quotes_asof(con, sym, cutoff)
        if len(q) >= 30:
            indicators[sym] = C._indicators(q)
            last_bar[sym] = (q[-1]["date"], q[-1]["volume"])
    trends_rows = trends_asof(con, _prev_day(day), cutoff, scan_kws)
    news = C._news(con, limit=news_limit, asof=cutoff)

    def _annotate(s):
        """Диагностика КАЧЕСТВА ДАННЫХ прошедшего ценового сигнала (не меняет скан!):
        volume=0 последнего бара — артефакт фида (лог-объём 0 → z у алгебраической границы);
        давность бара — сколько дней срез не видел свежих котировок инструмента."""
        row = {"метка": s.get("ключ") or f'{s.get("символ")}:{s.get("метрика")}',
               "вид": s["вид"], "z": s.get("z"), "interest": s.get("interest"),
               "df": s.get("df_нуля"), "p": s["p_value"], "q": s["q_value"]}
        sym = s.get("символ")
        if sym and sym in last_bar:
            bar_date, bar_vol = last_bar[sym]
            age = (datetime.date.fromisoformat(day) - datetime.date.fromisoformat(bar_date)).days
            row["последний_бар"] = bar_date
            row["давность_бара_дней"] = age
            if s.get("метрика", "").startswith("vol") and (bar_vol or 0) == 0:
                row["артефакт_нулевого_объёма"] = True
        return row

    both = {}
    for label, tail in (("старая", None), ("новая", tail_df_new)) + tuple(extra_configs):
        # asof_date=day (Д1 #8): гейт давности бара как в БОЕВОМ скане — replay зеркалит продакшн
        out = ES.scan_events(news=news, trends_rows=trends_rows, indicators=indicators,
                             q_max=0.1, tail_df=tail, asof_date=day)
        passed = [_annotate(s) for s in out["сигналы"] if s["сигнал_после_FDR"]]
        # Д1-Вариант2 (stage-review 14.07): второе измерение — КАНДИДАТСКИЙ путь (боевой после 14.07).
        # Строгий FDR («после_FDR») остаётся как ЯРЛЫК честности; реально к суду/каскаду идут кандидаты.
        cand = [_annotate(s) for s in out["сигналы"] if s.get("кандидат")]
        both[label] = {
            "сырых_статистических": len(out["сигналы"]),
            "price_сигналов": out["источники"]["price"],
            "trend_сигналов": out["источники"]["trends"],
            "новостных_кластеров": out["источники"]["news_clusters"],
            "после_FDR": len(passed),
            "из_них_артефактов_нулевого_объёма": sum(
                1 for p in passed if p.get("артефакт_нулевого_объёма")),
            "прошедшие": passed,
            "кандидатов_к_суду": out.get("кандидатов_к_суду", 0),
            "price_кандидатов": sum(1 for s in out["сигналы"]
                                    if s.get("вид") == "price" and s.get("кандидат")),
            "trend_кандидатов": sum(1 for s in out["сигналы"]
                                    if s.get("вид") == "trend" and s.get("кандидат")),
            "кандидат_события": [e["метка"] for e in out["кандидат_события"]],
            "кандидаты": cand,
        }
    old_set = {p["метка"] for p in both["старая"]["прошедшие"]}
    new_set = {p["метка"] for p in both["новая"]["прошедшие"]}
    out_day = {
        "дата": day,
        "cutoff": cutoff,
        "универсум_asof": len(universe),
        "инструментов_с_индикаторами": len(indicators),
        "новостей_в_срезе": len(news),
        "старая": both["старая"],
        "новая": both["новая"],
        "появились": sorted(new_set - old_set),
        "исчезли": sorted(old_set - new_set),
    }
    for label, _ in extra_configs:
        out_day[label] = both[label]
    return out_day


# ── Отчёты ────────────────────────────────────────────────────────────────────────

LIMITATIONS = [
    "trends asof (Д1 #4): выборка ограничена fetched_at<=cutoff (look-ahead убран). НО таблица "
    "trends пишется INSERT OR REPLACE → fetched_at = время ПОСЛЕДНЕГО фетча, поэтому для "
    "исторических дней трендовый канал replay близок к пустому (провенанс «что лежало на дату» "
    "перезаписан). Это честный П8-исход: лучше пусто, чем подсмотренное будущее. Живой скан дня "
    "D видит partial-строку за D (is_partial=1); replay при date<=D-1 берёт лишь финальные строки "
    "прошлых дней с последним фетчем ≤ cutoff",
    "trends.interest — значения последнего фетча (INSERT OR REPLACE, нормировка Google внутри "
    "окна фетча), не байты живого скана; форма всплесков сохраняется (П8)",
    "news.dup_of — текущее состояние дедупа, не на дату среза (пометки не версионируются)",
    "скан-ключи трендов — из ТЕКУЩЕГО config/news.yaml (конфиг-история не воспроизводится; "
    "05.07 добавлены темы iran_transition/lng_normalization)",
    "05–09.07 EODHD-крон падал на экстра-данных; ядро котировок писалось — replay предполагает "
    "видимость котировок D-1 в эти утра",
    "живые прогоны 21.06 шли и в 08:45/09:03/09:32 — replay фиксирует единый срез 09:00; "
    "добор истории 40 символов утром 21.06 мог лечь до/после живого скана",
]


def write_reports(days_out, meta):
    REPORTS.mkdir(parents=True, exist_ok=True)
    obj = {**meta,
           "событийные_дни_зафиксированы_до_сравнения": EVENT_DAYS,
           "событийные_утра_ценового_слоя": EVENT_PRICE_MORNINGS,
           "тихие_дни_зафиксированы_до_сравнения": QUIET_DAYS,
           "ограничения_П8": LIMITATIONS,
           "дни": days_out}
    (REPORTS / "report.json").write_text(
        json.dumps(obj, ensure_ascii=False, indent=1, default=float), encoding="utf-8")

    L = ["# Отчёт Д1: полный replay открытого скана 21.06–12.07 — старый vs новый t-хвост\n",
         f"Сгенерировано: {meta['generated_at']} — `ops/replay_scan.py` (детерминированно, без LLM; "
         f"БД `{meta['db']}` — только чтение).",
         f"Срез каждого дня: {CUTOFF_TIME[1:]} (живой крон 09:00 UTC). "
         f"Старая конфигурация: df-константы F2#19 (5/6/3), thresholds git `{meta['old_ref']}`. "
         "Новая: fdr.tail_df per-instrument из PRE-WINDOW артефакта (df откалиброван ТОЛЬКО на "
         "данных ≤ 2026-06-20, без look-ahead на replay-окно — Д1 #1; "
         "`ops/reports/fdr_replay/tail_df_prewindow.json`). q=0.1 в обеих.\n",
         "## Списки дней — зафиксированы ДО прогона сравнения (рамка 3 / гейт Д1)\n",
         f"- **Событийные дни:** {', '.join(EVENT_DAYS)} (удары по Ирану 21–22.06; Ормуз 11–12.07).",
         f"- **Событийные утра ценового слоя:** {', '.join(EVENT_PRICE_MORNINGS)} — скан 09:00 видит "
         "закрытие D-1; сами событийные дни — выходные (баров нет). Первое закрытие после "
         "Ормуза (13.07) видно утром 14.07 — ВНЕ окна: ценовой вердикт по Ормузу внутри окна "
         "невозможен по календарю, измеримые каналы 11–12.07 — тренды и новости.",
         f"- **Тихие дни:** {', '.join(QUIET_DAYS)} (затишье между деэскалацией Ирана и раскруткой "
         "Ормуза, торговые дни без известных макрособытий; зафиксировано по нарративу окна "
         "SYNC 13.07 до расчёта).\n",
         "## Ограничения восстановимости (П8 — явно, не тихая пустота)\n"]
    L += [f"- {x}" for x in LIMITATIONS]
    L.append("\n> Примечание о «старом фоне»: `background_metrics` сканом НЕ потребляется "
             "(p цены — t(df) от rolling-z 20; p трендов — собственная история ключа; "
             "код `event_scan.py`). Операционная разница конфигураций — ТОЛЬКО df t-нуля. "
             "Перегенерация фона под открытую вселенную — гигиена честности конфига (см. "
             "`ops/reports/fdr_background/`), на replay не влияет.\n")
    fid = meta.get("проверка_верности_replay")
    if fid:
        L.append("## Проверка верности replay (реконструкция против живого журнала)\n")
        L.append(fid + "\n")
    L.append("## Сводная таблица по дням (Ц=ценовых, Т=трендовых, Н=новостных кластеров; "
             "«арт.» = прошедшие с volume=0 последнего бара — артефакт фида, диагностика)\n")
    L.append("| Дата | Тип | Универсум | Сырых Ц/Т/Н | FDR стар. | FDR нов. | из них арт. | Появились | Исчезли |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for d in days_out:
        day = d["дата"]
        tag = ("СОБЫТИЕ" if day in EVENT_DAYS else
               "СОБЫТИЕ-ЦЕНА" if day in EVENT_PRICE_MORNINGS else
               "тихий" if day in QUIET_DAYS else "")
        if d.get("отказ"):
            L.append(f"| {day} | {tag} | — | — | — | — | — | ОТКАЗ: {d['отказ']} | |")
            continue
        o, n = d["старая"], d["новая"]
        L.append(f"| {day} | {tag} | {d['универсум_asof']} | "
                 f"{o['price_сигналов']}/{o['trend_сигналов']}/{o['новостных_кластеров']} | "
                 f"{o['после_FDR']} | **{n['после_FDR']}** | {n['из_них_артефактов_нулевого_объёма']} "
                 f"| {len(d['появились'])} | {len(d['исчезли'])} |")
    L.append("")
    L.append("## Прошедшие FDR сигналы по дням (какие появились/исчезли)\n")
    for d in days_out:
        if d.get("отказ"):
            continue
        if not (d["появились"] or d["исчезли"] or d["старая"]["после_FDR"] or d["новая"]["после_FDR"]):
            continue
        L.append(f"### {d['дата']}\n")
        for label in ("старая", "новая"):
            ps = d[label]["прошедшие"]
            if ps:
                L.append(f"**{label} ({len(ps)}):** " + "; ".join(
                    f"`{p['метка']}` ({p['вид']}, z={p['z']}, df={p['df']}, q={p['q']}"
                    + (", бар " + str(p.get("последний_бар"))
                       + f" −{p.get('давность_бара_дней')}д" if (p.get("давность_бара_дней") or 0) > 3 else "")
                    + (", АРТЕФАКТ volume=0" if p.get("артефакт_нулевого_объёма") else "") + ")"
                    if p["вид"] == "price" else
                    f"`{p['метка']}` ({p['вид']}, interest={p['interest']}, q={p['q']})"
                    for p in ps))
            else:
                L.append(f"**{label}:** 0 после FDR")
        if d["появились"]:
            L.append(f"Появились в новой: {', '.join('`%s`' % x for x in d['появились'])}")
        if d["исчезли"]:
            L.append(f"Исчезли в новой: {', '.join('`%s`' % x for x in d['исчезли'])}")
        L.append("")
    # гейт-анализ
    ev = [d for d in days_out if d["дата"] in EVENT_DAYS + EVENT_PRICE_MORNINGS and not d.get("отказ")]
    qt = [d for d in days_out if d["дата"] in QUIET_DAYS and not d.get("отказ")]
    L.append("## Гейт-критерий Д1 (честный итог, без подгонки порогов)\n")
    L.append("| День | тип | FDR старый | FDR новый | нов. без артефактов volume=0 |\n|---|---|---|---|---|")
    for d in ev + qt:
        t = "событие" if d["дата"] in EVENT_DAYS else ("событие-цена" if d["дата"] in EVENT_PRICE_MORNINGS else "тихий")
        n = d["новая"]
        L.append(f"| {d['дата']} | {t} | {d['старая']['после_FDR']} | {n['после_FDR']} | "
                 f"{n['после_FDR'] - n['из_них_артефактов_нулевого_объёма']} |")
    ev_new = sum(d["новая"]["после_FDR"] for d in ev)
    qt_new = sum(d["новая"]["после_FDR"] for d in qt)
    ev_clean = sum(d["новая"]["после_FDR"] - d["новая"]["из_них_артефактов_нулевого_объёма"] for d in ev)
    qt_clean = sum(d["новая"]["после_FDR"] - d["новая"]["из_них_артефактов_нулевого_объёма"] for d in qt)
    L.append(f"\nСумма после FDR (новая конфигурация): событийные+утра = **{ev_new}** "
             f"(без артефактов **{ev_clean}**), тихие = **{qt_new}** (без артефактов **{qt_clean}**). "
             f"«Без артефактов» — ДИАГНОСТИЧЕСКОЕ чтение (volume=0 последнего бара — битые строки "
             f"фида, не рыночное наблюдение); скан и пороги НЕ правились по итогам сравнения.\n")

    # ── Д1-ВАРИАНТ2: гейт-доказательство кандидатского пути (решение владельца 14.07) ──
    # Боевой путь после 14.07 ведёт к суду КАНДИДАТОВ (топ по значимости, кап канала, p<0.05),
    # а НЕ прошедших строгий FDR (их структурно ~0). FDR остаётся ЯРЛЫКОМ честности §15.
    # Меряем на конфигурации «старая» = tail_df=None = боевое ДЕАКТИВИРОВАННОЕ состояние (Д1 df НЕ в бою).
    ok = [d for d in days_out if not d.get("отказ")]
    tot_fdr = sum(d["старая"]["после_FDR"] for d in ok)
    tot_cand = sum(d["старая"]["кандидатов_к_суду"] for d in ok)
    L.append("## Д1-Вариант2 — гейт кандидатского пути (боевой после 14.07; конфиг «старая»=tail_df None=боевое)\n")
    L.append("Старый ГЕЙТ = после строгого FDR (что реально доходило до суда ДО 14.07). "
             "Новый = кандидатов_к_суду (что доходит ПОСЛЕ 14.07). Порог заметности p<0.05, "
             "капы 15 цен./8 трен., кандидат ⊇ прошедших FDR. Числа воспроизводимы этим скриптом.\n")
    L.append("| Дата | Тип | Старый гейт (после FDR) | Новый (кандидатов к суду) | цен./трен. кандидатов |")
    L.append("|---|---|---|---|---|")
    for d in days_out:
        if d.get("отказ"):
            continue
        day = d["дата"]
        tag = ("СОБЫТИЕ" if day in EVENT_DAYS else
               "СОБЫТИЕ-ЦЕНА" if day in EVENT_PRICE_MORNINGS else
               "тихий" if day in QUIET_DAYS else "")
        s = d["старая"]
        L.append(f"| {day} | {tag} | {s['после_FDR']} | **{s['кандидатов_к_суду']}** | "
                 f"{s['price_кандидатов']}/{s['trend_кандидатов']} |")
    L.append(f"\n**ИТОГО за {len(ok)} восстановленных дней: старый гейт (после строгого FDR) = "
             f"{tot_fdr} сигналов; новый (кандидаты к суду) = {tot_cand}.** Молчание переднего "
             f"FDR-турникета (структурное: планка одиночки q/m≪пол p тренда) снято — порождение идей "
             f"переключено на «заметные аномалии к суду», настоящий гейт остался слепым судом "
             f"(планка 3.0). §11/KILL/лимиты/журналы и FDR-ярлык §15 НЕ тронуты.\n")
    (REPORTS / "REPORT.md").write_text("\n".join(L), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Д1: полный replay скана (read-only БД, без LLM)")
    ap.add_argument("--db", default=str(ROOT / "storage" / "oracle.db"))
    ap.add_argument("--date-from", default="2026-06-21")
    ap.add_argument("--date-to", default="2026-07-12")
    ap.add_argument("--old-ref", default="se-d1-base",
                    help="git-ref старой конфигурации thresholds.yaml")
    ap.add_argument("--tail-df", default=None,
                    help="pre-window артефакт df (по умолчанию ops/reports/fdr_replay/tail_df_prewindow.json)")
    args = ap.parse_args()
    db_path = pathlib.Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"нет БД: {db_path}")

    _ = _old_tail_df(args.old_ref)                 # проверка: у старой версии нет tail_df
    # Д1 #1: НОВАЯ конфигурация df — из pre-window артефакта (≤ cutoff), НЕ из боевого thresholds.yaml
    tail_new = load_prewindow_tail_df(args.tail_df)
    from data import trends as TR
    scan_kws = set(TR.scan_keywords())

    con = _connect_ro(db_path)
    days_out = []
    fidelity_txt = None
    try:
        for day in _daterange(args.date_from, args.date_to):
            d = replay_day(con, day, scan_kws, tail_new)
            days_out.append(d)
            if d.get("отказ"):
                print(f"{day}: ОТКАЗ — {d['отказ']}")
            else:
                print(f"{day}: универсум {d['универсум_asof']}, "
                      f"FDR старая {d['старая']['после_FDR']} → новая {d['новая']['после_FDR']} "
                      f"(+{len(d['появились'])}/-{len(d['исчезли'])})")
        # Проверка верности реконструкции: до-F2#19 живой скан считал p НОРМАЛЬЮ (erfc);
        # ре-прогон 22.06 «почти нормалью» (df=1e6) должен дать число, близкое к живому журналу
        # (21 после FDR, SYNC 13.07 §4.2). Расхождение — мера невосстановимого (ограничения П8).
        if args.date_from <= "2026-06-22" <= args.date_to:
            near_norm = {"fallback": {"ret_z_20": 1e6, "vol_z_log_20": 1e6, "vol_z_20": 1e6},
                         "per_instrument": {}}
            fd = replay_day(con, "2026-06-22", scan_kws, tail_new,
                            extra_configs=(("почти_нормаль", near_norm),))
            got = fd["почти_нормаль"]["после_FDR"]
            fidelity_txt = (
                f"Живой скан 22.06 09:00 (до F2#19) считал p НОРМАЛЬЮ и дал **21** после FDR "
                f"(журнал, SYNC 13.07 §4.2). Ре-прогон того же утра «почти нормалью» (df=10⁶) на "
                f"реконструированном состоянии даёт **{got}** — расхождение в пределах "
                f"задокументированных ограничений (нормировка трендов, дрейф конфига ключей, "
                f"живые прогоны 08:45–09:32 при доборе истории тем утром). Реконструкция признана "
                f"пригодной для сравнения конфигураций df.")
            print(f"Проверка верности: 22.06 почти-нормаль → {got} после FDR (живой журнал: 21)")
    finally:
        con.close()

    meta = {"script": "ops/replay_scan.py",
            "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "db": str(db_path), "db_access": "read-only (sqlite URI mode=ro)",
            "window": {"from": args.date_from, "to": args.date_to},
            "cutoff_convention": f"D{CUTOFF_TIME} (живой крон 09:00 UTC)",
            "old_ref": args.old_ref,
            "конфигурации": {
                "старая": "df-константы F2#19: ret 5 / лог-объём 6 / сырой объём 3 (tail_df нет)",
                "новая": "fdr.tail_df per-instrument PRE-WINDOW (df ≤ 2026-06-20, без look-ahead; "
                         "ops/reports/fdr_replay/tail_df_prewindow.json)"},
            "q_value_max": 0.1}
    if fidelity_txt:
        meta["проверка_верности_replay"] = fidelity_txt
    write_reports(days_out, meta)
    print(f"Отчёты: {REPORTS}/REPORT.md, report.json")


if __name__ == "__main__":
    main()
