export const meta = {
  name: 'hunt-idea',
  description: 'Охота за идеей дня v3: разведка по зонам с планкой → шлагбаум кодом → суд-проверка (чинит, а не казнит) → починка → круги до защищаемой идеи → текст владельцу',
  whenToUse: 'Ежедневно (облачный охотник) или вручную: «найди идею дня». args: {today:"YYYY-MM-DD", rotation_domain?:string, rounds?:number, max_court?:number, out_dir?:string, work_dir?:string, seed_files?:[пути карточек — суд без повторной разведки]}. Пишет reports/ideas/idea_<дата>_<ТИКЕР>.{md,push.txt,card.json} и дописывает hunt_log.jsonl; коммит делает вызывающая сессия.',
  phases: [
    { title: 'Разведка', detail: 'разведчики по зонам A–G с планкой knowledge/idea_bar.yaml; каждый сам гоняет шлагбаум ops/idea_gate.py' },
    { title: 'Суд', detail: 'три линзы-проверки: факты, «в цене ли звено — числом», Баффет; убить можно только с доказательством' },
    { title: 'Починка', detail: 'запросы суда исполняются, перепроверка только упавших линз' },
    { title: 'Выбор', detail: 'ранг защищаемых: асимметрия × свежесть × простота × оценка Баффета' },
    { title: 'Текст', detail: 'md с источниками + пуш владельцу + §9-карточка; линт и шлагбаум кодом' },
  ],
}

const A = args || {}
const TODAY = A.today
if (!TODAY) throw new Error('hunt-idea: нужен args.today (YYYY-MM-DD) — Date в скрипте недоступен')
const ROUNDS = A.rounds || 3          // планка и CLAUDE.md: до 3 кругов (перепроверка №3)
const MAX_COURT = A.max_court || 5
const OUT = A.out_dir || 'reports/ideas'
const WORK = A.work_dir || 'reports/ideas/_work'
const ROT = A.rotation_domain || 'фарма и биотех (даты решений FDA, патентные обрывы)'

const COMMON = `Сегодня ${TODAY}. Проект «Оракул» (/home/user/Oracle). ПЛАНКА идеи — knowledge/idea_bar.yaml (прочитай целиком: жёсткие критерии, зоны поиска, правила суда, шаблон подачи) — она главнее твоих привычек. Контекст: тезисы владельца knowledge/theses_owner.yaml; библиотека механизмов knowledge/mechanisms.yaml (у механизмов — условия срабатывания, прецеденты с числами и URL, контрпримеры); журнал прошлых охот reports/ideas/hunt_log.jsonl (убитых не предлагай повторно, пока не выполнено их «пересмотреть_если»; бумагу, у которой в reports/ideas/ уже лежит карточка idea_*_<ТИКЕР>.card.json с датой сверки spec9.resolve_by НЕ РАНЬШЕ сегодняшнего дня, не предлагай — эта идея уже у владельца и ведётся своими проверками, а сервер не откроет второй прогноз по бумаге до сверки первого; со следующего дня после даты сверки бумага снова свободна). П8 строго: каждый факт — живым веб-поиском (WebSearch; при тонких результатах mode extended) с датой и URL; память до 2026 не источник; число без источника — не число. Вероятности владельцу не показываются никогда; сырая вероятность нужна только в §9-спеке для честного счёта. Пиши по-русски; ключи JSON — латиницей по схеме. БЮДЖЕТ ПОИСКА ОБЩИЙ: на всю охоту ~200 запросов WebSearch на всех агентов (живой прогон 02.11 его выбрал, и суду проверять стало нечем) — держи свой лимит из задания; факты проверяй ОТКРЫТИЕМ URL (WebFetch), а не новыми поисками; закрытые хосты не повторяй. ЦЕНЫ, А НЕ ПРОЦЕНТЫ: в карточке обязательны trigger_date + trigger_close (закрытие в день повода), target_price, stop_price и, если вход по условию, entry_limit; проценты хода/потенциала/риска и асимметрию от ХУДШЕГО входа считает код шлагбаума (сервер пересчитает по своим котировкам, а цену в день повода сверит с допуском 3%). ДАТА ФАКТА — дата ПУБЛИКАЦИИ источника, не дата твоей проверки (факт «из будущего» шлагбаум бракует).`

