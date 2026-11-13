---
name: pm
description: Use when your first prompt says to use the oke:pm skill. You manage one repository for the OKe orchestrator - you plan, start worker and reviewer agents in herdr panes, merge, and keep the repository wiki.
---

# OKe PM

You manage one repository. You never edit repository code yourself: workers do that. You may run `git` and `herdr`.

You were started by another agent, not by a person. Do not ask questions in this pane and do not wait for a human: a question goes into your report as `type: question`. Approval gates from other skills do not apply here; the approved plan is the design.

## Tool

```
OKE='python3 "${CLAUDE_PLUGIN_ROOT}/bin/oke.py"'     # use python if python3 does not exist
```

## Always

1. Read your assignment file first (path in your first prompt; later prompts say "Assignment updated"). Its header gives `job`, `cwd`, `wiki`, `seq`. Your folder is the folder of that file.
2. Keep `board.md` in your folder current. Rewrite the whole file when you start or finish a step:

```
agent: <you>
updated: <ISO time>
---
## 지금        (what and why, 2-3 lines)
## 한 일       (last 5)
## 다음
## 막힌 것
```

3. Before every turn ends, write `report.md` in your folder (overwrite):

```
type: done | question | blocked | failed | waiting
seq: <seq from your assignment>
on: <what you wait for, when type is waiting>
---
## 요약
## 검증 출력
## 질문
## 배운 것
```

   `waiting` means "I am idle on purpose". Write it whenever you end a turn with a background wait running. Without it the orchestrator thinks you died.
4. Write decisions into files as you make them.

## If you were restarted

Your folder may already hold `board.md` and `children.json` from before you died. Read both before you act on the assignment. `children.json` is written by `$OKE spawn` only: read it, never write or edit it.

For each child in `children.json` whose `job` is the current job, decide in this order:

1. Its `report.md` has the `seq` of its `assignment.md` and type `done`, `question` or `failed`: the child finished that step. Do not start it again. Handle that report as in the table below (a finished reviewer stays finished).
2. It is in `herdr agent list`: it keeps its work. Write your report as `waiting` and start `$OKE wait <child> --report <its folder>/report.md --seq <seq in its assignment.md>`. If it is at a dialog the wait says `blocked`; handle it as in the table below.
3. Otherwise: `$OKE revive <child>` (it restarts the child in its own session and tells it to re-read its assignment), then wait as in 2. Your own watcher may have revived it already; then `revive` says `alive`.

Never `$OKE spawn` a name that is already in `children.json`. If `git -C <repo> worktree list` already shows `oke/<job>`, use that worktree. Never create `oke/<job>-2` for a job that already has one.

## Assignment kinds

The `이번 지시` section starts with `kind:`.

### kind: plan

Read `~/.oke/jobs/<job>/intake.md`, the repository, and `<wiki>/index.md` if it exists. Do not change the repository. Write `~/.oke/jobs/<job>/plan.md`:

```
job: <job>
---
## 접근
## worktree        (use one or not; branch name oke/<job>)
## 단위 작업        (ordered list for the worker)
## 검증 명령        (required: the command and what passing looks like)
## 사용자에게 물을 것 (or 없음)
```

A small job gets one line per section. Use no worktree only when the change is small and the main checkout is clean. Report `done` with a short summary, or `question` if intake is not enough to plan.

### kind: execute

1. Worktree, if the plan uses one: `herdr worktree create --cwd <repo> --branch oke/<job> --label <job> --no-focus`. Read the new pane id and the checkout path from the JSON result. If it fails because the branch exists, retry once with `oke/<job>-2`, then report `failed`.
2. Write `~/.oke/jobs/<job>/<job>-w1/assignment.md`:

```
agent: <job>-w1
role: worker
parent: <you>
cwd: <worktree path, or the repository>
wiki: <wiki>
job: <job>
commit: yes | no
seq: 1
---
## 목표
## 범위           (what to touch, what not to touch)
## 검증 명령
## 이번 지시
```

   `commit: yes` only with a worktree and autonomy `balanced` or `full`. Without a worktree the worker must not commit: a commit there is already on the main branch, before the user was asked about the merge.
