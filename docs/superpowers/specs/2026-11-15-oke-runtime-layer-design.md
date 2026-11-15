# OKe runtime layer: herdr and background sessions behind one interface

Date: 2026-11-15. Status: design, not built. Replaces slice 6 ("no-herdr mode").

## Goal

OKe runs without herdr: in a plain terminal, in Orca, anywhere Claude Code runs.
Without herdr, children are Claude Code background sessions (`claude --bg`).
The user chose `--bg` over agent teams (teams cannot nest, cannot resume, and
inherit the lead's permissions).

The job logic (jobs, phases, ledger, merge gate, interview, pending queue) must not
know which runtime runs the agents. Today `oke.py` calls herdr from 28 places and
the skills name herdr commands 22 times. This design moves every runtime call
behind one interface.

## Measured facts (2026-11-15, Windows, Claude Code 2.1.325)

| Need | herdr | `claude --bg` |
|---|---|---|
| Start an agent with flags and a first prompt | `pane split` + `agent start` + `agent prompt` | `claude --bg --name <n> <flags> "<prompt>"` (cwd = the process cwd). Refused in an untrusted folder |
| List agents and their state | `agent list` | `claude agents --json --all`: `id`, `sessionId`, `name`, `pid`, `state` (working, done, blocked, failed, stopped), `status` |
| Send a prompt to a running or idle agent | `agent prompt` (from any process) | `SendMessage` from another Claude session: an idle session wakes up and keeps its context. No documented way from a plain script: the inbox pipe takes `{"type":"auth","token":...}` but the message format is not documented |
| Revive after a crash | new pane + `agent start --resume <session>` | `claude respawn <id>`: same id, same session, context kept. The supervisor also restarts a crashed process by itself |
| Answer a dialog | `agent send-keys` | none from outside: the human runs `claude attach <id>` |
| Read the screen | `agent read` | `claude logs <id>` (raw terminal output) or the transcript jsonl |
| Human looks or types | focus the pane | `claude attach <id>` |
| Close | `pane close` | `claude stop <id>` + `claude rm <id>` |
| Worktree | `herdr worktree create` | `git worktree add` |

`claude --resume <session> --bg` forks the conversation into a new session id; OKe
does not use it.

## Layers

```
skills (orchestrator, pm, worker, reviewer)   talk only to oke.py commands
        |
oke.py CLI            argument parsing, JSON output              (thin)
        |
domain                jobs, phases, ledger, merge, interview,    (no runtime calls)
                      pending, inbox, revive policy
        |
runtime interface     Runtime: start, agents, prompt, read,      (runtime.py)
                      revive, close, attach_hint, worktree, whoami
        |                         |
HerdrRuntime                 BgRuntime                            (runtime_herdr.py,
herdr CLI                    claude CLI, git                        runtime_bg.py)

display               board data (domain) -> mod pane/band (terminal),
                      HTML visual (Orca chat), text (others)
```

Rules:
- Only `runtime_*.py` run `herdr` or `claude`. A test of the domain uses a fake
  `Runtime`, not a fake herdr binary.
- The domain stores agents by OKe name and a runtime handle (`{"runtime": "herdr",
  "pane": ..., "session": ...}` or `{"runtime": "bg", "id": ..., "session": ...}`).
  Old state.json records without `runtime` are read as herdr.
- One runtime per OKe home, chosen at `/oke:start`: herdr when the orchestrator runs
  in a herdr pane (`HERDR_ENV=1`), else bg. Stored in state.json; a mixed run is not
  supported.
- Python 3.6 standard library only, as before.

## The interface

```python
class Runtime:
    name = "herdr" | "bg"
    def check(self) -> dict            # usable? missing commands, why not
    def whoami(self) -> Handle | None  # the calling session (herdr pane, or CLAUDE_CODE_SESSION_ID)
    def agents(self) -> list[Agent]    # name, handle, status: working|idle|blocked|dead
    def start(self, name, cwd, argv, prompt, near=None) -> Handle
    def prompt(self, name, text) -> Delivery   # "sent", or "ask_caller" with the text
    def read(self, name) -> str
    def revive(self, record, argv, prompt) -> Handle
    def close(self, name) -> None
    def attach_hint(self, name) -> str  # "herdr agent focus x" / "claude attach <id>"
    def worktree(self, repo, branch, label) -> (path, near)
```

Status mapping for bg: `working` -> working; `done` and status idle -> idle;
`blocked` -> blocked; `failed`, `stopped`, absent -> dead.

### Delivery in bg

A script cannot prompt a bg session (no documented wire format). Every OKe message
already ends up in a Claude session that waits on `oke wait`:

- `oke wait` and `oke send` return `{"deliver": {"to": <name>, "text": ...}}`
  when the runtime answers `ask_caller`.
- The skills say: on a `deliver` line, call `SendMessage` with that name and text,
  then run the wait again.
- Children start with `--settings` `crossSessionInbound: "accept"`, so a message is
  never held for approval.

When Claude Code documents the inbox format, `BgRuntime.prompt` sends it directly
and the skills' rule simply never fires.

### Dialogs in bg

A bg child cannot be answered from outside. OKe avoids dialogs instead:
- the workspace trust dialog: bg refuses an untrusted folder. Open question 1.
- permission dialogs: the children's permission flags already decide everything
  except in `conservative` autonomy. In bg, `conservative` uses `dontAsk` with the
  same allow rules (denied instead of asked), and the report says what was denied.
- a `blocked` state that still happens is reported to the user with
  `claude attach <id>`.

## Watching

The mod's tick keeps working: mod hooks run in every host, the Orca chat included
(only drawing is missing there). Its key changes from `HERDR_PANE_ID` to the
runtime's `whoami`. Revival calls `Runtime.revive`; the "three deaths without a new
report" rule stays in the domain.

## Display by host

| Host | Board | Animation | Todos |
|---|---|---|---|
| Terminal (herdr pane or not) | mod pane `/oke-board` | band conductor | in the pane |
| Orca chat (`ORCA_CHAT_SCRATCH_DIR` set) | `oke board --html` written there, shown with `::orca-visual` when the user asks or a job changes phase | none | in the HTML |
| Other SDK hosts | `oke board --text` in the orchestrator's reply | none | in the text |

The board data stays one function in the domain; only the renderers differ.

## Order of work

1. Extract `runtime.py` + `HerdrRuntime`, move all 28 herdr calls there, no
   behaviour change. The existing 300 tests must pass unchanged except their fake
   herdr setup. (safe-refactor)
2. Skills: replace herdr commands with `oke` commands (`oke worktree`, `oke attach`,
   `oke read`, `oke close`).
3. `BgRuntime` with a fake `claude` binary in tests, like `bin/fake/herdr.py`.
4. Delivery rule in the skills; `crossSessionInbound` for children.
5. Mod: identity from the runtime; `oke board --text|--html`.
6. Live run without herdr in this Orca terminal.

## Open questions

1. Trust: `claude --bg` refuses an untrusted folder, and every new worktree is
   untrusted. Options: (a) OKe marks the worktree trusted in `~/.claude.json`
   (writes the user's config), (b) worktrees under a folder the user trusted once,
   if trust covers subfolders (to measure), (c) start the child in the trusted repo
   root and `cd` to the worktree in its first prompt.
2. A bg session nobody attached stops after about an hour idle unless pinned. A PM
   waits for hours. Measure whether a session running a background `oke wait`
   counts as idle, and whether `--settings` can pin it.
3. RHEL8 at work: `claude --bg` needs the supervisor; not tried on Linux.
