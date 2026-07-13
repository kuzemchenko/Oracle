# Д3 — characterization-свидетельства (боевой путь не переключён)

Дата: 2026-07-13. Метод: одна и та же проба (фиксированный `now_dt=2026-07-13T12:00Z`,
один и тот же read-only снапшот боевых `quotes`) исполнена ДВАЖДЫ — кодом master
(`f5ec6da`, до Д3) и кодом этапа Д3; печати сравнены `diff`'ом байт-в-байт.
Скрипт пробы — сессионный (`char_probe.py` в scratchpad сессии), только чтение:
`seal=False`, `write=False`, журналы не тронуты.

| Поверхность | Проба | Результат |
|---|---|---|
| calibrate-режим (§17.3) | `orchestrator.calibrate.build_calibration_predictions` на снапшоте, фиксированный `now_dt` | **байт-в-байт идентично** |
| B4 форвард-тест (боевой seal-путь `build_from_db → node_to_facts → seal_spec`) | `orchestrator.edge_forward.run_edge_forward(write=False, seal=False)` — полный протокол | **байт-в-байт идентично** |
| dry-run путь `cascade_from_quotes` (event_first_dryrun) | USO.US → {BNO.US, FRO.US}, shock −0.03, H=5 | только ДОБАВЛЕН ключ `sensitivity_unconditional` (алиас того же объекта `sensitivity`); все прежние поля и значения идентичны |

Диффа в решениях нет: `sealable`/`probability`/`amplitude`/`причина`/ворота/ранг читают
прежние поля. `sensitivity_conditional` по умолчанию НЕ считается (lazy,
`with_conditional=False`) — включение потребителей: этап Э4(в,г), не Д3.

Закреплено регрессионно: `orchestrator/tests/test_d3_characterization.py`
(calibrate не зависит от модуля conditional даже при его отравлении; node_cascade —
только аддитивные ключи; дефолты `with_conditional=False`).
