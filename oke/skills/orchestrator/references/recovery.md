# Orchestrator: disconnected jobs at start

For each job with class `disconnected`, and each entry of `orphan_job_dirs`, ask: resume / archive / hold. Ask once per PM, not once per job: the jobs of one repository share their PM. Jobs with class `held` are not asked: name them in one line ("On hold: <jobs>. Say so to continue or drop one.").

- resume: `$OKE revive <pm agent> --reset`. It restarts the PM in its own session and tells it to re-read its assignments. `revived` or `alive`: go on. `unknown` (never spawned): spawn it as in "Give the job to the PM". `failed`: show the last line of `$OKE_HOME/events.log` to the user.
- hold: `$OKE job hold <job> --reason "<why, one line>"` for each of them (see `${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/references/hold.md`). An `orphan_job_dirs` entry has no job to hold: leave it.
- archive: `$OKE job cancel <job>`, then `$OKE job archive <job>`; an `orphan_job_dirs` entry has no job to cancel: only `job archive`. If `job cancel` refuses (its merge is under way or in main), tell the user why and leave that job. Never delete a worktree or branch. Report the worktree path and `git -C <worktree> status --short | wc -l`.
