// OKe mod: a thin shell around bin/oke.py. It ticks the watcher every 10 s
// (orchestrator and PM only), resubmits a turn that ended on an API error after
// the delay oke.py decides, and draws the /oke-board pane. All decisions live in
// oke.py, where they are tested against a fake herdr.
import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { BoardJob, BoardNode, BoardRepo, Since, Todo } from '../types'

const PANE = 'oke-board'
const TICK_MS = 10_000
const UNKNOWN_EVERY = 6 // ticks: a session that is not (yet) an OKe agent is checked every 60 s
const board = atom({ plugin: 'oke', key: 'board' } as const, [] as BoardRepo[], { shape: 'board-2' })
const expanded = atom({ plugin: 'oke', key: 'expanded' } as const, [] as string[], { shape: 'expanded-1' })
const since = atom({ plugin: 'oke', key: 'since' } as const, {} as Record<string, Since>, { shape: 'since-1' })
const todos = atom({ plugin: 'oke', key: 'todos' } as const, [] as Todo[], { shape: 'todos-1' })

// Module state starts over on a reload; session.start runs again then.
let pane: string | undefined
let role: string | null = null
let folder: string | null = null
let bandId: string | null = null
let mood: Mood = 'rest'
let frame = 0
let python: string | null = null
let eventsOffset: number | null = null
let ticking = false
let attempt = 0
let lastError = ''
let pending: { cancel: () => void } | null = null

/** Run `oke.py <args>` and parse its JSON line; null when it failed. */
async function oke($: any, args: string[]): Promise<any> {
  const script = `${$.plugin.root}/bin/oke.py`.replace(/\\/g, '/')
  for (const py of python ? [python] : ['python3', 'python']) {
    try {
      const r = await $.process.run([py, script, ...args], { timeoutMs: 300_000 })
      // exit 9009 or similar: a launcher stub, not Python
      if (r.exitCode > 2) continue
      python = py
      if (r.exitCode !== 0) return null
      return JSON.parse(r.stdout.trim().split('\n').pop() || 'null')
    } catch {
      // not on PATH: try the next name. Once a name worked, a failure is a timeout:
      // running the same call again with another name would only repeat it.
      if (python) return null
    }
  }
  return null
}

async function tick($: any) {
  if (!pane || ticking) return
  ticking = true
  try {
    const args = ['tick', '--pane', pane, '--board']
    if (eventsOffset !== null) args.push('--events-from', String(eventsOffset))
    const out = await oke($, args)
    if (!out) return
    role = out.role
    folder = out.folder ?? null
    eventsOffset = out.events_offset
    if (role === 'orchestrator') {
      for (const line of out.events as string[]) $.ui.toast(`OKe: ${line.replace(/^\S+ /, '')}`, { timeoutMs: 8000 })
    }
    if (out.board) {
      const now = Date.now()
      await update($, since, old => {
        const next: Record<string, Since> = {}
        for (const n of nodes(out.board)) {
          const prev = old[n.agent]
          next[n.agent] = prev && prev.status === n.status ? prev : { status: n.status, at: now }
        }
        return next
      })
      await update($, board, () => out.board)
    }
  } finally {
    ticking = false
  }
}

async function submitRetry($: any, kind: string) {
  pending = null
  $.ui.status(undefined)
  await $.prompt.submit({
    text: `Your previous turn ended with an API error (${kind || 'unknown'}). Continue where you left off.`,
  })
}

async function clearRetry($: any) {
  pending?.cancel()
  pending = null
  if (attempt > 0) $.ui.status(undefined)
  if (!pane) return
  // Keep the count until the marker is really gone, so the next good turn clears again.
  if ((await oke($, ['api-retry', '--pane', pane, '--clear'])) !== null) attempt = 0
}

/** herdr agent status: mark, word, color. */
const STATUS: Record<string, [string, string, string | undefined]> = {
  working: ['●', '작업 중', 'green'],
  idle: ['○', '대기', undefined],
  blocked: ['!', '막힘', 'yellow'],
  dead: ['✗', '죽음', 'red'],
}

