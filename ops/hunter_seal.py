# -*- coding: utf-8 -*-
"""ops/hunter_seal.py — ПЕЧАТЬ идей охотника на СЕРВЕРЕ (П16) и «счёт метода» (архитектура 02.11).

Облачный охотник журналы не пишет (П16: журналы только с сервера). Он кладёт рядом с пушем
карточку idea_<дата>_<тикер>.card.json со §9-спекой; бот на сервере ПЕРЕД доставкой пуша
печатает её в journal/predictions.jsonl отдельным герметичным треком kind=hunter_idea
(к §11-деньгам не приближается), а в конец пуша код дописывает строку о печати и счёт метода.
Так идея охотника впервые попадает в контур честности: через 3–13 недель резолвер сверит
её с ценой закрытия, и владелец увидит «идеи охотника сбывались в X% (K из N)».

Сырая вероятность охотника идёт ТОЛЬКО в журнал для Brier — владельцу не печатается
(Инв#6: вероятность владельцу — из расчёта по треку, а не со слов LLM).
"""
import datetime
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

KIND = "hunter_idea"
TRACK = "hunter_idea"
# «Вето надо заработать» (разбор 02.11): кандидаты, отвергнутые судом охотника, печатаются ТЕНЬЮ
# в свой трек — владельцу не показываются; через 20+ сверенных видно, отличает ли суд плохое от
# хорошего (в прошлый раз разбитые судом сбывались в 52.6% — как все).
SHADOW_KIND = "hunter_killed"
MAX_SEAL_LAG_DAYS = 3        # П16: карточку старше 3 дней не печатаем — это было бы задним числом
DEDUP_FIELDS = ("track", "asset", "direction", "threshold", "resolve_by")
PRED = ROOT / "journal" / "predictions.jsonl"
OUTC = ROOT / "journal" / "outcomes.jsonl"
_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря")


def card_path_for(push_path):
    p = pathlib.Path(push_path)
    return p.with_name(p.name[:-len(".push.txt")] + ".card.json")


def spec_from_card(card, idea_file=None, kind=KIND):
    """Карточка охотника → прогноз для mathlib.sealing.seal (или ValueError с причиной)."""
    s = (card or {}).get("spec9") or {}
    try:
        thr = float(s.get("threshold"))
        prob = float(s.get("probability"))
    except (TypeError, ValueError):
        raise ValueError("в §9-спеке порог/вероятность не числа")
    rb = str(s.get("resolve_by") or "")[:10]
    datetime.date.fromisoformat(rb)                    # ValueError при битой дате
    asset = str(s.get("asset") or card.get("ticker") or "").upper()
    return {
        "kind": kind, "track": kind,
        "asset": asset, "direction": str(s.get("direction") or "").lower(),
        "threshold": thr, "resolve_by": rb + "T20:00:00+00:00",
        "price_source": s.get("price_source") or f"EODHD close {asset}",
        "probability": prob,
        "вероятность_источник": "охотник (LLM, сырая, не калибрована — владельцу не показывается)",
        "mechanism_id": card.get("mechanism_id"), "zone": card.get("zone"),
        "idea_file": idea_file,
        "spec_ref": "архитектура охоты 02.11; §9 разрешимость; П16 форвард-онли",
    }


def _idea_stem(name):
    """idea_<дата>_<ТИКЕР>[.push.txt|.card.json|.shadow.json] → idea_<дата>_<ТИКЕР>."""
    n = str(name or "")
    for suf in (".push.txt", ".card.json", ".shadow.json", ".md"):
        if n.endswith(suf):
            return n[:-len(suf)]
    return n


