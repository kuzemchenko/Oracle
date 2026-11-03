# -*- coding: utf-8 -*-
"""ops/analogs.py — «память аналогий»: похожие исторические каскады для охотника (просьба владельца 03.11).

Охотник перед поиском спрашивает: «что в истории было похоже на сегодняшний повод?» — и получает 3–8
эпизодов из knowledge/cascade_history.yaml: повод, волны по порядкам с РОЛЯМИ (кто сегодня играет ту же
роль), ход цены каждого звена, посчитанный КОДОМ, провальный аналог и правило переноса. Похожесть —
детерминированная (механизм, тип шока, общие ресурсы, общие слова), без LLM: одинаковый запрос даёт
одинаковый ответ. Счёт пар (reports/history/pairs_<дата>.json, если есть) печатается рядом —
«что история говорит про эту связь».

Аналогия — ГИПОТЕЗА о роли, не доказательство (П16): эпизоды отобраны моделью, знающей исход.

  python3 ops/analogs.py --text "Ормуз танкеры Иран" [--mechanism chokepoint_disruption]
                         [--shock route_blocked] [--resource oil] [--top 6] [--json]
"""
import argparse
import glob
import json
import math
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LIBRARY = ROOT / "knowledge" / "cascade_history.yaml"
PAIRS_GLOB = str(ROOT / "reports" / "history" / "pairs_*.json")
_WORD = re.compile(r"[a-zа-яё0-9]{3,}", re.I)
_STOP = {"the", "and", "for", "что", "как", "это", "при", "после", "или", "его", "под", "над", "все", "был",
         "была", "были", "через", "когда", "если", "тот", "так", "уже", "ещё", "еще", "for", "with", "from"}


def _tokens(*parts):
    out = set()
    for p in parts:
        if isinstance(p, (list, tuple)):
            out |= _tokens(*p)
        elif p:
            out |= {w.lower()[:6] for w in _WORD.findall(str(p)) if w.lower() not in _STOP}
    return out


def load(path=LIBRARY):
    try:
        return (yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}).get("эпизоды") or []
    except OSError:
        return []


def latest_pairs(pattern=PAIRS_GLOB):
    fs = sorted(glob.glob(pattern))
    if not fs:
        return {}
    try:
        data = json.loads(pathlib.Path(fs[-1]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {(r["from"], r["to"], r.get("src_dir")): r for r in data.get("пары") or []}


def score(ep, *, text="", mechanism=None, shock=None, resources=()):
    """Похожесть эпизода на запрос: механизм 4, тип шока 2, общий ресурс 1.5 за каждый, слова 0.5 за
    каждое общее (до 6). Только детерминированная арифметика."""
    s = 0.0
    if mechanism and ep.get("mechanism_id") == mechanism:
        s += 4
    if shock and ep.get("shock_type") == shock:
        s += 2
    res = {str(r).lower() for r in ep.get("resources") or []}
    s += 1.5 * len(res & {str(r).lower() for r in resources or ()})
    if text:
        q = _tokens(text)
        body = _tokens(ep.get("title_ru"), ep.get("what_ru"), ep.get("resources"), ep.get("region"),
                       ep.get("mechanism_id"), [L.get("role_ru") for L in ep.get("links") or []],
                       [L.get("role_tag") for L in ep.get("links") or []])
        s += 0.5 * min(6, len(q & body))
    return s


def find(text="", mechanism=None, shock=None, resources=(), top=6, episodes=None):
    eps = load() if episodes is None else episodes
    ranked = sorted(((score(e, text=text, mechanism=mechanism, shock=shock, resources=resources), e)
                     for e in eps), key=lambda x: (-x[0], x[1].get("trigger_date") or ""))
    return [(s, e) for s, e in ranked if s > 0][:top]


def _pct(x):
    return "—" if x is None else f"{x * 100:+.0f}%"


def _move(x):
    """ход_h в библиотеке — логдоходность; печатаем изменение цены exp(x)−1 (ревью 03.11: −125% лог ≠ −125%)."""
    return "—" if x is None else f"{math.expm1(x) * 100:+.0f}%"


def render(found, pairs=None):
    pairs = pairs or {}
    if not found:
        return "Похожих эпизодов в библиотеке нет (или библиотека не собрана) — аналогии не будет; это нормально."
    out = ["ПОХОЖИЕ ИСТОРИЧЕСКИЕ КАСКАДЫ (аналогия — гипотеза о РОЛИ, не доказательство; числа — код по ценам, "
           "изменение цены от закрытия до повода):"]
    for s, e in found:
        out.append(f"\n■ {e.get('trigger_date')} — {e.get('title_ru')} [механизм {e.get('mechanism_id')}, "
                   f"похожесть {s:g}]")
        out.append(f"  что было: {e.get('what_ru')}")
        for L in e.get("links") or []:
            p = L.get("prices") or {}
            moves = (f"код: 5д {_move(p.get('ход_5'))}, 20д {_move(p.get('ход_20'))}, 60д {_move(p.get('ход_60'))}"
                     if p.get("есть") else f"код: {p.get('причина') or 'цены нет'}")
            out.append(f"  {L.get('order')}) {L.get('role_ru')} [{L.get('role_tag')}] — тогда {L.get('then_symbol') or 'нет'}, "
                       f"сегодня {L.get('today_symbol') or 'нет'}; {'вверх' if L.get('direction') == 'up' else 'вниз'}"
                       f"{' (неочевидно)' if L.get('non_obvious') else ''}; {moves}")
        for L in e.get("links") or []:
            for (a, b, sd), r in pairs.items():
                if b == L.get("today_symbol") and e.get("id") in (r.get("эпизоды") or []):
                    st = r.get("счёт") or {}
                    out.append(f"  история пары {a}→{b} (шоки {r.get('шоки_источника')}): {r.get('вердикт')}"
                               + (f" — из {st.get('крупных_ходов')} крупных ходов в сторону механизма "
                                  f"{st.get('пошло')} при ожидании по знаку {st.get('ожидание_по_знаку')}"
                                  if st.get("крупных_ходов") else ""))
        out.append(f"  провальный аналог: {e.get('counter_case_ru')}")
        out.append(f"  правило переноса: {e.get('analog_rule_ru')}")
        src = (e.get("sources") or [{}])[0].get("url")
        if src:
            out.append(f"  источник: {src}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="похожие исторические каскады для охотника")
    ap.add_argument("--text", default="")
    ap.add_argument("--mechanism")
    ap.add_argument("--shock")
    ap.add_argument("--resource", action="append", default=[])
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    found = find(a.text, a.mechanism, a.shock, a.resource, a.top)
    if a.json:
        print(json.dumps([{"похожесть": s, **e} for s, e in found], ensure_ascii=False, indent=1, default=str))
    else:
        print(render(found, latest_pairs()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