const CARD_PROPS = {
  ticker: { type: 'string', description: 'EODHD-тикер вида MP.US' },
  name: { type: 'string' }, role: { type: 'string', description: 'роль компании простыми словами' },
  direction: { type: 'string', enum: ['вверх', 'вниз'] },
  zone: { type: 'string' }, mechanism_id: { type: 'string' },
  catalyst: { type: 'object', properties: { what: { type: 'string' }, date: { type: 'string' }, url: { type: 'string' } }, required: ['what', 'date'] },
  thesis: { type: 'string', description: 'механизм в 2–3 фразах: почему цена этой бумаги сдвинется' },
  facts: { type: 'array', items: { type: 'object', properties: { text: { type: 'string' }, url: { type: 'string' }, date: { type: 'string', description: 'дата ПУБЛИКАЦИИ источника; если её нет — дата проверки и date_kind=«проверка»' }, date_kind: { type: 'string', enum: ['публикация', 'проверка'] } }, required: ['text', 'url', 'date'] } },
  precedent: { type: 'object', properties: { text: { type: 'string' }, amplitude: { type: 'string', description: 'числом: «+30–60%»' }, url: { type: 'string' }, counter: { type: 'string', description: 'контрпример' } }, required: ['text', 'amplitude', 'url'] },
  trigger_date: { type: 'string', description: 'дата повода/сетапа YYYY-MM-DD (от неё мерится ход)' },
  trigger_close: { type: 'number', description: 'цена закрытия бумаги в день повода, $ (сервер сверит с котировками, допуск 3%)' },
  trigger_what: { type: 'string', description: 'что было поводом, в родительном падеже коротко: «объявления продления перемирия» — для абзаца, который пишет код' },
  target_price: { type: 'number', description: 'цель, $ (= порог §9 или дальше)' },
  stop_price: { type: 'number', description: 'стоп, $ — закрытие по ту сторону = выход' },
  entry_limit: { type: 'number', description: 'худший вход, $: для «вверх» — не дороже, для «вниз» — не дешевле' },
  move_since_trigger_pct: { type: 'number', description: 'ход ЭТОЙ бумаги с даты повода, % — = (last_close/trigger_close−1)·100, код сверит' },
  upside_pct: { type: 'number', description: 'до цели от last_close, % — код сверит с ценами' }, downside_pct: { type: 'number', description: 'до стопа от last_close, % — код сверит с ценами' },
  why_not_priced: { type: 'string', description: 'почему звено ещё не в цене — ЧИСЛОМ' },
  who_sells: { type: 'string', description: 'кто продаёт нам и почему (структурная причина)' },
  kill_conditions: { type: 'array', items: { type: 'object', properties: { what: { type: 'string' }, check_by: { type: 'string' } }, required: ['what', 'check_by'] } },
  entry: { type: 'string', description: 'когда и как входить (или какой триггер ждать)' },
  spec9: { type: 'object', properties: { asset: { type: 'string' }, direction: { type: 'string', enum: ['above', 'below'] }, threshold: { type: 'number' }, resolve_by: { type: 'string' }, probability: { type: 'number' }, last_close: { type: 'number' }, last_close_date: { type: 'string' }, price_source: { type: 'string' } }, required: ['asset', 'direction', 'threshold', 'resolve_by', 'probability', 'last_close', 'last_close_date'] },
  gate_file: { type: 'string', description: 'путь к JSON карточки, прогнанной через шлагбаум' },
}
const CARD_REQ = ['ticker', 'name', 'role', 'direction', 'zone', 'mechanism_id', 'catalyst', 'thesis', 'facts', 'precedent', 'trigger_date', 'trigger_close', 'target_price', 'stop_price', 'move_since_trigger_pct', 'upside_pct', 'downside_pct', 'why_not_priced', 'who_sells', 'kill_conditions', 'entry', 'spec9', 'gate_file']
const CARD = { type: 'object', properties: CARD_PROPS, required: CARD_REQ }
const SCOUT_SCHEMA = {
  type: 'object',
  properties: {
    zone: { type: 'string' },
    cards: { type: 'array', items: CARD, description: 'ТОЛЬКО карточки, прошедшие шлагбаум (pass=true)' },
    dropped: { type: 'array', items: { type: 'object', properties: { ticker: { type: 'string' }, why: { type: 'string' } }, required: ['ticker', 'why'] } },
  },
  required: ['zone', 'cards', 'dropped'],
}
const VERDICT = {
  type: 'object',
  properties: {
    lens: { type: 'string' },
    verdict: { type: 'string', enum: ['PASS', 'FIX', 'FATAL'] },
    criterion: { type: 'string', description: 'какой критерий планки задет' },
    evidence: { type: 'string', description: 'для FATAL обязательно: URL и/или число' },
    fix_request: { type: 'string', description: 'для FIX: что конкретно досчитать/найти/поменять' },
    objection: { type: 'string', description: 'сильнейшее возражение по-человечески' },
    answer: { type: 'string', description: 'честный ответ идеи на это возражение, если он есть' },
    score_1_10: { type: 'number' },
  },
  required: ['lens', 'verdict', 'criterion', 'evidence', 'objection', 'score_1_10'],
}

