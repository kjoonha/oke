# PM peers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PMs ask each other about cross-repo impact while planning and working. A job can wait for another repository's job before it merges. The board shows both waits.

**Architecture:**
- New commands `oke peer ask` and `oke peer answer` carry the talk through the existing inbox (`queue_mail`). Each job records the talk in `jobs/<job>/peers.json` (who must still answer) and `jobs/<job>/peers.md` (the log).
- The asked PM's wait catches a PM that never answers.
- `oke job merge` reads `after:` from the plan header.
- The board JSON gets `peer_wait` and `after_wait`, and the mod draws them in the step line.

**Tech Stack:** Python 3.6 standard library (`oke/bin/oke.py`), unittest with the fake herdr, and the TypeScript mod (`oke/hooks/register.tsx`, `claude plugin test oke`).

**Spec:** `docs/superpowers/specs/2026-11-20-oke-pm-peers-design.md`

## Global Constraints

- Python 3.6 stdlib only. No new dependency.
- Every state change happens under `locked()`. Atomic writes go through `write_json_atomic`.
- No time-based judgment: "no answer" means the PM is idle with an empty inbox, never that a time limit ran out.
- A message must not start with `/`.
- Tests: `cd oke/bin && python -m unittest discover -p "test_*.py"`, then `claude plugin test oke`.
- E2e uses an isolated OKE_HOME and never the real `~/.oke`.

## Review Focus

1. **The asking PM is told "you stopped short" while it waits.** Expected: no `idle_no_report` for a job whose waiting list is not empty. Test in Task 2.
2. **An asked PM that has only held jobs.** Expected: it is dropped from the list, because nothing revives it. Test in Task 1.
3. **An `after:` job that is archived.** Its folder moved and its state entry is gone. Expected: pass only when the archived ledger says verified. Test in Task 3.
4. **A PM that is asked by two jobs at once.** Expected: each answer clears only its own job's list, and the nudge names the right job. Test in Task 2.
5. **A second ask on the same job while answers are pending.** Expected: the round goes up, the list is rebuilt from the current open repositories, and an answer counts for the current round. Test in Task 1.

---

### Task 1: `oke peer ask` / `oke peer answer` and the waiting list

**Files:**
- Modify: `oke/bin/oke.py`. Add a `# ---- peers` block next to `# ---- inbox` (around line 2930) and a `peer` subparser next to `send` (around line 3270).
- Test: `oke/bin/test_peers.py` (new). Base it on `test_slice3.InboxBase`.

**Interfaces:**
- Produces:
  - `peers_path(job) -> str`: `<home>/jobs/<job>/peers.json`.
  - `peer_waiting(state, job) -> dict`: `{pm_agent: {"repo": key, "nudged": int}}`. It keeps only PMs whose `open_jobs(state, key)` is non-empty. Missing file: `{}`.
  - `owed_answers(state, agent) -> list of (job, round)`: every job whose `peer_waiting` contains `agent`, sorted by job name.
  - `cmd_peer(args)`: `args.action` is `ask` or `answer`, plus `args.job` and `args.text` (nargs `+`).
  - peers.json shape: `{"round": int, "asker": pm_agent, "waiting": {...}}`.
  - Message texts (exact):
    - ask: `peer ask (job <job>, round <n>): <text>. Answer with: oke peer answer <job> <no impact | impact: ...>`
    - answer: `Job <job>: peer answer from <pm> (round <n>): <text>`

