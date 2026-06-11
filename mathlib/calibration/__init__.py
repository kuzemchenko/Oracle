# -*- coding: utf-8 -*-
"""mathlib.calibration — детерминированная walk-forward калибровка §23.1 («честная зона»).

Код «не помнит будущее» (в отличие от LLM, П16), поэтому классическая схема
train→test→сдвиг на истории здесь ЛЕГАЛЬНА. Модули покрывают пп. §23.1:
  backgrounds — фоновые дисперсии метрик скана (п.6, FDR)
  costs       — исторические/модельные издержки по инструментам ядра (п.8)
  timing      — пороги тайминг-детектора walk-forward (п.1)
  manipulation— ценовые детекторы манипуляций walk-forward (п.4)
  causal      — эмпирические лаги причинных связей (п.2)
  precursors  — библиотека предвестников по крупнейшим движениям (п.3)
  loader / walkforward — инфраструктура (чтение котировок, генератор фолдов)
"""
from . import loader, walkforward, backgrounds, costs, timing, manipulation, causal, precursors  # noqa: F401