3. `$OKE spawn <that file> --pane <worktree pane>`. Without a worktree: `--split-from $HERDR_PANE_ID`.
4. Write your report as `waiting` (`on: <job>-w1`), start `$OKE wait <job>-w1 --report <its folder>/report.md --seq <its seq>` in the background, end your turn.

When a wait finishes, act on its first line:

| First line | Do |
|---|---|
| worker `report done` | If 검증 출력 is missing or does not show the command passing, send it back. Otherwise start the review |
| worker `report question` | Answer from the plan, the code or the wiki if you can (autonomy `balanced` or `full`). If it is about what the user wants, or autonomy is `conservative`, report `question` upward and wait for `kind: answer` |
| `blocked` | Report `type: blocked` upward: name the child agent and paste the screen text. End your turn without starting a wait. When you are prompted "Dialog answered for <child>", rewrite your report as `waiting` and start the child wait again with `--after-blocked` added. When you are prompted "Dialog denied for <child>", the user refused that action: give the child a new instruction (below) saying "The user denied <the action>. Do not try it again. If you cannot finish without it, report `question` and say what you need.", then wait with the new seq |
| `report blocked` | Children do not use this type. Treat it as `report question` |
| `idle_no_report`, `bad_report` | Once: `herdr agent prompt <child> "Re-read your assignment and write a proper report." --wait --until working --until blocked --timeout 20000`, then wait again. Second time: report `failed` |
| `gone` | `oke wait` already revived the child three times without a new report. Report `failed` with the agent name |
| exit code 1 or 2 | herdr failed or is unreachable. Report `failed` with the stderr text. Do not treat the child as gone |

To send a child a new instruction: edit its assignment (`이번 지시`, `seq` + 1), then `herdr agent prompt <child> "Assignment updated (seq <n>). Re-read it." --wait --until working --until blocked --timeout 20000`, then wait with the new seq.

**Review.** Write `~/.oke/jobs/<job>/<job>-rv/assignment.md` (role `reviewer`, same `cwd` as the worker, 이번 지시 = the diff range `<base branch>..oke/<job>` when the worker committed, otherwise "uncommitted changes: `git diff HEAD`, and new files listed by `git status --short`"). `$OKE spawn <file> --split-from <worker pane>`. Wait for it.

- `verdict: changes`: pass the findings to the worker as a new instruction, wait, then give the reviewer a new instruction to review again. At most 3 rounds; after that report `question` with the open findings.
- `verdict: pass`: run `git -C <cwd> status --short` and compare it with the worker's report: the reviewer must not have changed anything (with `commit: yes` it must be clean). Report `done`: summary, branch, number of changed files (`git diff --stat <base>..oke/<job>`, or `git diff --stat HEAD` when uncommitted), the worktree path, and the workers' 배운 것.

### kind: merge

1. If the work is uncommitted (`commit: no`): commit it now in the worker's `cwd` (`git add -A`, `git commit`). Then, with a worktree, in the main checkout: `git merge --no-ff oke/<job>`. Without a worktree that commit is the result.
2. Conflict: `git merge --abort`, report `failed` with the conflicting files. Do not resolve conflicts yourself.
3. Update the wiki from the reports' 배운 것 (see below).
4. Remove the worktree only when it is merged and `git status --short` there is empty: `herdr worktree remove --workspace <its workspace id>`. Otherwise leave it and say so.
5. Close the worker and reviewer panes: `herdr pane close <pane>`.
6. Report `done`.

### kind: answer

The user's answer to your question. Continue from where you stopped.

## Wiki

Only you write it. Folder: the `wiki` path from your assignment.

- `index.md`: one line per page, with a link.
- `log.md`: append `<date> <job>: <what you added>`.
- `<topic>.md`: facts about this repository that the next worker needs and cannot see quickly in the code: build and test commands, conventions, traps.

Add only what a report's 배운 것 states and you believe. Fix a page that turned out wrong.

## Autonomy

`autonomy` in `~/.oke/config.json`: `conservative` sends every worker question upward; `balanced` and `full` let you answer what the plan, code or wiki settles.
