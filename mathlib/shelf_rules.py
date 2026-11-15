# -*- coding: utf-8 -*-
"""mathlib/shelf_rules.py — статус бумаги на полке длинных тем (Q-полка-длинных-тем; ревью 14.11 Fable п.3). Инв#6.

Входы — посчитанные кодом числа (просадка от годового максимума, перцентиль оценки против своей истории, пересмотр
прогноза прибыли за 90 дней); правила — из knowledge/long_themes.yaml. Порядок важен:
  1) «тезис под вопросом» — аналитики резко режут прогноз (это проверка тезиса, она главнее цены);
  2) «зона входа» — просадка от максимума ≥ порога и прогнозы не режут сильнее нормы;
  3) «дорого» — оценка выше, чем почти всё время за 5 лет;
  4) иначе «ждать».
Нет числа → правило, которому оно нужно, не срабатывает, а в причинах честно пишется «нет данных».
"""
from datetime import date

STATUSES = ("тезис под вопросом", "зона входа", "дорого", "ждать")


def drawdown(closes):
    """closes — [(дата, цена)] за ~год, по возрастанию даты. → {максимум, дата_максимума, сейчас, просадка_проц} или None."""
    rows = [(d, float(c)) for d, c in closes or [] if c is not None]
    if len(rows) < 20:
        return None
    hd, hi = max(rows, key=lambda x: x[1])
    d, now = rows[-1]
    return {"максимум": round(hi, 2), "дата_максимума": hd, "сейчас": round(now, 2), "дата": d,
            "просадка_проц": round((now / hi - 1) * 100, 2)}


def status(dd_pct=None, val_pct=None, revision=None, rules=None):
    """dd_pct — просадка от максимума в % (≤0), val_pct — перцентиль оценки 0..1, revision — пересмотр прогноза
    прибыли следующего года за 90 дней (доля). → (статус, [причины словами])."""
    r = {"просадка_входа_проц": 20, "дорого_перцентиль": 0.9, "пересмотр_тревога": -0.10, "пересмотр_норма": -0.05}
    r.update(rules or {})
    why = []
    if revision is not None and revision <= r["пересмотр_тревога"]:
        why.append(f"аналитики за 90 дней срезали прогноз прибыли на следующий год на {abs(revision) * 100:.0f}%")
        return "тезис под вопросом", why
    if dd_pct is not None and dd_pct <= -abs(r["просадка_входа_проц"]):
        if revision is None:
            why.append(f"цена на {abs(dd_pct):.0f}% ниже годового максимума; пересмотров прогнозов нет в данных")
            return "зона входа", why
        if revision >= r["пересмотр_норма"]:
            why.append(f"цена на {abs(dd_pct):.0f}% ниже годового максимума, а прогнозы аналитиков не режут")
            return "зона входа", why
        why.append(f"цена на {abs(dd_pct):.0f}% ниже максимума, но прогнозы срезаны на {abs(revision) * 100:.0f}%")
        return "ждать", why
    if val_pct is not None and val_pct >= r["дорого_перцентиль"]:
        why.append(f"оценка выше, чем в {round(val_pct * 100)}% кварталов за 5 лет")
        return "дорого", why
    if dd_pct is not None:
        why.append(f"цена на {abs(dd_pct):.0f}% ниже годового максимума — до зоны входа "
                   f"({abs(r['просадка_входа_проц']):.0f}%) не дошла" if dd_pct < 0 else "цена у годового максимума")
    else:
        why.append("нет данных о годовом максимуме")
    return "ждать", why


def next_review(earnings_date, today):
    """Пересмотр тезиса — через 2 дня после ближайшего отчёта; нет даты — через 13 недель. → ISO-дата."""
    t = date.fromisoformat(str(today)[:10])
    try:
        e = date.fromisoformat(str(earnings_date)[:10])
        if e >= t:
            return date.fromordinal(e.toordinal() + 2).isoformat()
    except (TypeError, ValueError):
        pass
    return date.fromordinal(t.toordinal() + 91).isoformat()
