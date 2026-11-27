# OKe end-to-end bench

An unattended run of the OKe orchestrator through 12 requests in Korean on small local repos. Each request is scored PASS or FAIL. All the agents run on the local free LLM proxy, never on real Claude tokens.

## What it covers

| task | request | checks |
|---|---|---|
| T01 | a question about calc's `div` | answered, no approval, no branch, no commit |
| T02 | "conv PM으로 README.md 오타 고쳐줘" | no interview, no PM-pick popup, at most 2 popups, the typo fixed, the user's NOTE line kept |
| T03 | a section in notes' guide.md (docs only) | check `none`, review grades VERIFIED |
| T04 | finish the user's `mul` in calc main | the commit holds only calc.py and test_calc.py; notes.txt and CHANGELOG.md stay uncommitted; the failing test that was already there is reported as pre-existing |
| T05 | `add_tax` in the linked worktree shop-add-tax | commits on test/add-tax, no `oke/` branch, main unchanged |
| T06 | `sub` and the README, "커밋은 코드랑 문서 따로" | two commits: code and docs |
| T07 | an HTML report in shop-add-tax, "커밋하지 말고 둬" | the file is there, not committed, no new worktree |
| T08 | "conv 좀 더 좋게 해줘" (vague) | an interview popup |
| T09 | `pow`, "묻지 말고 진행해" | one confirm popup, no approval popup |
| T10a/b | two jobs in calc main at once | the second waits, both finish |
| T11 | a change request typed into the approval popup | the plan is revised in one step |

`tasks.json` maps the tasks to the items of `oke-notes/2026-11-27-problems.md` (A1, A1b, A2, A3, A4, B5, B6, B7, B9, C10, C11, D13, U1, U2). `report.md` gives a verdict per item. It also records the popups, the time, the tokens (`oke cost`) and the English narration lines of each task.

## Run

From Git Bash:

```
sh bench/run.sh                       # everything; the run dir goes to %LOCALAPPDATA%/oke-bench/run-<stamp>
sh bench/run.sh --keep                # leave the session and the run dir for inspection
sh bench/run.sh --only T02-anchored-typo,T04-continue-mul --budget-scale 1.5
BENCH_MODEL=free-nvidia sh bench/run.sh   # every agent on another free proxy model (default free-coder)
```

`run.sh` runs these steps:

1. `setup.sh <run_dir>` creates the repos, `orch/` and `home/` (config.json and the PMs calc, conv, shop and notes).
2. `launch.ps1 -RunDir <run_dir>` starts the herdr session `oke-bench` and runs the env probe.
3. `driver.py <run_dir>` types the claude command into the orchestrator pane and runs `/oke:start`, then the tasks.
4. `score.py <run_dir>` writes `report.md`.
5. `teardown.py <run_dir>` cleans up.

Before teardown, the report and the logs are copied to `bench/results/<stamp>/`. Each step also runs on its own. `driver.py <run_dir> --dry-run` checks every prompt against OKe's triage and starts nothing.

Each task has a time budget: 12 min for a small one, 25 for a big one, 35 for the queue pair. When the budget runs out the task is TIMEOUT. When no job changes phase and every agent is idle for 8 min, the task is STUCK and the screens of all panes are saved. Either way the driver says `hold <job>` and goes on to the next task. Every herdr and git call has a timeout.

Popups are answered by policy. The default is the first option (approve, or the Recommended one), but never an option that cancels, deletes or goes to a pane. A child's permission dialog for a destructive command is denied. After two interview answers, the driver picks "start planning". T11 types its change request into the free-text option. Every popup screen and its answer is logged in `logs/<task>/popup-NN.txt`.

## The free-proxy guarantee

- `launch.ps1` loads `~/litellm-proxy/.env` through `ensure-proxy.ps1`, which also starts the proxy, and checks `/health/liveliness`. It never prints the key.
- It removes every `CLAUDE*`, `ANTHROPIC_*`, `HERDR_*`, `OKE_*` and `AI_AGENT*` variable and the provider keys from its own process. `CLAUDE_CODE_GIT_BASH_PATH` stays.
- It then sets `CLAUDE_CONFIG_DIR=~/.claude-free`, `ANTHROPIC_AUTH_TOKEN` (the proxy key), `ANTHROPIC_BASE_URL=http://127.0.0.1:4000`, every model variable to the bench model (`BENCH_MODEL`, default `free-coder`; haiku to `free-fast`), `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `OKE_HERDR=["herdr","--session","oke-bench"]` and `OKE_HOME=<run_dir>/home`.
- With that env it starts `herdr --session oke-bench server`. Every pane inherits the server's env, and so does every claude in a pane (orchestrator, PMs, workers, reviewers). herdr inside a pane targets `oke-bench` through `HERDR_SOCKET_PATH`. `herdr agent start`, which OKe uses for PMs, workers and reviewers, types claude into the pane's shell, so those get the pane's env too.
- `run.sh`, `launch.ps1`, `driver.py` and `teardown.py` drop every `HERDR_*` variable from each herdr call they make, so an inherited socket cannot point `--session oke-bench` at another session.
- Before claude starts, `probe.py` runs in the orchestrator pane and prints yes or no per check: the config dir, the base URL, the token, no `ANTHROPIC_API_KEY`, the models, `OKE_HOME`, `OKE_HERDR`, the bench socket, no leftover session vars, the cwd, the proxy, and the proxy's model list (`/v1/models`): no `claude`/`anthropic` name and no wildcard route. If any check says no, the launcher stops its own session and exits.
- The driver starts claude only by typing its command into that probed pane. Within 60 s of its first prompt a session file must appear under `~/.claude-free/projects/`, and none of this run under `~/.claude/projects/`; else the driver stops `oke-bench` and the run ends.
- Every minute the driver looks under `~/.claude/projects/` for session files written since the run began in folders of the run dir or of the bench repos' herdr worktrees. One found: it records `LEAK` (`logs/LEAK.json`, `results.json`) and stops `oke-bench` at once.
- `config.json` gives every role the bench model and sets `allowed_models` to it and `free-fast`: a `model:` line in an assignment that names anything else falls back to the bench model. `BENCH_MODEL` must be a `free-*` name; setup, the launcher, the probe and the driver each refuse any other.
- The harness never touches the default herdr session, `~/.oke` or other processes, and never pushes. Teardown stops and deletes only the `oke-bench` session. It removes the run dir and only those `~/.herdr/worktrees/<repo>-*` folders whose worktrees all point into the run dir; a worktree whose `.git` it cannot read keeps its folder.
