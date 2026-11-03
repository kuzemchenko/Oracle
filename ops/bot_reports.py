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
    # имя без вложенных скобок: рендер печатает «имя (ТИКЕР)», и «(VIX) (VXX.US)» читалось двойными
    # скобками (испытатель 01.11)
    "VIXY.US": ("фонд на индекс страха VIX", "растёт, когда рынок в панике"),
    "VXX.US": ("фонд на индекс страха VIX", "растёт, когда рынок в панике"),
    # — ходовые индексные/секторные фонды (испытатель 01.11: «RSP.US — компанию не опознал» на
    # одном из крупнейших ETF США бьёт по доверию; это фонды, не компании) —
    "RSP.US": ("фонд широкого рынка США с равными весами", "каждая компания индекса с одинаковым весом — без перекоса в гигантов"),
    "QQQ.US": ("фонд индекса Nasdaq", "крупнейшие технологические компании США"),
    "DIA.US": ("фонд индекса Dow Jones", "три десятка крупнейших промышленных компаний США"),
    "SMH.US": ("фонд производителей чипов", "полупроводниковая отрасль целиком"),
    "SOXX.US": ("фонд производителей чипов", "полупроводниковая отрасль целиком"),
    "LQD.US": ("фонд надёжных корпоративных облигаций", "долг крупных компаний США"),
    "EFA.US": ("фонд акций развитых стран без США", "Европа, Япония, Австралия одной корзиной"),
    "VGK.US": ("фонд акций Европы", "крупные европейские компании одной корзиной"),
    "MCHI.US": ("фонд акций Китая", "крупные китайские компании одной корзиной"),
    "EWJ.US": ("фонд акций Японии", "крупные японские компании одной корзиной"),
    "EWG.US": ("фонд акций Германии", "крупные немецкие компании одной корзиной"),
    "XLF.US": ("фонд финансового сектора США", "банки, страховые, биржи"),
    "XLI.US": ("фонд промышленного сектора США", "машиностроение, транспорт, оборонка"),
    "XLU.US": ("фонд коммунального сектора США", "электро- и водоснабжение — защитный сектор"),
    "XLV.US": ("фонд сектора здравоохранения США", "фарма, страховки, медтехника"),
    "XLP.US": ("фонд товаров первой необходимости", "еда, напитки, бытовые товары — защитный сектор"),
    "XLB.US": ("фонд сектора материалов США", "химия, металлы, упаковка"),
    "URA.US": ("фонд урановой отрасли", "добытчики урана и ядерное топливо"),
    "XME.US": ("фонд металлургии и добычи США", "сталь, уголь, цветные металлы"),
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
            "send_mock_to_telegram": bool(cfg.get("send_mock_to_telegram", False)),
            # Этап3: «Разбор дня» как ГЛАВНЫЙ дневной пуш (читаемый кейс без эмодзи) вместо длинного
            # эмодзи-дайджеста. Дефолт False — включаем после подтверждения читаемости владельцем.
            "daily_case_lead": bool(cfg.get("daily_case_lead", False)),
            # V0 (карта v3, 31.10): «Идея дня» — формат владельца 15.07; ГЛАВНЫЙ пуш, приоритетнее Топ-3.
            # Не прошёл линт → пуш НЕ уходит, заметка в дозор (без фолбэка — по правилу этапа V0).
            "idea_day_lead": bool(cfg.get("idea_day_lead", False)),
            # Flow-fix В1 (15.07): «Топ-3 одной строкой» + защита №1 — формат, выбранный владельцем
            # взамен «Разбора дня». Приоритетнее daily_case_lead: если включён и собрался — он лид.
            "top3_lead": bool(cfg.get("top3_lead", False)),
            # V5 (карта v3): пуш «Шансов недели». Дефолт False (ПРАВИЛО ВНЕДРЕНИЯ 01.11) —
            # включается ТОЛЬКО после ручного прогона испытателя и «ок» владельца.
            # Баг аудита 01.11: ключ раньше не пробрасывался — флаг было невозможно включить.
            "week_odds_push": bool(cfg.get("week_odds_push", False)),
            # 02.11 (вердикт владельца «ничего не понимаю»): редактор-рассказчик «Идеи дня».
            # Дефолт False — включается после ручного прогона испытателя и «ок» (правило внедрения).
            "story_editor": bool(cfg.get("story_editor", False)),
            # Мост идей охотника (облако → reports/ideas/*.push.txt → git pull → бот).
            # Дефолт False (правило внедрения); в бою контент гейтится lint_hunter_idea КОДОМ.
            "hunter_push": bool(cfg.get("hunter_push", False)),
            # Архитектура 02.11: «наблюдение» серверного конвейера (защищать нечего) главным
            # пушем не уходит — идея дня от охотника. Дефолт True = прежнее поведение.
            "server_observation_push": bool(cfg.get("server_observation_push", True))}


def save_presentation(mode=None, still_unclear=None, send_mock=None, path=None):
    """Записать настройки подачи (для команды /format). Меняет ТОЛЬКО слой отображения."""
    import yaml
    p = pathlib.Path(path or (ROOT / "config" / "presentation.yaml"))
    cur = load_presentation(p)
    obj = {"version": 1,
           "mode": mode if mode in ("long", "short") else cur["mode"],
           "still_unclear": sorted(still_unclear if still_unclear is not None else cur["still_unclear"]),
           "send_mock_to_telegram": cur["send_mock_to_telegram"] if send_mock is None else bool(send_mock),
           # флаги лидов и V5 /format НЕ меняет, но ОБЯЗАН сохранять (аудит 01.11: перезапись
           # конфига молча стирала включённые руками флаги)
           "daily_case_lead": cur["daily_case_lead"],
           "top3_lead": cur["top3_lead"],
           "idea_day_lead": cur["idea_day_lead"],
           "week_odds_push": cur["week_odds_push"],
           "story_editor": cur["story_editor"],
           "hunter_push": cur["hunter_push"],
           "server_observation_push": cur["server_observation_push"]}
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


def _rank_line(obj, indent="   "):
    """П2б (§R4.4, подпись 05.07): строка продуктового ранга показа. None-safe: ранга нет
    (старый протокол/не аннотировано) — молчим. Поздняя фаза — обязательная пометка (§R5)."""
    pr = obj.get("продуктовый_ранг") or {}
    if not pr:
        return []
    c = pr.get("компоненты") or {}
    прайоры = f" · прайоры: {len(pr['не_измерено'])}" if pr.get("не_измерено") else ""
    L = [f"{indent}⭐ Ранг показа {pr.get('балл')} — неочевидность {c.get('неочевидность')} × "
         f"свежесть {c.get('свежесть')} × доказуемость {c.get('доказуемость')}{прайоры}"]
    if pr.get("поздняя_фаза"):
        L.append(f"{indent}⏰ ПОМЕТКА: фаза «{pr['поздняя_фаза']}» — толпа уже здесь, вход поздний (§R5)")
    return L


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
    L += _rank_line(ci)                         # П2б: продуктовый ранг показа (§R4.4)
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
    L += _rank_line(n)                          # П2б: продуктовый ранг показа (§R4.4)
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
    # П2б (§R4.4, подпись 05.07): порядок ПОКАЗА в дайджесте = продуктовый ранг «неочевидность ×
    # свежесть × доказуемость». Слой Б: протокол/треки/суд не тронуты — только очередность показа.
    from orchestrator import product_rank as PR
    топ = PR.annotate_and_sort(list(g.get("топ_k") or []))
    карто = PR.annotate_and_sort(list(protocol.get("картограф_идеи") or []))
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


# ── Этап3 (пакет после аудита): артефакт «Разбор дня» — материал для тренировки суждения ──────
# Рендер БЕЗ ЭМОДЗИ (style contract STYLE_CONTRACT.md): владельцу нужен читаемый текст, а не иконки.
# Данные — orchestrator.daily_case.select_case; здесь только человекочитаемая раскладка 7 блоков.
DAILY_CASE_MAX = 3500     # кэп «Разбора дня» (STYLE_CONTRACT); линтер ops/presentation_lint это держит


def format_daily_case(case):
    """«Разбор дня» → 7 обязательных блоков простым языком, БЕЗ эмодзи. case — из
    daily_case.select_case. Пустой день (нет даже урока) — честная строка, не отписка (§15)."""
    if not case or case.get("пусто"):
        rid = (case or {}).get("тех_id", "?")
        дата = (case or {}).get("дата")
        head = f"Разбор дня{(', ' + дата) if дата else ''}."
        return (head + "\n"
                "Что это значит для тебя. Сегодня не набралось даже учебного кейса — ни кандидатов "
                "к разбору, ни разрешённых прогнозов, ни урока про шум. Молчу честно, а не натягиваю "
                "материал.\n"
                "Что делать. Ничего — навязывать пустой день не буду. Загляну завтра.\n"
                f"Как я искал — /progress · тех.id {rid}")

    дата = case.get("дата")
    L = [f"Разбор дня{(', ' + дата) if дата else ''}. Тип: {case.get('статус_человек')}.",
         "",
         # Секция «что это значит для тебя» (STYLE_CONTRACT п.3 — обязательна, линтер валит без неё)
         f"Что это значит для тебя. {case.get('значит_для_тебя')}",
         "",
         f"Тезис. {case.get('заголовок') or '—'}"]

    # Блок 2 — цепочка рассуждения по порядкам (данные или честное «данных нет»)
    цепочка = case.get("цепочка") or []
    L.append("")
    L.append("Цепочка рассуждения — как связь доходит до компании:")
    if цепочка:
        for nd in цепочка:
            ordn = nd.get("порядок")
            head = f"  {ordn}-й шаг от события" if isinstance(ordn, int) else "  звено"
            if nd.get("чокпоинт"):
                head += " (узкое место — через него идёт весь поток)"
            tk = ", ".join(_safe_name(t) for t in (nd.get("тикеры") or []))
            L.append(f"{head}: {_trunc(nd.get('узел') or 'узел не назван', 150)}"
                     + (f" [{tk}]" if tk else ""))
    else:
        L.append("  Данных по звеньям пока нет — связь не разложена по шагам (честно: здесь данных нет).")

    # Блок 3 — таблица баллов (6 критериев; неизмеренное = «не измерено», не выдумка)
    L.append("")
    L.append("Баллы по кейсу (что не измерено — так и помечено):")
    for крит, знач in (case.get("баллы") or []):
        L.append(f"  {крит}: {_score_human(знач)}")

    # Блок 4 — кто продаёт нам и почему он неправ, или честное «ответа нет»
    L.append("")
    кто = case.get("кто_продаёт")
    if кто:
        L.append(f"Кто на другой стороне и почему может быть неправ: {_trunc(кто, 300)}")
    else:
        L.append("Кто на другой стороне: честного ответа пока нет — идею не прогоняли через "
                 "глубокий состязательный разбор. Это быстрый дневной кейс, а не вердикт.")

    # Блок 5 — идея неверна, если…
    L.append("")
    L.append("Идея неверна, если:")
    for w in (case.get("неверна_если") or []):
        L.append(f"  — {_trunc(w, 200)}")

    # Блок 6 — статус в воронке + причина смерти/остановки
    L.append("")
    L.append(f"Где кейс в воронке: {_trunc(case.get('статус_воронки') or '—', 300)}")

    # Секция «что делать / чего не делать / что решать» (STYLE_CONTRACT п.6 — обязательна)
    L.append("")
    L.append(f"Что делать. {case.get('что_делать')}")

    # Блок 7 — вопрос владельцу с вариантами (кормит decisions_user.jsonl)
    q = case.get("вопрос") or {}
    if q.get("текст"):
        L.append("")
        L.append(f"Твой ход: {q['текст']}")
        вар = q.get("варианты") or []
        if вар:
            L.append("  Варианты: " + " · ".join(вар))

    L.append(f"· тех.id {case.get('тех_id', '?')}")
    text = "\n".join(L)
    if len(text) > DAILY_CASE_MAX:                       # кэп STYLE_CONTRACT: режем ХВОСТ, шапку храним
        text = text[:DAILY_CASE_MAX - 1].rstrip() + "…"
    return text


