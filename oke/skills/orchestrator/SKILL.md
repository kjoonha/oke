---
name: orchestrator
description: Use when the user runs /oke:start or this session is the OKe orchestrator. The user talks only to you; you hand each job to a per-repository PM agent running in another herdr pane and relay decisions.
---

# OKe orchestrator

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
2. `herdr.inside` false: say OKe needs a herdr pane, stop.
3. `herdr.missing` not empty: name the missing herdr commands, stop.
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

## Intake

1. Settle three things, asking only what is unclear: the repository path, the wanted result, the success criteria.
2. Pick a job name: lowercase letters, digits and `-`, at most 24 characters, starting with a letter. It must differ from every open job.
3. `$OKE job new <job> --repo <path>`. It prints `job_dir`, `repo`, `pm_dir`, `pm_job_dir`, `pm_agent`, `wiki`. Use these values as they are; do not build the paths yourself. If it fails because the name or its branch already exists, pick another name (add `-2`).
4. Write `<job_dir>/intake.md`:

```
job: <job>
repo: <repo path>
---
## 원하는 결과
## 성공 기준
## 사용자 원문
<the user's words, verbatim, not summarized>
```

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
   - never spawned (`revive` says `unknown`): find a pane for it. `herdr pane list`: if a pane has `cwd` equal to the repository root, use `--split-from <that pane>`. Otherwise `herdr workspace create --cwd <repo> --label <repo name> --no-focus` and use `--pane <.result.root_pane.pane_id>`. Then `$OKE spawn <pm_dir>/assignment.md --split-from <pane>` (or `--pane <pane>`). If `spawn` exits 1, show its message to the user (with the screen text when it reports a dialog).
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
| `blocked` | The PM itself is at a dialog. This comes before the queue. Ask: approve once / deny / go to that pane. Send the key yourself: `herdr agent send-keys <pm> enter` to approve, `herdr agent send-keys <pm> esc` to deny, or `herdr agent focus <pm>` (then tell the user to answer the dialog there and to tell you when done, end your turn, and continue only after the user says so). Start the wait again with `--after-blocked` added. Never send a second key for the same dialog |
| `idle_no_report <job>` | `$OKE send <pm> --job <job> "Job <job>: re-read <pm_job_dir>/assignment.md, continue, and write <pm_job_dir>/report.md when done."` and wait again |
| `bad_report <job> ...` | `$OKE send <pm> --job <job> "Job <job>: rewrite <pm_job_dir>/report.md in the required format."` and wait again |
| `idle_no_report again <job>`, `bad_report again <job> ...` | The nudge did not help. Do not nudge again: the wait now leaves that job alone and `pending` lists it as `silent`. Wait again (for the other jobs), then run the queue |
| `no_jobs` | The PM has no open job left. Do not start its wait again |
| `gone` | OKe already revived the PM three times without a new report, or no job needs it. Ask once for all its open jobs: resume (`$OKE revive <pm> --reset`, then wait) / archive |
| `superseded` | A newer wait replaced this one. Do nothing: do not run the queue and do not start a wait |
| `deliver_failed` | Messages for the PM could not be typed in three times in a row. Tell the user and offer to go to the PM pane (`herdr agent focus <pm>`); the messages stay in `~/.oke/inbox/<pm>/`. Start the wait again after the user says the pane is free |
| exit code 1 or 2 | herdr failed or is unreachable (the message is on stderr). Tell the user. Do not treat the agent as gone |

## Queue

`$OKE pending` lists what waits for you and the user, in the order to handle it: first finished reports you act on yourself (`report_done`), then blocked items (`question`, `blocked`, `failed`), then plan approvals (`plan`), then merge approvals (`merge`); within a kind, the oldest first. It is computed from the files, so it survives compaction.

Run the queue after every wait result except `superseded`, after the start procedure, and after each answer:

1. `$OKE pending`.
2. Take the first item whose `deferred` is false. Before you ask the user, say in one line how many other items wait (and how many are deferred).
3. Handle it by `kind`:
   - `report_done`: no question. Read `<pm_job_dir>/report.md` and act as in "Flow".
   - `question`: read the report. Ask the user with `AskUserQuestion`. Before you give the answer to the PM (`kind: answer`), read the report again: if it now says `blocked`, ask about the dialog too, and give both in one assignment: `kind: dialog` with the dialog line, then a line `Answer: <the user's answer>`. Write it, send the key to the child as for `blocked`, then steps 3 to 6. Never write two assignments for one job before the PM has reported on the first: the second overwrites the first.
   - `blocked`: the report names a child agent and shows its screen. Ask: approve once / deny / go to that pane. For "go to that pane": `herdr agent focus <child>`, `$OKE job defer <job>`, and continue that item only after the user says they are done. Then, in this order: write the PM's `kind: dialog` assignment ("Dialog answered for <child>." or "Dialog denied for <child>: <what was refused>."; step 2 of "Give the job to the PM"), send the key to that child yourself (`herdr agent send-keys <child> enter` or `esc`; none after "go to that pane"), then steps 3 to 6. Writing first means a compaction in between shows the item as `unsent`, never as the same dialog again. Never send a second key for the same report.
   - `legacy`: a job started before this OKe version; it has no `<pm_job_dir>/assignment.md`. Do "Give the job to the PM" from step 1 (step 1 rewrites the old PM file) with the kind its phase needs (planning: plan, working or reviewing: execute, merging: merge) and 이번 지시 "Continue this job from where it stands; read the board and children.json". No question.
   - `unsent`: you wrote that job's assignment but never sent the message (you were compacted in between). Read the assignment's `kind` and do steps 3 to 6 of "Give the job to the PM" now: set the phase that kind belongs to (see "Flow"), make sure the PM runs, send, wait. For `kind: cancel`, send first and then set `failed` (see "Giving up"). For `kind: dialog`, the key may not have gone to the child yet: read the child's screen (`herdr agent read <child>`). Send the key only if it still shows the same dialog as the PM's report; a different dialog is a new one the user has not seen, so send nothing and let the PM report it. No question.
   - `silent`: the PM did not report on that job even after a nudge. Ask: go to the PM pane (`herdr agent focus <pm>`, then `$OKE job defer <job>`) / send the same step again (steps 2 to 6 of "Give the job to the PM" with the same 이번 지시: the new seq gives the PM a fresh start) / give up (see "Giving up").
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

Plan approval question: put a short summary of the plan in the option `preview`, mention the plan file path, and offer: approve / request changes / discuss in the PM pane (then `herdr agent focus <pm>` and defer).

Merge question: summary, branch name, number of changed files. Offer: merge / hold / look myself.

## Autonomy

Read `autonomy` from `~/.oke/config.json` (default `balanced`).

| Situation | conservative | balanced | full |
|---|---|---|---|
| Plan | ask | ask, even for a small job | approve without asking, keep the file |
| Merge to the main branch | ask | ask | assign `kind: merge` without asking; conflicts still come to the user |
| Dialog (`blocked`) | ask | ask | ask |

## Rules

- Write a decision or a user instruction into the assignment or intake file the moment you get it. Your context can be compacted; the files cannot.
- After compaction, run `$OKE status` and the queue, and start a wait for each PM with open jobs.
- Do not send text starting with `/` through `herdr agent prompt` or `$OKE send` from Git Bash; it is rewritten into a Windows path.
