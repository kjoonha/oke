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
  <img src="docs/oke-org.svg" alt="Org chart: you talk to the orchestrator; it hands each job to the PM you pick; each PM runs a worker and a reviewer. Assignments flow down, reports flow up.">
</p>

<p align="center">
  <img src="docs/oke-band.svg" alt="The OKe characters at work: orchestrator, PM, worker and reviewer in the disco, slime and mushroom themes">
</p>

<p align="center">
  A Claude Code plugin. You talk to one orchestrator.<br>
  You create named PMs (roles); each PM finds where a job's files are, then runs a worker and a reviewer in their own panes.<br>
  Questions and approvals come back to you as popups in the orchestrator's window.
</p>

## How it is organised

- **Orchestrator**: the Claude Code session you talk to. It interviews you about a job, asks you to approve plans and merges, and keeps the queue.
- **PM**: a named role you create (`backend`, `docs`, ...). A PM is only a name: it owns no folders. For every job the orchestrator asks you which PM takes it (every PM plus "New PM"; recommended is the PM you named, else the PM that already worked on the path you said). Each job changes one path, a git checkout or a plain folder. The PM finds it while planning, from your words, its wiki and its places (the paths of its earlier jobs), and asks you when none shows it. A PM's session can read the whole file system, so it can look anywhere; it writes only where the guard below allows. Work across several paths becomes several jobs: the PM splits them off and the orchestrator tells you. Each PM keeps its own wiki (`<wiki_path>/<pm>/`). It has a planner subagent draft the plan in the background, checks it, starts the worker and the reviewer, and merges and verifies only what the review passed.
- **Worker / reviewer**: each runs in its own pane. They write reports. Only the PM merges, through `oke job merge`, and only what the review passed. A hook blocks git commands that publish or destroy work (`push`, `reset --hard`, forced `clean`, `checkout` and branch deletes) in every OKe session.
- **Where a code job works**: its PM decides and the plan says so, and you see it before approving: in a new worktree (branch `oke/<job>`), or directly in the main checkout. In the main checkout every uncommitted change to a tracked file is committed with the job (a new file only when the job stages it), so the plan also names which of your uncommitted changes it takes in (`이어받을 수정: <files>` under the plan's `## 작업 위치`, for "finish what I was doing") or none. OKe refuses to start a job in the main checkout while it holds an uncommitted change the plan does not name. Before the approval question, the orchestrator shows the plan's main parts in the chat.
- **What a job hands over** (its output, shown as `output` in `oke status`): `code`, a change merged into the main branch after you approve the merge (every size asks, except with `autonomy: full`); `files`, a plain folder's files changed in place (no merge); `result`, an answer (a research job) or a run's outputs (an experiment: a temporary edit and a run in a worktree, for example a simulation). An experiment merges nothing: the reviewer checks its outputs against the plan, and its worktree stays after the job until you say to remove it (`oke job clean <job>`). A job that turns out to need no change ends with no change once you agree, as finished, not failed.
- **Messages**: a message to an agent waits in its inbox (`$OKE_HOME/inbox/<agent>/`) and is typed in as soon as that agent's turn ends, even while its background subagents or tasks still run. A PM busy with one job's review therefore still takes the next job.
- **Checks**: the plan names numbered acceptance criteria (`## 수용 기준`) and one check command (`## 검증 명령`, in backticks). Before any work, `oke baseline <job>` runs the command on the untouched checkout and saves what already fails. The worker checks its work with `oke check <job>`: OKe runs the command, and only failure lines the baseline did not have count as new failures. OKe writes the result into the job's ledger and signs every row with a key (`$OKE_HOME/ledger.key`) no agent may read; a row without a valid signature, typed or copied by hand, does not count. `oke job merge` merges only when the review names the plan's current `criteria_rev` (a hash of the criteria and the command) and the ledger has a passing `oke check` on the reviewed commit. After the merge, `oke job verify <job>` records a pass without running when the merged code is the code the check passed on; when other merges changed main since the check, it lists the files they changed and the PM decides: `--skip "<why>"` when those files cannot change the result (documentation, say), recorded in the ledger with the reason and the files, or `--run` to run the command on main, which undoes the merge on a new failure.

Agents talk through files under `$OKE_HOME` (default `~/.oke`), so a crash loses no state. After a terminal, Claude Code or the whole app dies, `/oke:start` finds the open jobs and asks, per job, whether to **resume**, **hold** (set it aside, keep its worktree and branch) or **archive** it.

A PM, worker or reviewer may Edit or Write only in its own folder under OKe's home (a PM also in its open jobs' folders there; never the files only OKe writes, such as the ledger), OKe's `tmp/`, its own memory folder, and the paths of its open jobs that change something (not research): a git job's worktree (else its checkout), or a folder job's folder without the git repositories below it and links that lead out of it (`oke path-check`). Folder jobs change files in place, with no backup.