def _score_human(знач):
    """Значение критерия таблицы баллов → человеческий текст. None → честное «не измерено» (П8)."""
    if знач is None:
        return "не измерено"
    if isinstance(знач, float):
        return f"{знач:.2f}"
    return str(знач)


def _recent_outcomes(limit=10, path=None):
    """Свежие РАЗРЕШЁННЫЕ исходы из journal/outcomes.jsonl (ТОЛЬКО чтение — журнал не трогаем; П16).
    Для resolved_postmortem «Разбора дня». Сбой/нет файла → пусто (кейс возьмётся из кандидатов)."""
    p = pathlib.Path(path or (ROOT / "journal" / "outcomes.jsonl"))
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("актив") or rec.get("asset"):
                out.append(rec)
    except OSError:
        return []
    return list(reversed(out))[:limit]


def daily_case_from_protocol(proto, *, name_fn=None, outcomes=None):
    """Этап3: протокол прогона → текст «Разбора дня», ПРОВЕРЕННЫЙ линтером стиль-контракта
    (fail-closed: сломанный шаблон НЕ уходит владельцу — возвращаем None, вызывающий логирует).
    outcomes — свежие исходы (по умолчанию читаются из журнала read-only). name_fn — тикер→имя."""
    from orchestrator import daily_case as DC
    try:
        from ops import presentation_lint as PL
    except ImportError:
        import presentation_lint as PL
    if name_fn is None:
        name_fn = lambda s: _safe_name(s)
    outs = outcomes if outcomes is not None else _recent_outcomes(10)
    case = DC.select_case(proto, outcomes=outs, name_fn=name_fn)
    text = format_daily_case(case)
    viol = PL.check_daily_case(text)
    if viol:
        return None, case, viol            # fail-closed: не отправляем сломанный артефакт
    return text, case, []


# ── Flow-fix В1 (решение владельца 15.07): «Топ-3 одной строкой» + защита №1 ───────────────────
# Формат выбран владельцем (он забраковал длинный «Разбор дня» как «сложный и бесполезный»):
# три сильнейшие идеи дня по одной строке, №1 — с готовой защитой (аргумент «за» + голос скептика
# «против» из состязательного суда, поле протокола «разбор_идей»), приглашение «скажи номер».
# Стиль Forbes: без эмодзи, без жаргона (линтер стиль-контракта fail-closed). LLM-текст суда чистим
# от служебных терминов _clean_llm ДО показа (иначе редкий «каскад»/«ярус» в реплике судьи валит
# линтер и роняет пуш на эмодзи-фолбэк). Это слой ПОДАЧИ — журналы/треки/суд не трогаются.
_MONTHS_RU = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]

# Служебные термины (стоп-список стиль-контракта) в СВОБОДНОМ тексте суда/новостей → человеческий
# эквивалент. Только для ПОКАЗА; регистр игнорируем; латиница по границе слова.
_DEJARGON = [
    # «каскад»→«сценарий», «ярус»→«уровень»: нейтральные существительные — в отличие от «цепочке
    # последствий» сохраняют согласование («медвежий каскад»→«медвежий сценарий», не «…цепочке»).
    (re.compile(r"[\w-]*каскад[\w-]*", re.I), "сценарий"),
    (re.compile(r"[\w-]*ярус[\w-]*", re.I), "уровень"),
    (re.compile(r"\btier\w*\b", re.I), "уровень"),
    (re.compile(r"\bfdr\b", re.I), "контроль ложных срабатываний"),
    (re.compile(r"\bz-?score\w*\b", re.I), "отклонение от нормы"),
    (re.compile(r"\bbrier\w*\b", re.I), "точность прогнозов"),
    (re.compile(r"\bq-?value\w*\b", re.I), "значимость"),
    (re.compile(r"r²|\br2\b", re.I), "статистика связи"),
    (re.compile(r"σ", re.I), "сигма"),
    # испытатель №1 (01.11): жаргон картографа в тексте для владельца
    (re.compile(r"чок-?поинт\w*", re.I), "узкое горло"),
    (re.compile(r"\bкапекса\b", re.I), "капвложений"),
    (re.compile(r"\bкапексу\b", re.I), "капвложениям"),
    (re.compile(r"\bкапекс\w*", re.I), "капвложения"),
    # биржевой жаргон в цитатах судьи/генератора → человеческие слова, смысл сохраняем (П8)
    (re.compile(r"\bret_z_?\d*\s*=?\s*", re.I), "отклонение цены от нормы "),
    (re.compile(r"\bSMA\s?\d+(/\d+)?", re.I), "скользящих средних"),
    (re.compile(r"\bRSI\b", re.I), "индикатор перегретости RSI"),
    (re.compile(r"\blow-?vol\w*", re.I), "с низкими колебаниями"),
    (re.compile(r"\bedge\b", re.I), "преимущество"),
    (re.compile(r"равновзвешенн\w*", re.I), "с равными весами"),
]


def _trunc_words(s, n):
    """Обрезка по ГРАНИЦЕ СЛОВА (не «…from Jo»): режем до n, откатываемся к последнему пробелу."""
    s = " ".join(str(s).split())
    if len(s) <= n:
        return s
    cut = s[: n - 1]
    sp = cut.rfind(" ")
    if sp >= n // 2:                      # откат к слову, только если не съедаем половину
        cut = cut[:sp]
    return cut.rstrip(" ,;:-–—") + "…"


def _trunc_clause(s, n):
    """Обрезка по границе ФРАЗЫ (испытатель №1: «равновзвешенный уход в…» посреди мысли —
    дыра в тексте для владельца). Ищем последний конец предложения/крупный разделитель до n;
    не нашли — честная пословная обрезка."""
    s = " ".join(str(s).split())
    if len(s) <= n:
        return s
    cut = s[: n - 1]
    best = max(cut.rfind(m) for m in (". ", "; ", " — ", ": ", ", ", " → ", " and ", " и "))
    if best >= n // 3:
        return cut[:best].rstrip(" ,.;:—-") + "."
    return _trunc_words(s, n)


def _clean_llm(s, n=170):
    """Свободный текст LLM (реплика суда, заголовок новости) → безопасный для стиль-контракта:
    убираем эмодзи/декор, заменяем служебный жаргон человеческим, обрезаем по слову. П8: смысл
    сохраняем, ничего не добавляем. Пусто/не-строка → None."""
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        from ops import presentation_lint as PL
    except ImportError:
        import presentation_lint as PL
    out = "".join(ch for ch in s if ch not in set(PL._emoji_glyphs(s)))
    for rx, repl in _DEJARGON:
        out = rx.sub(repl, out)
    out = " ".join(out.split())          # схлопываем пробелы после вырезаний
    return _trunc_clause(out, n) if out.strip() else None   # по фразе, не по слову (итерация-2)


# Хвост-атрибуция агрегатора новостей: «… - Jordan News | Latest News from Jo», «… | Reuters».
# Режем всё после « | » и после « - <Источник>», если хвост похож на имя издания (латиница/News/…).
_SRC_TAIL = re.compile(
    r"\s+[|]\s+.*$"
    r"|\s+[-–—]\s+(?=[^а-яё]*$)[A-Z][\w.& ]{1,40}$",
    re.U)


def _looks_latin(s):
    """Текст преимущественно латиница (английский заголовок)? Доля латинских букв среди букв > 0.6."""
    lat = sum(1 for c in s if "a" <= c.lower() <= "z")
    cyr = sum(1 for c in s if "а" <= c.lower() <= "я")
    return (lat + cyr) > 0 and lat / (lat + cyr) > 0.6


def _clean_event(s):
    """Заголовок-повод → человекочитаемый: снять служебный хвост-источник, почистить, обрезать по
    слову. Возвращает (текст|None, английский?). Английские заголовки помечаем — на одной строке
    их лучше заменить русским механизмом (владельцу нужен читаемый русский)."""
    if not isinstance(s, str) or not s.strip():
        return None, False
    core = _SRC_TAIL.sub("", s).strip() or s.strip()
    txt = _clean_llm(core, 130)
    if not txt:
        return None, False
    return txt, _looks_latin(txt)


