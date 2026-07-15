# -*- coding: utf-8 -*-
"""orchestrator/event_first.py — EVENT-FIRST КОНТУР end-to-end (Этап 6 PLAN_cascade_first.md).

Сшивает всё: открытый скан §6 (event_scan) → шок-источники из событий → ДЛЯ КАЖДОГО прогоняется
ПОЛНЫЙ состязательный контур run_funnel (агенты §4 + слепой суд П10, mock/live) ДЛЯ качественной
проверки (тайминг/манипуляция/кто-продаёт-нам/дебаты) + ДЕТЕРМИНИРОВАННЫЙ каскад-резолв §9
(амплитуда из калиброванной чувствительности) для торгуемых спеков. Дирижёр сводит и диверсифицирует.

Развязка открытие/запечатывание: контур и каскад открыты по событиям; запечатываемые §9-спеки —
только разрешимые (cascade_resolve), остальное — лист ожидания. seal в журнал — отдельно (live + §11).
mode='mock' прогоняет агентов БЕЗ сети/трат — доказательство сшивки перед live.
"""
import json
import sqlite3
import datetime
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from orchestrator import event_scan as ES            # noqa: E402
from orchestrator import cascade_resolve as CR       # noqa: E402
from orchestrator import forecast as FC              # noqa: E402
from orchestrator import cascade_build as CB          # noqa: E402
from orchestrator import graph_build as GB            # noqa: E402
from orchestrator import universe_resolver as U      # noqa: E402
from orchestrator import event_mapping as EM         # noqa: E402
from orchestrator import openrouter as OR             # noqa: E402
from orchestrator import funnel as F                  # noqa: E402
from orchestrator import multi_event as ME            # noqa: E402
from orchestrator import progress as PROG              # noqa: E402
from orchestrator import run_budget as RB              # noqa: E402
from orchestrator import attention_field as AF         # noqa: E402  (П2а §R4.2: инфо-поле «внимание»)
from mathlib import cascade as CAS                    # noqa: E402

DB = ROOT / "storage" / "oracle.db"
LOGS = ROOT / "journal" / "funnel_logs"

# FГ B2: сколько шок-источников ИЩЕМ (широта скана) — отдельно от k (сколько прогоняем через
# дорогой контур). Раньше оба были k=2: узкий скан привязан к бюджету контура. Теперь скан шире.
MAX_SHOCK_SOURCES = 8


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _last_return(con, symbol):
    """УСТАРЕЛО для шока каскада (1-дневная доходность рассинхронена с realized за окно §R2.1 — см.
    _window_return). Оставлено только для event_first_dryrun. В боевой свёртке НЕ использовать."""
    rows = con.execute(
        "SELECT adjusted_close FROM quotes WHERE symbol=? AND adjusted_close IS NOT NULL "
        "ORDER BY date DESC LIMIT 6", (symbol,)).fetchall()
    px = [float(r[0]) for r in rows][::-1]
    r = CAS.log_returns(px)
    return float(r[-1]) if r.size else None


def _window_return(con, symbol, asof=None):
    """§R2.1: доходность символа за ОКНО реакции на событие — шок корня каскада (выровнен с realized).

    F3#25 (§5.3): asof-ГЕЙТ — окно оканчивается последним баром НЕ ПОЗЖЕ asof ('YYYY-MM-DD'), а не
    «последние N баров в БД» безусловно (SQL date<=asof). Это защитный отсекатель случайных
    БУДУЩИХ/интрадей-баров в момент решения (live). asof=None → прежнее поведение (весь хвост).

    ГРАНИЦЫ (не переоценивать покрытие — stage-review F3):
      • Полноценного backtest/replay-режима у этого контура НЕТ: run_event_first якорит asof на дату
        ПРОГОНА (now), а не на исторический cutoff, и параметра cutoff не принимает. Значит для
        реального masked/replay (событие в прошлом) гейт бесполезен — это ДОЛГ, а не готовая защита.
      • Гейт наложен ТОЛЬКО на шок КОРНЯ. Терминальные realized/vol считаются внутри
        cascade_build.build_from_db БЕЗ asof — соосность окон (§R2.1) держится лишь пока asof=now.
        Настоящий replay потребует протянуть asof и в build_from_db (тот же ДОЛГ)."""
    if asof:
        rows = con.execute(
            "SELECT COALESCE(adjusted_close, close) FROM quotes WHERE symbol=? "
            "AND COALESCE(adjusted_close, close) IS NOT NULL AND date <= ? "
            "ORDER BY date DESC LIMIT ?", (symbol, asof, CAS.EVENT_WINDOW_DAYS + 1)).fetchall()
    else:
        rows = con.execute(
            # F0#8: adjusted_close (корпдействия) — иначе на сплите/дивиденде ложный скачок шока корня
            "SELECT COALESCE(adjusted_close, close) FROM quotes WHERE symbol=? "
            "AND COALESCE(adjusted_close, close) IS NOT NULL "
            "ORDER BY date DESC LIMIT ?", (symbol, CAS.EVENT_WINDOW_DAYS + 1)).fetchall()
    px = [float(r[0]) for r in rows][::-1]
    return CAS.window_return(px)


def _shock_sources(scan, universe, con, max_sources):
    """Источники шока из ОТКРЫТОГО скана: значимые ценовые сигналы + прокси тем, чьи слова совпали
    с салиентными новостными кластерами (событие дня → инструмент-исток)."""
    cand = []
    # Д1-Вариант2 + stage-review 14.07: новостные прокси СНАЧАЛА — дорогой 21-агентный контур обязан
    # якориться на СОБЫТИИ дня (новость), а не только на крупнейшем ценовом движении. Иначе ценовые
    # кандидаты (их до 15) монополизировали бы sources[:k] и вытесняли событие. Ценовые кандидаты
    # (топ по значимости, возр. q_value) ЗАПОЛНЯЮТ оставшиеся слоты.
    for ne in scan["новостные_события"][:8]:
        theme, _ = EM.match_cluster_to_theme({"keywords": ne["ключи"], "sample": ne["пример"]}, universe)
        if theme:
            proxy = ((universe.get("themes") or {}).get(theme) or {}).get("proxy_etf")
            if proxy:
                cand.append(proxy)
    cand_sigs = sorted((s for s in scan["сигналы"] if s.get("кандидат") and s.get("символ")),
                       key=lambda s: (s.get("q_value") if s.get("q_value") is not None else 1.0))
    for s in cand_sigs:
        cand.append(s["символ"])
    # Этап2 (роутинг трендов до суда, закрытие долга Д1-Вариант2): трендовый кандидат → тема → proxy_etf
    # (как новостной прокси). Заполняют оставшиеся слоты — трендовый канал реально доходит до контура.
    for _key, proxy in _trend_proxy_syms(scan, universe):
        cand.append(proxy)
    seen, uniq = set(), []
    for s in cand:
        if s in seen or not U.is_sealable(s, con=con):
            continue
        seen.add(s)
        uniq.append(s)
    return uniq[:max_sources]


def _trend_proxy_syms(scan, universe):
    """Этап2: трендовый КАНДИДАТ → тема универсума (матч по ключу, как новостной кластер) → proxy_etf.
    Возвращает [(ключ, proxy)] для ЗАМАПЛЕННЫХ. Незамапленные честно выпадают (П8: нет привязки к
    инструменту — трендовый канал доходит до суда ТОЛЬКО через реальный инструмент, а не декларацией).
    Закрывает долг Д1-Вариант2 (трендовые кандидаты считались, но кода-пути до суда не имели)."""
    themes = (universe or {}).get("themes") or {}
    out = []
    for s in scan.get("сигналы", []):
        if s.get("вид") != "trend" or not s.get("кандидат"):
            continue
        key = s.get("ключ")
        theme, _ = EM.match_cluster_to_theme({"keywords": [key], "sample": key or ""}, universe)
        proxy = ((themes.get(theme) or {}).get("proxy_etf")) if theme else None
        if proxy:
            out.append((key, proxy))
    return out


