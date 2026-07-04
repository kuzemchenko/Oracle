# -*- coding: utf-8 -*-
"""ops/bot_reports.py — рендер для бота-пульта: пуш отчётов (13 полей §8), inline-клавиатура
решений, утренняя строка бюджета, алерты бюджета и срабатываний триггеров.

Чистый слой форматирования + скан артефактов (journal/funnel_logs/*.json). Сетевого I/O нет —
его делает bot.py. Так логика тестируется без Telegram.
"""
import re
import json
import html
import pathlib

import bot_state as S

ROOT = pathlib.Path(__file__).resolve().parents[1]
FUNNEL_LOGS = ROOT / "journal" / "funnel_logs"

# Поля §8 в каноническом порядке (ключи — как их кладёт синтезатор; рендер терпим к отсутствию).
FIELDS_8 = [
    "1. Актив/направление/инструмент", "2. Каскадная цепочка", "3. Вероятность + калибровка",
    "4. Сценарии заработать/потерять; асимметрия", "5. Отыгранность и стадия входа",
    "6. Кто продаёт нам и почему неправ", "7. Манип-балл + поведенческий диагноз",
    "8. Балансировка риска", "9. Скоринг; критик и судья", "10. Источники с credibility",
    "11. Что неизвестно (П8)", "12. Сценарии инвалидации", "13. Рамка-дисклеймер",
]

MAX_FIELD = 320          # обрезка одного поля в пуше
MAX_MSG = 3800           # запас под лимит Telegram 4096
CARTO_SHOW = 5           # #14: сколько историй картографа показываем в дайджесте; хвост → /progress
# §8 поле13 / §16 — рамка-дисклеймер. ИНВАРИАНТ: метка снимается ТОЛЬКО после калибр-гейта §11
# (fix #11 «метку НЕ снимаем»). Источник истины — здесь, не LLM (под --deep judgment мог бы её убрать).
RESEARCH_FRAME = "исследовательский инструмент, не инвестиционная рекомендация"


def _trunc(s, n):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _fields_dict(idea):
    """Достаёт словарь 13 полей §8 из карточки отчёта.

    Синтезатор оборачивает отчёт в запись вызова агента: отчёт = {agent, …, judgment: {…, поля}}.
    Терпимо ищем 'поля' на нескольких уровнях; иначе отдаём сам отчёт (рендер покажет, что есть)."""
    rep = idea.get("отчёт") or {}
    if not isinstance(rep, dict):
        return {}
    for cand in (rep.get("поля"),
                 (rep.get("judgment") or {}).get("поля") if isinstance(rep.get("judgment"), dict) else None):
        if isinstance(cand, dict) and cand:
            return cand
    return rep


# ── скан новых отчётов прогона ──────────────────────────────────────────────────────
def load_protocol(run_id, logs_dir=None):
    p = pathlib.Path(logs_dir or FUNNEL_LOGS) / f"{run_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


_TS_RE = re.compile(r"\d{8}T\d{6}Z")


def _ts_key(protocol):
    """Хронологический ключ протокола: timestamp из ts/run_id. БЕЗ timestamp (статичные тест-
    фикстуры вроде week7_testday) → пустой ключ, такие идут НИЖЕ боевых прогонов. Иначе «последним
    прогоном» в чате/пуше оказывается mock-фикстура, отсортированная по имени файла (баг: Дирижёр
    заземлялся на week7_testday вместо реальной выданной идеи)."""
    ts = (protocol or {}).get("ts") or ""
    if ts:
        return ts
    m = _TS_RE.search(str((protocol or {}).get("run_id") or ""))
    return m.group(0) if m else ""


def scan_protocols(logs_dir=None):
    """Все протоколы в журнале по ХРОНОЛОГИИ (timestamp из ts/run_id), не по имени файла."""
    d = pathlib.Path(logs_dir or FUNNEL_LOGS)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    out.sort(key=lambda pr: (_ts_key(pr), str(pr.get("run_id") or "")))
    return out


def ideas_from_protocol(protocol):
    """ЕДИНЫЙ КОНТРАКТ карточек-идей из любого протокола. Пусто — слабый день §6.

    Возвращает список карточек ЕДИНОЙ ФОРМЫ (пригодной для доставки и заземления чата). Поля как у
    отчётов этапа 6 в той мере, что нужна боту:
        {
          "актив":        тикер мишени,
          "направление":  'лонг'/'шорт'/None,
          "балл":         сводная оценка (score графа / тектонический потенциал),
          "позиция":      {"вероятность": float|None},
          "событие":      новость-повод (П8: РОВНО активировавшее цепочку), либо None,
          "трек":         'money'/'провизорный'/'граф'/'картограф' (для маршрутизации),
          "провизорный":  bool — гипотеза (ценами не подтверждена),
          "отчёт":        {"поля": {§8-поля}} — минимум 1/2/13, чтобы idea_brief/format_report работали,
          "_node":        сырой узел/идея-источник (для §8-промоушена агентом C, #11),
        }

    Два источника по типу протокола:
      • ЛЕГАСИ 21-агентный синтез (тематический funnel): этап6_синтез.отчёты — уже канонические карточки,
        отдаём как есть.
      • EVENT-FIRST (run_id 'ef_*'): этапа 6 нет — идеи живут в воронке отбора графа
        (граф_отбор.топ_k / .money_трек) и в идеях картографа (картограф_идеи). Собираем из них.

    money-идеи, ПЕРЕЖИВШИЕ слепой суд (под §11-промоушен агентом C), — отдельной точкой
    money_ideas_from_protocol() (fail-closed: только исход «УСТОЯЛА»)."""
    protocol = protocol or {}
    synth = protocol.get("этап6_синтез") or {}
    legacy = synth.get("отчёты") or []
    if legacy:
        return legacy
    if str(protocol.get("run_id") or "").startswith("ef_"):
        return _ef_ideas(protocol)
    return legacy


def _money_survived(verdict):
    """Зеркалит orchestrator.event_first._money_kind: money-идея пережила гейт §11 ТОЛЬКО при явном
    исходе слепого суда «УСТОЯЛА» И без процедурного вето §6 (fail-closed). РАЗБИТА/ВЕТО/ПРОПУСК/
    ОШИБКА_СУДА/не судили (None) → НЕ money. verdict — строка-исход или dict {исход,...}."""
    if isinstance(verdict, dict) and verdict.get("процедурное_вето"):
        return False
    outcome = verdict.get("исход") if isinstance(verdict, dict) else verdict
    return outcome == "УСТОЯЛА"


def _ef_card_from_node(node, kind=None):
    """Узел воронки отбора графа (форма _node_brief) → карточка единого контракта. Каскад (поле 2)
    строим ТОЛЬКО из посчитанного: событие-повод + шок-якорь + порядок (П8, ничего не выдумываем)."""
    a = node.get("актив")
    каскад = []
    ev = node.get("событие")
    if ev:
        каскад.append(ev)
    як = node.get("якорь")
    if як:
        order = node.get("порядок")
        каскад.append(f"шок по {як}" + (f" → {order}-й порядок" if isinstance(order, int) else ""))
    if a:
        каскад.append(a)
    поля = {
        "1. Актив/направление/инструмент": f"{a} {node.get('направление') or ''}".strip(),
        "2. Каскадная цепочка": [x for x in каскад if x],
        "13. Рамка-дисклеймер": RESEARCH_FRAME,
    }
    return {
        "актив": a, "направление": node.get("направление"), "балл": node.get("score"),
        "позиция": {"вероятность": node.get("вероятность")},
        "событие": ev,
        "трек": kind or ("провизорный" if node.get("провизорный") else "граф"),
        "провизорный": bool(node.get("провизорный")),
        "отчёт": {"поля": поля},
        "_node": node,
    }


def _ef_card_from_carto(ci):
    """Идея картографа (форма _proposal_ideas) → карточка единого контракта. Каскад — событие +
    текстовые узлы цепочки по порядкам (П8). Картограф-идеи не запечатываются (research, §16)."""
    insts = ci.get("все_инструменты") or ([ci.get("актив")] if ci.get("актив") else [])
    a = insts[0] if insts else ci.get("актив")
    nodes = sorted((ci.get("узлы_каскада") or []), key=lambda x: x.get("порядок") or 0)
    каскад = [ci.get("событие")] if ci.get("событие") else []
    for nd in nodes:
        u = nd.get("узел")
        if u:
            каскад.append(u)
    if a and a not in каскад:
        каскад.append(a)
    поля = {
        "1. Актив/направление/инструмент": a,
        "2. Каскадная цепочка": [x for x in каскад if x],
        "13. Рамка-дисклеймер": RESEARCH_FRAME,
    }
    return {
        "актив": a, "направление": None, "балл": ci.get("тектонический_потенциал"),
        "позиция": {"вероятность": None},
        "событие": ci.get("событие"),
        "трек": "картограф", "провизорный": True,
        "отчёт": {"поля": поля},
        "_node": ci,
    }