const COURT_RULES = `ПРАВИЛА СУДА (планка, раздел court_rules): ты ПРОВЕРЯЕШЬ, а не казнишь. Бремя доказательства — на тебе: FATAL только с уликой (URL и/или число) и только за жёсткий провал: несущий факт ложен; звено уже отыграно ЧИСЛОМ (ход этой бумаги с даты повода ≥70% прецедентной амплитуды или катализатор уже разрешился); катализатора нет в окне 3–13 недель; бумага неликвидна; механизм ложен (бизнес бумаги не связан с поводом). НЕ основания для FATAL: «тема в новостях», «дата известна всем», «аналитики уже пишут», «рынок эффективен» — это вопрос ЦЕНЫ, отвечай числом или ставь FIX с конкретным запросом. Сомнение = FIX с точным запросом, не FATAL. PASS — если по твоей линзе идею можно защищать перед умным скептиком.`

function brief(c) {
  return JSON.stringify({ ticker: c.ticker, name: c.name, role: c.role, direction: c.direction, zone: c.zone, mechanism_id: c.mechanism_id, catalyst: c.catalyst, thesis: c.thesis, facts: c.facts, precedent: c.precedent, trigger_date: c.trigger_date, trigger_close: c.trigger_close, trigger_what: c.trigger_what, target_price: c.target_price, stop_price: c.stop_price, entry_limit: c.entry_limit, move_since_trigger_pct: c.move_since_trigger_pct, upside_pct: c.upside_pct, downside_pct: c.downside_pct, why_not_priced: c.why_not_priced, who_sells: c.who_sells, kill_conditions: c.kill_conditions, entry: c.entry, spec9: c.spec9 }, null, 1)
}

const LENSES = [
  ['facts', 'Ты факт-чекер П8 (лимит: сначала ОТКРОЙ URL из карточки через WebFetch, новых WebSearch — не больше 6). Проверь КАЖДЫЙ несущий факт карточки (повод с датой, ключевые числа, ход цены, дата катализатора, последняя цена закрытия). Несущий факт ложен/не подтверждается → FATAL с URL опровержения. Подтвердить не смог, но правдоподобно → FIX: «найти первоисточник для …». Всё подтвердилось → PASS; в evidence перечисли, что и по каким URL подтверждено.'],
  ['priced', 'Ты рыночная линза v2 (лимит: не больше 4 WebSearch). Единственный вопрос: досчитано ли ЭТО ЗВЕНО в цене — ЧИСЛОМ? Найди: ход этой бумаги с даты повода; ожидаемый опционами ход к дате катализатора, если находится; пересмотры консенсус-оценок по этой компании после повода. Считай долю прецедентной амплитуды, уже пройденную. ≥70% или катализатор уже разрешён → FATAL с числами. Меньше → PASS. Не смог найти чисел → FIX с запросом, что досчитать.'],
  ['buffett', 'Ты Уоррен Баффет (лимит: не больше 2 WebSearch, опирайся на факты карточки), но оцениваешь ставку на горизонте 3–13 недель, а не «владеть пять лет» (эта мерка запрещена). Понятен ли механизм за минуту умному обывателю? Есть ли канал к деньгам ИМЕННО этой компании фактом (доля сегмента, контракт, гарантия, валюта издержек)? Ограничен ли убыток стопом или полом; нет ли в окне риска допэмиссии, банкротства, делистинга? Не слишком ли длинна цепочка? Кто продаёт и почему ошибается — убедительно? FATAL — только если механизм ложен (бизнес бумаги на деле не связан с поводом), с уликой. Иначе PASS или FIX (чем усилить: другой инструмент, точка входа, стоп). Поле objection — твоё сильнейшее возражение; answer — честный ответ идеи на него, если он есть.'],
]

