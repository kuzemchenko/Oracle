# -*- coding: utf-8 -*-
"""orchestrator/debate.py — состязательный контур §4 блок E (этап 5 воронки §6).

Работает на ОДНОЙ идее-кандидате: Генератор → Критик/Red Team → Адвокат → Reviewer данных →
СЛЕПОЙ Судья по версионируемой рубрике (config/rubric.yaml).

Инварианты блока E (П10 «состязательность» + требования Нед.7):
  • СЛЕПОТА судьи: аргументы подаются под нейтральными метками A/B/C… без указания модели и
    без ярлыка роли (генератор/критик/адвокат); порядок РАНДОМИЗИРОВАН (seed из run_id+актив —
    воспроизводимо и аудируемо). Карта «метка→источник» хранится в протоколе для аудита, но
    судье НЕ передаётся.
  • РАЗВЯЗКА СЕМЕЙСТВ: судья и ВСЯ цепочка его фолбеков — НЕ из семейства генератора текущей
    идеи (openrouter.filtered_chain(exclude_family=...)). Это код, не доверие к модели.
  • РУБРИКА — версионируемый файл; судья оценивает строго по ней; вердикт УСТОЯЛА/РАЗБИТА
    пересчитывается КОДОМ из баллов рубрики vs verdict.break_threshold (не на слово судьи).
  • ОБЯЗАТЕЛЬНЫЕ ВОПРОСЫ §4 («кто продаёт нам и почему он неправ», «почему возможность ещё
    существует») — без ответа на оба процедурное вето §5.6 (идея не проходит).
"""
import json
import random
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUBRIC_PATH = ROOT / "config" / "rubric.yaml"

import sys
sys.path.insert(0, str(ROOT))
from orchestrator import agents as A          # noqa: E402
from orchestrator import openrouter as OR     # noqa: E402
from mathlib import base_rate as BR           # noqa: E402  (F2#17: детерминир. base_rate, Инв#6)


def load_rubric(path=RUBRIC_PATH):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _seed(run_id, asset):
    """Детерминированный seed для рандомизации порядка (воспроизводимость аудита)."""
    return int(__import__("hashlib").sha256(f"{run_id}|{asset}".encode()).hexdigest(), 16) % (2**32)


def _case_blob(d):
    return "```json\n" + json.dumps(d, ensure_ascii=False, indent=1, default=str) + "\n```"


def _user_for(agent_id, payload):
    """User-промпт дела дебатов (тикер из универсума виден явно — и для mock-детекции)."""
    return ("Дело состязательного контура (ОДНА идея-кандидат). Используй ТОЛЬКО поданные данные "
            "(П8). Тикеры — только из универсума.\n\n" + _case_blob(payload) +
            "\n\nВерни РОВНО один объект JSON по контракту из системного промпта.")


def _norm_direction(val):
    """Нормализует направление, выведенное генератором, к метке для дела судьи.
    Пусто/None → None (школа/контур направление не дали)."""
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    low = s.lower()
    if "лонг" in low or "long" in low:
        return "лонг"
    if "шорт" in low or "short" in low:
        return "шорт"
    if low.startswith("нет") or "no trade" in low or "нет идеи" in low:
        return "нет"
    return s


def _generator_family(models=None):
    cfg = OR.resolve_role("generator", models)
    return cfg.get("family") or OR.family_of(cfg["primary"], models)


