# -*- coding: utf-8 -*-
"""H6 (таймскейл): типовой 5-дневный ход инструментов ядра vs их дневная сигма.
Гипотеза: 5-дневное окно каскада тонет в шуме на ликвидных ETF. Read-only storage/oracle.db.
Метрика: |5-дн лог-доходность| в единицах дневной сигмы = |r_5| / (σ_дн·√5). <1 ⇒ типичный ход
внутри шума окна; сколько дней |r_5| превышает 2σ окна (различимое движение)."""
import sqlite3, math, statistics
SEED = ["BNO.US","USO.US","SPY.US","DBC.US","CPER.US","COPX.US","RKLB.US","ASTS.US",
        "VRT.US","GEV.US","ETN.US","CLF.US","NUE.US"]
con = sqlite3.connect("file:/root/oracle/storage/oracle.db?mode=ro", uri=True)
print(f"{'тикер':<9}{'N дн':>6}{'σ_дн %':>9}{'медиан|r5| %':>14}{'медиан|r5|/окно σ':>19}{'дней |r5|>2σ_окна':>18}")
rows_out=[]
for s in SEED:
    q = con.execute("SELECT date, adjusted_close FROM quotes WHERE symbol=? AND adjusted_close IS NOT NULL "
                    "ORDER BY date", (s,)).fetchall()
    px=[r[1] for r in q]
    if len(px) < 130:
        print(f"{s:<9}{len(px):>6}   мало баров"); continue
    px=px[-130:]  # ~полгода торговых дней
    daily=[math.log(px[i]/px[i-1]) for i in range(1,len(px))]
    sig=statistics.pstdev(daily)
    r5=[math.log(px[i]/px[i-5]) for i in range(5,len(px))]
    win_sig=sig*math.sqrt(5)
    med_abs_r5=statistics.median(abs(x) for x in r5)
    med_ratio=med_abs_r5/win_sig if win_sig else 0
    exceed=sum(1 for x in r5 if abs(x)>2*win_sig)
    print(f"{s:<9}{len(px):>6}{sig*100:>9.2f}{med_abs_r5*100:>14.2f}{med_ratio:>19.2f}{exceed:>10}/{len(r5)}")
    rows_out.append((s,sig,med_abs_r5,med_ratio,exceed,len(r5)))
# сводка
if rows_out:
    print(f"\nМедиана по ядру: типовой 5-дн ход = {statistics.median(r[3] for r in rows_out):.2f} σ окна "
          f"(если <1 — 5-дн движение обычно НЕ различимо на фоне шума окна).")
    tot_ex=sum(r[4] for r in rows_out); tot_n=sum(r[5] for r in rows_out)
    print(f"Доля 5-дн окон с различимым (>2σ) ходом: {tot_ex}/{tot_n} = {100*tot_ex/tot_n:.1f}%")
con.close()
