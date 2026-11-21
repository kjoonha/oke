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
// the role survives a hot reload here: until the first tick, the guards below depend on it
const who = atom({ plugin: 'oke', key: 'who' } as const, null as { role: string, folder: string | null, named: boolean } | null, { shape: 'who-2' })

// Module state starts over on a reload; session.start runs again then.
let pane: string | undefined
let role: string | null = null
let named = false  // role and folder came from the first prompt, not yet from oke
let folder: string | null = null
let home: string | null = null
let claudeDir: string | null = null
let wiki: string | null = null
let theme: string | null = null  // config.json `theme`: the band's characters
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

/** Whether OKe runs on this machine at all: without its state.json no session is an
 * OKe agent, and the many plain sessions need not run python. */
async function okeRunning($: any): Promise<boolean> {
  const base = home ?? (await $.env.get('OKE_HOME'))
    ?? `${(await $.env.get('USERPROFILE')) || (await $.env.get('HOME'))}/.oke`
  try {
    // a Git Bash style /c/x home, as oke.py's native_path reads it
    return await $.fs.exists(`${base}/state.json`.replace(/\\/g, '/').replace(/^\/([a-zA-Z])\//, '$1:/'))
  } catch {
    return true  // unsure: look as before
  }
}

async function tick($: any) {
  if (!pane || ticking) return
  ticking = true
  try {
    if (role === null && !(await okeRunning($))) return
    const args = ['tick', '--pane', pane, '--board']
    if (eventsOffset !== null) args.push('--events-from', String(eventsOffset))
    const out = await oke($, args)
    if (!out) return
    // a role the first prompt named stays until spawn has recorded the agent
    if (out.role || !named) {
      role = out.role
      folder = out.folder ?? null
      named = false
      await update($, who, () => (role ? { role, folder, named: false } : null))
    }
    home = out.home ?? home
    claudeDir = out.claude_dir ?? claudeDir
    wiki = out.wiki ?? wiki
    theme = out.theme ?? theme
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
      if (role === 'orchestrator') await notifyAsks($, out.board)
    }
  } finally {
    ticking = false
  }
}

let asked: Set<string> | null = null

/** A new item waiting on the person raises a native notification at once (they may be
 * away, on their phone). The first look only learns what already waited. */
async function notifyAsks($: any, tree: BoardRepo[]) {
  if (!tree.length) return  // a failed read, not "nothing waits": keep what was asked
  const jobs = tree.flatMap(r => r.jobs)
  const now = new Set(jobs
    .filter(j => j.pending && ASKS_USER.includes(j.pending))
    .map(j => `${j.job}:${j.pending}`))
  // a wait past 1.5x its expected time: told once, nothing is stopped
  for (const j of jobs) for (const n of j.agents) if (n.overdue) now.add(`${n.agent}:overdue:${n.overdue}`)
  const fresh = asked ? [...now].filter(k => !asked!.has(k)) : []
  asked = now
  for (const k of fresh) {
    const [jobName, kind, expect] = k.split(':') as [string, string, string?]
    const text = kind === 'overdue' ? `waiting longer than expected (${expect})` : PENDING[kind] ?? kind
    try {
      await $.ui.notify(`${jobName}: ${text}`, { title: 'OKe', key: `oke-${jobName}` })
    } catch {
      // no channel, or the person turned notifications off
    }
  }
}

const COMPACT_NOTE ='OKe task boundary: the report for this assignment is written. Keep the role, the job, '
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

const USAGE_HEAD = 'time,model,input,output,cache_read,cache_write'

/** One line per turn in the agent's usage.csv (the orchestrator's in the OKe home), which
 * `oke cost` adds up. */
async function logUsage($: any, e: any) {
  const u = e.usage
  const dir = folder ?? (role === 'orchestrator' ? home : null)
  if (!pane || !u || !dir) return
  const path = `${dir}/usage.csv`.replace(/\\/g, '/')
  const line = [new Date().toISOString(), u.model ?? '', u.input_tokens ?? 0, u.output_tokens ?? 0,
    u.cache_read_input_tokens ?? 0, u.cache_creation_input_tokens ?? 0].join(',')
  try {
    let old = ''
    try {
      old = await $.fs.read(path)
    } catch {
      old = `${USAGE_HEAD}\n`
    }
    await $.fs.write(path, `${old}${old.endsWith('\n') ? '' : '\n'}${line}\n`)
  } catch {
    // a missed line only makes the totals read lower
  }
}

async function submitRetry($: any, kind: string) {
  pending = null
  // the person is typing: their own turn replaces the retry (prompt.submit cancels it)
  if ((await $.prompt.read()).text.trim()) {
    $.ui.status('OKe: API error retry waits for your prompt')
    return
  }
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

/** The board's colors: the mascot's neon pastels, bright enough on a dark terminal. */
const INK = {
  pink: '#ff7ad9', lavender: '#b9a6ff', cyan: '#5fe3ea', green: '#7ee8a6', yellow: '#ffd866',
  red: '#ff6b6b', magenta: '#e58bff', white: '#f4f1ff', dim: '#6b6880', ink: '#1a1626',
}

/** herdr agent status: mark, word, color. */
const STATUS: Record<string, [string, string, string | undefined]> = {
  working: ['●', 'working', INK.green],
  idle: ['○', 'idle', undefined],
  blocked: ['!', 'blocked', INK.yellow],
  dead: ['✗', 'dead', INK.red],
  off: ['–', 'off', INK.dim],
}

/** `oke pending` kinds, as the person reads them. */
const PENDING: Record<string, string> = {
  plan: 'approve the plan',
  merge: 'approve the merge',
  question: 'PM asks',
  peer: 'a PM did not answer',
  blocked: 'PM is blocked',
  failed: 'PM reports a failure',
  silent: 'PM is silent',
  report_done: 'read the PM report',
  unsent: 'assignment not sent',
  legacy: 'needs a look',
}

/** Pending kinds that wait on the person (the rest the orchestrator handles itself). */
const ASKS_USER = ['plan', 'merge', 'question', 'blocked', 'failed']

/** Git commands no OKe agent runs, the orchestrator included: OKe merges through `oke job
 * merge`, and these lose work or reach other people. `git -C <dir>`, `-c k=v` and
 * `--git-dir <dir>` may come first, and a wrapper word (`time`, `command`, ...) or a path. */
const GIT_DENIED = new RegExp(
  // only where git starts a command: not inside a commit message or a grep pattern
  // a wrapper word or VAR=x counts only right where a command starts
  String.raw`(?:^|[;&|(\n\x60]|\$\()\s*(?:(?:then|do|else|time|command|env|exec|nohup|xargs)\s+|\w+=\S*\s+)*`
  + String.raw`(?:\S*[\\/])?git(?:\.exe)?`
  + String.raw`(?:\s+(?:-C\s+\S+|-c\s+\S+|--(?:git-dir|work-tree|namespace)\s+\S+|--[\w-]+(?:=\S+)?))*\s+`
  + String.raw`(push\b|reset\s[^;&|\n]*--(?:hard|merge)\b|clean\s[^;&|\n]*(?:-\w*f|--force)`
  + String.raw`|branch\s[^;&|\n]*(?:-D\b|--delete\s+--force|--force\s+--delete|-d\s+-f\b)`
  + String.raw`|checkout\s[^;&|\n]*(?:-f\b|--force)|stash\s+(?:drop|clear)\b|update-ref\s[^;&|\n]*-d\b`
  + String.raw`|filter-(?:branch|repo)\b|worktree\s+remove\s[^;&|\n]*--force)`,
)

/** The command with quoted text and heredoc bodies blanked: what git would see as words. */
function unquoted(command: string): string {
  return command
    .replace(/<<-?\s*['"]?(\w+)['"]?[^\n]*\n[\s\S]*?\n\s*\1\b/g, '<<_')
    .replace(/"(?:\\.|[^"\\])*"|'[^']*'/g, '""')
}

/** The refusal for a Bash command a child must not run, else null. */
export function gitDenied(command: string, role?: string): string | null {
  const m = GIT_DENIED.exec(unquoted(command))
  return m ? `OKe blocks \`git ${m[1]!.split(/\s/)[0]}\` here: it can lose work or publish it. `
    + (role === 'orchestrator' ? 'Tell the user to run it themselves if they want it.'
      : 'Merges go through `oke job merge`; if you really need it, report a question instead.') : null
}

// no `-c`: `-c core.pager=<cmd>` and the like run commands
const REVIEWER_GIT = /^\s*git(?:\s+(?:-C\s+\S+|--no-pager))*\s+(?:diff|log|show|status)\b/
// Tools a reviewer gets at all (the launch's ROLE_TOOLS["reviewer"]); anything else, Agent and MCP tools included, is refused.
const REVIEWER_TOOLS = ['Read', 'Grep', 'Glob', 'Skill', 'ToolSearch', 'TaskCreate', 'TaskUpdate', 'TaskList',
  'WebFetch', 'WebSearch', 'Bash', 'Edit', 'Write']
// Inside Orca a reviewer also reports with `orca orchestration send` (worker_done) or asks
// with `ask`; a quoted orca path reads as "" here.
const REVIEWER_ORCA = /^\s*(?:\S*orca(?:\.exe)?|"")\s+orchestration\s+(?:send|ask)\b/

/** The role and folder an OKe first or revive prompt names, else null: the mod knows
 * them from the first turn on, before `oke spawn` has recorded the agent. */
export function promptRole(text: string): { role: string, folder: string } | null {
  // a path may hold spaces (a user folder): it ends at assignment.md
  const m = /oke:(pm|worker|reviewer) skill\.[\s\S]*?assignment file (?:is )?(\S.*?assignment\.md)/.exec(text)
  return m ? { role: m[1]!, folder: m[2]!.replace(/[\\/][^\\/]*$/, '') } : null
}

/** The refusal for a reviewer's tool call that could change the repository, else null.
 * The reviewer's launch flags say the same; this holds where no flags reach the agent
 * (Orca starts it itself). It writes only its own folder (report, board). */
export function reviewerDenied(tool: string, input: any, own: string | null): string | null {
  if (!REVIEWER_TOOLS.includes(tool)) return `OKe reviewers do not use ${tool}: read, review and report.`
  if (tool === 'Edit' || tool === 'Write') {
    const norm = (p: string) => p.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()
    const target = norm(String(input.file_path ?? ''))
    // an absolute path without `..`: a prefix check is then enough
    const plain = /^([a-z]:)?\//.test(target) && !/(^|\/)\.\.(\/|$)/.test(target)
    if (own && plain && target.startsWith(norm(own) + '/')) return null
    return 'OKe reviewers change nothing: write only report.md and board.md in your own folder.'
  }
  if (tool !== 'Bash') return null
  const raw = String(input.command ?? '')
  const cmd = unquoted(raw)
  // `$`, backticks and line breaks act inside double quotes too, so they count before unquoting
  const safe = !/[`$\r\n]/.test(raw) && !/[;&|<>]|--output|--ext-diff/.test(cmd)
  if (safe && (REVIEWER_GIT.test(cmd) || REVIEWER_ORCA.test(cmd))) return null
  return 'OKe reviewers run only read-only git: git diff, log, show or status, one command, without --output.'
}

/** The refusal for a worker's file write into the OKe jobs folder outside its own folder,
 * else null: the plan, the ledger and the reviewer's report there are what the merge gate
 * trusts. The launch's deny rules say the same where flags reach the agent.
 * shortcut: a Bash command can still write there; the user's merge approval is the last layer. */
export function workerDenied(tool: string, input: any, own: string | null): string | null {
  if (!own || (tool !== 'Edit' && tool !== 'Write' && tool !== 'NotebookEdit')) return null
  // drive letter, case, backslashes and `.`/`..` segments resolved: `..` could leave the own folder
  const norm = (p: string) => {
    const out: string[] = []
    for (const part of p.replace(/\\/g, '/').toLowerCase().replace(/^[a-z]:/, '').split('/')) {
      if (part === '..') out.pop()
      else if (part !== '.' && part !== '') out.push(part)
    }
    return '/' + out.join('/')
  }
  const mine = norm(own)
  const jobs = mine.replace(/\/[^/]+\/[^/]+$/, '')
  if (!jobs.endsWith('/jobs')) return null
  const target = norm(String(input.file_path ?? input.notebook_path ?? ''))
  if (!target.startsWith(jobs + '/')) return null
  if (target.startsWith(mine + '/')) return null
  return 'OKe workers write only in cwd and in their own folder: the plan, the ledger and the review are not yours to change.'
}

/** The refusal for an orchestrator tool call that does a job's work itself, else null.
 * Every request becomes a job, so plan approval, review and the merge gate are never
 * skipped. It writes only OKe's home outside the wikis (those change through `oke wiki`)
 * and its own memory (<claude config>/projects/<project>/memory/). */
export function orchestratorDenied(tool: string, input: any,
  dirs: { home: string | null, wiki?: string | null, claude?: string | null, root?: string | null }): string | null {
  const why = 'OKe orchestrators never do the work: make it a job (`oke job new`) and give it to the PM.'
  if (tool === 'Agent' || tool === 'Task' || tool === 'Workflow') return `${why} No subagents either.`
  if (tool !== 'Edit' && tool !== 'Write' && tool !== 'NotebookEdit') return null
  const norm = (p: string) => p.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()
  const under = (dir: string | null | undefined) => dir ? target.startsWith(norm(dir) + '/') : false
  const target = norm(String(input.file_path ?? input.notebook_path ?? ''))
  // only a canonical absolute path: `.`, `..`, `//` or a name ending in a dot or space
  // (Windows drops those) or an 8.3 short name (KNOWLE~1) could reach the wiki past the
  // prefix checks
  const plain = /^([a-z]:)?\/[^/]/.test(target) && !/(^|\/)\.{1,2}(\/|$)|\/\/|[. ](\/|$)|~\d/.test(target)
  // Claude Code names a project's folder after its root, every other character a dash; a
  // root it names otherwise (a git root above it) only refuses: never a foreign memory
  const memory = !!dirs.claude && !!dirs.root
    && under(`${dirs.claude}/projects/${dirs.root.replace(/[^A-Za-z0-9]/g, '-')}/memory`)
  if (plain && ((under(dirs.home) && !under(dirs.wiki)) || memory)) return null
  return `${why} You may write only under ${dirs.home ?? '$OKE_HOME'} (a scratch file: its tmp/ folder; `
    + 'never a wiki: use `oke wiki`) and in your own memory folder.'
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
  completed: ['✓', INK.green],
  in_progress: ['▸', INK.pink],
  pending: ['○', undefined],
}

const TODO_ORDER = ['in_progress', 'pending', 'completed']

/** The lines of one `## <title>` section of a board.md, an empty marker (없음, none, -) dropped. */
function section(board: string, title: string, max: number): string[] {
  const lines = board.split(/\r?\n/).map(l => l.trim())
  const at = lines.findIndex(l => l.startsWith('##') && l.slice(2).replace(/\(.*$/, '').trim() === title)
  if (at < 0) return []
  const end = lines.findIndex((l, i) => i > at && l.startsWith('## '))
  return lines.slice(at + 1, end < 0 ? undefined : end)
    .map(l => l.replace(/^[-*]\s+/, ''))
    .filter(l => l && !/^(없음|none|-)$/i.test(l))
    .slice(0, max)
}

/** board.md sections shown under an agent's todos: title, mark, label, color. */
const NOTES: [string, string, string, string][] = [
  ['결정 필요', '?', 'decide', INK.yellow],
  ['막힌 것', '✗', 'blocked', INK.red],
  ['확인 필요', '!', 'check', INK.magenta],
]  // 결정 (made already) asks nothing of the person: it shows only in the opened board.md

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
// 12x8 pixel characters drawn as a 12x4 Raster: each cell's upper half block takes the
// top pixel as its foreground, the bottom as background. Columns 10-11 hold effects.
// `theme` in config.json picks the set; each role has its own character and color.
type Mood = 'conduct' | 'rest' | 'wave' | 'stop'
const W = 12
const DEFAULT = 0x01000000
const PALETTE: Record<string, number> = {
  p: 0xff7ad9, c: 0xb9a6ff, v: 0x2de2e6, w: 0xffffff, m: 0xff5c8a, d: 0x2b2140,
  n: 0xffe66d, y: 0x6ff7ff, h: 0xff4d8d, z: 0x8a8fa8, r: 0xff5c5c,
  // disco bodies for the other roles
  e: 0x6aa0ff, g: 0x7ee8a6, O: 0xffa24d,
  // slime (body, shine) and mushroom (cap, stem, feet); k: eyes
  s: 0x7bd84f, l: 0xdcffc8, k: 0x1a1626, b: 0x4f9dff, B: 0xc8e4ff,
  a: 0xff8fc8, A: 0xffdcef, i: 0xffd84a, I: 0xfff3bf,
  o: 0xff8c2e, G: 0x4fbf55, P: 0xb38cff, t: 0xffe3b3, u: 0xe0bf8f,
  // cast sprites: f leaf, j brown, q dark grey, x light grey
  f: 0x5cc85c, j: 0x8a5a3c, q: 0x3a3a48, x: 0xd8d8e4,
}
/** Per-theme colors over PALETTE: the cast themes' role coats and color-scheme overrides. */
const PAL: Record<string, Record<string, number>> = {}
/** `a` mixed toward `b` by `t` (0..1), per RGB channel. */
const mix = (a: number, b: number, t: number) =>
  [16, 8, 0].reduce((n, s) => n | Math.round(((a >> s) & 255) * (1 - t) + ((b >> s) & 255) * t) << s, 0)
/** `frame` with pixels set at [row, col, color]. */
function put(frame: string[], dots: [number, number, string][]): string[] {
  const rows = frame.map(r => r.split(''))
  for (const [r, c, ch] of dots) rows[r]![c] = ch
  return rows.map(r => r.join(''))
}
/** `frame` with each color letter in `map` swapped. */
function paint(frame: string[], map: Record<string, string>): string[] {
  return frame.map(r => r.replace(/./g, ch => map[ch] ?? ch))
}
/** Stuck: the whole character turns red, its eyes (k, d) stay. */
function stuck(frame: string[]): string[] {
  return frame.map(r => r.replace(/[^.kd]/g, 'r'))
}
const SPARKS: [number, number, string][][] = [
  [[0, 10, 'n'], [2, 11, 'y'], [5, 9, 'w']],
  [[1, 11, 'y'], [3, 10, 'n'], [0, 9, 'w']],
  [[1, 9, 'y'], [4, 11, 'n'], [0, 11, 'w']],
  [[0, 10, 'n'], [2, 9, 'w'], [5, 11, 'y']],
]

// disco: a blob with headphones and a neon visor; the body color tells the role
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
function disco(body: string): Record<Mood, string[][]> {
  const blob = paint(BLOB, { c: body })
  const bob = ['............', ...blob.slice(0, 7)]
  const sleepy = put(blob, [[3, 2, 'd'], [3, 3, 'd'], [3, 4, body], [3, 5, body], [3, 6, 'd'], [3, 7, 'd']])
  return {
    conduct: [blob, bob, blob, bob].map((f, i) => put(f, SPARKS[i]!)),
    rest: [put(sleepy, [[2, 10, 'z']]), put(sleepy, [[1, 10, 'z'], [0, 11, 'z']])],
    wave: [
      put(blob, [[4, 8, body], [3, 9, body], [2, 10, body], [0, 10, 'h'], [0, 11, 'h']]),
      put(blob, [[4, 8, body], [4, 9, body], [3, 10, body], [1, 11, 'h']]),
    ],
    stop: [stuck(put(BLOB, [[3, 3, 'd'], [3, 6, 'd']]))],
  }
}

// slime and mushroom: a hopping slime (s body, l shine, k eyes) and a mushroom (o cap, w spots,
// t stem, k eyes). Each hop: rest, squash, leap (stretched), land.
const SLIME = {
  rest: [
    '............',
    '...sss......',
    '..sssss.....',
    '.slsssss....',
    '.sksssks....',
    '.sssmsss....',
    'sssssssss...',
    '.sssssss....',
  ],
  squash: [
    '............',
    '............',
    '..ssssss....',
    '.slssssss...',
    '.skssssks...',
    'ssssmsssss..',
    'ssssssssss..',
    '.ssssssss...',
  ],
  leap: [
    '...sss......',
    '..sssss.....',
    '.slsssss....',
    '.sksssks....',
    '.sssmsss....',
    '.sssssss....',
    '..sssss.....',
    '............',
  ],
}
const SHROOM = {
  rest: [
    '...oooo.....',
    '.oooowooo...',
    'oowoooooo...',
    'oooooowoo...',
    '..ttttt.....',
    '..tktkt.....',
    '..ttttt.....',
    '..u...u.....',
  ],
  squash: [
    '............',
    '...oooo.....',
    '.oooowooo...',
    'oowoooooo...',
    'oooooowoo...',
    '..tktkt.....',
    '.ttttttt....',
    '.u.....u....',
  ],
  leap: [
    '..oooo......',
    'oooowooo....',
    'owoooooo....',
    'ooooowoo....',
    '.ttttt......',
    '.tktkt......',
    '.ttttt......',
    '..u.u.......',
  ],
}
function hopper(sprite: typeof SLIME, map: Record<string, string>, eye: string): Record<Mood, string[][]> {
  const [rest, squash, leap] = [sprite.rest, sprite.squash, sprite.leap].map(f => paint(f, map))
  // eyes shut: the eye row's dark pixels turn into the skin around them
  const sleepy = (f: string[]) => f.map(r => r.includes('k') ? r.replace(/k/g, eye) : r)
  const shout: [number, number, string][] = [[0, 10, 'n'], [1, 10, 'n'], [2, 10, 'n'], [4, 10, 'n']]
  return {
    conduct: [rest!, squash!, leap!, squash!].map((f, i) => put(f, i === 2 ? SPARKS[i]! : [])),
    rest: [put(sleepy(squash!), [[2, 10, 'z']]), put(sleepy(squash!), [[1, 10, 'z'], [0, 11, 'z']])],
    wave: [put(leap!, shout), put(rest!, shout)],
    stop: [stuck(rest!)],
  }
}

// cast: the generic builder. A sprite draws c (body), l (light) and s (shade) once; each role
// repaints them with its coat. k are eyes (shut while sleeping), d other dark details.
// Rows may stop before column 12: the rest is empty.
type Sprite = {
  a: string[]            // the pose; stop is this pose turned red
  b?: string[]           // conduct's second frame; default: `a` bobbed down a row
  conduct?: string[][]   // conduct's frames instead of a, b, a, b
  sleep?: string[]       // rest's pose; default: `a` with its eyes turned to `lid`
  lid?: string           // the color around the eyes; default c
  wave?: string[][]      // wave's frames; default: a and b
}
const ROLES = ['orchestrator', 'pm', 'worker', 'reviewer']
const COATS = [0xb9a6ff, 0x6aa0ff, 0x7ee8a6, 0xffa24d] // the disco role colors
const SHOUT: [number, number, string][] = [[0, 10, 'n'], [1, 10, 'n'], [2, 10, 'n'], [4, 10, 'n']]
/** `frame` with effect pixels set only where it is empty. */
const fx = (frame: string[], dots: [number, number, string][]) => put(frame, dots.filter(([r, c]) => frame[r]?.[c] === '.'))
function moods(s: Sprite): Record<Mood, string[][]> {
  const pad = (f: string[]) => Array.from({ length: 8 }, (_, i) => (f[i] ?? '').padEnd(W, '.'))
  const a = pad(s.a)
  const b = pad(s.b ?? ['', ...s.a.slice(0, 7)])
  const shut = pad(s.sleep ?? s.a.map(r => r.replace(/k/g, s.lid ?? 'c')))
  return {
    conduct: (s.conduct?.map(pad) ?? [a, b, a, b]).map((f, i) => fx(f, SPARKS[i % 4]!)),
    rest: [fx(shut, [[2, 10, 'z']]), fx(shut, [[1, 10, 'z'], [0, 11, 'z']])],
    wave: (s.wave?.map(pad) ?? [a, b]).map(f => fx(f, SHOUT)),
    stop: [stuck(a)],
  }
}
/** Registers theme `name`: sprite `s` once per role in that role's coat (with a light and a shade). */
function cast(name: string, s: Sprite, coats = COATS, extra: Record<string, number> = {}) {
  const m = moods(s)
  const pal: Record<string, number> = { ...PALETTE, ...extra }
  THEMES[name] = Object.fromEntries(ROLES.map((role, i) => {
    const [c, l, sh] = ['QRST'[i]!, 'UVWX'[i]!, 'EFHJ'[i]!]
    Object.assign(pal, { [c]: coats[i]!, [l]: mix(coats[i]!, 0xffffff, 0.55), [sh]: mix(coats[i]!, 0, 0.3) })
    const map = { c, l, s: sh }
    return [role, Object.fromEntries(Object.entries(m).map(([k, fs]) => [k, fs.map(f => paint(f, map))])) as Record<Mood, string[][]>]
  }))
  PAL[name] = pal
}
const THEMES: Record<string, Record<string, Record<Mood, string[][]>>> = {
  disco: { orchestrator: disco('c'), pm: disco('e'), worker: disco('g'), reviewer: disco('O') },
  slime: {
    orchestrator: hopper(SLIME, { s: 's', l: 'l' }, 's'),
    pm: hopper(SLIME, { s: 'b', l: 'B' }, 'b'),
    worker: hopper(SLIME, { s: 'a', l: 'A' }, 'a'),
    reviewer: hopper(SLIME, { s: 'i', l: 'I' }, 'i'),
  },
  mushroom: {
    orchestrator: hopper(SHROOM, { o: 'P' }, 't'),
    pm: hopper(SHROOM, { o: 'b' }, 't'),
    worker: hopper(SHROOM, { o: 'o' }, 't'),
    reviewer: hopper(SHROOM, { o: 'G' }, 't'),
  },
}

// puppy: floppy ears (s), wagging tail; curls up to sleep
const DOG = [
  '..ccccc...',
  'scccccccs.',
  'sckccckcs.',
  'sacldlcas.',
  's.ccccc.s.',
  '..clllc.c.',
  '..clllcc..',
  '..cc.cc...',
]
const DOG_B = put(DOG, [[5, 8, '.'], [6, 8, 'c']])
cast('cute-dog', {
  a: DOG,
  conduct: [DOG, DOG_B, put(DOG, [[2, 2, 'c'], [2, 6, 'c']]), put(DOG_B, [[4, 0, '.'], [3, 0, '.'], [0, 1, 's']])],
  sleep: ['', '', '', '.cccc.....', 'sccccccc..', 'scssccccc.', 'dllccccccc', '..cllllcc.'],
  wave: [put(DOG, [[4, 1, 'l'], [5, 1, 'c']]), put(DOG_B, [[4, 0, '.'], [3, 0, '.'], [0, 1, 's']])],
}, [0xe8a85a, 0x9a6a44, 0xf0dcb0, 0x8a8fa0])

// kitten: pointy ears, whiskers (w), swishing tail; curls up with its tail around it
const CAT = [
  '.c.....c..',
  '.ca...ac..',
  '.ccccccc..',
  'wckccckcw.',
  'wcccacccw.',
  '..clllc..c',
  '..clllc.c.',
  '..cc.ccc..',
]
const CAT_B = put(CAT, [[5, 9, '.'], [5, 8, 'c'], [4, 9, 'c']])
const KITTEN: Sprite = {
  a: CAT,
  conduct: [CAT, CAT_B, put(CAT, [[3, 2, 'c'], [3, 6, 'c']]), put(CAT_B, [[0, 7, '.'], [0, 8, 'c']])],
  sleep: ['', '', '', '.c..c.....', 'cccccccc..', 'csccsccccc', 'ccaacccccc', 'sssssssc..'],
  wave: [put(CAT, [[5, 1, 'c'], [4, 0, 'l']]), put(CAT_B, [[5, 1, 'c'], [4, 0, 'l']])],
}
cast('cute-cat', KITTEN, [0xf2a65a, 0x9ba7c0, 0xf4f0e8, 0xd9b98c])

const GHOST: Sprite = {
  a: ['...cccc', '..cccccc', '.cckcckcc', '.caccccac', '.cccccccc', '.cccccccc', '.cccccccc', '.cc.cc.cc'],
  b: ['..cccc', '.cccccc', 'cckcckcc', 'caccccac', 'cccccccc', 'cccccccc', 'cccccccc', 'c.cc.cc.c'],
}
const ROBOT: Sprite = {
  a: ['....i', '....x', '.cccccccc', 'xclkllklcx', 'xcllddllcx', '.cccccccc', '..cscsc', '..ss..ss'],
  b: ['....m', '....x', '.cccccccc', 'xclkllklcx', 'xcllddllcx', '.cccccccc', 'x.cscsc.x', '..ss..ss'],
  lid: 'l',
}
const FROG: Sprite = {
  a: ['', '.cc...cc', 'cwkc.cwkc', 'ccccccccc', 'cacccccac', 'ccdddddcc', '.clllllc', 'cc.....cc'],
  b: ['', '', '.cc...cc', 'cwkc.cwkc', 'ccccccccc', 'cacdddcac', 'cclllllcc', 'c.c...c.c'],
  sleep: ['', '', '.cc...cc', 'cccc.cccc', 'ccccccccc', 'cacdddcac', 'cclllllcc', 'c.c...c.c'],
}
const PENGUIN: Sprite = {
  a: ['...cccc', '..cccccc', '.ccwwwwcc', '.cwkwwkwc', '.cwwoowwc', 'ccwwwwwwcc', '.cwwwwwwc', '..oo..oo'],
  b: ['', '...cccc', '..cccccc', '.ccwwwwcc', 'ccwkwwkwcc', 'c.cwoowc.c', '.cwwwwwwc', '..oo..oo'],
  lid: 'w',
}
const BUNNY: Sprite = {
  a: ['..ca..ac', '..ca..ac', '..cc..cc', '.cccccccc', '.cckcckcc', '.cacmmcac', '..cccccc', '..ll..ll'],
  b: ['..ca', '..ca..accc', '..cc..cc', '.cccccccc', '.cckcckcc', '.cacmmcac', '..cccccc', '..ll..ll'],
}
const BEAR: Sprite = {
  a: ['.cc....cc', '.csccccsc', '.cccccccc', '.cckcckcc', '.cclddlcc', '.acllllca', '.cccccccc', '.cc....cc'],
}
const DUCK: Sprite = {
  a: ['...ccc', '..ccccc', '..ckccc', 'ooocccc', '...cccc..c', '..cccccccc', '..clllccc', '...ccccc'],
  b: ['', '...ccc', '..ccccc', '..ckccc', 'ooocccc..c', '..ccclllcc', '..cccccc', '...ccccc'],
}
const OCTOPUS: Sprite = {
  a: ['...cccc', '..cccccc', '.cccccccc', '.cckcckcc', '.caccccac', '.cccccccc', '.cc.cc.cc', 'c..c..c..c'],
  b: ['', '...cccc', '..cccccc', '.cccccccc', '.cckcckcc', '.caccccac', 'cccccccccc', '.c..c..c'],
}
const ALIEN: Sprite = {
  a: ['.l....l', '..c..c', '..cccc', '.cccccc', 'ckkcckkc', '.cccccc', '..cmmc', '...cc'],
  b: ['l......l', '.c....c', '..cccc', '.cccccc', 'ckkcckkc', '.cccccc', '..cddc', '...cc'],
}
const CACTUS: Sprite = {
  a: ['....pp', '...cccc', '..cccccc', 'c.ckcckc.c', 'ccaccccacc', '..cccccc', '.oooooooo', '..oooooo'],
  b: ['....pp', '...cccc', 'c.cccccc.c', 'c.ckcckc.c', 'ccaccccacc', '..cccccc', '.oooooooo', '..oooooo'],
}
const JELLY: Sprite = {
  a: ['...cccc', '..clcccc', '.cccccccc', '.cckcckcc', '.caccccac', '.c.c..c.c', '..c.cc.c', '.c..c..c'],
  b: ['', '...cccc', '..clcccc', '.cccccccc', '.cckcckcc', '.caccccac', '..c.cc.c', '.c.c..c.c'],
}
const FOX: Sprite = {
  a: ['c.......c', 'cc.....cc', 'ccccccccc', 'cckccckcc', 'wwwcdcwww', '.wwwwwww.c', '..ccccc.cc', '..c...c.cw'],
  b: ['c.......c', 'cc.....cc', 'ccccccccc', 'cckccckcc', 'wwwcdcwww', '.wwwwwww', '..ccccc.cc', '..c...cccw'],
}
const PANDA: Sprite = {
  a: ['.qq....qq', '.qwwwwwwq', '.wwwwwwww', '.wqkwwkqw', '.wwwddwww', '.cccccccc', '.qwwwwwwq', '..qq..qq'],
  lid: 'q',
}
const HAMSTER: Sprite = {
  a: ['', '..cc..cc', '.cccccccc', 'cckcccckcc', 'caclddlcac', 'cllllllllc', '.cllllllc', '..aa..aa'],
  b: ['', '', '..cc..cc', '.cccccccc', 'cckcccckcc', 'aaclddlcaa', 'cllllllllc', '.caa..aac'],
}
const OWL_A = ['.c......c', '.cccccccc', '.cwwccwwc', '.cwkcckwc', '.cccooccc', '.cllllllc', '.cllllllc', 'jjojjjjojj']
const OWL: Sprite = {
  a: OWL_A,
  b: put(OWL_A.map(r => r.padEnd(10, '.')), [[2, 0, 'c'], [3, 0, 'c'], [4, 0, 'c'], [2, 9, 'c'], [3, 9, 'c'], [4, 9, 'c']]),
  sleep: OWL_A.map((r, i) => (i === 2 ? r.replace(/w/g, 'c') : i === 3 ? r.replace(/[wk]/g, 's') : r)),
}
const WHALE: Sprite = {
  a: ['..e.e', '...e', '..ccccc..c', '.ccccccccc', 'cckccccccc', 'calllllcc', '.llllllc', ''],
  b: ['...e', '..e.e', '..ccccc', '.cccccccc', 'cckccccccc', 'calllllccc', '.llllllc.c', ''],
}
const DINO: Sprite = {
  a: ['.....cccc', '.....ckccc', '.....ccccc', 'c...cccc', 'cc.ccccl', '.cccllll', '..cccccc', '..cc..cc'],
  b: ['.....cccc', '.....ckccc', '.....ccddd', 'c...cccc', 'cc.cccc.l', '.cccllll', '..cccccc', '..c..cc'],
}
const STAR: Sprite = {
  a: ['....cc', '....cc', 'cccccccccc', '.cckcckcc', '..caccac', '..cccccc', '.ccc..ccc', '.cc....cc'],
  b: ['....cc', 'w...cc...w', '.cccccccc', '.cckcckcc', '..caccac', '..cccccc', '.ccc..ccc', 'cc......cc'],
}
const HEART: Sprite = {
  a: ['.ccc..ccc', 'cwcccccccc', 'cccccccccc', 'cckcccckcc', '.cacddcac', '..cccccc', '...cccc', '....cc'],
  b: ['', '..cc..cc', '.cwcccccc', '.cckcckcc', '.cacddcac', '..cccccc', '...cccc', '....cc'],
}
const COFFEE: Sprite = {
  a: ['...x.x', '..x.x', '.cjjjjjc', '.ckccckcc', '.cacdcac.c', '.cccccccc', '..ccccc', '.xxxxxxxx'],
  b: ['..x.x', '...x.x', '.cjjjjjc', '.ckccckcc', '.cacdcac.c', '.cccccccc', '..ccccc', '.xxxxxxxx'],
}
const ONIGIRI: Sprite = {
  a: ['....ww', '...wwww', '..wwwwww', '..wkwwkw', '.wawwwwaw', 'wwwwddwwww', 'wwccccccww', '.wccccccw'],
  lid: 'w',
}
const AXOLOTL: Sprite = {
  a: ['.s......s', 's.cccccc.s', 'sccccccccs', 'scckcckccs', '.cacddcac', '..cccccc', '..cllllc', '..c.cc.c'],
  b: ['s........s', '.scccccccs', 'sccccccccs', '.cckcckcc', 'scacddcacs', '..cccccc', '..cllllc', '.c..cc..c'],
}
const BEE: Sprite = {
  a: ['..d....d', '...d..d', 'ww.cccc.ww', 'wwckcckcww', '..cccccc', '..qqqqqq', '..cccccc', '....qq'],
  b: ['..d....d', 'ww.d..d.ww', 'ww.cccc.ww', '..ckcckc', '..cccccc', '..qqqqqq', '..cccccc', '....qq'],
}
const SNAIL: Sprite = {
  a: ['.k.k', '.t.t.cccc', '.t.tccsscc', '.tttcsccsc', '.tttcscscc', '.tttccsscc', 'tttttttttt', ''],
  b: ['', '.k.k.cccc', '.t.tccsscc', '.tttcsccsc', '.tttcscscc', '.tttccsscc', 'tttttttttt', ''],
  lid: 't',
}
const CHICK: Sprite = {
  a: ['....cc', '...cccc', '..cccccc', '.cckcckcc', '.cacoocac', '.lccccccl', '..cccccc', '...o..o'],
  b: ['....cc', '...cccc', '..cccccc', 'lcckcckccl', '.cacoocac', '.cccccccc', '..cccccc', '...o..o'],
}
const SAKURA: Sprite = {
  a: ['....cc', '.cc.cc.cc', '.cciiiicc', '..ikiiki', '.ccimmicc', '.cc.cc.cc', '....ff', '..fffff'],
  b: ['', '....cc', '.cc.cc.cc', '.cciiiicc', '..ikiiki', '.ccimmicc', '.cc.ff.cc', '..fffff'],
  lid: 'i',
}

for (const [name, s] of [
  ['ghost', GHOST], ['robot', ROBOT], ['frog', FROG], ['penguin', PENGUIN], ['bunny', BUNNY], ['bear', BEAR],
  ['duck', DUCK], ['octopus', OCTOPUS], ['alien', ALIEN], ['cactus', CACTUS], ['jellyfish', JELLY], ['fox', FOX],
  ['panda', PANDA], ['hamster', HAMSTER], ['owl', OWL], ['whale', WHALE], ['dino', DINO], ['star', STAR],
  ['pixel-heart', HEART], ['coffee', COFFEE], ['onigiri', ONIGIRI], ['axolotl', AXOLOTL], ['bee', BEE],
  ['snail', SNAIL], ['chick', CHICK], ['sakura', SAKURA],
] as [string, Sprite][]) cast(name, s)
// palette variants of the sprites above
cast('pastel', BUNNY, [0xffc8dd, 0xbde0fe, 0xcaffbf, 0xfdffb6])
cast('candy', HEART, [0xff8fc8, 0xff5c8a, 0xffc2e2, 0xc86bff])
cast('ocean', WHALE, [0x2a6fdb, 0x2ec4b6, 0x5ab0ff, 0x7ef0c8])
cast('autumn', FOX, [0xd9534f, 0xf08a24, 0xc9a227, 0xa0703c])
cast('midnight', OWL, [0x5b5fc7, 0x3d7ea6, 0x7a5ba6, 0x9aa5c8])

// famous color schemes: role coats from the scheme's accents; k/d (eyes, details) its background,
// w its foreground, a its pink or red, o its orange, i its yellow
const scheme = (bg: number, fg: number, a: number, o: number, i: number) => ({ k: bg, d: bg, w: fg, a, m: a, p: a, o, i })
cast('tokyo-night', JELLY, [0xbb9af7, 0x7aa2f7, 0x9ece6a, 0xff9e64], scheme(0x1a1b26, 0xc0caf5, 0xf7768e, 0xff9e64, 0xe0af68))
cast('catppuccin', KITTEN, [0xcba6f7, 0x89b4fa, 0xa6e3a1, 0xfab387], scheme(0x11111b, 0xcdd6f4, 0xf5c2e7, 0xfab387, 0xf9e2af))
cast('github', ROBOT, [0xbc8cff, 0x58a6ff, 0x3fb950, 0xf0883e], { ...scheme(0x0d1117, 0xe6edf3, 0xff7b72, 0xffa657, 0xd29922), x: 0x8b949e, m: 0xff7b72 })
cast('dracula', GHOST, [0xbd93f9, 0x8be9fd, 0x50fa7b, 0xffb86c], scheme(0x282a36, 0xf8f8f2, 0xff79c6, 0xffb86c, 0xf1fa8c))
cast('nord', PENGUIN, [0xb48ead, 0x81a1c1, 0xa3be8c, 0xd08770], scheme(0x2e3440, 0xeceff4, 0xbf616a, 0xd08770, 0xebcb8b))
cast('gruvbox', BEAR, [0xd3869b, 0x83a598, 0xb8bb26, 0xfe8019], scheme(0x282828, 0xebdbb2, 0xfb4934, 0xfe8019, 0xfabd2f))
cast('solarized', STAR, [0x6c71c4, 0x268bd2, 0x859900, 0xcb4b16], scheme(0x002b36, 0xfdf6e3, 0xd33682, 0xcb4b16, 0xb58900))
cast('one-dark', ALIEN, [0xc678dd, 0x61afef, 0x98c379, 0xd19a66], scheme(0x282c34, 0xabb2bf, 0xe06c75, 0xd19a66, 0xe5c07b))
cast('monokai', FROG, [0xae81ff, 0x66d9ef, 0xa6e22e, 0xfd971f], scheme(0x272822, 0xf8f8f2, 0xf92672, 0xfd971f, 0xe6db74))
cast('rose-pine', BUNNY, [0xc4a7e7, 0x9ccfd8, 0xebbcba, 0xf6c177], scheme(0x191724, 0xe0def4, 0xeb6f92, 0xebbcba, 0xf6c177))
cast('everforest', CACTUS, [0xd699b6, 0x7fbbb3, 0xa7c080, 0xe69875], scheme(0x2d353b, 0xd3c6aa, 0xe67e80, 0xe69875, 0xdbbc7f))
cast('kanagawa', WHALE, [0x957fb8, 0x7e9cd8, 0x98bb6c, 0xffa066], { ...scheme(0x1f1f28, 0xdcd7ba, 0xd27e99, 0xffa066, 0xe6c384), e: 0x7fb4ca })
cast('ayu', FOX, [0xd2a6ff, 0x59c2ff, 0xaad94c, 0xff8f40], scheme(0x0b0e14, 0xbfbdb6, 0xf07178, 0xff8f40, 0xe6b450))
cast('material', OWL, [0xc792ea, 0x82aaff, 0xc3e88d, 0xf78c6c], { ...scheme(0x263238, 0xeeffff, 0xf07178, 0xffcb6b, 0xffcb6b), j: 0x546e7a })
cast('synthwave-84', HEART, [0xff7edb, 0x36f9f6, 0x72f1b8, 0xfede5d], scheme(0x262335, 0xffffff, 0xfe4450, 0xf97e72, 0xfede5d))

/** Every theme name `theme` in config.json takes. */
export const THEME_NAMES = Object.keys(THEMES)
const known = (name: string | null) => (name && Object.hasOwn(THEMES, name) ? name : 'disco')
/** The band's frames for this session's theme and role. */
export function framesOf(name: string | null, who: string | null): Record<Mood, string[][]> {
  const set = THEMES[known(name)]!
  return set[who ?? ''] ?? set.orchestrator!
}
/** The colors of the theme's pixel letters. */
export function paletteOf(name: string | null): Record<string, number> {
  return PAL[known(name)] ?? PALETTE
}

function cells(pixels: string[], pal: Record<string, number>): string {
  const words = new Uint32Array(W * 4 * 3)
  for (let row = 0; row < 4; row++) {
    for (let col = 0; col < W; col++) {
      const top = pal[pixels[row * 2]?.[col] ?? '.']
      const bottom = pal[pixels[row * 2 + 1]?.[col] ?? '.']
      const at = (row * W + col) * 3
      if (top === undefined && bottom === undefined) words.set([0x20, DEFAULT, DEFAULT], at)
      else if (top === undefined) words.set([0x2584, bottom ?? DEFAULT, DEFAULT], at) // ▄
      else words.set([0x2580, top, bottom ?? DEFAULT], at) // ▀
    }
  }
  return (new Uint8Array(words.buffer) as any).toBase64()
}

function frameCells(m: Mood, i: number): string {
  const list = framesOf(theme, role)[m]
  return cells(list[Math.floor(i) % list.length] ?? [], paletteOf(theme))
}

/** The band's mood from the board, or for a worker or reviewer from its own turn. */
function moodOf(tree: BoardRepo[], working: boolean): Mood {
  const jobs = tree.flatMap(r => r.jobs)
  if (jobs.some(j => j.pending)) return 'wave'
  const all = nodes(tree)
  // a held job's dead agents and PM are expected: they are not stuck
  const open = jobs.filter(j => j.phase !== 'done' && j.phase !== 'failed' && !j.held)
  if (open.some(j => j.agents.some(n => n.status === 'dead')) || tree.some(r => r.pm?.status === 'dead' && r.jobs.some(j => open.includes(j)))) {
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
    // herdr pane, else Orca terminal, else this Claude session (bg children)
    // the process env has no session id (seen live): ask the session itself
    pane = (await $.env.get('HERDR_PANE_ID')) || (await $.env.get('ORCA_TERMINAL_HANDLE'))
      || `session:${await $.session.id()}`
    if (!pane) return r
    await $.command.register({ name: 'oke-board', description: 'Show the OKe agents and their boards' })
    // A process that died during a backoff leaves its marker; this one starts clean.
    if (await okeRunning($)) await clearRetry($)
    if (role === null) {
      // a hot reload cleared the module state: the guards hold again before the tick answers
      const was = await read($, who)
      if (was) {
        role = was.role
        folder = was.folder
        named = was.named  // a role oke recorded clears again when oke stops naming it
      }
    }
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
      if (e.tool === 'Bash' && (role === 'orchestrator' || role === 'pm' || role === 'worker' || role === 'reviewer')) {
        const denied = gitDenied(String(e.command ?? ''), role)
        if (denied) return { deny: denied }
      }
      if (role === 'reviewer') {
        const denied = reviewerDenied(e.tool, e, folder)
        if (denied) return { deny: denied }
      }
      if (role === 'worker') {
        const denied = workerDenied(e.tool, e, folder)
        if (denied) return { deny: denied }
      }
      if (role === 'orchestrator') {
        let root: string | null = null  // asked on each write: /cd or a worktree move changes it
        if (['Edit', 'Write', 'NotebookEdit'].includes(e.tool)) {
          try {
            root = await $.session.root()
          } catch {
            // no root: its memory is refused too, never a foreign one allowed
          }
        }
        const denied = orchestratorDenied(e.tool, e, { home, wiki, claude: claudeDir, root })
        if (denied) return { deny: denied }
      }
      const ran: any = await next(e)
      // /oke:start in a session older than a minute: an unknown pane ticks only every 60 s,
      // so look right after its oke call instead of leaving the band dark for up to a minute
      if (role === null && e.tool === 'Bash' && String(e.command ?? '').includes('oke.py')) void tick($)
      const tool: string = e.tool
      // a subagent's tasks are its own: only the agent's main loop fills its board
      if (!pane || !TASK_TOOLS.includes(tool) || e.agentId || ran.deny !== undefined || ran.isError) return ran
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
          {job.held
            ? <Text dimColor>{' on hold'}</Text>
            : <Text color={job.pending ? 'yellow' : 'cyan'}>{` ${job.pending ? 'waiting' : STEPS[step(job)] ?? 'done'}`}</Text>}
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
    if (e.agent_id) return next(e)  // a subagent's error is not the main turn's
    lastError = String(e.error ?? '')
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    // The person typed during a backoff: their turn replaces the retry.
    pending?.cancel()
    pending = null
    // Orca runs a new agent's first turn before spawn records it: its guardrails hold from the first tool call
    const said = role === null ? promptRole(String((e as any).text ?? '')) : null
    if (said) {
      role = said.role
      folder = said.folder
      named = true
      await update($, who, () => ({ role: said.role, folder: said.folder, named: true }))
    }
    // a new assignment's seq for the status line; workers and reviewers do not tick otherwise
    if (role === 'worker' || role === 'reviewer') void tick($)
    return next(e)
  })

  on('turn.complete', async ($, e: any, next) => {
    await logUsage($, e)  // subagents' turns too: they are part of the agent's cost
    // a subagent's turn ends with its own turn.complete: retry and compaction are the main loop's
    if (e.agentId !== undefined) return next(e)
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
    const todoRows = (list: Todo[], now: string[] = []) => TODO_ORDER.flatMap(status =>
      list.filter(t => (TODO[t.status] ? t.status : 'pending') === status).flatMap(t => {
        const [tm, tc] = TODO[status]!
        return [
          <Text color={tc} dimColor={status === 'completed'} bold={status === 'in_progress'}>
            {`${tm} ${t.content}`}
          </Text>,
          ...(status === 'in_progress' ? now : []).map((line, i) => (
            <Text color={INK.cyan} italic>{`  ${i ? '  ' : '└ '}${line}`}</Text>
          )),
        ]
      }))
    // a label chip on its own color, then the line: scanned by color, read in plain text
    const chip = (label: string, color: string) => <Text backgroundColor={color} color={INK.ink} bold>{` ${label} `}</Text>
    const progress = (list: Todo[]) => {
      const done = list.filter(t => t.status === 'completed').length
      const bar = '▰'.repeat(done) + '▱'.repeat(Math.max(0, list.length - done))
      return <Text color={INK.green}>{` ${bar.length <= 12 ? bar : ''} ${done}/${list.length}`}</Text>
    }

    const toggle = (agent: string) =>
      void update($, expanded, list => (list.includes(agent) ? list.filter(a => a !== agent) : [...list, agent]))
    // herdr focuses the pane; a background session can only be attached by the user
    const focus = async (agent: string) => {
      const out = await oke($, ['attach', agent])
      if (out?.hint) $.ui.status(`OKe: ${out.hint}`)
    }

    // One row per agent, the row itself toggles it open: a second row of buttons per
    // agent doubled the height, and buttons beside text squeezed it away (seen live).
    const agentBlock = (n: BoardNode, name: string) => {
      const t = times[n.agent]
      const [mark, word, color] = STATUS[n.status] ?? ['·', n.status, undefined]
      const extra = [n.report ? `report ${n.report}` : '', n.overdue ? `over expected ${n.overdue}` : '',
        n.revives ? `revived ${n.revives}x` : ''].filter(Boolean)
      const isOpen = open.includes(n.agent)
      const notes = NOTES.flatMap(([title, mark, label, noteColor]) =>
        section(n.board, title, 5).map(line => (
          <Text>{chip(`${mark} ${label}`, noteColor)}<Text>{` ${line}`}</Text></Text>
        )))
      return (
        <Box flexDirection="column" marginTop={1}>
          <Button key={`open-${n.agent}`} plain onPress={() => toggle(n.agent)}>
            <Text color={color} dimColor={n.status === 'idle'}>{`${mark} `}</Text>
            <Text bold color={INK.white}>{name}</Text>
            <Text color={color} dimColor={n.status === 'idle'}>{` ${word}`}</Text>
            <Text dimColor>{`${t ? ` ${elapsed(now - t.at)}` : ''}${extra.length ? ` · ${extra.join(', ')}` : ''}`}</Text>
            <Text dimColor>{isOpen ? '  ▾' : '  ▸'}</Text>
          </Button>
          <Box flexDirection="column" paddingLeft={2}>
            {todoRows(n.todos ?? [], section(n.board, '지금', 3))}
            {notes}
            {isOpen
              ? [
                  <Box flexDirection="row" marginTop={1}>
                    <Button key={`go-${n.agent}`} label="go" onPress={() => void focus(n.agent)} />
                  </Box>,
                  <Box flexDirection="column" borderStyle="single" borderColor={INK.dim} paddingX={1}>
                    {(n.board || '(no board.md)').split('\n').map(line => (
                      <Text dimColor={!line.startsWith('##')} color={line.startsWith('##') ? INK.lavender : undefined} bold={line.startsWith('##')}>
                        {line || ' '}
                      </Text>
                    ))}
                  </Box>,
                ]
              : []}
          </Box>
        </Box>
      )
    }

    const cards = []
    const waiting = tree.flatMap(repo => repo.jobs.filter(j => j.pending))
    if (waiting.length) {
      cards.push(
        <Box flexDirection="column" borderStyle="round" borderColor={INK.yellow} paddingX={1}>
          <Text bold color={INK.yellow}>{`✦ waiting on you ${waiting.length}`}</Text>
          {waiting.map(j => <Text>{`▸ ${j.job}: ${PENDING[j.pending as string] ?? j.pending}`}</Text>)}
        </Box>,
      )
    }
    if (mine.length) {
      const done = mine.filter(t => t.status === 'completed').length
      cards.push(
        <Box flexDirection="column" borderStyle="round" borderColor={INK.lavender} paddingX={1} marginTop={waiting.length ? 1 : 0}>
          <Text><Text bold color={INK.lavender}>{`OKe todo ${done}/${mine.length}`}</Text>{progress(mine)}</Text>
          {todoRows(mine)}
        </Box>,
      )
    }
    for (const repo of tree) {
      const finished = repo.jobs.filter(j => j.phase === 'done' || j.phase === 'failed')
      cards.push(
        <Box flexDirection="column" marginTop={1}>
          <Text bold color={INK.pink}>{`▍${repo.repo.replace(/-[0-9a-f]{6}$/, '')}`}</Text>
          {repo.pm ? <Box flexDirection="column" paddingLeft={1}>{agentBlock(repo.pm, 'PM')}</Box> : []}
        </Box>,
      )
      for (const job of repo.jobs) {
        if (finished.includes(job)) continue
        const [label, labelColor] = LEDGER[job.merge] ?? ['', undefined]
        const at = step(job)
        const peers = (job.peer_wait ?? []).map(r => r.replace(/-[0-9a-f]{6}$/, ''))
        const wait = [peers.length ? `PM ${peers.join(', ')}` : '', (job.after_wait ?? []).join(', ')]
          .filter(Boolean).join('; ')
        const hurt = !job.held && job.agents.some(n => n.status === 'dead')
        cards.push(
          <Box flexDirection="column" borderStyle="round" borderColor={job.pending ? INK.yellow : hurt ? INK.red : INK.lavender} paddingX={1} marginTop={1}>
            <Text>
              <Text bold color={INK.white}>{job.job}</Text>
              {job.size ? <Text color={INK.dim}>{` · ${job.size}`}</Text> : ''}
              {label ? <Text color={labelColor}>{`  ${label}`}</Text> : ''}
              {job.held ? <Text color={INK.dim}>{'  ⏸ on hold'}</Text> : ''}
            </Text>
            <Text>
              {STEPS.flatMap((s, i) => [
                i ? <Text color={INK.dim}>{' › '}</Text> : '',
                <Text
                  color={i === at ? INK.ink : i < at ? INK.green : undefined}
                  backgroundColor={i === at ? INK.cyan : undefined}
                  dimColor={i > at}
                  bold={i === at}
                >
                  {i === at ? ` ${s} ` : s}
                </Text>,
                i === at && wait ? <Text color={INK.yellow} dimColor>{` (wait ${wait})`}</Text> : '',
              ])}
            </Text>
            {job.agents.map(n => agentBlock(n, n.agent.startsWith(`${job.job}-`) ? n.agent.slice(job.job.length + 1) : n.agent))}
          </Box>,
        )
      }
      for (const job of finished) {
        cards.push(
          <Text dimColor color={job.phase === 'failed' ? INK.red : undefined}>
            {`  ${job.phase === 'done' ? '✓' : '✗'} ${job.job} ${job.phase === 'done' ? 'done' : 'failed'}`}
          </Text>,
        )
      }
    }
    return <Box flexDirection="column">{cards}</Box>
  })
}
