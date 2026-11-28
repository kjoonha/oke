import { expect, mock, test } from 'claude-code/testing'
import { THEME_NAMES, answered, bashWrites, framesOf, gitDenied, jobActionDenied, keyDenied, orchestratorDenied, paletteOf, pendingWords, promptRole, rmDenied, todoLine, withBriefs, withPython, workerDenied, writeVerdict } from './register'

type Run = { argv: readonly string[] }

const BOARD = [{
  name: 'toy', key: 'toy', pm: null,
  jobs: [{ job: 'calc-sub', phase: 'reviewing', deliver: 'delivered', pending: 'question', size: 'small',
    words: 'add a subtract button to the calculator', step: 'reviewing', left: ['delivery'], agents: [{
    agent: 'calc-sub-w1', role: 'worker', status: 'working', pane: 'w7:p9', report: null,
    board: '## Now\nwriting sub', revives: 1, cost: 0.4, todos: [
      { content: 'write sub', status: 'completed' }, { content: 'test sub', status: 'in_progress' },
      { content: 'docs', status: 'pending' }] }] },
  { job: 'calc-add', phase: 'done', deliver: 'delivered', pending: null, agents: [{
    agent: 'calc-add-w1', role: 'worker', status: 'idle', pane: 'w7:p8', report: 'done',
    board: '', revives: 0, todos: [] }] }],
}]

let tickRole = 'worker'
let tickBoard: unknown = BOARD
let tickRecent: unknown = []
let answeredNow: string[] = []
let autoCompact = true
let contextPercent = 34
let nextWork = ''
let okeState = true
let draft = ''
let mailFlag = false
let dropMail = false
let unrecorded = false  // spawn has not recorded the agent yet
let mailText: string | null = null
let writeAreas: { path: string; direct: boolean }[] = []  // the tick's write areas
let tickFolder = '/oke/jobs/j/w1'
let failTick = false  // oke tick prints no JSON
let failCheck = false  // oke path-check prints no JSON

function answer(args: readonly string[]) {
  if (args[0] === 'tick') {
    if (failTick) return undefined
    return { role: tickRole, agent: unrecorded ? null : tickRole === 'pm' ? 'pm-toy' : 'calc-sub-w1', folder: tickFolder, home: 'C:/oke', wiki: 'C:/oke/wiki', claude_dir: 'C:/Users/u/.claude', job: 'calc-sub', seq: 2, events: [], events_offset: 0, write_areas: writeAreas, memories: ['C:/Users/u/.claude/projects/x/memory', 'C:/Users/u/.claude/projects/C--repo/memory'], board: tickRole === 'worker' ? null : tickBoard, recent: tickRecent }
  }
  if (args[0] === 'take-mail') {
    if (args.includes('--give-back')) return { text: null, given_back: 1 }
    const text = mailText
    mailText = null
    return text ? { text, names: ['1.txt'] } : { text: null }
  }
  if (args[0] === 'boundary') return { role: tickRole, answered: answeredNow, next: nextWork, min_percent: 30 }
  if (args[0] === 'path-check') {
    if (failCheck) return undefined
    const file = String(args[args.indexOf('--path') + 1])
    return /\/nested\/|^D:\/elsewhere/.test(file) ? { allow: false, reason: `${file} is not this job's to change` }
      : { allow: true, reason: null }
  }
  if (args[0] === 'api-retry' && args.includes('--clear')) return { action: 'cleared' }
  if (args.includes('authentication_failed')) return { action: 'give_up', limit: 20 }
  return { action: 'retry', delay: 30, limit: 20 }
}

function engine(on: any, runs: Run[], prompts: string[]) {
  on('process.run', (_: any, e: Run) => {
    runs.push({ argv: e.argv })
    return { value: { exitCode: 0, stdout: JSON.stringify(answer(e.argv.slice(2))), stderr: '' } }
  })
  on('command.register', () => ({ value: { command: 'oke-board' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.id', () => ({ value: 'sid-1' }))
  on('session.usage', () => ({ value: { startedAt: 0, context: { window: 200000, percent: contextPercent, breakdown: { isAutoCompactEnabled: autoCompact } }, rateLimits: [], cost: { usd: 1.2 } } }))
  on('session.model', () => ({ value: 'claude-sonnet-5-5' }))
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  on('prompt.submit', (_: any, e: any) => {
    if (dropMail && String(e.text).startsWith('OKe inbox')) return { drop: 'a Stop stands' }
    prompts.push(e.text)
    return { text: e.text }
  })
  on('turn.complete', () => ({ text: '' }))
  on('classic.StopFailure', () => ({}))
  on('fs.exists', (_: any, e: any) => ({
    value: /[\\/]\.new$/.test(String(e.path)) ? mailFlag : okeState && String(e.path).endsWith('state.json'),
  }))
  on('prompt.read', () => ({ value: { text: draft, cursor: draft.length } }))
}

const TURN = { answer: '', durationMs: 1, isAborted: false, turnId: 't1' }

test('an API-error turn is resubmitted after the delay oke.py gives', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  const runs: Run[] = []
  const prompts: string[] = []
  engine(on, runs, prompts)

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.classic.StopFailure({ error: 'overloaded' } as any)
  await $.turn.complete({ ...TURN, reason: 'error' } as any)

  const retry = runs.find(r => r.argv.includes('api-retry') && r.argv.includes('--attempt'))
  expect(retry?.argv.slice(-4)).toEqual(['--attempt', '1', '--error', 'overloaded'])
  await clock.advance(29_000)
  expect(prompts.length).toBe(0)
  await clock.advance(2_000)
  expect(prompts.length).toBe(1)
  expect(prompts[0]).toContain('API error (overloaded)')
})

test('a prompt typed during the backoff cancels the retry', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  const runs: Run[] = []
  const prompts: string[] = []
  engine(on, runs, prompts)

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.turn.complete({ ...TURN, reason: 'error' } as any)
  await $.prompt.submit({ text: 'person typed this' } as any)
  await clock.advance(60_000)
  expect(prompts).toEqual(['person typed this'])
})

test('a permanent error is not resubmitted', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  const runs: Run[] = []
  const prompts: string[] = []
  engine(on, runs, prompts)

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.classic.StopFailure({ error: 'authentication_failed' } as any)
  await $.turn.complete({ ...TURN, reason: 'error' } as any)
  await clock.advance(600_000)
  expect(prompts.length).toBe(0)
})

test('a subagent API error is not the main loop one', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  const runs: Run[] = []
  const prompts: string[] = []
  engine(on, runs, prompts)

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.classic.StopFailure({ error: 'overloaded', agent_id: 'a1' } as any)
  await $.turn.complete({ ...TURN, reason: 'error', agentId: 'a1' } as any)
  await clock.advance(600_000)
  expect(runs.some(r => r.argv.includes('--attempt'))).toBe(false)
  expect(prompts.length).toBe(0)
})

test('a retry does not submit over a draft the person is typing', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  const prompts: string[] = []
  engine(on, [], prompts)

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.turn.complete({ ...TURN, reason: 'error' } as any)
  draft = 'half a sentence'
  try {
    await clock.advance(60_000)
  } finally {
    draft = ''
  }
  expect(prompts.length).toBe(0)
})

test('without OKe state a plain session runs no python', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { USERPROFILE: 'C:/Users/u' })
  const runs: Run[] = []
  engine(on, runs, [])
  okeState = false
  try {
    await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
    await clock.advance(600_000)
  } finally {
    okeState = true
  }
  expect(runs.length).toBe(0)
})

test('outside herdr the session id is the place', async ($, on) => {
  mock.clock(on)
  mock.env(on, {})
  const runs: Run[] = []
  engine(on, runs, [])

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const place = runs[0]?.argv[runs[0].argv.indexOf('--pane') + 1]
  expect(place).toBe('session:sid-1')
})

