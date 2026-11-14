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
IDEAS = ROOT / "reports" / "ideas"
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
        elif stage in ("day2", "closed"):
            cur["stage"] = "day2"               # VQ-4: closed = идея снята после дня 1 (отклонена / нужны данные)
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


def _humanize_tickers(text, names=None):
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
        имя = (names or {}).get(t) or R._asset_human(t)[0]
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


# VQ-4 (09.11): вердикт дня 1 решает судьбу дня 2. Вживую 07.11 суд вынес MP и DHT «РАЗБИТА», а 08.11
# паспорт всё равно выдал план входа (вход/цель/стоп). Теперь: РАЗБИТА/ВЕТО → идея «отклонена», плана нет,
# поиск идёт к следующей ветке; неясный вердикт → «нужны данные», плана нет; УСТОЯЛА → экономика СРАЗУ
# в тот же прогон (второй день не ждём искусственно — расчёт детерминированный).
NEGATIVE = ("РАЗБИТА", "ВЕТО", "ОТКЛОН", "ОПРОВЕРГ", "НЕ УСТОЯЛА", "НЕ ВЫДЕРЖАЛА")


def verdict_class(verdict):
    v = str(verdict or "").upper()
    if any(x in v for x in NEGATIVE):
        return "negative"
    if v.startswith("УСТОЯЛА") and "БЕЗ_КРИТИКИ" not in v and "БЕЗ КРИТИКИ" not in v:
        return "positive"
    return "unclear"


def challenge_verdict(challenge_run_id, challenges_dir=None):
    """Вердикт сохранённого разбора journal/challenges/<id>.json (для записей дня 1 до 09.11)."""
    p = pathlib.Path(challenges_dir or (ROOT / "journal" / "challenges")) / f"{challenge_run_id}.json"
    try:
        return _extract_challenge(json.loads(p.read_text(encoding="utf-8")))[0]
    except (OSError, ValueError):
        return None


def _day1_closed_text(asset, label, verdict, cls, разбор, смерть, names=None):
    label = label or _asset_label(asset)
    резюме = _humanize_tickers(разбор, names)
    if cls == "negative":
        L = [f"Прокачка {label}: идея не устояла.", "",
             f"Что это значит для тебя. Сильнейший противник разбил идею (итог суда — {verdict}). "
             "Плана покупки не будет: идея помечена «отклонена», поиск пойдёт к следующей ветке."]
    else:
        L = [f"Прокачка {label}: вывод пока не получен.", "",
             f"Что это значит для тебя. Суд не дал ясного итога ({verdict}). Плана покупки нет: идея помечена "
             "«нужны данные» — сначала недостающие факты."]
    if резюме:
        L += ["", f"Суд говорит: «{резюме.rstrip('.')}»."]
    if смерть:
        L += ["", "Главное против: " + "; ".join(_humanize_tickers(x, names) for x in list(смерть)[:3]) + "."]
    L += ["", "Что делать. Ничего: прежние уровни входа больше не действуют.",
          "Это исследовательский инструмент, не инвестиционная рекомендация."]
    return "\n".join(L)


def _set_idea_status(run_id, cls, verdict, reason, ch_id, ideas_dir=None, status_dir=None, now=None):
    """Решение дня 1 → актуальный статус идеи охотника (ops/idea_revision) + продолжение поиска."""
    try:
        from ops import idea_revision as IR
    except ImportError:
        import idea_revision as IR
    stem = IR.stem_for_run_id(run_id, ideas_dir)
    if not stem:
        return None
    st = {"negative": "отклонена", "unclear": "нужны данные", "positive": "действует"}[cls]
    rec = IR.set_status(stem, st, [f"прокачка, день 1: итог суда «{verdict}» — {_clean_text(reason, 300)}"],
                        questions=(["какие факты снимут сомнение суда"] if cls == "unclear" else []),
                        source="прокачка, день 1", date=(now or _now()).date().isoformat(), out_dir=status_dir,
                        refs=[f"journal/challenges/{ch_id}.json"] if ch_id else [])
    if cls == "negative":
        try:
            from ops import search_queue as SQ
            SQ.refute(str(run_id).rsplit("_", 1)[-1], f"прокачка, день 1: {verdict} — {_clean_text(reason, 200)}")
        except Exception:                                          # noqa: BLE001 — поиск не блокирует прокачку
            pass
    return rec


def backfill_statuses(queue_path=None, ideas_dir=None, status_dir=None, challenges_dir=None):
    """Разовая перепроверка записей дня 1 до 09.11 (вердикт не сохранялся): вердикт из journal/challenges →
    актуальный статус идеи, если его ещё нет. История прогноза не трогается. → [(run_id, статус)]."""
    try:
        from ops import idea_revision as IR
    except ImportError:
        import idea_revision as IR
    out = []
    for r in _read_jsonl(queue_path or QUEUE):
        if r.get("stage") != "day1" or not str(r.get("run_id") or "").startswith("h_"):
            continue
        stem = IR.stem_for_run_id(r["run_id"], ideas_dir)
        if not stem or (IR._status_doc(stem, status_dir).get("текущий")):
            continue
        verdict = r.get("вердикт") or challenge_verdict(r.get("challenge_run_id"), challenges_dir)
        if not verdict:
            continue
        cls = verdict_class(verdict)
        rec = _set_idea_status(r["run_id"], cls, verdict, r.get("резюме") or "", r.get("challenge_run_id"),
                               ideas_dir, status_dir, now=datetime.datetime.fromisoformat(r["ts"]))
        if rec:
            out.append((r["run_id"], rec["статус"]))
    return out


