# -*- coding: utf-8 -*-
"""mathlib/calibration/dataquality.py — точечный детектор БИТЫХ ТИКОВ (аудит Нед.4, п.1).

НЕ winsorize (решение пользователя): не сглаживаем хвосты, а точечно ловим одиночный
ошибочный принт по трём ОДНОВРЕМЕННЫМ признакам:
  1) однодневный выброс: |дневная log-доходность| ≥ spike;
  2) полный откат на следующий день: цена возвращается к уровню ДО выброса
     (|log(C[t+1]/C[t-1])| ≤ roundtrip);
  3) идиосинкразия: в РОДСТВЕННЫХ инструментах в ТОТ ЖЕ день нет аномалии
     (max|доходность пиров| < peer_calm) — настоящее рыночное событие двигало бы и пиров.

Без пиров инструмент НЕ флагуется (консервативно: идиосинкразию подтвердить нечем).
Сплиты этим детектором НЕ ловятся (у них нет отката) — для них adjusted_close (loader).
Каждый пойманный тик возвращается с числовыми причинами для журналируемого исключения.
"""
import numpy as np


def _logret(close):
    c = np.asarray(close, float)
    lr = np.full(c.size, np.nan)
    pos = c > 0
    lr[1:] = np.where(pos[1:] & pos[:-1], np.log(np.where(pos, c, 1.0))[1:] - np.log(np.where(pos, c, 1.0))[:-1], np.nan)
    return lr


def detect_bad_ticks(aligned_map, peer_map, spike=0.15, roundtrip=0.06, peer_calm=0.08):
    """Найти битые тики по выровненным рядам (одинаковые даты у всех инструментов).

    aligned_map : {symbol: loader.Series}, синхронные даты (loader.load_aligned + adjusted_view)
    peer_map    : {symbol: [peer_symbol, ...]}
    Возвращает {symbol: [ {date, ret, next_ret, roundtrip_logret, peer_max_abs_ret, peers, reason} ]}.
    """
    lr = {s: _logret(ser.close) for s, ser in aligned_map.items()}
    logc = {s: np.log(np.where(ser.close > 0, ser.close, np.nan)) for s, ser in aligned_map.items()}
    out = {}
    for sym, ser in aligned_map.items():
        dates = ser.dates
        n = ser.close.size
        peers = [p for p in peer_map.get(sym, []) if p in aligned_map]
        flagged = []
        if not peers:
            out[sym] = flagged
            continue
        r = lr[sym]
        for t in range(1, n - 1):
            if not np.isfinite(r[t]) or abs(r[t]) < spike:
                continue
            if not (np.isfinite(logc[sym][t + 1]) and np.isfinite(logc[sym][t - 1])):
                continue
            rtrip = logc[sym][t + 1] - logc[sym][t - 1]
            if abs(rtrip) > roundtrip:                      # отката нет → не битый тик (реальный ход/сплит)
                continue
            peer_rets = [abs(lr[p][t]) for p in peers if np.isfinite(lr[p][t])]
            if not peer_rets:                               # нет данных пиров в этот день → не флагуем
                continue
            pmax = max(peer_rets)
            if pmax >= peer_calm:                           # пиры тоже двигались → рыночное событие
                continue
            flagged.append({
                "date": str(dates[t]),
                "ret": round(float(r[t]), 4),
                "next_ret": round(float(r[t + 1]), 4) if np.isfinite(r[t + 1]) else None,
                "roundtrip_logret": round(float(rtrip), 4),
                "peer_max_abs_ret": round(float(pmax), 4),
                "peers": peers,
                "reason": (f"однодневный выброс {r[t]*100:+.1f}% с полным откатом "
                           f"(round-trip {rtrip*100:+.1f}%) при спокойных пирах "
                           f"(max|пир|={pmax*100:.1f}% < {peer_calm*100:.0f}%) → битый тик"),
            })
        out[sym] = flagged
    return out


def flagged_dates(bad_ticks):
    """{symbol: set(дат битых тиков)} — для исключения из каталога движений."""
    return {s: {x["date"] for x in lst} for s, lst in bad_ticks.items()}
