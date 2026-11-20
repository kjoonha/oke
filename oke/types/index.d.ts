export type BoardNode = {
  agent: string
  role: string
  status: string
  pane: string | null
  report: string | null
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
  /** The ledger: none, unverified, verified or rolled_back. */
  merge: string
  /** The job size the PM chose: small, normal or large (or research). */
  size?: string
  /** What the job waits on the user for (`oke pending` kind), null when nothing or deferred. */
  pending: string | null
  /** The user put it on hold (`oke job hold`): nothing runs for it until it is back. */
  held?: boolean
  /** Repositories whose PM still owes this job an answer (`oke peer ask`). */
  peer_wait?: string[]
  /** Jobs that must merge and verify before this one may merge (plan `after:`). */
  after_wait?: string[]
}

export type BoardRepo = { repo: string; key: string; pm: BoardNode | null; jobs: BoardJob[] }

/** When an agent's herdr status last changed, for the elapsed time shown beside it. */
export type Since = { status: string; at: number }

declare module 'claude-code' {
  interface PluginState {
    oke: {
      board: Shaped<BoardRepo[]>
      expanded: Shaped<string[]>
      since: Shaped<Record<string, Since>>
      todos: Shaped<Todo[]>
      /** Whether the band's folded job rows are open. */
      bandOpen: Shaped<boolean>
      /** The `job:seq` assignments this agent had answered at its last task boundary. */
      answered: Shaped<string[]>
      /** This agent's role and folder, kept across a hot reload so the guards hold before the first tick. */
      who: Shaped<{ role: string, folder: string | null } | null>
    }
  }
}
