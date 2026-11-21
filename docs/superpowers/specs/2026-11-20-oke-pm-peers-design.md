# OKe: PMs talk to each other about cross-repo impact

Date: 2026-11-20. Branch: `pm-peers`.

## Why

Two jobs in different repositories can affect each other. Example: repo A changes an API that repo B calls. Today each PM sees only its own repository and jobs. The idea OKe started from (an orchestrator, a PM per service, a shared dev-wiki) has the PM check "does this affect other services?" before it plans, and the PMs talk to each other about it.

The PM of a repository knows that repository best. So the PM that plans asks the other PMs, and each one answers from its own code and open jobs. Reading another job's plan file is not enough: a repository with no open job on the topic can still call the API that changes.

Prior art (checked 2026-11-20): Overstory and Gas Town route cross-repo matters through the top agent. Repowire lets agents in different repos ask and notify each other directly. An agent-teams write-up lets per-repo agents negotiate contracts directly, while the lead orders the merges. OKe takes the direct route and shows the talk on the board.

## Decisions (user interview, 2026-11-20)

1. Who is asked: the PM of every other repository that has an open job, whether that PM is alive or dead right now. A dead PM with open jobs is revived by the orchestrator's wait and gets the question from its inbox. Repositories with no open job are not asked.
2. The asking job waits: the plan is not reported until every asked PM answered. The user approves a plan that already reflects the answers.
3. When an answer says the answering PM's own open job must change, each PM changes its own job. A changed plan goes to the user for approval again. If the PMs cannot agree, both report `question` to the user.
4. Merge order: when the talk shows that job B must merge after job A, B's plan header gets `after: A`. `oke job merge B` refuses until A is merged and verified.
5. The user sees the talk on the board, in the job's step line. The orchestrator gets no copy of the messages. It learns the outcome from the plan at approval time, from reports when the user must decide, and from `peers.md` when the user asks.
6. When work shows an impact the plan did not have, the PM stops the worker and asks again, with the same steps. A changed plan goes to the user again.
7. An asked PM that goes idle without answering is reminded once. The second time, the asked PM's wait returns `peer_no_answer` (once per ask) and the user decides.

## Flow

1. While writing plan.md, the PM lists what its plan changes that other code may use: APIs, schemas, CLI flags, config keys, file formats, shared services. Nothing like that: no ask; the section says 없음.
2. `$OKE peer ask <job> "<what my plan changes>"`. OKe picks the PMs (decision 1), queues the question in each inbox, records who must answer, and appends to `peers.md`. It prints the asked PMs, or `none` when there is no other repository with an open job.
3. The PM ends its turn. Answers arrive in its inbox as `OKe inbox:` messages, like any other mail.
4. A PM that gets a question reads its repository and its open jobs (an `Explore` subagent for a wide search, never a worker pane), then `$OKE peer answer <job> "<no impact | impact: ...>"`. The answer goes to the asking PM's inbox. The answering PM is removed from the waiting list. If its own open job must change, it changes that job's plan and reports it (decision 3).
5. When nobody is left on the waiting list, the asking PM finishes plan.md with the `## 다른 레포 영향` section and reports `done` as now.
6. If an answer needs a reply ("can you keep the old field for one release?"), the PMs use `peer ask`/`peer answer` again on the same job. Each round waits as above.

The same steps run during work (decision 6): the PM gives the worker a "stop and wait" instruction (a new seq), asks, and continues or re-plans after the answers.

## Commands

### `oke peer ask <job> <text>`

- The caller must be the PM of `<job>`'s repository (whoami). Refuse otherwise.
- Targets: for every other repository key with an open job (`open_jobs(state, key)` non-empty) and a recorded PM agent: that PM.
- Queues `"<pm>: peer ask (job <job>, round <n>): <text>. Answer with: oke peer answer <job> ..."` to each target. The sender is the asking PM.
- Writes `$OKE_HOME/jobs/<job>/peers.json`: `{"round": n, "waiting": {"<pm>": {"repo": key, "nudged": 0}}}`. A new ask on the same job starts round n+1 and replaces the waiting list.
- Appends to `$OKE_HOME/jobs/<job>/peers.md`: `## round <n> (<time>)`, then `- ask (<pm>): <text>`.
- Prints `{"round": n, "asked": [...]}`. With no target: `{"round": n, "asked": []}` and no waiting list.
- Under `locked()`, like every state change.

### `oke peer answer <job> <text>`