const ZONES_R1 = [
  ['A', 'недосчитанные дальние звенья: возьми 2–3 крупных события последних 1–6 недель, найди второе-третье звено цепочки (поставщик, заказчик, заместитель), чья бумага ещё НЕ сдвинулась — мерь ход ЗВЕНА, не повода'],
  ['B', 'календарные развилки с асимметрией: датированные решения в ближайшие 3–13 недель (регуляторные сроки, тарифы и их истечение, решения ОПЕК, суды, сроки контрактов, выборы) — где один исход даёт большой ход бумаге, а цена закладывает другой'],
  ['C', '«сухой порох»: механизмы библиотеки, чьи УСЛОВИЯ СРАБАТЫВАНИЯ выполнены сейчас (проверь веб-поиском запасы/мощности/режим), а событие ещё не случилось — идея = вахта с датированным триггером входа'],
  ['D', 'отставшие рёбра: knowledge/edge_candidates.jsonl и рёбра библиотеки — где источник уже сдвинулся (числом), а цель ещё нет; проверь ценами за последние 2–6 недель'],
  ['E', 'особые ситуации с ВЫНУЖДЕННЫМ продавцом (продаёт не потому, что знает, а потому что обязан): исключения из индексов и ребалансировки, спин-оффы (индексные фонды сбрасывают выделенную компанию), налоговые продажи ноября–декабря у сильно упавших бумаг с целым бизнесом, падшие ангелы (облигации/акции после понижения рейтинга до мусорного — фонды обязаны продать), выход из банкротства. Здесь ответ «кто продаёт и почему неправ» — структурный'],
  ['F', 'тезисы владельца T1/T2 (knowledge/theses_owner.yaml: потоки сложно-воспроизводимых активов; сдерживание Китая → замещающие производители): найди БЛИЖАЙШИЙ датированный катализатор, который переоценит конкретного бенефициара'],
  ['G', `домен вне библиотеки на сегодня: ${ROT} — найди 1–2 датированных катализатора с асимметрией`],
]

async function scoutRound(zones, round, extra) {
  return parallel(zones.map(([z, how]) => () => agent(
    `${COMMON}

Ты разведчик зоны ${z} (круг ${round}). Где искать: ${how}.
${extra || ''}
Твой лимит — не больше 10 запросов WebSearch. Задача: принести 1–2 КАРТОЧКИ кандидатов, каждая из которых проходит ЖЁСТКИЕ критерии планки. Порядок для каждой: (1) собери факты живым поиском (≥3 факта с URL и датой, ≥2 разных источника); (2) прецедент с ЧИСЛОВОЙ амплитудой и контрпример (из библиотеки механизмов или из поиска, с URL); (3) ход бумаги с даты повода (числом); (4) потенциал и риск до стоп-условия в %; (5) §9-спека: актив, above/below, порог, заякоренный к свежей цене закрытия (с датой), дата сверки 3–13 недель от ${TODAY} и не раньше катализатора, сырая вероятность; (6) запиши карточку JSON в файл ${WORK}/r${round}_${z}_<тикер>.json (mkdir -p ${WORK}) и выполни \`python3 ops/idea_gate.py <файл> --today ${TODAY}\`. Не прошла — почини по причинам шлагбаума (найди число, смени инструмент/порог/срок) и прогони снова; не чинится — в dropped с причиной. В cards — только прошедшие (pass=true), с gate_file. Лучше одна сильная, чем две слабых; ноль — легитимно, но тогда объясни в dropped.`,
    { label: `разведка:${z}:к${round}`, phase: 'Разведка', schema: SCOUT_SCHEMA },
  )))
}

