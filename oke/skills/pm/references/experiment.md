# PM: experiment job

`output: result` for a git job (`$OKE status`, `jobs.<job>.output`): what the job hands over is a run's outputs and what they show, not a change to keep. Example: change one line, run the simulation, read its output. Nothing is merged, and the edits never reach the main branch. Plan and execute as usual, with these differences.

Plan:
- Record the output while the job plans: `$OKE job output <job> result` (before you report the plan, on every plan and replan; it refuses once the job made something or reached its merge).
- Size `normal` or `large`, never `small`: a run can take long, and only a worker pane waits for it (the worker skill's long-run rule). `$OKE job output <job> result` raises a `small` job to `normal` (its output then shows `size`): change it in 접근 too.
- `## worktree` always names branch `oke/<job>`, at every size: the temporary edits must stay out of the main checkout. `$OKE job phase <job> working` refuses an experiment without it.
- 단위 작업: the temporary edit, the command that runs, and which output files to keep.
- 수용 기준: what the result has to show, or that the run finished and its outputs answer the question. A run whose outputs show a failure can still meet them when the job asked what happens.
- 검증 명령: the command that runs the experiment.

Execute:
- Step 1 (the worktree) as usual. No `$OKE baseline` and no `$OKE check`: they refuse a job that does not merge.
- The worker works in the worktree with `commit: no`. Its 이번 지시 starts with `output: result` (the worker then runs the 검증 명령 itself instead of `oke check`, and keeps the outputs). It reports where the outputs are (paths inside the worktree) and what they show.
- Never commit the edit, before the review or between rounds: it stays uncommitted in the worktree. The reviewer's assignment is `$OKE_HOME/jobs/<job>/<job>-rv/assignment.md`, as in **Review** (role `reviewer`, `cwd` the worktree; a reviewer pane at size large, the review subagent at normal, its answer saved to `report.md` next to the assignment: `job phase done` reads it there), but with no `commit:` or `tree:`. Size large: `$OKE spawn <that file> --split-from <worker pane>`, write the job's report as `waiting` (`on: <job>-rv`), start `$OKE wait <job>-rv --report <its folder>/report.md --seq <its seq>` in the background, end your turn. Size normal: the review subagent (Job size), its answer saved to that `report.md`. Its 이번 지시 starts with `output: result`, names the edit (`git diff` in the worktree: a reviewer pane runs it plain in its `cwd`, the review subagent with `git -C <worktree>`; new files of the edit, `??` in `git status --short`, listed by name to read whole) and the output files to read, and asks: do the outputs meet the 수용 기준, and did the run use the planned edit and nothing else?
- `verdict: pass`: close the worker and reviewer (`$OKE close <pane> <pane>`). Never commit, never run `$OKE job merge`, and never remove the worktree: it holds the outputs and stays until the user says to remove it. `changes`: a new round as usual. The orchestrator's `job phase done` refuses until the reviewer passed the latest result.
- Log the wiki (`$OKE wiki write` or `$OKE wiki log --job <job> --note none`), then report `done`: the result (what the outputs show) in 요약, the output files and the worktree's path.