def _price_signal_syms(scan):
    """Символы с заметным ЦЕНОВЫМ сигналом-кандидатом — узлы цепочки могут совпасть (Д1-Вариант2:
    кандидаты, не строгий BH). Трендовые узлы добавляет _candidate_node_syms (Этап2)."""
    return sorted({s["символ"] for s in scan["сигналы"]
                   if s.get("кандидат") and s.get("символ")})


def _daily_debate_alert(mode, now, *, costs_log=None, limits_path=None, notices=None):
    """Этап2 (2.4): суммарная стоимость LLM за СЕГОДНЯ > дневного ориентира (config/limits.yaml
    budget.daily_debate_alert_usd, дефолт = месячный токен-бюджет/30) → АЛЕРТ владельцу в
    journal/notices.jsonl (бот пушит). Прогон НЕ урезается, пороги НЕ двигаются (§12). Дедуп: один
    алерт в день (маркер с датой). mode=mock → не считаем. Возвращает {spent_today, cap, over}.
    Пути опциональны (тестируемость); по умолчанию — боевые файлы репозитория."""
    if mode == "mock":
        return None
    limits_path = pathlib.Path(limits_path) if limits_path else ROOT / "config" / "limits.yaml"
    costs_log = pathlib.Path(costs_log) if costs_log else ROOT / "journal" / "costs.jsonl"
    notices = pathlib.Path(notices) if notices else ROOT / "journal" / "notices.jsonl"
    try:
        limits = yaml.safe_load(open(limits_path, encoding="utf-8")) or {}
    except Exception:
        return None
    b = limits.get("budget") or {}
    cap = b.get("daily_debate_alert_usd")
    cap = float(cap) if cap is not None else float(b.get("tokens_usd_month", 500)) / 30.0
    today = now.date().isoformat()
    spent = 0.0
    if costs_log.exists():
        for line in costs_log.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("mode") != "mock" and str(rec.get("ts", "")).startswith(today):
                spent += float(rec.get("cost_usd") or 0)
    res = {"spent_today": round(spent, 4), "cap": round(cap, 4), "over": spent > cap}
    if not res["over"]:
        return res
    marker = f"[дневной-расход {today}]"
    if notices.exists() and marker in notices.read_text(encoding="utf-8"):
        return res                                       # уже алертили сегодня — не спамим
    text = (f"{marker} Расход на работу за сегодня {spent:.2f}$ превысил дневной ориентир "
            f"{cap:.2f}$ (= месячный токен-бюджет ${b.get('tokens_usd_month', 500)}/30). Прогон НЕ "
            f"урезан и пороги НЕ тронуты — сообщаю, чтобы ты видел расход (§12).")
    notices.parent.mkdir(parents=True, exist_ok=True)
    with open(notices, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "text": text},
                           ensure_ascii=False) + "\n")
    return res


def _candidate_node_syms(scan, universe):
    """Этап2: инструменты-кандидаты для активации каскадных цепочек = ценовые кандидаты + трендовые
    кандидаты, замапленные на proxy_etf. Так трендо-событийный день активирует цепочку и через тренд."""
    return sorted(set(_price_signal_syms(scan)) | {p for _k, p in _trend_proxy_syms(scan, universe)})


def _theme_for_chain(chain_id, universe):
    """Тема универсума, привязанная к цепочке (themes.<t>.cascade_chain == chain_id)."""
    for tname, t in (universe.get("themes") or {}).items():
        if (t or {}).get("cascade_chain") == chain_id:
            return tname
    return None


def activated_chains(scan, universe, chains, price_signal_syms):
    """ШИРОКАЯ активация (дыра №1): авторская цепочка включается, если её ТЕМА салиентна в новостях
    ИЛИ есть ценовой сигнал на ЛЮБОМ её узле — а не только когда якорь попал в источники шока.
    Якорь (узел минимального порядка) даёт корневой шок для свёртки вниз по каскаду.
    """
    # тема → КОНКРЕТНЫЙ заголовок, который её активировал (П8: не «тема салиентна», а сама новость).
    matched = {}
    for ne in scan["новостные_события"][:12]:
        th, _ = EM.match_cluster_to_theme({"keywords": ne["ключи"], "sample": ne["пример"]}, universe)
        if th and th not in matched:
            matched[th] = (ne.get("пример") or "").strip()
    sigset = set(price_signal_syms or [])
    out = []
    for ch in (chains or []):
        reasons = []
        событие = None  # человекочитаемый «повод» = реальный заголовок (для дайджеста «новость→…→компания»)
        th = _theme_for_chain(ch.get("id"), universe)
        if th and th in matched:
            hl = matched[th]
            if hl:
                событие = hl
                reasons.append(f"тема «{th}» — новость: {hl[:160]}")
            else:
                reasons.append(f"тема «{th}» салиентна в новостях")
        node_syms = {i for n in (ch.get("nodes") or []) for i in (n.get("instruments") or [])}
        hit = node_syms & sigset
        if hit:
            reasons.append(f"ценовой сигнал на узле(ах): {sorted(hit)}")
        if reasons:
            anchor_nodes = sorted((ch.get("nodes") or []), key=lambda n: n.get("order", 0))
            anchor = (anchor_nodes[0].get("instruments") or [None])[0] if anchor_nodes else None
            out.append({"chain": ch, "anchor": anchor, "причины": reasons, "событие_новость": событие})
    return out


def _proposal_ideas(proposals):
    """РЕШЕНИЕ «A» (анти-brent): предложения LLM-картографа по событиям ВНЕ реестра тем → research-идеи
    на КОНКРЕТНЫХ компаниях (целевой дальний чокпоинт-узел), ранг по тектоническому потенциалу. Вход
    не зажат темами — Украина/ставки/золото и т.п. рождают идеи, а не молчат. Не запечатываются (П16):
    тег research, в Brier-трек не идут; полная картина §8 не прячется."""
    ideas = []
    for p in (proposals or []):
        far = p.get("целевой_дальний_узел") or {}
        insts = far.get("instruments") or []
        if not insts:
            continue
        ideas.append({
            "актив": insts[0], "все_инструменты": insts,
            "событие": p.get("событие"), "ключи": p.get("ключи"),
            "порядок_узла": far.get("order"), "чокпоинт": bool(far.get("chokepoint")),
            "тектонический_потенциал": p.get("тектонический_потенциал"),
            "отыгранность_узла": far.get("priced"),
            "research": True, "источник_идеи": "LLM-картограф (вне реестра тем)",
            "узлы_каскада": p.get("узлы"),
        })
    ideas.sort(key=lambda x: (x.get("тектонический_потенциал") or -1), reverse=True)
    return ideas


