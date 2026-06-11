# -*- coding: utf-8 -*-
"""mathlib/calibration/walkforward.py — генератор walk-forward окон (MASTER_SPEC §23.1).

Классическая честная схема для ДЕТЕРМИНИРОВАННЫХ компонентов: окно обучения → окно
проверки → сдвиг окна вперёд. Код не помнит будущее, поэтому подбор порога на train
и проверка на ИДУЩЕМ ПОСЛЕ него test — корректная оценка обобщения (в отличие от LLM, П16).

Возвращаются индексы в исходный массив, чтобы вызывающий сам резал любые свои ряды.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Fold:
    fold: int
    train_start: int
    train_end: int       # эксклюзивно
    test_start: int
    test_end: int        # эксклюзивно

    @property
    def train(self):
        return slice(self.train_start, self.train_end)

    @property
    def test(self):
        return slice(self.test_start, self.test_end)


def walk_forward(n, train_size, test_size, step=None, anchored=False):
    """Сгенерировать список Fold по ряду длины n.

    train_size : длина окна обучения (минимальная при anchored=True)
    test_size  : длина окна проверки, идущего СРАЗУ за train
    step       : сдвиг вперёд (по умолчанию = test_size — непересекающиеся test-окна)
    anchored   : True → train всегда от 0 (расширяющееся окно); False → скользящее окно
    """
    if train_size <= 0 or test_size <= 0:
        raise ValueError("train_size и test_size должны быть > 0")
    if n < train_size + test_size:
        raise ValueError(f"ряд короче train+test ({n} < {train_size}+{test_size})")
    step = test_size if step is None else step
    if step <= 0:
        raise ValueError("step должен быть > 0")

    folds = []
    i = 0
    test_start = train_size
    while test_start + test_size <= n:
        train_start = 0 if anchored else (test_start - train_size)
        folds.append(Fold(
            fold=i,
            train_start=train_start,
            train_end=test_start,
            test_start=test_start,
            test_end=test_start + test_size,
        ))
        i += 1
        test_start += step
    return folds
