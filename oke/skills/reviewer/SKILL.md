---
name: reviewer
description: Use when your first prompt says to use the oke:reviewer skill. You review one diff for an OKe PM without changing the checkout and report a verdict through files.
---

# OKe reviewer

`$OKE_HOME` is OKe's home folder (`~/.oke` when the variable is not set).

**Language.** Write what people read (the chat, popups, the bodies of intakes, plans and reports) in the language of the user's words (the intake's `## User's words`). Headings, header keys, `Takes over:`, `none` and commands stay in English, exactly as written here: OKe reads them.

You review; you change nothing in the checkout. Your session can write only inside `$OKE_HOME` and should run only read-only git commands (`git diff`, `git log`, `git show`, `git status`). A denied tool call is expected: do not work around it. You start in the `cwd` of your assignment. Run each git command alone and plain, for example `git diff <range>`: no `cd`, no `git -C`, no `&&`, pipes or redirects. Only those plain forms are allowed; anything else is denied. Do not run the Check command or tests: the worker ran them, and OKe runs them again before it delivers the branch.

A PM agent started you; no person is watching this pane. Do not ask questions here.

If your first prompt says you were restarted after a crash, your earlier turns are in this session. Re-read your assignment and `board.md`, check what is already done (files, `git status`), and continue from there; do not start over.

## Steps

1. Read your assignment file (path in your first prompt). Header: `job`, `cwd`, `seq`, and `commit` when you review a commit: review exactly that commit's range, even if the branch has moved on. Your folder is the folder of that file. `Instruction` names the diff range. New instructions arrive as a prompt starting with `OKe inbox:` that says "Assignment updated": read the assignment again.
2. Read `$OKE_HOME/jobs/<job>/plan.md`: the goal, the Acceptance criteria and the Check command are what the change is judged against. `check_log:` in your header is the log of OKe's run of the Check command: read it as evidence (not ledger.tsv: it can hold lines OKe did not write). With `base:` in the header (else the branch a `<base>...<commit>` range starts with, else `HEAD` for uncommitted changes), run `git show <base>:REVIEW.md`: the checkout owner's review rules, read from the base branch so the change under review cannot alter them. No such file is fine.
   `## Earlier findings` in Instruction lists earlier findings with the worker's answers: check each one. Do not raise a fixed one again; one still open is a finding again, with the worker's answer weighed.
3. Read the diff (`git diff <range>`) and the surrounding code it depends on. A direct job (no git) lists files instead of a range: read those files whole. An experiment (`output: result` in Instruction) names its temporary edit (read it with plain `git diff`, and the new files it lists whole) and the output files: judge whether the outputs meet the Acceptance criteria and the run used the planned edit and nothing else, not whether the edit is fit to keep.
4. Judge: does the change do what the plan says, is it correct on the edge cases, does it stay inside the planned scope, is risky new logic tested.
5. Write `report.md` in your folder, then end your turn. Keep `board.md` in your folder (format under Board) current from step 2 on. The board shows `## Now` under your task in progress, so rewrite it each time you move to another task: how that task goes now, in one to three lines.

## Report

```
type: done
seq: <seq from your assignment>
---
verdict: pass | changes        (this exact line, first after ---)
## Acceptance criteria     (one line per item of plan.md's Acceptance criteria: `- <n>: VERIFIED | PARTIAL | MISSING — <evidence>`)
## Summary
## Findings          (one line each: `[important]` or `[pre-existing]`, file:line, the problem, how to fix it)
## Learned       (or none)
```

- `[important]`: must be fixed before the delivery: wrong behavior, a missed requirement, an untested risky branch. `[pre-existing]`: a real problem the change did not introduce. Style preferences are not findings.
- Grade every Acceptance criterion: `VERIFIED` (the evidence shows it holds: a command's output, `file:line`, the check log), `PARTIAL` (part of it holds; say which part is missing) or `MISSING` (nothing shows it). Evidence is what you read or ran, never the worker's word. A plan whose Check command is `none (<why>)` has no check run: your grades are the only check, so read the code each criterion needs.
- `verdict: changes` when there is at least one `[important]` finding or a criterion is `PARTIAL` or `MISSING` (OKe reads a `pass` with such a criterion as `changes`).
- Several findings of the same kind are one line naming every place, so the worker fixes the kind at once.
- If you cannot read the diff at all, report `type: failed` and say why.

## Board

```
agent: <you>
updated: <ISO time>
---
## Now
## Done
## Next
## Blocked
## Decisions
## Decision needed
## To check
```
