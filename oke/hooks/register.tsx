// OKe mod: a thin shell around bin/oke.py. It ticks the watcher every 10 s
// (orchestrator and PM only), resubmits a turn that ended on an API error after
// the delay oke.py decides, and draws the /oke-board pane. All decisions live in
// oke.py, where they are tested against a fake herdr.
import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { BoardNode, BoardRepo, Since } from '../types'

const PANE = 'oke-board'
const TICK_MS = 10_000
const UNKNOWN_EVERY = 6 // ticks: a session that is not (yet) an OKe agent is checked every 60 s
const board = atom({ plugin: 'oke', key: 'board' } as const, [] as BoardRepo[], { shape: 'board-1' })
const expanded = atom({ plugin: 'oke', key: 'expanded' } as const, [] as string[], { shape: 'expanded-1' })
const since = atom({ plugin: 'oke', key: 'since' } as const, {} as Record<string, Since>, { shape: 'since-1' })

// Module state starts over on a reload; session.start runs again then.
let pane: string | undefined
let role: string | null = null
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
    return r
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
    const now = Date.now()
    if (tree.length === 0) return <Text dimColor>No OKe work yet.</Text>

    const toggle = (agent: string) =>
      void update($, expanded, list => (list.includes(agent) ? list.filter(a => a !== agent) : [...list, agent]))
    const focus = (agent: string) => void $.process.run(['herdr', 'agent', 'focus', agent], { timeoutMs: 10_000 })

    const agentRows = (n: BoardNode, indent: string) => {
      const t = times[n.agent]
      const age = t ? ` ${elapsed(now - t.at)}` : ''
      const extra = [n.report ? `report ${n.report}` : '', n.revives ? `revived ${n.revives}x` : '']
        .filter(Boolean)
        .join(', ')
      // Text and buttons on separate lines: in a narrow pane one row squeezed the text away (seen live).
      const rows = [
        <Text color={n.status === 'dead' ? 'red' : n.status === 'blocked' ? 'yellow' : undefined}>
          {`${indent}${n.agent} ${n.role} ${n.status}${age}${extra ? ` (${extra})` : ''}`}
        </Text>,
        <Box flexDirection="row">
          <Text>{indent}</Text>
          <Button key={`open-${n.agent}`} label={open.includes(n.agent) ? '접기' : '펼치기'} onPress={() => toggle(n.agent)} />
          <Text> </Text>
          <Button key={`go-${n.agent}`} label="이동" onPress={() => focus(n.agent)} />
        </Box>,
      ]
      if (open.includes(n.agent)) {
        for (const line of (n.board || '(no board.md)').split('\n')) {
          rows.push(<Text dimColor>{`${indent}  ${line}`}</Text>)
        }
      }
      return rows
    }

    const rows = []
    for (const repo of tree) {
      rows.push(<Text bold>{repo.repo}</Text>)
      if (repo.pm) rows.push(...agentRows(repo.pm, '  '))
      for (const job of repo.jobs) {
        rows.push(<Text>{`    ${job.job} [${job.phase}]`}</Text>)
        for (const n of job.agents) rows.push(...agentRows(n, '      '))
      }
    }
    return <Box flexDirection="column">{rows}</Box>
  })
}
