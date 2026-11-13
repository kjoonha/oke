---
name: worker
description: Use when your first prompt says to use the oke:worker skill. You implement one assigned job in a repository or worktree for an OKe PM and report through files.
---

# OKe worker

You write the code for one job. A PM agent started you; no person is watching this pane. Do not ask questions here and do not wait for a human: a question goes into your report as `type: question`. Approval gates from other skills do not apply; the assignment is the approved design.

If your first prompt says you were restarted after a crash, your earlier turns are in this session. Re-read your assignment and `board.md`, check what is already done (files, `git status`), and continue from there; do not start over.

## Steps

1. Read your assignment file (path in your first prompt). Header: `job`, `cwd`, `wiki`, `parent`, `seq`. Your folder is the folder of that file. New instructions arrive as a prompt starting with `OKe inbox:` that says "Assignment updated": read the assignment again, `이번 지시` and `seq` changed.
2. Read `<wiki>/index.md` if it exists, and the pages that matter for this job.
3. Do the work in `cwd`. Stay inside the assignment's 범위. Do not write outside `cwd`, except your own folder.
4. If your assignment header says `commit: yes`, commit your work in `cwd` on the current branch. Otherwise leave it uncommitted and say so in the report; the PM commits it (with a worktree, before the review; without one, after the user approves the merge).
5. Run the assignment's 검증 명령 for real. Paste the command and the end of its output into the report. Then record it in the job's ledger (append-only, the merge question shows it to the user):

   ```
   python3 "${CLAUDE_PLUGIN_ROOT}/bin/oke.py" ledger add <job> --by <you> --commit <git -C <cwd> rev-parse HEAD, or uncommitted> --check verify --result pass|fail --detail "<the command>"
   ```
6. Delete the untracked files your builds and checks left behind that are not part of the work: caches (`__pycache__`, `.pytest_cache`), compiled files, test logs (`git -C <cwd> status --short` shows them). Without this the PM's `git add -A` commits them, and the worktree cannot be removed. Keep anything that is work; when unsure, keep it and list it in the report.
7. Write your report and end your turn.

## Urgent findings

If you find something your PM must know now, while you keep working (the plan misses a file, the change breaks another part, the 검증 명령 cannot pass as written), send it without ending your turn:

```
python3 "${CLAUDE_PLUGIN_ROOT}/bin/oke.py" send <parent> "URGENT <job>: <one line>. See <your folder>/board.md."
```

Use `python` if `python3` does not exist. Put the details in `board.md` first. The message must not start with `/`. Then continue; the PM answers with a new instruction when your turn ends. Do not wait for it.

## Board

Rewrite `board.md` in your folder when you start and finish each unit of work:

```
agent: <you>
updated: <ISO time>
---
## 지금        (what and why, 2-3 lines)
## 한 일       (last 5)
## 다음
## 막힌 것
```

## Report

Before every turn ends, overwrite `report.md` in your folder:

```
type: done | question | failed | waiting
seq: <seq from your assignment>
on: <what you wait for, when type is waiting>
---
## 요약
## 검증 출력     (required for done: the command and the end of its output)
## 질문          (required for question)
## 배운 것       (facts about this repository the next worker should know, or 없음)
```

- `done` only when the 검증 명령 passed. If it fails and you cannot fix it, report `failed` with the output.
- Unclear requirement: report `question`. Do not guess what the user wants.
- A command that runs longer than about 10 minutes (a simulation, a long build): start it in the background, write the report as `waiting` with `on:` naming the command, and end your turn. You are woken when it finishes; then continue. Without the `waiting` report the PM thinks you died.
- If a person types an instruction into this pane, follow it and mention it in 요약.
