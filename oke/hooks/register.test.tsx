import { expect, mock, test } from 'claude-code/testing'

type Run = { argv: readonly string[] }

const BOARD = [{
  repo: 'toy-6b0484', key: 'k', pm: null,
  jobs: [{ job: 'calc-sub', phase: 'working', agents: [{
    agent: 'calc-sub-w1', role: 'worker', status: 'working', pane: 'w7:p9', report: null,
    board: '## 지금\nwriting sub', revives: 1 }] }],
}]

let tickRole = 'worker'

function answer(args: readonly string[]) {
  if (args[0] === 'tick') {
    return { role: tickRole, events: [], events_offset: 0, board: tickRole === 'worker' ? null : BOARD, revived: {} }
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
  await clock.advance(50_000)
  expect(ticks()).toBe(first)
  await clock.advance(10_000)
  expect(ticks()).toBe(first + 1)
  tickRole = 'pm'  // spawn recorded it: from the next look on it is a PM
  await clock.advance(60_000)
  await clock.advance(10_000)
  expect(ticks()).toBe(first + 3)
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
    expect((await ui.find({ type: 'Text', text: /calc-sub-w1 worker working/ }))?.text).toContain('revived 1x')
    expect(await ui.find({ type: 'Text', text: /writing sub/ })).toBeFalsy()
    await ui.press({ key: 'open-calc-sub-w1' })
    expect(await ui.find({ type: 'Text', text: /writing sub/ })).toBeTruthy()
    await ui.press({ key: 'open-calc-sub-w1' })
    await ui.unmount()
  }
  tickRole = 'worker'
})
