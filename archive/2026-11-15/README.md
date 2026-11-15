# Архив 15.11.2026 — уборка по итогам ревью Codex и Fable (14.11), «да на уборку» владельца 15.11

Что здесь: модули, которые не вызывает ничто работающее (бот, агент, расписание, воркфлоу охоты), и их тесты.
Файлы перенесены `git mv` — история каждого цела. Вернуть: `git mv archive/2026-11-15/<путь> <путь>`.
pytest эту папку не собирает (корневой conftest.py).

- Разовые исследования калибровки июля–октября: ops/calibrate_conditional, calibrate_fdr_background,
  calibrate_sensitivities, calibrate_week4, diagnose_calibration, fit_recalib, replay_scan.
- Задачи, выключенные из расписания 14.11 (кормили треки без умения): ops/promote_edges, refit_prob_shrink, rescan_maps.
- Исследования «карты мира» и рёбер: orchestrator/world_map, world_enum, segment_screen, thesis_edges, history_edges.
- Сессия партнёра (/session, «дай идеи») — команда убрана из бота 15.11, разговор ведёт агент: orchestrator/partner_session.

Оставлены как ручные инструменты: ops/ledger, history_prices, precedent_prices, apply_themes, demo_find_idea,
orchestrator/mechanisms, mathlib/* (пакет mathlib/calibration нужен работающему коду).