def run_debate(candidate, ctx, client, *, run_id, costs=None, rubric=None, models=None,
               eval_context=None, user_doubt=None):
    """Прогон состязательного контура по одному кандидату.

    candidate: {актив, направление, тезис, разрешимость, школа, вероятность_школы, ...}
    eval_context: опц. СТОЙКО-НЕЙТРАЛЬНАЯ пометка контекста оценки для судьи (напр. §23.2б:
        «идентичности скрыты намеренно, оценивай СТРУКТУРУ рассуждения и работу с поданными
        данными»). НЕ раскрывает направление/исход; ловушку не вытягивает. В воронке = None.
    user_doubt: опц. возражение/сомнение владельца по этой идее (ad hoc разбор, см.
        orchestrator/challenge.py). Подаётся ДВАЖДЫ (решение пользователя §30): (а) критику —
        как обязательная линия атаки Red Team; (б) судье — как отдельный вопрос, на который тот
        обязан явно ответить, меняет ли возражение вердикт. Слепоту судьи и развязку семейств
        НЕ нарушает (это данные дела, не идентичность автора). В воронке = None.
    Возвращает dict-протокол дебатов: реплики ролей, СЛЕПОЕ дело судьи, вердикт, пересчёт.
    """
    rubric = rubric or load_rubric()
    models = models or OR.load_models()
    asset = candidate.get("актив")
    direction = candidate.get("направление")

    # срез по идее (без выдумок: то, что есть в ctx по активу)
    idea_slice = {
        "актив": asset, "направление": direction,
        "тезис_школы": candidate.get("тезис"), "школа": candidate.get("школа"),
        "разрешимость": candidate.get("разрешимость"),
        "котировка": ctx.get("quotes", {}).get(asset, {}).get("last") if ctx else None,
        "индикаторы": ctx.get("indicators", {}).get(asset) if ctx else None,
        "издержки": (costs or {}).get(asset),
        "calibration_status": (ctx or {}).get("calibration_status", {}).get("thresholds_calibrated"),
    }
    # P2#7: ПРОВЕРЯЕМОЕ дело каскада (событие-исток, механизм по звеньям, edge/r²/lag/порядок) — это
    # ФАКТЫ дела, не авторство, поэтому слепоту судьи (П10) не нарушает. Без них ревьюер по П8 клеймил
    # аргументы по событию как недоказанные, а судья снижал балл «нет первоисточника/нет расчёта».
    дело = candidate.get("дело_каскада")
    if дело:
        idea_slice["каскадное_дело_проверяемое"] = дело

    # 1. Генератор
    gen = A.call_agent("e_generator", ctx, client, user_prompt=_user_for("e_generator", idea_slice))
    gen_j = (gen.get("judgment") or {}) if gen.get("ok") else {}
    # F3#23 (§4.1): семейство генератора — из МОДЕЛИ, что реально ответила (пост-вызов), а НЕ из
    # config: генератор мог уйти в кросс-семейный фолбек, и развязка судьи должна считаться по факту.
    gen_family = (OR.family_of(gen["model"], models)
                  if gen.get("ok") and gen.get("model") else _generator_family(models))

    # Если школа не зафиксировала направление (напр. маскированный кейс §23.2(б)) — идею для
    # ДАЛЬНЕЙШЕГО контура задаёт направление, выведенное генератором, и его §9-формулировка.
    # Так дело судьи когерентно (нет «null на верхнем уровне против long внутри»). Когда школа
    # направление дала — поведение прежнее (eff_* == исходные).
    eff_direction = direction or _norm_direction(gen_j.get("направление"))
    # F2#17: base_rate — ДЕТЕРМИНИРОВАННАЯ эмпирическая частота направленного хода по истории цен
    # (mathlib/base_rate), а НЕ выдуманное генератором число (раньше gen_j['base_rate'] → Инв#6).
    # Нет серии/направления → None (П8), судье/прогнозу не передаём фикцию.
    base_rate = _empirical_base_rate(ctx, asset, eff_direction)
    eff_resolvability = candidate.get("разрешимость") or gen_j.get("разрешимость")
    down_slice = {**idea_slice, "направление": eff_direction, "разрешимость": eff_resolvability}

    # 2. Критик (видит гипотезу; возражение владельца — обязательная линия атаки).
    # F3#23 (§4.3 симметрия): критик — адверсарий генератора, развязываем по семейству (не из
    # семейства генератора), чтобы фолбек-коллизия не сделала критика той же моделью/семейством.
    crit_payload = {**down_slice, "гипотеза": _ok_judgment(gen)}
    if user_doubt:
        crit_payload["возражение_владельца_ОБЯЗАТЕЛЬНО_РАЗОБРАТЬ"] = user_doubt
    crit = A.call_agent("e_critic", ctx, client, user_prompt=_user_for("e_critic", crit_payload),
                        exclude_family=gen_family)
    crit_family = OR.family_of(crit["model"], models) if crit.get("ok") and crit.get("model") else None

    # 3. Адвокат (видит гипотезу + критику). Адвокат защищает тезис генератора — ему НЕ требуется
    # отличаться от него по семейству (не адверсарий), поэтому исключения не навязываем; его
    # семейство лишь учитываем в развязке судьи ниже.
    adv_payload = {**down_slice, "гипотеза": _ok_judgment(gen), "критика": _ok_judgment(crit)}
    adv = A.call_agent("e_advocate", ctx, client, user_prompt=_user_for("e_advocate", adv_payload))
    adv_family = OR.family_of(adv["model"], models) if adv.get("ok") and adv.get("model") else None

    # 4. СЛЕПОЕ дело: обезличиваем 3 аргумента, рандомизируем порядок. Строим ДО ревьюера —
    # чтобы ревьюер видел те же метки A/B/C (§4.4: роли не должны протечь судье через находки).
    blind_case, label_map = _build_blind_case(gen, crit, adv, run_id, asset)

    # 5. Reviewer данных (проверка фактов) — на ОБЕЗЛИЧЕННОМ деле (§4.4): его находки ссылаются на
    # метки A/B/C, а не на роли «гипотеза/критика/адвокат», иначе судья деанонимизировал бы дело.
    rev_payload = {**down_slice, "аргументы_обезличенно_в_случайном_порядке": blind_case}
    rev = A.call_agent("e_data_reviewer", ctx, client, user_prompt=_user_for("e_data_reviewer", rev_payload))
    judge_payload = {
        "идея": {"актив": asset, "направление": eff_direction, "разрешимость": eff_resolvability},
        "base_rate": base_rate,
        "аргументы_обезличенно_в_случайном_порядке": blind_case,
        "проверка_данных_reviewer": _review_findings(rev),
        "рубрика": {
            "version": rubric.get("version"),
            "scale": rubric.get("scale"),
            "criteria": [{"id": c["id"], "title": c["title"], "desc": c["desc"]}
                         for c in rubric.get("criteria", [])],
            "verdict": rubric.get("verdict"),
            "mandatory_questions": rubric.get("mandatory_questions"),
        },
        "напоминание": "ты НЕ знаешь, кто автор какого аргумента; суди по существу (§16.6)",
    }
    if дело:
        # проверяемое дело каскада судье ЯВНО (факты, не авторство — П10 цел): событие-исток+механизм+
        # edge/r²/lag. Чтобы вердикт «нет первоисточника/расчёта» выносился только когда их РЕАЛЬНО нет.
        judge_payload["каскадное_дело_проверяемое"] = дело
    if eval_context:
        judge_payload["контекст_оценки"] = eval_context
    if user_doubt:
        # Возражение владельца — отдельный вопрос судье (решение пользователя §30): судья обязан
        # явно учесть его при оценке рубрики и в выводе сказать, меняет ли оно вердикт.
        judge_payload["возражение_владельца"] = user_doubt
        judge_payload["напоминание"] += (
            "; владелец поднял возражение (поле 'возражение_владельца') — учти его в баллах "
            "рубрики и в выводе ЯВНО ответь, выдерживает ли идея это возражение")
    # П10 (F3#23 §4.3): судья (и вся его цепочка фолбеков) НЕ из семейств НИ ОДНОГО дебатёра,
    # чьи аргументы он слепо судит — генератора, критика И адвоката. Раньше исключался только
    # генератор → судья мог совпасть по семейству с критиком. Семейства — по факту (пост-вызов).
    debater_families = {f for f in (gen_family, crit_family, adv_family) if f}
    judge = A.call_agent("e_judge", ctx, client, user_prompt=_user_for("e_judge", judge_payload),
                         exclude_family=debater_families)

    verdict = _adjudicate(judge, rubric)

    return {
        "актив": asset, "направление": direction, "школа": candidate.get("школа"),
        "возражение_владельца": user_doubt,
        "base_rate": base_rate,
        "семейство_генератора": gen_family,
        "семейство_критика": crit_family,
        "семейство_адвоката": adv_family,
        "семейство_судьи": OR.family_of(judge.get("model", ""), models) if judge.get("ok") else None,
        "развязка_семейств_П10": {"исключено_у_судьи": sorted(debater_families),
                                  "судья_вне_дебатёров": (
                                      OR.family_of(judge.get("model", ""), models) not in debater_families
                                      if judge.get("ok") else None)},
        "реплики": {"генератор": gen, "критик": crit, "адвокат": adv, "reviewer_данных": rev, "судья": judge},
        "слепое_дело": {"метки_в_деле": [b["метка"] for b in blind_case],
                        "карта_меток_АУДИТ": label_map},  # судье НЕ передавалась
        "вердикт": verdict,
    }