- [ ] **Step 1: Write the failing tests** in `test_peers.py`. Set up two repositories (`self.repo` plus a second git repo made the way `SpawnBase` makes `self.repo`), each with a PM record and one open job. Set `HERDR_PANE_ID` to the caller's pane, as in `test_the_sender_is_the_agent_in_this_pane`.
  - `test_ask_queues_to_every_other_pm_with_an_open_job`:
    - The other PM's inbox has one message starting `pm-a: peer ask (job fix-a, round 1):`.
    - The output is `{"round": 1, "asked": ["pm-b"]}`.
    - peers.json `waiting` has key `pm-b`.
  - `test_ask_includes_a_dead_pm_and_skips_held_and_jobless_repos`:
    - A third repo with only a held job is not asked.
    - A fourth repo whose job is done is not asked.
    - A dead PM (not in `agent list`) with an open job is asked.
  - `test_ask_with_no_other_repo_prints_empty_and_writes_no_waiting`: `asked == []` and `peer_waiting` is `{}`.
  - `test_ask_from_a_non_pm_is_refused`: the caller pane is a worker. Exit 1, nothing queued.
  - `test_answer_reaches_the_asker_and_clears_the_entry`:
    - The asker's inbox gets `pm-b: Job fix-a: peer answer from pm-b (round 1): no impact`.
    - `peer_waiting` is `{}`.
    - peers.md has the lines `- ask (pm-a): ...` and `- answer (pm-b): no impact`.
  - `test_answer_from_a_pm_not_asked_is_refused`: exit 1, error text has `not asked`.
  - `test_new_round_rebuilds_the_list`: pm-b answers round 1. Then a second ask runs. Expected: `round == 2`, pm-b is waiting again, and peers.md has a `## round 2` heading.
  - `test_held_target_drops_out`: put the target's only job on hold (`held: True`). Then `peer_waiting` is `{}`.

- [ ] **Step 2: Run** `cd oke/bin && python -m unittest test_peers -v`. Expected: FAIL (`invalid choice: 'peer'`).

- [ ] **Step 3: Implement** in `oke.py`.
  - Identify the caller as `cmd_send` does: `runtime().whoami()`, then `whoami(state, pane)`. Require role `pm` and `key == state["jobs"][job]["repo"]` for ask. Require the caller name in `peer_waiting` for answer.
  - Text normalisation and the leading `/` refusal are the same as in `cmd_send`.
  - Write peers.json and append to peers.md under `locked()`. Queue the mail after the save (same order as `cmd_send --job`).
  - peers.md lines: `## round <n> (<YYYY-MM-DD HH:MM>)` on ask, then `- ask (<pm>): <text>`. On answer: `- answer (<pm>): <text>`.

- [ ] **Step 4: Run** `python -m unittest test_peers -v`. Expected: PASS. Then the full suite. Expected: all OK.

- [ ] **Step 5: Commit** `feat: oke peer ask/answer: PMs ask each other about cross-repo impact`.

### Task 2: waits: no false "stopped short", and the asked PM that never answers

**Files:**
- Modify: `oke/bin/oke.py`, in `check_pm_once` (around line 995) and `wait_loop` (the confirmed-idle branch, around line 1133).
- Test: `oke/bin/test_peers.py`.

**Interfaces:**
- Consumes: `peer_waiting`, `owed_answers` from Task 1.
- Produces:
  - `check_pm_once` returns `"?peer_idle <job>"` when the PM is idle, `owed_answers(state, agent)` is non-empty, and no other result applies. `<job>` is the first owed job.
  - `peer_nudge(agent, job) -> bool`, under `locked()`:
    - If `nudged == 0`: set it to 1, queue `Answer the peer ask of job <job> (round <n>): oke peer answer <job> <no impact | impact: ...>` (sender `oke`), and return True.
    - Otherwise return False.
  - `wait_loop`, on a confirmed `?peer_idle <job>`: if `peer_nudge` returns True, set `idle = 0` and keep waiting (the next poll delivers the reminder). If it returns False, return `peer_no_answer <job> <agent>`. This check comes before the `warn_job` call.

- [ ] **Step 1: Write the failing tests.**
  - `test_asking_pm_waiting_on_peers_gets_no_idle_no_report`: job fix-a has a sent seq and no report, and its peers.json has `pm-b` waiting. `check_pm_once("pm-a", key_a)` returns None, not `?idle_no_report fix-a`.
  - `test_idle_asked_pm_is_reminded_once_then_peer_no_answer`: pm-b is idle with an empty inbox and owes fix-a.
    - First `wait_loop("pm-b", None, None, poll=0, max_polls=..., jobs_key=key_b)` round: one reminder queued to pm-b, `nudged == 1`.
    - After that mail is moved to `read/`, the next wait returns `peer_no_answer fix-a pm-b`.
  - `test_pm_with_mail_is_delivered_before_any_peer_check`: pm-b owes fix-a and has unread mail. The wait delivers the mail and does not return `peer_no_answer`.
  - `test_two_asks_nudge_names_the_first_owed_job`: pm-b owes fix-a and fix-c. The reminder names fix-a. Answering fix-a leaves fix-c's list unchanged.

