<h1 align="center">OKe</h1>

<p align="center">
  <b>English</b> | <a href="README.ko.md">한국어</a>
</p>

<p align="center">
  <a href="https://github.com/kjoonha/oke/actions/workflows/ci.yml"><img src="https://github.com/kjoonha/oke/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fkjoonha%2Foke%2Fmain%2Foke%2F.claude-plugin%2Fplugin.json&query=%24.version&label=version&color=7a4fd1" alt="version">
  <a href="https://github.com/kjoonha/oke/stargazers"><img src="https://img.shields.io/github/stars/kjoonha/oke?style=flat&logo=github" alt="GitHub stars"></a>
  <a href="https://github.com/kjoonha/oke/commits/main"><img src="https://img.shields.io/github/last-commit/kjoonha/oke" alt="last commit"></a>
  <img src="https://img.shields.io/badge/Claude%20Code-plugin-d97757" alt="Claude Code plugin">
  <img src="https://img.shields.io/badge/python-3.6%2B%20stdlib%20only-3776ab?logo=python&logoColor=white" alt="Python 3.6+, stdlib only">
  <img src="https://img.shields.io/badge/platform-Linux%20%7C%20Windows-555" alt="Linux | Windows">
</p>

<p align="center">
  <img src="docs/oke-org.svg" alt="Org chart: you talk to the orchestrator; it runs one PM per repository; each PM runs a worker and a reviewer. Assignments flow down, reports flow up.">
</p>

<p align="center">
  <img src="docs/oke-band.svg" alt="The OKe characters at work: orchestrator, PM, worker and reviewer in the disco, slime and mushroom themes">
</p>

<p align="center">
  A Claude Code plugin. You talk to one orchestrator.<br>
  It gives each repository a PM, and each PM runs a worker and a reviewer in their own panes.<br>
  Questions and approvals come back to you as popups in the orchestrator's window.
</p>

## How it is organised

- **Orchestrator**: the Claude Code session you talk to. It interviews you about a job, asks you to approve plans and merges, and keeps the queue.
- **PM**: one per repository. It plans the job, starts the worker and the reviewer, and merges and verifies only what the review passed.
- **Worker / reviewer**: each runs in its own pane and git worktree. They write reports. Only the PM merges, through `oke job merge`, and only what the review passed. A hook blocks git commands that publish or destroy work (`push`, `reset --hard`, forced `clean`, `checkout` and branch deletes) in every OKe session.
- **Checks**: the plan names numbered acceptance criteria (`## 수용 기준`) and one check command (`## 검증 명령`, in backticks). Before any work, `oke baseline <job>` runs the command on the untouched checkout and saves what already fails. The worker checks its work with `oke check <job>`: OKe runs the command, and only failure lines the baseline did not have count as new failures. OKe writes the result into the job's ledger and signs every row with a key (`$OKE_HOME/ledger.key`) no agent may read; a row without a valid signature, typed or copied by hand, does not count. `oke job merge` merges only when the review names the plan's current `criteria_rev` (a hash of the criteria and the command) and the ledger has a passing `oke check` on the reviewed commit. After the merge, `oke job verify <job>` records a pass without running when the merged code is the code the check passed on; when other merges changed main since the check, it lists the files they changed and the PM decides: `--skip "<why>"` when those files cannot change the result (documentation, say), recorded in the ledger with the reason and the files, or `--run` to run the command on main, which undoes the merge on a new failure.

Agents talk through files under `$OKE_HOME` (default `~/.oke`), so a crash loses no state. After a terminal, Claude Code or the whole app dies, `/oke:start` finds the open jobs and asks, per job, whether to **resume**, **hold** (set it aside, keep its worktree and branch) or **archive** it.

## The board

`/oke-board` opens a live board beside the orchestrator: one line per PM, one card per job with its phase, and its worker and reviewer.

```
╭─────────────────────────────────────────────────────────────╮
│ ▍shop5                                                      │
│                                                             │
│  · PM done 0m  ▸                                            │
│                                                             │
│ ╭─────────────────────────────────────────────────────────╮ │
│ │ textkit · large  ⏸ on hold                              │ │
│ │ plan › approve ›  work  › review › merge › verify       │ │
│ │                                                         │ │
│ │ – w1 off 0m  ▸                                          │ │
│ ╰─────────────────────────────────────────────────────────╯ │
╰─────────────────────────────────────────────────────────────╯
```

