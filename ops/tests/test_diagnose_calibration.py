# -*- coding: utf-8 -*-
"""Д2 — тесты диагностического пересчёта калибровочного трека (ops/diagnose_calibration.py).

Проверяем на синтетике ТОЙ ЖЕ ФОРМЫ, что боевые журналы:
  1) честные журналы → 0 расхождений, баг НЕ подтверждён;
  2) инъекция подмены исхода / не того бара → диагноз ЛОВИТ и классифицирует причину;
  3) воспроизведение первопричины «hit 36.1% без бага»: коррелированная корзина + общий
     обвал в окне сверки → hit-rate много ниже заявленных вероятностей при НУЛЕ расхождений
     конвенций (то самое «низкий hit ≠ баг сверки»);
  4) кластерный null шире наивного биномиального (вложенные пороги + кросс-корреляция) —
     ошибка независимости алярма воспроизводится кодом.
Все данные синтетические (tmp_path); боевые журналы/БД не читаются и не пишутся."""
import datetime
import json
import math
import pathlib
import random
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ops import diagnose_calibration as DIAG   # noqa: E402

UTC = datetime.timezone.utc
H = 5
K_OFFSETS = (0.0, 0.5, -0.5)


def _weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _std_ddof1(xs):
    import statistics
    return statistics.stdev(xs)