def _empirical_base_rate(ctx, asset, direction):
    """F2#17: эмпирическая base_rate из истории цен ctx (Инв#6, П8). None — нет серии/направления."""
    qd = ((ctx or {}).get("quotes", {}) or {}).get(asset) or {}
    px = qd.get("adj_closes")
    if not px or not direction:
        return None
    rate, _n = BR.empirical_directional_base_rate(px, direction)
    return None if rate is None else round(rate, 4)


def _ok_judgment(rec):
    """Суждение агента для подачи следующему — или пометка, что агент не дал валидного ответа."""
    if rec.get("ok"):
        j = dict(rec["judgment"])
        j.pop("_output_kind", None)
        j.pop("_no_data", None)
        return j
    return {"_недоступно": rec.get("error", "агент не дал валидного ответа")}


def _review_findings(rev):
    if rev.get("ok"):
        j = rev["judgment"]
        return {"вердикт": j.get("вердикт"), "находки": j.get("находки", [])}
    return {"вердикт": "нет данных", "находки": [], "_ошибка": rev.get("error")}


def _build_blind_case(gen, crit, adv, run_id, asset):
    """Обезличить 3 аргумента нейтральными метками и рандомизировать порядок (слепота судьи).

    Возвращает (blind_case, label_map): blind_case — список {метка, аргумент} БЕЗ роли/модели;
    label_map — {метка: роль/модель} ТОЛЬКО для аудит-протокола (судье не отдаётся).
    """
    items = []
    for role, rec in (("генератор", gen), ("критик", crit), ("адвокат", adv)):
        items.append({"_роль": role, "_модель": rec.get("model"), "аргумент": _ok_judgment(rec)})
    rng = random.Random(_seed(run_id, asset))
    rng.shuffle(items)
    labels = ["A", "B", "C", "D", "E"]
    blind_case, label_map = [], {}
    for i, it in enumerate(items):
        mk = labels[i]
        blind_case.append({"метка": mk, "аргумент": it["аргумент"]})  # без _роль/_модель
        label_map[mk] = {"роль": it["_роль"], "модель": it["_модель"]}
    return blind_case, label_map


