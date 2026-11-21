# OKe Quality Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A job merges only after code has run the plan's 검증 명령 on the reviewed commit and found no failure that main did not already have, and the reviewer has judged every acceptance criterion.

**Architecture:** Everything lives in `oke/bin/oke.py` next to the existing ledger, `run_check`, `merge_job` and `verify_locked`. Two new subcommands (`oke baseline`, `oke check`) run the command and diff OMC-style failure signatures against `jobs/<job>/baseline.json`. The merge gate gains two checks; `job verify` reuses the diff and skips the run when the merged tree equals the checked tree. Skills tell agents when to call them.

**Tech Stack:** Python 3.6 stdlib only (RHEL8), unittest via pytest, git.

**Spec:** `docs/superpowers/specs/2026-11-22-oke-quality-protocol-design.md`

## Global Constraints

- Python 3.6 stdlib only: no f-strings with `=`, no `dataclasses`, no variable-width lookbehind in `re`.
- No time limit on any check (`run_check` already has none; keep it).
- Ledger rows keep the six columns `time, by, commit, check, result, detail`.
- Files under `$OKE_HOME` are written the way neighbouring code writes them (`io.open`, utf-8).
- Repo docs describe current behaviour only: no history or rationale in README or skills.
- Research and `direct` jobs are untouched by every new rule.
- Run tests from `oke/bin`: `python -m pytest -q <file>`; the full suite takes about 8 minutes.

## Review Focus

- A baseline whose command exits 0 must store no signatures, even when its output contains "Error" (test in Task 1).
- Two runs of the same failing pytest command with different durations and tmp paths give identical signatures (test in Task 1).
- `oke check` with no baseline, or with a baseline recorded under another `criteria_rev`, exits 2 and writes no ledger row (test in Task 2).
- A job without a worktree: `oke check` records `tree:<sha>` and the direct-commit merge accepts it only when it equals the review's `tree:` (test in Task 3).
- `job verify` after main moved past the checked commit must run the command, not skip (test in Task 4).

---

### Task 1: Failure signatures and the baseline diff

**Files:**
- Modify: `oke/bin/oke.py` (new block after `run_check`, ~line 2522)
- Create: `oke/bin/test_quality.py`

**Interfaces:**
- Produces:
  - `signature_lines(output: str) -> list` — sorted, de-duplicated, at most `MAX_SIGNATURES = 200`.
  - `diff_baseline(before: dict or None, now: dict) -> (list new, list resolved)`. Both dicts have keys `exit` (int or None), `unrunnable` (bool), `signatures` (list). `before=None` means "no baseline": treat it as `{"exit": 0, "unrunnable": False, "signatures": []}`.
  - `run_signatures(command, cwd, log, lock) -> dict` — calls `run_check`, reads the log, returns `{"exit": code, "unrunnable": code in (126, 127), "signatures": [] if code == 0 or unrunnable else signature_lines(text)}`.

- [ ] **Step 1: Write the failing tests** in `test_quality.py` (plain `unittest.TestCase`, `import oke`):

