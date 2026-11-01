# -*- coding: utf-8 -*-
"""orchestrator/mechanisms.py — V6 (карта v3): загрузчик и детерминированный валидатор
библиотеки механизмов knowledge/mechanisms.yaml.

Зачем: библиотека собрана LLM-воркфлоу и помечена ЧЕРНОВИК — прежде чем ей кормить
V5/V6, каждый механизм проверяется КОДОМ (Инв#6): структура, прецеденты с датой и
источником, числа в «observed», П16-чистота форвард-счёта (репутация рождается
ТОЛЬКО из журнала, не из файла), фальсифицируемость (есть «НЕ СРАБАТЫВАЕТ» и
контрпримеры), зона лагов против зоны охоты 3–13 недель.

ОШИБКА = механизм в бой не идёт; ПРЕДУПРЕЖДЕНИЕ = идёт, но слабость названа.
Валидатор ничего не чинит и не переписывает — отчёт для машиниста/испытателя.

CLI: python3 -m orchestrator.mechanisms [--path ...] [--out reports/...md]
Код выхода 1 при ошибках (пригодно для крона/CI).
"""
import argparse
import datetime
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_PATH = ROOT / "knowledge" / "mechanisms.yaml"

REQUIRED = ("id", "rule", "theory", "trigger_class", "chain", "conditions", "lag_weeks",
            "precedents", "watch_assets", "tradeable", "amplitude", "counterexamples",
            "forward_score")
ID_RE = re.compile(r"^[a-z0-9_]{3,64}$")
URL_RE = re.compile(r"https?://[^\s;,]+\.[^\s;,]+")   # ≥1 URL в строке (их может быть несколько через « ; »)
YEAR_RE = re.compile(r"(1[89]\d{2}|20\d{2})")
RANGE_RE = re.compile(r"(\d+)\s*[–\-—]\s*(\d+)")
DIR_MARKERS = ("↑", "↓", "вверх", "вниз", "раст", "пада", "выше", "ниже", "ослаб", "укреп",
               "сжим", "расшир", "дорожа", "дешеве")
HUNT_LO, HUNT_HI = 3, 13          # зона охоты Тетлока (нед), как в week_odds
PRICE_CHECK_YEAR = 2000           # прецеденты моложе — кандидаты на проверку ценами сервера


def load(path=None):
    p = pathlib.Path(path) if path else DEFAULT_PATH
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return doc.get("механизмы") or []


def lag_ranges(lag_weeks):
    """'1-й ход 0–4 (пик 2–8); 2-й ход 2–12' → [(0,4),(2,8),(2,12)]. Детерминированно."""
    s = str(lag_weeks or "")
    out = [(int(a), int(b)) for a, b in RANGE_RE.findall(s)]
    if not out:
        singles = [int(x) for x in re.findall(r"\d+", s)]
        out = [(x, x) for x in singles]
    return [(a, b) if a <= b else (b, a) for a, b in out]


def in_hunt_zone(lag_weeks):
    return any(a <= HUNT_HI and b >= HUNT_LO for a, b in lag_ranges(lag_weeks))


def _years(precedent):
    return [int(y) for y in YEAR_RE.findall(str(precedent.get("year", "")) +
                                            " " + str(precedent.get("event", "")))]