def _server_last_close(asset, con=None):
    """Последняя цена закрытия по СЕРВЕРНЫМ котировкам (Инв#6: не со слов LLM) — с добором истории
    для бумаги вне базы (data/eodhd.ensure_history), если есть ключ EODHD. → (цена, дата) | (None, None)."""
    import os
    import sqlite3
    db = ROOT / "storage" / "oracle.db"
    own = con is None
    try:
        if con is None:
            if not db.exists():
                return None, None
            con = sqlite3.connect(db, timeout=30)
        key = os.environ.get("EODHD_API_KEY")
        if key:
            try:
                sys.path.insert(0, str(ROOT / "data"))
                import eodhd as E
                E.ensure_history(con, [asset], key)
            except Exception:                          # noqa: BLE001 — добор best-effort
                pass
        row = con.execute("SELECT close, date FROM quotes WHERE symbol=? AND close IS NOT NULL "
                          "ORDER BY date DESC LIMIT 1", (asset,)).fetchone()
        return (float(row[0]), str(row[1])[:10]) if row else (None, None)
    except Exception:                                  # noqa: BLE001
        return None, None
    finally:
        if own and con is not None:
            con.close()


def server_check(card, *, idea_file, today=None, last_close_fn=None):
    """ШЛАГБАУМ КОДОМ НА СЕРВЕРЕ (ревью 02.11: в облаке он исполнялся на слово LLM). Карточка
    перепроверяется ops/idea_gate.check с СЕРВЕРНОЙ датой; цена закрытия для якоря порога берётся
    из серверных котировок (если есть). → (ok, причины, карточка_с_серверной_ценой)."""
    import copy
    try:
        from ops import idea_gate as G
    except ImportError:
        import idea_gate as G
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    hd = hunt_date_from_name(idea_file)
    if hd is None:
        return False, ["в имени файла нет даты охоты — возраст карточки не проверить"], card
    if (today - hd).days > MAX_SEAL_LAG_DAYS:
        return False, [f"карточка от {hd} старше {MAX_SEAL_LAG_DAYS} дней — задним числом не печатаем"], card
    c2 = copy.deepcopy(card)
    s = c2.get("spec9") if isinstance(c2.get("spec9"), dict) else None
    if s is not None:
        asset = str(s.get("asset") or c2.get("ticker") or "").upper()
        px, pxd = (last_close_fn or _server_last_close)(asset)
        if px is not None:
            s["last_close"], s["last_close_date"] = px, pxd
            s["цена_источник"] = "серверные котировки EODHD"
    res = G.check(c2, today=today)
    return res["pass"], res["fails"], c2


def hunt_date_from_name(name):
    """idea_<YYYY-MM-DD>_<ТИКЕР>.* → date | None."""
    try:
        return datetime.date.fromisoformat(str(name)[len("idea_"):len("idea_") + 10])
    except ValueError:
        return None


def seal_card(card, *, idea_file=None, predictions_path=None, sealable_fn=None, kind=KIND,
              today=None):
    """→ (record|None, причина|None). Дубль ставки → (None, 'уже запечатано') — не ошибка.
    П16: карточка старше MAX_SEAL_LAG_DAYS от даты охоты (из имени файла) не печатается —
    за это время цена уже ушла, печать была бы задним числом."""
    from mathlib import sealing as SEAL
    hd = hunt_date_from_name(idea_file) if idea_file else None
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    if hd is None:                                  # ревью 02.11: без даты лаг-гард был fail-open
        return None, "в имени карточки нет даты охоты — возраст не проверить, печать отклонена"
    if (today - hd).days > MAX_SEAL_LAG_DAYS:
        return None, (f"карточка от {hd} старше {MAX_SEAL_LAG_DAYS} дней — печать задним числом "
                      "запрещена")
    stem = _idea_stem(idea_file)
    if any(r.get("kind") == kind and _idea_stem(r.get("idea_file")) == stem
           for r in _read_jsonl(predictions_path or PRED)):
        return None, "уже запечатано"               # одна печать на идею (и при «обновлённой версии»)
    try:
        spec = spec_from_card(card, idea_file=idea_file, kind=kind)
    except ValueError as e:
        return None, f"спека не разрешима: {e}"
    if sealable_fn is None:
        try:
            from orchestrator import universe_resolver as U
            sealable_fn = U.is_sealable
        except Exception:                              # noqa: BLE001
            sealable_fn = None
    if sealable_fn is not None and not sealable_fn(spec["asset"]):
        return None, f"нет истории цен {spec['asset']} в базе — сверять нечем (§9)"
    try:
        rec = SEAL.seal(spec, path=predictions_path or PRED, dedup_fields=DEDUP_FIELDS)
    except ValueError as e:
        return None, str(e)
    if rec is None:
        return None, "уже запечатано"
    return rec, None


