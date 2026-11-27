# Orchestrator: wiki curation

When `job archive` prints a `curate:` line, or the user asks to clean up a wiki, give that wiki's PM `kind: curate`. It needs a job: `$OKE job new <name> --pm <pm>`, no path, no PM question needed, no interview needed: run `$OKE interview <job> --goal 1 --constraints 1 --criteria 1`, then phase `planning`. The PM ends it as an answer: its `done` report is an `answer` queue item.

The shared wiki you curate yourself, in a job of its own that ends as an answer:

1. `$OKE job new <name> --pm <any PM> --shared-wiki` (your own job: no PM works on it, sees it or waits for it; the PM is named only for the job's wiki log), `$OKE interview <job> --goal 1 --constraints 1 --criteria 1`, `$OKE job phase <job> planning`, then `$OKE job answer <job>` (without it the wiki writes and `job phase done` are refused). Write no assignment for it.
2. Do the steps of `${CLAUDE_PLUGIN_ROOT}/skills/pm/references/curate.md`, each `$OKE wiki` command with `--shared --job <job>`, ending with `$OKE wiki log --shared --job <job> --note "curated: <what changed>"`.
3. `$OKE job phase <job> done`, then `$OKE job archive <job>`. Tell the user in one line what changed.