- The caller must be a PM listed in `<job>`'s waiting list. Refuse otherwise (`not asked`).
- Queues `"<job>: peer answer from <pm> (round <n>): <text>"` to the PM of `<job>`.
- Removes the caller from `waiting` and appends `- answer (<pm>): <text>` to `peers.md`.
- Prints `{"waiting": [...]}` with the PMs still waiting.

### `oke job merge` (change)

- Reads `after:` from the job's plan header (comma-separated job names).
- For each named job: merged and verified (`merge_state` verified, or phase done) passes. An archived job passes only if its ledger has a verified merge. A failed or cancelled job refuses with `after <job> was cancelled: ask the user`. Anything else refuses with `waiting for <job> to merge`.
- The refusal exits non-zero before any git work.

### Waiting list upkeep

- The waiting list is read through one helper that drops every PM whose repository has no open job left (done, archived, cancelled or held: `open_jobs` leaves held jobs out, and nothing revives or watches a PM that has only held jobs). No hook in archive, cancel or hold is needed.
- When a job is cancelled, every job whose plan says `after: <it>` gets a queued notice to its PM: `"Job <x>: after-job <it> was cancelled. Report question to the user."`

### `oke wait` (change, PM waits)

- The check runs in the wait of the asked PM, which already looks at that PM. An asked PM that is idle (confirmed like any idle verdict) with an empty inbox, and still on a waiting list, has not answered: its question was delivered, because an idle PM with mail gets the mail first. First time: the wait queues a reminder `"Answer the peer ask of job <job> (round <n>): oke peer answer <job> ..."`, sets `nudged: 1` and keeps waiting. Second time: it returns `peer_no_answer <job> <pm>`.
- While a job's waiting list is not empty, an idle asking PM without a report for that job is waiting, not stopped short: no `idle_no_report` for that job.
- The orchestrator handles `peer_no_answer` like a `question`. It asks the user: "proceed as no impact", "wait", or "cancel the job". It sends the choice to the asking PM as `kind: answer`.

## Board

The step line of a job shows a wait next to its step:

```
plan (wait PM repo-b, repo-c) › approve › work › review › merge › verify
plan › approve › work (wait PM repo-b) › review › merge › verify
plan › approve › work › review › merge (wait a1) › verify
```

- Peer waits come from `peers.json` `waiting` (the repository's short name).
- `merge (wait <job>)` shows while the job is in `awaiting_merge` or `merging` and its `after:` job does not pass the merge rule.
- `oke board` (the JSON the mod reads) gets `peer_wait: [<repo>...]` and `after_wait: [<job>...]` per job; `register.tsx` draws them.

## Skills

PM (`skills/pm/SKILL.md`):
- `kind: plan`: the list of contract changes, `peer ask`, waiting for the answers, the `## 다른 레포 영향` section (who was asked, each answer, what changed in the plan, `after:` if any). Add the section to the plan template.
- A new "Peer asks" section: how to answer, how to change your own job after an impact answer (re-plan, report for approval), and when to give up and report `question`.
- Work: a worker report or review that shows a new cross-repo change. Stop the worker, ask, then continue or re-plan.
- Inbox: messages from another PM (`peer ask`, `peer answer`) are expected now.

Orchestrator (`skills/orchestrator/SKILL.md`):
- The `peer_no_answer` wait row.
- Plan approval: show the `## 다른 레포 영향` section in the popup when it is not 없음.
- When the user asks what the PMs discussed: read `$OKE_HOME/jobs/<job>/peers.md`.

## Not in this version

- Asking a repository that has no open job: it has no PM to answer.
- A copy of the messages to the orchestrator.
- Broadcast notices without a question (`notify`): every message is an ask that expects an answer.
- Time limits: OKe judges "no answer" by idle state, never by time.

## Tests

Unit tests (`oke/bin/test_*.py`, with the fake runtime):
- `peer ask` picks every other repository with an open job, alive or dead PM, and skips its own repository and repositories with no open job or only held jobs.
- `peer ask` from a non-PM refuses. `peer answer` from a PM not on the list refuses.
- The waiting list empties as answers arrive. A new round replaces it.
- A target's last job archived or cancelled drops it from the list.
- `job merge` with `after:` refuses until the named job is verified, and refuses with the cancel message for a cancelled job.
- The wait reminds once and then returns `peer_no_answer`.
- `oke board` shows `peer_wait` and `after_wait`.

Mod test: the step line draws `(wait ...)`.

E2e on Sonnet with an isolated OKE_HOME:
- Two toy repositories: `api` changes a function's return shape, and `app` calls it.
- Start an `app` job, then an `api` job. Check that the `api` PM asks, the `app` PM answers "impact", the `app` PM re-plans for approval, `after:` holds the `app` merge until `api` is verified, and the board shows the waits.