def _human_date(ts):
    """ISO-время прогона → «15 июля» (человеку). Сбой парса → исходная дата-строка либо None."""
    try:
        import datetime as _dt
        d = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
        return f"{d.day} {_MONTHS_RU[d.month]}"
    except Exception:
        s = str(ts or "")
        return s[:10] or None


_DIR_WORD = {"лонг": "вверх", "long": "вверх", "шорт": "вниз", "short": "вниз",
             "флэт": "вбок", "flat": "вбок"}


# A1: короткие русские метки критериев исследовательской карты (для «одной строки силы»).
_CRIT_RU = [("mechanism_real", "механизм"), ("non_obviousness", "неочевидность"),
            ("crowd_fresh", "свежесть"), ("timing_amplitude", "тайминг+ход"),
            ("asymmetry", "асимметрия"), ("falsifiability", "проверяемость")]


def _idea_read(verdict):
    """A1: вердикт РАЗБОРА идеи → структура для показа. Два источника:
      • research (разбор_идей): {оценка 0–5, band, критерии, кто_против, почему_возможность} —
        БАЛЛ СИЛЫ идеи (ранжируем, НЕ убиваем);
      • money (суд_money): {исход УСТОЯЛА/РАЗБИТА/…, балл} — денежный §11-вердикт (money_survived).
    ВАЖНО (A1, блокер stage-review wf_b7dfc21f): score — ЕДИНАЯ ось СИЛЫ идеи (research_subrubric, 0–5).
    Денежный балл §11 — это ПОРОГ ДОПУСКА к деньгам (планка 3.0), а НЕ шкала силы; его в score НЕ кладём,
    иначе §11-отклонённая идея показалась бы «Силой X/5» и могла бы лидировать. Деньги живут отдельными
    флагами money_survived/money_rejected/money_failed + money_балл.
    Возвращает: score/band/kind/money_survived/money_rejected/money_failed/money_балл/оценено/…"""
    d = {"score": None, "band": None, "kind": None, "money_survived": False,
         "money_uncontested": False, "money_rejected": False, "money_failed": False,
         "money_балл": None, "money_порог": None, "оценено": False,
         "кто_против": None, "почему": None, "критерии": {}}
    if not isinstance(verdict, dict):
        return d
    d["кто_против"] = _clean_llm(verdict.get("кто_против"), 300)
    d["почему"] = _clean_llm(verdict.get("почему_возможность"), 300)
    if "оценка" in verdict:                               # исследовательский разбор (сила идеи)
        d["kind"] = "research"
        d["score"] = verdict.get("оценка")
        d["band"] = verdict.get("band")
        d["критерии"] = verdict.get("критерии") or {}
        d["оценено"] = isinstance(verdict.get("оценка"), (int, float))
    elif "исход" in verdict:                              # денежный суд §11 (отдельная шкала — НЕ сила)
        d["kind"] = "money"
        исход = verdict.get("исход")
        d["money_survived"] = исход == "УСТОЯЛА"
        d["money_uncontested"] = исход == "УСТОЯЛА_БЕЗ_КРИТИКА"   # прошла §11, но критик невалиден (П10 не подтв.)
        d["money_rejected"] = исход in ("РАЗБИТА", "ВЕТО")
        d["money_failed"] = исход in ("ПРОПУСК", "ОШИБКА_СУДА")
        d["money_балл"] = verdict.get("балл")            # балл §11 — порог допуска, а не сила → мимо score
        d["money_порог"] = verdict.get("порог")          # порог §11 из вердикта (не хардкодим «3.0»)
        d["band"] = {"УСТОЯЛА": "прошла денежный суд",
                     "УСТОЯЛА_БЕЗ_КРИТИКА": "прошла денежный суд без ред-тима",
                     "РАЗБИТА": "разбита денежным судом",
                     "ВЕТО": "снята вето", "ПРОПУСК": "суд не смог оценить (нет котировки)",
                     "ОШИБКА_СУДА": "сбой суда"}.get(исход)
    return d


def _score5(x):
    """Балл силы идеи → «3.4/5» (шкала рубрики 0–5). None → пусто."""
    return f"{x:.1f}/5" if isinstance(x, (int, float)) else None


def _порог_txt(r, default=3.0):
    """Ярлык порога §11 «из 3.0» — из поля вердикта «порог» (money_порог), а не хардкодом (латентный
    дрейф, если владелец сдвинет планку). Порога нет в вердикте → дефолт 3.0."""
    п = r.get("money_порог")
    return f"из {п:.1f}" if isinstance(п, (int, float)) and not isinstance(п, bool) else f"из {default:.1f}"


def _crit_line(критерии):
    """Разбивка по критериям в одну строку: «механизм 4, неочевидность 4, свежесть 3…» (только
    посчитанные). Пусто → None."""
    if not isinstance(критерии, dict) or not критерии:
        return None
    parts = [f"{ru} {int(round(критерии[cid]))}" for cid, ru in _CRIT_RU
             if isinstance(критерии.get(cid), (int, float))]
    return ", ".join(parts) or None


_PHASE_RU = {"РАНО": "толпа ещё не зашла — свежо", "ВОВРЕМЯ": "толпа заходит сейчас",
             "ПОЗДНО": "уже на радаре толпы", "ЛОВУШКА": "перегрето по вниманию, пик интереса прошёл",
             "ОТЫГРАНО": "хайп сдулся"}   # stage-review 31.10 №3: датчик цены не видит — «цена уехала» убрано


def _crowd_line(brief):
    """A2: одна строка про датчик толпы (Google Trends) для показа владельцу — из посчитанного поля
    «внимание» узла (attention.py). «Не измерено» → None (не выдумываем свежесть). Плоский язык."""
    f = brief.get("внимание")
    if not isinstance(f, dict) or f.get("статус") != "ok":
        return None
    свеж, фаза = f.get("свежесть"), f.get("фаза")
    if not isinstance(свеж, (int, float)):
        return None
    хвост = _PHASE_RU.get(фаза)
    return (f"Толпа (Google Trends): свежесть {свеж * 100:.0f}% из 100"
            + (f" — {хвост}" if хвост else "") + ".")


def _top3_pool(protocol, *, name_fn=None):
    """Ранжированный пул идей дня для «Топ-3»: узлы графа (топ_k) по продуктовому рангу П2б, с
    прикреплённым вердиктом суда по символу (разбор_идей для провизорных, суд_money для money).
    Возвращает список (brief, court|None). Пусто → честный пустой день."""
    g = protocol.get("граф_отбор") or {}
    топ = list(g.get("топ_k") or [])
    защ = g.get("разбор_идей") or {}          # A1: исследовательский разбор (балл силы) провизорных
    суд = g.get("суд_money") or {}            # денежный суд §11 (не тронут)
    # Ревью #4: разбор/суд гоняются по ПОЛНОМУ ранжиру треков (route_tracks → «ранжировано»), а показ
    # берёт топ_k (top-8). Судимая идея может оказаться за пределами top-8 (когда money заполняет верх)
    # → её вердикт есть, но показать неоткуда. Добираем СУДИМЫЕ идеи, не попавшие в топ_k, из треков
    # (их брифы protocol уже несёт). Сегодня money≈0 → эффекта нет.
    seen = {b.get("актив") for b in топ if b.get("актив")}
    judged = set(защ) | set(суд)
    for b in ((g.get("money_трек") or []) + (g.get("провизорный_трек") or [])):
        a = b.get("актив")
        if a and a in judged and a not in seen:
            топ.append(b)
            seen.add(a)
    if not топ:
        return []
    from orchestrator import product_rank as PR
    ranked = PR.annotate_and_sort(топ)
    out = []
    for b in ranked:
        a = b.get("актив")
        court = защ.get(a) or суд.get(a)
        out.append((b, court if isinstance(court, dict) else None))
    return out


def _top3_selection(protocol, *, name_fn=None, max_n=3):
    """A1: единый ПОРЯДОК показа «Топ-3» (общий для текста и карты номер→тикер). РАНЖИРУЕМ, не убиваем:
      1) идея, прошедшая ДЕНЕЖНЫЙ суд §11 (money_survived) — редкий сильный сигнал, выше всех;
      2) далее — по БАЛЛУ СИЛЫ исследовательского разбора (убыв.);
      3) неоценённые — по продуктовому рангу (сохраняется как стабильный порядок при равенстве).
    Лид = верх этого порядка. Никогда не «защищать нечего» — всегда показываем лучшее из имеющегося."""
    pool = _top3_pool(protocol, name_fn=name_fn)          # уже по продуктовому рангу (убыв., стабильно)
    if not pool:
        return []

    def _key(x):
        r = _idea_read(x[1])
        return (1 if r["money_survived"] else 0,
                r["score"] if isinstance(r["score"], (int, float)) else -1.0)
    ranked = sorted(pool, key=_key, reverse=True)         # стабильно: равные — в порядке продуктового ранга
    return ranked[:max_n]


_BAND_MODE = {"сильная": "strong", "средняя": "medium", "слабая": "weak"}


def _lead_mode(top):
    """Режим подачи по СИЛЕ лида: 'money' (прошёл денежный суд §11) | 'strong'/'medium'/'weak'
    (по band исследовательского разбора) | 'money_out' (пробовалась на деньги, §11 отклонил/не смог) |
    'unjudged' (ещё не оценивали). Управляет только вводным текстом — не тем, что показываем.
    Полосу силы берём из band вердикта (единый источник — он посчитан из config в _vet_research),
    а не из захардкоженных порогов."""
    if not top:
        return "unjudged"
    r = _idea_read(top[0][1])
    if r["money_survived"]:
        return "money"
    if r["money_uncontested"]:
        return "money_uncontested"
    if r["оценено"] and r.get("band") in _BAND_MODE:
        return _BAND_MODE[r["band"]]
    if r["money_rejected"] or r["money_failed"]:
        return "money_out"
    return "unjudged"


def _oneend(s):
    """Фрагмент внутрь строки: снимаем финальную пунктуацию, чтобы не плодить «..» при склейке."""
    return (s or "").rstrip(" .!?…") or None