function rankKey(c) {
  const asym = (c.upside_pct || 0) / Math.max(c.downside_pct || 1, 0.1)
  return asym
}

async function runLenses(c, round, tag, only) {
  // Личность линзы — из КОДА (порядок LENSES), а не из подписи модели: ревью 02.11 показало
  // «рыночная линза v2: …», «Баффет», «facts (факт-чекер П8)» — сводка по свободной строке
  // давала false всем или игнорировала FATAL. Пропавший голос — один повтор, затем «нет голоса».
  const ask = (k, role, n) => agent(
    `${COMMON}\n\n${role}\n\n${COURT_RULES}\n\nКАРТОЧКА:\n${brief(c)}`,
    { label: `суд:${k}:${c.ticker}:к${round}${tag}${n ? ':повтор' : ''}`, phase: tag ? 'Починка' : 'Суд', schema: VERDICT })
  const use = LENSES.filter(([k]) => !only || only.has(k))
  const votes = await parallel(use.map(([k, role]) => () => ask(k, role, 0).then(v => v || ask(k, role, 1))))
  return use.map(([k], idx) => votes[idx] ? { ...votes[idx], lens: k } : { lens: k, verdict: 'NOVOTE', criterion: 'линза не ответила', evidence: '', objection: '', score_1_10: 0 })
}

async function courtAndRepair(cards, round) {
  return pipeline(cards,
    (c) => runLenses(c, round, '').then(votes => ({ card: c, votes })),
    async (r) => {
      const fatal = r.votes.filter(v => v.verdict === 'FATAL')
      const fixes = r.votes.filter(v => v.verdict === 'FIX' || v.verdict === 'NOVOTE')
      if (fatal.length || !fixes.length) return r
      const rep = await agent(
        `${COMMON}\n\nТы чинишь карточку по запросам суда (не спорь с судом — исполни): \n${fixes.map(v => `• ${v.lens}: ${v.fix_request || v.criterion}`).join('\n')}\n\nКАРТОЧКА:\n${brief(r.card)}\n\nНайди недостающее живым поиском, поправь карточку (инструмент/порог/срок/вход можно менять, факты — только найденные), запиши в ${WORK}/r${round}_fix_<тикер>.json и прогони \`python3 ops/idea_gate.py <файл> --today ${TODAY}\` до pass=true. Не чинится — верни исходную карточку без изменений, gate_file прежний.`,
        { label: `починка:${r.card.ticker}:к${round}`, phase: 'Починка', schema: CARD })
      if (!rep) return r
      // после починки — без наводки: если сменилась БУМАГА — все линзы заново (старые PASS к ней
      // не относятся); если бумага та же — заново только упавшие линзы (бюджет поиска общий)
      if (String(rep.ticker || '').toUpperCase() !== String(r.card.ticker || '').toUpperCase()) {
        const votes = await runLenses(rep, round, ':после-починки')
        return { card: rep, votes, repaired: true }
      }
      const redo = new Set(fixes.map(v => v.lens))
      const fresh = await runLenses(rep, round, ':после-починки', redo)
      const votes = r.votes.map(v => redo.has(v.lens) ? (fresh.find(f => f.lens === v.lens) || v) : v)
      return { card: rep, votes, repaired: true }
    },
  )
}

function byLens(r) { return Object.fromEntries(r.votes.map(v => [v.lens, v.verdict])) }
// строго защищаемая: все три линзы PASS
function shippable(r) {
  if (!r || !r.card) return false
  const by = byLens(r)
  return by.facts === 'PASS' && by.priced === 'PASS' && by.buffett === 'PASS'
}
// «идея в проверке»: факты подтверждены, ни одного FATAL, но остался неснятый FIX — честно в тексте
function checkable(r) {
  if (!r || !r.card) return false
  const by = byLens(r)
  return by.facts === 'PASS' && !r.votes.some(v => v.verdict === 'FATAL' || v.verdict === 'NOVOTE')
}