def _ef_ideas(protocol):
    """Event-first идеи: топ воронки отбора графа (+ money-трек как фолбэк) ∪ идеи картографа,
    дедуп по активу (узлы графа приоритетнее — у них богаче расчёт)."""
    g = protocol.get("граф_отбор") or {}
    money_assets = {n.get("актив") for n in (g.get("money_трек") or [])}
    primary = g.get("топ_k") or g.get("money_трек") or []
    out, seen = [], set()
    for n in primary:
        a = n.get("актив")
        if not a or a in seen:
            continue
        seen.add(a)
        kind = "money" if a in money_assets else ("провизорный" if n.get("провизорный") else "граф")
        out.append(_ef_card_from_node(n, kind=kind))
    for ci in (protocol.get("картограф_идеи") or []):
        insts = ci.get("все_инструменты") or ([ci.get("актив")] if ci.get("актив") else [])
        a = insts[0] if insts else None
        if not a or a in seen:
            continue
        seen.add(a)
        out.append(_ef_card_from_carto(ci))
    return out


def money_ideas_from_protocol(protocol):
    """Карточки money-трека, ПЕРЕЖИВШИЕ слепой суд (исход «УСТОЯЛА», без процедурного вето §6) —
    ровно то, что гейт §11 разрешает двигать к ставке. Контракт для агента C (#11): промоушен
    money-идеи в §8-карточку. Обогащены вердиктом суда ('суд') и, если был deep-report, ПОЛНЫМ
    §8-синтезом (суд_money[акт]['отчёт_§8'] → отчёт.judgment, его и читает _fields_dict/format_report).

    fail-closed: суд не гонялся / РАЗБИТА / ВЕТО / ПРОПУСК → пусто (деньги ТОЛЬКО при «УСТОЯЛА»)."""
    g = (protocol or {}).get("граф_отбор") or {}
    суд = g.get("суд_money") or {}
    out = []
    for n in (g.get("money_трек") or []):
        v = суд.get(n.get("актив"))
        if not _money_survived(v):
            continue
        card = _ef_card_from_node(n, kind="money")
        card["суд"] = v
        deep = v.get("отчёт_§8") if isinstance(v, dict) else None
        if isinstance(deep, dict) and deep and not deep.get("_ошибка"):
            card["отчёт"] = {"judgment": deep}     # полный §8-синтез важнее грубых node-полей
        out.append(card)
    return out


def _money_after_court(граф):
    """Полка money ПОСЛЕ слепого суда: (устояло, демотировано). Устояло — узлы money-трека с
    исходом «УСТОЯЛА» (см. _money_survived); остальные money-узлы демотированы в гипотезу (§11
    fail-closed). Это и закрывает самопротиворечие дайджеста (#12): «можно довести до ставки»
    считаем ПОСЛЕ суда, а не по сырому треку до него."""
    money = граф.get("money_трек") or []
    суд = граф.get("суд_money") or {}
    survived = sum(1 for n in money if _money_survived(суд.get(n.get("актив"))))
    return survived, len(money) - survived


def idea_brief(idea_card):
    """Сжатая суть выданной идеи для ЗАЗЕМЛЕНИЯ свободного чата Дирижёра (он должен предметно
    обсуждать пушнутые карточки, а не один последний прогон). Только то, что в карточке (П8).

    Возвращает {актив, направление, вероятность, тезис, каскад, что_неизвестно} — поля §8 №1/2/11
    + вероятность судьи (поле 3). Сохраняется и в pending при пуше, чтобы суть пережила ротацию
    файлов протоколов."""
    f = _fields_dict(idea_card)
    pos = idea_card.get("позиция") if isinstance(idea_card.get("позиция"), dict) else {}
    return {
        "актив": idea_card.get("актив"),
        "направление": idea_card.get("направление"),
        "вероятность": pos.get("вероятность"),
        "тезис": _humanize(_field(f, 1), 200) or idea_card.get("актив"),
        "каскад": _cascade_line(_field(f, 2)),
        "что_неизвестно": _humanize(_field(f, 11), 220),
    }


def new_runs(pushed_run_ids, logs_dir=None):
    """Протоколы, которые бот ещё не пушил (по run_id)."""
    pushed = set(pushed_run_ids or [])
    return [p for p in scan_protocols(logs_dir or FUNNEL_LOGS) if p.get("run_id") not in pushed]


# ── рендер отчёта: колонка, а не дамп словаря ────────────────────────────────────────
# Перевод тикера/макро-драйвера в человеческое слово для заголовка (мягкий фолбек на тикер).
_DRIVER_RU = {
    "copper": "медь", "oil": "нефть", "brent": "нефть Brent", "wti": "нефть WTI",
    "gold": "золото", "silver": "серебро", "gas": "газ", "natgas": "газ",
    "equities": "рынок акций", "spy": "рынок акций", "rates": "ставки",
    "wheat": "пшеница", "grain": "зерно", "uranium": "уран", "lithium": "литий",
}