def _day1_text(asset, ch, now, label=None, names=None):
    label = label or _asset_label(asset)
    вердикт, разбор, смерть_raw = _extract_challenge(ch)
    резюме = _humanize_tickers(разбор, names)
    смерть = [_humanize_tickers(x, names) for x in смерть_raw]
    L = [f"Прокачка, день первый — {label}: испытание на прочность.",
         "",
         "Что это значит для тебя. Ты нажал «Качаем» — идею атаковал её сильнейший "
         "противник, и она устояла; экономика ставки — следующим сообщением.",
         ""]
    if резюме:
        # слова суда/критика — чужой текст, печатаем дословной цитатой (линт «…» не правит)
        L.append(f"Суд говорит: «{резюме.rstrip('.')}».")
        L.append("")
    if смерть:
        L.append("Идея умирает, если: " + "; ".join(str(x) for x in list(смерть)[:3]) + ".")
    else:
        L.append(f"Итог испытания: {вердикт}. Условия смерти идеи — в паспорте следующим сообщением.")
    L += ["",
          "Что делать. Ничего — следом придёт экономика: размер, правило выхода, дата сверки.",
          "Это исследовательский инструмент, не инвестиционная рекомендация."]
    return "\n".join(L)


def _day2_passport(asset, run_id, day1_rec, now, card=None, status_ideas_dir=None, status_dir=None):
    """Детерминированная экономика §11/§21: до доказанной калибровки Келли выключен —
    фикс микро-размер. Возвращает (текст_пуша, markdown_паспорта). У идеи охотника план
    (вход/цель/выход/срок) — из ЦЕН её карточки, а условия смерти — из её kill_conditions."""
    from mathlib import kelly as K
    label = _card_label(asset, card)
    size = K.position_size(0.5, 1.0, CAPITAL_USD, gate_passed=False)  # ФИКС 0.5% — честно до калибровки
    amount = size.get("amount_usd") or CAPITAL_USD * 0.005
    смерть = (day1_rec or {}).get("смерть") or []
    строки_смерти = ("; ".join(_humanize_tickers(x) for x in смерть[:3])
                     if смерть else "условия из вчерашнего разбора — см. паспорт")
    plan = _hunter_plan_lines(card) if card else []
    cur = None
    if card:
        # VQ-4: план — только если актуальный статус идеи «действует» (стоп, цель, срок, катализатор,
        # свежесть цены перепроверены кодом прямо сейчас); иначе — архивные уровни с пометкой
        try:
            from ops import idea_revision as IR
        except ImportError:
            import idea_revision as IR
        stem = IR.stem_for_run_id(run_id, status_ideas_dir)
        if stem:
            cur = IR.current(stem, card=card, today=now.date().isoformat(), out_dir=status_dir)
            plan = IR.plan_or_archive(card, cur, plan)
    if card:
        kills = _hunter_kills(card)[:2] + [_humanize_tickers(x) for x in смерть[:1]]
        строки_смерти = "; ".join(kills) if kills else строки_смерти
    size_line = (f"Размер. Пока калибровка системы не доказана, работает жёсткое правило: не больше "
                 f"${amount:,.0f} на идею (половина процента капитала-ориентира). Формула Келли включится "
                 f"только после доказанного счёта — так задумано, это защита от сверхуверенности.")
    fname = f"{str(asset).replace('.', '_')}_{now.strftime('%Y%m%d')}.md"
    active = cur is None or cur.get("план_действует")
    push = "\n".join([
        f"Прокачка, день второй — {label}: {'экономика ставки' if active else 'план не действует'}.",
        "",
        ("Что это значит для тебя. Вот рамка, ЕСЛИ решишь действовать (решение всегда твоё)." if active else
         "Что это значит для тебя. Перепроверка перед выдачей: цена ушла от границы входа — сейчас не входить, "
         "план ждёт возврата цены (решение всегда твоё)." if cur.get("статус") == "ждёт входа" else
         f"Что это значит для тебя. Перепроверка перед выдачей: статус идеи «{cur['статус']}» — действовать "
         "по старому плану нельзя."),
        "",
        *([size_line] if active else []),
        *plan,
        f"{'Когда идея мертва' if card else 'Выход'}. Идея умирает, если: {строки_смерти}.",
        "Сверка. Итог идеи попадёт в счёт честности в дату её срока — покажу сам.",
        "",
        "Что делать. Решение за тобой. Паспорт идеи сохранён в репозитории: reports/deep_dive/" + fname + ".",
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
        (f"- Размер: фикс ${amount:,.0f} (0.5% капитала; Келли до доказанной калибровки выключен — §11/§21)"
         if active else f"- Статус идеи: {cur['статус']} ({cur.get('дата')}) — план не действует"),
        *[f"- {x}" for x in plan],
        f"- Смерть идеи: {строки_смерти}",
        "- Рамка: исследовательский инструмент, не инвестиционная рекомендация (§16)",
    ])
    return push, md