/** `oke pending` kinds, as the person reads them. */
const PENDING: Record<string, string> = {
  plan: '계획 승인',
  merge: 'merge 승인',
  question: 'PM 질문',
  blocked: 'PM 막힘',
  failed: 'PM 실패 보고',
  silent: 'PM 응답 없음',
  report_done: 'PM 보고 확인',
  unsent: '지시 안 보냄',
  legacy: '확인 필요',
}

/** The ledger's merge state beside the job name. */
const LEDGER: Record<string, [string, string]> = {
  unverified: ['검증 대기', 'yellow'],
  verified: ['검증 ✓', 'green'],
  rolled_back: ['rollback ✗', 'red'],
}

const STEPS = ['계획', '승인', '작업', '리뷰', '병합', '검증']
const PHASE_STEP: Record<string, number> = {
  intake: 0, planning: 0, awaiting_approval: 1, working: 2, reviewing: 3, awaiting_merge: 4, merging: 4,
}

/** The step a job is on: past the merge the ledger says, verified is past the last. */
function step(job: BoardJob): number {
  const at = PHASE_STEP[job.phase] ?? 0
  if (at < 4) return at
  return job.merge === 'verified' ? STEPS.length : job.merge === 'unverified' ? 5 : 4
}

function nodes(tree: BoardRepo[]): BoardNode[] {
  const out: BoardNode[] = []
  for (const repo of tree) {
    if (repo.pm) out.push(repo.pm)
    for (const job of repo.jobs) out.push(...job.agents)
  }
  return out
}

