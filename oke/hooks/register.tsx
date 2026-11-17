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
const answeredSeen = atom({ plugin: 'oke', key: 'answered' } as const, [] as string[], { shape: 'answered-1' })
const bandOpen = atom({ plugin: 'oke', key: 'bandOpen' } as const, false as boolean, { shape: 'band-open-1' })

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
let job: string | null = null
let seq: number | null = null

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
    job = out.job ?? null
    seq = out.seq ?? null
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

const COMPACT_NOTE = 'OKe task boundary: the report for this assignment is written. Keep the role, the job, '
  + 'the assignment path and seq, decisions made and why, open questions and the files touched. '
  + 'Drop tool output and exploration details.'

/** At a task boundary (a report for an assignment's current seq, new since the last
 * look) a PM, worker or reviewer compacts, unless the person turned auto-compact off. */
async function compactAtBoundary($: any) {
  if (role !== 'pm' && role !== 'worker' && role !== 'reviewer') return
  const out = await oke($, ['boundary', '--pane', pane as string])
  if (!out) return
  const seen = await read($, answeredSeen)
  const fresh = (out.answered as string[]).some(a => !seen.includes(a))
  await update($, answeredSeen, () => out.answered)
  if (!fresh) return
  try {
    const u = await $.session.usage({ breakdown: 'summary' })
    if (u.context?.breakdown?.isAutoCompactEnabled === false) return
  } catch {
    return
  }
  // after this turn has settled, not inside its hook
  $.clock.after(0, () => void $.session.compact({ instructions: COMPACT_NOTE }))
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
  working: ['●', 'working', 'green'],
  idle: ['○', 'idle', undefined],
  blocked: ['!', 'blocked', 'yellow'],
  dead: ['✗', 'dead', 'red'],
}

/** `oke pending` kinds, as the person reads them. */
const PENDING: Record<string, string> = {
  plan: 'approve the plan',
  merge: 'approve the merge',
  question: 'PM asks',
  blocked: 'PM is blocked',
  failed: 'PM reports a failure',
  silent: 'PM is silent',
  report_done: 'read the PM report',
  unsent: 'assignment not sent',
  legacy: 'needs a look',
}

/** The ledger's merge state beside the job name. */
const LEDGER: Record<string, [string, string]> = {
  unverified: ['unverified', 'yellow'],
  verified: ['verified ✓', 'green'],
  rolled_back: ['rollback ✗', 'red'],
}

const STEPS = ['plan', 'approve', 'work', 'review', 'merge', 'verify']
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

const TODO_ORDER = ['in_progress', 'pending', 'completed']