def _cartographer_pass(scan, universe, mode, run_id, max_map=10, guard=None):
    """B2.5 (§R2): картограф ОДИН раз — новостные кластеры вне реестра тем → каскадные карты. Один
    результат кормит И воронку отбора (узлы графа), И стейджинг/поток research-идей (дедуп: раньше
    картограф гонялся дважды). LLM-путь — только live/auto; mock → []. max_map — кэп бюджета §30."""
    if mode == "mock":
        return []
    import os
    client = OR.make_client(mode=mode, run_id=run_id)
    if guard is not None:
        client.cost_guard = guard            # F0#9: стоп-на-лету по потолку режима (§24)
    checker = EM.make_eodhd_checker(os.environ.get("EODHD_API_KEY", ""))
    tl = EM.make_eodhd_type_lookup(os.environ.get("EODHD_API_KEY", ""))
    clusters = [{"keywords": ne["ключи"], "sample": ne["пример"], "salience": ne["салиентность"]}
                for ne in scan.get("новостные_события", [])]
    return EM.proposal_chains(clusters, universe, client, checker, type_lookup=tl, max_map=max_map)


def _stage_cartographer(pcs, now):
    """Стейджинг карт картографа в proposed_themes (§30) + форма для _proposal_ideas (поток research).
    Берёт ТЕ ЖЕ карты, что и воронка (дедуп) — повторного LLM-вызова нет."""
    out = []
    ts = now.isoformat(timespec="seconds")
    for pc in pcs:
        m = pc["mapped"]
        rec = EM.stage_proposal(m, ts)
        tec = m.get("tectonic") or {}
        out.append({"событие": rec["событие"], "ключи": pc.get("ключи"), "узлы": rec["узлы"],
                    "staged": True, "тектонический_потенциал": tec.get("tectonic_potential"),
                    "целевой_дальний_узел": tec.get("best_far_node")})
    return out


def _money_kind(verdict):
    """Слепой суд РАЗБИЛ money-идею (или ВЕТО) → демотируем в провизорный (НЕ пускаем к §11).
    Иначе (устояла / не судили) — money. Это и есть гейт качества денежного трека (П10).
    verdict — либо строка-исход (старый контракт/тесты), либо подробный dict {исход,...}.

    Решение D (вариант 3): процедурное вето Дирижёра (§5) от ПОЛНОГО §8-контура (тайминг ЛОВУШКА/
    ПОЗДНО или манипуляция выше порога) тоже демотирует money→провизорный — это не суждение о рынке,
    а процедурный стоп-фильтр §6 (П10 цел: вето применяет Дирижёр, не подменяя слепого судью)."""
    if isinstance(verdict, dict) and verdict.get("процедурное_вето"):
        return "cascade_provisional"
    outcome = verdict.get("исход") if isinstance(verdict, dict) else verdict
    # F0#2: FAIL-CLOSED — деньги ТОЛЬКО при явном «УСТОЯЛА» слепого суда. Всё прочее (None=не судили,
    # РАЗБИТА/ВЕТО, ПРОПУСК=нет котировки, ОШИБКА_СУДА=сбой) → провизорный. Раньше дефолт был
    # cascade_money → несуженный хвост и упавшие дебаты протекали в §11-деньги без суда (гейт обходился).
    return "cascade_money" if outcome == "УСТОЯЛА" else "cascade_provisional"


def _событие_из_цепочки(ch_meta):
    """Чистый текст события (П8: РОВНО то, что активировало). Приоритет — конкретный заголовок
    новости (событие_новость / картограф), иначе служебная причина активации."""
    if not isinstance(ch_meta, dict):
        return None
    if ch_meta.get("событие_новость"):
        return ch_meta["событие_новость"]
    акт = ch_meta.get("активация") or []
    for r in акт:
        if str(r).startswith("картограф: "):
            return str(r)[len("картограф: "):]
    return (акт or [None])[0]


def _money_thesis(n, событие=None):
    """Содержательный тезис для состязательного суда из ПОСЧИТАННЫХ полей каскадного узла (П8):
    событие-исток → механизм цепочки по звеньям → ожидаемый ход и НЕотыгранный edge с надёжностью →
    тайминг. Раньше суду уходил голый «edge {число}» — судья по П8 честно валил «нет данных»; здесь
    собираем ровно то, что система уже посчитала (ничего не выдумываем сверх полей узла)."""
    def _p(x):
        return f"{x * 100:+.1f}%" if isinstance(x, (int, float)) else None
    # F0#1: узел приходит как FACT (node_to_facts) — читаем его имена (edge_total/reliability/lag_days)
    # с фолбэком на сырые имена узла. Раньше читались только сырые → судья видел full/r2/lag=None.
    amp = n.get("amplitude")
    full = n.get("edge_total", n.get("amplitude_total"))
    r2 = n.get("reliability", n.get("reliability_r2"))
    order = n.get("order")
    lag = n.get("lag_days", n.get("lag_total"))
    prob, root = n.get("probability"), n.get("root")
    chain = [str(x) for x in (n.get("провенанс_звеньев") or []) if x and "без данных" not in str(x)]
    parts = []
    if событие:
        parts.append(f"Событие-исток: {событие}")
    if root:
        parts.append(f"Корневой шок идёт от якоря {root}.")
    if chain:
        parts.append("Механизм по звеньям каскада: " + " → ".join(chain) + ".")
    if order is not None:
        parts.append(f"Целевой узел — {order}-й порядок цепочки"
                     + (" (узкое место/чокпоинт — через него идёт весь поток)" if n.get("chokepoint") else "") + ".")
    if _p(full):
        parts.append(f"Расчётный полный ход цели ≈ {_p(full)}.")
    if _p(amp):
        parts.append(f"НЕотыгранный рынком edge ≈ {_p(amp)} (расчётный ход за вычетом уже реализованного на терминале).")
    if isinstance(r2, (int, float)):
        parts.append(f"Надёжность связи r²={r2} (доля колебаний звена, объяснённая корнем на истории цен).")
    if isinstance(lag, (int, float)) and lag:
        parts.append(f"Историческое окно доведения реакции ≈ {int(round(lag))} дн.")
    if isinstance(prob, (int, float)):
        parts.append(f"Базовая вероятность хода ≈ {prob * 100:.0f}% (нормальная модель, БЕЗ учёта новости).")
    return " ".join(parts) or "каскадный узел: посчитанных полей нет (П8 — нет данных для тезиса)"