# ── идеи охотника (кнопка под reports/ideas/idea_*.push.txt, run_id «h_<дата>_<ТИКЕР>») ──────
# Аудит 03.11: прокачка по run_id h_* искала идею в старой воронке (find_idea → ef_20261020 по
# тому же тикеру) — разбирала ЧУЖОЙ тезис. Теперь кандидат собирается из карточки охотника,
# а без карточки прокачка отказывает с заметкой (без отката в воронку).
def hunter_card(run_id, ideas_dir=None):
    """run_id кнопки под идеей охотника → её §9-карточка reports/ideas/idea_<…>.card.json или None."""
    rid = str(run_id or "")
    if not rid.startswith("h_"):
        return None
    inc = ROOT / ".cache" / "ideas_incoming" / "reports" / "ideas"   # снимок облака при заморозке протяжки
    d = pathlib.Path(ideas_dir or (inc if inc.is_dir() else IDEAS))
    stem = "idea_" + rid[2:]
    p = d / (stem + ".card.json")
    if not p.exists() and (d / "alt" / (stem + ".card.json")).exists():
        p = d / "alt" / (stem + ".card.json")         # доставлена запасная идея дня
    if not p.exists():
        found = sorted(d.glob(stem + "*.card.json"))   # бот режет id до 40 знаков — префикс
        p = found[0] if len(found) == 1 else None
    if p is None:
        return None
    try:
        card = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return card if isinstance(card, dict) else None


def _flat(s, limit):
    s = " ".join(str(s or "").split())
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "…"


def candidate_from_hunter_card(card):
    """Кандидат для состязательного разбора — только из полей карточки (П8): тезис, почему не в
    цене, кто продаёт; §9-разрешимость из spec9 (ставка запечатана сервером ровно такой)."""
    spec = card.get("spec9") if isinstance(card.get("spec9"), dict) else {}
    asset = spec.get("asset") or card.get("ticker")
    side = spec.get("direction")
    cmp_ = {"above": "выше", "below": "ниже"}.get(side)
    razr = (f"§9: {asset} закроется {cmp_} {spec.get('threshold')} к {spec.get('resolve_by')} "
            f"(закрытие EODHD)") if cmp_ and spec.get("threshold") is not None and spec.get("resolve_by") else None
    parts = [_flat(card.get("thesis"), 900)]
    if card.get("why_not_priced"):
        parts.append("почему не в цене: " + _flat(card.get("why_not_priced"), 400))
    if card.get("who_sells"):
        parts.append("кто продаёт и почему неправ: " + _flat(card.get("who_sells"), 400))
    return {"актив": asset, "направление": {"above": "лонг", "below": "шорт"}.get(side),
            "тезис": "; ".join(x for x in parts if x) or asset, "разрешимость": razr,
            "школа": "охотник v3 (облачная охота с проверкой кодом)"}


def _money(x):
    try:
        return f"${float(x):,.2f}"
    except (TypeError, ValueError):
        return None


def _ru_date(iso):
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]
    try:
        d = datetime.date.fromisoformat(str(iso)[:10])
        return f"{d.day} {months[d.month - 1]}"
    except ValueError:
        return str(iso)


def _card_label(asset, card):
    """«Имя (ТИКЕР)»: у идеи охотника имя — из её карточки (словарь бота знает не всех)."""
    name = " ".join(str((card or {}).get("name") or "").split())
    return f"{name} ({asset})" if name else _asset_label(asset)


def _hunter_plan_lines(card):
    """План из ЦЕН карточки (их пересчитал и запечатал сервер): вход, цель, выход, срок."""
    spec = card.get("spec9") if isinstance(card.get("spec9"), dict) else {}
    up = spec.get("direction") == "above"
    entry, target, stop = _money(card.get("entry_limit")), _money(card.get("target_price")), _money(card.get("stop_price"))
    out = []
    if entry:
        out.append(f"Вход. {'Не дороже' if up else 'Не дешевле'} {entry} за акцию — так в плане идеи; "
                   f"если рынок ушёл дальше, не догонять.")
    if target and stop:
        out.append(f"Выход. Цель {target} — фиксировать результат; выход без раздумий, если цена "
                   f"закрывается {'ниже' if up else 'выше'} {stop}.")
    if spec.get("resolve_by"):
        out.append(f"Срок. Крайний выход — {_ru_date(spec['resolve_by'])}: в этот день система сверит "
                   f"прогноз и покажет итог сама.")
    return out


def _hunter_kills(card):
    rows = []
    for k in (card.get("kill_conditions") or [])[:3]:
        if isinstance(k, dict) and k.get("what"):
            rows.append(_flat(k.get("what"), 200).rstrip(".") +
                        (f" (проверка до {_ru_date(k['check_by'])})" if k.get("check_by") else ""))
    return rows