PMs: say "add a PM", "rename PM <x>" or "remove PM <x>". A PM with open or held jobs is removed only after you agree to cancel those jobs (its wiki is set aside as `<name>.removed-<time>`); a PM is renamed only while it has none; its finished jobs carry over to the new name. `oke pm list` shows them from a shell.

## The board

`/oke-board` opens a live board beside the orchestrator: one line per PM, one card per job with its phase, the absolute path it works in (its worktree, or its path), and its worker and reviewer.

```
╭─────────────────────────────────────────────────────────────╮
│ ▍backend                                                    │
│                                                             │
│  · PM done 0m  ▸                                            │
│                                                             │
│ ╭─────────────────────────────────────────────────────────╮ │
│ │ textkit · shop5 · large  ⏸ on hold                      │ │
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
  - neither: agents run as background `claude --bg` sessions. A background session cannot answer the trust dialog, so open `claude` once in the OKe home (`~/.oke`) and in each git checkout or folder a job works in, and accept it.

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

Start Claude Code in a herdr pane or a plain terminal, and run `/oke:start`. That session becomes the orchestrator. Tell it what you want done. If the session was cut off, run `/oke:resume`.

`oke status` (and `oke board`) work from any terminal: they read the runtime the orchestrator chose. Outside that runtime's terminals (for example a plain shell while OKe runs in herdr) they only report; nothing can be started from there.

Things you can say along the way:

- `hold <job>`: set a job aside. Its agents are closed; its worktree and branch stay.
- `continue <job>`: bring a held job back; the PM restarts what it needs.
- `cancel <job>`: drop it.

## Scheduled runs: `/oke:automate`

Run `/oke:automate` in any Claude Code session and say what should run and how often. Claude asks for the prompt, the folder, the schedule, the permission mode and optionally the model, shows the setup, and registers it: with `crontab` on Linux and macOS, with Task Scheduler (folder `OKe`) on Windows. At each scheduled time a background session (`claude --bg`, named `auto-<name>-<MMDD-HHMMSS>`) starts in that folder with the saved prompt; watch it with `claude attach <session>` or list sessions with `claude agents`.

- Schedules (local time): `every 30m`, `every 2h`, `daily 09:00`, `weekdays 09:00`, `weekly mon,thu 09:00`, and `cron <five fields>` outside Windows.
- The folder must be one Claude Code already trusts: a background session cannot answer the trust dialog.
- Nobody answers a scheduled session's dialogs. With the user's default permission mode it waits at its first dialog until someone attaches; a run is skipped while the previous run of the same automation still works or waits.
- The computer must be on at that time; on Windows the user must be logged in (the task also runs on battery). On Linux and macOS a run gets the PATH and locale the shell had when it was registered; on Windows it gets the user's login environment.
- Each automation is a folder `$OKE_HOME/automations/<name>/` (`automation.json`, `prompt.md`, and `run.log` with one line per run, manual ones included). `oke automate list`, `oke automate run <name>` (one run now) and `oke automate remove <name>` work from a shell too.

## Settings: `~/.oke/config.json`

Optional. Only the values you set override the defaults.

OKe keeps its files in `~/.oke`. To use another folder, set `OKE_HOME` in the `env` of `~/.claude/settings.json` (`"env": {"OKE_HOME": "D:/oke"}`): every agent OKe starts gets it there.

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
- `compact_min_percent`: when a PM, worker or reviewer has written the report for a task, its mod compacts its context, keeping what its next work needs (its waiting mail; for a PM each of its jobs that is still open and not on hold, the one just reported included; for a worker or reviewer, that the PM may come back on its job) and dropping the finished task's details. It does so only when at least this percentage of the context is used. Default `30`. Turning auto-compact off in Claude Code turns this off too.
- `merge_ignore_untracked`: untracked paths in the checkout that never block a merge, such as another tool's state folder. Default `[".omc/"]`. Python caches never block either.
- `interview_threshold`, `interview_max_rounds`: the interview the orchestrator runs when it takes a job. It scores the goal, constraints and success criteria from 0 to 1 and asks one question at a time about the vaguest part, until the ambiguity (`1 - 0.4 goal - 0.3 constraints - 0.3 criteria`) is at or below the threshold. It also stops when you pick "plan now" or it reaches the round limit.

After installing, follow the checks in `docs/work-checklist.md`.

## Token use

Each agent's mod adds one line per turn (its subagents' turns included) to `usage.csv` in the agent's folder: time, model, input, output, cache read and cache write tokens. The orchestrator writes `$OKE_HOME/usage.csv`. `python oke/bin/oke.py cost [job]` adds them up per job (archived jobs too), per PM and for the orchestrator.

## Development

```
cd oke/bin && python -m unittest discover -p "test_*.py"   # CLI and runtime tests
claude plugin test oke                                     # mod (band, board, git guard) tests
```
