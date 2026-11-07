# -*- coding: utf-8 -*-
"""Перепрогон исправленного процесса на исходных карточках DHT / MP / FSLR (аудит 07.11).

Только чтение: карточки не меняются (их отпечатки сверяются до и после). Что делает:
  1. шлагбаум idea_gate в строгом режиме доказательств (как для новой охоты);
  2. предпросмотр пуша hunter_seal.preview --strict-evidence (итоговый текст с оговорками кода);
  3. проверка кодом настоящих ответов агента из journal/chat_agent.jsonl (06.11);
  4. время отчёта MP 05.11 → сессия и ход до/после (mathlib.event_timing) по снимку закрытий;
  5. арифметика DHT (бронь 4-го квартала) — пересчёт и разбор, что в ней учтено.
Запуск: python3 reports/review_2026-11-07/rerun_audit.py > reports/review_2026-11-07/rerun_result.json
"""
import datetime
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ops import idea_gate as G, hunter_seal as HS, claim_check as CC, evidence_check as EV  # noqa: E402
from mathlib import event_timing as ET  # noqa: E402

IDEAS = ROOT / "reports" / "ideas"
STEMS = {"MP": "idea_2026-11-02_MP", "DHT": "idea_2026-11-03_DHT", "FSLR": "idea_2026-11-04_FSLR"}


def load(stem):
    return json.loads((IDEAS / (stem + ".card.json")).read_text(encoding="utf-8"))


out = {"запуск": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "идеи": {}}
before = {t: HS.card_sha256(load(s)) for t, s in STEMS.items()}
for t, stem in STEMS.items():
    c = load(stem)
    hd = HS.hunt_date_from_name(stem)
    g = G.check(c, today=hd, horizon_from=hd, evidence=True)
    pv = HS.preview(IDEAS / stem, strict_evidence=True)
    ev = EV.check(c)
    out["идеи"][t] = {
        "шлагбаум_строгий": {"pass": g["pass"], "брак_доказательств": [f for f in g["fails"] if f.startswith("доказательства")],
                             "прочий_брак": [f for f in g["fails"] if not f.startswith("доказательства")]},
        "цель": ev["target"], "оговорки_в_пуш": ev["labels"],
        "предпросмотр_проблем": len(pv["problems"]),
        "абзац_оговорок": next((b for b in pv["text"].split("\n\n") if b.startswith("Оговорки проверки")), None)}

# 3. настоящие ответы агента 06.11
sealed = {"DHT": 0.3, "MP": 0.15, "FSLR": 0.15}
chats = []
for line in (ROOT / "journal" / "chat_agent.jsonl").read_text(encoding="utf-8").splitlines():
    r = json.loads(line)
    if not r.get("reply"):
        continue
    chats.append({"ts": r["ts"], "вопрос": r["question"][:120],
                  "находки": [{"code": f["code"], "где": f["where"]} for f in CC.check(r["reply"], r["tools"], sealed)]})
out["ответы_агента_06_11"] = chats

# 4. MP: отчёт 05.11 после закрытия
snap = json.loads((ROOT / "reports" / "prices" / "closes_latest.json").read_text(encoding="utf-8"))
mp = {d: c for d, c in snap["symbols"]["MP.US"]}
out["MP_время_отчёта"] = ET.reaction("2026-11-05T21:05:00Z", mp)
out["MP_время_отчёта"]["оговорка"] = ("точное время публикации не проверено: «после закрытия» — по анонсу компании "
                                     "(факт карточки №17); 21:05 UTC — условная точка внутри окна после закрытия")

# 5. DHT: арифметика брони
days, share, tce = 2055, 0.78, 225_200
costs = {"содержание судов": 18.6, "амортизация": 27.8, "офис": 5.6, "проценты нетто": 6.0}
booked = share * days * tce / 1e6
out["DHT_расчёт"] = {
    "забронированная_выручка_TCE_млн": round(booked, 1),
    "Q3_для_сравнения_млн": round(days * 141_300 / 1e6, 1),
    "издержки_Q2_без_рейсовых_млн": round(sum(costs.values()), 1), "издержки_в_карточке_млн": 61.0,
    "прибыль_на_акцию_оценка": round((booked - 61.0) / 161.235573, 2),
    "выводы": [
        "TCE ($225 200 в сутки) — заработок уже ЗА ВЫЧЕТОМ рейсовых расходов (топливо, порты); в карточке это "
        "учтено: в факте за 2-й квартал рейсовые $29.9 млн вычтены из выручки ДО перехода к TCE-выручке, а $58–61 млн — "
        "только содержание судов, амортизация, офис и проценты. Двойного вычета рейсовых расходов нет.",
        "Слово «выручка рейсов» неточно: это выручка на базе TCE (net voyage revenue), а не выручка из отчёта о прибылях.",
        "2 055 судо-дней — факт 3-го квартала; в 4-м квартале тоже 92 календарных дня, но число дней флота зависит от "
        "сделок с судами и доков — это ДОПУЩЕНИЕ, в карточке оно названо.",
        "Налоги, изменение числа акций и разовые статьи в расчёте не учтены — не проверено.",
        "Связь прибыли квартала с целью $27 в карточке не раскрыта (target_basis нет) — цель предварительная."]}

after = {t: HS.card_sha256(load(s)) for t, s in STEMS.items()}
out["исходные_карточки_не_изменены"] = before == after
print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
