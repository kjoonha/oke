# Orchestrator: writing the shared wiki

- `index.md`: one line per page: a link and what the page answers, the trigger first.
- `<topic>.md`: one topic per page.
- `log.md`: one line per change; `oke wiki` writes it.

Change it only through `$OKE wiki ... --shared`, the same way a PM changes its wiki (see `${CLAUDE_PLUGIN_ROOT}/skills/pm/references/wiki.md`: `wiki read`, then `wiki write --from <file> --base <base> --note ..`). Write that page file under `$OKE_HOME/tmp/`. A guard lets you write files only in OKe's home (never a wiki: those change through `$OKE wiki`) and in your own memory folder. Add what a PM report's 배운 것 (공용) states and you believe, and what the user tells you to keep; add `--job <job>` for a job's lessons. Fix a page that turned out wrong. No repository names or paths: facts about one repository stay in that repository's wiki, which its PM keeps (`wiki write --shared` refuses a repository's path). The PM's rules for what to write hold here too.
