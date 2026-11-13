export type BoardNode = {
  agent: string
  role: string
  status: string
  pane: string | null
  report: string | null
  board: string
  revives: number
}

export type BoardJob = { job: string; phase: string; agents: BoardNode[] }

export type BoardRepo = { repo: string; key: string; pm: BoardNode | null; jobs: BoardJob[] }

/** When an agent's herdr status last changed, for the elapsed time shown beside it. */
export type Since = { status: string; at: number }

declare module 'claude-code' {
  interface PluginState {
    oke: {
      board: Shaped<BoardRepo[]>
      expanded: Shaped<string[]>
      since: Shaped<Record<string, Since>>
    }
  }
}