def _deep_report_money(cand, debate, ctx, client):
    """ВАРИАНТ 3 (решение D): ПОЛНЫЙ §4-контур качества §8 на ОДНУ пережившую слепой суд money-идею.
    Дешёвый каскад+money-vet — ежедневный фильтр; полный контур (тайминг/манипуляция/неочевидность/
    риск/синтез 13 полей §8) тратится ТОЧЕЧНО — только на идею, которая претендует в деньги. Дебаты
    (генератор/критик/слепой судья) УЖЕ проведены (передаём debate), второй раз их не гоняем — §8
    добирает измерения, которых money-vet не делает. Возвращает отчёт + процедурное_вето (§6/§5)."""
    from orchestrator import context as _C
    from orchestrator import synthesis as SY
    from orchestrator import agents as A
    thresholds = _C._load_yaml("config/thresholds.yaml") or {}
    manip_thr = F.manip_block_threshold(thresholds)   # F0#4: единый ключ score_block_threshold (был дефолт 70)
    # пер-кандидатные качественные измерения §8, которых money-vet НЕ делает (тайминг/манип/неочевидн.)
    quality = {}
    for aid, key in (("d_timeliness", "тайминг"), ("d_anti_manipulation", "манипуляция"),
                     ("c_non_obviousness", "неочевидность")):
        try:
            rec = A.call_agent(aid, ctx, client,
                               user_prompt=F._candidate_slice_for(aid, cand, ctx, thresholds))
            quality[key] = rec.get("judgment") if rec.get("ok") else {"_ошибка": rec.get("error")}
        except Exception:  # noqa: BLE001
            quality[key] = None
    tv = str((quality.get("тайминг") or {}).get("вердикт") or "").upper()
    mscore = (quality.get("манипуляция") or {}).get("балл")
    причины_вето = []
    if tv in ("ПОЗДНО", "ЛОВУШКА"):
        причины_вето.append(f"тайминг {tv} без контр-сценария (§6)")
    if isinstance(mscore, (int, float)) and mscore >= manip_thr:
        причины_вето.append(f"манипуляционный балл {mscore} ≥ {manip_thr} (§6)")
    # Ревью 2026-07-04 M1: сбой вето-несущей проверки (тайминг/манипуляция) раньше был FAIL-OPEN —
    # quality=None → «вето нет» → идея шла в §11-трек БЕЗ обещанной --deep проверки. Fail-closed,
    # как F0#2: непройденная проверка на money-пути = процедурное вето с честной причиной
    # (демотирование в провизорный, не потеря идеи).
    for key in ("тайминг", "манипуляция"):
        q = quality.get(key)
        if q is None or (isinstance(q, dict) and q.get("_ошибка")):
            причины_вето.append(f"проверка «{key}» не выполнена (сбой агента) — вето fail-closed (§6)")
    # риск-агент (§4 блок F) + синтез 13 полей §8
    prob_j = (debate.get("вердикт") or {}).get("вероятность_судьи")
    risk = SY.run_risk({**cand, "вероятность_судьи": prob_j}, ctx, client, costs={})
    bundle = {
        "идея": {k: cand.get(k) for k in ("актив", "направление", "тезис", "разрешимость", "школа")},
        "дело_каскада": cand.get("дело_каскада"),
        "вероятность_судьи": prob_j,
        "риск": SY._rec({"f_risk": risk}, "f_risk") or {"_ошибка": (risk or {}).get("error")},
        "качество_§8": quality,
        "вердикт_судьи": debate.get("вердикт"),
        "позиции_критика_и_судьи": {
            "критик": SY._rec({"x": (debate.get("реплики") or {}).get("критик")}, "x"),
            "судья": SY._rec({"x": (debate.get("реплики") or {}).get("судья")}, "x"),
        },
    }
    rep = SY.synthesize_report(bundle, ctx, client)
    return {
        "отчёт_§8": (rep.get("judgment") if rep.get("ok") else {"_ошибка": rep.get("error")}),
        "качество": quality,
        "риск": SY._rec({"f_risk": risk}, "f_risk") or {"_ошибка": (risk or {}).get("error")},
        "процедурное_вето": bool(причины_вето),
        "причина_вето": "; ".join(причины_вето) or None,
    }


def _vet_money(money_members, run_id, top_k=3, chain_events=None, deep_report=False, guard=None):
    """ПЕРЕНАПРАВЛЕНИЕ КОНТУРА: дорогой состязательный суд (генератор/критик/слепой судья П10) на
    топ-K money-каскадов (ярус A → путь к деньгам §11), а НЕ на слепые шок-источники (там бюджет
    горел впустую). Возвращает {symbol: исход}. ~$1/идея (точечный разбор, не вся 21-агентная воронка).

    chain_events: {chain_id: событие} — событие-исток цепочки, чтобы судья видел ПОВОД, а не голый edge.
    Котировку/индикаторы по каскадной мишени инъектируем в ctx ЯВНО: build_context знает только CORE,
    а мишени каскада — вне CORE; без этого судья видит null-котировку и структурно валит идею."""
    from orchestrator import context as _C
    from orchestrator import debate as _D
    from orchestrator.openrouter import LiveClient
    chain_events = chain_events or {}
    ctx = _C.build_context()
    # Ревью 2026-07-04 HIGH: LiveClient без ключа (побитый .env, отозванный ключ) бросал
    # RuntimeError ВНЕ try — прогон умирал на этапе суда без протокола и уведомления, хотя скан
    # и картограф уже отработали. Fail-closed + graceful: суд недоступен → ОШИБКА_СУДА для всех
    # кандидатов (→ демотирование в провизорный через _money_kind), контур доводит прогон до конца.
    try:
        client = LiveClient(run_id=run_id)
    except Exception as e:  # noqa: BLE001
        return {s.get("symbol"): {"исход": "ОШИБКА_СУДА",
                                  "примечание": f"суд недоступен (клиент LLM): {e} — money демотированы fail-closed"}
                for s in (money_members or [])[:top_k] if s.get("symbol")}
    if guard is not None:
        client.cost_guard = guard            # F0#9: стоп-на-лету по потолку режима (§24)
    con = sqlite3.connect(str(_C.DB), timeout=30) if _C.DB.exists() else None
    out = {}
    try:
        for s in (money_members or [])[:top_k]:
            n = s.get("node") or {}
            sym, amp = s.get("symbol"), n.get("amplitude")
            direction = "лонг" if (amp or 0) > 0 else "шорт"
            # котировка/индикаторы ПО ЭТОМУ символу (вне CORE) → судья видит цену, а не null
            if con is not None and sym and sym not in ctx.get("quotes", {}):
                q = _C._quotes(con, sym)
                if q:
                    ctx["quotes"][sym] = {"last": q[-1], "n_bars": len(q),
                                          "first_date": q[0]["date"], "last_date": q[-1]["date"]}
                    ctx["indicators"][sym] = _C._indicators(q)
            # P2#8: ФЕЙЛ-ФАСТ — нет котировки терминала (после попытки инъекции) → НЕ гоним дорогой
            # суд (он структурно завалит «нет цены»), помечаем research-only. Экономит бюджет и не
            # засчитывает идее провал из-за дыры данных, а не по существу.
            if not (ctx.get("quotes", {}).get(sym) or {}).get("last"):
                out[sym] = {"исход": "ПРОПУСК", "направление": direction,
                            "примечание": "нет котировки терминала — research-only, не на состязательный суд (P2#8)"}
                continue
            событие = chain_events.get(n.get("_chain"))
            # P2#7: ПРОВЕРЯЕМОЕ дело каскада судье/ревьюеру (факты, не авторство — П10 цел)
            дело = {
                "событие_исток_заголовок": событие,
                "якорь_корневого_шока": n.get("root"),
                "механизм_по_звеньям": [str(x) for x in (n.get("провенанс_звеньев") or []) if x],
                "порядок_узла": n.get("order"), "чокпоинт": bool(n.get("chokepoint")),
                # F0#1: читаем имена FACT (edge_total/reliability/lag_days) с фолбэком на сырые
                "ожидаемый_полный_ход": n.get("edge_total", n.get("amplitude_total")),
                "неотыгранный_edge": amp, "надёжность_r2": n.get("reliability", n.get("reliability_r2")),
                "лаг_дней": n.get("lag_days", n.get("lag_total")),
                "котировка_терминала": (ctx.get("quotes", {}).get(sym) or {}).get("last"),
                # П2а (§R4.2): детерминированное поле «внимание» (Trends, gate-P1) — вход тайминга §8;
                # судья видит данные, вердикт остаётся за ним (на ранжирование поле не влияет)
                "внимание_trends": s.get("внимание"),
            }
            cand = {"актив": sym, "направление": direction,
                    "тезис": _money_thesis(n, событие),
                    "дело_каскада": дело, "школа": "каскад", "разрешимость": None}
            try:
                d = _D.run_debate(cand, ctx, client, run_id=f"{run_id}__vet__{sym}")
                v = d.get("вердикт") or {}
                judge = ((d.get("реплики") or {}).get("судья") or {}).get("judgment") or {}
                # Сохраняем НЕ только ярлык исхода, но и аргументацию суда (§8: «кто против и почему
                # неправ», «почему возможность ещё существует»), чтобы дайджест объяснял, а не клеймил.
                out[sym] = {
                    "исход": v.get("исход"), "направление": direction,
                    "балл": v.get("средний_балл_рубрики"), "порог": v.get("порог"),
                    "кто_против": judge.get("кто_продаёт_нам_и_почему_неправ"),
                    "почему_возможность": judge.get("почему_возможность_ещё_существует"),
                    "примечание": v.get("примечание") or v.get("причина"),
                }
                # ВАРИАНТ 3 (решение D): идея ПЕРЕЖИЛА слепой суд (устояла, не ПРОПУСК) → полный §8-контур
                # ТОЧЕЧНО на неё (тайминг/манип/неочевидн./риск/синтез 13 полей). Процедурное вето §6
                # может демотировать её money→провизорный (_money_kind это видит). Бюджет: лишь survivors.
                if deep_report and _money_kind(out[sym]) == "cascade_money":
                    deep = _deep_report_money(cand, d, ctx, client)
                    out[sym]["отчёт_§8"] = deep["отчёт_§8"]
                    out[sym]["качество"] = deep["качество"]
                    out[sym]["риск"] = deep["риск"]
                    out[sym]["процедурное_вето"] = deep["процедурное_вето"]
                    out[sym]["причина_вето"] = deep["причина_вето"]
            except Exception as e:  # noqa: BLE001
                # F0#2: FAIL-CLOSED — сбой суда (нет ключа/сеть/невалидный JSON) НЕ должен молча
                # промотировать идею к деньгам. Маркер ОШИБКА_СУДА → _money_kind → провизорный.
                out[sym] = {"исход": "ОШИБКА_СУДА", "направление": direction,
                            "примечание": f"сбой состязательного суда: {type(e).__name__}: {e}"}
    finally:
        if con is not None:
            con.close()
    return out


