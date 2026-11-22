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
        self.assertEqual(oke.signature_lines(out), ["not ok <n> - y", "result mismatch at step 4"])

    def test_failure_words_keep_log_and_summary_lines(self):
        out = "INFO validation failed\nTests 1 failed | 3 passed\nTests 4 passed\n"
        self.assertEqual(oke.signature_lines(out), ["INFO validation failed", "Tests <n> failed | <n> passed"])

    def test_temp_paths_hex_and_node_pid_normalized(self):
        a = "Error in /tmp/abc123/x deadbeef01 (node:4242) [DEP0040] DeprecationWarning: p\n"
        b = "Error in /tmp/zz9/x cafebabe99 (node:7) [DEP0040] DeprecationWarning: p\n"
        self.assertEqual(oke.signature_lines(a), oke.signature_lines(b))
        w1 = "Error C:\\Users\\me\\AppData\\Local\\Temp\\tmpab12\\x.py\n"
        w2 = "Error C:\\Users\\me\\AppData\\Local\\Temp\\tmpzz99\\x.py\n"
        self.assertEqual(oke.signature_lines(w1), oke.signature_lines(w2))

    def test_linux_pytest_tmp_and_object_addresses_normalized(self):
        a = "FAILED t.py::x - assert <Obj at 0x7f3a2bc1d0e0> in /tmp/pytest-of-me/pytest-12/t0/f\n"
        b = "FAILED t.py::x - assert <Obj at 0x7f00000a1b2c> in /tmp/pytest-of-me/pytest-13/t0/f\n"
        self.assertEqual(oke.signature_lines(a), oke.signature_lines(b))

    def test_added_test_does_not_change_old_failure_signatures(self):
        # seen live: a job adding one test made every old unittest failure look new
        before = ('FAIL: test_known_bug (t.T.test_known_bug)\n'
                  'File "/w/test_calc.py", line 14, in test_known_bug\nRan 3 tests in 0.001s\n'
                  'FAILED (failures=1)\nt.py:14: AssertionError\n===== 1 failed, 2 passed in 0.1s =====\n')
        after = before.replace("line 14", "line 19").replace("Ran 3", "Ran 4").replace("t.py:14", "t.py:19") \
            .replace("2 passed", "3 passed")
        self.assertEqual(oke.signature_lines(before), oke.signature_lines(after))
        self.assertIn("FAIL: test_known_bug (t.T.test_known_bug)", oke.signature_lines(after))

    def test_other_runners_counts_do_not_move_with_added_passing_tests(self):
        pairs = [("Tests: 1 failed, 5 passed, 6 total", "Tests: 1 failed, 6 passed, 7 total"),
                 ("Test Suites: 1 failed, 2 passed, 3 total", "Test Suites: 1 failed, 3 passed, 4 total"),
                 ("Tests  1 failed | 5 passed (6)", "Tests  1 failed | 6 passed (7)"),
                 ("5 passing (12ms)\n1 failing", "6 passing (14ms)\n1 failing"),
                 ("collected 6 items", "collected 7 items"),
                 ("..F.. [100%]", "..F... [100%]"),
                 ("not ok 3 - old", "not ok 4 - old"),
                 ("> 12 | expect(x).toBe(1)\n  13 | });", "> 14 | expect(x).toBe(1)\n  15 | });")]
        for before, after in pairs:
            b = oke.signature_lines("FAIL old\n" + before)
            self.assertEqual(b, oke.signature_lines("FAIL old\n" + after), before)
        self.assertIn("not ok <n> - old", oke.signature_lines("not ok 3 - old"))

    def test_passing_lines_of_more_runners_are_noise(self):
        for line in ("test_a.py ..F. [ 50%]", "test tests::new_one ... ok", "test_new (mod.T) ... ok",
                     u"✔ new passing mocha test", "PASS src/new.test.js", "ok github.com/x/newpkg 0.01s",
                     "ok github.com/x/newpkg (cached)", "--- PASS: TestNew (0.00s)", "=== RUN TestNew"):
            self.assertEqual(oke.signature_lines("FAIL old\n" + line), ["FAIL old"], line)

    def test_more_moving_lines(self):
        pairs = [("> 11 |     expect(fn()).toThrow(TypeError);", "> 14 |     expect(fn()).toThrow(TypeError);"),
                 ("my tests/test_a.py ..F. [ 50%]", "my tests/test_a.py ..F.. [ 60%]")]
        for before, after in pairs:
            self.assertEqual(oke.signature_lines("FAIL old\n" + before), oke.signature_lines("FAIL old\n" + after))
        for line in ("test_x (m.T) ... skipped 'slow'", "--- SKIP: TestY (0.00s)", "=== NAME TestX",
                     "?   example.com/p [no test files]"):
            self.assertEqual(oke.signature_lines("FAIL old\n" + line), ["FAIL old"], line)
        self.assertEqual(len(oke.signature_lines("not ok 3 -\nnot ok 4 -")), 2)
        self.assertIn("PASS a.sh FAILED", oke.signature_lines("PASS a.sh FAILED"))
        for line in ("--- PASS: TestParseError (0.00s)", "=== RUN   TestParseError", "PASS src/utils/errors.test.js",
                     "test_error (tests.test_a.TestA) ... ok", "test_raises_ValueError (t.T) ... skipped 'no net'",
                     u"✔ reports failure to caller (3ms)"):
            self.assertEqual(oke.signature_lines("FAIL old\n" + line), ["FAIL old"], line)
        self.assertEqual(oke.signature_lines("FAIL old\ntests/test_errors.py ....F [ 50%]"),
                         oke.signature_lines("FAIL old\ntests/test_errors.py .....F [ 50%]"))
        self.assertEqual(oke.signature_lines("tests/test_exceptions.py ... [100%]"), [])
        for line in ("." * 87 + " 88/120", "..i.F", "[gw0] [ 50%] PASSED tests/a.py::t",
                     "[gw1] [ 60%] SKIPPED tests/a.py::u"):
            self.assertEqual(oke.signature_lines("FAIL old\n" + line), ["FAIL old"], line)
        self.assertIn("FAILED tests/a.py::t", oke.signature_lines("[gw0] [ 50%] FAILED tests/a.py::t"))
        pairs = [("t.py::b FAILED [ 50%]", "t.py::b FAILED [ 40%]"),
                 ("[gw1] [ 66%] FAILED t.py::b", "[gw0] [ 57%] FAILED t.py::b"),
                 ("gw0 [12] / gw1 [12]", "gw0 [13] / gw1 [13]")]
        for before, after in pairs:
            self.assertEqual(oke.signature_lines("FAIL old\n" + before), oke.signature_lines("FAIL old\n" + after))
        line = "FAILED tests/test_io.py::test_read - OSError: cannot open data.py ."
        self.assertEqual(oke.signature_lines(line), [line])

    def test_unnamed_tap_failures_and_result_tables_stay_apart(self):
        self.assertEqual(len(oke.signature_lines("not ok 3\nnot ok 7")), 2)
        self.assertEqual(oke.signature_lines("12 | test_login | FAILED"), ["<n> | test_login | FAILED"])

    def test_cwd_edge_cases(self):
        self.assertEqual(oke.signature_lines("rootdir: /home/u/wt, configfile: pytest.ini failed", "/home/u/wt"),
                         ["rootdir: <cwd>, configfile: pytest.ini failed"])
        self.assertEqual(oke.signature_lines("Error in /home/u/wt.bak/x", "/home/u/wt"), ["Error in /home/u/wt.bak/x"])
        self.assertEqual(oke.signature_lines("Generic: boom failed", "/"), ["Generic: boom failed"])
        self.assertEqual(oke.signature_lines("basic: x failed", "c:/"), ["basic: x failed"])
        self.assertEqual(oke.signature_lines("Error in C:\\w\\wt2\\a.py", "C:\\w\\wt"), ["Error in C:\\w\\wt2\\a.py"])

    def test_run_directory_is_normalized(self):
        # the baseline may run in a temporary checkout, the check in the worktree, verify in main
        a = 'File "C:\\work\\wt-a\\test_calc.py", line 3, in x\nFAIL: x (t.x)\n'
        b = 'File "c:\\work\\wt-b\\test_calc.py", line 3, in x\nFAIL: x (t.x)\n'
        self.assertEqual(oke.signature_lines(a, "C:\\work\\wt-a"), oke.signature_lines(b, "C:/work/wt-b"))

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
            self.assertEqual(oke.run_signatures("echo TypeError: fine", d, log), entry(0))
            self.assertEqual(oke.run_signatures("echo boom; exit 3", d, log), entry(3, ["boom"]))
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
        self.git(self.wt, "add", "a.txt")
        self.git(self.wt, "commit", "-q", "-m", "a")
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
        self.git(self.wt, "add", "b.txt")
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

    def test_uncommitted_worktree_change_is_recorded_as_a_tree(self):
        self.plan("true")
        self.write(os.path.join(self.wt, "c.txt"), "c\n")
        self.git(self.wt, "add", "c.txt")
        self.git(self.wt, "commit", "-q", "-m", "c")
        self.assertEqual(self.cli("baseline", "q"), 0, self.err)
        self.write(os.path.join(self.wt, "c.txt"), "changed\n")
        self.assertEqual(self.cli("check", "q"), 0, self.err)
        target = self.rows()[-1]["commit"]
        self.assertTrue(target.startswith("tree:"), target)
        self.assertNotEqual(target[5:], self.git_out(self.wt, "rev-parse", "HEAD^{tree}"))

    def test_untracked_work_file_refuses_the_check(self):
        # a forgotten `git add`: the check would pass on a file the merge never carries
        self.plan("true")
        self.assertEqual(self.cli("baseline", "q"), 0, self.err)
        self.write(os.path.join(self.wt, "new_module.py"), "x = 1\n")
        self.assertEqual(self.cli("check", "q"), 2)
        self.assertIn("new_module.py", self.err)
        self.assertEqual(self.rows(), [])
        self.write(os.path.join(self.wt, "__pycache__", "m.pyc"), "x")  # a byproduct does not count
        os.remove(os.path.join(self.wt, "new_module.py"))
        self.assertEqual(self.cli("check", "q"), 0, self.err)

    def test_rebaseline_after_work_still_sees_the_base_code(self):
        # a worker cannot erase its own failure by recording a new baseline
        self.plan('test -f broken.txt && { echo "FAILED broken"; exit 1; }; exit 0')
        self.assertEqual(self.cli("baseline", "q"), 0, self.err)
        self.write(os.path.join(self.wt, "broken.txt"), "x\n")
        self.git(self.wt, "add", "broken.txt")
        self.git(self.wt, "commit", "-q", "-m", "work")
        self.assertEqual(self.cli("check", "q"), 1)
        self.assertEqual(self.cli("baseline", "q"), 0, self.err)
        with open(os.path.join(self.home, "jobs", "q", "baseline.json")) as f:
            self.assertEqual(json.load(f)["exit"], 0)
        self.assertEqual(self.cli("check", "q"), 1)
        self.assertEqual(self.git_out(self.repo, "worktree", "list").count("\n"), 1)  # temp checkout removed

    def test_unrunnable_command_is_never_a_pass(self):
        self.plan("no-such-command-oke-xyz")
        self.assertEqual(self.cli("baseline", "q"), 2)
        self.assertIn("could not run", self.err)
        self.assertEqual(self.cli("check", "q"), 2)  # unrunnable at baseline too: still no pass
        self.assertEqual(self.rows(), [])

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