def _idea_label(brief, name_fn):
    a = brief.get("актив")
    nm = (name_fn or _safe_name)(a)
    return a if nm == a else f"{nm} ({a})"


def _top3_oneliner(brief, court, rank, name_fn=None):
    """A1: одна строка идеи: «N. Название (ТИКЕР) — вверх/вниз. <повод>. Сила X.X/5.» Плоский язык,
    без эмодзи/жаргона. Повод — событие (русское) или механизм цепочки; заголовок — БАЛЛ силы идеи
    (ранжируем, не убиваем). Денежный §11-балл словом «Сила» НЕ подписываем — у него отдельный ярлык
    (прошла/отклонена судом). Неоценённая → «наводка, пока без оценки». Всё из посчитанного (П8)."""
    label = _idea_label(brief, name_fn)
    dir_word = _DIR_WORD.get((brief.get("направление") or "").lower(), "движение")
    # повод: событие-новость. Английский заголовок владельцу НЕ показываем — заменяем русским
    # механизмом; если якоря нет (нечем заменить) — повод ОПУСКАЕМ (ревью #2/#10: без сырого английского).
    повод, is_en = _clean_event(brief.get("событие"))
    if is_en or not повод:
        як = brief.get("якорь")
        повод = (f"импульс по {_safe_name(як)} доходит до {(name_fn or _safe_name)(brief.get('актив'))}"
                 if як else None)
    r = _idea_read(court)
    parts = [f"{rank}. {label} — {dir_word}."]
    if brief.get("инверсия"):   # B1: пометка картографа; сторона ставки (dir_word) — из расчёта движка
        parts.append(" По оценке картографа связь контринтуитивна: толпа ждёт обратного.")
    повод = _oneend(повод)
    if повод:
        повод = повод[0].upper() + повод[1:]        # предложение с заглавной (после «вверх.»)
        parts.append(f" {повод}.")
    if r["money_survived"]:
        parts.append(" Прошла денежный суд.")
    elif r["money_uncontested"]:
        parts.append(" Прошла денежный суд §11 (без ред-тима).")
    elif r["оценено"] and r.get("band"):
        # П-4 (31.10): «Сила X/5» убрана — балл vs исход r=0.038 на 501 вердикте (шум). Оставляем слово суда.
        parts.append(f" Суд оценил как {r['band']}.")
    elif r["money_rejected"]:
        parts.append(f" Денежный суд §11 отклонил"
                     + (f" (балл {r['money_балл']:.1f} {_порог_txt(r)})"
                        if isinstance(r["money_балл"], (int, float)) else "")
                     + ".")
    elif r["money_failed"]:
        parts.append(" Денежный суд §11 не смог оценить (нет котировки).")
    else:
        parts.append(" Наводка, пока без оценки.")
    return "".join(parts)


def _defense_block(brief, court, ts, rank=None, name_fn=None):
    """A1: разбор одной идеи без эмодзи — по БАЛЛУ СИЛЫ (ранжируем, не убиваем). Показываем: балл + одна
    строка по критериям (механизм/неочевидность/свежесть/тайминг+ход/асимметрия/проверяемость), «почему
    может сработать» (взгляд суда) и «кто против / слабое место». Провизорную помечаем: связь ценами
    ещё НЕ подтверждена (герметичность provisional≠money). Возвращает list строк."""
    label = _idea_label(brief, name_fn)
    dir_word = _DIR_WORD.get((brief.get("направление") or "").lower(), "движение")
    provis = bool(brief.get("провизорный"))
    r = _idea_read(court)
    за, против = _pro_con(brief, None)     # ЧИСТЫЕ посчитанные доводы (fallback, без сырого текста судьи)
    за_calc = _trunc_words(re.sub(r"^суд:\s*", "", за[0]), 240) if за else None
    против_calc = _trunc_words(против[0], 240) if против else None
    head = (f"Идея №{rank} — {label}" if rank else f"Идея — {label}") + f" (ставка на {dir_word})."
    L = [head]
    if brief.get("инверсия"):        # B1: пометка картографа — честно, без «выигрывает» (stage-review 31.10 №4)
        L.append(f"Контринтуитивная связь (2-й порядок) по оценке картографа: толпа ждёт обратного эффекта. "
                 f"Сторона ставки — {dir_word} — посчитана движком по цепочке, а не выведена из этой пометки; "
                 f"сверка знака пометки и расчёта пока не делается.")
    if r["money_survived"]:
        L.append("Прошла даже строгий денежный суд §11 — редкий сильный сигнал.")
    elif r["money_uncontested"]:
        L.append("Прошла денежный суд §11, но критик дал невалидный ответ — состязательность (П10) "
                 "не подтверждена. Беру осторожно, не как полностью просуженную.")
    elif r["оценено"] and r.get("band"):
        # П-4 (31.10): без «Сила X/5» (балл не связан с исходами: r=0.038); словесная оценка + критерии.
        строка = f"Оценка суда: {r['band']}."
        crit = _crit_line(r["критерии"])
        if crit:
            строка += f" По критериям: {crit}."
        L.append(строка)
    elif r["money_rejected"]:
        балл = f" (балл {r['money_балл']:.1f} {_порог_txt(r)})" if isinstance(r["money_балл"], (int, float)) else ""
        L.append(f"Денежный суд §11 отклонил эту идею{балл} — в деньги не пускаю. "
                 "Это не оценка силы идеи, а порог допуска к деньгам (нужно подтверждённое историей "
                 "преимущество над рынком).")
    elif r["money_failed"]:
        L.append("Денежный суд §11 запускался, но оценить не смог (нет котировки/сбой) — вердикта нет.")
    else:
        L.append("Глубоко ещё не оценивал (быстрый скан) — это наводка, не вердикт.")
    crowd = _crowd_line(brief)                            # A2: датчик толпы (Trends), если измерен
    if crowd:
        L.append(crowd)
    # Перепроверка №3 02.11 (регрессия STOPLIST): сырой текст судьи — ЧУЖИЕ слова, идут
    # ЦИТАТОЙ «…» (жаргон суда — «лонг»/«edge» — не наша подача и не валит линтом весь Топ-3);
    # вложенные «» суда — в „…“. Посчитанные фолбэки (за_calc) — наш текст, без кавычек.
    def _ct(s):
        return "«" + str(s).replace("«", "„").replace("»", "“") + "»"
    if r["почему"] or за_calc:
        L.append("Почему может сработать: " + (_ct(r["почему"]) if r["почему"] else за_calc))
    if r["кто_против"] or против_calc:
        L.append("Кто на другой стороне / слабое место: "
                 + (_ct(r["кто_против"]) if r["кто_против"] else против_calc))
    if provis:
        # Ревью #3: суд проверяет СИЛУ идеи, а не подтверждает статистическую связь звена ценой.
        L.append("Важно: связь этого звена ценами пока НЕ подтверждена — это исследовательская идея, "
                 "не денежная ставка. В деньги не пускаю (за это отвечает отдельный денежный суд §11).")
    when = _when_line_plain(brief, ts)
    if when:
        L.append(when)
    return L


def _when_line_plain(n, ts):
    """«Когда ждать» без эмодзи (для стиль-контракта): из посчитанных лага и горизонта (П8)."""
    лаг, гор = n.get("лаг_дней"), n.get("горизонт_дней")
    parts = []
    if isinstance(лаг, (int, float)) and лаг:
        parts.append(f"исторически реакция доходила до этого звена за ~{int(round(лаг))} дн.")
    if isinstance(гор, (int, float)) and гор:
        d = _resolve_date(ts, гор)
        parts.append(f"горизонт оценки {int(гор)} дн" + (f", сверю ~{d}" if d else ""))
    return "Когда ждать: " + "; ".join(parts) + "." if parts else None


def format_top3(protocol, *, name_fn=None, max_n=3):
    """Flow-fix В1: главный дневной пуш — «Топ-3 одной строкой» + защита №1. Без эмодзи, стиль Forbes.
    Пустой день — честная строка (§15). Проверку линтером делает top3_from_protocol (fail-closed)."""
    rid = protocol.get("run_id", "?")
    ts = protocol.get("ts")
    дата = _human_date(ts)
    top = _top3_selection(protocol, name_fn=name_fn, max_n=max_n)
    поток = ((protocol.get("поток_идей") or {}).get("всего")) or len(top)
    if not top:
        return ("Идеи дня" + (f" — {дата}" if дата else "") + ".\n"
                "Что это значит для тебя. Сегодня ни одно событие в мире не дотянуло до торгуемой "
                "цепочки — честный пустой день, а не сбой. Лучше промолчать, чем выдумать идею.\n"
                "Что делать. Ничего — навязывать пустой день не буду. Загляну завтра.\n"
                f"· как я искал — /progress · тех.id {rid}")
    mode = _lead_mode(top)
    lead = _idea_read(top[0][1])
    # П-4 (31.10): «Сила X/5» из подачи убрана (балл силы vs исход r=0.038 — шум); вместо неё честная
    # база трека — доля сбывшихся идей этого типа по журналу исходов (только чтение).
    base = track_base_rate()
    база = (f" Честная база: идеи этого трека за последние месяцы сбывались примерно в "
            f"{base['hit'] * 100:.0f}% случаев — отличать хорошие от плохих система пока не умеет."
            if base.get("hit") is not None else "")
    if mode == "money":
        значит = (f"Ниже — {len(top)} идей дня по силе. №1 прошла даже строгий денежный суд §11 — "
                  f"редкий сильный сигнал; разбираю её первой. По любой другой скажи номер.")
    elif mode == "strong":
        значит = (f"Ниже — {len(top)} идей дня, отсортированы по оценке суда (механизм, неочевидность, "
                  f"свежесть, тайминг, асимметрия, проверяемость). №1 суд назвал сильной, разбираю её "
                  f"первой. По любой другой скажи номер.{база}")
    elif mode == "medium":
        значит = (f"Ниже — {len(top)} идей дня. №1 суд оценил как среднюю: есть сильные стороны и слабые, "
                  f"показываю честно и разбираю первой. По любой другой скажи номер.{база}")
    elif mode == "weak":
        значит = (f"Честно: сегодня даже лучшую идею суд оценил как слабую — но я её не прячу, "
                  f"показываю и прямо говорю, где тонко. Сильная идея = крепкий механизм + неочевидный "
                  f"бенефициар + толпа ещё не зашла; сегодня до этого недотянули. Разбираю №1.{база}")
    elif mode == "money_uncontested":
        значит = (f"Ниже — {len(top)} идей дня. №1 прошла денежный суд §11, но БЕЗ ред-тима "
                  f"(критик не отработал) — состязательность не подтверждена, показываю с оговоркой. "
                  f"Разбираю №1; по любой другой скажи номер.")
    elif mode == "money_out" and lead["money_failed"]:
        значит = (f"Ниже — {len(top)} идей дня. №1 пробовалась на деньги, но денежный суд §11 её "
                  f"НЕ смог оценить (нет котировки/сбой) — показываю честно; исследовательских идей "
                  f"сильнее сегодня не набралось. Разбираю №1; по любой другой скажи номер.")
    elif mode == "money_out":
        балл = (f" (балл {lead['money_балл']:.1f} {_порог_txt(lead)})"
                if isinstance(lead["money_балл"], (int, float)) else "")
        значит = (f"Ниже — {len(top)} идей дня. №1 пробовалась на деньги, но денежный суд §11 её "
                  f"отклонил{балл} — показываю честно, а не прячу; исследовательских идей сильнее "
                  f"сегодня не набралось. Разбираю №1; по любой другой скажи номер.")
    else:
        значит = (f"Ниже — {len(top)} идей дня. Глубоко их сегодня ещё не оценивал (быстрый скан) — "
                  f"это наводки, а не вердикты. №1 разбираю подробнее; по любой скажи номер.")
    L = ["Идеи дня" + (f" — {дата}" if дата else "") + f". {len(top)} сильнейших из {поток} за день.",
         "",
         f"Что это значит для тебя. {значит}",
         ""]
    for i, (b, c) in enumerate(top, 1):
        L.append(_top3_oneliner(b, c, i, name_fn=name_fn))
    L.append("")
    L += _defense_block(top[0][0], top[0][1], ts, rank=1, name_fn=name_fn)
    L.append("")
    L.append("Что делать. Скажи номер (1–3) — разберу любую идею подробно; или «разбери ТИКЕР» "
             "для полного состязательного разбора. Решение за тобой.")
    L.append(f"Это {RESEARCH_FRAME}.")
    L.append(f"· тех.id {rid}")
    return "\n".join(L)