# ── лист ставки вместо «суда, который убивает» (ревью 14.11: Fable п.4, Codex «экономика кодом»; да владельца
# 14.11 на шаг, ВКЛЮЧЕНИЕ — флаг deep_dive_bet_sheet после ручного прогона и «ок»). Суд остаётся, но его итог
# больше не закрывает идею: довод противника печатается как главный риск, а решение — за владельцем по
# деньгам: сколько выигрыш при цели, сколько проигрыш при стопе, при каком шансе ставка в плюс (считает
# mathlib/bet_sheet, Инв#6). Статус идеи по-прежнему решает только перепроверка кодом (idea_revision.current).
FEES_USD = 2.0                 # ориентир комиссии брокера туда-обратно; честно помечается в тексте


def _bet_sheet_flag():
    try:
        from ops import bot_reports as R
    except ImportError:
        sys.path.insert(0, str(ROOT / "ops"))
        import bot_reports as R
    try:
        return bool((R.load_presentation() or {}).get("deep_dive_bet_sheet"))
    except Exception:                                              # noqa: BLE001 — нет конфига = прежний путь
        return False


def bet_sheet_for_card(card, amount_usd, fees_usd=FEES_USD):
    """Сценарии из ЦЕН карточки: цель / без движения / стоп при входе по границе плана → mathlib.bet_sheet."""
    from mathlib import bet_sheet as BS
    spec = card.get("spec9") if isinstance(card.get("spec9"), dict) else {}
    entry = card.get("entry_limit")
    direction = "long" if spec.get("direction", "above") == "above" else "short"
    sc = [{"name": "цель", "price": card.get("target_price")},
          {"name": "без движения", "price": entry},
          {"name": "стоп", "price": card.get("stop_price")}]
    if any(x.get("price") is None for x in sc):
        return {"ok": False, "причина": "в карточке нет цены входа, цели или стопа"}
    return BS.sheet(entry, sc, size_usd=amount_usd, fees_usd=fees_usd, direction=direction)


def _usd0(x):
    return f"${abs(float(x)):,.0f}".replace(",", "\u00a0")


PLAIN_PROMPT = ROOT / "agents" / "prompts" / "bet_sheet_plain.md"
PLAIN_MAX = 700
BET_SHEET_MAX_LEN = 4000       # владелец 14.11: «пусть длиннее, но понятно»; Telegram — до 4096


def _lint_bet_sheet(text):
    """Тот же контракт подачи (секции, жаргон, тех.id, тикеры с именами), но длина до 4000 знаков."""
    try:
        sys.path.insert(0, str(ROOT / "ops"))
        import presentation_lint as PL
        viol = PL.lint_for_user(text, require_sections=PL.DAILY_SECTIONS, max_len=BET_SHEET_MAX_LEN)
        if PL._TECH_ID_RE.search(text):
            viol.append("тех.id в тексте для владельца")
        bare = PL._bare_tickers(text)
        if bare:
            viol.append("тикеры без имени: " + ", ".join(bare))
        return viol
    except Exception:                                              # noqa: BLE001
        return ["линт подачи недоступен"]


def _plain_guard(text, source):
    """Пересказ — только язык: ни одного числа, тикера или количественного слова, которых нет во входе (П8)."""
    try:
        from ops import story_editor as SE, presentation_lint as PL
    except ImportError:
        import story_editor as SE
        import presentation_lint as PL
    t = str(text or "").strip()
    if not t:
        return ["пусто"]
    viol = []
    if SE._nums(t) - SE._nums(source):
        viol.append("числа не из входа: " + ", ".join(sorted(SE._nums(t) - SE._nums(source))[:4]))
    if SE._tickers(t) - SE._tickers(source):
        viol.append("тикеры не из входа")
    _dates = re.compile(r"\bк\s+\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)",
                        re.I)                               # «$25.5 к 29 января» — дата, а не шансы «5 к 29»
    extra_q = (PL.quantity_markers(_dates.sub("к ДАТА", t), include_quotes=True)
               - PL.quantity_markers(_dates.sub("к ДАТА", source), include_quotes=True))
    if extra_q:
        viol.append("количественные слова не из входа: " + ", ".join(sorted(extra_q)[:4]))
    if len(t) > PLAIN_MAX * 2:
        viol.append("слишком длинно")
    return viol


