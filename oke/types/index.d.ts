export type BoardNode = {
  agent: string
  role: string
  status: string
  pane: string | null
  report: string | null
  /** The `expect:` of a waiting report whose wait ran past 1.5x it; null otherwise. */
  overdue?: string | null
  board: string
  revives: number
  /** US dollars the agent's session has cost, from its usage.json; null before its first turn. */
  cost?: number | null
  /** The agent's own task list (TaskCreate/TaskUpdate/TodoWrite), from its todos.json. */
  todos: Todo[]
}

/** One task: pending, in_progress or completed. `id` is TaskCreate's, absent for TodoWrite. */
export type Todo = { id?: string; content: string; status: string }

export type BoardJob = {
  job: string
  phase: string
  agents: BoardNode[]
  /** The ledger: none or delivered (unreadable when its key is lost). */
  deliver: string
  /** The job size the PM chose: small, normal or large. */
  size?: string
  /** The `## User's words` of the job's intake.md, on one line. */
  words?: string
  /** Where the job is, in plain words: planning, awaiting approval, working, ... */
  step?: string
  /** The steps still to come, in plain words. */
  left?: string[]
  /** What the job waits on the user for (`oke pending` kind), null when nothing or deferred. */
  pending: string | null
  /** The user put it on hold (`oke job hold`): nothing runs for it until it is back. */
  held?: boolean
  subagent?: boolean  // the PM waits on a background subagent for it (no pane of its own)
  /** The folder name of the job's path, or `path?` while its PM has not set it. */
  path?: string
  /** PMs that still owe this job an answer (`oke peer ask`). */
  peer_wait?: string[]
  /** When its current step began (epoch seconds): the board shows how long it runs. */
  since?: number | null
  /** The user said not to ask again for this job (`oke job auto`). */
  auto?: boolean
  /** Its pending item is taken without asking (`oke job auto`): not the person's to do. */
  auto_pending?: boolean
  /** Its PM is blocked by an OKe rule (`rule:` in its report), not by a dialog. */
  oke_rule?: boolean
}

/** A finished job for the board's Done section (`oke tick` `recent`, newest first). */
export type RecentJob = {
  job: string
  /** The PM it was a job of (its board section shows it). */
  pm: string | null
  words: string
  phase: string
  /** It ended with an answer, changing nothing. */
  answer: boolean
  /** The first line of its report's Summary. */
  result: string
  /** The branch it handed over for the user to merge, or null. */
  branch: string | null
}

/** One PM: its name, its node and its jobs. */
export type BoardPm = { name: string; key: string; pm: BoardNode | null; jobs: BoardJob[] }

/** When an agent's herdr status last changed, for the elapsed time shown beside it. */
export type Since = { status: string; at: number }

declare module 'claude-code' {
  interface PluginState {
    oke: {
      board: Shaped<BoardRepo[]>
      recent: Shaped<RecentJob[]>
      expanded: Shaped<string[]>
      since: Shaped<Record<string, Since>>
      todos: Shaped<Todo[]>
      /** Whether the band's folded job rows are open. */
      bandOpen: Shaped<boolean>
      /** The `job:seq` assignments this agent had answered at its last task boundary. */
      answered: Shaped<string[]>
      /** This agent's role and folder, kept across a hot reload so the guards hold before the first tick. */
      who: Shaped<{ role: string, folder: string | null, named: boolean } | null>
    }
  }
}