- [ ] **Step 2: Run** them. Expected: FAIL.

- [ ] **Step 3: Implement.**
  - In `check_pm_once`, after `look == "idle"`: skip a job in the idle_no_report loop when `peer_waiting(state, name)` is non-empty.
  - After that loop, return `?peer_idle <job>` if the PM owes an answer.

- [ ] **Step 4: Run** test_peers, then the full suite. Expected: PASS.

- [ ] **Step 5: Commit** `feat: the wait reminds a PM that owes a peer answer, then returns peer_no_answer`.

### Task 3: `after:` holds the merge; cancel tells the waiting PMs

**Files:**
- Modify: `oke/bin/oke.py`, in `merge_job` (line 2025, before any git work, right after the phase check) and in the `cancel` action (around line 2695, next to the existing PM notice).
- Test: `oke/bin/test_peers.py`. Reuse the merge fixtures in `test_merge.py` only if a full merge is needed. The refusals happen before git, so phase `merging` plus state is enough.

**Interfaces:**
- Produces:
  - `after_jobs(job) -> list of str`: from `read_header(<home>/jobs/<job>/plan.md)` key `after`, split on commas, stripped, empty items dropped.
  - `after_block(state, other) -> str or None`. Exact refusal texts:
    - In state and phase failed with `cancelled`, or phase failed: `after %s was cancelled: ask the user`
    - In state, `merge_state(other)[0] != "verified"`: `waiting for %s to merge`
    - Not in state: take the newest `<home>/archive/<other>-*/ledger.tsv`. Verified merge: None. Otherwise: `after %s was archived without a verified merge: ask the user`
  - `merge_job` returns `fail("job %s: %s" % (job, reason))` for the first blocking `after` job.
  - Cancel: for every open job whose `after_jobs` contains the cancelled job, queue to that job's PM (sender `oke`): `Job <x>: after-job <cancelled> was cancelled. Report question to the user.`

- [ ] **Step 1: Write the failing tests.**
  - `test_merge_waits_for_the_after_job`
  - `test_merge_passes_once_the_after_job_is_verified`. Write ledger rows merge + verify pass with `ledger_add`. Expect the refusal to no longer mention `after`.
  - `test_merge_refuses_a_cancelled_after_job`
  - `test_archived_after_job_counts_by_its_archived_ledger`. Cover verified and not verified.
  - `test_cancel_notifies_the_pm_of_a_job_waiting_on_it`

- [ ] **Step 2: Run** them. Expected: FAIL.

- [ ] **Step 3: Implement** as in Interfaces. Read `merge_state` from the archived folder by reading its `ledger.tsv` with the same parser that `ledger_rows` uses. Factor `ledger_rows(job)` into `ledger_rows_at(path)` if needed, and keep `ledger_rows(job)` as the caller-facing name.

- [ ] **Step 4: Run** test_peers, test_merge, then the full suite. Expected: PASS.

- [ ] **Step 5: Commit** `feat: plan after: holds a merge until the other job is verified`.

### Task 4: the board shows the waits

**Files:**
- Modify: `oke/bin/oke.py`, in `board_tree` (line 1548, the job dict) and `board_lines` (the job line).
- Modify: `oke/types/index.d.ts`. Add to `BoardJob`: `peer_wait?: string[]` (repository short names) and `after_wait?: string[]`.
- Modify: `oke/hooks/register.tsx`, in the step line (around line 982). At the current step, render ` (wait PM a, b)` from `peer_wait` and ` (wait x)` from `after_wait`, dim yellow.
- Test: `oke/bin/test_peers.py` and `oke/hooks/register.test.tsx`.

**Interfaces:**
- Consumes: `peer_waiting`, `after_jobs`, `after_block`.
- Produces: board job keys:
  - `peer_wait`: `[repo_name(e["repo"]) for e in peer_waiting(...).values()]`, sorted.
  - `after_wait`: the `after_jobs` whose `after_block` is not None. Only filled while phase is `awaiting_merge` or `merging`. Otherwise `[]`.
  - `board_lines` appends `, waiting for PM <names>` and `, waiting for <jobs> to merge`.

