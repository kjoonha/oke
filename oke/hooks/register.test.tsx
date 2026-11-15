import { expect, mock, test } from 'claude-code/testing'

type Run = { argv: readonly string[] }

const BOARD = [{
  repo: 'toy-6b0484', key: 'k', pm: null,
  jobs: [{ job: 'calc-sub', phase: 'working', merge: 'rolled_back', pending: 'question', agents: [{
    agent: 'calc-sub-w1', role: 'worker', status: 'working', pane: 'w7:p9', report: null,
    board: '## 지금\nwriting sub', revives: 1, todos: [
      { content: 'write sub', status: 'completed' }, { content: 'test sub', status: 'in_progress' },
      { content: 'docs', status: 'pending' }] }] },
  { job: 'calc-add', phase: 'done', merge: 'verified', pending: null, agents: [{
    agent: 'calc-add-w1', role: 'worker', status: 'idle', pane: 'w7:p8', report: 'done',
    board: '', revives: 0, todos: [] }] }],
}]

let tickRole = 'worker'

function answer(args: readonly string[]) {
  if (args[0] === 'tick') {
    return { role: tickRole, folder: '/oke/jobs/j/w1', events: [], events_offset: 0, board: tickRole === 'worker' ? null : BOARD, revived: {} }
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
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  on('prompt.submit', (_: any, e: any) => {
    prompts.push(e.text)
    return { text: e.text }
  })
  on('turn.complete', () => ({ text: '' }))
  on('classic.StopFailure', () => ({}))
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

test('outside herdr the mod does nothing', async ($, on) => {
  mock.clock(on)
  mock.env(on, {})
  const runs: Run[] = []
  const prompts: string[] = []
  engine(on, runs, prompts)

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.turn.complete({ ...TURN, reason: 'error' } as any)
  expect(runs.length).toBe(0)
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
    expect((await ui.find({ key: 'open-calc-sub-w1' }))?.text).toMatch(/● w1 작업 중 .*revived 1x/)
    expect(await ui.find({ type: 'Text', text: /writing sub/ })).toBeFalsy()
    expect(await ui.find({ key: 'go-calc-sub-w1' })).toBeFalsy()
    await ui.press({ key: 'open-calc-sub-w1' })
    expect(await ui.find({ type: 'Text', text: /writing sub/ })).toBeTruthy()
    expect(await ui.find({ key: 'go-calc-sub-w1' })).toBeTruthy()
    await ui.press({ key: 'open-calc-sub-w1' })
    await ui.unmount()
  }
  tickRole = 'worker'
})

test('the board puts what waits on the user first and folds finished jobs', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w7:p1' })
  tickRole = 'orchestrator'
  engine(on, [], [])

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /내 차례 1/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /calc-sub: PM 질문/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /^toy$/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /calc-sub rollback ✗/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /계획 승인 작업 리뷰 병합 검증/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /✓ calc-add 끝/ })).toBeTruthy()
  expect(await ui.find({ key: 'open-calc-add-w1' })).toBeFalsy()
  await ui.unmount()
  tickRole = 'worker'
})

test('the board marks done, current and open todos apart', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w7:p1' })
  tickRole = 'orchestrator'
  engine(on, [], [])

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /✓ write sub/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /▸ test sub/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /○ docs/ })).toBeTruthy()
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

test('the band shows the conductor in an OKe session', async ($, on) => {
  mock.clock(on)
  mock.env(on, { HERDR_PANE_ID: 'w7:p1' })
  tickRole = 'orchestrator'
  engine(on, [], [])

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({
    plugin: 'oke', component: 'AbovePrompt', requestId: 'band', surface: 'terminal',
    props: { hasSurvey: false, isCollapsed: false, isWorking: false, maxRows: 20, bodyColumns: 60,
      scroll: { offset: 0, bodyRows: 20 }, view: {} },
  } as any)
  expect(await ui.find({ key: 'conductor' })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /내 차례!/ })).toBeTruthy()
  expect(await ui.find({ type: 'Text', text: /OKe orchestrator · job 1 · 작업 1 · 내 차례 1/ })).toBeTruthy()
  await ui.unmount()
  tickRole = 'worker'
})