REPLAY_LOGS = ROOT / "journal" / "replay_logs"


def run_replay(cutoff, *, top_k=8, horizon_days=5, write=True):
    """REPLAY-режим (долг F3/П2а, закрыт ночью 04.07): детерминированный граф каскадов
    «как был бы на дату cutoff» — сырьё для будущего П2б-тюнинга БЕЗ новых LLM-затрат.

    П16-границы (все в протоколе, честно):
      • LLM НЕ вызывается (картограф/суд — только live-контур): активируются ВСЕ авторские
        цепочки без новостного гейта — replay отвечает «что показал бы ГРАФ», не «что показал
        бы весь контур» (новости прошлого дня в БД есть, но их кластеризация под cutoff —
        отдельный долг);
      • шок корня и realized/vol терминалов — строго по барам date<=cutoff (asof-гейты);
      • чувствительности — по полной истории (калибровочные параметры, граница v1);
      • НИЧЕГО не запечатывается (П16: прогноз задним числом — не прогноз) и не пушится ботом;
        протокол — в journal/replay_logs/ (ОТДЕЛЬНО от боевых funnel_logs: /session и дашборд
        replay не видят).
    """
    import re as _re
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", str(cutoff or "")):
        return {"ОТКАЗ": f"cutoff должен быть YYYY-MM-DD, получено {cutoff!r}"}
    now = _now()
    run_id = f"replay_{cutoff}_{now.strftime('%Y%m%dT%H%M%SZ')}"
    chains_doc = CB.load_chains()      # авторские цепочки, резолв в конкретные компании (§3c/D1)
    con = sqlite3.connect(str(DB), timeout=30)
    try:
        каскады, graph_nodes = [], []
        for ch in (chains_doc or []):
            anchor_nodes = sorted((ch.get("nodes") or []), key=lambda n: n.get("order", 0))
            anchor_sym = (anchor_nodes[0].get("instruments") or [None])[0] if anchor_nodes else None
            if not anchor_sym:
                continue
            shock = _window_return(con, anchor_sym, asof=cutoff)
            if shock is None:
                каскады.append({"chain_id": ch.get("id"), "якорь": anchor_sym,
                                "пропуск": f"нет баров ≤ {cutoff} для шока корня (П8)"})
                continue
            built = CB.build_from_db(ch, shock, horizon_days=horizon_days, con=con, asof=cutoff)
            for n in built.get("узлы", []):
                n["_chain"] = ch.get("id")
            graph_nodes += built.get("узлы", [])
            каскады.append({"chain_id": ch.get("id"), "якорь": anchor_sym,
                            "shock": round(shock, 5), "узлов": len(built.get("узлы", []))})
        отбор = GB.select_from_nodes(graph_nodes, con=con, horizon_days=horizon_days, top_k=top_k)
        треки = GB.route_tracks(отбор)
    finally:
        con.close()
    protocol = {
        "run_id": run_id, "ts": now.isoformat(timespec="seconds"), "REPLAY": True,
        "cutoff": cutoff,
        "границы_честности": [
            "LLM не вызывался: все авторские цепочки активированы без новостного гейта",
            "шок корня и realized/vol терминалов — по барам date<=cutoff (П16 asof-гейты)",
            "чувствительности — по полной истории (калибровочные параметры; asof-срез — долг)",
            "ничего не запечатано и не доставлено (задним числом прогнозов не бывает)"],
        "каскады": каскады,
        "граф_отбор": {"узлов": len(graph_nodes),
                       "топ_k": [{"актив": s.get("symbol"), "score": s.get("score"),
                                  "edge": (s.get("node") or {}).get("amplitude"),
                                  "цепочка": (s.get("node") or {}).get("_chain"),
                                  "провизорный": bool((s.get("node") or {}).get("research"))}
                                 for s in отбор.get("топ_k", [])],
                       "треки": {"money": len(треки["money"]),
                                 "провизорный": len(треки["provisional"]),
                                 "дайджест": len(треки["digest_only"])}},
        "spec_ref": "replay-долг F3/П2а; §R2 граф; П16 asof-гейты",
    }
    if write:
        REPLAY_LOGS.mkdir(parents=True, exist_ok=True)
        PROG.atomic_write_text(REPLAY_LOGS / f"{run_id}.json",
                               json.dumps(protocol, ensure_ascii=False, indent=2))
    return protocol