def plain_retell(thesis, objection, *, mode="auto"):
    """Модель-редактор (роль story_editor, НЕ аналитик) пересказывает идею и довод противника простыми словами.
    → {"идея": str, "против": str} или None (сбой/бюджет/нарушение гарда — тогда код печатает короткую цитату)."""
    src = f"ИДЕЯ:\n{thesis}\n\nДОВОД ПРОТИВНИКА:\n{objection}"
    try:
        system = PLAIN_PROMPT.read_text(encoding="utf-8")
        from orchestrator.openrouter import make_client
        from orchestrator import run_budget as RB
        client = make_client(mode=mode, run_id="bet_sheet_plain")
        if getattr(client, "mode", None) == "live":
            d = RB.precheck("story_editor")
            if not d.get("allowed"):
                return None
            client.cost_guard = RB.RunBudgetGuard("story_editor", d["cap_usd"])
        try:
            resp = client.complete("story_editor", system, src, agent_id="bet_sheet_plain",
                                   output_kind="plain_retell")
        except RB.RunBudgetExceeded:
            return None
        txt = (resp.get("text") if isinstance(resp, dict) else resp) or ""
    except Exception:                                              # noqa: BLE001 — пересказ не обязателен
        return None
    m = re.search(r"ИДЕЯ:\s*(.+?)\s*ПРОТИВ:\s*(.+)", txt, re.S)
    if not m:
        return None
    out = {"идея": _clean_text(m.group(1), PLAIN_MAX), "против": _clean_text(m.group(2), PLAIN_MAX)}
    if _plain_guard(out["идея"] + "\n" + out["против"], src):
        return None
    return out


_STATUS_PLAIN = {
    "ждёт входа": "Цена сейчас выше уровня, по которому идея предлагала покупать. Догонять не стоит: ждём, пока цена "
                  "опустится до {entry}.",
    "отменена условием": "Цена уже опускалась ниже {stop} — это уровень, при котором мы заранее договорились выходить. "
                         "По правилам идеи она отменилась сама, покупать сейчас нельзя.",
    "отклонена": "Идею сняли раньше, по итогам прежней проверки противником. Покупать по ней сейчас нельзя.",
    "устарела": "Срок идеи прошёл — она больше не действует.",
    "нужны данные": "Для решения не хватает фактов — пока их нет, не покупать.",
    "проверить результат катализатора": "Событие, ради которого затевалась идея, уже прошло, а его итог система ещё "
                                         "не проверила. Пока не проверим — не покупать.",
}