const judged = []
const seenTickers = new Set()
let ship = []
for (let round = 1; round <= ROUNDS && !ship.length; round++) {
  phase('Разведка')
  let zones = ZONES_R1
  let extra = ''
  if (round > 1) {
    const fatal = judged.filter(r => r.votes.some(v => v.verdict === 'FATAL'))
    const seeds = fatal.map(r => `${r.card.ticker} (${r.card.mechanism_id}): ${r.votes.filter(v => v.verdict === 'FATAL').map(v => v.lens + ' — ' + v.evidence).join('; ').slice(0, 300)}`).join('\n')
    extra = `ОБРАТНАЯ СВЯЗЬ прошлых кругов — НЕ приноси эти бумаги снова: ${[...seenTickers].join(', ')}. Почему их отвергли (учись на этом, не повторяй ошибку):\n${seeds || '(отказов с уликой нет — прошлые кандидаты не прошли шлагбаум)'}`
    zones = [
      ['N', `СЛЕДУЮЩЕЕ ЗВЕНО отвергнутых идей: если механизм был верен, но бумага уже отыграла — найди поставщика/заказчика/заместителя дальше по цепочке, чья бумага ещё не сдвинулась. Семена:\n${seeds || 'нет'}`],
      ['E', ZONES_R1[4][1]],
      ['B', ZONES_R1[1][1]],
      ['G', `другой домен вне библиотеки, НЕ «${ROT}»: выбери сам из (люкс и потребитель, Япония/Индия, страхование и перестраховка, агро-технологии, телеком и спутники, оборонка вне США) — 1–2 датированных катализатора`],
    ]
  }
  // посев (args.seed_files): суд над уже найденными карточками без повторной разведки — для
  // продолжения охоты в свежем ходе, когда прошлый ход выбрал общий бюджет поиска
  const seeded = (round === 1 && Array.isArray(A.seed_files) && A.seed_files.length)
    ? (await parallel(A.seed_files.map((f, i) => () => agent(
        `${COMMON}\n\nПрочитай файл ${f} и верни его содержимое как карточку РОВНО по схеме, ничего не меняя и не добавляя; gate_file = "${f}". Поиск не нужен.`,
        { label: `посев:${i}`, phase: 'Разведка', schema: CARD })))).filter(Boolean)
    : null
  const scouts = seeded ? [{ zone: 'посев', cards: seeded, dropped: [] }] : (await scoutRound(zones, round, extra)).filter(Boolean)
  const cards = []
  for (const s of scouts) for (const c of (s.cards || [])) {
    const t = String(c.ticker || '').toUpperCase()
    if (!t || seenTickers.has(t)) continue
    seenTickers.add(t)
    cards.push(c)
  }
  const dropped = scouts.flatMap(s => s.dropped || [])
  log(`круг ${round}: карточек после шлагбаума ${cards.length}, отсеяно разведчиками ${dropped.length}`)
  const ranked = cards.sort((a, b) => rankKey(b) - rankKey(a))
  if (ranked.length > MAX_COURT) log(`в суд — топ-${MAX_COURT} по асимметрии; не судились: ${ranked.slice(MAX_COURT).map(c => c.ticker).join(', ')}`)
  phase('Суд')
  const res = (await courtAndRepair(ranked.slice(0, MAX_COURT), round)).filter(Boolean)
  judged.push(...res)
  ship = judged.filter(shippable)
  log(`круг ${round}: защищаемых ${ship.length}, «в проверке» ${judged.filter(checkable).length - ship.length}, судимых ${res.length}`)
}

