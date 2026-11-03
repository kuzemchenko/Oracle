# -*- coding: utf-8 -*-
"""mathlib/event_study.py — проверка исторических каскадов ценами (Инв#6: числа — код, не LLM).

Зачем (просьба владельца 03.11, «набей примеры из истории и ищи подобное»): модель хорошо
рассказывает, ЧТО было в эпизоде, но плохо помнит цифры и не помнит провалов — случаев, когда похожий
повод был, а цепочка не сработала. Поэтому:
  • episode_move — ход бумаги звена вокруг даты повода по настоящим закрытиям (5/20/60 торговых дней
    от закрытия ДО повода), в долях и в «сигмах» собственной обычной качки до повода;
  • shock_events — ВСЕ резкие движения первого звена за историю (|ход за окно| ≥ k·σ·√окна, σ —
    только по дням ДО окна, без заглядывания вперёд), без перекрытий;
  • follow_stats — как часто ПОСЛЕ такого движения второе звено шло в сторону механизма сильнее
    своей обычной качки, против БАЗЫ (как часто оно ходит так само по себе в любой день).
    Ход второго звена меряется ПОСЛЕ окна шока — это ещё не отыгранная часть, на которую можно
    успеть; синхронный ход в окне шока — отдельная диагностика «уже в цене».
Значимость — точная пуассон-биномиальная против баз (mathlib/base_odds.poisson_binomial_sf).

Ограничения (честно): эпизоды выбраны моделью, знающей историю, — исторический счёт пары лишь
ОТСЕВ («история против»), а не доказательство умения; умение пара зарабатывает только форвардом
(П16). События кризисов скучены (2008, 2020) — поэтому требуем разброс по годам.
"""
import bisect
import datetime
import math

from mathlib import base_odds as BO

WINDOW = 5            # окно шока, торговых дней (как EVENT_WINDOW_DAYS боевого B4)
SHOCK_K = 2.0         # порог шока в сигмах окна
SIGMA_WINDOW = 250    # дней для σ до окна (год)
GAP = 20              # минимум торговых дней между событиями (без перекрытий)
FOLLOW_FRAC = 0.5     # «пошло следом» = ход ≥ 0.5·σ·√горизонта в сторону механизма (как гейт B4)
MIN_EVENTS = 8
MIN_YEARS = 4


def _logret(a, b):
    return math.log(b / a) if a and b and a > 0 and b > 0 else None


class _Roll:
    """Префиксные суммы логдоходностей: σ по закрытиям closes[end-window : end] за O(1)."""

    def __init__(self, closes):
        self.c = closes
        self.s1 = [0.0]
        self.s2 = [0.0]
        for a, b in zip(closes, closes[1:]):
            r = math.log(b / a)
            self.s1.append(self.s1[-1] + r)
            self.s2.append(self.s2[-1] + r * r)

    def sigma(self, end, window):
        lo = max(0, end - window)
        m = end - lo - 1                      # число доходностей внутри closes[lo:end]
        if m < 20:
            return None
        a = self.s1[end - 1] - self.s1[lo]
        b = self.s2[end - 1] - self.s2[lo]
        v = (b - a * a / m) / (m - 1)
        return math.sqrt(v) if v > 1e-18 else None


def _sigma(closes, end, window):
    """σ дневных логдоходностей по закрытиям closes[end-window : end] (только прошлое)."""
    if end <= 0:
        return None
    lo = max(0, end - window)
    return _Roll(closes[lo:end]).sigma(end - lo, window)


def clean_series(rows):
    """[(дата 'YYYY-MM-DD', close)] → отсортированные (dates, closes) без пустых/неположительных и дублей дат."""
    seen = {}
    for d, c in rows or []:
        try:
            c = float(c)
        except (TypeError, ValueError):
            continue
        if c > 0 and d and math.isfinite(c):
            seen[str(d)[:10]] = c
    ds = sorted(seen)
    return ds, [seen[d] for d in ds]


def _roll(closes_or_roll):
    return closes_or_roll if isinstance(closes_or_roll, _Roll) else _Roll(list(closes_or_roll))


