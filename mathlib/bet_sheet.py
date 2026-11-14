# -*- coding: utf-8 -*-
"""mathlib/bet_sheet.py — арифметика «листа ставки» (ревью 14.11: Fable п.4, Codex «расчёт экономики кодом»).

Вместо «доказан ли перевес над рынком» — «грамотно ли построена ставка»: что получим и потеряем в долларах
при каждом сценарии, при каком шансе хорошего исхода ставка в плюс, и (если шансы названы с основанием) какое
ожидание. Лучший ответ агента за всю историю (BRNT 09.11: «ставка в плюс, если шанс эскалации выше 36%»)
считался в голове модели — здесь то же считает код (Инв#6). Цены сценариев и шансы — входы с основанием
от вызывающего; код их не выдумывает и не подбирает.
"""

MIN_SCEN, MAX_SCEN = 2, 5


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def sheet(entry_price, scenarios, *, size_usd=None, qty=None, fees_usd=0.0, direction="long"):
    """entry_price — цена входа; scenarios — [{"name", "price", "probability"?}] (2–5 штук);
    размер — size_usd ИЛИ qty; fees_usd — комиссии туда-обратно; direction long|short.
    → {"ok", "позиция", "сценарии": [{name, price, pnl_usd, pnl_pct}], "порог_шанса", "ожидание_usd"?, "причина"?}."""
    px = _f(entry_price)
    if not px or px <= 0:
        return {"ok": False, "причина": "нет цены входа"}
    if not isinstance(scenarios, (list, tuple)) or not MIN_SCEN <= len(scenarios) <= MAX_SCEN:
        return {"ok": False, "причина": f"нужно от {MIN_SCEN} до {MAX_SCEN} сценариев с ценой"}
    if direction not in ("long", "short"):
        return {"ok": False, "причина": "направление — long или short"}
    q = _f(qty)
    if q is None:
        s = _f(size_usd)
        if not s or s <= 0:
            return {"ok": False, "причина": "размер — сумма в долларах или количество бумаг"}
        q = s / px
    if q <= 0:
        return {"ok": False, "причина": "количество должно быть положительным"}
    fees = _f(fees_usd) or 0.0
    sign = 1 if direction == "long" else -1
    rows, probs = [], []
    for sc in scenarios:
        p = _f((sc or {}).get("price"))
        if p is None or p < 0:
            return {"ok": False, "причина": f"у сценария «{(sc or {}).get('name')}» нет цены"}
        pnl = sign * (p - px) * q - fees
        rows.append({"name": str(sc.get("name") or f"сценарий {len(rows) + 1}")[:80], "price": p,
                     "pnl_usd": round(pnl, 2), "pnl_pct": round(pnl / (px * q) * 100, 2)})
        probs.append(_f(sc.get("probability")))
    best = max(rows, key=lambda r: r["pnl_usd"])
    worst = min(rows, key=lambda r: r["pnl_usd"])
    out = {"ok": True, "позиция": {"цена_входа": px, "кол_во": round(q, 4), "вложено_usd": round(px * q, 2),
                                   "направление": direction, "комиссии_usd": fees},
           "сценарии": rows, "лучший": best["name"], "худший": worst["name"]}
    # порог шанса: при каком шансе лучшего исхода (против худшего) ставка не в минусе — p* = |худший|/(лучший+|худший|)
    if best["pnl_usd"] > 0 > worst["pnl_usd"]:
        out["порог_шанса"] = round(-worst["pnl_usd"] / (best["pnl_usd"] - worst["pnl_usd"]), 4)
        out["порог_словами"] = (f"ставка не в минусе, если шанс исхода «{best['name']}» выше "
                                f"{out['порог_шанса'] * 100:.0f}% (против исхода «{worst['name']}»)")
    elif worst["pnl_usd"] >= 0:
        out["порог_словами"] = "во всех сценариях не в минусе — проверь, не забыт ли плохой сценарий"
    else:
        out["порог_словами"] = "ни в одном сценарии нет выигрыша — ставка не имеет смысла"
    if all(p is not None for p in probs):
        tot = sum(probs)
        if any(p < 0 for p in probs) or abs(tot - 1.0) > 0.02:
            out["причина_без_ожидания"] = f"шансы сценариев в сумме {tot:.2f}, а должны давать 1 — ожидание не считаю"
        else:
            ev = sum(p * r["pnl_usd"] for p, r in zip(probs, rows))
            out["ожидание_usd"] = round(ev, 2)
            out["ожидание_pct"] = round(ev / (px * q) * 100, 2)
    return out
