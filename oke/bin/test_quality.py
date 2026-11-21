# -*- coding: utf-8 -*-
"""Quality protocol: failure signatures, baseline, oke check."""
import json
import os
import subprocess
import tempfile
import unittest

import oke
from test_oke import SpawnBase

PYTEST_A = ("tests/t.py::test_div FAILED [ 50%]\nFAILED tests/t.py::test_div - ZeroDivisionError\n"
            "===== 1 failed, 4 passed in 3.21s (0:00:03) =====\n....F\n")
PYTEST_B = PYTEST_A.replace("3.21s (0:00:03)", "9.80s (0:00:09)")


def entry(exit, signatures=(), unrunnable=False):
    return {"exit": exit, "unrunnable": unrunnable, "signatures": list(signatures)}


class TestSignatures(unittest.TestCase):
    def test_volatile_values_give_equal_signatures(self):
        self.assertEqual(oke.signature_lines(PYTEST_A), oke.signature_lines(PYTEST_B))
        self.assertIn("FAILED tests/t.py::test_div - ZeroDivisionError", oke.signature_lines(PYTEST_A))

    def test_noise_is_dropped_and_plain_lines_kept(self):
        out = (u"\x1b[31mresult mismatch at step 4\x1b[0m\n\u2713 adds\nok 1 - x\n1..3\n# pass 2\n# fail 0\n"
               u"INFO starting\nnpm warn deprecated\n.....\n12 passed in 0.5s\n  at foo (a.js:1:1)\n"
               u"12| const x = 1\nnot ok 2 - y\n")
        self.assertEqual(oke.signature_lines(out), ["not ok 2 - y", "result mismatch at step 4"])

    def test_failure_words_keep_log_and_summary_lines(self):
        out = "INFO validation failed\nTests 1 failed | 3 passed\nTests 4 passed\n"
        self.assertEqual(oke.signature_lines(out), ["INFO validation failed", "Tests 1 failed | 3 passed"])

    def test_temp_paths_hex_and_node_pid_normalized(self):
        a = "Error in /tmp/abc123/x deadbeef01 (node:4242) [DEP0040] DeprecationWarning: p\n"
        b = "Error in /tmp/zz9/x cafebabe99 (node:7) [DEP0040] DeprecationWarning: p\n"
        self.assertEqual(oke.signature_lines(a), oke.signature_lines(b))
        w1 = "Error C:\\Users\\me\\AppData\\Local\\Temp\\tmpab12\\x.py\n"
        w2 = "Error C:\\Users\\me\\AppData\\Local\\Temp\\tmpzz99\\x.py\n"
        self.assertEqual(oke.signature_lines(w1), oke.signature_lines(w2))

    def test_cap_and_long_lines(self):
        self.assertEqual(len(oke.signature_lines("\n".join("E%d boom" % i for i in range(300)))), 200)
        self.assertEqual(oke.signature_lines("x" * 501), [])

    def test_diff_green_to_red_is_new_even_without_lines(self):
        new, _ = oke.diff_baseline(entry(0), entry(1))
        self.assertEqual(new, ["<exited with code 1>"])

    def test_diff_red_reports_only_new_lines_and_counts_resolved(self):
        new, resolved = oke.diff_baseline(entry(1, ["old", "gone"]), entry(1, ["old", "fresh"]))
        self.assertEqual((new, resolved), (["fresh"], ["gone"]))

    def test_diff_unrunnable_and_no_baseline(self):
        new, _ = oke.diff_baseline(entry(1), entry(127, unrunnable=True))
        self.assertEqual(new, ["<command became unrunnable>"])
        self.assertEqual(oke.diff_baseline(None, entry(2, ["x"]))[0], ["<exited with code 2>", "x"])

    def test_run_signatures_ignores_output_on_success(self):
        d = tempfile.mkdtemp()
        old = os.environ.get("OKE_HOME")
        os.environ["OKE_HOME"] = d
        try:
            log = os.path.join(d, "x.log")
            self.assertEqual(oke.run_signatures("echo TypeError: fine", d, log, "t"), entry(0))
            self.assertEqual(oke.run_signatures("echo boom; exit 3", d, log, "t"), entry(3, ["boom"]))
        finally:
            if old is None:
                os.environ.pop("OKE_HOME")
            else:
                os.environ["OKE_HOME"] = old


