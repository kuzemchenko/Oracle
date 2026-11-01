# -*- coding: utf-8 -*-
"""orchestrator/deep_dive.py — V3 «качаем 2 дня» (карта v3): кнопка владельца → двухдневная
прокачка идеи → паспорт.

Поток: бот пишет нажатие [Качаем 2 дня] в journal/deep_dive.jsonl (append-only). Этот модуль
по крону двигает каждую идею по стадиям:
  • ДЕНЬ 1 «устойчивость»: состязательный контур (orchestrator/challenge) с сильнейшим
    возражением — «почему идея НЕ сработает» + из вердикта собираем «что должно случиться,
    чтобы идея умерла»;
  • ДЕНЬ 2 «экономика»: размер ставки по правилам системы (mathlib/kelly: до доказанной
    калибровки — ФИКС микро-размер §11, Келли честно выключен), правило выхода, дата сверки;
    итог — ПАСПОРТ идеи (reports/deep_dive/) и финальное сообщение владельцу.
Доставка: тексты дней пишутся в journal/notices.jsonl (бот пушит с ретраем, как дозор §25),
но ТОЛЬКО после блокирующего линта подачи (fail-closed: не прошло — в notices короткая
служебная заметка, сломанный текст владельцу не уходит).
Прогресс — append-only записями стадий в тот же deep_dive.jsonl (журнал не редактируется).
"""
import json
import pathlib
import datetime
import argparse
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

QUEUE = ROOT / "journal" / "deep_dive.jsonl"
NOTICES = ROOT / "journal" / "notices.jsonl"
PASSPORTS = ROOT / "reports" / "deep_dive"
CAPITAL_USD = 100_000          # §30 капитал-ориентир
MAX_PER_RUN = 2                # не больше двух продвижений за прогон (бюджет/шум)

DOUBT_DAY1 = ("Найди САМОЕ сильное возражение против этой идеи и атакуй им: почему она "
              "НЕ сработает? Честный разнос важнее вежливости.")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _read_jsonl(path):
    p = pathlib.Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def _append(path, rec):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def queue_state(queue_path=None):
    """(run_id, asset) → {'stage': 'queued'|'day1'|'day2', 'last_ts': iso, 'day1': rec|None}."""
    st = {}
    for r in _read_jsonl(queue_path or QUEUE):
        key = (r.get("run_id"), r.get("asset"))
        if None in key:
            continue
        cur = st.setdefault(key, {"stage": "queued", "last_ts": r.get("ts"), "day1": None})
        stage = r.get("status") or r.get("stage")
        if stage == "day1":
            cur["stage"], cur["day1"] = "day1", r
        elif stage == "day2":
            cur["stage"] = "day2"
        if (r.get("ts") or "") > (cur["last_ts"] or ""):
            cur["last_ts"] = r.get("ts")
    return st


def _lint_or_fallback(text):
    try:
        sys.path.insert(0, str(ROOT / "ops"))
        import presentation_lint as PL
        return PL.lint_week_odds(text)          # тот же строгий контракт: секции/жаргон/тикеры с именами
    except Exception:                            # noqa: BLE001 — линт недоступен ≠ слать сломанное
        return ["линт подачи недоступен"]


def _notice(text, notices_path=None, now=None):
    return _append(notices_path or NOTICES,
                   {"ts": (now or _now()).isoformat(timespec="seconds"), "text": text,
                    "source": "deep_dive"})


_TICKER_RE = None


def _humanize_tickers(text):
    """Голые тикеры *.US в чужом тексте (резюме суда) → «Имя (ТИКЕР)» — требование линта
    «тикер без имени не печатается». Имени нет в словаре — оставляем как есть (линт отбросит
    текст целиком — fail-closed лучше выдумки, П8)."""
    global _TICKER_RE
    import re as _re
    if _TICKER_RE is None:
        _TICKER_RE = _re.compile(r"\b([A-Z0-9]{1,8}\.US)\b")
    try:
        sys.path.insert(0, str(ROOT / "ops"))
        import bot_reports as R
    except Exception:                            # noqa: BLE001
        return text

    def sub(m):
        t = m.group(1)
        имя = R._asset_human(t)[0]
        if имя and not имя.startswith("инструмент ") and имя not in text[max(0, m.start() - 60):m.start()]:
            return f"{имя} ({t})"
        return t
    return _TICKER_RE.sub(sub, str(text))


def _asset_label(asset):
    try:
        sys.path.insert(0, str(ROOT / "ops"))
        import bot_reports as R
        return f"{R._asset_human(asset)[0]} ({asset})"
    except Exception:                            # noqa: BLE001
        return str(asset)


_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")