test('a session that is no OKe agent ticks once a minute, a PM every 10 s', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  const runs: Run[] = []
  engine(on, runs, [])
  const ticks = () => runs.filter(r => r.argv.includes('tick')).length

  tickRole = null as any
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const first = ticks()
  await clock.advance(60_000)  // the first minute: every 10 s
  expect(ticks()).toBe(first + 6)
  await clock.advance(50_000)
  expect(ticks()).toBe(first + 6)
  await clock.advance(10_000)
  expect(ticks()).toBe(first + 7)
  tickRole = 'pm'  // spawn recorded it: from the next look on it is a PM
  await clock.advance(60_000)
  await clock.advance(10_000)
  expect(ticks()).toBe(first + 9)
  tickRole = 'worker'
})

const PANE = {
  plugin: 'oke', component: 'Pane', requestId: 'oke-board',
  props: { title: 'OKe board', isFocused: false, bodyColumns: 30, placement: 'dock',
           scroll: { offset: 0, bodyRows: 20 }, view: {} },
} as const

test('the board lists an agent and expands its board.md', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w7:p1' })
  tickRole = 'orchestrator'
  engine(on, [], [])

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ key: 'open-calc-sub-w1' })).toBeFalsy()  // agents show once the job is open
    await ui.press({ key: 'open-job:calc-sub' })
    expect((await ui.find({ key: 'open-calc-sub-w1' }))?.text).toMatch(/● w1 working .*revived 1x/)
    expect(await ui.find({ type: 'Text', text: /## Now/ })).toBeFalsy()
    expect(await ui.find({ key: 'go-calc-sub-w1' })).toBeFalsy()
    await ui.press({ key: 'open-calc-sub-w1' })
    expect(await ui.find({ type: 'Text', text: /## Now/ })).toBeTruthy()
    expect(await ui.find({ key: 'go-calc-sub-w1' })).toBeTruthy()
    await ui.press({ key: 'open-calc-sub-w1' })
    await ui.press({ key: 'open-job:calc-sub' })
    await ui.unmount()
  }
  tickRole = 'worker'
})

test('the board puts what waits on the user first, then each job in the user words, then the last done', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w7:p1' })
  tickRole = 'orchestrator'
  const long = 'make the calculator round every result to two decimals, also for division and the square root, and show it'
  tickRecent = [
    { job: 'calc-add', pm: 'toy', words: 'add an add button', phase: 'done', answer: false, result: 'the add button works', branch: 'oke/calc-add' },
    { job: 'why-slow', pm: 'gone', words: 'why is it slow?', phase: 'done', answer: true, result: 'the cache is off', branch: null }]
  tickBoard = [{ ...BOARD[0], jobs: [...BOARD[0]!.jobs, { job: 'calc-round', phase: 'awaiting_approval', deliver: 'none',
    pending: 'plan', words: long, step: 'awaiting approval', left: ['your approval', 'work', 'review', 'delivery'], agents: [] }] }]
  engine(on, [], [])

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const all = async () => (await ui.findAll({ type: 'Text' })).map((t: any) => t.text as string)
  let texts = await all()
  const at = (re: RegExp) => texts.findIndex((t: string) => re.test(t))
  expect(at(/To do for you: 1 approval · 1 question$/)).toBe(0)
  expect(await ui.find({ type: 'Text', text: /▸ add a subtract button to the calculator: PM asks/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /^▍toy$/ })).toBeTruthy()
  // words first, then where it is, then what is left
  const sub = at(/^add a subtract button to the calculator$/)
  expect(sub).toBeGreaterThan(0)
  expect(texts[sub + 1]).toMatch(/^\s*calc-sub\s+▸$/)
  expect(texts.slice(sub).find((t: string) => t.startsWith('now '))).toMatch(/^now\s+reviewing\s+PM asks$/)
  expect(texts.slice(sub).find((t: string) => t.startsWith('left '))).toBe('left delivery')
  expect(await ui.find({ type: 'Text', text: /left your approval › work › review › delivery/ })).toBeTruthy()
  // long words fold at 100 characters and open whole
  expect(await ui.find({ type: 'Text', text: `${long.slice(0, 100).trimEnd()}…` })).toBeTruthy()
  await ui.press({ key: 'open-job:calc-round' })
  expect(await ui.find({ type: 'Text', text: long })).toBeTruthy()
  // size, delivery, cost, PM and agents only once a job is open
  expect(await ui.find({ type: 'Text', text: /small\s+delivered ✓/ })).toBeFalsy()
  expect(await ui.find({ key: 'open-calc-sub-w1' })).toBeFalsy()
  await ui.press({ key: 'open-job:calc-sub' })
  expect(await ui.find({ type: 'Text', text: /small\s+delivered ✓\s+\$0\.40/ })).toBeTruthy()
  expect(await ui.find({ key: 'open-calc-sub-w1' })).toBeTruthy()
  // the last finished jobs, from the archive: words, result, branch; under their PM, a removed PM's last
  texts = await all()
  expect(at(/^Done$/)).toBeGreaterThan(at(/calc-sub/))
  expect(at(/^Done$/)).toBeLessThan(at(/add an add button/))
  expect(texts.lastIndexOf('Done')).toBeGreaterThan(at(/add an add button/))
  expect(texts.lastIndexOf('Done')).toBeLessThan(at(/why is it slow/))
  expect(await ui.find({ type: 'Text', text: '✓ You said: add an add button  calc-add · the add button works · branch oke/calc-add  ▸' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: '✓ You said: why is it slow?  why-slow · answered · the cache is off  ▸' })).toBeTruthy()
  // a done entry opens like a job card: the whole words, then its result on its own line
  await ui.press({ key: 'open-done:calc-add' })
  expect(await ui.find({ type: 'Text', text: '✓ You said: add an add button  calc-add  ▾' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: '  the add button works · branch oke/calc-add' })).toBeTruthy()
  // the user's words carry their label on the job cards too
  expect((await all()).filter((t: string) => t === 'You said: ').length).toBeGreaterThan(0)
  expect(await ui.find({ key: 'open-calc-add-w1' })).toBeFalsy()
  await ui.unmount()
  tickBoard = BOARD
  tickRecent = []
  tickRole = 'worker'
})

test('the step line names the PMs a job waits on', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w7:p1' })
  tickRole = 'orchestrator'
  tickBoard = [{ name: 'toy', key: 'toy', pm: null, jobs: [
    { job: 'calc-sub', phase: 'planning', deliver: 'none', pending: null, peer_wait: ['api'], agents: [] }] }]
  engine(on, [], [])

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /^now\s+plan\s+\(wait PM api\)$/ })).toBeTruthy()
  await ui.unmount()
  tickBoard = BOARD
  tickRole = 'worker'
})

test('the step line says how long the step runs and when the job approves without asking', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w7:p1' })
  tickRole = 'orchestrator'
  tickBoard = [{ name: 'toy', key: 'toy', pm: null, jobs: [
    { job: 'calc-sub', phase: 'planning', deliver: 'none', pending: null, step: 'planning', auto: true,
      since: Date.now() / 1000 - 12 * 60, agents: [] }] }]
  engine(on, [], [])

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /^now\s+planning\s+· 1[23]m\s+⏵ auto$/ })).toBeTruthy()
  await ui.unmount()
  tickBoard = BOARD
  tickRole = 'worker'
})

test('a held job shows its closed agents as off, like an idle PM, not as dead', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w7:p1' })
  tickRole = 'orchestrator'
  tickBoard = [{ name: 'toy', key: 'toy', pm: { agent: 'pm-toy', role: 'pm', status: 'off', board: '', todos: [] },
    jobs: [{ job: 'calc-sub', phase: 'working', deliver: 'none', pending: null, held: true, agents: [{
      agent: 'calc-sub-w1', role: 'worker', status: 'off', pane: null, report: null, board: '', revives: 0, todos: [] }] }] }]
  engine(on, [], [])

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /on hold/ })).toBeTruthy()
  await ui.press({ key: 'open-job:calc-sub' })
  expect((await ui.find({ key: 'open-calc-sub-w1' }))?.text).toMatch(/– w1 off/)
  expect(await ui.find({ type: 'Text', text: /✗|dead/ })).toBeFalsy()
  await ui.unmount()
  tickBoard = BOARD
  tickRole = 'worker'
})

