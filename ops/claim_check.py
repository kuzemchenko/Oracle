# -*- coding: utf-8 -*-
"""ops/claim_check.py — ПРОВЕРКА ОТВЕТА АГЕНТА ДО ОТПРАВКИ, кодом (аудит 07.11).

Аудит трёх идей нашёл в ответах агента Telegram ошибки, которые общая инструкция «проверяй
внимательнее» не ловит. Каждый класс здесь — детектор по тексту ответа + требование
доказательства, которое код может проверить (вызван ли нужный инструмент, есть ли пометка,
совпадает ли число с журналом):

  timing      №1  ход цены объяснён событием («на отчёте упала») без расчёта времени события
                  (инструмент event_reaction: сессия Нью-Йорка, ход ДО и ПОСЛЕ);
  sealed_prob №7  «на момент записи было N%» — сверяется с journal/predictions.jsonl;
  prob_label  №7  вероятность успеха без пометки «моя оценка / субъективно»;
  track       №7  «точность на уровне монетки» без счёта по механизму (track_record);
  eps_basis   №3  прибыль на акцию / сравнение с консенсусом без базиса (GAAP / скорректированный);
  tce         №5  ставка × судо-дни названа выручкой без оговорки, что TCE — уже за вычетом рейсовых расходов;
  revision    №6  «перенести срок / пересмотреть стоп» без оговорки, что исходный прогноз неизменен;
  absence     №2  «не вижу / не найдено» как доказательство отсутствия;
  pipeline    №8  «прогон ef_… подтверждает работу» без статуса цепочки (pipeline_status).

check() → список находок {code, where, fix}. Агент получает их и исправляет ответ; оставшиеся
код дописывает в конец ответа как непроверенное (bot_agent.Agent.run).
"""
import re

_SENT = re.compile(r"(?<=[.!?…])\s+|\n+")
PCT = r"\d{1,3}(?:[.,]\d+)?\s?%"
G = r"(?:[^.?!]|(?<=\d)\.(?=\d))"          # «любой символ предложения», точка внутри числа/даты — можно

R = {
    "timing_event": re.compile(r"отч[её]т|результат\w* квартал|earnings|публикаци|объявлени|новост", re.I),
    "timing_move": re.compile(r"упал|падени|снизил|подешев|вырос|рост\w*|подорож|отскок|[−\-+]\s?\d+(?:[.,]\d+)?\s?%", re.I),
    "timing_causal": re.compile(r"\bна\s+(?:\w+\s+){0,2}(?:отч[её]т\w*|новост\w*|объявлени\w*|результат\w*)|"
                                r"в ответ на|ответил\w*|отреагировал\w*|реакци\w*|из-за", re.I),
    "sealed_ctx": re.compile(r"запечат|на момент (?:записи|печати)|в карточке|записал\w* в журнал|в журнале", re.I),
    "prob": re.compile(r"(?:шанс\w*|вероятност\w*)" + G + r"{0,60}?" + PCT + r"|" + PCT + r"" + G + r"{0,40}?(?:шанс|вероятност)", re.I),
    "prob_label": re.compile(r"оценк|оцениваю|субъективн|моё мнение|мое мнение|не измерен|не расч[её]т|"
                             r"не посчитан|калибр|база шума|от обычных колебаний", re.I),
    "track": re.compile(r"монетк|(?:наш\w*|историческ\w*|системы)\s+(?:прогноз\w*|точност\w*)" + G + r"{0,40}"
                        r"(?:уровн|хуже|лучше|не лучше)|сбывал\w* в \d", re.I),
    "eps": re.compile(r"на акцию|\beps\b|(?:прибыл|убыт)\w*\s+(?:\w+\s+){0,2}[−\-]?\$\s?\d", re.I),
    "revenue": re.compile(r"выручк\w*\s+(?:\w+\s+){0,2}\$\s?\d", re.I),
    "revenue_ok": re.compile(r"разов|единоразов|повторя|без учёта|без учета|в том числе|включая продаж", re.I),
    "reg_absolute": re.compile(r"(?:нельзя|невозможно|никто не сможет|запрещ\w+)" + G + r"{0,80}(?:ввоз|импорт|продат|продава|экспорт)|"
                               r"(?:ввоз|импорт|экспорт)" + G + r"{0,60}(?:нельзя|невозможно)", re.I),
    "reg_ok": re.compile(r"исключени|кроме|за исключением|не распространя|старых контракт|заключ\w+ до", re.I),
    # испытатель 10.11: «Базис консенсуса подтверждён сверкой» ловилось как отсутствие базиса — слово «базис»
    # и есть пометка
    "eps_basis": re.compile(r"\bgaap\b|non-gaap|скорректир|adjusted|по стандартам|бухгалтерск|чист\w+ убыт|базис",
                            re.I),
    "consensus": re.compile(r"консенсус|ожидани\w* аналитик|прогноз\w* аналитик", re.I),
    "tce_rate": re.compile(r"\btce\b|судо-?дн|в сутки" + G + r"{0,60}(?:дн|×|\*)|ставк\w*" + G + r"{0,40}сутк", re.I),
    "tce_revenue": re.compile(r"выручк|доход\w* рейс|заработ", re.I),
    "tce_ok": re.compile(r"за вычетом рейсов|уже за вычетом|tce" + G + r"{0,40}(?:нетто|чист|за вычетом)|"
                         r"тайм-чартерн\w* эквивалент", re.I),
    "revision": re.compile(r"(?:пересмотр\w*|пересматрива\w*|перенест\w*|перенос\w*|сдвинуть|сдвига\w*|подвинуть|"
                           r"поменять|изменить|новый|нов\w+)\s+(?:\w+\s+){0,2}(?:стоп\w*|цел[ьи]\w*|срок\w*|дат\w* проверки|проверк\w*)|"
                           r"стоп\s+(?:\w+\s+){0,2}пересматрива", re.I),
    "revision_ok": re.compile(r"исходн\w+" + G + r"{0,60}(?:не меня|неизмен|остаётся|остается|сверя)|нов\w+ верси|"
                              r"отдельн\w+ верси|запис\w+ не правится", re.I),
    "absence": re.compile(r"(?:не вижу|не нашёл|не нашел|не найден\w*|не обнаружил\w*|нет в новостях|в новостях не)"
                          r"" + G + r"{0,80}(?:перенос|отмен|поправк|запрет|документ|продлени|решени|суд)|"
                          r"(?:перенос\w*|отмен\w*|поправк\w*|продлени\w*)" + G + r"{0,60}(?:не вижу|не нашёл|не найден)", re.I),
    "absence_ok": re.compile(r"не доказ|не значит|не означает|первоисточник|на сайте ведомства|искал" + G + r"{0,40}(?:в|по)",
                             re.I),
    "pipeline": re.compile(r"\bef_\d{8}|прогон\w*" + G + r"{0,50}(?:подтвержд|показыва|доказыва|работа\w* систем)|"
                           r"(?:конвейер|система|охотник)\w*\s+(?:работает|жив\w*|исправн)", re.I),
}

