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


def run_deep_dive(mode="auto", now=None, queue_path=None, notices_path=None,
                  passports_dir=None, challenge_fn=None, max_per_run=MAX_PER_RUN,
                  ideas_dir=None, dry=False, status_dir=None, challenges_dir=None):
    """Продвинуть очередь прокачки: день 1 (испытание) → по вердикту: УСТОЯЛА — экономика сразу в тот же
    прогон; РАЗБИТА/ВЕТО — идея «отклонена», плана нет; неясно — «нужны данные», плана нет (VQ-4).

    dry=True («--show»): ничего не пишет (ни заметок, ни стадий, ни паспортов, ни протокола
    разбора) — возвращает в итог["тексты"] ровно то, что ушло бы владельцу; для идеи в очереди
    показывает сразу оба дня (день-2 — по итогам дня-1 этого же показа)."""
    now = now or _now()
    today = now.date().isoformat()
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


def main(argv=None):
    ap = argparse.ArgumentParser(description="V3 «качаем 2 дня»: продвижение очереди прокачки")
    ap.add_argument("--mode", default="auto", choices=["auto", "live", "mock"])
    ap.add_argument("--show", action="store_true",
                    help="показать владельцу-тексты обоих дней БЕЗ записи (заметки/стадии/паспорта)")
    a = ap.parse_args(argv)
    r = run_deep_dive(mode=a.mode, dry=a.show)
    for t in r.get("тексты") or []:
        print("\n" + "─" * 60 + "\n" + t)
    for e in r.get("ошибки") or []:
        print("[ошибка]", e)
    print(f"[deep_dive] продвинуто {r['продвинуто']} (день1 {r['день1']}, день2 {r['день2']}), "
          f"пропущено {len(r['пропущено'])}, ошибок {len(r['ошибки'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