def top3_from_protocol(proto, *, name_fn=None):
    """Протокол → текст «Топ-3» + мета для бота ({run_id, idx: {«1»:ТИКЕР,…}}), ПРОВЕРЕННЫЙ линтером
    стиль-контракта (fail-closed: сломанный шаблон НЕ уходит владельцу → (None, meta, нарушения))."""
    try:
        from ops import presentation_lint as PL
    except ImportError:
        import presentation_lint as PL
    text = format_top3(proto, name_fn=name_fn)
    top = _top3_selection(proto, name_fn=name_fn, max_n=3)   # тот же порядок, что в тексте
    idx = {str(i): b.get("актив") for i, (b, _c) in enumerate(top, 1) if b.get("актив")}
    meta = {"run_id": proto.get("run_id"), "idx": idx}
    viol = PL.lint_for_user(text, require_sections=[
        ("что это значит для тебя", "нет секции «что это значит для тебя»"),
        ("что делать", "нет вывода «что делать»")], max_len=PL.MAX_LEN)
    if viol:
        return None, meta, viol
    return text, meta, []


def top3_defense_card(proto, ticker, *, name_fn=None):
    """Ответ на «скажи номер»: полный (но без эмодзи) разбор выбранной идеи из ГОТОВОГО суда
    (разбор_идей/суд_money) — мгновенно, без нового прогона. Линтер fail-closed → None."""
    try:
        from ops import presentation_lint as PL
    except ImportError:
        import presentation_lint as PL
    for b, c in _top3_pool(proto, name_fn=name_fn):
        if b.get("актив") == ticker:
            L = _defense_block(b, c, proto.get("ts"), name_fn=name_fn)
            L.append("")
            L.append("Что делать. Хочешь ещё глубже — «разбери " + str(ticker) +
                     "»: прогоню полный состязательный разбор (генератор, критик, независимый судья). "
                     "Решение за тобой.")
            L.append(f"Это {RESEARCH_FRAME}.")
            text = "\n".join(L)
            viol = PL.lint_for_user(text, require_sections=[("что делать", "нет вывода «что делать»")],
                                    max_len=PL.MAX_LEN)
            return (None if viol else text), viol
    return None, ["идея не найдена в последнем прогоне"]


# ── П3: сессия партнёра (§17.6, REVISION_2026-07 §R3) ─────────────────────────────
def format_partner_session(session, metrics=None):
    """«Дай идеи» → 3-5 идей с готовой защитой и приглашением докапываться. Только посчитанное
    прогоном (П8); порядок = продуктовый ранг П2б (§R4.4, подпись 05.07); рамка §16 обязательна."""
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
        L += _rank_line(idea)                   # П2б: почему идея на этом месте (§R4.4)
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


# ── V0 «Идея дня» (карта v3, 31.10.2026; формат владельца 15.07) ──────────────────────────────────
# Одна идея, которую защищаем: событие → цепочка с отыгранностью каждого звена → «моё звено» →
# «потенциал × шанс × срок ≈ матожидание» → кто продаёт нам → суд за/против → счёт честности одной
# строкой. ≤4 числа вне цитат, 0 тех.id, тикеры с именами (линт ops/presentation_lint.lint_idea_of_day —
# блокирующий). «Честный ноль» — одной фразой. Остальные идеи — по запросу («что ещё было»).
_ORD_RU = {1: "первый", 2: "второй", 3: "третий", 4: "четвёртый", 5: "пятый", 6: "шестой", 7: "седьмой"}
_BASE_RATE_CACHE = {}
_OUTCOMES_JOURNAL = ROOT / "journal" / "outcomes.jsonl"     # ТОЛЬКО чтение (П16)


def track_base_rate(kind="cascade_provisional", path=None):
    """Честная база трека: доля сбывшихся печатей этого kind по журналу исходов (ТОЛЬКО чтение,
    П16). Это и есть «шанс» в строке потенциала: система пока не умеет отличать идеи (балл силы vs исход
    r=0.038), поэтому честный шанс любой идеи трека = его базовая частота. Кэш по mtime файла."""
    p = pathlib.Path(path or _OUTCOMES_JOURNAL)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {"hit": None, "n": 0}
    key = (str(p), kind)
    cached = _BASE_RATE_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    k = n = 0
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") == kind and rec.get("outcome") in (0, 1):
                n += 1
                k += int(rec["outcome"])
    except OSError:
        return {"hit": None, "n": 0}
    res = {"hit": (k / n) if n else None, "n": n}
    _BASE_RATE_CACHE[key] = (mtime, res)
    return res


def _played_word(frac):
    """Отыгранность узла (0..1, из посчитанного unpriced_fraction) → слова, без чисел (лимит ≤4)."""
    if not isinstance(frac, (int, float)):
        return "отыгранность не измерена"
    if frac >= 0.8:
        return "рынок уже отыграл"
    if frac >= 0.3:
        return "отыграно частично"
    return "ещё не отыграно"


def _chain_links(protocol, chain_id):
    """Все звенья цепочки chain_id, дошедшие до протокола (топ_k + треки), по порядку. Звено = запись
    трека (актив, порядок, текст узла, отыгранность). Чего нет в протоколе — того нет (честно)."""
    g = protocol.get("граф_отбор") or {}
    seen, out = set(), []
    for e in (g.get("топ_k") or []) + (g.get("money_трек") or []) + (g.get("провизорный_трек") or []):
        if e.get("цепочка") != chain_id or e.get("актив") in seen or not e.get("актив"):
            continue
        seen.add(e["актив"])
        out.append(e)
    return sorted(out, key=lambda e: (e.get("порядок") if isinstance(e.get("порядок"), int) else 99,
                                      e.get("актив")))


def _idea_label_v0(asset, name_fn):
    """«Nvidia (NVDA.US)» или честное «NVDA.US — компанию не опознал» (П8: имя не выдумываем)."""
    nm = (name_fn or _safe_name)(asset)
    if nm and nm != asset:
        return f"{nm} ({asset})"
    try:
        from ops import presentation_lint as PL
    except ImportError:
        import presentation_lint as PL
    return f"{asset} — {PL.UNKNOWN_NAME_MARK}"


def _horizon_words(days):
    if not isinstance(days, (int, float)) or not days:
        return "срок не задан"
    if days <= 6:
        return "около недели"
    if days <= 12:
        return "около двух недель"
    return "около месяца"


def _honest_zero(дата, why=None):
    why = why or "ни одно событие не дотянуло до торгуемой цепочки — пустой день, не сбой"
    return ("Идея дня" + (f" — {дата}" if дата else "") + ".\n"
            f"Что это значит для тебя. Сегодня стоящего нет: {why}.\n"
            "Что делать. Ничего. Загляну завтра.\n"
            f"Это {RESEARCH_FRAME}.")


# Перепроверка №3 02.11 (регрессия): переход на префикс токена ПОТЕРЯЛ приставочные формы,
# которые подстрока ловила («подешевела» ⊃ «дешеве», «ослабеет» ⊃ «слабе», «прирост»/«выросла»
# ⊃ «рост») — возвращены явными основами. Словарь общий для гарда лида и жанра подачи.
_SIGN_DOWN_M = ("давлени", "под вопрос", "страда", "падени", "обвал", "снижен", "сокращ",
                "убыт", "ущерб", "дешеве", "подешев", "удешев", "теря", "потер",
                "слабе", "ослаб", "обруш", "просад")
_SIGN_UP_M = ("рост", "прирост", "вырос", "выигр", "бенефициар", "усилива", "дорожа",
              "подорожа", "прибав", "укрепл", "наращива", "взлет", "взлёт")