def episode_move(dates, closes, trigger_date, horizons=(5, 20, 60), sigma_window=SIGMA_WINDOW):
    """Ход бумаги после повода: база — последнее закрытие СТРОГО ДО trigger_date.
    → {"база_дата", "база", "σ_дн", "ходы": {h: {"ход", "z", "дата"}}} или None (нет истории)."""
    t = str(trigger_date)[:10]
    i0 = bisect.bisect_left(dates, t) - 1
    if i0 < 0 or not closes:
        return None
    sd = _sigma(closes, i0 + 1, sigma_window)
    out = {"база_дата": dates[i0], "база": closes[i0], "σ_дн": sd, "ходы": {}}
    for h in horizons:
        j = i0 + int(h)
        if j >= len(closes):
            out["ходы"][int(h)] = None
            continue
        r = _logret(closes[i0], closes[j])
        z = (r / (sd * math.sqrt(h))) if (r is not None and sd) else None
        out["ходы"][int(h)] = {"ход": r, "z": z, "дата": dates[j]}
    return out


def shock_events(closes, window=WINDOW, k=SHOCK_K, sigma_window=SIGMA_WINDOW, gap=GAP, dates=None):
    """Все резкие движения за окно: |log(c[e]/c[e-window])| ≥ k·σ·√window, σ по дням ДО начала окна
    (закрытия closes[s-sigma_window+1 : s+1], s = e−window). Без перекрытий: следующее событие не
    раньше e+gap. → [{"e", "ход", "z", "дата"}]."""
    R = _roll(closes)
    c = R.c
    ev = []
    n = len(c)
    e = sigma_window + window
    while e < n:
        s = e - window
        sd = R.sigma(s + 1, sigma_window)
        r = _logret(c[s], c[e])
        if sd and r is not None and abs(r) >= k * sd * math.sqrt(window):
            ev.append({"e": e, "ход": r, "z": r / (sd * math.sqrt(window)),
                       "дата": dates[e] if dates else None})
            e += gap
            continue
        e += 1
    return ev


def align(a, b):
    """Пересечение по датам двух рядов (dates, closes) → (dates, ca, cb)."""
    da, ca = a
    db, cb = b
    mb = dict(zip(db, cb))
    ds, xa, xb = [], [], []
    for d, c in zip(da, ca):
        if d in mb:
            ds.append(d)
            xa.append(c)
            xb.append(mb[d])
    return ds, xa, xb


def _follow(R, e, horizon, want, sigma_window, frac):
    """Пошло ли звено в сторону want (+1/−1) за horizon дней ПОСЛЕ индекса e сильнее frac·σ·√h
    (σ — по закрытиям до e включительно). → (bool, ход) или None (нет данных)."""
    c = R.c
    j = e + horizon
    if j >= len(c):
        return None
    sd = R.sigma(e + 1, sigma_window)
    r = _logret(c[e], c[j])
    if not sd or r is None:
        return None
    return (r * want > 0 and abs(r) >= frac * sd * math.sqrt(horizon)), r


def base_rates(closes, horizon, sigma_window=SIGMA_WINDOW, frac=FOLLOW_FRAC, step=1):
    """База: доля дней, после которых бумага сама по себе ушла ≥ frac·σ·√h вверх / вниз
    (тот же критерий и та же σ, что у «пошло следом»)."""
    R = _roll(closes)
    c = R.c
    up = dn = n = 0
    for e in range(sigma_window, len(c) - horizon, step):
        sd = R.sigma(e + 1, sigma_window)
        r = _logret(c[e], c[e + horizon])
        if not sd or r is None:
            continue
        n += 1
        thr = frac * sd * math.sqrt(horizon)
        if r >= thr:
            up += 1
        elif r <= -thr:
            dn += 1
    if n == 0:
        return None
    return {"вверх": up / n, "вниз": dn / n, "n": n}


