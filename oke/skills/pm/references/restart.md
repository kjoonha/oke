# PM: after a restart or compaction

Your folder may already hold `board.md` and `children.json` from before. Read both, and every job assignment your prompt names, before you act. A job on hold stays as **On hold** in SKILL.md says.

For each child in `children.json` whose `job` is an open job, decide in this order:

1. Its `report.md` has the `seq` of its `assignment.md` and type `done`, `question` or `failed`: the child finished that step. Do not start it again. If that job's report is `waiting` on a subagent, the next step already runs (or must be called again): handle it as the subagent paragraph below says, not here. Otherwise handle that report as in the wait table of SKILL.md (a finished reviewer stays finished).
2. It is in `$OKE agents`: it keeps its work. Write that job's report as `waiting` (unless it already has the current seq and type `question`, `blocked` or `failed`: leave that one) and start `$OKE wait <child> --report <its folder>/report.md --seq <seq in its assignment.md>`. If it is at a dialog the wait says `blocked`; handle it as in the wait table.
3. Otherwise: `$OKE revive <child>` (it restarts the child in its own session and tells it to re-read its assignment), then wait as in 2. Only this restart of yours, and a "back from hold" message, revive children; nothing else revives them while you run.

Then, for each open job whose assignment `seq` is higher than the `seq` of its `pm/report.md`: you had not acted on that instruction yet. Act on it now. And for each job whose report is `waiting` on another job that is no longer open (a merge that waited): start it now. A report `waiting` on a subagent (`on: ... subagent`): what sent you here decides. The prompt "You were restarted after a crash" (this very prompt, not one in an earlier summary): that subagent ended with your old session and its answer will not come; call it again for that step and write the new id in the report. The message "OKe: context was compacted": it still runs in this session; leave it, its completion notification comes as usual. For each job in phase `merging`, `$OKE ledger show <job>`: `merge: unverified` means its verification never finished; run step 3a of the merge (`${CLAUDE_PLUGIN_ROOT}/skills/pm/references/merge.md`) again.

Never `$OKE spawn` a name that is already in `children.json`. If `git -C <the job's path> worktree list` already shows `oke/<job>`, use that worktree. Never create `oke/<job>-2` for a job that already has one.