def _rubric_mean(judge_rec):
    """Средний балл рубрики из ответа судьи (для пересчёта вердикта кодом)."""
    if not judge_rec.get("ok"):
        return None
    rub = (judge_rec["judgment"] or {}).get("рубрика", {})
    scores = []
    for o in (rub.get("оценки") or []):
        b = o.get("балл")
        if isinstance(b, (int, float)) and not isinstance(b, bool):
            scores.append(float(b))
    return round(sum(scores) / len(scores), 4) if scores else None


def _mandatory_answered(judge_rec):
    """Оба обязательных вопроса §4 должны иметь непустой ответ (процедурное вето §5.6)."""
    if not judge_rec.get("ok"):
        return False, ["судья не дал валидного вердикта"]
    j = judge_rec["judgment"]
    missing = []
    for key, q in (("кто_продаёт_нам_и_почему_неправ", "кто продаёт нам"),
                   ("почему_возможность_ещё_существует", "почему возможность существует")):
        ans = str(j.get(key, "")).strip()
        if not ans or ans.lower() in ("нет данных", "—", "null", "none"):
            missing.append(q)
    return (not missing), missing


def _adjudicate(judge_rec, rubric):
    """Пересчёт вердикта КОДОМ: средний балл рубрики vs break_threshold + ворота обязательных вопросов.

    Не доверяем строке "вердикт" от судьи слепо — сверяем с баллами рубрики (защита §16.6 от
    «протекания»: модель могла написать УСТОЯЛА, но баллы её не подтверждают)."""
    thr = float(rubric.get("verdict", {}).get("break_threshold", 3.0))
    mean = _rubric_mean(judge_rec)
    answered, missing_q = _mandatory_answered(judge_rec)
    judge_says = (judge_rec["judgment"].get("вердикт") if judge_rec.get("ok") else None)

    if not answered:
        return {"исход": "ВЕТО", "причина": "процедурное вето §5.6: не отвечены обязательные вопросы",
                "пропущенные_вопросы": missing_q, "средний_балл_рубрики": mean,
                "судья_заявил": judge_says, "вероятность_судьи": None}

    if mean is None:
        return {"исход": "ВЕТО", "причина": "судья не вернул баллы рубрики",
                "средний_балл_рубрики": None, "судья_заявил": judge_says, "вероятность_судьи": None}

    code_verdict = "УСТОЯЛА" if mean >= thr else "РАЗБИТА"
    prob = (judge_rec["judgment"] or {}).get("вероятность")
    note = None
    if judge_says and judge_says != code_verdict:
        note = (f"расхождение: судья заявил {judge_says}, но средний балл рубрики {mean} "
                f"{'≥' if mean >= thr else '<'} порога {thr} → код фиксирует {code_verdict} (§16.6)")
    return {
        "исход": code_verdict,
        "средний_балл_рубрики": mean, "порог": thr,
        "судья_заявил": judge_says,
        "вероятность_судьи": prob if code_verdict == "УСТОЯЛА" else None,
        "примечание": note,
    }