phase('Выбор')
const SEL = {
  type: 'object',
  properties: {
    leader: { type: 'string', description: 'тикер лидера' },
    backup: { type: 'string', description: 'тикер запасной или пусто' },
    why: { type: 'string', description: 'почему этот лидер, по-русски' },
  },
  required: ['leader', 'backup', 'why'],
}
let pool = ship
let genre = 'идея'
if (!pool.length) {
  // все круги без строгого прохода: кандидаты с подтверждёнными фактами и без FATAL, но с неснятым
  // FIX — жанр «идея в проверке»: что именно ещё не досчитано, пишется в тексте прямо
  pool = judged.filter(checkable)
  genre = 'идея в проверке'
}
const KILLED = judged.filter(r => r.votes.some(v => v.verdict === 'FATAL')).map(r => r.card)
// перепроверка №2: тени пишет КОД из файлов шлагбаума (gate_file), а не модель из обрезанного JSON
const KILLED_FILES = KILLED.map(c => c.gate_file).filter(Boolean)
if (KILLED_FILES.length < KILLED.length) log(`тени: у ${KILLED.length - KILLED_FILES.length} отвергнутых нет файла шлагбаума — тенью не пойдут`)
const SHADOW_CMD = `python3 ops/hunter_files.py shadow ${OUT}/idea_${TODAY}_killed.shadow.json ${KILLED_FILES.join(' ')}`
if (!pool.length) {
  // день без идеи всё равно пишет тени (вето надо заработать) и журнал охоты
  await agent(`${COMMON}\n\nВыполни ровно эту команду (тени пишет код, карточки не переписывай): \`${SHADOW_CMD}\`\nИ допиши (append) в ${OUT}/hunt_log.jsonl по строке на каждого судимого: {"дата":"${TODAY}","инструмент","механизм","вердикт":"отвергнута"|"не доведена","причина","пересмотреть_если"}. СУДИМЫЕ: ${JSON.stringify(judged.map(r => ({ ticker: r.card.ticker, mechanism: r.card.mechanism_id, votes: r.votes.map(v => `${v.lens}:${v.verdict}:${(v.evidence || v.fix_request || '').slice(0, 160)}`) }))).slice(0, 40000)}`, { label: 'тени-и-журнал', phase: 'Текст' })
  return { status: 'нет защищаемой идеи после всех кругов', rounds: ROUNDS, judged: judged.map(r => ({ ticker: r.card.ticker, votes: r.votes.map(v => `${v.lens}:${v.verdict}:${(v.evidence || '').slice(0, 160)}`) })) }
}
const sel = await agent(`${COMMON}\n\nТы выбираешь ОДНУ идею дня из защищаемых (и запасную). Критерии планки (soft): асимметрия (потенциал/риск), свежесть (малая доля прецедентного хода пройдена), простота цепочки (меньше звеньев), убедительность «кто продаёт и почему», близость катализатора, оценки линз.\n\nКАНДИДАТЫ:\n${JSON.stringify(pool.map(r => ({ card: r.card, votes: r.votes })), null, 1).slice(0, 80000)}`, { label: 'выбор', phase: 'Выбор', schema: SEL })
// перепроверка №2: модель отвечает «MP» вместо «MP.US» — строгое === молча подменяло лидера на pool[0]
const norm = (t) => String(t || '').toUpperCase().trim().replace(/\.US$/, '')
const lead = pool.find(r => norm(r.card.ticker) === norm(sel && sel.leader)) || pool[0]
if (sel && norm(lead.card.ticker) !== norm(sel.leader)) log(`выбор: лидер «${sel.leader}» не найден среди защищаемых — взят ${lead.card.ticker}`)
const backup = pool.find(r => norm(r.card.ticker) === norm(sel && sel.backup) && r !== lead) || null