## Requirements

- Claude Code 2.1.322 or later (a version with mod support)
- Python 3.6 or later (standard library only) and git
- One place to run the agents:
  - herdr 0.9.5 or later (agents run in herdr panes), or
  - Orca (agents run as Orca orchestration workers, each in its own Orca terminal; tested with 1.4.261), or
  - neither: agents run as background `claude --bg` sessions.

## Install (online)

```
claude plugin marketplace add kjoonha/oke
claude plugin install oke@oke
```

Update: `claude plugin marketplace update oke`, then `claude plugin update oke@oke`.

## Install (offline, e.g. an air-gapped cluster)

On a machine with internet, make one bundle. This folder is the whole install (no packages, no daemon, no database). `core.autocrlf=false` keeps LF line endings even when you build it on Windows.

```
git -c core.autocrlf=false archive --format=tar.gz -o oke.tar.gz HEAD .claude-plugin oke
```

On the target machine (Linux; tcsh works too):

```
mkdir -p ~/.claude/plugins/marketplaces/oke && tar xzf oke.tar.gz -C ~/.claude/plugins/marketplaces/oke
claude plugin marketplace add ~/.claude/plugins/marketplaces/oke
claude plugin install oke@oke
```

This is the same place a marketplace from GitHub lives. The plugin is read from this folder, so do not delete it (`claude plugin marketplace remove oke` removes it with the marketplace). For a new version, unpack it into the same folder again and run `claude plugin marketplace update oke`.

## Use

Start Claude Code in a herdr pane, an Orca terminal or a plain terminal, and run `/oke:start`. That session becomes the orchestrator. Tell it what you want done. If the session was cut off, run `/oke:resume`.

In Orca:

- OKe runs only in an Orca terminal tab. `/oke:start` in an Orca chat refuses ("OKe runs in an Orca terminal, not a chat"); a chat draws no mod pane.
- Orca's Agent Orchestration setting must be on, and Orca's default for new agent tabs must be CLI, not Chat. OKe starts each PM, worker and reviewer as an Orca worker in a new terminal tab (titled `OKe PM: <repo>`, `OKe worker 1: <job>`, ...); if Orca starts it as a chat instead, OKe stops it right after it started and says to change that default (or, if it could not stop it, which one to close in Orca). The first PM start is where you meet this.
- Orca starts the agents itself, so OKe's launch flags do not reach them: install the OKe plugin in Claude Code, and `lean_launch` has no effect there.
- A closed agent terminal is reopened by Orca with the next message to that agent. An agent whose process died reads `gone`, and the orchestrator asks whether to resume it (resume, hold or cancel; with autonomy `full` see `autonomy` below). A resume starts a new Orca worker that reads the dead one's transcript, and gets the messages queued for it meanwhile.
- If Orca sent an agent its task but never saw claude start (`start_unknown`), reading that agent shows a note instead of its screen: revive it (`oke close <handle>`, then `oke revive <name> --reset`) or wait; it turns back to working once Orca sees its turn.

`oke status` (and `oke board`) work from any terminal: they read the runtime the orchestrator chose. Outside that runtime's terminals (for example a plain shell while OKe runs in herdr or Orca) they only report; nothing can be started from there.

Things you can say along the way:

- `hold <job>`: set a job aside. Its agents are closed; its worktree and branch stay.
- `continue <job>`: bring a held job back; the PM restarts what it needs.
- `cancel <job>`: drop it.

## Settings: `~/.oke/config.json`

Optional. Only the values you set override the defaults.

OKe keeps its files in `~/.oke`. To use another folder, set `OKE_HOME` in the `env` of `~/.claude/settings.json` (`"env": {"OKE_HOME": "D:/oke"}`): every agent OKe starts gets it there, including those Orca starts. A variable set only in your shell does not reach Orca's agents.

```json
{
  "autonomy": "balanced",
  "wiki_path": "~/.oke/wiki",
  "models": {"pm": "opus", "worker": "opus", "reviewer": "opus"},
  "agents": {"worker": "claude", "reviewer": "claude"},
  "interview_threshold": 0.2,
  "interview_max_rounds": 10,
  "lean_launch": true,
  "effort": {"pm": "high", "worker": "medium", "reviewer": "high"},
  "theme": "disco"
}
```

