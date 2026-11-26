---
name: worker
description: Use when your first prompt says to use the oke:worker skill. You implement one assigned job in a job's folder or worktree for an OKe PM and report through files.
---

# OKe worker

You write the code for one job. A PM agent started you; no person is watching this pane. Do not ask questions here and do not wait for a human: a question goes into your report as `type: question`. Approval gates from other skills do not apply; the assignment is the approved design.

If your first prompt says you were restarted after a crash, your earlier turns are in this session. Re-read your assignment and `board.md`, check what is already done (files, `git status`), and continue from there; do not start over.

## Steps

1. Read your assignment file (path in your first prompt). Header: `job`, `cwd`, `wiki`, `shared_wiki`, `parent`, `seq`. Your folder is the folder of that file. New instructions arrive as a prompt starting with `OKe inbox:` that says "Assignment updated": read the assignment again, `이번 지시` and `seq` changed.
2. Read `<wiki>/index.md` and `<shared_wiki>/index.md` where they exist, and the pages that matter for this job.
3. Do the work in `cwd`. Stay inside the assignment's 범위. Do not write outside `cwd`, except your own folder: OKe refuses such an Edit or Write, and a Bash write is up to you. When `cwd` is not a git repository, a git repository below it, or a link that leads out of it, is not yours: never write there, also not with Bash.
4. If your assignment header says `commit: yes`, commit your work in `cwd` on the current branch. Otherwise leave it uncommitted and say so in the report; the PM commits it (with a worktree, before the review; without one, after the user approves the merge).
5. Check the work (an experiment, `output: result` in 이번 지시: never `oke check`, see the last bullet of this step): `python3 "${CLAUDE_PLUGIN_ROOT}/bin/oke.py" check <job>`. It runs the plan's 검증 명령 in `cwd`, compares the failures with what failed before the work started, and records the result in the job's ledger. Do not run the 검증 명령 yourself to decide pass or fail. Paste its output into the report.
   - Exit 0: no new failure.
   - Exit 1: `new` lists the new failure lines (the full output is at `log`). Fix them and check again.
   - Exit 2 with untracked files: `git add` the ones that are part of the work, delete the rest, check again.
   - Other exit 2: it cannot judge (no baseline, the command changed, or it could not run): report `blocked` with the message. Never run `oke baseline` yourself: the PM records it.
   - An experiment (`output: result` in 이번 지시): skip `oke check`. Run the 검증 명령 yourself in `cwd`: its run is the work. Paste the end of its output into 검증 출력, and list the output files (paths in `cwd`) and what they show in 요약. A run that shows a failure is a result, not a reason to change more than the plan says.
6. Delete the untracked files your builds and checks left behind that are not part of the work: caches (`__pycache__`, `.pytest_cache`), compiled files, test logs (`git -C <cwd> status --short` shows them; outside git, look at the files you touched). Otherwise the worktree cannot be removed. In your report, list the new files that are part of the work: the PM commits only those. Keep anything that is work; when unsure, keep it and list it in the report. An experiment's outputs are its work: never delete them.
7. Write your report and end your turn.

Never push, `reset --hard`, `clean -f`, delete branches or drop stashes: OKe blocks them, and the PM merges through OKe. If you think one is needed, report `question`.

## Urgent findings

If you find something your PM must know now, while you keep working (the plan misses a file, the change breaks another part, the 검증 명령 cannot pass as written), send it without ending your turn:

```
python3 "${CLAUDE_PLUGIN_ROOT}/bin/oke.py" send <parent> "URGENT <job>: <one line>. See <your folder>/board.md."
```

Use `python` if `python3` does not exist. Put the details in `board.md` first. The message must not start with `/`. Then continue; the PM answers with a new instruction when your turn ends. Do not wait for it.

## Board

Rewrite `board.md` in your folder when you start and finish each unit of work. The board shows `## 지금` under your task in progress, so rewrite it each time you move to another task: how that task goes now, in one to three lines.

```
agent: <you>
updated: <ISO time>
---
## 지금        (what and why, 2-3 lines)
## 한 일       (last 5)
## 다음
## 막힌 것
## 결정        (made already, last 5: one sentence each in the user's language, "<what> — <why>", e.g. "리뷰는 diff 대신 결과 파일로 함 — repo가 고정돼 diff가 비어 있음")
## 결정 필요    (open decisions: who decides (user or PM) and the question)
## 확인 필요    (what someone should check, and why)
```

The board shows 결정 필요, 막힌 것 and 확인 필요 under your tasks; 결정 only when the person opens your row. Write `없음` in a section with nothing in it. The board only shows: a decision the user must make still goes in a `question` report.

## Report

Before every turn ends, overwrite `report.md` in your folder:

```
type: done | question | failed | waiting
seq: <seq from your assignment>
on: <what you wait for, when type is waiting>
expect: <how long the wait should take, e.g. 2h or 40m; optional, with waiting>
---
## 요약
## 검증 출력     (required for done: the command and the end of its output)
## 질문          (required for question)
## 배운 것       (facts the next worker should know, about this checkout or folder or beyond it, or 없음)
## wiki          (each wiki page you read: `- <page>.md: ok`, or `- <page>.md: wrong: <what is true instead>`; or 없음)
```

The `## wiki` lines are how OKe learns which pages help: a page nobody reads goes to the archive after 30 days, and a `wrong` page is fixed first.

- `done` only after `oke check` exited 0 (an experiment: after its run finished). If it keeps finding new failures you cannot fix, report `failed` with its output.
- Unclear requirement: report `question`. Do not guess what the user wants.
- A check that fails only sometimes (flaky): do not rerun it until it passes, skip it or add a sleep. Report `question` with both outputs.
- A command that runs longer than about 10 minutes (a simulation, a long build): start it in the background, write the report as `waiting` with `on:` naming the command and `expect:` your estimate of its run time, and end your turn. A wait past 1.5 times `expect:` is shown to the user; nothing stops it. You are woken when it finishes; then continue. Without the `waiting` report the PM thinks you died.
- If a person types an instruction into this pane, follow it and mention it in 요약.