class TestBaselineCheck(SpawnBase):
    def setUp(self):
        SpawnBase.setUp(self)
        self.job("q", "working")
        self.wt = self.git_worktree(self.repo, "wt")
        state = oke.load_state()
        state["jobs"]["q"]["worktree"] = {"id": "wt", "path": self.wt}
        oke.save_state(state)

    def plan(self, command, criteria="1. a.txt exists"):
        self.write(os.path.join(self.home, "jobs", "q", "plan.md"),
                   u"job: q\n---\n## 접근\nx\n## 수용 기준\n%s\n## 검증 명령        (what passing is)\n"
                   u"`%s` passes\n## 다른 레포 영향\n없음\n" % (criteria, command))

    def rows(self):
        return [r for r in oke.ledger_rows("q") if r["check"] == "check"]

    def test_criteria_rev_changes_with_command_or_criteria(self):
        self.plan("true")
        a = oke.criteria_rev("q")
        self.plan("true")
        self.assertEqual(oke.criteria_rev("q"), a)
        self.plan("false")
        b = oke.criteria_rev("q")
        self.plan("true", "1. other")
        self.assertEqual(len({a, b, oke.criteria_rev("q")}), 3)
        self.assertEqual(oke.plan_command("q"), "true")

    def test_check_without_baseline_exits_2_and_writes_no_row(self):
        self.plan("true")
        self.assertEqual(self.cli("check", "q"), 2)
        self.assertIn("oke baseline", self.err)
        self.assertEqual(self.rows(), [])

    def test_baseline_then_pass(self):
        self.plan('test -f a.txt || { echo "FAILED old"; exit 1; }')
        self.assertEqual(self.cli("baseline", "q"), 0, self.err)
        with open(os.path.join(self.home, "jobs", "q", "baseline.json")) as f:
            base = json.load(f)
        self.assertEqual((base["exit"], base["signatures"]), (1, ["FAILED old"]))
        self.write(os.path.join(self.wt, "a.txt"), "a\n")
        self.assertEqual(self.cli("check", "q"), 0, self.err)
        row = self.rows()[-1]
        self.assertEqual(row["result"], "pass")
        self.assertEqual(row["detail"].split()[0], oke.criteria_rev("q"))
        self.assertEqual(row["commit"], self.git_out(self.wt, "rev-parse", "HEAD"))

    def test_new_line_fails_old_line_does_not(self):
        self.plan('echo "FAILED old"; test -f b.txt && echo "FAILED new"; exit 1')
        self.assertEqual(self.cli("baseline", "q"), 0, self.err)
        self.assertEqual(self.cli("check", "q"), 0, self.err)
        self.write(os.path.join(self.wt, "b.txt"), "b\n")
        self.assertEqual(self.cli("check", "q"), 1)
        self.assertEqual(json.loads(self.out)["new"], ["FAILED new"])
        self.assertEqual(self.rows()[-1]["result"], "fail")

    def test_stale_baseline_refused(self):
        self.plan("true")
        self.assertEqual(self.cli("baseline", "q"), 0, self.err)
        self.plan("true && true")
        self.assertEqual(self.cli("check", "q"), 2)
        self.assertIn("oke baseline", self.err)

    def test_rebaseline_keeps_history(self):
        self.plan('echo "FAILED one"; exit 1')
        self.assertEqual(self.cli("baseline", "q"), 0, self.err)
        self.plan('echo "FAILED two"; exit 1')
        self.assertEqual(self.cli("baseline", "q"), 0, self.err)
        out = json.loads(self.out)
        self.assertEqual((out["added"], out["removed"]), (1, 1))
        self.assertEqual(len(os.listdir(os.path.join(self.home, "jobs", "q", "baseline.history"))), 1)

    def test_ledger_add_cannot_forge_check(self):
        self.assertEqual(self.cli("ledger", "add", "q", "--by", "w", "--commit", "abc", "--check", "check",
                                  "--result", "pass"), 1)
        self.assertEqual(self.rows(), [])

    def test_no_worktree_records_tree(self):
        state = oke.load_state()
        state["jobs"]["q"]["worktree"] = None
        oke.save_state(state)
        self.plan("true")
        self.assertEqual(self.cli("baseline", "q"), 0, self.err)
        self.assertEqual(self.cli("check", "q"), 0, self.err)
        self.assertEqual(self.rows()[-1]["commit"], "tree:" + oke.pending_tree(self.repo))

    def git_out(self, cwd, *args):
        return subprocess.check_output(["git", "-C", cwd] + list(args)).decode().strip()


if __name__ == "__main__":
    unittest.main()