- `theme`: the characters in the band above the prompt. Each role has its own shape or colour.
  - `disco` (default): blobs in headphones. Orchestrator purple, PM blue, worker green, reviewer orange.
  - `slime`: bouncing slimes. Orchestrator green, PM blue, worker pink, reviewer yellow.
  - `mushroom`: bouncing mushrooms. Orchestrator purple, PM blue, worker orange, reviewer green.
  - `cute-dog` (floppy-eared puppy) and `cute-cat` (kitten): tails wag, ears twitch, they blink and curl up to sleep. Each role has its own coat.
  - In the disco colours (orchestrator purple, PM blue, worker green, reviewer orange): `ghost`, `robot`, `frog`, `penguin`, `bunny`, `bear`, `duck`, `octopus`, `alien`, `cactus`, `jellyfish`, `fox`, `panda`, `hamster`, `owl`, `whale`, `dino`, `star`, `pixel-heart`, `coffee`, `onigiri`, `axolotl`, `bee`, `snail`, `chick`, `sakura`.
  - `claude`: square critters with arm nubs and four legs that throw their arms up while working. Orchestrator Claude orange, PM blue, worker green, reviewer cream.
  - Palette variants: `pastel` (bunnies), `candy` (hearts), `ocean` (whales), `autumn` (foxes), `midnight` (owls).
  - Colour schemes, with role colours from the scheme's own palette: `tokyo-night`, `catppuccin` (mocha), `github` (dark), `dracula`, `nord`, `gruvbox`, `solarized`, `one-dark`, `monokai`, `rose-pine`, `everforest`, `kanagawa`, `ayu`, `material`, `synthwave-84`.
  - An unknown name falls back to `disco`.
- `autonomy`: `conservative` (the worker asks before every command), `balanced` (default) or `full`. With `full` the orchestrator approves plans and merges without asking, and a PM or child that died (`gone`) is resumed once without asking (`oke revive <name> --auto`); you are told in one line (a PM notes a resumed child on its board). If it dies again before it reported anything, you are asked (resume, hold or cancel). Held jobs are never resumed. `conservative` and `balanced` always ask.
- `lean_launch`: start PMs, workers and reviewers without your plugins, hooks and MCP servers (`--setting-sources project,local --strict-mcp-config`). Only the herdr hooks, your `PreToolUse` hooks, `permissions.deny` and `env` from your `settings.json` are copied into the agent's `lean-settings.json` (readable only by you; `job archive` deletes it). `permissions.ask` is not copied. This cuts the starting context from about 76k to 40k tokens. Each role also gets only its core tools (`--tools`, about 15k tokens at start). For each job, the PM turns on extra tools with the assignment header's `tools:`, `mcp:` and `plugins:` lines (`oke.py extras` lists them). Set it to `false` if the agents need all your personal settings.
- `models`: the model of each role (`pm`, `worker`, `reviewer`). Default `opus`.
- `effort`: the effort level per role (`pm`, `worker`, `reviewer`): `low`, `medium`, `high`, `xhigh` or `max`. With `lean_launch`, a role left out runs at Claude Code's default effort, not at the `effortLevel` of your own settings; without it, children read your settings.
- `merge_ignore_untracked`: untracked paths in the repository that never block a merge, such as another tool's state folder. Default `[".omc/"]`. Python caches never block either.
- `interview_threshold`, `interview_max_rounds`: the interview the orchestrator runs when it takes a job. It scores the goal, constraints and success criteria from 0 to 1 and asks one question at a time about the vaguest part, until the ambiguity (`1 - 0.4 goal - 0.3 constraints - 0.3 criteria`) is at or below the threshold. It also stops when you pick "plan now" or it reaches the round limit.

After installing, follow the checks in `docs/work-checklist.md`.

## Token use

Each agent's mod adds one line per turn (its subagents' turns included) to `usage.csv` in the agent's folder: time, model, input, output, cache read and cache write tokens. The orchestrator writes `$OKE_HOME/usage.csv`. `python oke/bin/oke.py cost [job]` adds them up per job (archived jobs too), per repository PM and for the orchestrator.

## Development

```
cd oke/bin && python -m unittest discover -p "test_*.py"   # CLI and runtime tests
claude plugin test oke                                     # mod (band, board, git guard) tests
```