def _marker_hit(text_l, toks, markers):
    """Перепроверка №2 02.11 (high): маркеры-слова матчатся ПРЕФИКСОМ токена («рост» не
    ловится внутри «просто»); маркеры-фразы (с пробелом) — подстрокой. Перепроверка №4:
    словесная часть — ЕДИНОЙ механикой mechanism_match._word_hit (отрицание «не/ни»/
    постфиксное «нет» в пределах клаузы), чтобы два знак-гарда не расходились."""
    phrases = [m for m in markers if " " in m]
    if any(p in text_l for p in phrases):
        return True
    words = tuple(m for m in markers if " " not in m)
    if not words:
        return False
    try:
        from ops import mechanism_match as _MM
    except ImportError:
        import mechanism_match as _MM
    return _MM._word_hit(text_l, words)


def _node_sign_conflict(node_text, dir_word):
    """Семантический знак-гард (испытатель №1): СЫРОЙ текст звена против направления расчёта.
    Конфликт — только строгий: маркеры одной стороны БЕЗ маркеров другой («ставка на рост»
    под звеном «давление на поставщиков»). Единый словарь для гарда лида и рендера."""
    t = str(node_text or "").lower()
    toks = re.findall(r"[а-яё]+", t)
    if dir_word == "вверх":
        return _marker_hit(t, toks, _SIGN_DOWN_M) and not _marker_hit(t, toks, _SIGN_UP_M)
    if dir_word == "вниз":
        return _marker_hit(t, toks, _SIGN_UP_M) and not _marker_hit(t, toks, _SIGN_DOWN_M)
    return False


def _lead_ok(brief, court):
    """Гард отбора лида (stage-review V0, блокер №1 + испытатель №1). Узел с отыгранностью ≥0.8 —
    рынок уже отыграл механизм: его edge = остаток реализованного хода с перевёрнутым знаком, а не
    прогноз; знак остатка против полного хода — тот же артефакт; текст звена против направления
    расчёта — самопротиворечие. Такое НЕ защищаем. Возвращает (ok, причина)."""
    r = _idea_read(court)
    if r["money_survived"] or r["money_uncontested"]:
        return True, None
    frac = brief.get("отыгранность_узла")
    if isinstance(frac, (int, float)) and frac >= 0.8:
        return False, "рынок уже отыграл"
    edge, full = brief.get("edge"), brief.get("полный_ход")
    if (isinstance(edge, (int, float)) and isinstance(full, (int, float)) and edge and full
            and (edge > 0) != (full > 0)):
        return False, "остаток хода против механизма (переотыграно)"
    ход = full if isinstance(full, (int, float)) and full else edge
    node = str(((brief.get("узлы_каскада") or [{}])[0]).get("узел") or "")
    if (isinstance(ход, (int, float)) and ход
            and _node_sign_conflict(node, "вверх" if ход > 0 else "вниз")):
        return False, "текст звена против направления расчёта"
    return True, None


def select_idea_day_lead(protocol, *, name_fn=None):
    """Лид «Идеи дня»: первый кандидат порядка _top3_selection, прошедший гард отыгранности.
    Возвращает ((brief, court)|None, причина_нуля|None, отклонённые[(актив, причина)])."""
    pool = _top3_selection(protocol, name_fn=name_fn, max_n=12)
    skipped = []
    for b, c in pool:
        ok, why = _lead_ok(b, c)
        if ok:
            return (b, c), None, skipped
        skipped.append((b.get("актив"), why))
    if pool:
        reasons = {w for _a, w in skipped if w}
        if reasons == {"текст звена против направления расчёта"}:
            return None, ("кандидаты противоречат сами себе (текст звена против расчёта) — "
                          "защищать нечего"), skipped
        if "текст звена против направления расчёта" in reasons:
            return None, ("всё, что дошло до выдачи, либо уже отыграно рынком, либо противоречит "
                          "само себе — защищать нечего"), skipped
        return None, "всё, что дошло до выдачи, рынок уже отыграл — защищать нечего", skipped
    return None, None, skipped


def _money_seals_recent(days=90, path=None):
    """Сколько денежных печатей (kind=cascade_money) за последние days дней — по журналу печатей
    (ТОЛЬКО чтение, П16). Для строки «счёт честности» словами."""
    p = pathlib.Path(path or (ROOT / "journal" / "predictions.jsonl"))
    if not p.exists():
        return None
    import datetime as _dt
    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).isoformat()
    n = 0
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") == "cascade_money" and str(rec.get("sealed_at") or "") >= cutoff:
                n += 1
    except OSError:
        return None
    return n


def _honesty_line(base_prov, base_money, money_recent):
    """«Счёт честности» ОДНОЙ строкой и СЛОВАМИ (лимит чисел) — из посчитанного, не литерал."""
    def word(base):
        hit, n = (base or {}).get("hit"), (base or {}).get("n") or 0
        if hit is None or n < 30:
            return "сверенных исходов пока мало"
        return "хуже монетки" if hit < 0.45 else ("лучше монетки" if hit > 0.55 else "как монетка")
    парт = [f"Исследовательские идеи сбываются {word(base_prov)}"]
    if (base_money or {}).get("n"):
        парт.append(f"денежные — {word(base_money)}")
    if money_recent is None:
        парт.append("денежный суд за последние месяцы — данных нет")
    elif money_recent == 0:
        парт.append("денежный суд за последние три месяца не пропустил ни одной идеи")
    else:
        парт.append("денежный суд за последние три месяца пропускал идеи — редко")
    return "Счёт честности. " + "; ".join(парт) + ". Подробности — /review-week."


def _chain_built_count(protocol, chain_id):
    for c in (protocol.get("каскады_в_компании") or []):
        if isinstance(c, dict) and c.get("chain_id") == chain_id:
            return c.get("узлов_построено")
    return None