def _make_env(tmp_path, n_assets=3, n_bars=380, crash=0.0, seed=11, flip_outcome=False,
              wrong_bar=False):
    """Синтетика: коррелированная корзина (общий фактор + идиосинкразия), печати по конвенциям
    calibrate (якорь = последний бар до окна сверки), исходы по конвенциям resolve.
    crash — общий лог-ход НА КАЖДЫЙ из 5 баров окна сверки (обвал корзины как в июне)."""
    dates = _weekdays(datetime.date(2025, 1, 6), n_bars)
    ia = n_bars - 1 - H                                   # якорный бар: до него история, после — окно сверки
    series = {}
    # ОБЩИЙ фактор корзины: один и тот же ход для всех активов в каждый день (кросс-корреляция)
    rng = random.Random(seed)
    common_moves = [rng.gauss(0, 0.012) for _ in range(n_bars)]
    idio = {f"A{a}.US": [random.Random(seed * 100 + a * 7 + i).gauss(0, 0.004)
                         for i in range(n_bars)] for a in range(n_assets)}
    for a in range(n_assets):
        sym = f"A{a}.US"
        px, row = 100.0 * (a + 1), []
        for i, d in enumerate(dates):
            row.append((d, round(px, 6)))
            step = common_moves[i] + idio[sym][i]
            if i >= ia:                                   # окно сверки: общий обвал (или 0)
                step += crash
            px *= math.exp(step)
        series[sym] = row

    db = tmp_path / "q.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE quotes (symbol TEXT, date TEXT, close REAL, "
                "adjusted_close REAL, volume INTEGER)")
    for sym, row in series.items():
        for d, c in row:
            con.execute("INSERT INTO quotes VALUES (?,?,?,?,?)",
                        (sym, d.isoformat(), c, c, 1000))
    con.commit()
    con.close()

    anchor = dates[ia]
    resolve_by = anchor + datetime.timedelta(days=7)
    rb_iso = f"{resolve_by.isoformat()}T20:00:00+00:00"
    preds, outs = [], []
    n = 0
    for sym, row in series.items():
        px = row[ia][1]
        hist = [c for _, c in row[:ia + 1]][-61:]
        lr = [math.log(hist[i + 1] / hist[i]) for i in range(len(hist) - 1)]
        sigma_h = _std_ddof1(lr) * math.sqrt(H)
        obs = next((d, c) for d, c in row if d >= resolve_by)
        for k in K_OFFSETS:
            thr = round(px * math.exp(k * sigma_h), 4)
            n += 1
            h = f"hash{n:04d}"
            preds.append({
                "kind": "calibration", "run_id": "calibrate_syn", "asset": sym,
                "direction": "above", "threshold": thr, "resolve_by": rb_iso,
                "price_source": f"EODHD close {sym}", "probability": round(_norm_cdf(-k), 4),
                "prob_model": "gaussian_baseline_realized_vol", "k_sigma": k,
                "sigma_h": round(sigma_h, 6), "horizon_trading_days": H,
                "threshold_asof_close_date": anchor.isoformat(),
                "sealed_at": f"{(anchor + datetime.timedelta(days=1)).isoformat()}T08:00:00+00:00",
                "hash": h})
            outcome = 1 if obs[1] > thr else 0
            o_val, o_date = obs[1], obs[0]
            if wrong_bar and n == 1:                      # инъекция: не тот бар исхода
                o_val, o_date = row[ia + 1][1], row[ia + 1][0]
                outcome = 1 if o_val > thr else 0
            if flip_outcome and n == 1:                   # инъекция: подмена исхода 0↔1
                outcome = 1 - outcome
            outs.append({"hash": h, "asset": sym, "kind": "calibration", "direction": "above",
                         "threshold": thr, "resolve_by": rb_iso, "probability": round(_norm_cdf(-k), 4),
                         "observed_value": o_val, "observed_at": f"{o_date.isoformat()}T20:00:00+00:00",
                         "outcome": outcome})
    ppath = tmp_path / "preds.jsonl"
    opath = tmp_path / "outs.jsonl"
    ppath.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in preds), encoding="utf-8")
    opath.write_text("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in outs), encoding="utf-8")
    return ppath, opath, db


def test_clean_journals_zero_mismatch_no_bug(tmp_path):
    ppath, opath, db = _make_env(tmp_path)
    rep = DIAG.build_report(ppath, opath, db, with_null=False)
    d = rep["сводка"]["расхождения_по_причинам"]
    assert (d["σ_H"], d["порог"], d["бар_исхода"], d["исход_0/1"]) == (0, 0, 0, 0)
    assert rep["вердикт"]["баг_сверки_подтверждён"] is False
    assert rep["вердикт"]["пауза_refit_П-1_01.08"] is False
    assert rep["сводка"]["hit_rate"]["как_записано"] == rep["сводка"]["hit_rate"]["как_пересчитано"]
    assert rep["dashboard_row"]["n"] == 9                     # 3 актива × 3 порога


def test_flipped_outcome_detected_as_bug(tmp_path):
    ppath, opath, db = _make_env(tmp_path, flip_outcome=True)
    rep = DIAG.build_report(ppath, opath, db, with_null=False)
    d = rep["сводка"]["расхождения_по_причинам"]
    assert d["исход_0/1"] == 1 and d["hashes"]["outcome"] == ["hash0001"]
    assert rep["вердикт"]["баг_сверки_подтверждён"] is True
    assert rep["вердикт"]["пауза_refit_П-1_01.08"] is True    # решение владельца 13.07, Вопрос 3


def test_wrong_observation_bar_detected(tmp_path):
    ppath, opath, db = _make_env(tmp_path, wrong_bar=True)
    rep = DIAG.build_report(ppath, opath, db, with_null=False)
    d = rep["сводка"]["расхождения_по_причинам"]
    assert d["бар_исхода"] == 1                               # причина классифицирована: не тот бар
    assert rep["вердикт"]["баг_сверки_подтверждён"] is True


def test_reproduces_low_hit_without_any_bug(tmp_path):
    """Первопричина Д2 на синтетике той же формы: общий обвал корзины в окне сверки →
    hit-rate много ниже заявленной средней 0.5 при НУЛЕ расхождений конвенций."""
    ppath, opath, db = _make_env(tmp_path, crash=-0.02)       # −2% общего фактора на бар × 5 баров
    rep = DIAG.build_report(ppath, opath, db, with_null=False)
    d = rep["сводка"]["расхождения_по_причинам"]
    assert (d["σ_H"], d["порог"], d["бар_исхода"], d["исход_0/1"]) == (0, 0, 0, 0)
    assert rep["вердикт"]["баг_сверки_подтверждён"] is False
    assert rep["сводка"]["hit_rate"]["как_записано"] <= 0.35  # «36.1%» без единого бага
    assert rep["сводка"]["реализованный_ход_σH"]["среднее"] < -0.5


def test_cluster_null_wider_than_iid_binomial(tmp_path):
    """Ошибка независимости алярма: sd честного кластерного null (вложенные пороги +
    кросс-корреляция корзины) много шире биномиального sd = √(0.25/N)."""
    ppath, opath, db = _make_env(tmp_path)
    joined = DIAG.load_joined(ppath, opath)
    q = DIAG.Quotes(db)
    try:
        null = DIAG.cluster_null(q, joined, n_sims=300, seed=1, cutoff="2026-04-01",
                                 batch_offsets=(0,))
    finally:
        q.close()
    assert null["применимо"] is True
    n = len(joined)
    binom_sd = math.sqrt(0.25 / n)
    assert null["null_sd"] > 1.5 * binom_sd
    assert null["p_кластерный"] >= null["p_наивный_биномиальный"]


def test_report_files_written(tmp_path):
    ppath, opath, db = _make_env(tmp_path)
    out_dir = tmp_path / "rep"
    rc = DIAG.main(["--predictions", str(ppath), "--outcomes", str(opath), "--db", str(db),
                    "--out-dir", str(out_dir), "--sims", "50"])
    assert rc == 0
    rep = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert "dashboard_row" in rep and "вердикт" in rep
    md = (out_dir / "REPORT.md").read_text(encoding="utf-8")
    assert "диагностический пересчёт" in md and "Баг сверки подтверждён" in md