def validate(mechanisms):
    """→ {'механизмов', 'ошибки': [...], 'предупреждения': [...], 'в_бой', 'сводка'}.

    Честно и детерминированно; сеть не трогается (URL проверяется только по форме)."""
    errors, warns = [], []
    seen_ids = set()
    ok_ids, hunt_n, price_checkable = [], 0, 0
    for i, m in enumerate(mechanisms):
        mid = str(m.get("id") or f"<без id #{i}>")
        e0 = len(errors)

        if not ID_RE.match(str(m.get("id") or "")):
            errors.append(f"{mid}: id не snake_case ASCII 3–64")
        if mid in seen_ids:
            errors.append(f"{mid}: дубль id")
        seen_ids.add(mid)

        for f in REQUIRED:
            v = m.get(f)
            if v is None or (isinstance(v, (str, list, dict)) and not v):
                errors.append(f"{mid}: поле «{f}» пусто/отсутствует")

        # прецеденты: дата + событие + наблюдение + источник-URL (П8);
        # у механизма должен быть ≥1 прецедент с ЧИСЛОМ в observed (амплитуда),
        # качественные прецеденты-нюансы рядом с ним легитимны (предупреждение)
        precs = m.get("precedents") or []
        if isinstance(precs, list):
            if len(precs) == 1:
                warns.append(f"{mid}: один прецедент — правило на одной точке")
            numeric_any = False
            for j, p in enumerate(precs):
                tag = f"{mid}.прецедент[{j}]"
                if not isinstance(p, dict):
                    errors.append(f"{tag}: не словарь")
                    continue
                if not _years(p):
                    errors.append(f"{tag}: нет распознаваемого года")
                if not str(p.get("event") or "").strip():
                    errors.append(f"{tag}: пустое событие")
                obs = str(p.get("observed") or "")
                if re.search(r"\d", obs):
                    numeric_any = True
                else:
                    warns.append(f"{tag}: «observed» качественное, без числа")
                src = str(p.get("source") or "")
                if not URL_RE.search(src):
                    errors.append(f"{tag}: в источнике нет ни одного URL: «{src[:60]}»")
            if precs and not numeric_any:
                errors.append(f"{mid}: ни одного прецедента с числовой амплитудой в «observed»")
            if any(y >= PRICE_CHECK_YEAR for p in precs if isinstance(p, dict)
                   for y in _years(p)):
                price_checkable += 1

        # П16: форвард-репутация рождается ТОЛЬКО из журнала исходов
        fs = m.get("forward_score") or {}
        if isinstance(fs, dict):
            q = fs.get("вопросов", 0)
            o = fs.get("исходов", 0)
            h = fs.get("попаданий", 0)
            if not all(isinstance(x, int) and x >= 0 for x in (q, o, h)):
                errors.append(f"{mid}: forward_score не целые ≥0")
            elif not (h <= o <= q):
                errors.append(f"{mid}: forward_score несогласован (попаданий≤исходов≤вопросов)")
            elif (q, o, h) != (0, 0, 0):
                warns.append(f"{mid}: forward_score ненулевой В ФАЙЛЕ — репутация должна "
                             f"пересчитываться из journal/, не рукой (П16)")
        else:
            errors.append(f"{mid}: forward_score не словарь")

        # лаги и зона охоты
        if not lag_ranges(m.get("lag_weeks")):
            errors.append(f"{mid}: lag_weeks без чисел")
        else:
            lo = min(a for a, _ in lag_ranges(m.get("lag_weeks")))
            hi = max(b for _, b in lag_ranges(m.get("lag_weeks")))
            if hi > 160:
                warns.append(f"{mid}: лаг до {hi} нед — за горизонтом продукта")
            if in_hunt_zone(m.get("lag_weeks")):
                hunt_n += 1
            else:
                warns.append(f"{mid}: ни один ход не пересекает зону {HUNT_LO}–{HUNT_HI} нед")

        # фальсифицируемость: «НЕ СРАБАТЫВАЕТ»/«ЗАТУХАЕТ» в условиях + направления в цепочке
        conds = " ".join(str(c) for c in (m.get("conditions") or [])).upper().replace("Ё", "Е")
        if not any(k in conds for k in ("НЕ СРАБАТЫВАЕТ", "ЗАТУХА", "НЕ РАБОТАЕТ", "ЛОМАЕТСЯ")):
            warns.append(f"{mid}: в условиях нет «НЕ СРАБАТЫВАЕТ»/«ЗАТУХАЕТ» — "
                         f"правило нефальсифицируемо")
        chain = m.get("chain") or []
        if isinstance(chain, list) and len(chain) < 2:
            warns.append(f"{mid}: цепочка короче 2 ходов")
        for step in chain if isinstance(chain, list) else []:
            if not any(d in str(step).lower() for d in DIR_MARKERS):
                warns.append(f"{mid}: ход без направления: «{str(step)[:50]}…»")
                break

        if m.get("tradeable") is True and not str(m.get("watch_assets") or "").strip():
            errors.append(f"{mid}: tradeable без watch_assets")

        if len(errors) == e0:
            ok_ids.append(mid)

    return {
        "механизмов": len(mechanisms),
        "в_бой": len(ok_ids),
        "ошибки": errors,
        "предупреждения": warns,
        "сводка": {
            "торгуемых": sum(1 for m in mechanisms if m.get("tradeable") is True),
            "в_зоне_3_13_нед": hunt_n,
            "проверяемы_ценами_сервера": price_checkable,
        },
    }


def report_md(rep, path_label="knowledge/mechanisms.yaml"):
    s = rep["сводка"]
    lines = [
        f"# Валидация библиотеки механизмов — {datetime.date.today().isoformat()}",
        "",
        f"Файл: `{path_label}` · механизмов: {rep['механизмов']} · пригодны в бой: "
        f"{rep['в_бой']} · торгуемых: {s['торгуемых']} · в зоне {HUNT_LO}–{HUNT_HI} нед: "
        f"{s['в_зоне_3_13_нед']} · прецеденты проверяемы ценами сервера (с {PRICE_CHECK_YEAR}+): "
        f"{s['проверяемы_ценами_сервера']}",
        "",
        f"## Ошибки ({len(rep['ошибки'])}) — эти механизмы в бой НЕ идут",
    ]
    lines += [f"- {e}" for e in rep["ошибки"]] or ["- нет"]
    lines += ["", f"## Предупреждения ({len(rep['предупреждения'])})"]
    lines += [f"- {w}" for w in rep["предупреждения"]] or ["- нет"]
    lines += ["", "_Валидатор детерминированный (orchestrator/mechanisms.py); сеть не "
                  "использовалась — URL проверены по форме. Форвард-репутация механизмов "
                  "считается из журнала, файл её только кэширует._"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="V6: валидация knowledge/mechanisms.yaml кодом")
    ap.add_argument("--path", default=str(DEFAULT_PATH))
    ap.add_argument("--out", default=None, help="куда положить md-отчёт (опц.)")
    a = ap.parse_args(argv)
    rep = validate(load(a.path))
    print(f"механизмов {rep['механизмов']} · в бой {rep['в_бой']} · "
          f"ошибок {len(rep['ошибки'])} · предупреждений {len(rep['предупреждения'])} · "
          f"{rep['сводка']}")
    for e in rep["ошибки"]:
        print("  ОШИБКА:", e)
    for w in rep["предупреждения"][:20]:
        print("  предупр.:", w)
    if a.out:
        out = pathlib.Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report_md(rep, path_label=a.path), encoding="utf-8")
        print("→", a.out)
    return 1 if rep["ошибки"] else 0


if __name__ == "__main__":
    sys.exit(main())
