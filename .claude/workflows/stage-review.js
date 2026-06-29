export const meta = {
  name: 'stage-review',
  description: 'Обязательное ревью этапа дорожной карты фиксов: flow + код по диффу этапа → скептик-верификация → вердикт ПРОЙДЕН/НЕ ПРОЙДЕН',
  whenToUse: 'Гейт каждого этапа дорожной карты (ops/goals.md, раздел «Дорожная карта фиксов»). args: {stage, base, files?}. base — git-ref начала этапа (тег/коммит) для диффа.',
  phases: [
    { title: 'Review', detail: 'линзы код (корректность/инварианты/интеграция/числа) + flow по диффу этапа' },
    { title: 'Verify', detail: 'скептик адверсариально проверяет каждую находку' },
    { title: 'Synthesize', detail: 'вердикт ПРОЙДЕН/НЕ ПРОЙДЕН + блокеры + проверка «идеи потекли»' },
  ],
}

const stage = (args && args.stage) || 'этап'
const base = (args && args.base) || 'HEAD~1'
const files = (args && args.files) || null   // null → агент берёт весь git diff base..HEAD

const INV = `ИНВАРИАНТЫ (нарушение = блокер этапа), CLAUDE.md/spec/MASTER_SPEC.md каноничны:
П8 нулевые выдумки (факт со ссылкой, число с расчётом, «нет данных» легитимен); П10 состязательность (генератор/критик/судья РАЗНЫХ семейств, суд слепой, развязка в КОДЕ не в конфиге); П16 форвард-онли (запечатано до исхода, журнал не редактируется, не фитить на прошлом); §10 калибровка (N≥30, значимость, поправка на base-rate); §11 ворота + ГЕРМЕТИЧНОСТЬ треков (провизорный/calibration НЕ в money §11); Инв#5 лимиты программно перед прогоном; Инв#6 математика — детерминир. код в mathlib, не LLM.
КОНТЕКСТ: цель проекта — поток инвест-идей доходит до пользователя; ревью 28.06 нашло «числовое ядро отравлено» + рассинхрон контрактов модулей (node_to_facts переименовал поля → судья видит None; инверт. money-гейт; антиманип-вето мёртво). План фиксов — spec/FIXPLAN_2026-06-28.md.`

const SCOPE = files
  ? `ФАЙЛЫ ЭТАПА: ${files.join(', ')}. Изучи их `
  : `ДИФФ ЭТАПА: выполни \`git diff ${base}..HEAD --stat\` и \`git diff ${base}..HEAD\`, изучи изменения `

const FIND_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          severity: { type: 'string', enum: ['blocker', 'high', 'medium', 'low'] },
          lens: { type: 'string' },
          file: { type: 'string' },
          line: { type: 'string' },
          invariant: { type: 'string' },
          description: { type: 'string' },
          evidence: { type: 'string' },
          fix: { type: 'string' },
          regression: { type: 'boolean', description: 'этап СЛОМАЛ ранее работавшее?' },
        },
        required: ['title', 'severity', 'lens', 'file', 'description', 'evidence'],
      },
    },
  },
  required: ['findings'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['confirmed', 'refuted', 'uncertain'] },
    reasoning: { type: 'string' },
    adjusted_severity: { type: 'string', enum: ['blocker', 'high', 'medium', 'low', 'none'] },
  },
  required: ['verdict', 'reasoning', 'adjusted_severity'],
}

const GATE_SCHEMA = {
  type: 'object',
  properties: {
    gate: { type: 'string', enum: ['ПРОЙДЕН', 'НЕ ПРОЙДЕН'] },
    blockers: { type: 'array', items: { type: 'string' } },
    ideas_flow_ok: { type: 'string', description: 'оценка: идеи доходят до пользователя дальше, чем до этапа? есть ли регрессия потока?' },
    summary: { type: 'string' },
  },
  required: ['gate', 'blockers', 'summary'],
}