# Тикер → (короткое имя, что это простыми словами). Чтобы не слать «COPX.US» без объяснения.
# Открытый скан тянет тикеры на лету в цепочки каскадов — держим широкое покрытие повторяющихся
# бумаг (макро-ETF, секторные фонды, отраслевые лидеры), чтобы клиент видел СЛОВО, а не код.
# Незнакомец, которого тут нет → фолбэк на БД фундаментала, затем честная заглушка (_asset_human).
_ASSET = {
    # — ядро универсума (§30) —
    "BNO.US": ("нефть Brent", "биржевой фонд на цену нефти Brent"),
    "USO.US": ("нефть WTI", "биржевой фонд на цену американской нефти WTI"),
    "SPY.US": ("рынок акций США", "фонд на индекс S&P 500 — весь крупный рынок США"),
    "DBC.US": ("корзина сырья", "фонд на широкую корзину сырьевых товаров"),
    "CPER.US": ("медь", "биржевой фонд на цену меди"),
    "COPX.US": ("акции медедобытчиков", "фонд на акции компаний, добывающих медь"),
    "SPCX.US": ("SpaceX", "акции SpaceX (космос, Starlink, xAI)"),
    "RKLB.US": ("Rocket Lab", "акции Rocket Lab — пусковые услуги, конкурент SpaceX"),
    "ASTS.US": ("AST SpaceMobile", "акции AST SpaceMobile — спутниковая связь, конкурент Starlink"),
    "VRT.US": ("Vertiv", "акции Vertiv — питание и охлаждение для дата-центров"),
    "GEV.US": ("GE Vernova", "акции GE Vernova — оборудование для энергосетей и генерации"),
    "ETN.US": ("Eaton", "акции Eaton — электрооборудование"),
    "CLF.US": ("Cleveland-Cliffs", "акции Cleveland-Cliffs — сталь, в т.ч. для сердечников трансформаторов"),
    "NUE.US": ("Nucor", "акции Nucor — крупнейший сталевар США"),
    "FCX.US": ("Freeport", "акции Freeport-McMoRan — добыча меди"),
    "SCCO.US": ("Southern Copper", "акции Southern Copper — добыча меди"),

    # — товары через ETF —
    "GLD.US": ("золото", "биржевой фонд на цену золота"),
    "SLV.US": ("серебро", "биржевой фонд на цену серебра"),
    "UNG.US": ("природный газ США", "фонд на цену газа Henry Hub (с roll-дрейфом)"),
    "WEAT.US": ("пшеница", "биржевой фонд на цену пшеницы"),
    "CORN.US": ("кукуруза", "биржевой фонд на цену кукурузы"),
    "LIT.US": ("литий и АКБ", "фонд на акции добытчиков лития и производителей батарей"),

    # — золото-/серебродобытчики —
    "GDX.US": ("золотодобытчики", "фонд на акции крупных золотодобывающих компаний"),
    "GDXJ.US": ("юниоры-золотодобытчики", "фонд на акции небольших золотодобытчиков (волатильнее GDX)"),
    "SIL.US": ("серебродобытчики", "фонд на акции компаний, добывающих серебро"),
    "NEM.US": ("Newmont", "акции Newmont — крупнейший в мире золотодобытчик"),

    # — нефть и газ: добыча, сервис, переработка —
    "XLE.US": ("энергосектор США", "фонд на крупные нефтегазовые компании S&P 500"),
    "XOP.US": ("нефтегазодобыча", "фонд на акции компаний разведки и добычи нефти/газа (E&P)"),
    "OIH.US": ("нефтесервис", "фонд на акции нефтесервисных компаний"),
    "XOM.US": ("ExxonMobil", "акции ExxonMobil — крупнейшая нефтегазовая компания США"),
    "CVX.US": ("Chevron", "акции Chevron — вторая нефтяная мейджор США"),
    "TTE.US": ("TotalEnergies", "акции TotalEnergies — французская нефтегазовая мейджор"),
    "EOG.US": ("EOG Resources", "акции EOG — крупный сланцевый добытчик нефти"),
    "EQT.US": ("EQT", "акции EQT — крупнейший добытчик природного газа в США"),
    "SLB.US": ("SLB (Schlumberger)", "акции SLB — мировой лидер нефтесервиса"),
    "HAL.US": ("Halliburton", "акции Halliburton — нефтесервис, гидроразрыв"),
    "VLO.US": ("Valero", "акции Valero — крупный нефтепереработчик (НПЗ), маржа на топливе"),
    "MPC.US": ("Marathon Petroleum", "акции Marathon — нефтепереработка (НПЗ)"),
    "PSX.US": ("Phillips 66", "акции Phillips 66 — нефтепереработка и химия"),
    "LNG.US": ("Cheniere (СПГ)", "акции Cheniere Energy — крупнейший экспортёр СПГ из США (это компания, не газ)"),
    "VST.US": ("Vistra", "акции Vistra — электрогенерация, в т.ч. под спрос ИИ-дата-центров"),

    # — морские перевозки нефти/СПГ (танкеры): растут на ставках фрахта —
    "FRO.US": ("Frontline", "акции Frontline — крупный оператор нефтяных танкеров"),
    "STNG.US": ("Scorpio Tankers", "акции Scorpio — танкеры для нефтепродуктов"),
    "DHT.US": ("DHT Holdings", "акции DHT — супертанкеры сырой нефти (VLCC)"),
    "INSW.US": ("Int'l Seaways", "акции International Seaways — нефтеналивные танкеры"),
    "GLNG.US": ("Golar LNG", "акции Golar — перевозка и сжижение газа (СПГ)"),

    # — облигации, ставки, доллар —
    "TLT.US": ("длинные US Treasuries", "фонд гособлигаций США 20+ лет; растёт при падении ставок"),
    "TBT.US": ("шорт длинных Treasuries", "фонд 2x ПРОТИВ длинных US Treasuries; растёт при росте ставок"),
    "TIP.US": ("инфляц. Treasuries (TIPS)", "фонд гособлигаций США с защитой от инфляции"),
    "SCHP.US": ("инфляц. Treasuries (TIPS)", "фонд гособлигаций США с защитой от инфляции (Schwab)"),
    "HYG.US": ("мусорные облигации", "фонд высокодоходных корп.облигаций США (риск-аппетит/кредитный стресс)"),
    "EMB.US": ("госдолг развив. рынков", "фонд долларовых гособлигаций развивающихся стран"),
    "UUP.US": ("ставка на доллар", "фонд на рост индекса доллара США (DXY)"),
    "KRE.US": ("региональные банки США", "фонд на акции малых и средних банков США"),

    # — региональные рынки акций —
    "EEM.US": ("акции развив. рынков", "фонд на акции развивающихся рынков (Китай, Индия, Бразилия…)"),
    "FXI.US": ("крупные акции Китая", "фонд на крупнейшие китайские компании"),
    "INDA.US": ("акции Индии", "фонд на крупные индийские компании"),
    "EWZ.US": ("акции Бразилии", "фонд на крупные бразильские компании"),
    "EWW.US": ("акции Мексики", "фонд на крупные мексиканские компании"),
    "IWM.US": ("малые компании США", "фонд на индекс Russell 2000 — малая капитализация США"),

    # — секторные фонды S&P —
    "XLK.US": ("технологии (сектор)", "фонд на технологический сектор S&P 500"),
    "XLY.US": ("потреб. товары не-первой-необходимости", "фонд на сектор товаров вторичного спроса S&P 500"),
    "XLRE.US": ("недвижимость (сектор)", "фонд на сектор недвижимости S&P 500 (REIT)"),
    "XRT.US": ("ритейл", "фонд на акции розничной торговли США"),
    "JETS.US": ("авиакомпании", "фонд на акции мировых авиаперевозчиков"),

    # — оборона и аэрокосмос —
    "ITA.US": ("оборона и аэрокосмос", "фонд на акции оборонных и аэрокосмических компаний США"),
    "LMT.US": ("Lockheed Martin", "акции Lockheed Martin — оборонный подрядчик №1 (F-35, ракеты)"),
    "RTX.US": ("RTX (Raytheon)", "акции RTX — оборона и авиадвигатели (ПВО, Pratt & Whitney)"),
    "HON.US": ("Honeywell", "акции Honeywell — промышленный конгломерат (авиа, автоматизация)"),
    "BAH.US": ("Booz Allen", "акции Booz Allen — консалтинг для госсектора и обороны США"),

    # — крупные технологии / ИИ —
    "NVDA.US": ("Nvidia", "акции Nvidia — чипы-ускорители для ИИ, ядро бума ЦОД"),
    "MSFT.US": ("Microsoft", "акции Microsoft — облако Azure, OpenAI, ПО"),
    "AMZN.US": ("Amazon", "акции Amazon — облако AWS и e-commerce"),
    "AVGO.US": ("Broadcom", "акции Broadcom — чипы и сетевое оборудование для ИИ-ЦОД"),
    "SMCI.US": ("Super Micro", "акции Super Micro — серверы для ИИ-нагрузок"),
    "NBIS.US": ("Nebius", "акции Nebius — ИИ-облако и аренда GPU"),
    "NOW.US": ("ServiceNow", "акции ServiceNow — корпоративное ПО для автоматизации"),
    "WDAY.US": ("Workday", "акции Workday — облачное ПО для HR и финансов"),
    "ARKK.US": ("ARK Innovation", "спекулятивный фонд акций роста (Кэти Вуд) — барометр риск-аппетита"),
    "TSLA.US": ("Tesla", "акции Tesla — электромобили, АКБ, ИИ-автопилот"),
    "QS.US": ("QuantumScape", "акции QuantumScape — твердотельные аккумуляторы (доразработка)"),

    # — финтех / платежи / крипто —
    "V.US": ("Visa", "акции Visa — мировая платёжная сеть"),
    "PYPL.US": ("PayPal", "акции PayPal — онлайн-платежи"),
    "COIN.US": ("Coinbase", "акции Coinbase — крупнейшая крипто-биржа США"),
    "HOOD.US": ("Robinhood", "акции Robinhood — розничный брокер"),

    # — химия и материалы —
    "DOW.US": ("Dow", "акции Dow — базовая химия (пластики, упаковка)"),
    "LYB.US": ("LyondellBasell", "акции LyondellBasell — нефтехимия и полимеры"),
    "CE.US": ("Celanese", "акции Celanese — специальная химия"),
    "ALB.US": ("Albemarle", "акции Albemarle — крупнейший производитель лития"),
    "SQM.US": ("SQM", "акции SQM — литий и удобрения из Чили"),
    "EMR.US": ("Emerson", "акции Emerson — промышленная автоматизация"),
    "CARR.US": ("Carrier", "акции Carrier — отопление, вентиляция, кондиционеры (HVAC)"),

    # — логистика, авиа, потребитель, кадры —
    "DAL.US": ("Delta", "акции Delta Air Lines — авиаперевозчик"),
    "UAL.US": ("United Airlines", "акции United Airlines — авиаперевозчик"),
    "FDX.US": ("FedEx", "акции FedEx — экспресс-логистика (барометр торговли)"),
    "UPS.US": ("UPS", "акции UPS — логистика и доставка"),
    "WMT.US": ("Walmart", "акции Walmart — крупнейший ритейлер США"),
    "DG.US": ("Dollar General", "акции Dollar General — дискаунтер (барометр бедного потребителя)"),
    "YETI.US": ("YETI", "акции YETI — премиальные товары для активного отдыха"),
    "COLM.US": ("Columbia", "акции Columbia Sportswear — одежда для активного отдыха"),
    "VFC.US": ("VF Corp", "акции VF Corp — бренды одежды (Vans, The North Face)"),
    "RHI.US": ("Robert Half", "акции Robert Half — кадровое агентство (барометр найма)"),
    "MAN.US": ("ManpowerGroup", "акции ManpowerGroup — кадровое агентство (барометр найма)"),

    # — страх рынка (волатильность) —
    "VIXY.US": ("ставка на страх (VIX)", "фонд на рост волатильности рынка — растёт в панику"),
    "VXX.US": ("ставка на страх (VIX)", "фонд на рост волатильности рынка — растёт в панику"),
}