def format_idea_of_day(protocol, *, name_fn=None, base_rate=None, money_base=None, money_recent=None):
    """V0: текст «Идеи дня» из ГОТОВОГО протокола. Лид — через гард отыгранности. Направление и
    величина — из ПОЛНОГО расчётного хода механизма (полный_ход), остаток (edge) — отдельно. Все
    числа — из посчитанных полей; чего нет — «нет данных». base_rate/money_base — {hit, n} треков,
    money_recent — число денежных печатей за квартал (по умолчанию из журналов, только чтение)."""
    ts = protocol.get("ts")
    дата = _human_date(ts)
    lead, why_zero, _skipped = select_idea_day_lead(protocol, name_fn=name_fn)
    if lead is None:
        return _honest_zero(дата, why_zero)
    b, court = lead
    a = b.get("актив")
    label = _idea_label_v0(a, name_fn)
    r = _idea_read(court)
    money = bool(r["money_survived"] or r["money_uncontested"])
    edge, full = b.get("edge"), b.get("полный_ход")
    ход = full if isinstance(full, (int, float)) and full else edge
    if isinstance(ход, (int, float)) and ход:
        dir_word = "вверх" if ход > 0 else "вниз"
    else:
        dir_word = _DIR_WORD.get((b.get("направление") or "").lower(), "движение")
    ставка = {"вверх": "рост", "вниз": "снижение"}.get(dir_word, "движение")
    base = base_rate if base_rate is not None else track_base_rate()
    mbase = money_base if money_base is not None else track_base_rate("cascade_money")
    mrecent = money_recent if money_recent is not None else _money_seals_recent()
    # Жанр подачи (ПОДПИСЬ владельца 01.11, вариант А по испытателю №1): «идею защищаю» — только
    # когда она прошла денежный суд ИЛИ матожидание положительно; иначе жанр «наблюдение дня» —
    # та же честность без пяти приговоров подряд.
    _p_used = (mbase.get("hit") if money and isinstance(mbase, dict)
               else (base.get("hit") if isinstance(base, dict) else None))
    # Ре-гейт V0 02.11 (блокер честности): матожидание — от НЕОТЫГРАННОГО ОСТАТКА (edge),
    # а не от полного хода механизма: уже реализованную рынком часть с сегодняшнего входа
    # не заработать (у лида с отыгранностью до 0.8 завышение доходило до кратного).
    # Полный ход остаётся для направления и строки «Ход … из них не отыграно …».
    # Перепроверка №2 02.11: edge == 0.0 — ЛЕГИТИМНОЕ «всё отыграно» (матожидание 0),
    # falsy-фолбэк на полный ход завышал бы EV ровно в худшем случае.
    _ev_move = edge if isinstance(edge, (int, float)) else ход
    # Перепроверка №4 (регрессия, money-поверхность обходит _lead_ok по дизайну): остаток
    # ПРОТИВ полного хода = переотыграно — в сторону ставки с сегодняшнего входа заработать
    # нечего; abs() терял знак и печатал перевёрнутый остаток ПОЛОЖИТЕЛЬНЫМ матожиданием.
    if (isinstance(edge, (int, float)) and isinstance(ход, (int, float)) and ход
            and edge * ход < 0):
        _ev_move = 0.0
    _ev_pct = ((2 * _p_used - 1) * abs(_ev_move) * 100
               if _p_used is not None and isinstance(_ev_move, (int, float)) else None)
    # Знак-гард (испытатель №1): СЫРОЙ текст моего звена против направления расчёта —
    # «ставка на рост» под звеном «давление на поставщиков» не защищается.
    links = _chain_links(protocol, b.get("цепочка")) if b.get("цепочка") else []
    _my_raw = ""
    for _e in links:
        if _e.get("актив") == a:
            _my_raw = str((( _e.get("узлы_каскада") or [{}])[0].get("узел") or "")).lower()
            break
    sign_conflict = _node_sign_conflict(_my_raw, dir_word)   # единый словарь с гардом лида
    # Перепроверка 02.11: денежный лид, прошедший слепой суд, — защищаемая идея при EV ≥ 0 (подпись
    # варианта А), КРОМЕ переотыгранного (остаток против хода — свежего запаса нет); при базе
    # денежного трека ровно 0.5 строгое «> 0» гасило бы каждый денежный лид. Не-денежный — EV > 0.
    _overplayed = (isinstance(edge, (int, float)) and isinstance(ход, (int, float)) and ход
                   and edge * ход < 0)
    if money:
        defend = (not _overplayed) and (_ev_pct is None or _ev_pct >= 0) and not sign_conflict
    else:
        defend = (_ev_pct is not None and _ev_pct > 0) and not sign_conflict
    наблюдение = {"рост": "возможный рост", "снижение": "возможное снижение"}.get(ставка, "возможное движение")
    if sign_conflict:
        наблюдение = "направление под вопросом"
    if defend:
        head = (f"Идея дня — {дата}. {label}: ставка на {ставка}." if дата
                else f"Идея дня. {label}: ставка на {ставка}.")
        intro = ("Что это значит для тебя. Одна идея, которую я сегодня защищаю. Остальные — по запросу: "
                 "«что ещё было»."
                 + (" Эта идея прошла денежный суд — следом придёт отдельная карточка с кнопками." if money else ""))
    else:
        head = (f"Наблюдение дня — {дата}. {label}: {наблюдение}." if дата
                else f"Наблюдение дня. {label}: {наблюдение}.")
        intro = ("Что это значит для тебя. Сегодня защищать нечего — показываю самое интересное наблюдение "
                 "и что я на нём проверяю. Остальные — по запросу: «что ещё было».")
    L = [head, "", intro, ""]
    # Повод (чужой текст → чистим: эмодзи/жаргон/хвост источника; английский → механизм по якорю)
    ev_txt, ev_en = _clean_event(b.get("событие"))
    як = b.get("якорь")
    src = "авторской карты" if b.get("источник_карты") == "авторская" else "карты картографа"
    if ev_txt and not ev_en and not ev_txt.lower().startswith("ценовой"):
        L.append(f"Повод. «{ev_txt}» — цепочка из {src}.")
    elif як:
        L.append(f"Повод. Импульс по {_idea_label_v0(як, name_fn)} — цепочка из {src}.")
    else:
        L.append("Повод. Событие-исток в протоколе не названо — честно: здесь данных нет.")
    # Цепочка целиком с отыгранностью каждого звена
    L.append("")
    L.append("Цепочка от события до бумаги и что уже отыграно:")
    # Испытатель №1: цепочка ЦЕЛИКОМ и по порядку (первый→последний), дыры — на своём месте,
    # а не сноской в конце; номера словами (линт ≤4 чисел цел).
    my_order = b.get("порядок") if isinstance(b.get("порядок"), int) else None
    my_frac = b.get("отыгранность_узла")
    nearer_unplayed, nearer_unknown = [], []
    by_order = {}
    stray = []
    for e in links:
        o = e.get("порядок")
        (by_order.setdefault(o, []) if isinstance(o, int) else stray).append(e)

    def _link_line(e, ordw):
        node_txt = _clean_llm(((e.get("узлы_каскада") or [{}])[0].get("узел") or ""), 400)
        node_txt = _trunc_clause(node_txt, 130) if node_txt else node_txt
        who = _idea_label_v0(e.get("актив"), name_fn)
        txt = f"«{node_txt}» — " if node_txt else ""
        mine = " — моё звено" if e.get("актив") == a else ""
        return f"— {ordw}: {txt}{who} — {_played_word(e.get('отыгранность_узла'))}{mine}."

    max_o = max(by_order) if by_order else 1
    for o in range(1, max_o + 1):
        ordw = _ORD_RU.get(o, "дальний") + " шаг"
        if o == 1 and o not in by_order:
            if як:
                L.append(f"— первый шаг (исток): {_idea_label_v0(як, name_fn)} — первая реакция "
                         f"рынка, здесь всё отыгрывается быстрее всего.")
            continue
        if o not in by_order:
            L.append(f"— {ordw}: до выдачи не дошёл — в цепочке пропуск, показываю его честно.")
            continue
        for e in by_order[o]:
            L.append(_link_line(e, ordw))
    for e in stray:
        L.append(_link_line(e, "звено"))
    for e in links:
        o, frac = e.get("порядок"), e.get("отыгранность_узла")
        if my_order is not None and isinstance(o, int) and o < my_order and e.get("актив") != a:
            who = _idea_label_v0(e.get("актив"), name_fn)
            if isinstance(frac, (int, float)) and frac < 0.3:
                nearer_unplayed.append(who)
            elif not isinstance(frac, (int, float)):
                nearer_unknown.append(who)
    if not links:
        L.append(f"— {label} — звено выдачи; остальные звенья цепочки до протокола не дошли (здесь данных нет).")
    built = _chain_built_count(protocol, b.get("цепочка"))
    if isinstance(built, int) and links and built > len(links):
        L.append("Часть звеньев этой цепочки в выдачу не прошла — показываю только дошедшие.")
    if my_order is not None:
        шаг = _ORD_RU.get(my_order, "дальний")
        if not isinstance(my_frac, (int, float)):
            L.append(f"Моё звено — {шаг} шаг; его отыгранность не измерена — считать его неотыгранным не могу.")
        elif my_frac >= 0.8:                       # сюда попадает только лид, прошедший денежный суд
            L.append(f"Моё звено — {шаг} шаг; рынок его уже отыграл — защищаю потому, что идея прошла "
                     f"денежный суд; проверь меня на этом.")
        elif my_frac >= 0.3:
            L.append(f"Моё звено — {шаг} шаг; рынок отыграл его частично — идея держится на остатке хода; "
                     f"проверь меня на этом.")
        elif nearer_unplayed:
            L.append(f"Моё звено — {шаг} шаг. Честно: ближе к событию есть неотыгранные звенья "
                     f"({', '.join(nearer_unplayed[:2])}) — это звено выбрано рангом, а не близостью; "
                     f"проверь меня на этом.")
        elif nearer_unknown:
            L.append(f"Моё звено — {шаг} шаг; у более близких звеньев отыгранность не измерена, поэтому "
                     f"«ближайшее неотыгранное» утверждать не могу.")
        else:
            L.append(f"Моё звено — {шаг} шаг: ближайшее из неотыгранных, что дошли до выдачи.")
    if sign_conflict:
        L.append("Противоречие внутри идеи: текст звена описывает движение в другую сторону, чем расчёт "
                 "хода, — такое не защищают. Оставляю наблюдением и помечаю для разбора.")
    # Похожий случай из библиотеки механизмов (V6, приоритет испытателя №1): прецедент с числом
    # + контрпример, числа в «…» (линт не считает). Матч слабый/библиотеки нет → строки честно нет.
    try:
        try:
            from ops import mechanism_match as MM
        except ImportError:
            import mechanism_match as MM
        _idea_txt = " ".join([str(ev_txt or "")] + [
            str(((e.get("узлы_каскада") or [{}])[0]).get("узел") or "") for e in links])
        _mm = MM.match(_idea_txt)
        # 02.11: прецедент иллюстрирует МЕХАНИЗМ («как срабатывал раньше»), направление бумаги
        # не угадывается по тексту прецедента (см. mechanism_match.similar_case_line)
        _ln = MM.similar_case_line(_mm[0]) if _mm else None
        if _ln:
            L.append("")
            L.append(_ln)
    except Exception:                                    # noqa: BLE001 — подача живёт без библиотеки
        pass
    # Потенциал × шанс × срок (до 4 чисел во всём тексте: ход, остаток, шанс, матожидание)
    L.append("")
    гор = b.get("горизонт_дней")
    hit = base.get("hit") if isinstance(base, dict) else None
    if money:
        mh = mbase.get("hit") if isinstance(mbase, dict) else None
        шанс = (f"шанс по денежному треку около {mh * 100:.0f}% (столько денежных идей сбывалось)"
                if mh is not None else "шанс по денежному треку не измерен — исходов пока нет")
    elif hit is not None:
        # Испытатель 01.11: 48% — сбываемость ПОТОКА, не оценка именно этой идеи (иначе анкеринг);
        # честность — с назначением, а не приговором («не умеет» четырежды на экран = некролог).
        шанс = (f"шанс — как у средней идеи этого конвейера, около {hit * 100:.0f}% "
                f"(это сбываемость потока, не оценка именно этой идеи — свой счёт она заработает к дате сверки)")
    else:
        шанс = "шанс не измерен — сверенных исходов этого трека пока нет"
    if isinstance(ход, (int, float)) and ход:
        # Перепроверка №4: «ещё не отыграно» — только когда остаток В СТОРОНУ хода; остаток
        # против хода = рынок прошёл больше расчётного, «не отыграно» было бы переворотом.
        остаток = ""
        if isinstance(edge, (int, float)) and edge and isinstance(full, (int, float)) and full:
            if edge * full > 0 and abs(edge) < abs(full):
                остаток = f", из них ещё не отыграно около {abs(edge) * 100:.1f}%"
            elif edge * full < 0:
                остаток = ", причём рынок уже прошёл больше расчётного хода — свежего запаса нет"
        p_used, ev_pct = _p_used, _ev_pct
        if p_used is not None:
            вывод = ((("денежный суд её пропустил, но рынок уже прошёл расчётный ход — свежего запаса "
                        "нет, карточку смотри с этим в уме") if (money and _overplayed) else
                      ("денежный суд её пропустил; по счёту денежного трека шанс пока как у монетки — "
                       "решение в отдельной карточке") if money else
                      ("сегодня это наблюдение, не ставка — проверяю механизм и сверю к дате" if not defend
                       else "на деньги не тянет, показываю как исследовательскую идею")) if ev_pct <= 0 else
                     ("положительное; решение по деньгам — в отдельной карточке" if money else
                      ("положительное, но связь ценами не подтверждена — в деньги не пускаю" if defend
                       else "положительное, но защищать идею не готов — наблюдение с проверкой к дате")))
            _has_full = isinstance(full, (int, float)) and bool(full)
            _lbl = "Ход" if _has_full else "Неотыгранный остаток"
            _dir = dir_word if not sign_conflict else f"{dir_word} по расчёту (направление спорно)"
            _tail_full = "" if _has_full else " Полный ход механизма в протоколе не посчитан — честно."
            L.append(f"Потенциал. {_lbl} около {abs(ход) * 100:.1f}% {_dir}{остаток}, {шанс}, срок "
                     f"{_horizon_words(гор)} → матожидание около {ev_pct:+.1f}% (если выигрыш и проигрыш "
                     f"одного размера): {вывод}.{_tail_full}")
        else:
            _has_full = isinstance(full, (int, float)) and bool(full)
            _lbl = "Ход" if _has_full else "Неотыгранный остаток"
            L.append(f"Потенциал. {_lbl} около {abs(ход) * 100:.1f}% {dir_word}{остаток}, срок "
                     f"{_horizon_words(гор)}; {шанс}.")
    else:
        L.append(f"Потенциал. Величина хода не посчитана — честно: здесь данных нет; {шанс}.")
    # Кто продаёт нам / суд
    L.append("")
    if r["кто_против"]:
        L.append(f"Кто продаёт нам. «{r['кто_против']}»")
    else:
        L.append("Кто продаёт нам. Суд не отвечал — идея не прошла состязательный разбор; ответа нет.")
    band, за = r.get("band"), r["почему"]
    if money:
        L.append("Суд. Прошла денежный суд" + (" (без ред-тима — состязательность не подтверждена)"
                                                if r["money_uncontested"] else "")
                 + (f". За: «{за}»" if за else "."))
    elif band or за:
        # Испытатель 01.11: ярлык «За:» на цитате судьи с отрицательным выводом путал стороны —
        # нейтральный ярлык; настоящая сортировка за/против по знаку — шаг этапа (семантический гард).
        L.append("Суд. " + (f"Оценка суда: {band}. " if band else "")
                 + (f"Мнение судьи: «{за}»" if за else "Комментария судьи нет."))
    else:
        L.append("Суд. Идею сегодня не судили — наводка, не вердикт.")
    if b.get("провизорный") and not money:
        L.append("Связь этого звена ценами не подтверждена — исследовательская идея, не денежная ставка.")
    # Счёт честности — одной строкой, словами, из посчитанного
    L.append("")
    L.append(_honesty_line(base, mbase, mrecent))
    if isinstance(гор, (int, float)) and гор:
        import math as _m
        d = _resolve_date(ts, max(1, _m.ceil(float(гор) * 7.0 / 5.0)))   # как forecast._resolve_by: торговые → календарные
        when = _human_date(d) if d else None
        if when:
            L.append(f"Сверю к {when}.")
    L.append("")
    L.append(f"Что делать. Скажи «что ещё было» — покажу остальные идеи дня; «разбери {str(a).split('.')[0]}» "
             f"— полный разбор. Решение за тобой.")
    L.append(f"Это {RESEARCH_FRAME}.")
    return "\n".join(L)