test('the board marks done, current and open todos apart', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w7:p1' })
  tickRole = 'orchestrator'
  engine(on, [], [])

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /✓ write sub/ })).toBeFalsy()  // a job's agents show once it is open
  await ui.press({ key: 'open-job:calc-sub' })
  expect(await ui.find({ type: 'Text', text: /✓ write sub/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /▸ test sub/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /○ docs/ })).toBeTruthy()
  // in progress first with how it goes from board.md's Now, then planned, then done
  const texts = (await ui.findAll({ type: 'Text' })).map((t: any) => t.text)
  const at = (re: RegExp) => texts.findIndex((t: string) => re.test(t))
  expect(texts[at(/▸ test sub/) + 1]).toMatch(/^\s+└ writing sub$/)
  expect(at(/▸ test sub/)).toBeLessThan(at(/○ docs/))
  expect(at(/○ docs/)).toBeLessThan(at(/✓ write sub/))
  await ui.unmount()
  tickRole = 'worker'
})

test('task tool calls are written to the agent folder', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  engine(on, [], [])
  const writes: { path: string; text: string }[] = []
  on('fs.write', (_: any, e: any) => {
    writes.push({ path: e.path, text: e.text })
    return { value: undefined }
  })
  on('tool.call', (_: any, e: any) => (e.tool === 'TaskCreate'
    ? { result: { task: { id: '1', subject: e.subject } } }
    : { result: { success: true, taskId: e.taskId, updatedFields: ['status'] } }))

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.tool.call({ tool: 'TaskCreate', subject: 'write sub', description: 'd' } as any)
  await $.tool.call({ tool: 'TaskUpdate', taskId: '1', status: 'in_progress' } as any)
  const last = writes[writes.length - 1]!
  expect(last.path.replace(/\\/g, '/')).toMatch(/\/oke\/jobs\/j\/w1\/todos\.json$/)
  expect(JSON.parse(last.text)).toEqual([{ id: '1', content: 'write sub', status: 'in_progress' }])
})

test('task calls of a subagent stay off the agent board', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  engine(on, [], [])
  const writes: string[] = []
  on('fs.write', (_: any, e: any) => {
    writes.push(e.path)
    return { value: undefined }
  })
  on('tool.call', (_: any, e: any) => ({ result: { task: { id: '1', subject: e.subject } } }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.tool.call({ tool: 'TaskCreate', subject: 'sub', description: 'd', agentId: 'a1' } as any)
  expect(writes.filter(p => p.endsWith('todos.json'))).toEqual([])
})

test('a worker writes nothing in the jobs folder outside its own folder', () => {
  const own = 'C:/oke/jobs/j/j-w1'
  for (const bad of ['C:/oke/jobs/j/j-rv/report.md', 'c:\\oke\\jobs\\j\\plan.md', 'C:/oke/jobs/j/ledger.tsv',
    'C:/oke/jobs/j/j-w1/../j-rv/report.md', 'C:/oke/jobs/k/k-rv/report.md']) {
    expect(workerDenied('Write', { file_path: bad }, own)).toMatch(/^OKe workers/)
  }
  for (const ok of ['C:/oke/jobs/j/j-w1/report.md', 'C:/repo/src/a.py', 'C:/oke/wiki/x.md', 'C:/repo/src/../lib/x.py']) {
    expect(workerDenied('Edit', { file_path: ok }, own)).toBeNull()
  }
  expect(workerDenied('Bash', { command: 'ls' }, own)).toBeNull()
  expect(workerDenied('Write', { file_path: 'C:/oke/jobs/j/plan.md' }, null)).toBeNull()
})

const BAND = {
  plugin: 'oke', component: 'AbovePrompt', requestId: 'band', surface: 'terminal',
  props: { hasSurvey: false, isCollapsed: false, isWorking: false, maxRows: 20, bodyColumns: 80,
    scroll: { offset: 0, bodyRows: 20 }, view: {} },
} as any
const wide = (bodyColumns: number) => ({ ...BAND, props: { ...BAND.props, bodyColumns } })

test('the band shows the mascot, the status line and the org chart', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w7:p1' })
  tickRole = 'orchestrator'
  engine(on, [], [])

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount(BAND)
  expect(await ui.find({ key: 'conductor' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /your turn!/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: 'OKe orchestrator · waiting on you 1 · ctx 34% · $1.20' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /^toy$/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /└ calc-sub waiting \$0\.40 w1●/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /calc-add/ })).toBeFalsy()  // done: off the chart
  await ui.unmount()
  tickRole = 'worker'
})

test('a narrow band drops the least needed parts of the status line first', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w7:p1' })
  tickRole = 'orchestrator'
  engine(on, [], [])

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount(wide(50))
  expect(await ui.find({ type: 'Text', text: 'OKe orchestrator · waiting on you 1' })).toBeTruthy()
  await ui.unmount()
  tickRole = 'worker'
})

test('more than three open jobs fold under one row that opens them', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w7:p1' })
  tickRole = 'orchestrator'
  const job = (name: string) => ({ job: name, phase: 'working', deliver: 'none', pending: null, agents: [{
    agent: `${name}-w1`, role: 'worker', status: 'working', pane: null, report: null, board: '', revives: 0, todos: [] }] })
  // e: a PM's background subagent works on it, with no pane of its own; f: nobody works on it
  const quiet = (name: string, subagent: boolean) => ({ ...job(name), agents: [], subagent })
  tickBoard = [{ name: 'toy', key: 'toy', pm: null,
    jobs: [...['a', 'b', 'c', 'd'].map(job), quiet('e', true), quiet('f', false)] }]
  engine(on, [], [])

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount(BAND)
  expect(await ui.find({ type: 'Text', text: '▸ 6 jobs · 5 working' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /├ a work/ })).toBeFalsy()
  expect(await ui.find({ type: 'Text', text: /^toy$/ })).toBeTruthy()  // the repo row stays
  await ui.press({ key: 'jobs' })
  expect(await ui.find({ type: 'Text', text: /├ a work w1●/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /├ d work/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /└ f work/ })).toBeTruthy()
  await ui.unmount()
  tickBoard = BOARD
  tickRole = 'worker'
})

test('a worker status line shows its job, seq, model, context and cost', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  engine(on, [], [])

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount(wide(120))
  expect(await ui.find({ type: 'Text', text: 'OKe worker · calc-sub #2 · sonnet-5-5 · ctx 34% · $1.20' })).toBeTruthy()
  await ui.unmount()
})

test('a finished turn writes the session cost to the agent folder', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  engine(on, [], [])
  const writes: { path: string; text: string }[] = []
  on('fs.write', (_: any, e: any) => {
    writes.push({ path: e.path, text: e.text })
    return { value: undefined }
  })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.turn.complete({ ...TURN, reason: 'completed' } as any)
  const usage = writes.find(w => /usage\.json$/.test(w.path))
  expect(usage?.path.replace(/\\/g, '/')).toMatch(/\/oke\/jobs\/j\/w1\/usage\.json$/)
  expect(JSON.parse(usage!.text)).toEqual({ usd: 1.2 })
})

test('each turn, a subagent turn too, adds a line to the agent usage.csv', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  engine(on, [], [])
  let csv: string | null = null
  on('fs.read', () => (csv === null ? { deny: 'missing' } : { value: csv }))
  on('fs.write', (_: any, e: any) => {
    if (/usage\.csv$/.test(e.path)) csv = e.text
    return { value: undefined }
  })
  const usage = { model: 'claude-sonnet-5-5', input_tokens: 10, output_tokens: 20,
    cache_read_input_tokens: 300, cache_creation_input_tokens: 40 }

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.turn.complete({ ...TURN, reason: 'completed', usage } as any)
  await $.turn.complete({ ...TURN, reason: 'completed', usage, agentId: 'a1' } as any)
  const lines = csv!.trim().split('\n')
  expect(lines[0]).toBe('time,model,input,output,cache_read,cache_write')
  expect(lines.length).toBe(3)
  expect(lines[2]).toMatch(/,claude-sonnet-5-5,10,20,300,40$/)
})

