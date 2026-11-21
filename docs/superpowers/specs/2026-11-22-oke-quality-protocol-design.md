# OKe quality protocol: acceptance criteria, baseline, code-run checks

Date: 2026-11-22. Status: design agreed in conversation 2026-11-22; awaiting the user's review of this file.

## Goal

A job merges only when code, not an agent's word, has run the plan's check on the
reviewed commit and found no new failure, and a reviewer has judged every
acceptance criterion. Failures that main already had before the work started do
not block the job.

The failure comparison follows oh-my-claudecode's `omc ralph verify`
(`src/hooks/ralph/feedback-baseline.ts`, v5.8.6). Two guards go further than OMC:
the verify command is part of the criteria hash, and the merge gate requires a
passing `oke check` record.

## Out of scope

- Research jobs and `direct` jobs (no git): they have no merge. They keep today's flow.
- No time limit on any check. A 5-hour simulation is normal. (OMC times out at 10 minutes.)
- No user override for a false "new failure". The worker fixes it, or reports `blocked`.
- No config for failure words. The line filter keeps everything that is not known noise.
- Forgery by the PM (item 8, a later design).

## Flow

1. **Plan.** The PM copies the interview's `## 성공 기준` into a new plan.md section
   `## 수용 기준`, as a numbered list. The user sees it at plan approval.
   The `## 검증 명령` section's first line is the command in backticks; `oke.py`
   reads the first backticked span of that section as the command.
2. **Criteria revision.** `criteria_rev(job)` = `sha256:` + sha256 of the text of
   `## 수용 기준` plus the parsed verify command. It is computed from plan.md each
   time it is needed; nothing stores it. A change to either one changes it.
3. **Baseline.** Before the worker starts, the PM runs `oke baseline <job>` in the
   background and waits like any long command (`waiting`, `expect:`).
   - It runs the command in the job's fresh worktree, or in the main checkout when
     the job has no worktree.
   - It writes `jobs/<job>/baseline.json` (format below).
   - Running it again is allowed (OMC does the same). The old file moves to
     `jobs/<job>/baseline.history/<time>.json`, at most 20 kept, and the output
     says how many signatures were added and removed.
4. **Worker check.** The worker runs `oke check <job>` instead of the command itself.
   - `oke.py` runs the command in the worker's cwd, diffs against the baseline,
     and writes one ledger row (format below). Full output goes to
     `jobs/<job>/check-<n>.log`.
   - Exit 0: no new failure. Exit 1: new failures, printed one per line. Exit 2: no
     baseline, or the command could not run (126/127).
   - The worker reports `done` only after exit 0. On exit 1 it fixes the code.
     On exit 2 it reports `blocked`.
   - Small jobs (no worker pane): the PM runs `oke check` after its work subagent answers.
5. **Review.** The reviewer writes one line per criterion, `- 1: pass — <evidence>`,
   and gives `verdict: pass` only when every line is pass. The review assignment
   header gets `criteria_rev:` next to `commit:`; the PM copies it from the
   `oke check` output.
6. **Merge gate.** `oke job merge` keeps every check it has today and adds two:
   - the review assignment's `criteria_rev` equals the current `criteria_rev(job)`;
     otherwise "the criteria or the 검증 명령 changed since the review: review again";
   - the ledger has a `check pass` row for the reviewed commit with the current
     `criteria_rev`; otherwise "no passing `oke check` on <commit>".
7. **Post-merge check.** `oke job verify <job>` reads the command from plan.md
   (a command argument is still accepted, and refused when it differs).
   - If the merge commit's tree equals the tree of the commit with the passing
     check, it skips the run and writes `verify pass` with
     `same tree as <commit>`. This is the usual case: main did not move.
   - Otherwise it runs the command and uses the same diff against the baseline.
     A new failure rolls back as today (exit 3).
8. **Old failures.** Signatures that only the baseline has are not a failure.
   The PM's done report and the merge question show "main already failed N lines
   (warning)" when the baseline has any.

## Failure signatures (port of OMC `signatureLines`)