# Англ. сектор EODHD → русское слово (для фолбэка по БД фундаментала).
_SECTOR_RU = {
    "Technology": "технологии", "Industrials": "промышленность", "Healthcare": "здравоохранение",
    "Financial Services": "финансы", "Financial": "финансы", "Energy": "энергетика",
    "Basic Materials": "сырьё и материалы", "Consumer Cyclical": "потреб. товары (цикличные)",
    "Consumer Defensive": "потреб. товары (защитные)", "Communication Services": "связь и медиа",
    "Utilities": "коммунальные услуги", "Real Estate": "недвижимость",
}

_fund_cache = {}


def _fundamentals_human(ticker, db_path=None):
    """Фолбэк по тикеру из БД фундаментала (storage/oracle.db): (короткое имя, 'сектор · отрасль').
    Тянется на лету для бумаг, которых нет в курируемом словаре. None — если в БД пусто/нет имени.
    Кэш в памяти процесса; читаем БД read-only, никогда не падаем (П8 — нет данных → нет данных)."""
    if ticker in _fund_cache:
        return _fund_cache[ticker]
    res = None
    try:
        import sqlite3
        p = db_path or (ROOT / "storage" / "oracle.db")
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT name, sector, industry FROM fundamentals WHERE symbol=?",
                              (ticker,)).fetchone()
        finally:
            con.close()
        if row and row[0]:
            name, sector, industry = row
            short = _trunc(re.split(r"\s+(?:Inc|Corp|Ltd|PLC|LP|Holdings|Group|Co)\b", name)[0].strip(" .,"), 40) or name
            tail = " · ".join(x for x in (_SECTOR_RU.get(sector, sector), industry) if x)
            res = (short, f"акции {name}" + (f" — {tail}" if tail else ""))
    except Exception:
        res = None
    _fund_cache[ticker] = res
    return res


def _asset_human(ticker):
    """(имя, описание) для тикера: курируемый словарь → БД фундаментала → честная заглушка.
    Никогда не выдумываем суть: незнакомца помечаем явно и зовём за разбором (П8)."""
    if ticker in _ASSET:
        return _ASSET[ticker]
    fb = _fundamentals_human(ticker)
    if fb:
        return fb
    return (ticker, f"инструмент {ticker} — компанию пока не опознал; разбор по запросу «разбери {ticker}»")


def _humanize(v, max_n=MAX_FIELD):
    """Плоский читаемый текст из строки/списка/словаря (без JSON-скобок)."""
    if v is None:
        return ""
    if isinstance(v, dict):
        v = "; ".join(f"{k}: {_humanize(val, 120)}" for k, val in v.items())
    elif isinstance(v, list):
        v = "; ".join(_humanize(x, 160) for x in v if x not in (None, ""))
    return _trunc(v, max_n)


def _field(fields, n):
    """Значение поля §8 по его НОМЕРУ — ключи варьируются ('2_каскадная…' / '2. Каскадная…')."""
    for k, v in (fields or {}).items():
        m = re.match(r"\s*(\d+)", str(k))
        if m and int(m.group(1)) == n:
            return v
    return None


# Короткие заголовки 13 полей §8 (для шапки каждого пункта).
_FIELD_TITLES = {
    1: "Актив, направление, инструмент",
    2: "Каскадная цепочка — ГЛАВНОЕ ПОЛЕ",
    3: "Вероятность и калибровка",
    4: "Сценарии и асимметрия (после издержек)",
    5: "Отыгранность и стадия входа",
    6: "Кто продаёт нам — и почему он неправ",
    7: "Манипуляции и поведение толпы",
    8: "Как балансировать риск",
    9: "Скоринг + критик и судья",
    10: "Источники и их надёжность",
    11: "Что неизвестно",
    12: "Когда идея неверна (инвалидация)",
    13: "Рамка",
}

# ℹ️ «что это поле и зачем» — ОДИН словарь, правится здесь (формулировки) и переключателем short.
field_help = {
    1: "что покупаем/продаём и чем именно",
    2: "пошаговая цепочка причин: почему именно это сыграет",
    3: "шанс, что сыграет, и можно ли пока верить системе",
    4: "сколько заработать против сколько потерять, уже за вычетом комиссий и спреда",
    5: "сколько движения уже прошло — не поздно ли заходить",
    6: "кто по другую сторону сделки и в чём его ошибка; нет ответа — идея не выпускается",
    7: "не разгоняют ли актив искусственно; в какой фазе жадности/страха",
    8: "способы уменьшить риск, если он велик",
    9: "из чего сложился балл и что сказали «адвокат» и «прокурор» идеи",
    10: "откуда данные и насколько им можно верить",
    11: "честные пробелы — то, чего система НЕ знает",
    12: "при каком условии выходим и признаём ошибку",
    13: "напоминание: это инструмент, не приказ",
}

EMPTY_MARK = "[поле пустое]"   # П8: пусто → честно, ничего не выдумываем


def load_presentation(path=None):
    """Слой подачи: режим (long/short), still_unclear, слать ли mock. Читается при каждом рендере."""
    import yaml
    p = pathlib.Path(path or (ROOT / "config" / "presentation.yaml"))
    try:
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        cfg = {}
    return {"mode": cfg.get("mode", "long"),
            "still_unclear": set(cfg.get("still_unclear") or []),
            "send_mock_to_telegram": bool(cfg.get("send_mock_to_telegram", False))}