def _clean_text(s, limit=420):
    s = _EMOJI_RE.sub("", str(s or "")).strip()
    s = re.sub(r"\s+", " ", s)
    # внутренние «» → „“: текст пойдёт во внешнюю цитату «…», вложенность сломала бы линт
    s = s.replace("«", "„").replace("»", "“")
    if len(s) <= limit:
        return s
    return s[:limit].rsplit(" ", 1)[0].rstrip(".;,— ") + "…"


def _extract_challenge(ch):
    """БОЕВОЙ протокол challenge (orchestrator/challenge.py) → (вердикт, разбор, смерть[]).

    Аудит 01.11: прежний код читал сценарии_инвалидации/вердикт с ВЕРХНЕГО уровня — такие
    ключи были только в тестовом стабе, вживую день-1 печатал пустой фолбэк, а резюме суда
    с эмодзи ломало линт. Боевые места: дебаты.вердикт.исход; критик.judgment
    (вывод / сильнейшее_возражение / скрытые_допущения). Верхнеуровневые ключи
    (стаб/будущие версии) — в приоритете; эмодзи чистятся (линт подачи)."""
    деб = ch.get("дебаты") or {}
    верд = (ch.get("вердикт") or (ch.get("суд") or {}).get("вердикт")
            or (деб.get("вердикт") or {}).get("исход") or "вердикт в разборе")
    кр = ((деб.get("реплики") or {}).get("критик") or {}).get("judgment") or {}
    разбор = str(ch.get("резюме") or "").strip()
    if not разбор or _EMOJI_RE.search(разбор):
        разбор = str(кр.get("вывод") or "").strip() or _EMOJI_RE.sub("", разбор).strip()
    разбор = _clean_text(разбор)
    смерть = [str(x) for x in (ch.get("сценарии_инвалидации") or ch.get("что_убивает") or [])]
    if not смерть:
        s = кр.get("сильнейшее_возражение")
        if s:
            смерть.append("подтвердится сильнейшее возражение критика: «" + _clean_text(s, 180) + "»")
        for a in (кр.get("скрытые_допущения") or [])[:2]:
            смерть.append("рушится допущение: «" + _clean_text(a, 140) + "»")
    return str(верд), разбор, смерть


def _day1_text(asset, ch, now):
    label = _asset_label(asset)
    вердикт, разбор, смерть_raw = _extract_challenge(ch)
    резюме = _humanize_tickers(разбор)
    смерть = [_humanize_tickers(x) for x in смерть_raw]
    L = [f"Прокачка, день первый — {label}: испытание на прочность.",
         "",
         "Что это значит для тебя. Ты нажал «Качаем» — сегодня идею атаковал её сильнейший "
         "противник, завтра посчитаем экономику ставки.",
         ""]
    if резюме:
        # слова суда/критика — чужой текст, печатаем дословной цитатой (линт «…» не правит)
        L.append(f"Суд говорит: «{резюме.rstrip('.')}».")
        L.append("")
    if смерть:
        L.append("Идея умирает, если: " + "; ".join(str(x) for x in list(смерть)[:3]) + ".")
    else:
        L.append(f"Итог испытания: {вердикт}. Условия смерти идеи соберу в паспорт завтра.")
    L += ["",
          "Что делать. Ничего — завтра утром придёт экономика: размер, правило выхода, дата сверки.",
          "Это исследовательский инструмент, не инвестиционная рекомендация."]
    return "\n".join(L)


def _day2_passport(asset, run_id, day1_rec, now):
    """Детерминированная экономика §11/§21: до доказанной калибровки Келли выключен —
    фикс микро-размер. Возвращает (текст_пуша, markdown_паспорта)."""
    from mathlib import kelly as K
    label = _asset_label(asset)
    size = K.position_size(0.5, 1.0, CAPITAL_USD, gate_passed=False)  # ФИКС 0.5% — честно до калибровки
    amount = size.get("amount_usd") or CAPITAL_USD * 0.005
    смерть = (day1_rec or {}).get("смерть") or []
    строки_смерти = ("; ".join(_humanize_tickers(x) for x in смерть[:3])
                     if смерть else "условия из вчерашнего разбора — см. паспорт")
    push = "\n".join([
        f"Прокачка, день второй — {label}: экономика ставки.",
        "",
        "Что это значит для тебя. Вот рамка, ЕСЛИ решишь действовать (решение всегда твоё).",
        "",
        f"Размер. Пока калибровка системы не доказана, работает жёсткое правило: не больше "
        f"${amount:,.0f} на идею (половина процента капитала-ориентира). Формула Келли включится "
        f"только после доказанного счёта — так задумано, это защита от сверхуверенности.",
        f"Выход. Идея умирает, если: {строки_смерти}.",
        "Сверка. Итог идеи попадёт в счёт честности в дату её срока — покажу сам.",
        "",
        "Что делать. Паспорт идеи сохранён — «разбери " + str(asset).split(".")[0] +
        "» покажет его в любой момент. Решение за тобой.",
        "Это исследовательский инструмент, не инвестиционная рекомендация.",
    ])
    md = "\n".join([
        f"# Паспорт идеи — {label}",
        f"Создан: {now.date().isoformat()} · источник: кнопка «Качаем 2 дня» · прогон {run_id}",
        "",
        "## День 1 — устойчивость",
        str((day1_rec or {}).get("резюме") or "разбор дня 1 — в journal/challenges/"),
        "",
        "## День 2 — экономика",
        f"- Размер: фикс ${amount:,.0f} (0.5% капитала; Келли до доказанной калибровки выключен — §11/§21)",
        f"- Смерть идеи: {строки_смерти}",
        "- Рамка: исследовательский инструмент, не инвестиционная рекомендация (§16)",
    ])
    return push, md