def _read_jsonl(path):
    p = pathlib.Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def method_score(predictions_path=None, outcomes_path=None, kind=KIND):
    """Счёт МЕТОДА охотника из журналов (П16: только из журнала, join по hash)."""
    preds = [p for p in _read_jsonl(predictions_path or PRED) if p.get("kind") == kind]
    outs = {o.get("hash"): o for o in _read_jsonl(outcomes_path or OUTC)
            if o.get("hash") and o.get("outcome") in (0, 1)}
    n_out = [outs[p["hash"]] for p in preds if p.get("hash") in outs]
    return {"идей": len(preds), "исходов": len(n_out),
            "попаданий": sum(int(o["outcome"]) for o in n_out)}


def _human_date(iso):
    d = datetime.date.fromisoformat(str(iso)[:10])
    return f"{d.day} {_MONTHS[d.month - 1]}"


def footer(rec, why, score):
    """Строки, которые КОД дописывает в пуш: что запечатано и счёт метода (без вероятностей)."""
    lines = []
    if rec:
        side = "выше" if rec["direction"] == "above" else "ниже"
        base = rec["asset"].split(".")[0]
        lines.append(f"Прогноз запечатан в журнал: {base} закроется {side} {rec['threshold']:g} "
                     f"к {_human_date(rec['resolve_by'])}. В этот день сверю по цене закрытия и "
                     "пришлю итог сюда — да или нет; запись задним числом не правится.")
    elif why == "уже запечатано":
        lines.append("Проверяемый прогноз по этой идее уже записан в журнал раньше — сверю в срок.")
    elif why:
        plain = re.sub(r"\s*\((?:§|П)\d+[^)]*\)", "", str(why))
        plain = re.sub(r"\b([A-Z][A-Z0-9\-]{0,9})\.US\b", r"\1", plain).strip().rstrip(".")
        lines.append(f"Счёт по этой идее не веду: {plain}.")
    n, k, total = score["исходов"], score["попаданий"], score["идей"]
    if n == 0:
        lines.append(f"Счёт метода охотника: идей в журнале {total}, сверенных пока нет — "
                     "первые исходы через 3–13 недель.")
    else:
        lines.append(f"Счёт метода охотника: сбылось {k} из {n} сверенных"
                     + (" — выборка мала, доверять рано." if n < 30 else "."))
    return lines


def outcome_message(outcome, score):
    """Итог сверки идеи охотника → сообщение владельцу (обратная связь «сбылось/нет»)."""
    base = str(outcome.get("asset") or "?").split(".")[0]
    side = "выше" if outcome.get("direction") == "above" else "ниже"
    obs = outcome.get("observed_value")
    ok = int(outcome.get("outcome")) == 1
    when = _human_date(outcome.get("resolve_by")) if outcome.get("resolve_by") else "дату сверки"
    obs_txt = f"{float(obs):g}" if isinstance(obs, (int, float)) else "нет данных"
    n, k = score["исходов"], score["попаданий"]
    return (f"Итог идеи: ставил, что {base} закроется {side} {float(outcome.get('threshold')):g} "
            f"к {when}. Закрытие — {obs_txt}: {'сбылось' if ok else 'не сбылось'}.\n"
            f"Счёт метода охотника: сбылось {k} из {n} сверенных"
            + (" — выборка мала, доверять рано." if n < 30 else "."))