TOOL_FOR = {"timing": "event_reaction", "track": "track_record", "pipeline": "pipeline_status",
            "sealed_prob": "sealed_terms"}


def sentences(text):
    return [s.strip() for s in _SENT.split(str(text or "")) if s.strip()]


def _called(trace, tool):
    return any(t.get("tool") == tool for t in (trace or []))


def _short(s, n=140):
    s = " ".join(s.split())
    return "«" + (s if len(s) <= n else s[:n] + "…") + "»"


def _pcts(s):
    return [float(x.replace(",", ".")) for x in re.findall(r"(\d{1,3}(?:[.,]\d+)?)\s?%", s)]


def _pct_near(s, pos):
    """Процент, ближайший к позиции pos в предложении."""
    best = None
    for m in re.finditer(r"(\d{1,3}(?:[.,]\d+)?)\s?%", s):
        d = abs(m.start() - pos)
        if best is None or d < best[0]:
            best = (d, float(m.group(1).replace(",", ".")))
    return best[1] if best else None


def check(text, trace=None, sealed=None):
    """text — ответ; trace — вызванные инструменты [{tool, args}]; sealed — {ТИКЕР: вероятность 0..1}
    из журнала (agent_facts.sealed_terms). → [{code, where, fix}]."""
    out, seen = [], set()
    ss = sentences(text)

    def add(code, s, fix):
        if (code, s) in seen:
            return
        seen.add((code, s))
        out.append({"code": code, "where": _short(s), "fix": fix})

    for i, s in enumerate(ss):
        ctx = " ".join(ss[max(0, i - 1):i + 2])
        # №1 время события
        if R["timing_event"].search(s) and R["timing_move"].search(s) and R["timing_causal"].search(s) \
                and not _called(trace, "event_reaction"):
            add("timing", s, "ход цены объяснён событием без проверки времени: вызови event_reaction(тикер, время "
                             "публикации в UTC) — он скажет, какая сессия могла ответить и каков ход ДО и ПОСЛЕ; "
                             "причину хода без источника называй гипотезой")
        # №7 записанная вероятность
        m_seal = R["sealed_ctx"].search(s)
        if m_seal and sealed and _pcts(s):
            near = _pct_near(s, m_seal.start())
            tks = [tk for tk in sealed if re.search(r"\b" + re.escape(tk) + r"\b", ctx)] or \
                  [tk for tk in sealed if re.search(r"\b" + re.escape(tk) + r"\b", str(text))]
            if near is not None and len(tks) == 1 and sealed[tks[0]] is not None:
                p = float(sealed[tks[0]])
                if abs(near - 100 * p) >= 0.6:
                    add("sealed_prob", s, f"в журнале по {tks[0]} при записи стоит {round(100 * p)}% (субъективная "
                                          f"оценка охотника, не калибрована), а в ответе {near:g}% — исправь число")
        if R["prob"].search(s) and not R["prob_label"].search(ctx):
            add("prob_label", s, "вероятность без пометки: скажи, что это твоя субъективная оценка (не калибрована "
                                 "и не посчитана кодом), и чем она обоснована")
        # №7 качество
        if R["track"].search(s) and not _called(trace, "track_record"):
            add("track", s, "о качестве прогнозов — только из track_record: назови механизм, период, N, метрику и "
                            "базу; у охотника сверенных исходов пока нет, счёт старого конвейера на него не переносится")
        # №3 базис показателя
        if R["eps"].search(s) and re.search(r"\d", s) and not R["eps_basis"].search(ctx):
            add("eps_basis", s, "прибыль на акцию без базиса: укажи GAAP или скорректированная (adjusted) и период; "
                                "если есть оба — назови оба")
        if R["consensus"].search(s) and R["eps"].search(ctx) and not R["eps_basis"].search(ctx):
            add("eps_basis", s, "сравнение с консенсусом без проверки, что консенсус на том же базисе (обычно "
                                "консенсус — скорректированный)")
        if R["revenue"].search(s) and not R["revenue_ok"].search(ctx):
            add("revenue_oneoff", s, "выручка без проверки разовых статей: есть ли в ней разовые продажи "
                                     "(запасы, разовые контракты, доход по гарантиям) и повторятся ли они")
        if R["reg_absolute"].search(s) and not R["reg_ok"].search(ctx):
            add("reg_exceptions", s, "абсолютное утверждение о правиле без исключений: проверь в полном тексте "
                                     "документа исключения, переходные условия и старые контракты и назови их")
        # №5 TCE
        if R["tce_rate"].search(s) and R["tce_revenue"].search(ctx) and not R["tce_ok"].search(ctx):
            add("tce", s, "ставка в сутки × судо-дни — это выручка TCE, уже за вычетом рейсовых расходов (топливо, порты); "
                          "так и назови и не вычитай рейсовые расходы второй раз; число дней другого квартала — допущение")
        # №6 пересмотр
        if R["revision"].search(s) and not R["revision_ok"].search(ctx):
            add("revision", s, "изменение срока/цели/стопа — это НОВАЯ версия со своей датой и причиной; скажи, что "
                               "исходный прогноз и его условия не меняются и будут сверены как записаны")
        # №2 отсутствие
        if R["absence"].search(s) and not R["absence_ok"].search(ctx):
            add("absence", s, "«не нашёл» — не доказательство «нет»: скажи, где и на какую дату искал и открывал "
                              "ли первоисточник (сайт ведомства, реестр документов)")
        # №8 статус системы
        if R["pipeline"].search(s) and not _called(trace, "pipeline_status"):
            add("pipeline", s, "о работе системы — только из pipeline_status: какой механизм (охотник или старый "
                               "конвейер), чем закончился запуск, дошло ли до владельца, что ждёт и почему")
    return out


def repair_prompt(findings):
    lines = [f"{i + 1}. [{f['code']}] {f['where']} — {f['fix']}" for i, f in enumerate(findings)]
    return ("[система] Проверка кодом нашла в черновике ответа утверждения без доказательства:\n" + "\n".join(lines)
            + "\nИсправь ответ: вызови нужные инструменты (event_reaction, track_record, sealed_terms, "
              "pipeline_status), поправь числа и пометки. Если подтвердить нельзя — так и скажи и пометь как "
              "гипотезу. Верни ПОЛНЫЙ исправленный ответ целиком, не только правки.")


def footer(findings):
    """То, что агент не исправил, — дописывает КОД."""
    if not findings:
        return ""
    head = "⚠ Проверка кодом: в ответе остались утверждения без подтверждения —"
    rows = [f"• {f['where']}: {f['fix']}" for f in findings[:6]]
    more = f"\n…и ещё {len(findings) - 6}" if len(findings) > 6 else ""
    return head + "\n" + "\n".join(rows) + more