test('the orchestrator board shows its own todo list', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w7:p1' })
  tickRole = 'orchestrator'
  engine(on, [], [])
  on('fs.write', () => ({ value: undefined }))
  on('tool.call', (_: any, e: any) => ({ result: { oldTodos: [], newTodos: e.todos } }))

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.tool.call({ tool: 'TodoWrite', todos: [
    { content: 'intake calc-pow', status: 'completed', activeForm: 'a' },
    { content: 'relay plan', status: 'in_progress', activeForm: 'b' }] } as any)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /OKe todo 1\/2/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /✓ intake calc-pow/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /▸ relay plan/ })).toBeTruthy()
  await ui.unmount()
  tickRole = 'worker'
})

test('a worker compacts once at each new task boundary', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  engine(on, [], [])
  on('fs.write', () => ({ value: undefined }))
  const compacts: any[] = []
  on('session.compact', (_: any, e: any) => {
    compacts.push(e)
    return { skip: 'test' }
  })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.turn.complete({ ...TURN, reason: 'completed' } as any)
  await clock.advance(10)
  expect(compacts.length).toBe(0)  // no report yet
  answeredNow = ['calc-sub:1']
  await $.turn.complete({ ...TURN, reason: 'completed' } as any)
  await clock.advance(10)
  expect(compacts.length).toBe(1)
  expect(compacts[0].instructions).toMatch(/task boundary/)
  await $.turn.complete({ ...TURN, reason: 'completed' } as any)
  await clock.advance(10)
  expect(compacts.length).toBe(1)  // the same boundary
  answeredNow = []
})

test('a boundary compaction keeps what the next work needs', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  engine(on, [], [])
  on('fs.write', () => ({ value: undefined }))
  const compacts: any[] = []
  on('session.compact', (_: any, e: any) => {
    compacts.push(e)
    return { skip: 'test' }
  })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  nextWork = 'job calc-mul is open (working): add mul'
  answeredNow = ['calc-sub:1']
  await $.turn.complete({ ...TURN, reason: 'completed' } as any)
  await clock.advance(10)
  expect(compacts.length).toBe(1)
  expect(compacts[0].instructions).toMatch(/report for calc-sub:1 is written/)
  expect(compacts[0].instructions).toMatch(/Next: job calc-mul is open \(working\): add mul\. Keep in full/)
  answeredNow = []
  nextWork = ''
})

test('a boundary with little context used does not compact', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  engine(on, [], [])
  on('fs.write', () => ({ value: undefined }))
  contextPercent = 12
  const compacts: any[] = []
  on('session.compact', (_: any, e: any) => {
    compacts.push(e)
    return { skip: 'test' }
  })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  answeredNow = ['calc-sub:1']
  await $.turn.complete({ ...TURN, reason: 'completed' } as any)
  await clock.advance(10)
  expect(compacts.length).toBe(0)
  contextPercent = 40
  await $.turn.complete({ ...TURN, reason: 'completed' } as any)
  await clock.advance(10)
  expect(compacts.length).toBe(0)  // that boundary passed: the next one decides again
  answeredNow = []
  contextPercent = 34
})

test('auto-compact turned off by the person wins over a boundary', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  engine(on, [], [])
  on('fs.write', () => ({ value: undefined }))
  autoCompact = false
  const compacts: any[] = []
  on('session.compact', (_: any, e: any) => {
    compacts.push(e)
    return { skip: 'test' }
  })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  answeredNow = ['calc-sub:1']
  await $.turn.complete({ ...TURN, reason: 'completed' } as any)
  await clock.advance(10)
  expect(compacts.length).toBe(0)
  answeredNow = []
  autoCompact = true
})

test('the orchestrator never compacts by itself', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w7:p1' })
  tickRole = 'orchestrator'
  const runs: Run[] = []
  engine(on, runs, [])
  const compacts: any[] = []
  on('session.compact', (_: any, e: any) => {
    compacts.push(e)
    return { skip: 'test' }
  })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  answeredNow = ['calc-sub:1']
  await $.turn.complete({ ...TURN, reason: 'completed' } as any)
  await clock.advance(10)
  expect(compacts.length).toBe(0)
  expect(runs.some(r => r.argv.includes('boundary'))).toBe(false)
  answeredNow = []
  tickRole = 'worker'
})

test('the board shows to decide, to check and blocked from board.md, not what is decided', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w7:p1' })
  tickRole = 'orchestrator'
  tickBoard = [{ name: 'toy', key: 'toy', jobs: [], pm: {
    agent: 'pm-toy', role: 'pm', status: 'working', pane: null, report: null, revives: 0, todos: [],
    board: 'agent: pm-toy\n---\n## Now\nplanning\n## Decisions        (decided, with why)\n- JWT over sessions: no shared store\n'
      + '## Decision needed\nuser: keep the old endpoint?\n## To check\nnone\n## Blocked\nnone' } }]
  engine(on, [], [])

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /JWT over sessions/ })).toBeFalsy()  // decided: only in the opened board.md
  expect(await ui.find({ type: 'Text', text: /\? decide\s+user: keep the old endpoint\?$/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /check|blocked/ })).toBeFalsy()  // none is empty
  await ui.unmount()
  tickBoard = BOARD
  tickRole = 'worker'
})