Applied only when the command exits non-zero. Exit 0 means no signatures, whatever
the output says.

1. Strip ANSI colour codes. Trim each line; skip empty lines.
2. Replace volatile parts with `<v>`: durations (`0.42s`, `12 ms`), `duration: N`,
   ISO timestamps, `/tmp/...` paths, Windows temp paths, hex runs of 8+ characters,
   the pid in `(node:123)`, pytest's `(0:03:14)` after `in Ns`.
3. Collapse whitespace. Skip lines longer than 500 characters.
4. Drop known noise, keep every other line:
   - TRACE/DEBUG/INFO/NOTICE log lines without a failure word
   - `npm warn`, `> pkg@ver`, `> tsc|vite|vitest|jest|node|npm|...` lines
   - `RUN v1`, `Duration`, green `Test Files`/`Tests` summaries
   - source frames (`12| code`, `| ^`)
   - passing marks (`✓`, `√`, `ok N ...`), TAP plans (`1..5`), `# pass N`, `# fail 0`
   - progress lines (`running`, `collecting`, `compiling`, `at ...`) without a failure word
   - pytest dot lines and `N passed in <v>` summaries
   - Failure word: `fail`, `failed`, `failure`, `failing`, `...Error`, `...Exception`, `✗`, `×`.
5. De-duplicate, keep at most 200, sort.

## Diff rule (port of OMC `diffAgainstBaseline`)

New failures are:
- `<exited with code N>` when the baseline exited 0 and now it does not;
- `<command became unrunnable>` when it now exits 126/127 and did not before;
- every signature absent from the baseline.

Signatures only in the baseline are "resolved": printed as a count, never a failure.

## Formats

`jobs/<job>/baseline.json`:

```json
{"version": 1, "recorded_at": "2026-11-22T10:00:00", "criteria_rev": "sha256:...",
 "commit": "<sha the baseline ran on>", "exit": 1, "unrunnable": false,
 "signatures": ["FAILED tests/test_old.py::test_x - AssertionError"]}
```

A baseline whose `criteria_rev` differs from the current one (the command changed)
makes `oke check` exit 2: "the 검증 명령 changed: run `oke baseline <job>` again".

Ledger row written by `oke check` (the existing six columns):
`check` = `check`, `result` = `pass` or `fail`, `commit` = the worker's HEAD,
`detail` = `<criteria_rev> new=<n> resolved=<n> log=<path>`. `check` joins
`OKE_CHECKS`, so `oke ledger add` refuses to forge it.

A check on uncommitted changes (a small job without a worktree) records the
`git write-tree` of the working tree as `tree:<sha>`, matching how the review
assignment names such changes today.

## Changes

- `oke/bin/oke.py`: `criteria_rev`, `plan_command`, `signature_lines`,
  `diff_baseline`, `cmd_baseline`, `cmd_check`; two checks in `merge_job`;
  `verify_locked` uses the plan command, the tree skip and the diff;
  `check` in `OKE_CHECKS`.
- `oke/skills/pm/SKILL.md`: plan template (`## 수용 기준`, backticked command),
  baseline before the worker, `criteria_rev:` in the review assignment, the
  warning line, `oke check` for small jobs.
- `oke/skills/worker/SKILL.md`: run `oke check`, report rules for exit 0/1/2.
- `oke/skills/reviewer` board template and `oke/agents/reviewer.md`: per-criterion lines.
- `oke/skills/orchestrator/SKILL.md`: the plan approval shows `## 수용 기준`.
- `README.md`, `README.ko.md`: the new commands and the merge rule.

## Tests

- `signature_lines`: pytest, jest/vitest, TAP, tsc and a plain script's output;
  volatile values give equal signatures across two runs.
- `diff_baseline`: green to red, red with a new line, a resolved line, unrunnable.
- `oke baseline` / `oke check` in the existing fake-repo fixtures: history kept,
  stale `criteria_rev` refused, ledger row written.
- Merge gate: refused without a check row, with a stale review `criteria_rev`;
  allowed with both.
- `job verify`: tree skip when main did not move; diff run when it did; rollback
  on a new failure only.