def run_deep_dive(mode="auto", now=None, queue_path=None, notices_path=None,
                  passports_dir=None, challenge_fn=None, max_per_run=MAX_PER_RUN):
    """Продвинуть очередь прокачки на одну стадию на идею (день-1 → день-2 в разные дни)."""
    now = now or _now()
    today = now.date().isoformat()
    итог = {"ts": now.isoformat(timespec="seconds"), "продвинуто": 0, "день1": 0, "день2": 0,
            "пропущено": [], "ошибки": []}
    state = queue_state(queue_path)
    for (run_id, asset), st in sorted(state.items(), key=lambda kv: kv[1]["last_ts"] or ""):
        if итог["продвинуто"] >= max_per_run:
            break
        stage, last_day = st["stage"], (st["last_ts"] or "")[:10]
        if stage == "day2":
            continue
        if stage == "day1" and last_day >= today:
            итог["пропущено"].append({"asset": asset, "причина": "день-1 сегодня — день-2 завтра"})
            continue
        try:
            if stage == "queued":
                ch_fn = challenge_fn
                if ch_fn is None:
                    from orchestrator import challenge as CH
                    ch_fn = lambda a: CH.run_challenge(DOUBT_DAY1, asset=a, mode=mode, write=True)  # noqa: E731
                ch = ch_fn(asset) or {}
                if "ОТКАЗ" in ch:
                    итог["ошибки"].append({"asset": asset, "этап": "day1", "ошибка": str(ch["ОТКАЗ"])})
                    continue
                _верд, _разбор, смерть = _extract_challenge(ch)   # боевая структура протокола
                text = _day1_text(asset, ch, now)
                viol = _lint_or_fallback(text)
                if viol:
                    итог["ошибки"].append({"asset": asset, "этап": "day1", "ошибка": f"линт: {viol[:2]}"})
                    continue
                _notice(text, notices_path, now)
                _append(queue_path or QUEUE,
                        {"ts": now.isoformat(timespec="seconds"), "run_id": run_id, "asset": asset,
                         "stage": "day1", "challenge_run_id": ch.get("run_id"),
                         "резюме": _разбор[:2000], "смерть": list(смерть)[:5]})
                итог["день1"] += 1
            else:                                                     # day1 → day2
                push, md = _day2_passport(asset, run_id, st.get("day1"), now)
                viol = _lint_or_fallback(push)
                if viol:
                    итог["ошибки"].append({"asset": asset, "этап": "day2", "ошибка": f"линт: {viol[:2]}"})
                    continue
                d = pathlib.Path(passports_dir or PASSPORTS)
                d.mkdir(parents=True, exist_ok=True)
                fname = f"{str(asset).replace('.', '_')}_{now.strftime('%Y%m%d')}.md"
                (d / fname).write_text(md, encoding="utf-8")
                _notice(push, notices_path, now)
                _append(queue_path or QUEUE,
                        {"ts": now.isoformat(timespec="seconds"), "run_id": run_id, "asset": asset,
                         "stage": "day2", "passport": fname})
                итог["день2"] += 1
            итог["продвинуто"] += 1
        except Exception as e:                                        # noqa: BLE001 — одна идея ≠ весь прогон
            итог["ошибки"].append({"asset": asset, "ошибка": f"{type(e).__name__}: {e}"})
    return итог


def main(argv=None):
    ap = argparse.ArgumentParser(description="V3 «качаем 2 дня»: продвижение очереди прокачки")
    ap.add_argument("--mode", default="auto", choices=["auto", "live", "mock"])
    a = ap.parse_args(argv)
    r = run_deep_dive(mode=a.mode)
    print(f"[deep_dive] продвинуто {r['продвинуто']} (день1 {r['день1']}, день2 {r['день2']}), "
          f"пропущено {len(r['пропущено'])}, ошибок {len(r['ошибки'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