def _wilson(k, n, z=1.96):
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def follow_stats(src, dst, sign, horizon=20, window=WINDOW, k=SHOCK_K, sigma_window=SIGMA_WINDOW,
                 gap=GAP, frac=FOLLOW_FRAC, min_events=MIN_EVENTS, min_years=MIN_YEARS, src_dir=None):
    """Пара «звено src → звено dst» с механизмом sign (+1: dst идёт туда же, куда src; −1: обратно).
    src/dst — (dates, closes). src_dir (+1/−1) — считать только шоки src в эту сторону (механизм
    эпизода направленный: «нефть ВВЕРХ → танкеры вверх» не то же, что «нефть вниз → танкеры вниз»);
    None — шоки в обе стороны. → словарь счёта с вердиктом:
      «мало данных»   — событий < min_events или лет с событиями < min_years;
      «история против» — после шоков dst шло в сторону механизма НЕ чаще базы;
      «намёк»          — чаще базы, но незначимо (p > 0.05);
      «подтверждена»   — чаще базы, p ≤ 0.05 (до поправки на множественность — её делает вызывающий BH).
    """
    sign = 1 if sign >= 0 else -1
    ds, ca, cb = align(src, dst)
    res = {"горизонт": horizon, "знак": sign, "шоки": {1: "вверх", -1: "вниз"}.get(src_dir, "обе стороны"),
           "общих_дней": len(ds),
           "с": ds[0] if ds else None, "по": ds[-1] if ds else None}
    if len(ds) < sigma_window + window + horizon + 30:
        return {**res, "событий": 0, "вердикт": "мало данных", "причина": "короткая общая история"}
    # события не перекрываются ни окнами шока, ни окнами отклика: иначе соседние исходы зависимы и
    # значимость завышена (длинный горизонт при коротком зазоре — классическая ловушка событийных исследований)
    ev = shock_events(ca, window=window, k=k, sigma_window=sigma_window, gap=max(gap, horizon + window), dates=ds)
    if src_dir in (1, -1):
        ev = [x for x in ev if x["ход"] * src_dir > 0]
    RB = _Roll(cb)
    br = base_rates(RB, horizon, sigma_window=sigma_window, frac=frac)
    hits, probs, years, rows, sync = 0, [], set(), [], 0
    for x in ev:
        want = sign * (1 if x["ход"] > 0 else -1)
        f = _follow(RB, x["e"], horizon, want, sigma_window, frac)
        if f is None or br is None:
            continue
        p0 = br["вверх"] if want > 0 else br["вниз"]
        probs.append(p0)
        hits += int(f[0])
        years.add(ds[x["e"]][:4])
        # диагностика «уже в цене»: dst в окне шока уже ушло в сторону механизма
        rs = _logret(cb[x["e"] - window], cb[x["e"]])
        if rs is not None and rs * want > 0:
            sync += 1
        rows.append({"дата": ds[x["e"]], "шок_z": round(x["z"], 2), "ход_dst": round(f[1], 4),
                     "пошло": bool(f[0])})
    n = len(probs)
    exp = sum(probs)
    res.update({"событий": n, "лет": len(years), "пошло": hits,
                "доля": round(hits / n, 4) if n else None, "доля_ci95": _wilson(hits, n),
                "база": round(exp / n, 4) if n else None, "ожидание_базы": round(exp, 2),
                "синхронно_в_окне": sync, "события": rows})
    if n < min_events or len(years) < min_years:
        return {**res, "p": None, "вердикт": "мало данных",
                "причина": f"событий {n} (нужно ≥{min_events}), лет {len(years)} (нужно ≥{min_years})"}
    p = BO.poisson_binomial_sf(hits, probs)
    res["p"] = round(p, 5) if p is not None else None
    if hits <= exp:
        res["вердикт"] = "история против"
    elif p is not None and p <= 0.05:
        res["вердикт"] = "подтверждена"
    else:
        res["вердикт"] = "намёк"
    return res


def corr_sign(src, dst):
    """Знак синхронной корреляции дневных логдоходностей (как увидит форвард-тест B4 через β).
    → (+1|−1|0, ρ, n)."""
    ds, ca, cb = align(src, dst)
    ra = [_logret(a, b) for a, b in zip(ca, ca[1:])]
    rb = [_logret(a, b) for a, b in zip(cb, cb[1:])]
    pr = [(x, y) for x, y in zip(ra, rb) if x is not None and y is not None]
    n = len(pr)
    if n < 60:
        return 0, None, n
    mx = sum(x for x, _ in pr) / n
    my = sum(y for _, y in pr) / n
    sxy = sum((x - mx) * (y - my) for x, y in pr)
    sxx = sum((x - mx) ** 2 for x, _ in pr)
    syy = sum((y - my) ** 2 for _, y in pr)
    if sxx <= 0 or syy <= 0:
        return 0, None, n
    rho = sxy / math.sqrt(sxx * syy)
    return (1 if rho > 0 else -1 if rho < 0 else 0), round(rho, 4), n


def horizon_bucket(lag_days):
    """Заранее объявленный горизонт проверки из оценки лага звена (без подгонки под результат)."""
    lag = max(0, int(lag_days or 0))
    if lag <= 7:
        return 5
    if lag <= 30:
        return 20
    return 60


def iso_to_unix(d):
    return int(datetime.datetime.fromisoformat(str(d)[:10]).replace(tzinfo=datetime.timezone.utc).timestamp())
