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
    """Список карточек идей (отчёты этапа 6) из протокола. Пусто — слабый день §6."""
    synth = (protocol or {}).get("этап6_синтез") or {}
    return synth.get("отчёты") or []


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
_ASSET = {
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
}


def _asset_human(ticker):
    """(имя, описание) для тикера. Фолбек — сам тикер (честно, без выдумок)."""
    if ticker in _ASSET:
        return _ASSET[ticker]
    return (ticker, f"торгуемый инструмент {ticker}")


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
             "📋 Полный разбор (13 полей §8):"]

    show_help_all = (pres["mode"] == "long")
    for n in range(1, 14):
        lines.append("")
        lines.append(f"{n}. {_FIELD_TITLES[n]}")
        if show_help_all or n in pres["still_unclear"]:
            lines.append(f"   ℹ️ {field_help[n]}")
        lines.append(f"   → {_field_content(n, fields, idea_d, pos)}")

    lines += ["", "⚖️ «Принять» откроется через 24 ч (пауза §12).",
              f"· тех.id {protocol.get('run_id','?')}"]
    text = "\n".join(lines)
    if len(text) > MAX_MSG:
        text = text[:MAX_MSG].rstrip() + "\n…(полный отчёт — в журнале funnel_logs)"
    return text


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


def format_weak_day(protocol):
    """Пуш «идей нет» человеческим языком (это нормальный, частый и полезный результат)."""
    theme = protocol.get("theme") or "—"
    tname = _asset_human(theme.upper() + ".US")[0] if "." not in theme else theme
    return ("🟦 Идей нет — и это нормально.\n\n"
            f"Сегодня по теме «{theme}» система просмотрела варианты, но ни один не прошёл "
            "проверку. Это частый и полезный результат: лучшее решение в большинстве дней — "
            "НЕ сделать плохую ставку. Система намеренно молчит, когда нет чего-то стоящего.\n\n"
            f"· тех.id {protocol.get('run_id','?')}")


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
