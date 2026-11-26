# PM: writing the wiki

- `index.md`: one line per page: a link and what the page answers, the trigger first ("before running the tests: ..."). Workers read it first and open only the pages they need.
- `<topic>.md`: facts about the places you work in that the next worker needs and cannot see quickly in the code: build and test commands, conventions, traps.
- `log.md`: one line per change; `oke wiki` writes it.

How to change a page: `$OKE wiki read <page> --job <job>` prints a JSON line with `base`, then the page. Write the whole new page to a file in your folder and run `$OKE wiki write <page> --job <job> --from <file> --base <base> --note "<what changed>"` (`--base new` for a new page). A refused write says why: fix that and write again.

Write a fact only when this job's checked work shows it (`oke wiki write --job` refuses before the merge is verified, or the review of a job that never merges (a folder job, an experiment) passed; research may write; a job you will ask to end with no change gets only its `wiki log` line, before the question). Each line is one rule plus one clause of why, not a story:

- Keep exact names, commands, flags, paths (name the folder or checkout a fact is about: the next planner finds jobs' paths here) and numbers: a rule made vague teaches nothing.
- No job names, dates, ticket numbers or quotes of the user.
- One fact lives in one place: this wiki or the shared one, never both.
- The same lesson twice is one rule: strengthen the line that is there. `oke wiki write` refuses a line that rewords another page's line; pass `--dup-ok` only when they really differ.
- Fix a wrong line in place. Never append "UPDATE: actually ...".
- Do not record: a failure of one machine's setup (command not found, a missing package), a transient error, a one-off task, a claim that a tool is broken, or an approach that did not work in the end.
- A page has a cap (3000 characters, `index.md` 2000). Over it, merge or drop lines first; an old page can go to `oke wiki archive <page> --reason ..` (never delete a page).

Pages marked `wrong` in a report's `## wiki` section come first: correct them from what the worker found, or archive them.
