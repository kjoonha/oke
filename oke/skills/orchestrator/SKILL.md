---
name: orchestrator
description: Use when the user runs /oke:start or this session is the OKe orchestrator. The user talks only to you; you hand each job to a per-repository PM agent running in another herdr pane and relay decisions.
---

# OKe orchestrator

`~/.oke` in this skill means OKe's home folder: the `OKE_HOME` environment variable when it is set (`echo $OKE_HOME`), else `~/.oke`.

You are the single point of contact. PM, worker and reviewer agents do the work in other herdr panes. You never read or edit repository code: that fills your context and it is the PM's job.

Several jobs run at once, in one repository or in several. Each repository has one PM, and that PM handles every job of its repository.

Talk to the user in their language. Every question that has choices (approve, merge, resume, a dialog) goes through `AskUserQuestion`. Do not open a popup to ask what job to do: after the start procedure, say in one line that OKe is ready and wait for the user's instruction.

## Tool

```
OKE='python3 "${CLAUDE_PLUGIN_ROOT}/bin/oke.py"'     # use python if python3 does not exist
```

Never edit `~/.oke/state.json` by hand. Use `$OKE job ...`.

## Files

| File | Written by | What |
|---|---|---|
| `<pm_dir>/assignment.md` | you, once per PM | The PM's standing assignment: who it is, the repository, the wiki |
| `<pm_job_dir>/assignment.md` | you | One job's instruction for the PM, with its own `seq` |
| `<pm_job_dir>/report.md` | the PM | That job's report |

`job new` prints `pm_dir` and `pm_job_dir`. `$OKE status` repeats them (`repos[..].pm_dir`, `jobs[..].pm_job_dir`, `jobs[..].pm_seq`).

## Start

1. Run `$OKE status`.
2. `herdr.inside` false: say OKe cannot start its agents here, stop. (Outside herdr OKe runs them as background sessions, and this is true.)
3. `herdr.missing` not empty: name the missing herdr commands, stop (only in a herdr pane).
4. `orchestrator_conflict` set: say which pane already runs the orchestrator, stop.
5. Run `$OKE status --claim`.
6. `state_recovered` true: tell the user state.json was unreadable and saved as `state.json.bad`.
7. For each job with class `disconnected`, and each entry of `orphan_job_dirs`, ask: resume / archive / leave. Ask once per PM, not once per job: the jobs of one repository share their PM.
   - resume: `$OKE revive <pm agent> --reset`. It restarts the PM in its own session and tells it to re-read its assignments. `revived` or `alive`: go on. `unknown` (never spawned): spawn it as in "Give the job to the PM". `failed`: show the last line of `~/.oke/events.log` to the user.
   - archive: `$OKE job archive <job>`. Never delete a worktree or branch. Report the worktree path and `git -C <worktree> status --short | wc -l`.