def idea_of_day_from_protocol(proto, *, name_fn=None, base_rate=None, money_base=None, money_recent=None):
    """Протокол → («Идея дня», meta, нарушения). Линт блокирующий: нарушения → (None, meta, viol) —
    бот тогда шлёт Топ-3 (тоже через линт) и заметку в дозор."""
    try:
        from ops import presentation_lint as PL
    except ImportError:
        import presentation_lint as PL
    text = format_idea_of_day(proto, name_fn=name_fn, base_rate=base_rate, money_base=money_base,
                              money_recent=money_recent)
    lead, _why, _sk = select_idea_day_lead(proto, name_fn=name_fn)
    top = _top3_selection(proto, name_fn=name_fn, max_n=3)
    idx = {str(i): b.get("актив") for i, (b, _c) in enumerate(top, 1) if b.get("актив")}
    if lead is not None and lead[0].get("актив") and lead[0].get("актив") not in idx.values():
        idx = {"1": lead[0].get("актив"), **{str(int(k) + 1): v for k, v in idx.items() if int(k) < 3}}
    meta = {"run_id": proto.get("run_id"), "idx": idx, "lead": (lead[0].get("актив") if lead else None),
            # архитектура 02.11: жанр нужен боту — «наблюдение» (защищать нечего) не уходит лидом
            # ревью 02.11: честный пустой день («Сегодня стоящего нет») — не идея, лидом не уходит
            "genre": ("наблюдение" if ("Сегодня стоящего нет" in str(text or "")
                                       or not str(text or "").startswith("Идея дня")) else "идея")}
    viol = PL.lint_idea_of_day(text)
    if viol:
        return None, meta, viol
    return text, meta, []


# ── V5 «Шансы недели» (карта v3; крон включается после ручного прогона испытателя) ────
_MECH_NAMES_CACHE = {}


def _view_human(view_id):
    """mechanism_id/thesis_id → человеческое имя взгляда (владелец не видит тех.id).
    Механизмы — поле «название» библиотеки; тезисы — T1_потоки_… → «потоки …»."""
    if not view_id:
        return ""
    vid = str(view_id)
    if not _MECH_NAMES_CACHE:
        try:
            import yaml
            p = pathlib.Path(__file__).resolve().parents[1] / "knowledge" / "mechanisms.yaml"
            for m in (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("механизмы") or []:
                if m.get("id") and m.get("название"):
                    _MECH_NAMES_CACHE[m["id"]] = m["название"]
        except Exception:                                    # noqa: BLE001 — фолбэк ниже
            pass
        _MECH_NAMES_CACHE.setdefault("", "")
    if vid in _MECH_NAMES_CACHE:
        return _MECH_NAMES_CACHE[vid]
    return re.sub(r"^T\d+_", "", vid).replace("_", " ")


def track_score_line(track):
    """Строка счёта трека из журнала (point_b.track_skill) — без зашитых констант (аудит 03.11)."""
    t = track or {}
    n = int(t.get("исходов") or 0)
    if n <= 0:
        return "Счёт честности трека: исходов пока нет — он начинается с этих вопросов, сверка в даты их сроков."
    line = f"Счёт честности трека: сверено {n}, сбылось {int(t.get('сбылось') or 0)}"
    if t.get("база_ожидание") is not None:
        line += f"; от обычной качки цен сбылось бы около {t['база_ожидание']}"
    verdict = ((t.get("умение") or {}).get("verdict"))
    line += (" — данных мало, вывода об умении пока нет." if verdict in (None, "данных мало")
             else f" — вердикт: {verdict}.")
    return line


def format_week_odds(protocol, *, name_fn=None, scores=None, track=None):
    """Протокол week_odds → человеческий пуш: вопросы с процентами и счётом взгляда.
    Возвращает (text, viol): нарушения линта → пуш не уходит (fail-closed, как Идея дня)."""
    try:
        from ops import presentation_lint as PL
    except ImportError:
        import presentation_lint as PL
    qs = (protocol or {}).get("вопросы") or []
    дата = _human_date((protocol or {}).get("ts"))
    if not qs:
        return None, ["нет принятых вопросов — пуш не нужен (честный ноль недели)"]
    L = [f"Шансы недели — {дата}." if дата else "Шансы недели.",
         "",
         "Что это значит для тебя. Проценты на события ближайших недель — зона, где прогнозы "
         "проверяемы. Каждый вопрос запечатан и будет сверен с фактом по цене закрытия: так мой "
         "взгляд на мир зарабатывает (или теряет) право на доверие.",
         ""]
    for q in qs[:8]:
        asset = q.get("asset") or ""
        имя = _asset_human(asset)[0]
        текст = str(q.get("вопрос") or "").strip().rstrip("?.")
        # Ре-гейт V0 02.11: незнакомый тикер (имя==тикер) не валит ВЕСЬ выпуск из-за линта —
        # честная пометка «компанию не опознал» рядом с тикером (как у Идеи дня), пуш живёт.
        _base = (asset.split(".")[0] if asset else "")
        # 03.11: «Constellation Energy (CEG)» — имя уже названо в вопросе; пометка «не опознал» лишняя
        _named = bool(_base and re.search(rf"[A-Za-zА-Яа-яЁё][^()]*\(\s*{re.escape(_base)}\s*\)", текст))
        _unknown = (имя == asset) and not _named
        # Ре-гейт №2 02.11: замена тикера ТОЛЬКО по границе слова — подстрочный replace
        # мог искорёжить чужой тикер-надстроку в том же вопросе (asset='A.US' внутри 'NVDA.US')
        _pat = re.compile(rf"(?<![A-Z0-9.]){re.escape(asset)}(?![A-Z0-9])") if asset else None
        if _unknown and asset:
            if _pat.search(текст):
                текст = _pat.sub(lambda _m: f"{asset} — компанию не опознал —", текст)
            else:
                текст = f"{текст} [{asset} — компанию не опознал]"
        elif asset and _pat.search(текст) and имя not in текст:
            текст = _pat.sub(lambda _m: f"{имя} ({asset})", текст)
        elif asset and asset not in текст and not (
                _base and re.search(rf"\b{re.escape(_base)}\b", текст)):
            # базовый тикер уже назван в вопросе («фонд USO») → скобку-дубль не городим
            текст = f"{текст} [{имя} ({asset})]"
        p = q.get("probability")
        строка = f"— {текст}? — думаю, около {p * 100:.0f}%." if isinstance(p, (int, float)) else f"— {текст}?"
        почему = str(q.get("почему") or "").strip()
        if почему:
            строка += f" Почему: {почему}"
        в_цене = (q.get("что_уже_в_цене") or {}).get("строка")
        if в_цене:
            строка += f" Что уже в цене: {в_цене}."
        взгляд = q.get("mechanism_id") or q.get("thesis_id")
        if взгляд:
            имя_в = _view_human(взгляд)
            key = ("mechanism:" if q.get("mechanism_id") else "thesis:") + str(взгляд)
            счёт = None
            if isinstance(scores, dict):
                try:
                    from ops import forward_score as FS
                except ImportError:
                    import forward_score as FS
                счёт = FS.score_phrase(scores.get(key))
            строка += f" (взгляд «{имя_в}»: {счёт or 'счёт взгляда копится с этих вопросов'})"
        L.append(строка)
    L += ["",
          track_score_line(track),
          "",
          "Что делать. Ничего: в срок покажу, что сбылось, и счёт каждого взгляда. Интересно глубже — "
          "«разбери ТИКЕР» по любой бумаге выше.",
          "Это исследовательский инструмент, не инвестиционная рекомендация."]
    text = "\n".join(L)
    viol = PL.lint_week_odds(text)
    return (None, viol) if viol else (text, [])