phase('Текст')
const base = String(lead.card.ticker).split('.')[0]
const stem = `${OUT}/idea_${TODAY}_${base}`
const WRITE = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string' } },
    lint: { type: 'string', description: 'problems из python3 ops/hunter_seal.py preview: [] = чисто' },
    gate: { type: 'string', description: 'pass шлагбаума для card.json' },
    push_text: { type: 'string' },
  },
  required: ['files', 'lint', 'gate', 'push_text'],
}
const wr = await agent(`${COMMON}

Ты автор подачи. Жанр: «${genre}» (в заголовке пуша — «Идея дня», а если вход по условию с датой — «Идея с триггером»; слово «наблюдение» в заголовке запрещено). Запиши ЧЕТЫРЕ файла:
1) ${stem}.md — исследование: питч, вся карточка, КАЖДЫЙ факт с URL и датой (раздел «Источники»), прецедент и контрпример с URL, табло суда (линза → вердикт → улика/запрос → возражение и ответ), что починено, §9-спека, запасная идея (${backup ? backup.card.ticker : 'нет'}) одной строкой.
2) ${stem}.card.json — карточку лидера пишет КОД, не ты: выполни \`python3 ops/hunter_files.py card ${lead.card.gate_file || '<gate_file лидера>'} ${stem}.card.json\`, затем \`python3 ops/idea_gate.py ${stem}.card.json --today ${TODAY}\` — обязан pass=true; цель, стоп и дата сверки в пуше — ровно из этой карточки (код бота сверит).
3) ${stem}.push.txt — текст владельцу в Telegram СТРОГО по шаблону push_template из knowledge/idea_bar.yaml (и списку banned_in_push): одна идея, которую ты ЗАЩИЩАЕШЬ; главное возражение суда — вместе с ответом, не приговором; никаких вероятностей, шансов, коэффициентов ни цифрами, ни словами; ≤8 чисел вне кавычек «…» и ≤14 во всём тексте (даты не считаются) — хватает на вход/цель/стоп/потенциал/риск; если жанр «идея в проверке» — в абзаце «Главный риск — и мой ответ» прямо назови, что ещё не досчитано (неснятые запросы суда) и что я сделаю до какой даты, ответ на возражение НЕ выдумывай; тикер один раз, без «.US», рядом имя и роль компании; дни недели сверь с календарём (${TODAY} — укажи сам, какой это день); последняя строка дословно «Это исследовательский инструмент, не инвестиционная рекомендация.» Строку о печати прогноза и счёт метода НЕ пиши — их допишет код бота.
Проверь пуш кодом РОВНО так, как его отправит бот: \`python3 ops/hunter_seal.py preview ${stem}\` — обязан выйти с кодом 0 и problems = [] (сверка ИТОГОВОГО текста с карточкой: заголовок «ставлю на рост/снижение» = направление, цель/стоп/граница входа/дата сверки из карточки в тексте; линт итога, где абзацы «Почему это ещё не в цене» и «Сколько можно заработать…» подставлены кодом; запрет обещаний «напишу/пришлю/проверю к…» — бот сам напоминает в день катализатора и присылает итог). Иначе правь и повторяй. Не обещай в тексте сообщений, кроме двух, что шлёт код: напоминание в день катализатора и итог после даты сверки.
4) ${OUT}/idea_${TODAY}_killed.shadow.json — тени отвергнутых пишет КОД: выполни ровно \`${SHADOW_CMD}\` (сервер тихо запечатает их тенью, чтобы суд со временем доказал своё право убивать).
Пуш — не больше 250 слов.
Затем ДОПИШИ (append, не переписывай) в ${OUT}/hunt_log.jsonl по строке JSON на каждого судимого кандидата: {"дата":"${TODAY}","инструмент","механизм","вердикт": "выбрана"|"запасная"|"отвергнута"|"не доведена","причина","пересмотреть_если"}.

ЛИДЕР (карточка и вердикты):
${JSON.stringify({ card: lead.card, votes: lead.votes, repaired: !!lead.repaired }, null, 1)}

ВСЕ СУДИМЫЕ (для hunt_log):
${JSON.stringify(judged.map(r => ({ ticker: r.card.ticker, mechanism: r.card.mechanism_id, votes: r.votes.map(v => ({ lens: v.lens, verdict: v.verdict, evidence: (v.evidence || '').slice(0, 200), fix: v.fix_request || '' })) })), null, 1).slice(0, 60000)}`,
  { label: `текст:${base}`, phase: 'Текст', schema: WRITE })

return {
  status: genre,
  leader: lead.card.ticker,
  backup: backup ? backup.card.ticker : null,
  why: sel ? sel.why : '',
  files: wr ? wr.files : [],
  lint: wr ? wr.lint : 'нет ответа автора',
  gate: wr ? wr.gate : '',
  push_text: wr ? wr.push_text : '',
  judged: judged.map(r => ({ ticker: r.card.ticker, repaired: !!r.repaired, votes: r.votes.map(v => `${v.lens}:${v.verdict}`) })),
}