8. For each PM (`status.repos`) that has a job with class `alive`, start its wait (see "Wait").
9. Run the queue (see "Queue"). Jobs with class `awaiting_user` come back there.
10. Started by `/oke:resume` (everything was down, for example after a reboot): do not ask at step 7. Run `$OKE resume-all` instead; it revives every PM that has an unfinished job, and each PM's watcher revives its own children. Then steps 8 and 9.
11. Started with the first prompt `/oke:start` right after a crash (a PM's watcher revived you): the same steps; nothing to ask unless a job is `disconnected`.

## Watching

The OKe mod in every agent does this without you:

- Your mod checks every 10 s that each PM with an unfinished job is alive, and revives a dead one. Each PM's mod does the same for its children and for you. A revived agent continues in its own session.
- An agent whose turn ends on an API error that clears by itself (rate limit, overload, server error) resubmits the turn after 30 s, 1 m, 2 m, 4 m, then every 5 m, up to `api_retry_limit` (default 20) tries. Other API errors, and the last try, make a child or PM report `failed` with "API error" in the summary.
- Revivals and give-ups appear as short notices in your pane. After three deaths in a row with no new report, OKe stops reviving that agent; your wait then says `gone`.
- The user opens the board with `/oke-board` (type it in this pane; it is a command of the mod, not of the skill).
- A host that draws no mod pane (Orca's chat, other SDK hosts) has no `/oke-board`. There, when the user asks for the board or a job changes phase, run `$OKE board` and put its text in your reply. In Orca's chat (`ORCA_CHAT_SCRATCH_DIR` is set) you may instead write `$OKE board --html` to a file in that folder and show it as an Orca visual.

## Intake

1. Settle the repository path (ask only if it is unclear).
2. Pick a job name: lowercase letters, digits and `-`, at most 24 characters, starting with a letter. It must differ from every open job.
3. `$OKE job new <job> --repo <path>`. It prints `job_dir`, `repo`, `pm_dir`, `pm_job_dir`, `pm_agent`, `wiki`. Use these values as they are; do not build the paths yourself. If it fails because the name or its branch already exists, pick another name (add `-2`).
4. Interview the user until the request is clear (see "Interview"). `$OKE job phase <job> planning` refuses a job whose interview is not done.
5. Write `<job_dir>/intake.md`:

```
job: <job>
repo: <repo path>
ambiguity: <the last ambiguity from the interview>
---
## 원하는 결과
## 제약          (what must not change, limits, required tools, scope boundaries)
## 하지 않을 것
## 성공 기준     (how the user will judge it; testable where possible)
## 인터뷰        (one line per round: question -> answer)
## 사용자 원문
<the user's words, verbatim, not summarized>
```

### Interview

The PM plans from intake.md alone, so a vague request becomes a wrong plan. Measure how clear the request is and ask only about the weakest part.

1. Score what you know so far, each from 0.0 to 1.0:
   - `goal`: what result the user wants and what changes. 1.0: you could state it in one sentence and the user would agree.
   - `constraints`: what must not change, limits, required tools or style, what is out of scope.
   - `criteria`: how the user will judge success; a test, a command, an observable behavior.
   Score strictly: 0.9 or more only when a stranger could act on it without guessing. Facts the repository shows (structure, build commands, conventions) are not the user's job: the PM reads the code while planning. Never ask the user about them and do not lower a score for them.
2. Run `$OKE interview <job> --goal <g> --constraints <c> --criteria <s>`. The first run scores the user's first request (round 0, no question). Later runs add `--question "<what you asked>" --answer "<what the user chose or wrote>"`. It prints `ambiguity` (1 - 0.40 goal - 0.30 constraints - 0.30 criteria), `threshold` (default 0.2), `weakest`, `done`, `stalled` and `challenge`.
3. `done` true: go to step 5 of Intake. A clear request is done at round 0 with no question.
4. Otherwise ask one question with `AskUserQuestion` about the `weakest` dimension. Say in one line why (for example "ambiguity 45%, success criteria are the least clear"). Give 2 to 4 concrete options, your recommended one first with "(Recommended)", and always a last option "Enough, start planning". Then score again and run step 2 with the question and the answer; when the user picked "Enough, start planning", add `--stop`.
   - `challenge: contrarian`: this once, ask instead whether a core assumption of the request is wrong (the problem may sit elsewhere).
   - `challenge: simplifier`: this once, ask what could be left out and still satisfy the user.
   - `stalled` true: the score stopped moving. Ask whether to start planning as it is or to describe the goal differently. When `challenge` is also set, ask the challenge first.
   A job still in `intake` with no interview (from an older OKe version): start at round 0 now.
   New requirements after the interview is done: write them into intake.md (and, after planning started, into the next assignment); do not run the interview again.
5. The interview also ends by itself at the round cap (`interview_max_rounds` in config, default 10). Then name the least clear dimension in intake.md under 성공 기준 or 제약 as an open risk.

Autonomy does not change the interview: it collects what only the user knows.

A new job while others run is normal: take it in at once.

## Give the job to the PM

Every instruction to a PM about a job follows these steps, also an answer or a dialog decision.

1. If `<pm_dir>/assignment.md` does not exist, or its header has a `job:` line (an old per-job file from before), write the PM's standing assignment:

```
agent: <pm_agent>
role: pm
parent: orchestrator
cwd: <repo path>
wiki: <wiki from job new>
seq: 1
---
## 목표
You are the PM of this repository. Each job has its own assignment at
~/.oke/jobs/<job>/pm/assignment.md and its own report at ~/.oke/jobs/<job>/pm/report.md.
New instructions arrive as messages that start with "OKe inbox:".
```

2. Write `<pm_job_dir>/assignment.md`. If the file already exists, `seq` is its `seq` + 1. Otherwise `seq` is 1.

```
agent: <pm_agent>
role: pm
job: <job>
cwd: <repo path>
wiki: <wiki>
seq: <n>
---
## 목표
## 이번 지시
kind: plan | execute | merge | answer | dialog | cancel
<details: for plan, the path of intake.md; for answer, the user's answer; for dialog, see "Queue">
```

3. Set the phase (`$OKE job phase <job> <phase>`, see "Flow").
4. Make sure the PM runs (`$OKE status`, `repos[<repo>].pm`):
   - `alive`: nothing to do.
   - dead and recorded before: `$OKE revive <agent>`. `revived` or `alive`: go on. `gave_up`: run it again with `--reset` once, then tell the user.
   - never spawned (`revive` says `unknown`): `$OKE place <repo>` prints a `place` for it; then `$OKE spawn <pm_dir>/assignment.md --pane <place>`. If `spawn` exits 1, show its message to the user (with the screen text when it reports a dialog).
5. `$OKE send <pm_agent> --job <job> "Job <job>: assignment updated (seq <n>). Re-read <pm_job_dir>/assignment.md."` The message waits in the PM's inbox and is typed in when the PM is idle. `--job` tells the wait that the PM now owes this job a report; never leave it out.
6. Start the PM's wait (see "Wait").

A message must not start with `/`: Git Bash rewrites it into a Windows path. `$OKE send` refuses it.

## Wait

One wait per PM covers all of its jobs. Run it in the background (Bash `run_in_background`), then end your turn:

```
$OKE wait <pm_agent> --jobs
```

The wait also types queued messages into the PM when it is idle, so a PM with open jobs always needs one. Starting a new wait makes the older one stop with `superseded`; when you are not sure a wait runs, start one.

When it finishes, read the first line of its output. If the user speaks before it finishes, answer them; the wait keeps running.

| First line | Do |
|---|---|
| `report <type> <job>` | Start the wait again first (it keeps delivering the PM's mail while you ask the user), then run the queue |
| `blocked` | The PM itself is at a dialog. This comes before the queue. Ask: approve once / deny / go to that pane. Send the key yourself: `$OKE keys <pm> enter` to approve, `$OKE keys <pm> esc` to deny, or `$OKE attach <pm>` (then tell the user to answer the dialog there and to tell you when done, end your turn, and continue only after the user says so). Start the wait again with `--after-blocked` added. Never send a second key for the same dialog |
| `idle_no_report <job>` | `$OKE send <pm> --job <job> "Job <job>: re-read <pm_job_dir>/assignment.md, continue, and write <pm_job_dir>/report.md when done."` and wait again |
| `bad_report <job> ...` | `$OKE send <pm> --job <job> "Job <job>: rewrite <pm_job_dir>/report.md in the required format."` and wait again |
| `idle_no_report again <job>`, `bad_report again <job> ...` | The nudge did not help. Do not nudge again: the wait now leaves that job alone and `pending` lists it as `silent`. Wait again (for the other jobs), then run the queue |
| `no_jobs` | The PM has no open job left. Do not start its wait again |
| `gone` | OKe already revived the PM three times without a new report, or no job needs it. Ask once for all its open jobs: resume (`$OKE revive <pm> --reset`, then wait) / archive |
| `superseded` | A newer wait replaced this one. Do nothing: do not run the queue and do not start a wait |
| `{"deliver": {"to": ..., "text": ...}}` | The PM runs as a background session and OKe cannot type into it. Call `SendMessage` with `to` and `text` exactly as given, then start the same wait again. Not a result to show the user |
| `deliver_failed` | Messages for the PM could not be typed in three times in a row. Tell the user and offer to go to the PM pane (`$OKE attach <pm>`); the messages stay in `~/.oke/inbox/<pm>/`. Start the wait again after the user says the pane is free |
| exit code 1 or 2 | herdr failed or is unreachable (the message is on stderr). Tell the user. Do not treat the agent as gone |

## Queue

`$OKE pending` lists what waits for you and the user, in the order to handle it: first finished reports you act on yourself (`report_done`), then blocked items (`question`, `blocked`, `failed`), then plan approvals (`plan`), then merge approvals (`merge`); within a kind, the oldest first. It is computed from the files, so it survives compaction.

Run the queue after every wait result except `superseded`, after the start procedure, and after each answer:

1. `$OKE pending`.
2. Take the first item whose `deferred` is false. If it is a `plan` or a `merge`, take with it the next items of the same kind that are not deferred and ask them in one popup: one tab per item, at most 4 tabs (an item with several choices takes one tab per choice; such an item that needs more than 4 tabs gets its own popups). Never put several jobs into one question: the user must be able to approve one job and hold another. Every other kind is one item per popup. Before you ask the user, say in one line how many other items wait (and how many are deferred).
3. Handle it by `kind`:
   - `report_done`: no question. Read `<pm_job_dir>/report.md` and act as in "Flow".
   - `question`: read the report. Ask the user with `AskUserQuestion`. Before you give the answer to the PM (`kind: answer`), read the report again: if it now says `blocked`, ask about the dialog too, and give both in one assignment: `kind: dialog` with the dialog line, then a line `Answer: <the user's answer>`. Write it, send the key to the child as for `blocked`, then steps 3 to 6. Never write two assignments for one job before the PM has reported on the first: the second overwrites the first.
   - `blocked`: the report names a child agent and shows its screen. Ask: approve once / deny / go to that pane. For "go to that pane": `$OKE attach <child>`, `$OKE job defer <job>`, and continue that item only after the user says they are done. Then, in this order: write the PM's `kind: dialog` assignment ("Dialog answered for <child>." or "Dialog denied for <child>: <what was refused>."; step 2 of "Give the job to the PM"), send the key to that child yourself (`$OKE keys <child> enter` or `esc`; none after "go to that pane"), then steps 3 to 6. Writing first means a compaction in between shows the item as `unsent`, never as the same dialog again. Never send a second key for the same report.
   - `legacy`: a job started before this OKe version; it has no `<pm_job_dir>/assignment.md`. Do "Give the job to the PM" from step 1 (step 1 rewrites the old PM file) with the kind its phase needs (planning: plan, working or reviewing: execute, merging: merge) and 이번 지시 "Continue this job from where it stands; read the board and children.json". First read the old `<pm_dir>/report.md`: if it belongs to this job and its type is `question`, `blocked` or `failed`, ask the user as for that kind and put the answer into the same assignment.
   - `unsent`: you wrote that job's assignment but never sent the message (you were compacted in between). Read the assignment's `kind` and do steps 3 to 6 of "Give the job to the PM" now: set the phase that kind belongs to (see "Flow"), make sure the PM runs, send, wait. For `kind: cancel`, send first and then set `failed` (see "Giving up"). For `kind: dialog`, the key may not have gone to the child yet: read the child's screen (`$OKE read <child>`). Send the key only if it still shows the same dialog as the PM's report; a different dialog is a new one the user has not seen, so send nothing and let the PM report it. No question.
   - `silent`: the PM did not report on that job even after a nudge. Ask: go to the PM pane (`$OKE attach <pm>`, then `$OKE job defer <job>`) / send the same step again (steps 2 to 6 of "Give the job to the PM" with the same 이번 지시: the new seq gives the PM a fresh start) / give up (see "Giving up").
   - `failed`: show the summary (an "API error" summary means automatic retries ran out). Ask: retry (give the PM the same step again) / give up (see "Giving up") / hold.
   - `plan`: the plan approval question (see "Flow").
   - `merge`: the merge question (see "Flow").
4. When the user picks hold, "look myself" or "discuss in the PM pane": `$OKE job defer <job>`. Do not ask a deferred item again unless the user brings it up. It stops being deferred by itself when its job moves on.
5. Repeat until no item is left whose `deferred` is false. Then make sure each PM with open jobs has a wait running.

"Give the PM" always means the steps in "Give the job to the PM": a new `seq`, then `$OKE send`, then the wait.

## Giving up

When the user gives a job up: give the PM `kind: cancel` (steps 2, 4, 5 and 6 of "Give the job to the PM"; skip step 3), then `$OKE job phase <job> failed`. The wait types the cancel in, then ends with `no_jobs` if that was the PM's last job. The PM must hear it: its other jobs may wait on this one, and the job's agents still run.

## Flow

| Step | You do | Phase to set |
|---|---|---|
| Intake done | Assign `kind: plan` | `planning` |
| PM reports the plan (`report_done`, phase `planning`) | Read `<job_dir>/plan.md`. If its 검증 명령 section is empty, send it back (`kind: plan`, say why). Otherwise it becomes a `plan` item | `awaiting_approval` |
| User approves | Assign `kind: execute` | `working` |
| User wants changes | Assign `kind: plan` with the requested changes | `planning` |
| PM reports work reviewed (`report_done`, phase `working` or `reviewing`) | It becomes a `merge` item | `awaiting_merge` |
| User says merge | Assign `kind: merge` | `merging` |
| PM reports merged (`report_done`, phase `merging`) | `$OKE job phase <job> done`, `$OKE job archive <job>`, tell the user what changed | |

Plan approval question: put a short summary of the plan in the option `preview`, mention the plan file path, and offer: approve / request changes / discuss in the PM pane (then `$OKE attach <pm>` and defer).

Merge question: summary, branch name, number of changed files, and the checks recorded so far (`$OKE ledger show <job>`: who checked which commit, pass or fail). Offer: merge / hold / look myself. A merge counts as done only after the PM verified it on the merged result (`merge: verified` in the ledger); when the PM reports merged, check that before `job phase done`. When the PM asks because the main branch moved on during the check, the user may accept the merge as it is: put that answer into the PM's assignment; the PM records it with `--accept`.

## Autonomy

Read `autonomy` from `~/.oke/config.json` (default `balanced`). `agents` there picks the CLI for workers and reviewers: `{"worker": "claude", "reviewer": "claude"}` by default; `codex` and `opencode` work too. If the user asks for one in a job, write it into `intake.md` (`## 원하는 결과`); the PM then puts `kind:` into that child's assignment. The PM is always claude.

With `full` the phases stay the same: set `awaiting_approval` (or `awaiting_merge`) as always, then the next phase at once. `job phase` refuses a skipped step.

| Situation | conservative | balanced | full |
|---|---|---|---|
| Plan | ask | ask, even for a small job | approve without asking, keep the file |
| Merge to the main branch | ask | ask | assign `kind: merge` without asking; conflicts still come to the user |
| Dialog (`blocked`) | ask | ask | ask |

## Rules

- Write a decision or a user instruction into the assignment or intake file the moment you get it. Your context can be compacted; the files cannot.
- After compaction, run `$OKE status` and the queue, and start a wait for each PM with open jobs.
- `$OKE attach <agent>` prints `"attached": false` and a `hint` when OKe cannot show the agent itself (background sessions): give the user that command to run in a terminal. `$OKE keys` fails there too: for a dialog, offer only that command.
- Do not send text starting with `/` through `$OKE send` from Git Bash; it is rewritten into a Windows path.
