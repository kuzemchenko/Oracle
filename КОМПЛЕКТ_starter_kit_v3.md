# Стартовый набор «Оракул» v3 — все файлы одним документом

Этот документ заменяет oracle_starter_kit_v3.zip для знаний проекта (zip туда не грузится).
Каждый файл приведён целиком с его путём. Развернуть на машине можно двумя способами:
1) просто скачать zip из чата прямо на компьютер проекта и распаковать в корень oracle/;
2) или сказать Claude Code: «создай файлы стартового набора из документа КОМПЛЕКТ_starter_kit в знаниях проекта, пути и содержимое — как указано», затем chmod +x .claude/hooks/*.py


---

## Файл: `.claude/hooks/guard_journal.py`

````python
#!/usr/bin/env python3
"""PreToolUse-хук: запрещает изменять запечатанные журналы (П16, инвариант 3 CLAUDE.md).
Выход с кодом 2 = блокировка действия, сообщение из stderr возвращается Claude."""
import json, sys

PROTECTED = [
    "journal/predictions.jsonl",
    "journal/holdout_access.log",
    "journal/decisions_user.jsonl",
]

def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}
    text = " ".join(str(ti.get(k, "")) for k in ("file_path", "path", "command", "new_str", "old_str"))
    # Запись через bash тоже ловим (>>, >, sed -i, tee)
    for p in PROTECTED:
        if p in text:
            if tool == "Bash":
                cmd = str(ti.get("command", ""))
                # чтение разрешено, запись — нет
                if any(w in cmd for w in [">", "tee ", "sed -i", "truncate", "rm ", "mv "]):
                    print(f"БЛОКИРОВКА: {p} — запечатанный журнал, запись запрещена (П16). "
                          f"Добавление прогнозов — только через mathlib.seal() из оркестратора.", file=sys.stderr)
                    sys.exit(2)
            else:  # Edit / Write / MultiEdit / NotebookEdit
                print(f"БЛОКИРОВКА: {p} — запечатанный журнал, редактирование запрещено (П16).", file=sys.stderr)
                sys.exit(2)
    sys.exit(0)

if __name__ == "__main__":
    main()

````

---

## Файл: `.claude/hooks/test_mathlib.py`

````python
#!/usr/bin/env python3
"""PostToolUse-хук: после правки файлов в mathlib/ гоняет pytest.
Красные тесты => код 2, Claude обязан чинить (инвариант 6 CLAUDE.md)."""
import json, subprocess, sys, os

def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    ti = data.get("tool_input", {}) or {}
    fp = str(ti.get("file_path", "") or ti.get("path", ""))
    if "mathlib/" not in fp:
        sys.exit(0)
    if not os.path.isdir("mathlib/tests"):
        sys.exit(0)
    r = subprocess.run(["python", "-m", "pytest", "mathlib/tests", "-q", "--no-header"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print("ТЕСТЫ MATHLIB КРАСНЫЕ после твоей правки — почини до продолжения:\n"
              + (r.stdout or "")[-2000:], file=sys.stderr)
        sys.exit(2)
    sys.exit(0)

if __name__ == "__main__":
    main()

````

---

## Файл: `.claude/settings.json`

````json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
        "hooks": [
          { "type": "command", "command": "python3 .claude/hooks/guard_journal.py" }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          { "type": "command", "command": "python3 .claude/hooks/test_mathlib.py" }
        ]
      }
    ]
  }
}

````

---

## Файл: `.claude/skills/ablation/SKILL.md`

````
---
name: ablation
description: Ежемесячная абляция — расчёт вклада каждого агента в качество прогнозов по контрфактическим протоколам Дирижёра. "/ablation".
---
# Абляция вкладов агентов (MASTER_SPEC §11.1)

1. Запусти: `python orchestrator/run.py --mode ablation` (по journal/funnel_logs/ и разрешённым исходам считается: улучшал ли голос агента X Brier итога или ухудшал).
2. Таблица: агент → число участий → дельта Brier → значимость.
3. Агент с устойчиво отрицательным вкладом (N≥30, значимо) — предложи понижение веса вплоть до карантина. НЕ удаляй: его суждения продолжают журналироваться (как у агента примет).
4. Агент в карантине с положительным вкладом в журнале — предложи возврат.
5. Результат — в journal/proposed_adjustments.md (применение только через /apply-weights) + краткая таблица пользователю.

````

---

## Файл: `.claude/skills/apply-weights/SKILL.md`

````
---
name: apply-weights
description: ЕЖЕМЕСЯЧНОЕ применение накопленных поправок к весам по правилам устойчивой калибровки. Только раз в месяц или по явной команде "/apply-weights".
---
# Применение поправок (MASTER_SPEC §10, П11)

ЭТО ЕДИНСТВЕННАЯ ПРОЦЕДУРА, КОТОРОЙ РАЗРЕШЕНО МЕНЯТЬ weights.yaml.
1. Прочитай journal/proposed_adjustments.md — предложения, накопленные /review-week.
2. Для каждого проверь правила §10: N≥30 сопоставимых разрешённых исходов; значимое улучшение на out-of-sample; шаг ≤ ±10% от текущего веса. Не проходит хотя бы одно — предложение откладывается (НЕ удаляется) с пометкой причины.
3. Сначала запусти /ablation, чтобы решения опирались на свежие вклады агентов.
4. Прошедшие проверку — примени к config/weights.yaml. git commit "weights: <что и почему>" — версионирование §10.5.
5. Редкие события: веса НЕ трогать, пометка «неоткалибровано» сохраняется.
6. Доложи: что применено, что отложено и почему, ссылка на коммит. Изменение СОСТАВА критериев скоринга — только с явного согласия пользователя.

````

---

## Файл: `.claude/skills/budget/SKILL.md`

````
---
name: budget
description: Панель контроля бюджета — спенд OpenRouter против потолков limits.yaml, детализация по режимам и моделям. "/budget".
---
# Панель бюджета (MASTER_SPEC §15, §30 п.2)

КОРПОРАТИВНЫЙ OpenRouter: работаем ТОЛЬКО через выделенный ключ «oracle» со spending limit $500/мес на самом ключе. Запрещено: трогать настройки уровня организации, опрашивать общий баланс /credits, предлагать лимиты на аккаунт. Все цифры панели — спенд этого ключа (/api/v1/key).

1. Запусти: `python3 ops/budget.py` — обновит dashboard/budget.html и выведет однострочный статус.
2. Покажи пользователю строку статуса и топ-3 статьи расходов по режимам.
3. Если статус «ВНИМАНИЕ ≥80%»: предложи варианты экономии (понизить частоту калибровочного режима; перевести массовую классификацию на более дешёвую модель из фолбеков models.yaml; сократить этап дебатов до топ-5).
4. Если «ПРЕВЫШЕНИЕ»: боевые и калибровочные прогоны останавливаются до начала месяца или явного решения пользователя поднять потолок в config/limits.yaml (только его решение — П12).
5. Напомни про сверку: каждый вызов OpenRouter в оркестраторе обязан писать строку в journal/costs.jsonl (ts, mode, agent, model, cost) — это источник детализации «стоимость инсайта».

````

---

## Файл: `.claude/skills/calibrate/SKILL.md`

````
---
name: calibrate
description: Калибровочный режим — массовые мелкие недельные прогнозы без ставок для набора статистики Brier. Запускать 2 раза в неделю или по команде "/calibrate".
---
# Калибровочный режим (MASTER_SPEC §17.3, §23.2в)

1. Запусти: `python orchestrator/run.py --mode calibrate`
2. Если оркестратор не готов — вручную: сгенерируй 10–20 МЕЛКИХ разрешённых прогнозов с недельным горизонтом по активам ядра (Brent, медь, связанные ETF). Каждый — строго по стандарту разрешимости §9: актив/контракт, направление, величина, срок, источник цены сверки.
3. Каждому прогнозу — base rate («как часто такое случается вообще») и итоговая вероятность.
4. Запечатай все в journal/predictions.jsonl. Ставок и рекомендаций НЕ выдаём — это чистый набор статистики.
5. Сверь прогнозы прошлых недель, чьи сроки вышли: `python orchestrator/run.py --mode resolve` (или вручную по ценам EODHD). Обнови корзины Brier.
6. Доложи пользователю одной строкой: сколько запечатано, сколько разрешилось, текущий Brier по корзинам, сколько осталось до 270.

````

---

## Файл: `.claude/skills/gate-check/SKILL.md`

````
---
name: gate-check
description: Проверка критерия перехода (gate) текущего шага плана сборки или денежных ворот. "/gate-check" или перед закрытием /goal.
---
# Проверка gate (MASTER_SPEC §24, §11)

1. Определи текущий шаг по ops/goals.md (выполненные отмечены).
2. Прогони критерий шага БУКВАЛЬНО по чек-листу §24: тесты, наличие файлов, объёмы библиотек, работоспособность потоков. Каждый пункт — командой или скриптом, не «на глаз».
3. Денежные ворота §11: Б→Д требует 270 разрешённых прогнозов ИЛИ письменной подписи пользователя в журнале решений; калибровка ±10 п.п.; превышение бенчмарка (60% SPY + 40% BCOM) на бумаге.
4. Вердикт: ПРОЙДЕН (отметь в ops/goals.md, предложи git tag gate-<имя>) / НЕ ПРОЙДЕН (точный список недостающего).
5. Kill-критерий: если 270 прогнозов набраны и превышения над бенчмарком нет ИЛИ калибровка хуже ±15 п.п. — обязан прямо сказать: «сработал kill-критерий §11, по утверждённой спецификации проект закрывается или перезапускается с новой гипотезой edge». Не смягчать.

````

---

## Файл: `.claude/skills/review-week/SKILL.md`

````
---
name: review-week
description: Еженедельный разбор — сверка исходов, объяснение промахов, ПРЕДЛОЖЕНИЯ поправок (без применения). Запускать раз в неделю или "/review-week".
---
# Еженедельный разбор (MASTER_SPEC §25, §10.10)

1. Разреши все прогнозы с вышедшим сроком (`--mode resolve`).
2. По каждому промаху: открой протокол прогона в journal/funnel_logs/, определи по контрфактам Дирижёра, КАКОЙ голос завёл систему не туда. Объяснение обязательно — «не знаю почему» не принимается; «данные были недоступны» принимается с фиксацией пробела.
3. Сформулируй ПРЕДЛОЖЕНИЯ поправок в journal/proposed_adjustments.md с датой. ЗАПРЕЩЕНО менять weights.yaml на этом шаге — применение только через /apply-weights.
4. Обнови журнал ловушек journal/traps.jsonl: были ли пропущенные манипуляции за неделю.
5. Обнови дашборд: Brier по корзинам, hit rate по школам, стоимость инсайта, предотвращённые ошибки.
6. Доложи пользователю сводку недели: исходы, главный урок, накопленные предложения.

````

---

## Файл: `.claude/skills/run-funnel/SKILL.md`

````
---
name: run-funnel
description: Боевой прогон полной воронки идей (режим 1 спецификации). Использовать по команде пользователя "прогон", "найди идеи", "/run-funnel".
---
# Боевой прогон воронки (MASTER_SPEC §6, §17.1)

Порядок (не пропускать шаги):
1. Проверь бюджет: прочитай config/limits.yaml; если месячный расход токенов превышен — СТОП, доложи пользователю.
2. Запусти: `python orchestrator/run.py --mode funnel`
3. Если оркестратор ещё не готов (этап сборки) — выполни воронку вручную по §6: скан с FDR → 30–50 кандидатов от всех школ → грубая фильтрация → скоринг → дебаты топ-5–7 → топ-3.
4. Проверь отчёт: все 13 полей §8 присутствуют, включая «что неизвестно», «кто продаёт нам», отыгранность, манипуляционный балл.
5. Убедись, что Дирижёр записал протокол прогона в journal/funnel_logs/ и контрфакты для абляции (§11.1).
6. Каждый прогноз из отчёта запечатан в journal/predictions.jsonl (timestamp+hash) ДО показа пользователю.
7. Покажи пользователю: топ-3 + сводку воронки (сколько отсеяно на каждом этапе и почему).

Правила: «стоящих идей нет» — легитимный результат. Нарушение П8 в любом суждении — идея блокируется. НИКОГДА не редактируй journal/predictions.jsonl.

````

---

## Файл: `README_КОМАНДЫ.md`

````
# Стартовый набор команд «Оракул» — ничего придумывать не нужно

## Установка (5 минут)
1. Создай папку проекта `oracle/` и распакуй туда этот набор (папка `.claude/` должна оказаться в корне проекта).
2. Туда же положи: `CLAUDE.md` (в корень) и `spec/MASTER_SPEC.md` (мастер-спецификация v10 FINAL).
3. `chmod +x .claude/hooks/*.py`
4. Открой Claude Code в папке проекта. Skills подхватятся автоматически (`/` покажет список).

## Что внутри
| Файл | Назначение |
|---|---|
| .claude/skills/run-funnel | /run-funnel — боевой прогон воронки, топ-3 идеи |
| .claude/skills/calibrate | /calibrate — набор статистики мелкими прогнозами (2 р/нед) |
| .claude/skills/review-week | /review-week — пятничный разбор: исходы, промахи, ПРЕДЛОЖЕНИЯ поправок |
| .claude/skills/apply-weights | /apply-weights — ЕЖЕМЕСЯЧНОЕ применение поправок по правилам §10 |
| .claude/skills/ablation | /ablation — месячные вклады агентов по контрфактам Дирижёра |
| .claude/skills/gate-check | /gate-check — буквальная проверка критерия текущего шага и денежных ворот |
| .claude/hooks/guard_journal.py | Блокирует ЛЮБУЮ правку запечатанных журналов (predictions, holdout, decisions) |
| .claude/hooks/test_mathlib.py | После правки mathlib гоняет pytest; красные тесты — Claude чинит |
| .claude/settings.json | Подключение хуков |
| ops/goals.md | Готовые /goal на каждую неделю плана §24 — копируй построчно |
| ops/crontab.txt | Готовый crontab рутины (калибровка, Brent, resolve, воронка) |

## Рабочий цикл
СБОРКА (нед.1–9): открыть сессию → взять следующую строку из ops/goals.md → /goal ... → работа (можно /loop до зелёных тестов) → /gate-check → git tag → /goal clear → /clear → следующая неделя.
ЭКСПЛУАТАЦИЯ (этап Бумаги): cron гоняет рутину сам; ты в Claude Code делаешь /review-week по пятницам, /ablation + /apply-weights раз в месяц, /run-funnel когда хочешь свежие идеи.

## Три правила безопасности
1. /goal автономен и умеет жечь деньги часами — всегда смотри на панель цели и держи потолок в limits.yaml.
2. Поправки весов предлагает /review-week, применяет ТОЛЬКО /apply-weights раз в месяц — не проси Claude «подкрутить веса» в обход.
3. Если /gate-check сказал «kill-критерий» — это не баг, это спецификация работает.

## Корпоративный OpenRouter — изоляция вашей части
1. В оргнастройках OpenRouter создайте ОТДЕЛЬНЫЙ API-ключ с именем `oracle` и установите spending limit **на этот ключ**: $500/мес. Лимит ключа ограничивает только расход через него; баланс организации и ключи коллег не затрагиваются.
2. Нет прав создавать ключи — попросите администратора выпустить ключ с лимитом (или через Management/Provisioning API: ключ создаётся программно сразу с limit).
3. Только этот ключ кладётся в OPENROUTER_API_KEY проекта. Никогда не используйте общий ключ организации в «Оракуле».
4. ops/budget.py меряет спенд по /api/v1/key — то есть строго по вашему ключу; общий баланс организации панель не читает и не показывает.
5. Второй, независимый контур — local: limits.yaml + проверка перед каждым прогоном; он работает, даже если админ изменит лимит ключа.

````

---

## Файл: `ops/budget.py`

````python
#!/usr/bin/env python3
"""Панель контроля бюджета «Оракул».
ВАЖНО (корпоративный OpenRouter): все измерения и лимиты — ПО КЛЮЧУ, не по аккаунту.
Используется выделенный ключ проекта (OPENROUTER_API_KEY = ключ «oracle» с limit $500/мес).
/api/v1/key возвращает спенд именно этого ключа; /api/v1/credits (общий баланс организации)
НЕ опрашиваем и никаких настроек уровня аккаунта не трогаем.
Три источника: (1) OpenRouter key API — реальный спенд день/неделя/месяц по ключу;
(2) локальный journal/costs.jsonl — стоимость по прогонам/режимам/агентам;
(3) config/limits.yaml — утверждённые потолки ($500 токены, $200 данные, $700 всего).
Выход: dashboard/budget.html + однострочная сводка в stdout (для Telegram/cron)."""
import json, os, sys, datetime, urllib.request, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIMITS = {"tokens_usd": 500.0, "data_usd": 200.0, "total_usd": 700.0}  # из §30; синхронизировать с config/limits.yaml
ALERT = (0.8, 1.0)

def openrouter_key_status():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return {"error": "OPENROUTER_API_KEY не задан"}
    req = urllib.request.Request("https://openrouter.ai/api/v1/key",
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r).get("data", {})
    except Exception as e:
        return {"error": str(e)}

def local_costs():
    """journal/costs.jsonl: {"ts": iso, "mode": str, "agent": str, "model": str, "cost": float}"""
    f = ROOT / "journal" / "costs.jsonl"
    now = datetime.datetime.now(datetime.timezone.utc)
    month = now.strftime("%Y-%m")
    by_mode, by_model, total = {}, {}, 0.0
    if f.exists():
        for line in f.read_text().splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not str(rec.get("ts", "")).startswith(month):
                continue
            c = float(rec.get("cost", 0))
            total += c
            by_mode[rec.get("mode", "?")] = by_mode.get(rec.get("mode", "?"), 0) + c
            by_model[rec.get("model", "?")] = by_model.get(rec.get("model", "?"), 0) + c
    return total, by_mode, by_model

def bar(frac):
    frac = max(0.0, min(frac, 1.2))
    color = "#2e7d32" if frac < ALERT[0] else ("#f9a825" if frac < ALERT[1] else "#c62828")
    return f'<div style="background:#eee;border-radius:6px;height:18px;width:100%"><div style="width:{min(frac,1)*100:.0f}%;background:{color};height:18px;border-radius:6px"></div></div>'

def main():
    ors = openrouter_key_status()
    local_total, by_mode, by_model = local_costs()
    # Спенд месяца: предпочитаем цифру OpenRouter, локальный журнал — детализация
    or_month = None
    for k in ("usage_monthly", "monthly_spend", "usage"):  # поле зависит от версии API
        if isinstance(ors.get(k), (int, float)):
            or_month = float(ors[k]); break
    spend = or_month if or_month is not None else local_total
    frac = spend / LIMITS["tokens_usd"]
    total_frac = (spend + LIMITS["data_usd"]) / LIMITS["total_usd"]

    rows_mode = "".join(f"<tr><td>{m}</td><td style='text-align:right'>${v:.2f}</td></tr>" for m, v in sorted(by_mode.items(), key=lambda x: -x[1]))
    rows_model = "".join(f"<tr><td>{m}</td><td style='text-align:right'>${v:.2f}</td></tr>" for m, v in sorted(by_model.items(), key=lambda x: -x[1])[:12])
    err = f"<p style='color:#c62828'>OpenRouter API: {ors['error']}</p>" if "error" in ors else ""
    html = f"""<!doctype html><meta charset="utf-8"><title>Оракул — бюджет</title>
<body style="font-family:system-ui;max-width:760px;margin:30px auto;padding:0 16px">
<h2>Бюджет «Оракул» — {datetime.date.today()}</h2>{err}
<h3>Токены OpenRouter: ${spend:.2f} / ${LIMITS['tokens_usd']:.0f} в месяц</h3>{bar(frac)}
<h3>Весь бюджет (токены + данные ${LIMITS['data_usd']:.0f}): / ${LIMITS['total_usd']:.0f}</h3>{bar(total_frac)}
<p>Источник цифры месяца: {"OpenRouter /api/v1/key" if or_month is not None else "локальный journal/costs.jsonl (OpenRouter недоступен)"};
расхождение OR/локально: ${abs((or_month or 0)-local_total):.2f}</p>
<h3>По режимам (локальный журнал, месяц)</h3><table border=0 cellpadding=4>{rows_mode or '<tr><td>пока пусто</td></tr>'}</table>
<h3>Топ моделей по стоимости</h3><table border=0 cellpadding=4>{rows_model or '<tr><td>пока пусто</td></tr>'}</table>
<p style="color:#777">Жёсткий потолок — limit на ВЫДЕЛЕННОМ ключе «oracle»: при сбое панели остановится только этот ключ, корпоративный аккаунт и ключи коллег не затронуты.</p>
</body>"""
    out = ROOT / "dashboard"; out.mkdir(exist_ok=True)
    (out / "budget.html").write_text(html, encoding="utf-8")

    status = "OK" if frac < ALERT[0] else ("ВНИМАНИЕ ≥80%" if frac < ALERT[1] else "ПРЕВЫШЕНИЕ — прогоны стоп")
    print(f"[бюджет] {status}: токены ${spend:.2f}/${LIMITS['tokens_usd']:.0f} ({frac*100:.0f}%), всего ~${spend+LIMITS['data_usd']:.0f}/${LIMITS['total_usd']:.0f}")
    sys.exit(0 if frac < ALERT[1] else 3)  # код 3 = превышение, ловится cron-обёрткой/ботом

if __name__ == "__main__":
    main()

````

---

## Файл: `ops/crontab.txt`

````
# Установка: crontab -e и вставить строки (пути поправить под машину; ORACLE=/путь/к/oracle)
# Рутина идёт НАПРЯМУЮ через python — дешевле и воспроизводимо. Claude Code — для разработки и разбора.

# Калибровочный режим: вторник и пятница 08:00
0 8 * * 2,5  cd $ORACLE && python orchestrator/run.py --mode calibrate >> journal/cron.log 2>&1

# Тематический режим Brent: ежедневно по будням 07:30
30 7 * * 1-5 cd $ORACLE && python orchestrator/run.py --mode theme --asset brent >> journal/cron.log 2>&1

# Разрешение прогнозов с вышедшим сроком: ежедневно 21:00
0 21 * * *   cd $ORACLE && python orchestrator/run.py --mode resolve >> journal/cron.log 2>&1

# Боевой прогон воронки: понедельник 09:00
0 9 * * 1    cd $ORACLE && python orchestrator/run.py --mode funnel >> journal/cron.log 2>&1

# Напоминание о еженедельном разборе (сам разбор — в Claude Code: /review-week): пятница 18:00
0 18 * * 5   echo "$(date): пора /review-week в Claude Code" >> $ORACLE/journal/reminders.log

# Панель бюджета: пересборка каждое утро 07:00; код выхода 3 = превышение
0 7 * * *    cd $ORACLE && python3 ops/budget.py >> journal/cron.log 2>&1

````

---

## Файл: `ops/goals.md`

````
# Готовые /goal по плану §24 — копируй в Claude Code построчно, по одной цели на сессию.
# После достижения: /gate-check → если ПРОЙДЕН: git tag, /goal clear, /clear, следующая строка.

[ ] Нед.1: /goal Каркас репозитория по §28 создан, config/models.yaml limits.yaml weights.yaml thresholds.yaml заполнены из §26 и §30, коннектор EODHD качает дневные котировки Brent, меди, SPY, DBC с историей 10 лет в storage/oracle.db
[ ] Нед.2: /goal Коннекторы GDELT и NewsAPI.ai работают, pytrends подключен, суточный новостной поток нормализуется и тегируется (язык, страна, тип, время) без ручной правки, дедупликация работает
[ ] Нед.3: /goal mathlib собран целиком (Brier по корзинам, FDR Бенджамини-Хохберга, запечатывание hash, сверка исходов, индикаторы, Келли с shrinkage, проверка лимитов) и все pytest в mathlib/tests зелёные, запись в predictions.jsonl только append через mathlib.seal()
[ ] Нед.4: /goal Программа §23.1 выполнена: фоновые дисперсии и FDR-порог в thresholds.yaml, издержки по инструментам ядра, пороги тайминга и детекторы манипуляций откалиброваны walk-forward c отчётами, knowledge/causal_links.yaml содержит 30+ связей с лагами и интервалами, knowledge/precursors.yaml построен по 30+ большим движениям
[ ] Нед.5-6: /goal Промпты ВСЕХ агентов блоков B,C,D,G лежат в agents/prompts (в каждом П8), оркестратор воронки вызывает OpenRouter по models.yaml с фолбеками, сквозной тестовый прогон собирает поле суждений всех школ в стандартном формате Дирижёра
[ ] Нед.7: /goal Состязательный контур работает: слепота, рандомизация порядка, версионируемая рубрика, вопрос "кто продаёт нам"; риск-агент, портфельный менеджер, скоринг и синтезатор дают полный цикл этапов 1-6 воронки на тестовом дне
[ ] Нед.8: /goal Маскированные кейсы в knowledge/masked_cases дают >=70% по рубрике без нарушений П8, дашборд показывает все метрики §15, абляционная отчётность по контрфактам работает
[ ] Нед.9: /goal Первый боевой прогон выдал отчёт со всеми 13 полями §8, первые прогнозы запечатаны, калибровочный режим запущен, тематический режим Brent настроен, /gate-check подтверждает Gate С→Б

````