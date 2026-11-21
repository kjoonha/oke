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
- Optional: codex or opencode, to use as the worker or reviewer

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

In Orca, OKe runs only in a terminal tab, never in an Orca chat (a chat draws no mod pane), whatever your Orca default for new agent tabs (Chat or CLI): `/oke:start` in a chat refuses. OKe opens a terminal for each PM, worker and reviewer itself and starts claude there with OKe's own flags. Orca's Agent Orchestration setting must be on. Leave those terminals open: closing one means that agent is gone, and the orchestrator asks whether to resume it (in a new terminal, on the same session).

Things you can say along the way:

- `hold <job>`: set a job aside. Its agents are closed; its worktree and branch stay.
- `continue <job>`: bring a held job back; the PM restarts what it needs.
- `cancel <job>`: drop it.

## Settings: `~/.oke/config.json`

Optional. Only the values you set override the defaults.

```json
{
  "autonomy": "balanced",
  "wiki_path": "~/.oke/wiki",
  "models": {"pm": "opus", "worker": "opus", "reviewer": "opus"},
  "agents": {"worker": "claude", "reviewer": "claude"},
  "interview_threshold": 0.2,
  "interview_max_rounds": 10,
  "lean_launch": true,
  "effort": {"reviewer": "high"},
  "theme": "disco"
}
```

- `theme`: the characters in the band above the prompt. Each role has its own shape or colour.
  - `disco` (default): blobs in headphones. Orchestrator purple, PM blue, worker green, reviewer orange.
  - `slime`: bouncing slimes. Orchestrator green, PM blue, worker pink, reviewer yellow.
  - `mushroom`: bouncing mushrooms. Orchestrator purple, PM blue, worker orange, reviewer green.
  - `cute-dog` (floppy-eared puppy) and `cute-cat` (kitten): tails wag, ears twitch, they blink and curl up to sleep. Each role has its own coat.
  - In the disco colours (orchestrator purple, PM blue, worker green, reviewer orange): `ghost`, `robot`, `frog`, `penguin`, `bunny`, `bear`, `duck`, `octopus`, `alien`, `cactus`, `jellyfish`, `fox`, `panda`, `hamster`, `owl`, `whale`, `dino`, `star`, `pixel-heart`, `coffee`, `onigiri`, `axolotl`, `bee`, `snail`, `chick`, `sakura`.
  - Palette variants: `pastel` (bunnies), `candy` (hearts), `ocean` (whales), `autumn` (foxes), `midnight` (owls).
  - Colour schemes, with role colours from the scheme's own palette: `tokyo-night`, `catppuccin` (mocha), `github` (dark), `dracula`, `nord`, `gruvbox`, `solarized`, `one-dark`, `monokai`, `rose-pine`, `everforest`, `kanagawa`, `ayu`, `material`, `synthwave-84`.
  - An unknown name falls back to `disco`.
- `autonomy`: `conservative` (the worker asks before every command), `balanced` (default) or `full`.
- `agents`: the CLI that runs the worker and the reviewer: `claude`, `codex` or `opencode`. The orchestrator and PMs are always claude. Limits are listed under "안 되는 것" in `docs/superpowers/specs/2026-11-13-oke-slice4-design.md`.
- `lean_launch`: start PMs, workers and reviewers without your plugins, hooks and MCP servers (`--setting-sources project,local --strict-mcp-config`). Only the herdr hooks and `env` from your `settings.json` are copied into `~/.oke/lean-settings.json`. This cuts the starting context from about 76k to 40k tokens. Each role also gets only its core tools (`--tools`, about 15k tokens at start). For each job, the PM turns on extra tools with the assignment header's `tools:`, `mcp:` and `plugins:` lines (`oke.py extras` lists them). Set it to `false` if the agents need all your personal settings.
- `effort`: the effort level (`low` to `max`) per role (`pm`, `worker`, `reviewer`). A role left out keeps the `effortLevel` of your own settings. Claude agents only.
- `interview_threshold`, `interview_max_rounds`: the interview the orchestrator runs when it takes a job. It scores the goal, constraints and success criteria from 0 to 1 and asks one question at a time about the vaguest part, until the ambiguity (`1 - 0.4 goal - 0.3 constraints - 0.3 criteria`) is at or below the threshold. It also stops when you pick "plan now" or it reaches the round limit.

After installing, follow the checks in `docs/work-checklist.md`.

## Development

```
cd oke/bin && python -m unittest discover -p "test_*.py"   # CLI and runtime tests
claude plugin test oke                                     # mod (band, board, git guard) tests
```
