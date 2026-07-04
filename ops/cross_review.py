#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ops/cross_review.py — кросс-вендорное ревью этапа (гейт-цикл §24: stage-review + КРОСС-ВЕНДОР).

Восстановление утраченного scratchpad/codex_system_review.py (ревью 04.07 установило, что файлы
scratchpad не сохранились) — теперь ПОСТОЯННЫМ скриптом. Отправляет указанные файлы + задание
модели ЧУЖОГО семейства (роль critic из config/models.yaml — openai) через штатный
orchestrator.openrouter.LiveClient: стоимость логируется в journal/costs.jsonl, бюджет-гард
работает как у всех LLM-вызовов.

Запуск (от oracle, с .env):
    sudo -u oracle bash -c 'cd /home/oracle/oracle && set -a && . ./.env && set +a && \
        .venv/bin/python ops/cross_review.py --task "П1-гейт: ..." файл1 файл2 ...'
Вывод — в stdout и reports/cross_review_<ts>.md.
"""
import argparse
import datetime
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.openrouter import LiveClient  # noqa: E402

SYSTEM = (
    "Ты — независимый кросс-вендорный ревьюер кода инвестиционной системы «Оракул». "
    "Твоя задача — АДВЕРСАРНО искать реальные дефекты: ошибки математики/логики, краевые случаи, "
    "нарушения контрактов, нечестность чисел (П8: каждое число со ссылкой на расчёт; «нет данных» "
    "— честный ответ). НЕ хвали. Формат ответа: строка «ВЕРДИКТ: ПРОЙДЕН» или «ВЕРДИКТ: НЕ ПРОЙДЕН», "
    "затем находки списком: [BLOCKER|HIGH|LOW] файл:строка — суть — конкретный контрпример/сценарий. "
    "Если находок нет — так и скажи. Отвечай по-русски."
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True, help="описание этапа/что ревьюим")
    ap.add_argument("files", nargs="+", help="файлы проекта для ревью")
    args = ap.parse_args()

    parts = [f"ЗАДАНИЕ ЭТАПА:\n{args.task}\n"]
    for f in args.files:
        p = ROOT / f
        parts.append(f"\n===== ФАЙЛ: {f} =====\n{p.read_text(encoding='utf-8')}")
    user = "\n".join(parts)

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    client = LiveClient(run_id=f"cross_review_{ts}")
    res = client.complete("critic", SYSTEM, user,
                          agent_id="cross_review", output_kind="critic_review")
    out = res.get("text") or ""
    model = res.get("model")
    header = f"# Кросс-вендорное ревью {ts}\n\nМодель: {model} (роль critic, чужое семейство)\nЗадание: {args.task}\nФайлы: {', '.join(args.files)}\n\n---\n\n"
    dst = ROOT / "reports" / f"cross_review_{ts}.md"
    dst.write_text(header + out, encoding="utf-8")
    print(f"модель: {model}\nотчёт: {dst}\n\n{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
