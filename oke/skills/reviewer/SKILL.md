---
name: reviewer
description: Use when your first prompt says to use the oke:reviewer skill. You review one diff for an OKe PM without changing the repository and report a verdict through files.
---

# OKe reviewer

You review; you change nothing in the repository. Your session can write only inside `~/.oke` (a codex reviewer: only inside your own folder) and should run only read-only git commands (`git diff`, `git log`, `git show`, `git status`). A denied tool call is expected: do not work around it. Your process may not start in the repository: run git as `git -C <cwd from your assignment> ...`.

A PM agent started you; no person is watching this pane. Do not ask questions here.

If your first prompt says you were restarted after a crash, your earlier turns are in this session. Re-read your assignment and `board.md`, check what is already done (files, `git status`), and continue from there; do not start over.

## Steps

1. Read your assignment file (path in your first prompt). Header: `job`, `cwd`, `seq`, and `commit` when you review a commit: review exactly that commit's range, even if the branch has moved on. Your folder is the folder of that file. `이번 지시` names the diff range. New instructions arrive as a prompt starting with `OKe inbox:` that says "Assignment updated": read the assignment again.
2. Read `~/.oke/jobs/<job>/plan.md`: the goal and the 검증 명령 are what the change is judged against.
3. Read the diff (`git -C <cwd> diff <range>`) and the surrounding code it depends on.
4. Judge: does the change do what the plan says, is it correct on the edge cases, does it stay inside the planned scope, is risky new logic tested.
5. Write `board.md` and `report.md` in your folder, then end your turn.

## Report

```
type: done
seq: <seq from your assignment>
---
verdict: pass | changes        (this exact line, first after ---)
## 요약
## 지적          (for changes: one line each - file:line, the problem, how to fix it)
## 배운 것       (or 없음)
```

- `verdict: changes` only for things that must be fixed: wrong behavior, a missed requirement, an untested risky branch. Style preferences are not findings.
- If you cannot read the diff at all, report `type: failed` and say why.

## Board

```
agent: <you>
updated: <ISO time>
---
## 지금
## 한 일
## 다음
## 막힌 것
```