```python
PYTEST_A = "tests/t.py::test_div FAILED [ 50%]\nFAILED tests/t.py::test_div - ZeroDivisionError\n===== 1 failed, 4 passed in 3.21s (0:00:03) =====\n....F\n"
PYTEST_B = PYTEST_A.replace("3.21s (0:00:03)", "9.80s (0:00:09)")

def test_volatile_values_give_equal_signatures(self):
    self.assertEqual(oke.signature_lines(PYTEST_A), oke.signature_lines(PYTEST_B))
    self.assertIn("FAILED tests/t.py::test_div - ZeroDivisionError", oke.signature_lines(PYTEST_A))

def test_noise_is_dropped_and_plain_lines_kept(self):
    out = "\x1b[31mresult mismatch at step 4\x1b[0m\n✓ adds\nok 1 - x\n1..3\n# pass 2\n# fail 0\nINFO starting\nnpm warn deprecated\n.....\n12 passed in 0.5s\n  at foo (a.js:1:1)\n12| const x = 1\nnot ok 2 - y\n"
    self.assertEqual(oke.signature_lines(out), ["not ok 2 - y", "result mismatch at step 4"])

def test_failure_words_keep_log_and_summary_lines(self):
    out = "INFO validation failed\nTests 1 failed | 3 passed\nTests 4 passed\n"
    self.assertEqual(oke.signature_lines(out), ["INFO validation failed", "Tests 1 failed | 3 passed"])

def test_temp_paths_hex_and_node_pid_normalized(self):
    a = "Error in /tmp/abc123/x deadbeef01 (node:4242) [DEP0040] DeprecationWarning: p\n"
    b = "Error in /tmp/zz9/x cafebabe99 (node:7) [DEP0040] DeprecationWarning: p\n"
    self.assertEqual(oke.signature_lines(a), oke.signature_lines(b))

def test_cap_and_long_lines(self):
    self.assertEqual(len(oke.signature_lines("\n".join("E%d boom" % i for i in range(300)))), 200)
    self.assertEqual(oke.signature_lines("x" * 501), [])

def test_diff_green_to_red_is_new_even_without_lines(self):
    new, _ = oke.diff_baseline({"exit": 0, "unrunnable": False, "signatures": []},
                               {"exit": 1, "unrunnable": False, "signatures": []})
    self.assertEqual(new, ["<exited with code 1>"])

def test_diff_red_reports_only_new_lines_and_counts_resolved(self):
    before = {"exit": 1, "unrunnable": False, "signatures": ["old", "gone"]}
    new, resolved = oke.diff_baseline(before, {"exit": 1, "unrunnable": False, "signatures": ["old", "fresh"]})
    self.assertEqual((new, resolved), (["fresh"], ["gone"]))

def test_diff_unrunnable_and_no_baseline(self):
    new, _ = oke.diff_baseline({"exit": 1, "unrunnable": False, "signatures": []},
                               {"exit": 127, "unrunnable": True, "signatures": []})
    self.assertEqual(new, ["<command became unrunnable>"])
    self.assertEqual(oke.diff_baseline(None, {"exit": 2, "unrunnable": False, "signatures": ["x"]})[0],
                     ["<exited with code 2>", "x"])

def test_run_signatures_ignores_output_on_success(self):
    d = tempfile.mkdtemp()
    log = os.path.join(d, "x.log")
    self.assertEqual(oke.run_signatures("echo TypeError: fine", d, log, "t"),
                     {"exit": 0, "unrunnable": False, "signatures": []})
    self.assertEqual(oke.run_signatures("echo boom; exit 3", d, log, "t"),
                     {"exit": 3, "unrunnable": False, "signatures": ["boom"]})
```

- [ ] **Step 2: Run** `python -m pytest -q test_quality.py` — expected: FAIL, `AttributeError: module 'oke' has no attribute 'signature_lines'`.

- [ ] **Step 3: Implement** `signature_lines`, `diff_baseline`, `run_signatures` in `oke.py`. Port OMC `signatureLines` (spec "Failure signatures") rule for rule, in its order. Python-specific regexes (no variable lookbehind):

```python
FAILURE_WORD = re.compile(r"\bfail(?:ed|ure|ures|ing)?\b|\w*(?:Error|Exception)s?\b|✗|×", re.I)
VOLATILE = [  # applied in this order, each replaced by <v> (the first keeps its group)
    (re.compile(r"(\bin \d+(?:\.\d+)?s\s)\(\d+:\d{2}(?::\d{2})?(?:\.\d+)?\)"), r"\1<v>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds)\b", re.I), "<v>"),
    (re.compile(r"\bduration(?:_ms)?\s*:?\s*\d+(?:\.\d+)?\b", re.I), "<v>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?"), "<v>"),
    (re.compile(r"[A-Za-z]:\\[^\s]*\\Temp\\[^\s]*", re.I), "<v>"),
    (re.compile(r"/tmp/[\w.-]+"), "<v>"),
    (re.compile(r"(?<=\(node:)\d+(?=\) (?:\[[^\]]+\] )?\w+:)"), "<v>"),
    (re.compile(r"\b[0-9a-f]{8,}\b", re.I), "<v>"),
]
```

`diff_baseline` order: green-to-red line first, then unrunnable line, then new signatures in sorted order; resolved = baseline-only signatures. A newly unrunnable command reports only `<command became unrunnable>` (no exit line). The macOS `/var/folders` normalizer is not ported (`shortcut:` comment: OKe runs on Windows and RHEL; port it when a macOS user shows up).

- [ ] **Step 4: Run** `python -m pytest -q test_quality.py` — expected: all pass.