/** The `## 지금` section of a board.md: how the agent's current work goes, at most 3 lines. */
function nowLines(board: string): string[] {
  const lines = board.split(/\r?\n/)
  const at = lines.findIndex(l => /^##\s*지금/.test(l.trim()))
  if (at < 0) return []
  const end = lines.findIndex((l, i) => i > at && l.trim().startsWith('## '))
  return lines.slice(at + 1, end < 0 ? undefined : end).map(l => l.trim()).filter(Boolean).slice(0, 3)
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

// ---------------------------------------------------------------- status line
/** One piece of the status line; a higher `drop` goes first when the line is too wide. */
type Part = { text: string; drop: number }

/** Terminal cells a string takes: Hangul and CJK take two. */
function cellWidth(s: string): number {
  let n = 0
  for (const ch of s) {
    const c = ch.codePointAt(0) ?? 0
    n += (c >= 0x1100 && c <= 0x115f) || (c >= 0x3130 && c <= 0x318f) || (c >= 0x4e00 && c <= 0x9fff)
      || (c >= 0xac00 && c <= 0xd7a3) ? 2 : 1
  }
  return n
}

const SEP = ' · '

/** The parts in their order, the least needed dropped until the line fits `width`. */
function fit(parts: Part[], width: number | undefined): string {
  let kept = parts
  const len = (list: Part[]) => cellWidth(list.map(p => p.text).join(SEP))
  while (width && kept.length > 1 && len(kept) > width) {
    const worst = Math.max(...kept.map(p => p.drop))
    const at = kept.map(p => p.drop).lastIndexOf(worst)
    kept = kept.filter((_, i) => i !== at)
  }
  return kept.map(p => p.text).join(SEP)
}

const usd = (n: number) => `$${n.toFixed(2)}`

type Self = { model: string; percent?: number; cost?: number }

/** The status line: for orchestrator and PM the work they watch, for a child its own job. */
function statusParts(role: string, tree: BoardRepo[], self: Self): Part[] {
  const own = [
    self.percent !== undefined ? { text: `ctx ${self.percent}%`, drop: 2 } : null,
    self.cost !== undefined ? { text: usd(self.cost), drop: 4 } : null,
  ]
  if (role === 'worker' || role === 'reviewer') {
    return [
      { text: `OKe ${role}`, drop: 0 },
      job ? { text: `${job}${seq !== null ? ` #${seq}` : ''}`, drop: 1 } : null,
      { text: self.model.replace(/^claude-/, ''), drop: 5 },
      ...own,
    ].filter((p): p is Part => p !== null)
  }
  const waiting = tree.flatMap(r => r.jobs).filter(j => j.pending).length
  return [
    { text: `OKe ${role}`, drop: 0 },
    waiting ? { text: `waiting on you ${waiting}`, drop: 1 } : null,
    ...own,
  ].filter((p): p is Part => p !== null)
}

const isOpen = (j: BoardJob) => j.phase !== 'done' && j.phase !== 'failed'
/** More open jobs than this fold under one row the person clicks open. */
const FOLD_AT = 3

/** The org chart under the status line: a repo with its PM, then one row per open job
 * with its step, its cost and its agents. A PM sees its own repo, so no repo row. */
function orgRows(role: string, tree: BoardRepo[]): { repo?: BoardRepo; job?: BoardJob; last: boolean }[] {
  const rows: { repo?: BoardRepo; job?: BoardJob; last: boolean }[] = []
  for (const repo of tree) {
    const jobs = repo.jobs.filter(isOpen)
    if (role === 'orchestrator' && (repo.pm || jobs.length)) rows.push({ repo, last: false })
    jobs.forEach((job, i) => rows.push({ job, last: i === jobs.length - 1 }))
  }
  return rows
}

async function selfUsage($: any): Promise<Self> {
  try {
    const u = await $.session.usage()
    return { model: await $.session.model(), percent: u.context?.percent, cost: u.cost?.usd }
  } catch {
    return { model: '' }
  }
}

// ---------------------------------------------------------------- mascot band
// A 12x8 pixel blob with headphones and a neon visor, drawn as a 12x4 Raster: each
// cell's upper half block takes the top pixel as its foreground, the bottom as background.
type Mood = 'conduct' | 'rest' | 'wave' | 'stop'
const W = 12
const DEFAULT = 0x01000000
const PALETTE: Record<string, number> = {
  p: 0xff7ad9, c: 0xb9a6ff, v: 0x2de2e6, w: 0xffffff, m: 0xff5c8a, d: 0x2b2140,
  n: 0xffe66d, y: 0x6ff7ff, h: 0xff4d8d, z: 0x8a8fa8, r: 0xff5c5c,
}
const BLOB = [
  '..pppppp....',
  '.p.cccc.p...',
  '.pccccccp...',
  '.pvwvvwvp...',
  '..cccccc....',
  '..ccmmcc....',
  '..cccccc....',
  '..c....c....',
]
/** `frame` with pixels set at [row, col, color]. */
function put(frame: string[], dots: [number, number, string][]): string[] {
  const rows = frame.map(r => r.split(''))
  for (const [r, c, ch] of dots) rows[r]![c] = ch
  return rows.map(r => r.join(''))
}
/** The blob bobbed one pixel down, its feet squashed. */
const BOB = ['............', ...BLOB.slice(0, 7)]
const SLEEPY = put(BLOB, [[3, 2, 'd'], [3, 3, 'd'], [3, 4, 'c'], [3, 5, 'c'], [3, 6, 'd'], [3, 7, 'd']])
const FRAMES: Record<Mood, string[][]> = {
  conduct: [
    put(BLOB, [[0, 10, 'n'], [2, 11, 'y'], [5, 9, 'w']]),
    put(BOB, [[1, 11, 'y'], [3, 10, 'n'], [0, 9, 'w']]),
    put(BLOB, [[1, 9, 'y'], [4, 11, 'n'], [0, 11, 'w']]),
    put(BOB, [[0, 10, 'n'], [2, 9, 'w'], [5, 11, 'y']]),
  ],
  rest: [
    put(SLEEPY, [[2, 10, 'z']]),
    put(SLEEPY, [[1, 10, 'z'], [0, 11, 'z']]),
  ],
  wave: [
    put(BLOB, [[4, 8, 'c'], [3, 9, 'c'], [2, 10, 'c'], [0, 10, 'h'], [0, 11, 'h']]),
    put(BLOB, [[4, 8, 'c'], [4, 9, 'c'], [3, 10, 'c'], [1, 11, 'h']]),
  ],
  stop: [put(BLOB.map(r => r.replace(/c/g, 'r')), [[3, 3, 'd'], [3, 6, 'd']])],
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
    // herdr pane, else this Claude session (OKe without herdr runs children as --bg sessions)
    // the process env has no session id (seen live): ask the session itself
    pane = (await $.env.get('HERDR_PANE_ID')) || `session:${await $.session.id()}`
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
    const { Box, Text, Raster, Button } = $.ui.resolve(e) as any
    const tree = await read($, board)
    const unfolded = await read($, bandOpen)
    mood = moodOf(tree, e.props.isWorking)
    bandId = e.requestId
    const words: Record<Mood, [string, string | undefined]> = {
      conduct: ['♫ vibing', 'magenta'], rest: ['☾ chilling', 'cyan'], wave: ['✦ your turn!', 'yellow'], stop: ['✗ stuck', 'red'],
    }
    const width = e.props.bodyColumns - W - 1
    const line = fit(statusParts(role, tree, await selfUsage($)), width)

    const agentDots = (job: BoardJob) => job.agents.map(n => {
      const [mark, , color] = STATUS[n.status] ?? ['·', n.status, undefined]
      const name = n.agent.startsWith(`${job.job}-`) ? n.agent.slice(job.job.length + 1) : n.agent
      return <Text color={color} dimColor={n.status === 'idle'}>{` ${name}${mark}`}</Text>
    })
    const org = role === 'worker' || role === 'reviewer' ? [] : orgRows(role, tree)
    const jobCount = org.filter(r => r.job).length
    const folded = jobCount > FOLD_AT && !unfolded
    // folded, the repos and their PMs stay: who is alive is still on screen
    const chart = org.filter(r => !folded || r.repo).map(r => {
      if (r.repo) {
        const pm = r.repo.pm
        const [mark, , color] = pm ? STATUS[pm.status] ?? ['·', pm.status, undefined] : ['', '', undefined]
        return (
          <Text wrap="truncate-end">
            <Text bold>{r.repo.repo.replace(/-[0-9a-f]{6}$/, '')}</Text>
            {pm ? <Text color={color} dimColor={pm.status === 'idle'}>{` PM${mark}`}</Text> : ''}
          </Text>
        )
      }
      const job = r.job as BoardJob
      const cost = job.agents.reduce((sum, n) => sum + (n.cost ?? 0), 0)
      return (
        <Text wrap="truncate-end">
          <Text dimColor>{role === 'orchestrator' ? (r.last ? '└ ' : '├ ') : '• '}</Text>
          {job.job}
          <Text color={job.pending ? 'yellow' : 'cyan'}>{` ${job.pending ? 'waiting' : STEPS[step(job)] ?? 'done'}`}</Text>
          {cost ? <Text dimColor>{` ${usd(cost)}`}</Text> : ''}
          {agentDots(job)}
        </Text>
      )
    })
    const working = org.filter(r => r.job?.agents.some(n => n.status === 'working')).length
    const toggle = jobCount > FOLD_AT
      ? [<Button key="jobs" plain onPress={() => void update($, bandOpen, v => !v)}>
          <Text dimColor>{`${unfolded ? '▾' : '▸'} ${jobCount} jobs · ${working} working`}</Text>
        </Button>]
      : []
    const [word, color] = words[mood]
    return (
      <Box flexDirection="row">
        <Raster key="conductor" columns={W} rows={4} cells={frameCells(mood, frame)} />
        <Box flexDirection="column" marginLeft={1}>
          <Text bold color={color}>{word}</Text>
          <Text dimColor>{line}</Text>
          {chart}
          {toggle}
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
    // a new assignment's seq for the status line; workers and reviewers do not tick otherwise
    if (role === 'worker' || role === 'reviewer') void tick($)
    return next(e)
  })

  on('turn.complete', async ($, e: any, next) => {
    const r = await next(e)
    if (!pane) return r
    if (folder) {
      // the job's cost on the PM's and the orchestrator's status line
      const self = await selfUsage($)
      if (self.cost !== undefined) {
        try {
          await $.fs.write(`${folder}/usage.json`.replace(/\\/g, '/'), JSON.stringify({ usd: self.cost }))
        } catch {
          // the job's cost just lags
        }
      }
    }
    if (e.reason !== 'error') {
      if (attempt > 0 || e.reason === 'aborted') await clearRetry($)
      if (e.reason !== 'aborted') await compactAtBoundary($)
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

    // in progress first, each with how it goes (board.md's 지금), then planned, then done
    const todoRows = (list: Todo[], indent: string, now: string[] = []) => TODO_ORDER.flatMap(status =>
      list.filter(t => (TODO[t.status] ? t.status : 'pending') === status).flatMap(t => {
        const [tm, tc] = TODO[status]!
        return [
          <Text color={tc} dimColor={status === 'completed'} bold={status === 'in_progress'}>
            {`${indent}${tm} ${t.content}`}
          </Text>,
          ...(status === 'in_progress' ? now : []).map((line, i) => <Text color="cyan">{`${indent}    ${i ? '  ' : '└ '}${line}`}</Text>),
        ]
      }))

    const toggle = (agent: string) =>
      void update($, expanded, list => (list.includes(agent) ? list.filter(a => a !== agent) : [...list, agent]))
    // herdr focuses the pane; a background session can only be attached by the user
    const focus = async (agent: string) => {
      const out = await oke($, ['attach', agent])
      if (out?.hint) $.ui.status(`OKe: ${out.hint}`)
    }

    // One row per agent, the row itself toggles it open: a second row of buttons per
    // agent doubled the height, and buttons beside text squeezed it away (seen live).
    const agentRows = (n: BoardNode, indent: string, name: string) => {
      const t = times[n.agent]
      const [mark, word, color] = STATUS[n.status] ?? ['·', n.status, undefined]
      const extra = [n.report ? `report ${n.report}` : '', n.revives ? `revived ${n.revives}x` : ''].filter(Boolean)
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
      rows.push(...todoRows(n.todos ?? [], `${indent}  `, nowLines(n.board)))
      if (isOpen) {
        rows.push(
          <Box flexDirection="row">
            <Text>{`${indent}  `}</Text>
            <Button key={`go-${n.agent}`} label="go" onPress={() => void focus(n.agent)} />
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
      rows.push(<Text bold color="yellow">{`waiting on you ${waiting.length}`}</Text>)
      for (const j of waiting) {
        rows.push(<Text color="yellow">{`  ▸ ${j.job}: ${PENDING[j.pending as string] ?? j.pending}`}</Text>)
      }
      rows.push(<Text> </Text>)
    }
    if (mine.length) {
      const done = mine.filter(t => t.status === 'completed').length
      rows.push(<Text bold>{`OKe todo ${done}/${mine.length}`}</Text>)
      rows.push(...todoRows(mine, '  '))
      rows.push(<Text> </Text>)
    }
    for (const repo of tree) {
      rows.push(<Text bold>{repo.repo.replace(/-[0-9a-f]{6}$/, '')}</Text>)
      if (repo.pm) rows.push(...agentRows(repo.pm, '  ', 'PM'))
      for (const job of repo.jobs) {
        if (job.phase === 'done' || job.phase === 'failed') {
          rows.push(<Text dimColor color={job.phase === 'failed' ? 'red' : undefined}>
            {`  ${job.phase === 'done' ? '✓' : '✗'} ${job.job} ${job.phase === 'done' ? 'done' : 'failed'}`}
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