test('a PM, worker or reviewer cannot run git commands that lose or publish work', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  engine(on, [], [])
  const ran: string[] = []
  on('tool.call', (_: any, e: any) => {
    ran.push(e.command)
    return { result: { stdout: '' } }
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  for (const bad of ['git push origin main', 'git -C "C:/a b/r" push --force', 'cd r && git reset --hard HEAD~1',
    'git -c core.x=1 clean -fd', 'git branch -D oke/j', 'git stash drop', 'git -c "a=b c" push',
    'git clean --force', 'git checkout -f main', 'x=$(git push)',
    'for b in a c; do git push origin $b; done', 'if x; then git push; fi', '`git push`',
    'git --git-dir .git push', 'command git push', '/usr/bin/git push', 'time git reset --hard',
    'env A=1 git push', 'C:/Git/cmd/git.exe push']) {
    const out: any = await $.tool.call({ tool: 'Bash', command: bad } as any)
    expect(out.deny).toMatch(/^OKe blocks `git /)
  }
  for (const ok of ['git -C r diff main...HEAD', 'git -C r commit -m "push the button"', 'git rebase main',
    'git -C r reset HEAD file.py', 'git checkout -- calc.py', 'git commit -m "why we avoid git push"',
    'grep -rn "git reset --hard" .', "git commit -F - <<'EOF'\nnever git push here\nEOF",
    'git commit -m "docs: then git push the tag"', 'grep -n "do git reset --hard" notes.md',
    'echo then git push the tag', 'git log --grep time git push']) {
    const out: any = await $.tool.call({ tool: 'Bash', command: ok } as any)
    expect(out.deny).toBeUndefined()
  }
  expect(ran.length).toBe(12)
})

test('workers, reviewers and subagents may not stash or throw away the whole tree', () => {
  for (const bad of ['git stash', 'git stash push -m x', 'git -C r stash pop', 'git checkout -- .', 'git checkout .',
    'git restore .', 'git restore --staged .', 'git restore -- ./', 'cd r && git checkout -- . && ls',
    'git reset --hard', 'git clean -fdx', 'git branch -D oke/j', 'git push']) {
    for (const role of ['worker', 'reviewer']) expect(gitDenied(bad, role)).toMatch(/^OKe blocks `git /)
    expect(gitDenied(bad, 'pm', true)).toMatch(/^OKe blocks `git /)  // a PM's subagent
  }
  for (const ok of ['git checkout -- calc.py', 'git restore src/a.py', 'git checkout .gitignore',
    'git checkout -b oke/j', 'git status', 'git commit -m "git stash it later"']) {
    expect(gitDenied(ok, 'worker')).toBeNull()
  }
  // the PM itself cleans up its own worktrees
  expect(gitDenied('git checkout -- .', 'pm')).toBeNull()
  expect(gitDenied('git stash', 'pm')).toBeNull()
})

test('no way round the whole-tree rule: a revision, a pathspec of everything, a forced switch', () => {
  for (const bad of ['git checkout HEAD -- .', 'git checkout HEAD .', 'git checkout main~2 -- .', 'git restore -s HEAD .',
    'git restore --source=HEAD .', 'git restore --source HEAD -- .', 'git rm -r .', 'git rm -rf -- .',
    "git checkout -- '*'", 'git checkout -- "*"', 'git checkout -- :/', 'git restore :/', 'git switch -f main',
    'git switch --discard-changes main', 'git switch --force main']) {
    expect(gitDenied(bad, 'worker')).toMatch(/^OKe blocks `git /)
    expect(gitDenied(bad, 'pm', true)).toMatch(/^OKe blocks `git /)
  }
  for (const ok of ['git checkout HEAD -- calc.py', 'git restore -s HEAD src/a.py', 'git rm -r old/', 'git switch oke/j',
    'git rm calc.py']) {
    expect(gitDenied(ok, 'worker')).toBeNull()
  }
})

test('a worker in the user\'s checkout and a PM\'s subagents never commit; the PM and other workers may', () => {
  for (const bad of ['git commit -m x', 'git -C r commit -am x', 'git revert HEAD', 'git cherry-pick abc',
    'git merge main', 'git rebase main', 'git am p.patch']) {
    expect(gitDenied(bad, 'worker', false, true)).toMatch(/^OKe blocks `git /)
    expect(gitDenied(bad, 'pm', true, true)).toMatch(/^OKe blocks `git /)
  }
  expect(gitDenied('git commit -m x', 'worker')).toBeNull()  // a worktree of the job's own
  expect(gitDenied('git commit -m x', 'pm')).toBeNull()
  // the orchestrator never commits (the tool.call hook passes noCommit for it)
  expect(gitDenied('cd r && git add a.py && git commit -m x', 'orchestrator', false, true)).toMatch(/Its PM delivers it/)
  expect(gitDenied('grep -n "git commit" notes.md', 'worker', false, true)).toBeNull()
  expect(gitDenied('git log --oneline', 'worker', false, true)).toBeNull()
})

test('the board reads a released job and a rule block plainly, and counts no auto-taken plan', () => {
  expect(pendingWords({ pending: 'released' })).toContain('starts now')
  expect(pendingWords({ pending: 'blocked', oke_rule: true })).toBe('blocked by an OKe rule')
  expect(pendingWords({ pending: 'blocked' })).toBe('PM is blocked')
  expect(todoLine([{ pending: 'plan', phase: 'awaiting_approval', auto_pending: true },
    { pending: 'plan', phase: 'awaiting_approval' }, { pending: 'question', phase: 'working' }]))
    .toBe('To do for you: 1 approval · 1 question')
})

test('the orchestrator opens a pull request through oke, never with git push', () => {
  expect(gitDenied('"$OKE" job pr fix-a', 'orchestrator')).toBeNull()
  expect(gitDenied('python C:/oke/bin/oke.py job pr fix-a', 'orchestrator')).toBeNull()
  expect(gitDenied('git push origin oke/fix-a', 'orchestrator')).toMatch(/^OKe blocks `git push`/)
  expect(gitDenied('git push origin oke/fix-a', 'pm')).toContain('oke job deliver')
})

test('a reviewer writes only its own folder and runs only read-only git', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p2' })
  tickRole = 'reviewer'
  engine(on, [], [])
  on('tool.call', () => ({ result: { stdout: '' } }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const call = (e: any) => $.tool.call(e as any) as Promise<any>
  for (const bad of [{ tool: 'Edit', file_path: '/repo/calc.py' }, { tool: 'Write', file_path: '/oke/jobs/j/w1x/report.md' },
    { tool: 'Bash', command: 'python -c "open(1)"' }, { tool: 'Bash', command: 'git diff --output=x' },
    { tool: 'Bash', command: 'git diff && rm -rf .' }, { tool: 'Bash', command: 'git apply p.diff' },
    { tool: 'Bash', command: 'git -c core.pager=sh log' }, { tool: 'Bash', command: 'git diff "$(touch x)"' },
    { tool: 'Bash', command: 'git status\nrm -rf src' }, { tool: 'Write', file_path: '/oke/jobs/j/w1/../../../repo/a.py' },
    { tool: 'Write', file_path: 'w1/report.md' }, { tool: 'Agent', prompt: 'edit calc.py' },
    { tool: 'mcp__fs__write', path: '/repo/a' }]) {
    expect((await call(bad)).deny).toMatch(/^OKe reviewers/)
  }
  for (const ok of [{ tool: 'Write', file_path: '\\oke\\jobs\\j\\w1\\report.md' }, { tool: 'Read', file_path: '/repo/a' },
    { tool: 'Bash', command: 'git -C r diff main...HEAD' }, { tool: 'Bash', command: 'git log --oneline -5' }]) {
    expect((await call(ok)).deny).toBeUndefined()
  }
  tickRole = 'worker'
})

test('the orchestrator writes only the OKe home and starts no subagent', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p1' })
  tickRole = 'orchestrator'
  engine(on, [], [])
  on('tool.call', () => ({ result: { stdout: '' } }))
  await $.session.start({ cwd: 'C:/work/toy', surface: 'terminal', isInteractive: true })
  const call = (e: any) => $.tool.call(e as any) as Promise<any>
  for (const bad of [{ tool: 'Edit', file_path: 'C:/repo/calc.py' }, { tool: 'Write', file_path: 'calc.py' },
    { tool: 'Write', file_path: 'C:/oke/../repo/a.py' }, { tool: 'NotebookEdit', notebook_path: 'C:/repo/n.ipynb' },
    { tool: 'Agent', prompt: 'edit calc.py' }, { tool: 'Workflow', script: 'agent("edit calc.py")' },
    { tool: 'Edit', file_path: 'C:/repo/.claude/worktrees/f/calc.py' },
    { tool: 'Write', file_path: 'C:/repo/.claude/commands/deploy.md' }, { tool: 'Edit', file_path: 'C:/Users/u/.claude/settings.json' },
    { tool: 'Write', file_path: 'C:/Users/u/.claude/plugins/cache/oke/hooks/register.tsx' },
    { tool: 'Write', file_path: 'C:/oke/wiki/toy/index.md' }, { tool: 'Write', file_path: 'C:/oke/./wiki/toy/index.md' },
    { tool: 'Write', file_path: 'C:/oke//wiki/toy/index.md' }, { tool: 'Write', file_path: 'C:/oke/wiki./toy/index.md' },
    { tool: 'Write', file_path: 'C:/oke/wiki /toy/index.md' }, { tool: 'Write', file_path: 'C:/oke/KNOWLE~1/x.md' },
    { tool: 'Write', file_path: 'C:/Users/u/.claude/projects/other/memory/MEMORY.md' }]) {
    expect((await call(bad)).deny).toMatch(/^OKe orchestrators/)
  }
  expect((await call({ tool: 'Bash', command: 'git -C C:/repo reset --hard' })).deny).toMatch(/^OKe blocks `git reset/)
  expect((await call({ tool: 'Bash', command: 'git push' })).deny).toMatch(/Tell the user to run it themselves if they want it\.$/)
  for (const ok of [{ tool: 'Write', file_path: 'C:\\oke\\jobs\\j\\pm\\assignment.md' },
    { tool: 'Read', file_path: 'C:/repo/a' },
    { tool: 'Bash', command: 'python oke.py pending' }]) {
    expect((await call(ok)).deny).toBeUndefined()
  }
  tickRole = 'worker'
})

test('the orchestrator may write its own memory, not that of another project', () => {
  const dirs = { home: 'C:/oke', wiki: 'C:/oke/wiki', claude: 'C:\\Users\\u\\.claude', root: 'C:\\work\\toy' }
  const write = (file_path: string) => orchestratorDenied('Write', { file_path }, dirs)
  expect(write('C:\\Users\\u\\.claude\\projects\\C--work-toy\\memory\\a.md')).toBeNull()
  expect(write('C:/Users/u/.claude/projects/other/memory/a.md')).toMatch(/^OKe orchestrators/)
  expect(orchestratorDenied('Write', { file_path: 'C:/Users/u/.claude/projects/C--work-toy/memory/a.md' },
    { ...dirs, root: null })).toMatch(/^OKe orchestrators/)  // no root known: refused
  // a job's plan is the PM's; its intake is the orchestrator's
  expect(write('C:/oke/jobs/fix-a/plan.md')).toMatch(/never change a plan/)
  expect(write('C:/oke/jobs/fix-a/intake.md')).toBeNull()
  expect(write('C:/oke/jobs/fix-a/pm/report.md')).toMatch(/never write a report/)
  expect(write('C:/oke/jobs/fix-a/pm/assignment.md')).toBeNull()
})

test('the first or revive prompt names the role and folder', () => {
  expect(promptRole('You are a dispatched worker...\nUse the oke:reviewer skill. Your assignment file is '
    + 'C:/oke/jobs/j/j-rv/assignment.md. Read it first, then follow it.'))
    .toEqual({ role: 'reviewer', folder: 'C:/oke/jobs/j/j-rv' })
  expect(promptRole('You were restarted after a crash. Use the oke:pm skill. Re-read your assignment file '
    + '/h/repos/r/pm/assignment.md and your board.md')).toEqual({ role: 'pm', folder: '/h/repos/r/pm' })
  expect(promptRole('Use the oke:worker skill. Your assignment file is C:/Users/A B/.oke/jobs/j/j-w1/assignment.md. Read'))
    .toEqual({ role: 'worker', folder: 'C:/Users/A B/.oke/jobs/j/j-w1' })
  expect(promptRole('Call the Skill tool with the oke:pm skill. Do that first: read or change nothing before it. '
    + 'Your assignment file is C:/h/pms/conv/assignment.md: then read it and follow it as the skill says.'))
    .toEqual({ role: 'pm', folder: 'C:/h/pms/conv' })
  expect(promptRole('You were restarted after a crash. Call the Skill tool with the oke:worker skill. Do that first, '
    + 'then re-read your assignment file /h/jobs/j/j-w1/assignment.md and your board.md, and continue.'))
    .toEqual({ role: 'worker', folder: '/h/jobs/j/j-w1' })
  expect(promptRole('please use the oke skill')).toBeNull()
})

test('the orchestrator notifies once when a new item waits on the person', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w7:p1' })
  tickRole = 'orchestrator'
  engine(on, [], [])
  const notes: string[] = []
  on('ui.notify', (_: any, e: any) => {
    notes.push(JSON.stringify(e))
    return { value: { isSent: true as const, channel: 'terminal_bell' as const } }
  })
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const plan = (b: any) => [{ ...b[0], jobs: [b[0].jobs[0], { ...b[0].jobs[1], phase: 'awaiting_approval', pending: 'plan' }] }]
  tickBoard = plan(BOARD)
  await clock.advance(10_000)
  await clock.advance(10_000)  // still waiting: no second notification
  const add = notes.filter(n => n.includes('calc-add: approve the plan'))
  expect(add.length).toBe(1)
  expect(add[0]).toContain('"title":"OKe"')
  tickBoard = BOARD
  tickRole = 'worker'
})

test('each theme gives every role its own 12x8 character in every mood', () => {
  expect(THEME_NAMES.length >= 50).toBe(true)
  for (const theme of THEME_NAMES) {
    const pal = paletteOf(theme)
    const seen = new Set<string>()
    for (const role of ['orchestrator', 'pm', 'worker', 'reviewer']) {
      const moods = framesOf(theme, role)
      for (const frames of Object.values(moods)) {
        expect(frames.length > 0).toBe(true)
        for (const f of frames) {
          expect(f.length).toBe(8)
          for (const row of f) {
            expect(/^[.a-zA-Z]{12}$/.test(row)).toBe(true)
            expect([...row].filter(ch => ch !== '.' && pal[ch] === undefined).join('')).toBe('') // every pixel has a color
          }
        }
      }
      seen.add(moods.conduct[0]!.join('/'))
    }
    expect(seen.size).toBe(4)
  }
  expect(framesOf('nope', 'pm')).toBe(framesOf('disco', 'pm'))
  expect(framesOf('constructor', 'pm')).toBe(framesOf('disco', 'pm'))
})

test('no agent tool call may name the ledger key', async () => {
  expect(keyDenied({ command: 'cat ~/.oke/ledger.key' })).toContain('oke.py only')
  expect(keyDenied({ file_path: 'C:/oke/LEDGER.KEY' })).toContain('oke.py only')
  expect(keyDenied({ pattern: '.', path: '/home/u/.oke/ledger.key' })).toContain('oke.py only')
  expect(keyDenied({ file_path: 'oke.py', old_string: 'LEDGER_KEY = "ledger.key"' })).toBeNull()
  expect(keyDenied({ command: 'python oke.py ledger show fix-a' })).toBeNull()
})


async function mailSession($: any, on: any, as: string, setup?: () => void) {
  const clock = mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  tickRole = as
  const runs: Run[] = []
  const prompts: string[] = []
  engine(on, runs, prompts)
  setup?.()
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  return { clock, runs, prompts, takes: () => runs.filter(r => r.argv.includes('take-mail')) }
}

test('a PM takes its mail as soon as its turn ends, though a background subagent still runs', async ($, on) => {
  const s = await mailSession($, on, 'pm')
  await $.prompt.submit({ text: 'Use the oke:pm skill.' } as any)
  mailFlag = true
  mailText = 'OKe inbox: orchestrator: Job b: assignment updated (seq 1).'
  await s.clock.advance(10_000)
  expect(s.takes().length).toBe(0)  // its turn still runs
  await $.turn.complete({ ...TURN, reason: 'completed' } as any)
  await s.clock.advance(1)
  expect(s.prompts).toContain('OKe inbox: orchestrator: Job b: assignment updated (seq 1).')
  const take = s.takes()[0]!
  expect(take.argv.slice(-4)).toEqual(['--agent', 'pm-toy', '--pane', 'w1:p5'])
  mailFlag = false
  tickRole = 'worker'
})

test('without the new-mail file the mod runs no python for mail', async ($, on) => {
  const s = await mailSession($, on, 'worker')
  await $.prompt.submit({ text: 'go' } as any)
  await $.turn.complete({ ...TURN, reason: 'completed' } as any)
  await s.clock.advance(30_000)
  expect(s.takes().length).toBe(0)
})

test('a worker checks for mail every tick once idle, and not while the person types', async ($, on) => {
  const s = await mailSession($, on, 'worker')
  await $.prompt.submit({ text: 'go' } as any)
  await $.turn.complete({ ...TURN, reason: 'completed' } as any)
  mailFlag = true
  draft = 'half a sentence'
  await s.clock.advance(10_000)
  expect(s.takes().length).toBe(0)
  draft = ''
  mailText = 'OKe inbox: pm-toy: new instruction (seq 3).'
  await s.clock.advance(10_000)
  expect(s.prompts).toContain('OKe inbox: pm-toy: new instruction (seq 3).')
  mailFlag = false
})

test('the orchestrator has no inbox to take', async ($, on) => {
  const s = await mailSession($, on, 'orchestrator')
  mailFlag = true
  await $.prompt.submit({ text: 'hi' } as any)
  await $.turn.complete({ ...TURN, reason: 'completed' } as any)
  await s.clock.advance(10_000)
  expect(s.takes().length).toBe(0)
  mailFlag = false
  tickRole = 'worker'
})

test('a refused mail prompt gives the mail back and the next end of turn tries again', async ($, on) => {
  const s = await mailSession($, on, 'worker')
  await $.prompt.submit({ text: 'go' } as any)
  mailFlag = true
  mailText = 'OKe inbox: pm-toy: one'
  dropMail = true
  await $.turn.complete({ ...TURN, reason: 'completed' } as any)
  await s.clock.advance(1)
  const back = s.runs.find(r => r.argv.includes('--give-back'))
  expect(back?.argv.slice(-2)).toEqual(['--give-back', '1.txt'])
  dropMail = false
  mailText = 'OKe inbox: pm-toy: one'
  await s.clock.advance(10_000)  // not stuck: the next look takes it
  expect(s.prompts).toContain('OKe inbox: pm-toy: one')
  mailFlag = false
})

test('no mail while an API-error retry waits or after the person pressed Esc', async ($, on) => {
  const s = await mailSession($, on, 'worker')
  mailFlag = true
  mailText = 'OKe inbox: pm-toy: later'
  await $.prompt.submit({ text: 'go' } as any)
  await $.classic.StopFailure({ error: 'overloaded' } as any)
  await $.turn.complete({ ...TURN, reason: 'error' } as any)
  await s.clock.advance(10_000)
  expect(s.takes().length).toBe(0)
  await $.prompt.submit({ text: 'person: stop that' } as any)  // cancels the retry
  await $.turn.complete({ ...TURN, reason: 'aborted' } as any)
  await s.clock.advance(20_000)
  expect(s.takes().length).toBe(0)
  await $.prompt.submit({ text: 'person: go on' } as any)
  await $.turn.complete({ ...TURN, reason: 'completed' } as any)
  await s.clock.advance(1)
  expect(s.prompts).toContain('OKe inbox: pm-toy: later')
  mailFlag = false
})

test('a compaction at a task boundary runs first, then the mail goes in', async ($, on) => {
  let compacted = 0
  const s = await mailSession($, on, 'worker', () => {
    on('fs.write', () => ({ value: undefined }))
    on('session.compact', () => {
      compacted += 1
      return { skip: 'test' }
    })
  })
  await $.prompt.submit({ text: 'go' } as any)
  mailFlag = true
  mailText = 'OKe inbox: pm-toy: after compact'
  answeredNow = ['calc-sub:2']
  await $.turn.complete({ ...TURN, reason: 'completed' } as any)
  expect(s.takes().length).toBe(0)
  await s.clock.advance(1)
  expect(compacted).toBe(1)
  expect(s.prompts).toContain('OKe inbox: pm-toy: after compact')
  answeredNow = []
  mailFlag = false
})

test('a child that spawn recorded only after its first turn began still takes its mail', async ($, on) => {
  unrecorded = true
  const s = await mailSession($, on, 'worker')
  await $.prompt.submit({ text: 'Use the oke:worker skill.' } as any)
  unrecorded = false  // spawn recorded it meanwhile
  mailFlag = true
  mailText = 'OKe inbox: pm-toy: Stop and wait'
  await $.turn.complete({ ...TURN, reason: 'completed' } as any)
  await s.clock.advance(1)
  expect(s.prompts).toContain('OKe inbox: pm-toy: Stop and wait')
  mailFlag = false
})

test('a write in its own folder, tmp, memory or a git job area needs no python; others ask oke path-check', () => {
  const dirs = { home: 'C:/oke', wiki: 'C:/oke/wiki', folder: 'C:/oke/jobs/j/j-w1',
    memories: ['C:/u/.claude/projects/x/memory', 'C:/u/.claude/projects/C--repo/memory'],
    areas: [{ path: 'C:/repo/wt', direct: false }, { path: 'C:/u/notes', direct: true }] }
  expect(writeVerdict('C:\\oke\\jobs\\j\\j-w1\\report.md', dirs)).toBe('allow')
  expect(writeVerdict('C:/oke/tmp/page.md', dirs)).toBe('allow')
  expect(writeVerdict('C:/u/.claude/projects/x/memory/a.md', dirs)).toBe('allow')
  expect(writeVerdict('C:/u/.claude/projects/C--repo/memory/a.md', dirs)).toBe('allow')
  expect(writeVerdict('C:/u/.claude/projects/other/memory/a.md', dirs)).toBe('ask')
  expect(writeVerdict('C:/repo/wt/src/a.py', dirs)).toBe('allow')
  // the guard's own inputs and other agents' files: python decides (and refuses)
  for (const f of ['C:/oke/state.json', 'C:/oke/config.json', 'C:/oke/pms/other/board.md', 'C:/oke/automations/a.json',
    'C:/oke/jobs/j/plan.md', 'C:/oke/jobs/j/j-w1/settings.json', 'C:/oke/jobs/j/j-w1/lean-settings.json',
    'C:/oke/wiki/pm/a.md', 'C:/u/notes/a.md', 'C:/repo/wt2/a.py', 'C:/oke/../repo/a.py', 'C:/oke/WIKI~1/pm/a.md']) {
    expect([f, writeVerdict(f, dirs)]).toEqual([f, 'ask'])
  }
  // a folder job on the home folder does not hide a git job's worktree under it
  const home = { ...dirs, areas: [{ path: 'C:/u', direct: true }, { path: 'C:/u/repo/wt', direct: false }] }
  expect(writeVerdict('C:/u/repo/wt/a.py', home)).toBe('allow')
  expect(writeVerdict('C:/u/notes/a.md', home)).toBe('ask')
  // Git Bash paths on Windows; case counts only off Windows
  expect(writeVerdict('/c/repo/wt/a.py', dirs)).toBe('allow')
  const posix = { home: '/h/.oke', wiki: '/h/.oke/wiki', folder: null, memories: [], areas: [{ path: '/src/app', direct: false }] }
  expect(writeVerdict('/src/app/a.py', posix)).toBe('allow')
  expect(writeVerdict('/SRC/App/a.py', posix)).toBe('ask')
  // in a job's folder: OKe's file cache and the copies of the user's files are OKe's alone
  const job = { ...dirs, folder: 'C:/oke/jobs/j' }
  expect(writeVerdict('C:/oke/jobs/j/notes.md', job)).toBe('allow')
  for (const f of ['C:/oke/jobs/j/marks.json', 'C:/oke/jobs/j/dirty-before/src/a.py', 'C:/oke/jobs/j/dirty-before',
    'C:/oke/jobs/j/dirty-before.new-12/a.py']) {
    expect([f, writeVerdict(f, job)]).toEqual([f, 'ask'])
  }
})

test('only the orchestrator runs the oke job actions that record the user or move a job', () => {
  for (const c of ['python /p/oke.py job phase j done', '"$OKE" job auto j on', '$OKE job files j include a.py',
    'python3 "C:/x/oke.py" job accept-check j tree:abc --by-user', '${OKE} job accept-head j',
    '$OKE job output j result --no-change']) {
    for (const role of ['pm', 'worker', 'reviewer']) expect([c, role, jobActionDenied(c, role) !== null]).toEqual([c, role, true])
    expect(jobActionDenied(c, 'orchestrator')).toBeNull()
  }
  for (const c of ['$OKE job tree j', '$OKE job deliver j', '$OKE job output j result', 'echo job phase', '$OKE check j']) {
    expect(jobActionDenied(c, 'pm')).toBeNull()
  }
})

test('a PM, worker or reviewer write outside its areas is refused by oke path-check', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  writeAreas = [{ path: 'C:/u', direct: true }, { path: 'C:/repo', direct: false }]
  tickFolder = 'C:/oke/jobs/j/w1'
  const runs: Run[] = []
  engine(on, runs, [])
  on('tool.call', () => ({ result: { success: true } }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const asks = () => runs.filter(r => r.argv.includes('path-check')).length
  const bad: any = await $.tool.call({ tool: 'Write', file_path: 'C:/u/nested/x.py', content: 'x' } as any)
  expect(bad.deny).toMatch(/^OKe: .*not this job's/)
  const away: any = await $.tool.call({ tool: 'Write', file_path: 'D:/elsewhere/x.py', content: 'x' } as any)
  expect(away.deny).toMatch(/^OKe: /)
  const before = asks()
  const home: any = await $.tool.call({ tool: 'Write', file_path: 'C:/oke/jobs/j/w1/report.md', content: 'x' } as any)
  const git: any = await $.tool.call({ tool: 'Edit', file_path: 'C:/repo/a.py', old_string: 'a', new_string: 'b' } as any)
  expect([home.deny, git.deny]).toEqual([undefined, undefined])
  expect(asks()).toBe(before)  // plainly allowed: no python
  const shared: any = await $.tool.call({ tool: 'Write', file_path: 'C:/Users/u/.claude/projects/C--repo/memory/m.md', content: 'x' } as any)
  expect(shared.deny).toBeUndefined()  // the repository's memory, shared by its worktrees
  expect(asks()).toBe(before)
  writeAreas = []
  tickFolder = '/oke/jobs/j/w1'
})

test('the write guard fails closed: a failed path-check refuses, stale PM areas ask', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  tickRole = 'pm'
  writeAreas = [{ path: 'C:/repo', direct: false }]
  const runs: Run[] = []
  engine(on, runs, [])
  on('tool.call', () => ({ result: { success: true } }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  try {
  failCheck = true
  const out: any = await $.tool.call({ tool: 'Write', file_path: 'D:/x/a.py', content: 'x' } as any)
  expect(out.deny).toMatch(/^OKe: OKe could not check this write \(.*\): try again; if it keeps failing, report a question/)
  const ok: any = await $.tool.call({ tool: 'Write', file_path: 'C:/repo/a.py', content: 'x' } as any)
  expect(ok.deny).toBeUndefined()  // fresh areas: no python needed
  failTick = true
  await clock.advance(31_000)  // the ticks fail: the areas grow stale
  const stale: any = await $.tool.call({ tool: 'Write', file_path: 'C:/repo/a.py', content: 'x' } as any)
  expect(stale.deny).toMatch(/could not check this write/)
  } finally {
    failTick = false
    failCheck = false
    writeAreas = []
    tickRole = 'worker'
  }
})

test('worker areas from its last good tick do not expire', async ($, on) => {
  const clock = mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w1:p5' })
  writeAreas = [{ path: 'C:/repo', direct: false }]
  const runs: Run[] = []
  engine(on, runs, [])
  on('tool.call', () => ({ result: { success: true } }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  try {
  failTick = true
  failCheck = true
  await clock.advance(10 * 60_000)  // no new prompt, no tick for a long time
  const before = runs.filter(r => r.argv.includes('path-check')).length
  const ok: any = await $.tool.call({ tool: 'Write', file_path: 'C:/repo/a.py', content: 'x' } as any)
  expect(ok.deny).toBeUndefined()
  expect(runs.filter(r => r.argv.includes('path-check')).length).toBe(before)  // no python
  } finally {
    failTick = false
    failCheck = false
    writeAreas = []
  }
})

test('the board names each PM and each job its path', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w7:p1' })
  tickRole = 'orchestrator'
  tickBoard = [{ name: 'backend', key: 'backend', pm: null, jobs: [
    { job: 'fix-a', phase: 'planning', deliver: 'none', pending: null, path: 'c:/work/api', agents: [] }] }]
  engine(on, [], [])
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /▍backend/ })).toBeTruthy()
  await ui.press({ key: 'open-job:fix-a' })
  expect(await ui.find({ type: 'Text', text: /^c:\/work\/api$/ })).toBeTruthy()  // its own line: a long path is not cut off
  await ui.unmount()
  tickBoard = BOARD
  tickRole = 'worker'
})

test('the plan approval question shows the job brief as its first option preview', () => {
  const briefs = [{ job: 'k-to-c', path: 'c:/work/conv', brief: 'job: k-to-c\npath: c:/work/conv' }]
  const ask = [{ question: 'C:\\Work\\conv 에서 작업합니다. 승인할까요?', header: 'Plan',
    options: [{ label: '승인', preview: 'short' }, { label: '고쳐' }] }]
  const out = withBriefs(ask, briefs)!
  expect(out[0].options[0].preview).toBe(briefs[0].brief)
  expect(out[0].options[1].preview).toBeUndefined()
  expect(withBriefs([{ question: 'PM 이름?', header: 'PM', options: [{ label: 'a' }] }], briefs)).toBeNull()
  expect(withBriefs([{ ...ask[0], multiSelect: true }], briefs)).toBeNull()
  // a name only whole, a path only whole
  const q = (question: string) => [{ question, header: 'x', options: [{ label: 'a' }] }]
  expect(withBriefs(q('add k-to-cx now'), briefs)).toBeNull()
  expect(withBriefs(q('c:/work/conv2 에서'), briefs)).toBeNull()
  expect(withBriefs(q('job k-to-c: 승인?'), briefs)).not.toBeNull()
  // another job named, or two awaiting jobs on that path: the path alone names none
  expect(withBriefs(q('c:/work/conv 의 fix-b 를 merge 할까요?'), briefs, ['k-to-c', 'fix-b'])).toBeNull()
  const two = [...briefs, { job: 'docs', path: 'c:/work/conv', brief: 'docs brief' }]
  expect(withBriefs(q('c:/work/conv 에서 작업합니다'), two)).toBeNull()
  expect(withBriefs(q('c:/work/conv 의 docs 계획'), two)![0].options[0].preview).toBe('docs brief')
  // a job named like its repo folder is not named by the path; a sentence's period ends a name
  const conv = [{ job: 'conv', path: 'c:/work/conv', brief: 'conv brief' }]
  expect(withBriefs(q('c:/work/conv 의 fix-b 를 merge 할까요?'), conv, ['conv', 'fix-b'])).toBeNull()
  expect(withBriefs(q('worktree oke/k-to-c.'), briefs)).not.toBeNull()
  expect(withBriefs(q('c:/work/conv 에서 작업합니다.'), briefs, ['k-to-c'])).not.toBeNull()
})

test('python3 before oke.py becomes python where python3 does not run', () => {
  const cmd = 'python3 "C:/p/oke/bin/oke.py" status --claim && python3 -m pytest -q'
  expect(withPython(cmd, 'python')).toBe('python "C:/p/oke/bin/oke.py" status --claim && python3 -m pytest -q')
  expect(withPython(cmd, 'python3')).toBe(cmd)
  expect(withPython(cmd, null)).toBe(cmd)
  expect(withPython('cd r; python3 /p/bin/oke.py job phase j done', 'python')).toBe('cd r; python /p/bin/oke.py job phase j done')
  expect(withPython('echo mypython3 /p/oke.py', 'python')).toBe('echo mypython3 /p/oke.py')
})

test('bashWrites finds the files a Bash command writes in its common forms', () => {
  const repo = 'C:/u/repo'
  expect(bashWrites(`sed -i 's/a/b/' ${repo}/calc.py`)).toEqual([`${repo}/calc.py`])
  expect(bashWrites(`cd "${repo}" && sed -i.bak 's/a/b/' calc.py`)).toEqual([`${repo}/calc.py`])
  expect(bashWrites(`echo x > ${repo}/a.txt; echo y >>"${repo}/b c.txt"`)).toEqual([`${repo}/a.txt`, `${repo}/b c.txt`])
  expect(bashWrites(`cat <<'EOF' > ${repo}/n.md\nline > not/a/file\nEOF`)).toEqual([`${repo}/n.md`])
  expect(bashWrites(`git diff | tee -a ${repo}/d.txt`)).toEqual([`${repo}/d.txt`])
  expect(bashWrites(`Set-Content -Path ${repo}\\x.py -Value 1`)).toEqual([`${repo}/x.py`])
  expect(bashWrites(`cp a.py ${repo}/a.py`)).toEqual([`${repo}/a.py`])
  // not writes: a quoted >, stream redirects, temp files, a relative path with no cd (the agent's cwd)
  expect(bashWrites(`grep ">" ${repo}/a.py 2>&1 >/dev/null`)).toEqual([])
  expect(bashWrites(`python x.py > /tmp/out.txt 2>$null; echo > C:/Users/u/AppData/Local/Temp/t`)).toEqual([])
  expect(bashWrites('sed -i s/a/b/ calc.py')).toEqual([])
})

test('rmDenied refuses an orchestrator deleting a folder tree', () => {
  for (const bad of ['rm -rf /h/jobs/x', 'cd /h && rm -r jobs/x', 'Remove-Item -Recurse -Force C:\\h\\jobs\\x',
    'rmdir /s /q jobs', 'python -c "import shutil; shutil.rmtree(\'/h/jobs\')"']) {
    expect(rmDenied(bad)).toMatch(/never delete folders/)
  }
  expect(rmDenied('rm /h/tmp/a.txt')).toBeNull()
  expect(rmDenied('grep -n "rm -rf" notes.md')).toBeNull()
})

test('answered is a popup the user answered', () => {
  expect(answered({ result: { questions: [], answers: { 'Approve?': 'Approve' } } })).toBe(true)
  expect(answered({ result: { questions: [], answers: {} } })).toBe(false)
  expect(answered({ deny: 'no' })).toBe(false)
  expect(answered({ result: { answers: { q: 'x' } }, isError: true })).toBe(false)
})