def _bet_sheet_text(asset, card, ch, cur, amount, plan=None, kills=None, names=None, plain=None):
    """Лист ставки языком обычного человека (владелец 14.11: «пусть длиннее, но понятно»). Все числа — код."""
    label = _card_label(asset, card)
    вердикт, разбор, смерть = _extract_challenge(ch or {})
    bs = bet_sheet_for_card(card, amount)
    spec = card.get("spec9") if isinstance(card.get("spec9"), dict) else {}
    up = spec.get("direction", "above") == "above"
    role = _flat(card.get("role"), 300) if card.get("role") else ""
    L = [f"Лист ставки: {label}" + (f" — {role}" if role and not (plain and plain.get("идея")) else "") + ".", "",
         "Что это значит для тебя. Не приговор «хорошая идея или плохая», а расчёт: сколько заработаешь, если идея "
         "сработает, сколько потеряешь, если нет, и как часто она должна срабатывать, чтобы игра стоила свеч."]
    if plain and plain.get("идея"):
        L += ["", "В чём идея (пересказ моделью-редактором; числа сверены кодом, полный текст — в карточке идеи). "
              + plain["идея"]]
    if not bs.get("ok"):
        L += ["", f"Посчитать ставку не получилось: {bs.get('причина')}.",
              "", "Что делать. Ничего — без цен входа, цели и выхода ставку не оценить.",
              "Это исследовательский инструмент, не инвестиционная рекомендация."]
        return "\n".join(L), bs
    rows = {r["name"]: r for r in bs["сценарии"]}
    pos = bs["позиция"]
    entry, tgt, stp = pos["цена_входа"], rows["цель"]["price"], rows["стоп"]["price"]
    win, loss = rows["цель"]["pnl_usd"], rows["стоп"]["pnl_usd"]
    fee = pos["комиссии_usd"]
    qty = pos["кол_во"]
    till = f" до {_ru_date(spec['resolve_by'])}" if spec.get("resolve_by") else ""
    L += ["", "Что предлагается. " + (f"Купить акции примерно на {_usd0(pos['вложено_usd'])} (около {qty:.0f} шт.) "
          f"по цене не выше {_money(entry)}." if up else
          f"Сыграть на падение (продать «в долг», шорт) примерно на {_usd0(pos['вложено_usd'])} по цене не ниже "
          f"{_money(entry)}.") + f" Сумма — фиксированная, полпроцента от условного капитала: пока система не "
          f"доказала, что умеет угадывать, больше на одну идею не ставим.",
          "", "Три возможных исхода:",
          f"1) Хороший: цена {'дорастает' if up else 'опускается'} до {_money(tgt)} "
          f"({'+' if up else '−'}{abs(tgt / entry - 1) * 100:.0f}% от входа). Продаёшь — заработок около {_usd0(win)}.",
          f"2) Плохой: цена {'падает' if up else 'растёт'} до {_money(stp)}. Здесь мы заранее договариваемся выйти "
          f"(это называется «стоп» — чтобы не потерять больше). Потеря около {_usd0(loss)}.",
          f"3) Ничего не происходит{till}: выходишь примерно по цене входа и теряешь только комиссию брокера — "
          f"около {_usd0(fee)} (это моя прикидка комиссии, а не данные)."]
    pstar, n = bs.get("порог_шанса"), None
    if pstar is not None and win > 0 > loss:
        n = min(99, max(1, int(-(-pstar * 100 // 1))))           # округление вверх до целого «из 100»
        ratio = win / -loss
        L += ["", "Когда такая ставка выгодна. "
              + (f"Выигрыш в хорошем исходе больше потери в плохом (в {ratio:.1f} раза). Поэтому выигрывать чаще, "
                 f"чем проигрывать, не обязательно. " if ratio > 1 else
                 f"Потеря в плохом исходе больше выигрыша в хорошем. Поэтому выигрывать нужно чаще, чем проигрывать. ")
              + f"Порог — хороший исход хотя бы в {n} из 100 случаев. Проверка на примере: если из 100 таких ставок {n} закончатся "
                f"хорошо, а {100 - n} плохо, то заработок {n} × {_usd0(win)} = {_usd0(n * win)}, потеря "
                f"{100 - n} × {_usd0(loss)} = {_usd0((100 - n) * loss)} — примерно в ноль. "
                f"Чаще, чем в {n} из 100 случаев, — ставка в плюсе, реже — в минусе."]
        p = spec.get("probability")
        if isinstance(p, (int, float)):
            k = round(p * 100)
            verdict_words = ("выше порога — по оценке самой системы ставка скорее выгодна" if k > n else
                             "ниже порога — значит, по оценке самой системы ставка скорее убыточна" if k < n else
                             "ровно на пороге — ставка примерно в ноль")
            L += ["", f"Насколько вероятен хороший исход. Когда система выдавала идею, она оценила шанс "
                      f"примерно в {k} из 100, что{till} цена {'будет выше' if up else 'будет ниже'} {_money(tgt)}. "
                      f"Это {verdict_words}. Оговорка: оценка шанса — мнение модели, умение угадывать ещё не доказано."]
    if разбор or смерть:
        L += ["", "Что против идеи. Перед этим идею атаковал «противник» — другая модель, которой поручено найти "
                  "самое сильное возражение."]
        if plain and plain.get("против"):
            L.append("Его довод простыми словами: " + plain["против"])
        elif разбор:
            L.append("Его довод дословно: «" + _clean_text(_humanize_tickers(разбор, names), 300).rstrip(".") + "».")
        L.append("Раньше такой довод сам закрывал идею. Теперь это главный риск, который ты взвешиваешь сам.")
    if kills and (not cur or cur.get("план_действует")):          # у недействующей идеи условия выхода — шум
        L += ["", "Когда выходить сразу, не дожидаясь стопа: " + "; ".join(kills) + "."]
    st = (cur or {}).get("статус")
    L.append("")
    if cur and not cur.get("план_действует"):
        last = cur.get("последняя_цена")
        last_s = (f" Последняя цена закрытия — {_money(last[1])} ({_ru_date(last[0])})."
                  if isinstance(last, (list, tuple)) and len(last) == 2 else "")
        L.append("Что сейчас. " + _STATUS_PLAIN.get(st, f"Статус идеи — «{st}», покупать сейчас нельзя.").format(
            entry=_money(entry), stop=_money(stp)) + last_s)
        L += ["", "Что делать. Сейчас ничего не покупать. Расчёт выше — чтобы ты видел, как устроена ставка."]
    else:
        L += ["Что сейчас. План в силе: проверка свежих цен кодом не нашла причин его отменить.", "",
              (f"Что делать. Если ты сам считаешь, что хороший исход случится чаще, чем в {n} из 100 случаев, — "
               if n else "Что делать. Если веришь в хороший исход — ") + f"ставка разумна, покупай не дороже {_money(entry)} и сразу ставь выход на {_money(stp)}. "
              f"Если не веришь — мимо. Решение за тобой."]
    L.append("Это исследовательский инструмент, не инвестиционная рекомендация.")
    return "\n".join(L), bs


def run_deep_dive(mode="auto", now=None, queue_path=None, notices_path=None,
                  passports_dir=None, challenge_fn=None, max_per_run=MAX_PER_RUN,
                  ideas_dir=None, dry=False, status_dir=None, challenges_dir=None, bet_sheet=None, plain_fn=None):
    """Продвинуть очередь прокачки: день 1 (испытание) → по вердикту: УСТОЯЛА — экономика сразу в тот же
    прогон; РАЗБИТА/ВЕТО — идея «отклонена», плана нет; неясно — «нужны данные», плана нет (VQ-4).

    dry=True («--show»): ничего не пишет (ни заметок, ни стадий, ни паспортов, ни протокола
    разбора) — возвращает в итог["тексты"] ровно то, что ушло бы владельцу; для идеи в очереди
    показывает сразу оба дня (день-2 — по итогам дня-1 этого же показа)."""
    now = now or _now()
    today = now.date().isoformat()
    if bet_sheet is None:
        bet_sheet = _bet_sheet_flag()
    итог = {"ts": now.isoformat(timespec="seconds"), "продвинуто": 0, "день1": 0, "день2": 0,
            "пропущено": [], "ошибки": [], "тексты": []}
    state = queue_state(queue_path)
    # идеи охотника (главная поверхность с 02.11) — первыми, внутри — по времени нажатия
    for (run_id, asset), st in sorted(state.items(), key=lambda kv: (not str(kv[0][0]).startswith("h_"),
                                                                     kv[1]["last_ts"] or "")):
        if итог["продвинуто"] >= max_per_run:
            break
        stage, last_day = st["stage"], (st["last_ts"] or "")[:10]
        if stage == "day2":
            continue
        card = None
        if str(run_id).startswith("h_"):
            card = hunter_card(run_id, ideas_dir)
            if card is None:
                итог["ошибки"].append({"asset": asset, "этап": stage,
                                       "ошибка": "нет карточки идеи охотника — без неё прокачка "
                                                 "разобрала бы чужую идею из старой воронки"})
                if not dry:
                    _notice("Прокачка " + _asset_label(asset) + " не запущена: на сервере нет карточки "
                            "этой идеи охотника. Служебное: файл reports/ideas/idea_" + str(run_id)[2:]
                            + ".card.json.", notices_path, now)
                continue
        try:
            if stage == "queued":
                ch_fn = challenge_fn
                if ch_fn is None:
                    from orchestrator import challenge as CH

                    def ch_fn(a, candidate=None):
                        return CH.run_challenge(DOUBT_DAY1, asset=None if candidate else a,
                                                candidate=candidate, mode=mode, write=not dry)
                ch = (ch_fn(asset, candidate=candidate_from_hunter_card(card)) if card is not None
                      else ch_fn(asset)) or {}
                if "ОТКАЗ" in ch:
                    итог["ошибки"].append({"asset": asset, "этап": "day1", "ошибка": str(ch["ОТКАЗ"])})
                    continue
                _верд, _разбор, смерть = _extract_challenge(ch)   # боевая структура протокола
                names = {asset: card["name"]} if card and card.get("name") else None
                if bet_sheet and card is not None:
                    _bet_sheet_step(asset, run_id, card, ch, now, итог, dry, queue_path, notices_path,
                                    passports_dir, ideas_dir, status_dir, names,
                                    plain_fn=plain_fn if plain_fn is not None else
                                    (lambda th, ob: plain_retell(th, ob, mode=mode)))
                    итог["продвинуто"] += 1
                    continue
                text = _day1_text(asset, ch, now, label=_card_label(asset, card), names=names)
                viol = _lint_or_fallback(text)
                if viol:
                    итог["ошибки"].append({"asset": asset, "этап": "day1", "ошибка": f"линт: {viol[:2]}"})
                    continue
                cls = verdict_class(_верд)
                day1_rec = {"ts": now.isoformat(timespec="seconds"), "run_id": run_id, "asset": asset,
                            "stage": "day1", "challenge_run_id": ch.get("run_id"),
                            "резюме": _разбор[:2000], "смерть": list(смерть)[:5],
                            "вердикт": str(_верд)[:80], "класс": cls}
                if cls != "positive":
                    text = _day1_closed_text(asset, _card_label(asset, card), _верд, cls, _разбор, смерть, names)
                    viol = _lint_or_fallback(text)
                    if viol:
                        итог["ошибки"].append({"asset": asset, "этап": "day1", "ошибка": f"линт: {viol[:2]}"})
                        continue
                if dry:
                    итог["тексты"].append(text)
                else:
                    _notice(text, notices_path, now)
                    _append(queue_path or QUEUE, day1_rec)
                    if card is not None:
                        _set_idea_status(run_id, cls, _верд, _разбор, ch.get("run_id"), ideas_dir, status_dir, now)
                итог["день1"] += 1
                if cls != "positive":
                    if not dry:
                        _append(queue_path or QUEUE, {"ts": now.isoformat(timespec="seconds"), "run_id": run_id,
                                                      "asset": asset, "stage": "closed",
                                                      "итог": "отклонена" if cls == "negative" else "нужны данные"})
                    итог.setdefault("закрыто", []).append({"asset": asset, "вердикт": _верд, "класс": cls})
                    итог["продвинуто"] += 1
                    continue
                st = {"day1": day1_rec}                               # устояла → экономика СРАЗУ
            if True:                                                  # day1 → day2 (тот же прогон или позже)
                d1 = st.get("day1") or {}
                cls1 = d1.get("класс") or verdict_class(d1.get("вердикт") or challenge_verdict(
                    d1.get("challenge_run_id"), challenges_dir))
                if cls1 != "positive":
                    # запись дня 1 до 09.11 без вердикта: разбор не устоял или неясен → плана нет
                    v1 = d1.get("вердикт") or challenge_verdict(d1.get("challenge_run_id"), challenges_dir) or "нет итога"
                    text = _day1_closed_text(asset, _card_label(asset, card), v1, cls1, d1.get("резюме") or "",
                                             d1.get("смерть") or [], {asset: card["name"]} if card and card.get("name") else None)
                    if dry:
                        итог["тексты"].append(text)
                    else:
                        _notice(text, notices_path, now)
                        _append(queue_path or QUEUE, {"ts": now.isoformat(timespec="seconds"), "run_id": run_id,
                                                      "asset": asset, "stage": "closed",
                                                      "итог": "отклонена" if cls1 == "negative" else "нужны данные"})
                        if card is not None:
                            _set_idea_status(run_id, cls1, v1, d1.get("резюме") or "", d1.get("challenge_run_id"),
                                             ideas_dir, status_dir, now)
                    итог.setdefault("закрыто", []).append({"asset": asset, "вердикт": v1, "класс": cls1})
                    итог["продвинуто"] += 1
                    continue
                push, md = _day2_passport(asset, run_id, d1, now, card=card, status_ideas_dir=ideas_dir,
                                          status_dir=status_dir)
                viol = _lint_or_fallback(push)
                if viol:
                    итог["ошибки"].append({"asset": asset, "этап": "day2", "ошибка": f"линт: {viol[:2]}"})
                    continue
                fname = f"{str(asset).replace('.', '_')}_{now.strftime('%Y%m%d')}.md"
                if dry:
                    итог["тексты"].append(push)
                else:
                    d = pathlib.Path(passports_dir or PASSPORTS)
                    d.mkdir(parents=True, exist_ok=True)
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


def _bet_sheet_step(asset, run_id, card, ch, now, итог, dry, queue_path, notices_path, passports_dir,
                    ideas_dir, status_dir, names, plain_fn=None):
    """Один шаг «Качаем» в режиме листа ставки: суд → риск в тексте, экономика кодом, статус не трогаем."""
    from mathlib import kelly as K
    try:
        from ops import idea_revision as IR
    except ImportError:
        import idea_revision as IR
    size = K.position_size(0.5, 1.0, CAPITAL_USD, gate_passed=False)
    amount = size.get("amount_usd") or CAPITAL_USD * 0.005
    cur, plan = None, _hunter_plan_lines(card)
    stem = IR.stem_for_run_id(run_id, ideas_dir)
    if stem:
        cur = IR.current(stem, card=card, today=now.date().isoformat(), out_dir=status_dir)
        plan = IR.plan_or_archive(card, cur, plan)
    вердикт0, разбор0, _ = _extract_challenge(ch or {})
    who = _flat(card.get("role"), 300)
    plain = (plain_fn((f"Кто это: {who}. " if who else "") + _flat(card.get("thesis"), 1500), _clean_text(разбор0, 1500))
             if plain_fn else None)
    kills = _hunter_kills(card)[:2]
    text, bs = _bet_sheet_text(asset, card, ch, cur, amount, plan, kills, names, plain=plain)
    viol = _lint_bet_sheet(text)
    if viol:
        итог["ошибки"].append({"asset": asset, "этап": "лист ставки", "ошибка": f"линт: {viol[:2]}"})
        return
    вердикт, разбор, смерть = _extract_challenge(ch)
    fname = f"{str(asset).replace('.', '_')}_{now.strftime('%Y%m%d')}.md"
    итог.setdefault("листы", []).append({"asset": asset, "вердикт": вердикт, "лист": bs})
    if dry:
        итог["тексты"].append(text)
        return
    d = pathlib.Path(passports_dir or PASSPORTS)
    d.mkdir(parents=True, exist_ok=True)
    (d / fname).write_text(f"# Лист ставки — {_card_label(asset, card)}\nСоздан: {now.date().isoformat()} · "
                           f"прогон {run_id}\n\n" + text + "\n\n## Расчёт (mathlib/bet_sheet)\n```\n"
                           + json.dumps(bs, ensure_ascii=False, indent=1) + "\n```\n", encoding="utf-8")
    _notice(text, notices_path, now)
    ts = now.isoformat(timespec="seconds")
    _append(queue_path or QUEUE, {"ts": ts, "run_id": run_id, "asset": asset, "stage": "day1",
                                  "challenge_run_id": ch.get("run_id"), "резюме": разбор[:2000],
                                  "смерть": list(смерть)[:5], "вердикт": str(вердикт)[:80],
                                  "класс": verdict_class(вердикт), "режим": "лист ставки"})
    _append(queue_path or QUEUE, {"ts": ts, "run_id": run_id, "asset": asset, "stage": "day2",
                                  "passport": fname, "режим": "лист ставки"})
    итог["день1"] += 1
    итог["день2"] += 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="V3 «качаем 2 дня»: продвижение очереди прокачки")
    ap.add_argument("--mode", default="auto", choices=["auto", "live", "mock"])
    ap.add_argument("--show", action="store_true",
                    help="показать владельцу-тексты обоих дней БЕЗ записи (заметки/стадии/паспорта)")
    ap.add_argument("--bet-sheet", action="store_true",
                    help="режим листа ставки (как при флаге deep_dive_bet_sheet) — для ручного показа")
    a = ap.parse_args(argv)
    r = run_deep_dive(mode=a.mode, dry=a.show, bet_sheet=True if a.bet_sheet else None)
    for t in r.get("тексты") or []:
        print("\n" + "─" * 60 + "\n" + t)
    for e in r.get("ошибки") or []:
        print("[ошибка]", e)
    print(f"[deep_dive] продвинуто {r['продвинуто']} (день1 {r['день1']}, день2 {r['день2']}), "
          f"пропущено {len(r['пропущено'])}, ошибок {len(r['ошибки'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