const LENSES = [
  { key: 'корректность', focus: 'Баги в изменениях: краевые случаи, None/деление, индексы/срезы, знак/направление, рассинхрон горизонтов/лагов.' },
  { key: 'инварианты', focus: 'Нарушения П8/П10/П16/§10/§11/Инв#5/Инв#6 во внесённых изменениях. LLM считающий числа; утечка авторства судье; провизорный/calibration в money; фит на истории.' },
  { key: 'интеграция', focus: 'Контракт между модулями: совпадают ли ключи/имена полей у писателя и читателя (частая болячка cascade-first); что построено, но не вызывается; рассинхрон mock/live.' },
  { key: 'числовое_ядро', focus: 'Статистика/математика изменений: беты/r²/значимость/base-rate/свёртка дисперсии/walk-forward/adjusted_close. Сверхуверенность, артефакты.' },
  { key: 'flow', focus: 'КЛЮЧЕВОЕ: делает ли этап так, что идеи ТЕКУТ дальше к пользователю (суд не валит из-за дыр контракта, money-гейт честен, выдача доходит), или ломает поток? Проверь связь изменений со стадиями скан→каскад→ворота→треки→суд→seal→выдача.' },
]

log(`Ревью этапа «${stage}» (дифф ${base}..HEAD): ${LENSES.length} линз → верификация → вердикт`)

phase('Review')
const reviews = await parallel(LENSES.map(lens => () =>
  agent(
    `ТОЛЬКО ЧТЕНИЕ, ничего не редактируй.\n\n${INV}\n\nЛИНЗА: «${lens.key}». ${lens.focus}\n\n` +
    `${SCOPE}через свою линзу. Находи ТОЛЬКО реальные дефекты ВО ВНЕСЁННЫХ ИЗМЕНЕНИЯХ (и регрессии — что этап сломал). Опирайся на код (file+line+evidence). П8: не выдумывай.`,
    { label: `review:${stage}:${lens.key}`, phase: 'Review', schema: FIND_SCHEMA }
  ).then(r => (r?.findings || []).map(f => ({ ...f, lens: lens.key })))
))
const found = reviews.filter(Boolean).flat()
log(`Находок этапа: ${found.length}`)

phase('Verify')
const verified = await parallel(found.map(f => () =>
  agent(
    `ТОЛЬКО ЧТЕНИЕ. ${INV}\n\nТЫ СКЕПТИК. Адверсариально проверь находку по коду/диффу — попытайся опровергнуть. confirmed только при доказательстве в коде; refuted если не подтверждается; uncertain если нужны данные вне репо.\n\nНАХОДКА:\n${JSON.stringify(f, null, 1)}`,
    { label: `verify:${stage}:${(f.title || '').slice(0, 28)}`, phase: 'Verify', schema: VERDICT_SCHEMA }
  ).then(v => ({ ...f, verdict: v?.verdict, adjusted_severity: v?.adjusted_severity, verify_reasoning: v?.reasoning }))
))
const confirmed = verified.filter(Boolean).filter(f => f.verdict === 'confirmed')
const blockers = confirmed.filter(f => (f.adjusted_severity || f.severity) === 'blocker' || f.regression)

phase('Synthesize')
const gate = await agent(
  `${INV}\n\nТы выносишь ГЕЙТ-ВЕРДИКТ этапа «${stage}» дорожной карты фиксов. Ниже подтверждённые скептиком находки. ` +
  `Правило: gate=НЕ ПРОЙДЕН, если есть хоть один blocker или регрессия (этап сломал работавшее) или новое нарушение инварианта. ` +
  `Иначе ПРОЙДЕН (high/medium/low — записать как долг, но не блокировать). ` +
  `ОБЯЗАТЕЛЬНО оцени ideas_flow_ok: стал ли поток идей к пользователю дальше/честнее после этапа, нет ли регрессии потока. ` +
  `Русский, кратко.\n\nПОДТВЕРЖДЕНО (${confirmed.length}), из них блокеров ${blockers.length}:\n${JSON.stringify(confirmed, null, 1)}`,
  { label: `gate:${stage}`, phase: 'Synthesize', schema: GATE_SCHEMA }
)

return { stage, totals: { found: found.length, confirmed: confirmed.length, blockers: blockers.length },
         gate: gate?.gate, ideas_flow_ok: gate?.ideas_flow_ok, blockers: gate?.blockers,
         summary: gate?.summary, confirmed }