def save_presentation(mode=None, still_unclear=None, send_mock=None, path=None):
    """Записать настройки подачи (для команды /format). Меняет ТОЛЬКО слой отображения."""
    import yaml
    p = pathlib.Path(path or (ROOT / "config" / "presentation.yaml"))
    cur = load_presentation(p)
    obj = {"version": 1,
           "mode": mode if mode in ("long", "short") else cur["mode"],
           "still_unclear": sorted(still_unclear if still_unclear is not None else cur["still_unclear"]),
           "send_mock_to_telegram": cur["send_mock_to_telegram"] if send_mock is None else bool(send_mock)}
    p.write_text("# config/presentation.yaml — слой подачи (см. spec/PRESENTATION.md). "
                 "Меняется командой /format или руками.\n"
                 + yaml.safe_dump(obj, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return obj


def _cascade_line(v):
    """Каскадную цепочку (список звеньев) показываем как 'событие → … → актив'."""
    if isinstance(v, list) and v:
        return " → ".join(_humanize(x, 160) for x in v if x not in (None, ""))
    return _humanize(v)


def _field_content(n, fields, idea, pos):
    """Содержание поля §8 простыми словами (→). Пусто → честный маркер (П8)."""
    raw = _field(fields, n)
    if n == 1:
        name, what = _asset_human(idea.get("актив", "?"))
        direction = (idea.get("направление") or "").strip().lower()
        side = "ставка на рост" if direction.startswith("лонг") or direction in ("long", "buy") else "ставка на снижение"
        base = _humanize(raw) or f"{name} ({idea.get('актив')})"
        return f"{base} — {what}. «{idea.get('направление') or '—'}» = {side}."
    if n == 2:
        return _cascade_line(raw) or EMPTY_MARK
    if n == 9:
        score = idea.get("балл")
        note = (f"общий балл {float(score):.2f}/1.00 (это НЕ вероятность из п.3, а сводная оценка "
                f"по 6 критериям). " if isinstance(score, (int, float)) else "")
        return (note + _humanize(raw)).strip() or note.strip() or EMPTY_MARK
    if n == 13:
        # ИНВАРИАНТ §8/§16: рамка-дисклеймер НЕ делегируется LLM. Под --deep отчёт подменяется на
        # judgment, и поле 13 пришло бы из LLM (могло быть пустым → метка исчезла бы, fail-open на
        # money-карточке до калибр-гейта §11). Каноническая рамка — детерминированно, ВСЕГДА.
        return RESEARCH_FRAME
    return _humanize(raw, 360) or EMPTY_MARK


def format_mock_check(protocol):
    """Проверочный (mock/test) прогон — НЕ идея. Шлётся, только если send_mock_to_telegram=true."""
    return ("🔧 Проверка связи — это НЕ идея.\n"
            "Тестируем, что трубопровод работает и формат собирается. Содержательные поля пустые.\n"
            f"· тех.id {protocol.get('run_id','?')} · режим {protocol.get('mode')}")


def format_report(protocol, idea, presentation=None):
    """Карточка идеи §8 человеческим языком: шапка (1 цифра уверенности) + ВСЕ 13 полей с ℹ️.

    Слой ОТОБРАЖЕНИЯ: данные §8 не меняются (журналы пишутся полностью отдельно). mock сюда не
    попадает как идея — для него format_mock_check. long: ℹ️ у всех полей; short: ℹ️ только у
    полей из still_unclear (содержание → всегда)."""
    pres = presentation or load_presentation()
    mode = protocol.get("mode")
    if mode and mode != "live":
        return format_mock_check(protocol)

    idea_d = idea
    pos = idea.get("позиция") if isinstance(idea.get("позиция"), dict) else {}
    fields = _fields_dict(idea)
    asset = idea.get("актив", "?")
    name = _asset_human(asset)[0]
    direction = (idea.get("направление") or "").strip().lower()
    is_long = direction.startswith("лонг") or direction in ("long", "buy")
    arrow = "📈" if is_long else "📉"
    move = "ставка на рост" if is_long else "ставка на снижение"

    # Шапка: заголовок + ОДНА цифра уверенности (вероятность судьи, поле 3). НЕ сводный балл.
    prob = pos.get("вероятность")
    if isinstance(prob, (int, float)):
        conf_line = f"Насколько уверены: ~{round(prob*100)}% (против ~50% «вслепую»)."
    else:
        conf_line = "Насколько уверены: пока не оценена."
    lines = [f"{arrow} Идея дня: {name} ({asset}) — {move}", conf_line, "",
             "📋 Разбираю по полочкам — 13 граней идеи:"]

    show_help_all = (pres["mode"] == "long")
    for n in range(1, 14):
        lines.append("")
        lines.append(f"{n}. {_FIELD_TITLES[n]}")
        if show_help_all or n in pres["still_unclear"]:
            lines.append(f"   ℹ️ {field_help[n]}")
        lines.append(f"   → {_field_content(n, fields, idea_d, pos)}")

    # ИНВАРИАНТ §16/§8: неприкосновенный хвост (пауза §12 + рамка-дисклеймер + тех.id) НЕ участвует
    # в усечении. Иначе на длинной --deep money-карточке (13 полей long-режима > MAX_MSG) обрезка
    # text[:MAX_MSG] срезала бы и поле13, и футер → actionable-карточка с кнопками БЕЗ метки «не
    # рекомендация» (fail-open). Поэтому усекаем ТОЛЬКО тело полей, рамку доливаем ПОСЛЕ.
    tail = "\n".join(["",
                      "⚖️ Кнопка «Беру в работу» откроется через 24 ч — это намеренная пауза, "
                      "чтобы решение не было импульсивным.",
                      f"⚠️ Это {RESEARCH_FRAME} (§16): метка снимается только после калибр-гейта §11.",
                      f"· тех.id {protocol.get('run_id','?')}"])
    body = "\n".join(lines)
    cut_note = "\n…(полный отчёт — в журнале funnel_logs)"
    budget = MAX_MSG - len(tail) - len(cut_note)
    if len(body) > budget:
        body = body[:budget].rstrip() + cut_note
    return body + "\n" + tail


def build_keyboard(token, issued_at, now=None):
    """Inline-клавиатура решения. «Принять» заблокирована паузой §12 (24ч от выдачи).
    Заблокированная кнопка остаётся видимой (с таймером) — нажатие бот отвергнет с подсказкой."""
    unlocked = S.accept_unlocked(issued_at, now)
    if unlocked:
        accept_label = "✅ Беру в работу"
    else:
        rem = S.hours_remaining(issued_at, now)
        rem_txt = f"{rem:.0f} ч" if rem is not None and rem >= 1 else "<1 ч"
        accept_label = f"🔒 Беру в работу (через {rem_txt})"
    return {"inline_keyboard": [[
        {"text": accept_label, "callback_data": f"d:a:{token}"},
        {"text": "❌ Мимо", "callback_data": f"d:r:{token}"},
        {"text": "🕒 Позже", "callback_data": f"d:p:{token}"},
    ]]}


def _human_dir(side):
    return {"шорт": "шорт↓", "лонг": "лонг↑", "short": "шорт↓", "long": "лонг↑"}.get(
        (side or "").lower(), side or "?")


def _safe_name(a):
    try:
        return _asset_human(a or "")[0]
    except Exception:
        return a or "?"


def _sealed_cascades_for(protocol):
    """Best-effort: запечатанные §9-каскады в КОМПАНИИ из соседнего сводного протокола event-first
    (ef_<id>.json, поле каскады_в_компании). Для тематических прогонов (без '__') — пусто."""
    rid = protocol.get("run_id", "")
    if "__" not in rid:
        return []
    try:
        s = json.loads((FUNNEL_LOGS / f"{rid.split('__')[0]}.json").read_text(encoding="utf-8"))
        seals = []
        for c in s.get("каскады_в_компании", []) or []:
            for x in (c.get("запечатываемо") or []):
                seals.append(x.get("prediction", {}))
        return seals
    except Exception:
        return []


def format_analyst_report(idea):
    """Полный аналитический разбор одной research-идеи простым языком: видна ЦЕПОЧКА РАССУЖДЕНИЙ —
    новость → каскад → следствия высоких порядков → идея → рекомендация → обоснование → критика
    (§8, популярно). idea должна нести idea['разбор_аналитика'] = {разбор, критика}."""
    block = idea.get("разбор_аналитика") or {}
    r = block.get("разбор") or {}
    c = block.get("критика") or {}
    if not r:
        # нет разбора (модель не дала) — честный короткий фолбэк
        asset = idea.get("актив")
        return (f"📊 {_safe_name(asset)} ({asset}) — событие: {_trunc(idea.get('событие') or '', 90)}\n"
                "Разбор не сформирован (модель не дала валидного ответа, П8). Сырьё в /progress.")

    L = []
    if r.get("заголовок"):
        L.append(f"📊 {r['заголовок']}")
    L.append(f"\n📰 Что в новостях\n{r.get('что_в_новостях','—')}")

    chain = r.get("цепочка_эффектов") or []
    if chain:
        L.append("\n🔗 Цепочка эффектов (как одно тянет другое)")
        for n in chain:
            tick = n.get("тикер")
            tick = f" [{tick}]" if tick and tick != "null" else ""
            L.append(f"  {n.get('порядок','•')}) {n.get('звено','')} — {n.get('что_происходит','')}{tick}")

    if r.get("следствия_высоких_порядков"):
        L.append(f"\n📈 Что дальше — следствия высоких порядков\n{r['следствия_высоких_порядков']}")

    idd = r.get("идея") or {}
    if idd:
        напр = idd.get("направление", "")
        L.append(f"\n🎯 Идея: {idd.get('компания','')} ({idd.get('тикер', idea.get('актив',''))}) — {напр}")
        if idd.get("что_за_компания"):
            L.append(f"   Что за компания: {idd['что_за_компания']}")
        if idd.get("почему_она"):
            L.append(f"   Почему она: {idd['почему_она']}")

    q = idea.get("квант") or {}
    if q.get("измеримо"):
        L.append("\n📐 Цифры (детерминированный расчёт, не мнение модели)")
        if q.get("амплитуда_pct") is not None:
            r2 = q.get("надёжность_связи")
            shk = q.get("шок_корня_pct")
            shk_s = f" двинулся {shk:+}% → " if shk is not None else " → "
            L.append(f"   • Глубина влияния: корень {q.get('корень_шока')}{shk_s}ожидаемый ход цели "
                     f"≈ {q['амплитуда_pct']:+}% (связь {q.get('ярус','?')}, надёжность r²={r2})")
            if q.get("edge_pct") is not None:
                L.append(f"     ещё не отыграно (edge): {q['edge_pct']:+}%")
        else:
            L.append("   • Глубина влияния: количественно не измерить — нет синхронной истории корня "
                     "каскада (П8). Масштаб — только качественный (тектонический потенциал).")
        L.append(f"   • Масштаб шума: бумага и так обычно ходит ±{q.get('типичный_ход_pct')}% за "
                 f"{q.get('горизонт_дней')}д — сравни с глубиной выше (если меньше — сигнал тонет в шуме)")
        L.append(f"   • Тайминг: {q.get('тайминг')} (пройдено {q.get('spent_sigma')}σ хода) — {q.get('тайминг_почему')}")
        if q.get("вероятность_базовая_pct") is not None:
            L.append(f"   • Вероятность: базовый шанс такого хода ≈ {q['вероятность_базовая_pct']:.0f}% "
                     "(нормальная модель, БЕЗ учёта новости; событие смещает вверх, но насколько — см. надёжность)")
    elif q:
        L.append(f"\n📐 Цифры: не посчитать — {q.get('причина','нет данных (П8)')}")

    if r.get("рекомендация"):
        L.append(f"\n🧭 На что смотреть (это наблюдение, не совет купить)\n{r['рекомендация']}")
    if r.get("обоснование"):
        L.append(f"\n🧠 Почему это может сработать\n{r['обоснование']}")

    if c:
        L.append("\n⚔️ Критика (отдельная модель спорит с идеей)")
        if c.get("кто_на_другой_стороне"):
            L.append(f"   Кто против и почему может быть прав: {c['кто_на_другой_стороне']}")
        if c.get("самое_слабое_звено"):
            L.append(f"   Самое слабое звено: {c['самое_слабое_звено']}")
        for x in (c.get("что_обрушит_тезис") or [])[:3]:
            L.append(f"   ⚠ Обрушит тезис: {x}")
        if c.get("вердикт"):
            unc = c.get("уверенность_в_идее")
            L.append(f"   Вердикт скептика: {c['вердикт']}" + (f" (уверенность: {unc})" if unc else ""))

    unknowns = r.get("что_неизвестно") or []
    if unknowns:
        L.append("\n❓ Чего мы не знаем")
        for u in unknowns[:4]:
            L.append(f"   • {u}")

    L.append("\n⚠️ Это исследовательский разбор, не инвестиционная рекомендация. Полная цепочка — /progress.")
    return "\n".join(L)



def _attention_line(obj, indent="   "):
    """П2а (§R4.2): строка поля «внимание» для карточки/истории. None-safe: поля нет — молчим
    (старые протоколы); «не_измерено» — честная категория, не штраф (§R0#5)."""
    att = obj.get("внимание") or {}
    if not att:
        return []
    if att.get("статус") == "ok":
        L = [f"{indent}🌡 Внимание толпы (Trends «{att.get('ключ')}»): фаза {att.get('фаза')}, "
             f"свежесть {att.get('свежесть')}"]
        if att.get("предупреждение"):
            L.append(f"{indent}⚠️ {att['предупреждение']}")
        return L
    return [f"{indent}🌡 Внимание толпы: не измерено ({_trunc(att.get('причина') or 'нет данных', 90)})"]


def _carto_story(ci):
    """Картограф-идея → читаемый блок «новость → цепочка-аргументация → компания» (то, что владелец
    и хочет видеть: ПОЧЕМУ идея, а не голый тикер). Источник «почему» — событие новости + узлы каскада
    с текстовой логикой по порядкам; ничего не выдумываем сверх посчитанного картографом (П8)."""
    ev = _trunc(ci.get("событие") or "—", 220)
    insts = ci.get("все_инструменты") or ([ci.get("актив")] if ci.get("актив") else [])
    target = insts[0] if insts else "?"
    nm = _safe_name(target); label = target if nm == target else f"{nm} ({target})"
    L = [f"📰 {ev}", f"   → ставит под удар: {label}" + (f" (и {', '.join(insts[1:3])})" if len(insts) > 1 else "")]
    L += _attention_line(ci)                    # П2а: инфо-поле «внимание» (§R4.2)
    nodes = sorted((ci.get("узлы_каскада") or []), key=lambda n: n.get("порядок") or 0)
    if nodes:
        L.append("   Цепочка последствий:")
        for nd in nodes[:3]:
            ordn = nd.get("порядок"); txt = _trunc(nd.get("узел") or "", 130)
            chok = " 🔒узкое место" if nd.get("чокпоинт") else ""
            tk = ", ".join(_safe_name(t) for t in (nd.get("тикеры") or [])[:3])
            L.append(f"     {ordn}-й порядок{chok}: {txt}" + (f" [{tk}]" if tk else ""))
    # Тайминг входа человеческим языком: насколько рынок уже отыграл этот узел (ПОЗДНО/ВОВРЕМЯ).
    pr = ci.get("отыгранность_узла")
    if isinstance(pr, (int, float)):
        if pr >= 0.66:
            L.append(f"   ⏱ Тайминг: рынок уже отыграл ~{pr:.0%} — велик риск ВОЙТИ ПОЗДНО, запас хода мал")
        elif pr <= 0.33:
            L.append(f"   ⏱ Тайминг: рынок почти не отыграл (~{pr:.0%}) — запас хода ещё есть")
        else:
            L.append(f"   ⏱ Тайминг: рынок отыграл ~{pr:.0%} — отыгрывается прямо сейчас")
    # Доводы за/против простым языком (П8 — из посчитанного: чокпоинт=сила, далёкий порядок=риск).
    far_chok = any(nd.get("чокпоинт") for nd in nodes)
    far_order = max((nd.get("порядок") or 0) for nd in nodes) if nodes else 0
    за = "в цепочке есть узкое место — через него идёт весь поток, поэтому бьёт сильнее" if far_chok else \
         "идея на конкретной компании, а не на размытом индексе"
    против = (f"звено далеко ({far_order}-й шаг от события): связь косвенная, по дороге может рассеяться"
              if far_order >= 3 else "идею ещё не прогоняли через состязательный разбор — это сырая наводка, не вердикт")
    L.append(f"   ✅ За: {за}")
    L.append(f"   ⚠️ Против: {против}")
    tp = ci.get("тектонический_потенциал")
    if isinstance(tp, (int, float)):
        L.append(f"   Насколько прочна эта связь: {_strength_word(tp)}.")
    return "\n".join(L)


def _strength_word(x):
    """0–1 балл прочности связи → человеческое слово вместо голой цифры (клиенту код не нужен)."""
    if not isinstance(x, (int, float)):
        return "не оценена"
    if x >= 0.66:
        return "высокая"
    if x >= 0.33:
        return "средняя"
    return "слабая (косвенная)"


# Порог слабой связи — зеркалит mathlib.cascade.WEAK_R2 (держим локально: рендер не тянет mathlib).
_WEAK_R2 = 0.10


def _pct(x):
    """Доходность-доля узла → человеческий процент со знаком (амплитуда ≈ ожидаемое движение)."""
    return f"{x * 100:+.1f}%" if isinstance(x, (int, float)) else None


def _resolve_date(ts, horizon_days):
    """Дата ориентировочной сверки = время прогона + горизонт оценки (для «когда отыграется»)."""
    try:
        import datetime as _dt
        base = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (base + _dt.timedelta(days=int(horizon_days))).date().isoformat()
    except Exception:
        return None


def _when_line(n, ts):
    """«Когда ждать отыгрывания» — из ПОСЧИТАННЫХ лага связи и горизонта (П8: не выдуманный срок)."""
    лаг, гор = n.get("лаг_дней"), n.get("горизонт_дней")
    parts = []
    if isinstance(лаг, (int, float)) and лаг:
        parts.append(f"исторически реакция доходила до этого звена за ~{int(round(лаг))} дн.")
    if isinstance(гор, (int, float)) and гор:
        d = _resolve_date(ts, гор)
        parts.append(f"горизонт оценки {int(гор)} дн" + (f" — сверим ~{d}" if d else ""))
    return "   ⏱ Когда ждать: " + "; ".join(parts) + "." if parts else None


def _pro_con(n, court):
    """Доводы ЗА и ПРОТИВ — каждый ИЗ посчитанного поля (П8), плюс контраргумент слепого суда, если был.
    Это §8: «кто продаёт нам и почему неправ» + «что неизвестно». Возвращает (за:list, против:list)."""
    за, против = [], []
    edge, rel, order = n.get("edge"), n.get("надёжность_r2"), n.get("порядок")
    provis, court = n.get("провизорный"), (court if isinstance(court, dict) else None)
    # ЗА — почему здесь возможность (довод судьи первым: он содержательнее служебных меток)
    if court and court.get("почему_возможность"):
        за.append("суд: " + _trunc(str(court["почему_возможность"]), 160))
    if isinstance(edge, (int, float)) and edge:
        за.append(f"рынок ещё не отыграл ожидаемое движение ~{_pct(edge)} — запас хода есть")
    if isinstance(rel, (int, float)) and rel >= _WEAK_R2:
        за.append(f"связь подтверждена историей цен: объясняет ~{rel * 100:.0f}% колебаний звена")
    if n.get("чокпоинт"):
        за.append("это узкое место цепочки — через него проходит весь поток, бьёт сильнее")
    if not provis:
        за.append("связь проверена на многолетней истории цен, а не просто на логике")
    # ПРОТИВ — кто продаёт нам и что может сорваться
    if court and court.get("кто_против"):
        против.append("кто против нас: " + _trunc(str(court["кто_против"]), 170))
    if isinstance(order, int) and order >= 3:
        против.append(f"дальнее звено ({order}-й порядок): связь косвенная, эффект может рассеяться")
    if isinstance(rel, (int, float)) and rel < _WEAK_R2:
        против.append(f"слабая статистика связи (объясняет лишь ~{rel * 100:.0f}% движений — мало истории)")
    if provis:
        против.append("это пока гипотеза: связь не подтверждена ценами, на цифры опираться рано")
    if court and court.get("примечание"):
        против.append(_trunc(str(court["примечание"]), 150))
    if not court:
        против.append("идею ещё не прогоняли через состязательный разбор (это быстрый дневной скан) — "
                      "сырая наводка, а не вердикт; за глубоким разбором напиши «разбери ТИКЕР»")
    return за, против


def _idea_card(n, court, ts):
    """Полная карточка идеи для инвестора: что → почему (новость+цепочка) → когда → за/против → суд.
    Закрывает претензию владельца: не голый тикер+edge, а решение можно понять и принять (§8)."""
    a = n.get("актив"); nm = _safe_name(a)
    label = a if nm == a else f"{nm} ({a})"
    track = "🧪 пока гипотеза" if n.get("провизорный") else "💰 связь проверена ценами"
    # Сторона: явное поле, иначе выводим из знака ожидаемого хода (edge>0 → рост → лонг).
    side = n.get("направление")
    if not side:
        edge = n.get("edge")
        if isinstance(edge, (int, float)) and edge:
            side = "лонг" if edge > 0 else "шорт"
    L = [f"\n• {label} — {_human_dir(side)} · {track}"]
    L += _attention_line(n)                     # П2а: инфо-поле «внимание» (§R4.2)
    # ПОЧЕМУ смотрим: новость-повод + где это в цепочке последствий
    ev = n.get("событие")
    if ev:
        src = "картограф событий" if n.get("источник_карты") == "картограф" else "авторская цепочка"
        L.append(f"   📰 Повод ({src}): {_trunc(str(ev), 170)}")
    як, order = n.get("якорь"), n.get("порядок")
    if як:
        tail = f", это {order}-й порядок цепочки" if isinstance(order, int) else ""
        L.append(f"   🔗 Как доходит: шок по {_safe_name(як)} ({як}) → доводится до {nm}{tail}")
    when = _when_line(n, ts)
    if when:
        L.append(when)
    за, против = _pro_con(n, court)
    if за:
        L.append("   ✅ Доводы ЗА:")
        L += [f"      + {x}" for x in за[:4]]
    if против:
        L.append("   ⚠️ Доводы ПРОТИВ:")
        L += [f"      − {x}" for x in против[:4]]
    if isinstance(court, dict) and court.get("исход"):
        mark = {"УСТОЯЛА": "✅", "РАЗБИТА": "❌", "ВЕТО": "⛔"}.get(court["исход"], "•")
        балл, порог = court.get("балл"), court.get("порог")
        sc = f" (балл {балл:.1f}/{порог:.0f})" if isinstance(балл, (int, float)) and isinstance(порог, (int, float)) else ""
        L.append(f"   ⚖ Независимый разбор (суд из другой модели): {mark} {court['исход']}{sc}")
    p = n.get("вероятность")
    if isinstance(p, (int, float)):
        cav = " (грубая прикидка, не основание для ставки)" if n.get("провизорный") else ""
        L.append(f"   🎲 На глаз шанс, что движение случится: ~{p * 100:.0f}%{cav}")
    return "\n".join(L)


def format_research_digest(protocol):
    """Event-first RESEARCH-ПОТОК (РЕШЕНИЕ A, анти-brent): идеи по событиям мира на КОНКРЕТНЫХ компаниях.
    Каждая идея ОБЪЯСНЯЕТСЯ: новость (событие) → цепочка-аргументация по порядкам → инструмент, а для
    money-трека — вердикт слепого суда (П10) с контраргументом. Это НЕ ставки (research-метка §16)."""
    g = protocol.get("граф_отбор") or {}
    rid = protocol.get("run_id", "?")
    топ = g.get("топ_k") or []
    карто = protocol.get("картограф_идеи") or []
    карты = [c for c in (protocol.get("каскады_в_компании") or []) if not c.get("пропуск")]
    if not топ and not карто:
        return ("🧭 Сегодня ни одно событие в мире не дотянуло до торгуемой цепочки. "
                "Это честный пустой день, а не сбой — лучше промолчать, чем выдумать идею.\n"
                f"Как я искал — /progress · тех.id {rid}")
    треки = g.get("треки") or {}
    зап = g.get("запечатано") or {}
    добор = (g.get("добор_истории") or {}).get("скачано") or []
    суд = g.get("суд_money") or {}

    lines = ["🧭 Идеи дня: от события в мире — к конкретной компании",
             "Ниже — над чем я думаю. Это пища для размышления, НЕ сигнал к покупке: "
             "вероятности ещё не откалиброваны, ставки по ним не делаем.",
             f"Сегодня разобрал {len(карты)} событий, построил {g.get('узлов', 0)} звеньев цепочек, "
             f"по ходу подтянул {len(добор)} новых бумаг."]

    # БЛОК 1 — истории «новость → цепочка → компания» (картограф): это и есть объяснение ПОЧЕМУ.
    if карто:
        lines.append("\n— Как событие дотягивается до компании —")
        # #14: НЕ режем истории до 3 — это хоронило сигнал при 6-7 идеях. Показываем все; если их
        # много (>CARTO_SHOW) — первые, а хвост отправляем в /progress, чтобы не раздувать пуш.
        show = карто if len(карто) <= CARTO_SHOW else карто[:CARTO_SHOW]
        for ci in show:
            lines.append("\n" + _carto_story(ci))
        if len(карто) > len(show):
            lines.append(f"\n…ещё {len(карто) - len(show)} таких историй — полный список /progress")

    # БЛОК 2 — отобранное графом под форвард-трек: ПОЛНАЯ карточка каждой идеи (что → почему →
    # когда → доводы за/против → вердикт суда). Это и есть разбор, по которому инвестор примет решение.
    ts = protocol.get("ts")
    if топ:
        lines.append("\n— Где рынок, похоже, ещё не отыграл движение —")
        for n in топ[:6]:
            lines.append(_idea_card(n, суд.get(n.get("актив")), ts))

    # #12: «можно довести до ставки» считаем ПОСЛЕ слепого суда (fail-closed §11): только money-узлы
    # с исходом «УСТОЯЛА». Сырой треки['money'] — это ДО суда; те же узлы ниже показаны как РАЗБИТА,
    # отчего дайджест противоречил сам себе. Демотированные судом money-узлы уходят в полку гипотез.
    money_ok, money_demoted = _money_after_court(g)
    prov_total = (треки.get("провизорный", 0) or 0) + money_demoted
    lines.append(f"\nДве полки идей: 💰 {money_ok} с проверенной ценами связью, ПЕРЕЖИВШИХ слепой суд "
                 f"(их можно довести до реальной ставки) · 🧪 {prov_total} пока на уровне "
                 f"гипотезы (считаю отдельно, в деньги не пускаю).")
    if зап.get("money") or зап.get("провизорный"):
        lines.append(f"Зафиксировал с отметкой времени, чтобы потом честно сверить прогноз с фактом: "
                     f"{зап.get('money', 0)} проверенных · {зап.get('провизорный', 0)} гипотез.")
    lines.append("⚠️ Это исследование, а не инвестиционная рекомендация — я показываю ход мысли, "
                 "решение за тобой. Хочешь, чтобы я копнул любую идею глубоко — напиши «разбери ТИКЕР». "
                 "Вся кухня отбора — /progress.")
    lines.append(f"· тех.id {rid}")
    return "\n".join(lines)



# ── П3: сессия партнёра (§17.6, REVISION_2026-07 §R3) ─────────────────────────────
def format_partner_session(session, metrics=None):
    """«Дай идеи» → 3-5 идей с готовой защитой и приглашением докапываться. Только посчитанное
    прогоном (П8); порядок = порядок выдачи прогона (граница П2б); рамка §16 обязательна."""
    if session.get("ОТКАЗ"):
        return ("🤝 Сессия не собралась: " + session["ОТКАЗ"])
    L = ["🤝 Сессия партнёра — над чем стоит подумать сегодня"]
    if session.get("прогон_устарел"):
        L.append(f"⚠️ Свежего прогона нет — идеи из прогона {session.get('возраст_прогона_дней')} дн "
                 f"назад. Хочешь свежий скан — «/run-funnel».")
    if session.get("мало_идей"):
        L.append(f"Сегодня честно только {session.get('идей')} ид. — день слабый, натягивать не буду (П8).")
    for i, idea in enumerate(session.get("идеи", []), 1):
        a = idea.get("актив"); nm = _asset_human(a)[0]
        label = a if nm == a else f"{nm} ({a})"
        side = _human_dir(idea.get("направление")) if idea.get("направление") else "направление не мерено"
        L.append(f"\n{i}. {label} — {side} · {idea.get('источник')}")
        arg = idea.get("аргумент") or {}
        if arg.get("событие"):
            L.append(f"   📰 Повод: {_trunc(arg['событие'], 180)}")
        if arg.get("узлы_каскада"):
            for nd in arg["узлы_каскада"][:3]:
                chok = " 🔒узкое место" if nd.get("чокпоинт") else ""
                L.append(f"   {nd.get('порядок')}-й порядок{chok}: {_trunc(nd.get('узел') or '', 120)}")
        if arg.get("цепочка"):
            edge = arg.get("неотыгранный_edge")
            L.append(f"   Цепочка {arg['цепочка']}, узел {arg.get('порядок_узла')}-го порядка"
                     + (" 🔒" if arg.get("чокпоинт") else "")
                     + (f"; неотыгранный ход ≈ {round(edge * 100, 1)}%" if isinstance(edge, (int, float)) else ""))
        суд = idea.get("суд") or {}
        исход = суд.get("исход")
        if исход:
            L.append(f"   ⚖️ Слепой суд: {исход}" +
                     (f" (балл {суд.get('балл')})" if суд.get("балл") is not None else ""))
            # stage-review П3 HIGH-2: заголовки соответствуют ВЕРДИКТУ — для РАЗБИТА судья встал
            # на сторону «другой стороны», рамка «почему неправ» лгала бы (П8)
            if суд.get("кто_продаёт_нам"):
                head = ("🥊 Кто продаёт нам и почему он неправ" if исход == "УСТОЯЛА"
                        else "🥊 Кто на другой стороне — суд встал на их сторону" if исход == "РАЗБИТА"
                        else "🥊 Кто на другой стороне")
                L.append(f"   {head}: {_trunc(суд['кто_продаёт_нам'], 200)}")
            if суд.get("почему_возможность"):
                head2 = ("⏳ Почему возможность ещё жива" if исход == "УСТОЯЛА"
                         else "⏳ Что суд сказал о возможности")
                L.append(f"   {head2}: {_trunc(суд['почему_возможность'], 200)}")
            if исход in ("ПРОПУСК", "ОШИБКА_СУДА") and суд.get("примечание"):
                L.append(f"   ℹ️ {_trunc(суд['примечание'], 160)}")   # stage-review LOW-1: смысл, не ярлык
        elif суд.get("пометка"):
            L.append(f"   ⚖️ {суд['пометка']}")
        L += _attention_line(idea)
    m = metrics or {}
    if m and not m.get("ошибка"):
        surv = m.get("выживаемость")
        L.append(f"\n📈 Наша неделя: сессий {m.get('сессий')}, докапываний {m.get('докапываний')}"
                 + (f", выживаемость идей под вопросами {round(surv * 100)}%" if surv is not None else ""))
    L.append("\nДокапывайся: «разбери ТИКЕР» — полный разбор компании; «/debate <возражение>» — "
             "я сведу спор моделей по твоему сомнению (сам не сужу).")
    L.append("⚠️ Это исследование, а не инвестиционная рекомендация — решение принимаем вместе (§16).")
    return "\n".join(L)


def format_weak_day(protocol):
    """Пуш слабого дня — НЕ отписка, а ДАЙДЖЕСТ ПРОЗРАЧНОСТИ (§15): что рассмотрено, что
    ближе всех к проходному баллу, что отсеяно раньше, что всё-таки запечатано. Ставку не
    делаем — но показываем работу, чтобы система не была чёрным ящиком."""
    theme = protocol.get("theme") or "—"
    fr = protocol.get("воронка_отсева") or {}
    lines = [f"🔎 Идей под ставку сегодня нет — но я не молчу впустую. Что рассмотрено по «{theme}»:"]

    n_cand = fr.get("этап2_кандидатов")
    n_judged = fr.get("этап4_в_дебаты_топ")
    if n_cand is not None:
        seg = f"\nПросмотрел {n_cand} кандидатов"
        if n_judged is not None:
            seg += f", до независимого разбора довёл {n_judged}"
        lines.append(seg + ".")

    # ближе всех к порогу — дебаты по убыванию балла рубрики
    rows = []
    for x in (protocol.get("этап5_дебаты") or []):
        v = x.get("вердикт") or {}
        sc = v.get("средний_балл_рубрики")
        if sc is not None:
            rows.append((sc, x.get("актив"), x.get("направление"), v.get("порог", 3.0)))
    rows.sort(reverse=True)
    if rows:
        lines.append(f"\nБлиже всех подошли (проходной балл — ≥ {rows[0][3]:.0f} по 5-балльной шкале):")
        for i, (sc, asset, side, th) in enumerate(rows[:3]):
            tag = "  ⟵ ближе всех" if i == 0 else ""
            lines.append(f"  • {_safe_name(asset)} ({asset}) {_human_dir(side)} — оценка {sc:.1f}{tag}")
        lines.append("Никто не дотянул — значит доводов пока тонко, а не «на рынке совсем пусто».")

    drop3 = fr.get("отсев_этап3") or []
    if drop3:
        d0 = drop3[0]
        if isinstance(d0, dict):
            why = d0.get("причина_отсева") or d0.get("причина") or "не прошёл грубый фильтр"
            head = f"{_safe_name(d0.get('актив'))} {_human_dir(d0.get('направление'))} — {why}"
        else:
            head = str(d0)
        more = f" (и ещё {len(drop3) - 1})" if len(drop3) > 1 else ""
        lines.append(f"\nОтсеял ещё раньше: {_trunc(head, 110)}{more}")

    # что ВСЁ-ТАКИ зафиксировано — каскадные §9-прогнозы (это и есть «не пусто»)
    seals = _sealed_cascades_for(protocol)
    if seals:
        lines.append("\n📌 Кое-что я всё же зафиксировал на будущее — прогнозы по цепочкам "
                     "(записал с отметкой времени, потом честно сверю с фактом):")
        for pr in seals[:3]:
            d = {"below": "уйдёт ниже", "above": "уйдёт выше"}.get(pr.get("direction"), pr.get("direction", ""))
            r2 = pr.get("reliability_r2")
            r2s = f"; прочность связи {_strength_word(r2)}" if isinstance(r2, (int, float)) else ""
            lines.append(f"  • {_safe_name(pr.get('asset'))} ({pr.get('asset')}) {d} {pr.get('threshold')} "
                         f"к {(pr.get('resolve_by') or '')[:10]}{r2s}")

    lines.append("\nПочему без ставки: ставить на недотянувшие идеи — это терять деньги на «монетках» (50/50). "
                 "Лучшее, что можно сделать в слабый день, — НЕ сделать плохую ставку. Как я искал — /progress.")
    lines.append(f"· тех.id {protocol.get('run_id','?')}")
    return "\n".join(lines)


# ── бюджет ──────────────────────────────────────────────────────────────────────────
def format_budget_line(one_liner_text):
    """Утренняя строка бюджета — расход на работу системы, человеческим языком."""
    return ("🌅 Сколько стоит работа системы в этом месяце\n" + _budget_human(one_liner_text)
            + "\n(Это расходы на ИИ-модели, не твои инвестиции.)")


def _budget_human(one_liner_text):
    """Из тех.строки budget.one_liner вытащить только понятную часть: $потрачено/$лимит."""
    import re as _re
    m = _re.search(r"токены \$([\d.]+)/\$([\d.]+)", one_liner_text)
    if m:
        spent, cap = float(m.group(1)), float(m.group(2))
        return f"Потрачено ${spent:.0f} из месячного лимита ${cap:.0f} ({spent/cap*100:.0f}%)."
    return one_liner_text


def format_budget_alert(st, one_liner_text):
    """Предупреждение по бюджету человеческим языком (внимание / достигнут потолок)."""
    if st.get("exit_code") == 3:
        return ("🛑 Достигнут месячный лимит расходов на работу системы — новые прогоны "
                "приостановлены до начала следующего месяца. Это защитный лимит, не ошибка.\n"
                + _budget_human(one_liner_text))
    return ("⚠️ Внимание: расходы на работу системы приблизились к месячному лимиту.\n"
            + _budget_human(one_liner_text))


# ── сработал ожидаемый ценовой ориентир ──────────────────────────────────────────────
def format_trigger_alert(fired):
    """Сработал заранее заданный ценовой ориентир по активу из листа ожидания (человеческим языком)."""
    e = fired["entry"]
    obs = fired["observed"]
    t = e.get("trigger") or {}
    name = _asset_human(e.get("asset", ""))[0]
    side = {"above": "поднялась выше", "below": "опустилась ниже"}.get(t.get("dir"), "достигла")
    ctx = e.get("trigger_text")
    return (f"🔔 Сработал ориентир, который ты ждал: {name} ({e.get('asset')})\n\n"
            f"Цена {side} уровня {t.get('level')} (сейчас {obs.get('close')}, {obs.get('date')}).\n"
            + (f"Напоминание по идее: {ctx}\n" if ctx else "")
            + "Это был «вход по триггеру» — момент, которого мы ждали. Имеет смысл свежий разбор "
              "по этому активу (напиши мне «разбери {}»).".format(name))


def escape(s):
    """На случай parse_mode=HTML (по умолчанию шлём без разметки — экранирование не требуется)."""
    return html.escape(str(s))