- [ ] **Step 5: Commit** `git add oke/bin/oke.py oke/bin/test_quality.py && git commit -m "feat: OMC-style failure signatures and baseline diff"`

---

### Task 2: `criteria_rev`, `oke baseline`, `oke check`

**Files:**
- Modify: `oke/bin/oke.py` (`OKE_CHECKS` ~line 2468; new functions after Task 1's block; parser near `sub.add_parser("ledger")` ~line 3549)
- Modify: `oke/bin/test_quality.py`

**Interfaces:**
- Consumes: `run_signatures`, `diff_baseline` (Task 1); existing `ledger_add`, `pending_tree(main)`, `git_out`, `load_state`, `native_path`, `try_lock/unlock`.
- Produces:
  - `plan_command(job) -> str or None` — first backticked span inside plan.md's `## 검증 명령` section.
  - `criteria_rev(job) -> str` — `"sha256:" + sha256((수용 기준 section text stripped) + "\n" + (plan_command or "")).hexdigest()`; missing plan or section counts as empty text.
  - `check_dir(job) -> (cwd, commit_or_tree)` — the worktree path from `state["jobs"][job]["worktree"]["path"]` and its `rev-parse HEAD`; without a worktree the main checkout and `"tree:" + pending_tree(main)`.
  - `oke baseline <job>`: writes `jobs/<job>/baseline.json` = `{"version": 1, "recorded_at", "criteria_rev", "commit", "exit", "unrunnable", "signatures"}`; an existing file moves to `jobs/<job>/baseline.history/<YYYYmmddTHHMMSS>.json`, keeping the newest 20. Prints JSON `{"result": "recorded", "signatures": n, "added": a, "removed": r, "log": ...}`. Exit 0; exit 2 when the plan has no command.
  - `oke check <job>`: exit 0 pass, 1 new failures, 2 no baseline / stale baseline / unrunnable / no command. On 0 or 1 it writes ledger row `by="oke"`, `commit=<commit or tree:sha>`, `check="check"`, `result="pass"|"fail"`, `detail="<criteria_rev> new=<n> resolved=<n> log=<path>"`. Prints JSON `{"result", "criteria_rev", "commit", "new": [...], "resolved": n, "log"}`. Logs: `jobs/<job>/baseline.log`, `jobs/<job>/check-<n>.log`.
  - `"check"` added to `OKE_CHECKS`.
  - Both refuse research and `direct` jobs with the existing `NO_MERGE` message style.

- [ ] **Step 1: Write the failing tests** in `test_quality.py`, class `TestBaselineCheck(SpawnBase)` (`from test_oke import SpawnBase`). Helper `plan(command, criteria="1. a.txt exists")` writes `jobs/<job>/plan.md` with `## 수용 기준\n<criteria>\n## 검증 명령\n\`<command>\` passes\n`. Tests:
  - `test_criteria_rev_changes_with_command_or_criteria` — three plans, three distinct revs; same plan twice, same rev.
  - `test_check_without_baseline_exits_2_and_writes_no_row` — `cli("check", job) == 2`; ledger has no `check` row.
  - `test_baseline_then_pass` — plan `test -f a.txt || { echo "FAILED old"; exit 1; }`; baseline (exit 1, `["FAILED old"]`); create `a.txt`; check exits 0; ledger last row `("check", "pass")`, detail starts with the current rev.
  - `test_new_line_fails_old_line_does_not` — plan `echo "FAILED old"; test -f b.txt && echo "FAILED new"; exit 1`; baseline; check exits 0 (resolved 0, new 0); create `b.txt`; check exits 1 with `new == ["FAILED new"]`, row `("check", "fail")`.
  - `test_stale_baseline_refused` — baseline, then change the plan's command; check exits 2, message contains `oke baseline`.
  - `test_rebaseline_keeps_history` — two baselines; `baseline.history` has one file; second output has `added`/`removed`.
  - `test_ledger_add_cannot_forge_check` — `cli("ledger", "add", job, ... "check" ...)` is refused like `review`.
  - `test_no_worktree_records_tree` — job without worktree: row commit starts with `tree:` and equals `"tree:" + oke.pending_tree(main)`.

- [ ] **Step 2: Run** `python -m pytest -q test_quality.py` — expected: new tests FAIL (`invalid choice: 'baseline'`).

- [ ] **Step 3: Implement** the functions and parsers listed in Interfaces. Hold lock `"check-" + job` around a run, as `verify_job` does, so a second `oke check` waits instead of racing.

- [ ] **Step 4: Run** `python -m pytest -q test_quality.py test_oke.py` — expected: all pass.

- [ ] **Step 5: Commit** `git commit -am "feat: oke baseline and oke check run the plan's command and record the result"` (add `test_quality.py`).

---

### Task 3: Merge gate requires a current review revision and a passing check

**Files:**
- Modify: `oke/bin/oke.py` `merge_job` (~line 2204), `commit_direct` path
- Modify: `oke/bin/test_merge.py` (`MergeBase`, `TestDirectCommit.setUp`)

**Interfaces:**
- Consumes: `criteria_rev(job)`, ledger rows with `check == "check"` (Task 2).
- Produces: two refusals in `merge_job`, after the existing review-seq check and before any git change:
  - review assignment header `criteria_rev` != `criteria_rev(job)`: `fail("the criteria or the 검증 명령 of %s changed since the review: give the reviewer a new instruction with `criteria_rev: %s`" % (job, rev))`.
  - no ledger row with `check == "check"`, `result == "pass"`, `detail.split()[0] == rev` and `commit` equal to the reviewed commit (full sha; a row may hold a short sha, compare with `startswith` both ways like the review check) or, for the no-worktree path, `"tree:" + review_asg["tree"]`: `fail("no passing `oke check` on %s with the current criteria: run `oke check %s`" % (target, job))`.
  - Only the latest `check` row for that commit/tree counts (a later fail supersedes an earlier pass).

- [ ] **Step 1: Write the failing tests** in `test_merge.py`, class `TestQualityGate(MergeBase)`:
  - `test_merge_refused_without_check_row`
  - `test_merge_refused_when_plan_changed_after_review` (rewrite plan criteria after `review()`)
  - `test_merge_refused_when_last_check_failed` (pass row then fail row on the same commit)
  - `test_merge_allowed_with_current_review_and_check`
  - in `TestDirectCommit`: `test_direct_merge_needs_check_on_the_reviewed_tree`

- [ ] **Step 2: Update fixtures** so the existing tests keep their meaning: `MergeBase.setUp` writes `jobs/fix-a/plan.md` (criteria `1. a.txt exists`, command `` `test -f a.txt` ``); `MergeBase.review()` writes `criteria_rev: <oke.criteria_rev("fix-a")>` into the assignment and adds `oke.ledger_add("fix-a", "oke", commit, "check", "pass", rev + " new=0 resolved=0 log=-")` unless called with `checked=False`. Same for the direct-commit fixture with `tree:`.

- [ ] **Step 3: Run** `python -m pytest -q test_merge.py` — expected: only the new tests FAIL.

- [ ] **Step 4: Implement** the two refusals.

- [ ] **Step 5: Run** `python -m pytest -q test_merge.py test_hardening.py test_peers.py` — expected: all pass (other files that merge use `MergeBase` or need the same fixture fix; fix them in the fixture, not by loosening the gate).

- [ ] **Step 6: Commit** `git commit -am "feat: merge needs a review of the current criteria and a passing oke check"`

---

### Task 4: `job verify` reads the plan command, skips an identical tree, diffs against the baseline

**Files:**
- Modify: `oke/bin/oke.py` `cmd_job` verify branch (~line 2106), `verify_locked` (~line 2537), parser `verify.add_argument("command", nargs="?")` stays
- Modify: `oke/bin/test_merge.py` `TestLedgerAndVerify`

**Interfaces:**
- Consumes: `plan_command`, `criteria_rev`, `run_signatures`, `diff_baseline`, check rows (Tasks 1-3).
- Produces in `verify_locked`, after the existing "main moved past the merge" check:
  1. command = `plan_command(job)`; an argument that differs from it fails `"the 검증 명령 differs from plan.md's: ..."`; no plan command and no argument fails as today. `cmd_job` accepts a missing command when plan.md has one.
  2. Tree skip: the merge commit's tree (`rev-parse <merge>^{tree}`) equals the tree of the latest passing check target with the current rev (`<commit>^{tree}`, or the `tree:` sha itself) → `ledger_add(job, "oke", merge, "verify", "pass", "same tree as <target>")`, tell waiters, print `{"result": "verified", ..., "skipped": True}`, exit 0.
  3. Otherwise `run_signatures`; baseline = `jobs/<job>/baseline.json` only when its `criteria_rev` is current, else `None`; `new, resolved = diff_baseline(...)`. Empty `new` → `verify pass` (as today's exit 0 path). Non-empty `new` → today's fail-and-rollback path (exit 3) with `"new": new` added to the printed JSON. Exit 126/127 keeps today's "could not run" message when the baseline was not itself unrunnable.

- [ ] **Step 1: Write the failing tests** in `TestLedgerAndVerify`:
  - `test_identical_tree_skips_the_run` — merge with main unmoved; `job verify fix-a` (plan command `exit 1` would fail if run) exits 0, JSON `skipped`, ledger detail `same tree as`.
  - `test_moved_main_runs_and_old_failures_pass` — commit `b.txt` on main before the merge; baseline JSON with `exit 1` and signature `FAILED old`; plan command `echo "FAILED old"; exit 1`; verify exits 0.
  - `test_moved_main_new_failure_rolls_back` — same, command also prints `FAILED new`; exit 3, `new == ["FAILED new"]`, HEAD back at pre.
  - `test_argument_must_match_plan_command`.

- [ ] **Step 2: Update existing verify tests** that pass a command argument: their class commits a file on main before merging (so the tree skip does not apply) and writes the plan command equal to the argument via a helper `set_command(cmd)` that also re-records the fixture check row under the new rev.

- [ ] **Step 3: Run** `python -m pytest -q test_merge.py` — expected: new tests FAIL.

- [ ] **Step 4: Implement** as in Interfaces.

- [ ] **Step 5: Run** `python -m pytest -q` (full suite, ~8 min) — expected: all pass.

- [ ] **Step 6: Commit** `git commit -am "feat: job verify uses the plan command, skips an unchanged tree, ignores main's old failures"`

---

### Task 5: Skills and README

**Files:**
- Modify: `oke/skills/pm/SKILL.md` (plan template ~line 92-100, small-job work subagent ~line 122, review assignment ~line 180-184), `oke/skills/pm/references/merge.md` (the merge question)
- Modify: `oke/skills/worker/SKILL.md`, `oke/skills/reviewer/SKILL.md`, `oke/agents/reviewer.md`
- Modify: `oke/skills/orchestrator/SKILL.md` (plan approval)
- Modify: `README.md`, `README.ko.md`

**Interfaces:**
- Consumes: command names and exit codes from Tasks 2-4.

- [ ] **Step 1: PM skill.**
  - Plan template gets `## 수용 기준 (numbered, copied from the interview's 성공 기준)` before `## 검증 명령`; the 검증 명령 line becomes "`` `<one bash command>` `` then what passing looks like".
  - After plan approval, with the worktree created and before the worker starts: run `$OKE baseline <job>` in the background, wait for it like any long command (`expect:`).
  - Small jobs: after the work subagent answers, run `$OKE check <job>`; exit 1 goes back to a new subagent call with the new failures.
  - Review assignment header: `criteria_rev: <from the last oke check output>` next to `commit:`/`tree:`.
  - The done report and merge question: when `baseline.json` has signatures, add "main already failed N lines (warning)".
  - Changing 수용 기준 or the command after approval: allowed; the next merge needs a new `oke baseline` (if the command changed), `oke check` and review.
- [ ] **Step 2: Worker skill.** Replace "run the 검증 명령" with `$OKE check <job>`: exit 0 then report `done`; exit 1 fix and rerun; exit 2 report `blocked` with the message. Never run the command yourself to decide pass or fail.
- [ ] **Step 3: Reviewer skill and agent.** Report body: one line per 수용 기준, `- <n>: pass|fail — <evidence>`; `verdict: pass` only when every line is pass. Read the latest `check-<n>.log` named in the ledger as evidence.
- [ ] **Step 4: Orchestrator skill.** The plan approval shows `## 수용 기준` to the user with the plan.
- [ ] **Step 5: README / README.ko.md.** Document `oke baseline`, `oke check`, the two merge-gate rules and the `job verify` tree skip, in current-behaviour wording.
- [ ] **Step 6: Run** `claude plugin test oke` and `python -m pytest -q test_oke.py` (skill-size or text tests) — expected: pass.
- [ ] **Step 7: Commit** `git commit -am "docs: skills and README describe baseline, oke check and per-criterion review"`
