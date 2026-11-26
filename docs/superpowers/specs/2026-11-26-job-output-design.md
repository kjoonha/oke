# Job output: what a job hands over, apart from where it works

## Problem

A git job could only end with a verified merge. Jobs whose product is not code (run a simulation on a
temporary edit and look at its outputs, or a job that turns out to need no change) could end only by
`cancel`, recorded as failed, which broke every `after:` waiter. Research and folder jobs were each a
special case of "git means merge", and every rule (start gate, merge gate, hold, done, wiki) carried
`size == research or direct` checks.

## Model

Two separate properties of a job.

**output**: what the job hands over, and who accepts it.

| output | accepted by | done needs |
|---|---|---|
| `code` | the merge | a verified merge (as before) |
| `files` | the reviewer | the folder's changed files (a direct job: path outside git) |
| `result` | the reviewer, or the user for a no-change end | a passing review of the result, or the user's no-change acceptance |

**place**: where it works: a worktree, the main checkout (no worktree), a folder outside git, or
nowhere (research reads only).

Allowed pairs:

- `code`: worktree or main checkout (git paths).
- `files`: folder outside git (set by `job path`, as `direct` was).
- `result`: worktree (an experiment: edits stay in the worktree and never reach the main branch), or
  no place (research: `size: research`, the PM's own turn, no worker, no plan approval).
  A result job never works in the main checkout: its edits would land in the user's checkout.

One done rule: the output was accepted.

- Every rule that asked "does it merge" asks `output == code`. A job of another output never merges:
  `after:` holds its start instead of its merge, and done finishes it for its waiters.
- `job phase done` for a `result` job with a worker needs the latest review report's `verdict: pass`
  (or the no-change acceptance).

## Setting the output

- An experiment is `normal` or `large`, never `small`: a run can take long, and only a worker pane waits for one.
- The planner writes `output: code | result` at the top of 접근, beside `size:`. A folder job is `files`
  by its path. A research job is `result` by its size.
- The PM runs `$OKE job output <job> <code|result>` on every plan and replan of a git job (before the approval).
  `result` needs a worktree (checked at `job phase working`: the plan's `## worktree` names
  `oke/<job>`).
- The orchestrator's plan question shows the output. For `result`: "no merge; the edits are thrown away
  with the worktree; the worktree stays until you say to remove it".

## No-change end

A `code` job that, while it works, turns out to need no change (already fixed, cannot reproduce, not
needed):

1. The PM reports `question` whose 질문 starts with `no-change:`, with what it checked and that nothing changed.
2. The orchestrator asks the user: end with no change / other instruction.
3. Yes: `$OKE job output <job> result --no-change` (orchestrator only). OKe refuses unless nothing
   changed: a worktree's branch has no commit beyond the job's base and its tracked files are clean; a
   main-checkout job that worked has no tracked change (one that never worked owns none of the main
   checkout's changes). A folder job outside git cannot be compared: the user's yes is the check, and its
   output stays `files`. It records the user's acceptance in the ledger (`accept
   no-change`), then `job phase done` is allowed.

When the PM wrote no wiki line for the job, `--no-change` writes `ended with no change`, so `job phase done`
never has to go back to the PM.

Archive removes a no-change job's clean worktree and its branch (`git branch -d`); one it cannot remove
is listed like an experiment's and removed with `job clean`.

## Experiment job (result in a worktree)

- Plan approval follows `autonomy` as for any job.
- The worker makes the temporary edit in the worktree, runs the command, and reports where the outputs
  are (paths inside the worktree) and what they show. `commit: no`.
- The reviewer checks the outputs against the plan's 검증 (what result counts as answered), and passes
  or asks for changes. Pass: the PM reports done.
- The worktree is kept at done; the done report and the orchestrator's message name its path. It is
  removed only when the user says so: `$OKE job clean <job>` (through the runtime: herdr closes its workspace).
- The temporary edit is thrown away with the worktree.

## Merge approval

Outside `full`, the user approves every merge separately, whatever the size. The `small` plan question
offers approve / request changes (no "approve and merge"). `job phase merging` no longer accepts a
move straight from `working` or `reviewing`.

## Compatibility

Jobs saved before: no `output` field. `output(info)` derives it: research → `result`, direct → `files`,
else `code`.
