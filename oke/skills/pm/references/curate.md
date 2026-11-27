# PM: kind: curate

The orchestrator asks every 10 jobs, or when the user asks. Nothing in your jobs' paths changes. First `$OKE job answer <job>`: the job ends with its report and no plan, and your wiki writes count. `$OKE wiki status --job <job>` lists each page with its size, `uses`, `idle_days`, `wrong` and `state`. Change pages as `${CLAUDE_PLUGIN_ROOT}/skills/pm/references/wiki.md` says. In this order:

1. Pages with `wrong` above 0: fix them or archive them.
2. Pages that say the same thing: write the merged page with `--merging <other page>` for each page merged in, then archive those (`--reason "merged into <page>"`).
3. `$OKE wiki prune --job <job>` lists the pages no worker report marked as read for 30 days (a page you or a work subagent rely on may be among them); judge the list, `$OKE wiki pin <page> --job <job>` the ones that must stay, then `$OKE wiki prune --job <job> --apply`.
4. Make `index.md` match what is left.
5. `$OKE wiki log --job <job> --note "curated: <what changed>"`.

Report `done` with what changed.