- [ ] **Step 1: Write the failing tests.**
  - Python: `test_board_lists_peer_and_after_waits`. Check `board_tree(...)[i]["jobs"][j]["peer_wait"] == ["repo-b"]` and `after_wait == ["a1"]` in `merging`. Check `after_wait == []` in `working`.
  - Mod: a test like the step-line one at `register.test.tsx:175`, with a job carrying `peer_wait: ['repo-b']` in phase `planning`. Expect `/plan \(wait PM repo-b\)/`.

- [ ] **Step 2: Run** them. Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run** the full Python suite and `claude plugin test oke`. Expected: PASS. tsc must be clean (the mod test runs it).

- [ ] **Step 5: Commit** `feat: board shows peer and after waits in the step line`.

### Task 5: skills

**Files:**
- Modify: `oke/skills/pm/SKILL.md`, `oke/skills/orchestrator/SKILL.md`.

Content, as the spec's Skills section says:
- **PM, kind: plan.**
  - List what the plan changes that other code may use.
  - If anything, run `$OKE peer ask <job> "<it>"`. `asked: []` means the 다른 레포 영향 section is `없음 (no other repository has an open job)`.
  - End the turn and wait for the answers (`OKe inbox:` lines from other PMs).
  - Report the plan only when every asked PM answered.
  - Template: add `## 다른 레포 영향 (who was asked, each answer, what changed; or 없음)`, and the optional header line `after: <job>[, <job>]` under `job:`.
- **PM, new section "Peer asks".**
  - Answering: read your repository and open jobs (an Explore subagent for a wide search, never a worker pane). Then `$OKE peer answer <job> "no impact"` or `"impact: <what breaks, what you need>"`.
  - When your own open job must change: edit its plan and report `done` (re-plan) for it, so the user approves again.
  - Talk until agreed. If you cannot agree, report `question` with both positions.
  - A reminder `Answer the peer ask of job ...` means answer now.
- **PM, work.** A worker or review report that shows a cross-repo change not in the plan:
  - Give the worker a "stop, wait" instruction (seq + 1).
  - `peer ask`.
  - After the answers, continue, or re-plan and report for approval.
- **PM, inbox line** (`SKILL.md:32`): add peer ask, peer answer and reminder messages.
- **PM, merge.** `job merge` refusing with `waiting for <job> to merge`: tell the orchestrator in the report (type `waiting`) and retry when told. `after ... cancelled/archived`: report `question`.
- **Orchestrator, wait table.** Add a row for `peer_no_answer <job> <pm>`:
  - Ask the user one question: "proceed as no impact", "wait", or "cancel the job".
  - Send the asking PM `kind: answer` with the choice. For "wait": start the wait again.
- **Orchestrator, plan approval.** Include the plan's 다른 레포 영향 in the popup when it is not 없음.
- **Orchestrator.** When the user asks what PMs discussed: read `$OKE_HOME/jobs/<job>/peers.md`.

- [ ] **Step 1:** Edit both skills.
- [ ] **Step 2:** `grep -n "peer" oke/skills/*/SKILL.md`. Every command named exists in `oke.py --help` output (`python oke/bin/oke.py peer --help`).
- [ ] **Step 3: Commit** `docs: PM and orchestrator skills for peer asks and after:`.

### Task 6: review and e2e

- [ ] **Step 1:** Opus whole-branch review (`git diff main...pm-peers`). Fix the findings, and repeat until a round finds nothing.
- [ ] **Step 2:** Sonnet e2e with an isolated OKE_HOME, as in the spec's Tests section:
  - Toy repos: `api`, where a function's return shape changes, and `app`, which calls it.
  - Check each of these:
    - the ask happens
    - the impact answer is given
    - the app job is re-planned and approved again
    - `after:` holds the app merge
    - the board shows the waits
- [ ] **Step 3:** Fix what the e2e finds, review again, and rerun the failing part.
- [ ] **Step 4:** Report to the user. Merge and push only with their approval.