def run_event_first(mode="mock", k=3, horizon_days=5, write=True, run_id=None, skip_contour=False,
                    seal_predictions=False, vet_money_k=0, deep_money_report=False):
    """Полный event-first прогон. Возвращает сводный протокол.

    skip_contour=True — research-only: без дорогого 21-агентного качественного контура по источникам
    (только скан + LLM-картограф + каскады в компании). Дёшево, быстро, для ежедневного потока идей."""
    run_id = run_id or "ef_" + _now().strftime("%Y%m%dT%H%M%SZ")
    universe = yaml.safe_load(open(ROOT / "config" / "universe.yaml", encoding="utf-8")) or {}
    chains_doc = CB.load_chains()   # §3c/D1: резолв в КОНКРЕТНЫЕ компании авторских цепочек
    now = _now()
    # F3#25 (§5.3): asof-гейт шока — окно не берёт бары ПОЗЖЕ даты прогона (отсекатель случайных
    # будущих/интрадей-баров в live). НЕ бэктест: cutoff-параметра нет, поштучной даты события в
    # потоке тоже нет → якорь = дата прогона (точка решения). Реальный replay — долг (см. _window_return).
    asof_date = now.strftime("%Y-%m-%d")
    # F0#9: ПРЕД-проверка бюджета (§24/Инв#5) — для live ДО единого LLM-вызова. Отказ → прогон не
    # начинается (как в funnel/masked). Раньше боевой event_first (--vet --deep) шёл вообще без гарда.
    guard = None
    if mode != "mock":
        budget_decision = RB.precheck("event_first")
        if not budget_decision["allowed"]:
            return {"run_id": run_id, "ts": now.isoformat(timespec="seconds"), "mode": mode,
                    "режим": "event-first контур",
                    "ОТКАЗ_бюджет": budget_decision,
                    "spec_ref": "§24 пред-проверка per_run_token_budget; Инв#5 CLAUDE.md",
                    "следующий_шаг": ("прогон НЕ выполнен: оценка превышает потолок event_first (§24) "
                                      "или месячный бюджет (§30 п.2); поднять может только пользователь "
                                      "правкой config/limits.yaml (П12).")}
        guard = RB.RunBudgetGuard("event_first", budget_decision.get("cap_usd") or 5.0)
    # Прогресс (§15): открываем прогон на K событий; уточним число после скана.
    PROG.begin(run_id, mode, "event-first: поиск идей", outer_total=k)
    PROG.note("скан событий (новости + цены + тренды)…")
    con = sqlite3.connect(str(DB), timeout=30)
    try:
        scan = ES.scan_events_live(q_max=0.1, con=con)
        # FГ B2: скан ищет до max(k, 8) источников — ширина НИКОГДА не меньше запрошенного k
        # (иначе при k>8 контур голодал бы), но может быть шире для видимости всего фронта событий.
        sources = _shock_sources(scan, universe, con, max(k, MAX_SHOCK_SOURCES))
        # ШИРОКИЙ ВХОД (РЕШЕНИЕ «A» + B2.5): новостные кластеры вне реестра тем → картограф ОДИН раз.
        # Карты идут И в воронку отбора (узлы графа, ниже), И в стейджинг/поток research-идей (дедуп).
        pcs = _cartographer_pass(scan, universe, mode, run_id, guard=guard)
        proposals = _stage_cartographer(pcs, now)
        картограф_идеи = _proposal_ideas(proposals)
        # FГ B2: дорогой контур — только по первым k источникам (бюджет). Остальные найденные
        # источники идут в протокол ТОЛЬКО ДЛЯ ВИДИМОСТИ фронта событий — они НЕ ставятся в очередь
        # на будущие прогоны (каждый прогон сканирует заново). Поток идей рождается не из этого
        # контура, а из каскадов+картографа ниже, поэтому ширина скана на выдачу не влияет.
        contour_sources = sources[:k] if not skip_contour else []
        источники_вне_контура = sources[k:] if not skip_contour else sources
        PROG.set_outer_total((len(contour_sources) or 1) if not skip_contour else 1)
        per_source, all_ideas = [], []
        # research-only (дешёвый ежедневный режим): пропускаем дорогой 21-агентный качественный
        # контур по источникам; оставляем картограф + каскады в компании (это и есть поток идей).
        for _i, src in enumerate(contour_sources):
            PROG.outer(_i, src)
            # 1) КАЧЕСТВЕННО: полный состязательный контур, заякоренный на источник события
            p = F.run_funnel(theme=src, mode=mode, theme_focused=True, write=write,
                             run_id=f"{run_id}__{src}", cost_guard=guard)
            # кросс-ревью ночи: внутренняя воронка ловит RunBudgetExceeded сама (graceful) —
            # если при этом пробит ВНЕШНИЙ потолок event_first, честно останавливаем весь прогон
            if guard is not None and "ОСТАНОВ_бюджет" in p and guard.spent_usd >= guard.cap_usd:
                raise RB.RunBudgetExceeded(guard.mode, guard.spent_usd, guard.cap_usd)
            ideas = (p.get("этап6_синтез") or {}).get("отчёты", [])
            for idea in ideas:
                all_ideas.append({**idea, "_событие": src})
            fr = p.get("воронка_отсева") or {}
            per_source.append({
                "источник": src, "shock": (round(_window_return(con, src, asof=asof_date) or 0, 5) or None),
                "контур": {"run_id": p.get("run_id"), "кандидатов": p.get("candidates_count"),
                           "выдано": fr.get("этап6_выдано_топ", 0),
                           "итог": fr.get("вывод") or "—"},
            })

        # КАСКАД В КОМПАНИИ (§3c/D1 + Этап B3 §R2/§R3): авторские цепочки активируются по СОВПАДЕНИЮ
        # ТЕМЫ в новостях ИЛИ ценовому сигналу на узле. Узлы ВСЕХ активированных цепочек сливаются в
        # ОДИН граф и проходят воронку отбора (ворота → пред-ранг по edge-возможности → топ-K), затем
        # маршрутизируются по трекам (money / провизорный / дайджест). Запечатывание — отдельно (B3c).
        # Этап2: узлы-кандидаты для активации = ценовые кандидаты + трендовые (замапленные на proxy_etf),
        # чтобы трендо-событийный день активировал цепочку и через трендовый канал (роутинг до суда).
        node_syms = _candidate_node_syms(scan, universe)
        # активации цепочек: (а) АВТОРСКИЕ по теме/сигналу-кандидату + (б) КАРТОГРАФ из новостей (B2.5)
        chain_acts = list(activated_chains(scan, universe, chains_doc, node_syms))
        for _a in chain_acts:
            _a["источник_карты"] = "авторская"
        chain_acts += [{"chain": pc["chain"], "anchor": pc["anchor"], "источник_карты": "картограф",
                        "причины": [f"картограф: {pc['событие']}"]} for pc in pcs]   # те же карты (дедуп)

        # B2.6: ДИНАМИЧЕСКИЙ ДОБОР ИСТОРИИ — тикеры цепочек без локальной истории цен тянем из EODHD
        # на лету (снимает «универсум как стенку»: любой ликвидный тикер от картографа становится
        # анализируемым). live-only; mock работает на готовой базе. Кэш растёт сам.
        добор = {"fetched": [], "had": [], "refreshed": [], "failed": []}
        if mode != "mock":
            import os as _os
            from data import eodhd as _E
            _syms = [i for act in chain_acts for n in (act["chain"].get("nodes") or [])
                     for i in (n.get("instruments") or [])]
            добор = _E.ensure_history(con, _syms, _os.environ.get("EODHD_API_KEY", ""))

        каскады, graph_nodes = [], []
        for act in chain_acts:
            ch, anchor = act["chain"], act["anchor"]
            # §R2.1: шок корня — за ОКНО реакции (EVENT_WINDOW_DAYS), выровнен с realized терминала
            # (realized_fn=window_return). Раньше тут был _last_return (1 день) → амплитуда занижена
            # в ~√window и систематически перекошена в сторону realized-компоненты edge.
            # F3#25: asof-гейт (см. границы в _window_return) — на шок КОРНЯ; realized/vol терминала
            # внутри build_from_db без гейта, соосность §R2.1 держится пока asof=now (live).
            shock = _window_return(con, anchor, asof=asof_date) if anchor else None
            if shock is None:
                каскады.append({"chain_id": ch.get("id"), "активация": act["причины"],
                                "событие_новость": act.get("событие_новость"),
                                "источник_карты": act.get("источник_карты"),
                                "пропуск": f"нет шока якоря {anchor} (П8)"})
                continue
            nodes = CB.build_from_db(ch, shock, horizon_days=horizon_days, con=con, db=DB,
                                     asof=asof_date)["узлы"]   # кросс-ревью ночи: терминалы под тем же
            # asof-гейтом, что и шок корня (случайный «будущий» бар в БД не течёт в edge/seal — П16)
            каскады.append({"chain_id": ch.get("id"), "якорь": anchor, "shock": round(shock, 5),
                            "активация": act["причины"], "событие_новость": act.get("событие_новость"),
                            "источник_карты": act.get("источник_карты"),
                            "узлов_построено": len(nodes)})
            for n in nodes:
                graph_nodes.append({**n, "root": anchor, "_chain": ch.get("id")})

        # ВОРОНКА ОТБОРА (§R2/§R3): объединённый граф → ворота → ранг по возможности → треки seal.
        отбор = GB.select_from_nodes(graph_nodes, con=con, horizon_days=horizon_days, top_k=8)
        треки = GB.route_tracks(отбор)

        # П2а (§R4.2, подписано 04.07): поле «внимание» (датчик gate-P1) — ИНФОРМАЦИОННО, ПОСЛЕ
        # отбора/маршрутизации (на ранжирование не влияет; пере-ранжирование = П2б, отдельная
        # подпись). Картограф-идеи получают кандидатов ключа из слов своего кластера (назначение
        # журналируется, пересдача запрещена); узлы треков — по сидам/реестру. Судья увидит поле
        # в проверяемом деле каскада (вход тайминга §8). Покрытие — метрика §R5 в протоколе.
        # Stage-review П2а HIGH-2: поле ИНФОРМАЦИОННОЕ — его сбой (реестр/БД/датчик) не имеет
        # права ронять боевой прогон после уже сделанного отбора. Fail-soft с честной пометкой.
        try:
            внимание_покрытие = AF.annotate_ideas(con, картограф_идеи, треки,
                                                  asof=now.isoformat(timespec="seconds"), run_id=run_id,
                                                  fix_keys=(mode != "mock"))   # mock реестр не трогает (П16)
        except Exception as _e:  # noqa: BLE001
            внимание_покрытие = {"ошибка": f"{type(_e).__name__}: {_e}",
                                 "пояснение": "поле «внимание» не посчитано — прогон продолжен (fail-soft)"}

        # B3c: ЗАПЕЧАТЫВАНИЕ ПО ТРЕКАМ (П16 — только при seal_predictions, иначе журнал не трогаем).
        # money (ярус A) → kind=cascade_money → денежный Brier/§11; провизорный (ярус B/C) →
        # kind=cascade_provisional → СВОЙ Brier, к §11 не приближается (resolve сегментирует герметично).
        # ПЕРЕНАПРАВЛЕНИЕ КОНТУРА: дорогой состязательный суд — на топ-K money-каскадов (не на слепые
        # шок-источники). Сломанные слепым судом money-идеи демотируются в провизорный (гейт §11).
        суд_money = {}
        if vet_money_k and mode != "mock":
            # событие-исток по цепочке → судья видит ПОВОД, а не голый edge (см. _money_thesis)
            chain_events = {c.get("chain_id"): _событие_из_цепочки(c)
                            for c in каскады if c.get("chain_id")}
            суд_money = _vet_money(треки["money"], run_id, vet_money_k, chain_events=chain_events,
                                   deep_report=deep_money_report, guard=guard)

        # Ревью 2026-07-04: seal_prediction идемпотентен (дедуп той же ставки) — перезапуск прогона
        # в тот же день не плодит дубли, искусственно приближающие ворота-270. Дубли считаем честно.
        # Ревью 2026-07-04 M14: «демотировано_судом» — ТОЛЬКО те, кого суд реально разбил.
        # Money-узлы за пределами top-K (или при --seal без --vet) до суда не доходили, но
        # считались «разбитыми» — метрика протокола/дайджеста лгала (П8) и маскировала скрытый
        # потолок money-пропускной способности = vet_money_k/день.
        запечатано = {"money": 0, "провизорный": 0, "демотировано_судом": 0,
                      "вне_суда_topK": 0, "дубль_пропущен": 0}
        if seal_predictions and mode != "mock":
            for s in треки["money"]:
                вердикт = суд_money.get(s["symbol"])
                kind = _money_kind(вердикт)
                if kind == "cascade_provisional":
                    запечатано["демотировано_судом" if вердикт is not None else "вне_суда_topK"] += 1
                spec = CR.seal_spec(s["node"], kind=kind, run_id=run_id,
                                    horizon_days=horizon_days, con=con, now_dt=now)
                if spec:
                    if FC.seal_prediction(spec):
                        запечатано["money" if kind == "cascade_money" else "провизорный"] += 1
                    else:
                        запечатано["дубль_пропущен"] += 1
            for s in треки["provisional"]:
                spec = CR.seal_spec(s["node"], kind="cascade_provisional", run_id=run_id,
                                    horizon_days=horizon_days, con=con, now_dt=now)
                if spec:
                    if FC.seal_prediction(spec):
                        запечатано["провизорный"] += 1
                    else:
                        запечатано["дубль_пропущен"] += 1
    except RB.RunBudgetExceeded as e:
        # Долг[HIGH]: хард-стоп бюджета на лету (§24) — прогон ОСТАНОВЛЕН, graceful-протокол (не крэш)
        PROG.finish(f"остановлен по бюджету (§24): потрачено ${e.spent_usd:.2f} ≥ ${e.cap_usd}")
        return {"run_id": run_id, "ts": now.isoformat(timespec="seconds"), "mode": mode,
                "режим": "event-first контур",
                "ОСТАНОВ_бюджет": {"mode": e.mode, "spent_usd": round(e.spent_usd, 4),
                                   "cap_usd": e.cap_usd, "reason": str(e)},
                "spec_ref": "§24 стоп-на-лету RunBudgetGuard; Инв#5 CLAUDE.md",
                "следующий_шаг": ("прогон ОСТАНОВЛЕН на лету: реальная стоимость превысила потолок "
                                  "режима event_first; поднять может только пользователь (config/limits.yaml, П12).")}
    finally:
        con.close()

    top3 = ME._diversify(all_ideas)   # ЛЕГАСИ: выдача 21-агентного контура (ПУСТА под skip_contour/--vet)
    money_n, prov_n, dig_n = len(треки["money"]), len(треки["provisional"]), len(треки["digest_only"])
    # БАГ#13: HEADLINE-метрика = РЕАЛЬНЫЙ поток research-идей, а НЕ len(top3). Поток течёт через
    # картограф (события вне реестра тем) + узлы графа, прошедшие воронку отбора в топ-K
    # (money/провизорный/дайджест). Под --vet (skip_contour) 21-агентный контур НЕ жжётся → top3≡0,
    # но поток НЕнулевой при ненулевых картограф/граф. Состязательный контур считаем ОТДЕЛЬНО.
    граф_топ_n = len(отбор["топ_k"])
    # БАГ#13-долг (честность П8): поток = УНИКАЛЬНЫЕ активы, не сумма. Картограф-цепочки питают И
    # картограф_идеи, И граф→топ_k — один тикер считался дважды, headline «0→N» завышал ровно ту
    # метрику, которой этап доказывает успех. Считаем по union активов, как и фактическая выдача _ef_ideas.
    _граф_активы = {s.get("symbol") for s in отбор["топ_k"] if s.get("symbol")}
    _карто_активы = {i.get("актив") for i in картограф_идеи if i.get("актив")}
    поток_активы = _граф_активы | _карто_активы
    поток_n = len(поток_активы)
    пересечение_n = len(_граф_активы & _карто_активы)
    _поток_разбивка = (f"картограф {len(картограф_идеи)} + граф-топ {граф_топ_n}"
                       + (f", −{пересечение_n} пересеч." if пересечение_n else ""))

    # Карта цепочка_id → её активация/якорь (для «почему» в дайджесте). каскады уже посчитаны выше:
    # активация — это породившее событие («картограф: <событие>» либо тема/ценовой сигнал авторской цепочки).
    _chain_meta = {c.get("chain_id"): c for c in каскады if c.get("chain_id")}

    def _node_brief(s):
        n = s["node"]
        ch = _chain_meta.get(n.get("_chain")) or {}
        edge = n.get("amplitude")
        # направление = знак ожидаемого движения узла (П8: из посчитанной амплитуды, не выдумка)
        напр = None if edge is None else ("лонг" if edge > 0 else "шорт" if edge < 0 else "флэт")
        return {"актив": s["symbol"], "score": s["score"], "edge": edge, "направление": напр,
                "ярусы": n.get("tiers"), "лаг_дней": n.get("lag_days"),
                "горизонт_дней": n.get("horizon_days"), "вероятность": n.get("probability"),
                "надёжность_r2": n.get("reliability"), "изоляция_r2": n.get("r2"),
                "надёжность_метка": s["prerank"].get("reliability"), "цепочка": n.get("_chain"),
                "порядок": n.get("order"), "чокпоинт": n.get("chokepoint"),
                "провизорный": bool(n.get("research")),
                "внимание": s.get("внимание"),          # П2а: инфо-поле (ранжирование не трогает)
                # «почему»: событие, активировавшее цепочку, и её якорь (источник корневого шока)
                "событие": _событие_из_цепочки(ch),
                "якорь": ch.get("якорь"), "источник_карты": ch.get("источник_карты")}
    protocol = {
        "run_id": run_id, "ts": now.isoformat(timespec="seconds"), "mode": mode,
        "режим": "event-first контур (§6 скан + §4 контур + §5 каскад + §9 резолв)",
        "spec_ref": "PLAN_cascade_first Этап 6; §6/§4/§5/П5/§9/П10/П16",
        "скан": {"источники": scan["источники"], "сырых_сигналов": scan["сырых_сигналов"],
                 "статистических_после_FDR": scan["статистических_после_FDR"],
                 "топ_события": [e["метка"] for e in scan["кандидат_события"][:10]]},
        "шок_источники": sources,
        "источники_разбивка": {"найдено": len(sources), "в_контуре_k": len(contour_sources),
                               "вне_контура_для_видимости": источники_вне_контура},  # FГ B2: НЕ очередь, только обзор
        "новые_события_на_регистрацию": proposals,
        "картограф_идеи": картограф_идеи,        # анти-brent: research-идеи по событиям вне реестра тем
        "по_источникам": per_source,
        "каскады_в_компании": каскады,                     # лог активации цепочек (счётчики построенных узлов)
        "внимание_покрытие": внимание_покрытие,             # П2а §R5: доля идей с данными датчика
        # M7 (ревью 04.07): вызовы без usage.cost видимы — «без_стоимости» > 0 значит, что
        # фактический спенд ВЫШЕ учтённого (лимиты Инв#5 занижены), это сигнал, не мелочь
        "бюджет_прогона": ({"потрачено_usd": round(guard.spent_usd, 4), "вызовов": guard.calls,
                            "без_стоимости": guard.unaccounted_calls} if guard is not None else None),
        "граф_отбор": {                                     # §R2/§R3: воронка отбора объединённого графа + треки seal
            "узлов": отбор["всего"], "ворота_прошли": отбор["ворота_прошли"],
            "отсев_по_критериям": отбор["отсев_по_критериям"],
            "добор_истории": {"скачано": добор["fetched"], "было": len(добор["had"]),
                              "досинк": добор.get("refreshed", []),  # P0#2: ДОсинк до сегодня (свежий шок)
                              "не_удалось": добор["failed"]},   # B2.6: новые тикеры с EODHD на лету
            "треки": {"money": money_n, "провизорный": prov_n, "дайджест": dig_n},
            "запечатано": запечатано,           # B3c: фактически в журнал (только seal_predictions)
            "суд_money": суд_money,              # слепой суд по топ-K money-каскадов (перенаправл. контур)
            "топ_k": [_node_brief(s) for s in отбор["топ_k"]],
            "money_трек": [_node_brief(s) for s in треки["money"]],
            "провизорный_трек": [_node_brief(s) for s in треки["provisional"]],
        },
        # БАГ#13: headline-метрика РЕАЛЬНОГО потока (картограф + граф-топ), РАЗВЕДена с легаси-контуром.
        "поток_идей": {
            "всего": поток_n, "картограф": len(картограф_идеи), "граф_топ": граф_топ_n,
            "пересечение": пересечение_n,   # дубли актив-в-обоих (картограф ∩ граф-топ); всего = union уникальных
            "money": money_n, "провизорный": prov_n, "дайджест": dig_n,
            "состязательный_контур": len(top3), "контур_включён": not skip_contour,
        },
        # ЛЕГАСИ-вывод 21-агентного контура по шок-источникам (ПУСТ под --vet/skip_contour).
        # ВНИМАНИЕ: это НЕ headline «выдал N идей» — реальный поток см. в "поток_идей" выше.
        "контур_выдал_топ3": [{"актив": i.get("актив"), "направление": i.get("направление"),
                               "балл": i.get("балл"), "событие": i.get("_событие")} for i in top3],
        "итог": (f"идей в поток {поток_n} уникальных ({_поток_разбивка}); "
                 f"источников {len(sources)}; "
                 f"граф: {отбор['всего']} узлов → ворота {отбор['ворота_прошли']} → "
                 f"money {money_n}, провизорный {prov_n}, дайджест {dig_n}; "
                 f"состязательный контур {len(top3)} идей"
                 f"{' (выключен под --vet)' if skip_contour else ''}; "
                 f"новых событий на регистрацию {len(proposals)}"),
    }
    # Этап2: честный роутинг трендов (сколько трендовых кандидатов реально замаплено до суда) +
    # алерт дневного расхода (2.4). Обе метрики — в протокол; алерт при превышении уходит владельцу.
    _tr_cands = [s for s in scan.get("сигналы", []) if s.get("вид") == "trend" and s.get("кандидат")]
    _tr_mapped = _trend_proxy_syms(scan, universe)
    protocol["трендовый_роутинг"] = {"кандидатов": len(_tr_cands),
                                     "замаплено_до_суда": len(_tr_mapped),
                                     "инструменты": sorted({p for _k, p in _tr_mapped})}
    protocol["дневной_расход"] = _daily_debate_alert(mode, now)
    if write:
        LOGS.mkdir(parents=True, exist_ok=True)
        PROG.atomic_write_text(LOGS / f"{run_id}.json",
                               json.dumps(protocol, ensure_ascii=False, indent=2))   # M13: без битых JSON
    PROG.finish(f"Идей в поток {поток_n} уникальных ({_поток_разбивка}) · "
                f"источников {len(sources)} · граф {отбор['всего']} узлов "
                f"(ворота {отбор['ворота_прошли']}: money {money_n}/провиз {prov_n}/дайдж {dig_n}) · "
                f"состязательный контур {len(top3)} идей · новых событий {len(proposals)}.")
    return protocol


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="mock", choices=["mock", "live", "auto"])
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--research-only", action="store_true",
                    help="без дорогого качественного контура: только картограф + каскады (поток идей)")
    a = ap.parse_args()
    p = run_event_first(mode=a.mode, k=a.k, skip_contour=a.research_only)
    print(f"[{p['run_id']}] {p['режим']} · mode={p['mode']}")
    print(f"  скан: {p['скан']['сырых_сигналов']} сигналов, события: {', '.join(p['скан']['топ_события'][:5])}")
    print(f"  {p['итог']}")