function elapsed(ms: number): string {
  const m = Math.floor(ms / 60_000)
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h${m % 60}m`
}

/** How a todo shows on the board: mark and color by status. */
const TODO: Record<string, [string, string | undefined]> = {
  completed: ['✓', 'green'],
  in_progress: ['▸', 'yellow'],
  pending: ['○', undefined],
}

const TASK_TOOLS = ['TaskCreate', 'TaskUpdate', 'TodoWrite']

/** Apply one task tool call to the list, as the tool itself did. */
function applyTask(list: Todo[], tool: string, input: any, result: any): Todo[] {
  if (tool === 'TodoWrite') {
    const next = result?.newTodos ?? input.todos ?? []
    return next.map((t: any) => ({ content: t.content, status: t.status }))
  }
  if (tool === 'TaskCreate') {
    const id = result?.task?.id
    return id ? [...list, { id: String(id), content: input.subject, status: 'pending' }] : list
  }
  // TaskUpdate
  if (result?.success === false) return list
  if (input.status === 'deleted') return list.filter(t => t.id !== input.taskId)
  return list.map(t => (t.id === input.taskId
    ? { ...t, content: input.subject ?? t.content, status: input.status ?? t.status }
    : t))
}

// ---------------------------------------------------------------- conductor band
// A 10x8 pixel conductor drawn as a 10x4 Raster: each cell's upper half block takes
// the top pixel as its foreground and the bottom pixel as its background.
type Mood = 'conduct' | 'rest' | 'wave' | 'stop'
const W = 10
const DEFAULT = 0x01000000
const PALETTE: Record<string, number> = {
  h: 0x3b2a20, s: 0xf2c9a0, k: 0x23233a, w: 0xffffff, b: 0xf5f5f5, n: 0xffd24a, z: 0x8a8fa8, r: 0xe04848, '!': 0xffb020,
}
const BODY = ['..kwwk....', '..kkkk....', '..k..k....']
const FRAMES: Record<Mood, string[][]> = {
  conduct: [
    ['...hh....n', '..hssh..b.', '...ss..b..', '..kwwkkw..', '.skwwk....', ...BODY],
    ['...hh.....', '..hssh....', '...ss....n', '..kwwk....', '.skwwkkw..', '..kwwk..b.', '..kkkk...b', '..k..k....'],
    ['.n.hh.....', '..hssh..b.', '...ss..b..', '..kwwkkw..', '.skwwk....', ...BODY],
    ['...hh.....', '..hssh...n', '...ss.....', '..kwwk....', '.skwwkkw..', '..kwwk..b.', '..kkkk...b', '..k..k....'],
  ],
  rest: [
    ['...hh.....', '..hssh..z.', '...ss.....', '..kwwk....', '.skwwks...', ...BODY],
    ['...hh...z.', '..hssh....', '...ss.....', '..kwwk....', '.skwwks...', ...BODY],
  ],
  wave: [
    ['...hh..s.!', '..hssh.k..', '...ss..k..', '..kwwkk...', '.skwwk....', ...BODY],
    ['...hh...s!', '..hssh..k.', '...ss..k..', '..kwwkk...', '.skwwk....', ...BODY],
  ],
  stop: [['...hh.....', '..hssh....', '...ss.....', '..rwwr....', '.srwwrs...', '..rwwr....', '..rrrr....', '..r..r....']],
}

function cells(pixels: string[]): string {
  const words = new Uint32Array(W * 4 * 3)
  for (let row = 0; row < 4; row++) {
    for (let col = 0; col < W; col++) {
      const top = PALETTE[pixels[row * 2]?.[col] ?? '.']
      const bottom = PALETTE[pixels[row * 2 + 1]?.[col] ?? '.']
      const at = (row * W + col) * 3
      if (top === undefined && bottom === undefined) words.set([0x20, DEFAULT, DEFAULT], at)
      else if (top === undefined) words.set([0x2584, bottom ?? DEFAULT, DEFAULT], at) // ▄
      else words.set([0x2580, top, bottom ?? DEFAULT], at) // ▀
    }
  }
  return (new Uint8Array(words.buffer) as any).toBase64()
}

function frameCells(m: Mood, i: number): string {
  const list = FRAMES[m]
  return cells(list[Math.floor(i) % list.length] ?? [])
}

/** The band's mood from the board, or for a worker or reviewer from its own turn. */
function moodOf(tree: BoardRepo[], working: boolean): Mood {
  const jobs = tree.flatMap(r => r.jobs)
  if (jobs.some(j => j.pending)) return 'wave'
  const all = nodes(tree)
  const open = jobs.filter(j => j.phase !== 'done' && j.phase !== 'failed')
  if (open.some(j => j.agents.some(n => n.status === 'dead')) || (tree.some(r => r.pm?.status === 'dead') && open.length)) {
    return 'stop'
  }
  return working || all.some(n => n.status === 'working') ? 'conduct' : 'rest'
}

async function openBoard($: any) {
  return $.ui.open({ id: PANE, title: 'OKe board' })
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const r = await next(e)
    pane = await $.env.get('HERDR_PANE_ID')
    if (!pane) return r
    await $.command.register({ name: 'oke-board', description: 'Show the OKe agents and their boards' })
    // A process that died during a backoff leaves its marker; this one starts clean.
    await clearRetry($)
    await tick($)
    if (role === 'orchestrator') {
      const opened = await openBoard($)
      if (!opened.isPlaced) $.ui.status('OKe: /oke-board opens the board')
    }
    // Workers and reviewers watch no one. An unknown pane may become a PM once spawn
    // records it, or the orchestrator after /oke:start: look at it only every minute,
    // so the many non-OKe sessions inside herdr do not run python every 10 s.
    // The first minute ticks every time: spawn records a new PM a moment after it starts.
    let skipped = 0
    let early = UNKNOWN_EVERY
    $.clock.every(TICK_MS, () => {
      if (role === 'worker' || role === 'reviewer') return
      if (role === null && --early < 0 && ++skipped < UNKNOWN_EVERY) return
      skipped = 0
      void tick($)
    })
    // The conductor moves while there is something to show; rest breathes at half speed.
    $.clock.every(400, () => {
      if (!bandId || mood === 'stop') return
      frame += 1
      if (mood === 'rest' && frame % 2) return
      void $.ui.blit({ requestId: bandId, key: 'conductor', cells: frameCells(mood, mood === 'rest' ? frame / 2 : frame) })
    })
    return r
  })

  // Each agent's own task list goes to its folder, where the board reads it.
  on('tool.call', async ($, e: any, next) => {
      const ran: any = await next(e)
      const tool: string = e.tool
      if (!pane || !TASK_TOOLS.includes(tool) || ran.deny !== undefined || ran.isError) return ran
      const list = await update($, todos, old => applyTask(old, tool, e, ran.result))
      if (folder) {
        try {
          await $.fs.write(`${folder}/todos.json`.replace(/\\/g, '/'), JSON.stringify(list))
        } catch {
          // the board just shows the older list
        }
      }
      return ran
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!role || e.props.hasSurvey) return next(e)
    const { Box, Text, Raster } = $.ui.resolve(e) as any
    const tree = await read($, board)
    mood = moodOf(tree, e.props.isWorking)
    bandId = e.requestId
    const jobs = tree.flatMap(r => r.jobs)
    const waiting = jobs.filter(j => j.pending).length
    const busy = nodes(tree).filter(n => n.status === 'working').length
    const open = jobs.filter(j => j.phase !== 'done' && j.phase !== 'failed').length
    const words: Record<Mood, string> = { conduct: '연주 중', rest: '쉬는 중', wave: '내 차례!', stop: '멈춤' }
    const line = role === 'worker' || role === 'reviewer'
      ? `OKe ${role}`
      : `OKe ${role} · job ${open} · 작업 ${busy}${waiting ? ` · 내 차례 ${waiting}` : ''}`
    return (
      <Box flexDirection="row">
        <Raster key="conductor" columns={W} rows={4} cells={frameCells(mood, frame)} />
        <Box flexDirection="column" marginLeft={1}>
          <Text bold color={mood === 'stop' ? 'red' : mood === 'wave' ? 'yellow' : undefined}>{`♪ ${words[mood]}`}</Text>
          <Text dimColor>{line}</Text>
        </Box>
      </Box>
    )
  })

  on('command.run', { command: 'oke-board' }, async $ => {
    await tick($)
    const opened = await openBoard($)
    if (opened.isPlaced) $.ui.status(undefined)
    return { text: role ? 'OKe board opened.' : 'This session is not an OKe orchestrator or PM.' }
  })

  on('classic.StopFailure', async ($, e: any, next) => {
    lastError = String(e.error ?? '')
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    // The person typed during a backoff: their turn replaces the retry.
    pending?.cancel()
    pending = null
    return next(e)
  })

  on('turn.complete', async ($, e: any, next) => {
    const r = await next(e)
    if (!pane) return r
    if (e.reason !== 'error') {
      if (attempt > 0 || e.reason === 'aborted') await clearRetry($)
      return r
    }
    attempt += 1
    const kind = lastError
    lastError = ''
    const out = await oke($, ['api-retry', '--pane', pane, '--attempt', String(attempt), '--error', kind])
    if (out?.action === 'retry') {
      $.ui.status(`OKe: API error (${kind || 'unknown'}), retry ${attempt}/${out.limit} in ${out.delay}s`)
      pending?.cancel()
      pending = $.clock.after(out.delay * 1000, () => void submitRetry($, kind))
    } else if (out?.action === 'give_up') {
      attempt = 0
      $.ui.status(`OKe: API error (${kind || 'unknown'}), automatic retry stopped`)
      if (role === 'orchestrator') void $.ui.notify(`OKe orchestrator stopped on an API error: ${kind || 'unknown'}`)
    } else {
      attempt = 0  // not an OKe session
    }
    return r
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const tree = await read($, board)
    const open = await read($, expanded)
    const times = await read($, since)
    // the orchestrator's own task list: the person watches this session most of the time
    const mine = role === 'orchestrator' ? await read($, todos) : []
    const now = Date.now()
    if (tree.length === 0 && mine.length === 0) return <Text dimColor>No OKe work yet.</Text>

    const todoRows = (list: Todo[], indent: string) => list.map(t => {
      const [tm, tc] = TODO[t.status] ?? ['○', undefined]
      return (
        <Text color={tc} dimColor={t.status === 'completed'} bold={t.status === 'in_progress'}>
          {`${indent}${tm} ${t.content}`}
        </Text>
      )
    })

    const toggle = (agent: string) =>
      void update($, expanded, list => (list.includes(agent) ? list.filter(a => a !== agent) : [...list, agent]))
    const focus = (agent: string) => void $.process.run(['herdr', 'agent', 'focus', agent], { timeoutMs: 10_000 })

    // One row per agent, the row itself toggles it open: a second row of buttons per
    // agent doubled the height, and buttons beside text squeezed it away (seen live).
    const agentRows = (n: BoardNode, indent: string, name: string) => {
      const t = times[n.agent]
      const [mark, word, color] = STATUS[n.status] ?? ['·', n.status, undefined]
      const extra = [n.report ? `보고 ${n.report}` : '', n.revives ? `revived ${n.revives}x` : ''].filter(Boolean)
      const isOpen = open.includes(n.agent)
      const rows = [
        <Button key={`open-${n.agent}`} plain onPress={() => toggle(n.agent)}>
          {indent}
          <Text color={color} dimColor={n.status === 'idle'}>{`${mark} `}</Text>
          <Text bold>{name}</Text>
          <Text color={color} dimColor={n.status === 'idle'}>{` ${word}`}</Text>
          <Text dimColor>{`${t ? ` ${elapsed(now - t.at)}` : ''}${extra.length ? ` · ${extra.join(', ')}` : ''}`}</Text>
        </Button>,
      ]
      rows.push(...todoRows(n.todos ?? [], `${indent}  `))
      if (isOpen) {
        rows.push(
          <Box flexDirection="row">
            <Text>{`${indent}  `}</Text>
            <Button key={`go-${n.agent}`} label="이동" onPress={() => focus(n.agent)} />
          </Box>,
        )
        for (const line of (n.board || '(no board.md)').split('\n')) {
          rows.push(<Text dimColor>{`${indent}  ${line}`}</Text>)
        }
      }
      return rows
    }

    const rows = []
    const waiting = tree.flatMap(repo => repo.jobs.filter(j => j.pending))
    if (waiting.length) {
      rows.push(<Text bold color="yellow">{`내 차례 ${waiting.length}`}</Text>)
      for (const j of waiting) {
        rows.push(<Text color="yellow">{`  ▸ ${j.job}: ${PENDING[j.pending as string] ?? j.pending}`}</Text>)
      }
      rows.push(<Text> </Text>)
    }
    if (mine.length) {
      const done = mine.filter(t => t.status === 'completed').length
      rows.push(<Text bold>{`오케 할 일 ${done}/${mine.length}`}</Text>)
      rows.push(...todoRows(mine, '  '))
      rows.push(<Text> </Text>)
    }
    for (const repo of tree) {
      rows.push(<Text bold>{repo.repo.replace(/-[0-9a-f]{6}$/, '')}</Text>)
      if (repo.pm) rows.push(...agentRows(repo.pm, '  ', 'PM'))
      for (const job of repo.jobs) {
        if (job.phase === 'done' || job.phase === 'failed') {
          rows.push(<Text dimColor color={job.phase === 'failed' ? 'red' : undefined}>
            {`  ${job.phase === 'done' ? '✓' : '✗'} ${job.job} ${job.phase === 'done' ? '끝' : '실패'}`}
          </Text>)
          continue
        }
        const [label, labelColor] = LEDGER[job.merge] ?? ['', undefined]
        rows.push(
          <Text>
            <Text bold>{`  ${job.job}`}</Text>
            {label ? <Text color={labelColor}>{` ${label}`}</Text> : ''}
          </Text>,
        )
        const at = step(job)
        rows.push(
          <Text>
            {'    '}
            {STEPS.map((s, i) => (
              <Text color={i < at ? 'green' : undefined} dimColor={i > at} bold={i === at} inverse={i === at}>
                {i ? ` ${s}` : s}
              </Text>
            ))}
          </Text>,
        )
        for (const n of job.agents) rows.push(...agentRows(n, '    ', n.agent.startsWith(`${job.job}-`) ? n.agent.slice(job.job.length + 1) : n.agent))
      }
    }
    return <Box flexDirection="column">{rows}</Box>
  })
}
