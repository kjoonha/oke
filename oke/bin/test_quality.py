# -*- coding: utf-8 -*-
"""Quality protocol: failure signatures, oke check and its base run."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
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
        # the baseline may run in a temporary checkout, the check in the worktree
        a = 'File "C:\\work\\wt-a\\test_calc.py", line 3, in x\nFAIL: x (t.x)\n'
        b = 'File "c:\\work\\wt-b\\test_calc.py", line 3, in x\nFAIL: x (t.x)\n'
        self.assertEqual(oke.signature_lines(a, "C:\\work\\wt-a"), oke.signature_lines(b, "C:/work/wt-b"))

    def test_cap_and_long_lines(self):
        self.assertEqual(len(oke.signature_lines("\n".join("E%d boom" % i for i in range(300)))), 200)
        self.assertEqual(oke.signature_lines("x" * 501), [])

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
                   u"job: q\n---\n## Approach\nx\n## Acceptance criteria\n%s\n## Check command        (what passing is)\n"
                   u"`%s` passes\n## Impact elsewhere\nnone\n" % (criteria, command))

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

    def test_a_pass_needs_no_base_run(self):
        self.plan("true")
        self.assertEqual(self.cli("check", "q"), 0, self.err)
        out = json.loads(self.out)
        self.assertEqual((out["result"], out["new"], out["pre_existing"]), ("pass", [], []))
        self.assertFalse(os.path.exists(os.path.join(self.home, "jobs", "q", "baseline.json")))
        self.assertEqual([r["result"] for r in self.rows()], ["pass"])

    def test_the_baseline_command_is_a_no_op(self):
        self.plan("true")
        self.assertEqual(self.cli("baseline", "q"), 0, self.err)
        self.assertEqual(json.loads(self.out)["result"], "not_needed")
        self.assertEqual(self.rows(), [])

    def test_a_failure_the_base_has_too_is_pre_existing(self):
        self.plan("cat fails.txt 2>/dev/null; echo FAILED old_case; exit 1")
        self.assertEqual(self.cli("check", "q"), 0, self.err)
        out = json.loads(self.out)
        self.assertEqual((out["result"], out["new"], out["pre_existing"]), ("pass", [], ["FAILED old_case"]))
        self.assertTrue(os.path.exists(os.path.join(self.home, "jobs", "q", "baseline.json")))
        # the temporary checkout of the base is gone again
        self.assertFalse(os.path.exists(os.path.join(self.home, "tmp", "base-q")))
        self.assertNotIn("base-q", self.git_out(self.repo, "worktree", "list"))
        self.write(os.path.join(self.wt, "fails.txt"), "FAILED new_case\n")
        self.assertEqual(self.cli("check", "q"), 1, self.err)
        out = json.loads(self.out)
        self.assertEqual((out["new"], out["pre_existing"]), (["FAILED new_case"], ["FAILED old_case"]))
        self.assertEqual([r["result"] for r in self.rows()], ["pass", "fail"])

    def test_a_base_failure_that_names_a_changed_file_is_a_regression(self):
        self.plan("echo FAILED calc.py::test_add; exit 1")
        self.assertEqual(self.cli("check", "q"), 0, self.err)  # the job changed nothing yet
        self.assertEqual(json.loads(self.out)["pre_existing"], ["FAILED calc.py::test_add"])
        self.write(os.path.join(self.wt, "calc.py"), "x = 1\n")  # a new file counts as the job's
        self.assertEqual(self.cli("check", "q"), 1, self.err)
        self.assertEqual(json.loads(self.out)["new"], ["FAILED calc.py::test_add"])

    def test_a_base_that_passed_makes_any_failure_new(self):
        self.plan("test ! -f broken.txt")
        self.write(os.path.join(self.wt, "broken.txt"), "x\n")
        self.assertEqual(self.cli("check", "q"), 1, self.err)
        self.assertEqual(json.loads(self.out)["new"][0], "<exited with code 1>")

    def test_a_base_that_cannot_run_fails_the_check(self):
        self.plan("bash check.sh")  # bash: a missing script is exit 127 everywhere (dash says 2)
        self.write(os.path.join(self.wt, "check.sh"), "echo FAILED x\nexit 1\n")  # not on the base
        self.assertEqual(self.cli("check", "q"), 1, self.err)
        self.assertIn("could not check the base: ", self.err)
        self.assertEqual(self.rows()[-1]["result"], "fail")

    def test_unrunnable_command_is_never_a_pass(self):
        self.plan("definitely-not-a-command-xyz")
        self.assertEqual(self.cli("check", "q"), 2)
        self.assertIn("could not run", self.err)
        self.assertEqual(self.rows(), [])

    def test_a_plan_too_big_is_refused(self):
        self.plan("true", "1. " + "x" * 2100)
        self.assertEqual(self.cli("check", "q"), 2)
        self.assertIn("at most 2000", self.err)
        self.assertEqual(self.cli("job", "plan", "q"), 1)
        self.assertIn("Shorten it", self.err)
        self.plan("true", "\n".join("%d. criterion %s" % (i, "y" * 60) for i in range(400)))
        self.assertEqual(self.cli("job", "plan", "q"), 1)
        self.assertIn("KB", self.err)
        self.plan("true")
        self.assertEqual(self.cli("job", "plan", "q"), 0, self.err)

    def test_the_brief_cuts_long_lists(self):
        text = "\n".join(["Continues: " + ", ".join("src/file%03d.py" % i for i in range(200))]
                         + ["line %d" % i for i in range(60)])
        lines = oke.brief_lines(text)
        self.assertLessEqual(len(lines[0]), oke.BRIEF_CHARS + 30)
        self.assertIn("more", lines[0])
        self.assertEqual(lines[-1], "... 21 more lines in plan.md")

    def test_an_in_place_result_is_checked_reviewed_and_left_uncommitted(self):
        st = oke.load_state()
        st["jobs"]["q"].update(output="result", worktree=None)
        oke.save_state(st)
        self.plan("test -f report.md")
        self.write(os.path.join(self.repo, "mine.txt"), "the user's\n")  # left alone
        self.write(os.path.join(self.repo, "report.md"), "the result\n")
        self.write(os.path.join(self.home, "jobs", "q", "q-rv", "report.md"), "seq: 1\n---\nverdict: pass\n")
        head = self.git_out(self.repo, "rev-parse", "HEAD")
        self.assertEqual(self.cli("job", "deliver", "q"), 1)
        self.assertIn("no recorded run", self.err)
        self.assertEqual(self.cli("check", "q"), 0, self.err)
        self.assertEqual(self.cli("job", "deliver", "q"), 0, self.err)
        out = json.loads(self.out)
        self.assertEqual((out["result"], out["committed"]), ("in_place", False))
        self.assertIn("report.md", out["results"])
        self.assertEqual(self.git_out(self.repo, "rev-parse", "HEAD"), head)
        self.assertIn("mine.txt", self.git_out(self.repo, "status", "--porcelain"))

    def test_ledger_add_cannot_forge_check(self):
        self.assertEqual(self.cli("ledger", "add", "q", "--by", "w", "--commit", "abc", "--check", "check",
                                  "--result", "pass"), 1)
        self.assertEqual(self.rows(), [])

    def test_no_worktree_records_tree(self):
        state = oke.load_state()
        state["jobs"]["q"]["worktree"] = None
        oke.save_state(state)
        self.plan("true")
        self.write(os.path.join(self.repo, "a.txt"), "a\n")
        self.assertEqual(self.cli("check", "q"), 0, self.err)
        tree, files = oke.job_tree("q", oke.load_state()["jobs"]["q"], self.repo)
        self.assertEqual(self.rows()[-1]["commit"], "tree:" + tree)
        self.assertEqual(self.cli("job", "tree", "q"), 0, self.err)
        self.assertEqual(json.loads(self.out)["tree"], tree)

    def git_out(self, cwd, *args):
        return subprocess.check_output(["git", "-C", cwd] + list(args)).decode().strip()


class TestCheckInPlace(SpawnBase):
    """A job in its checkout: what the Check command leaves, the base run's checkout, its leftovers."""
    plan, rows, git_out = TestBaselineCheck.plan, TestBaselineCheck.rows, TestBaselineCheck.git_out

    def setUp(self):
        SpawnBase.setUp(self)
        self.job("q", "working")
        self.restart()

    def restart(self):
        """The job begins now: what is uncommitted in the checkout is the user's."""
        if os.path.exists(oke.snapshot_path("q")):
            os.remove(oke.snapshot_path("q"))
        self.assertTrue(oke.snapshot_dirty("q", oke.load_state()["jobs"]["q"]))

    def worktrees(self):
        return self.git_out(self.repo, "worktree", "list", "--porcelain")

    def test_what_the_check_makes_is_left_out_and_the_tree_settles(self):
        self.plan("echo $RANDOM$RANDOM$$ > run.log; mkdir -p out; date +%s%N > out/stamp.txt; true")
        self.write(os.path.join(self.repo, "a.txt"), "a\n")  # the job's
        self.assertEqual(self.cli("check", "q"), 0, self.err)
        first = json.loads(self.out)
        self.assertEqual(sorted(first["check_left"]), ["out/stamp.txt", "run.log"])
        self.assertEqual(self.cli("check", "q"), 0, self.err)  # another run writes other bytes
        second = json.loads(self.out)
        self.assertEqual(first["commit"], second["commit"])
        self.assertEqual(self.cli("job", "tree", "q"), 0, self.err)
        out = json.loads(self.out)
        self.assertEqual("tree:" + out["tree"], second["commit"])
        self.assertEqual((out["files"], out["new_files"]), (1, ["a.txt"]))
        self.assertEqual(sorted(out["check_left"]), ["out/stamp.txt", "run.log"])
        self.assertIn("left by the check (not committed): out/stamp.txt, run.log", oke.job_brief("q"))
        self.write(os.path.join(self.repo, "run.log"), "the worker wrote it\n")  # changed after: the job's
        self.assertIn("run.log", oke.job_files("q", oke.load_state()["jobs"]["q"], self.repo)["paths"])

    def test_the_base_run_has_the_users_files_and_installed_folders(self):
        self.write(os.path.join(self.repo, ".gitignore"), "node_modules/\n")
        self.git(self.repo, "add", ".gitignore")
        self.git(self.repo, "commit", "-q", "-m", "ignore deps")
        self.write(os.path.join(self.repo, "node_modules", "lib.txt"), "installed\n")
        self.write(os.path.join(self.repo, "conf.txt"), "the user's\n")  # uncommitted when the job begins
        self.restart()
        self.plan("if test -f conf.txt && test -f node_modules/lib.txt; then echo FAILED old_case; fi; exit 1")
        self.assertEqual(self.cli("check", "q"), 0, self.err)
        out = json.loads(self.out)
        self.assertEqual((out["new"], out["pre_existing"]), ([], ["FAILED old_case"]))
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "node_modules", "lib.txt")))  # the link went, not its target
        self.assertFalse(os.path.exists(os.path.join(self.home, "tmp", "base-q")))
        self.assertNotIn("base-q", self.worktrees())

    def test_failure_lines_naming_the_users_files_are_pre_existing(self):
        self.write(os.path.join(self.repo, "draft.py"), "x\n")  # the user's
        self.restart()
        self.plan("if test -f x.txt; then echo FAILED draft.py::test_x; else echo FAILED other; fi; exit 1")
        self.write(os.path.join(self.repo, "x.txt"), "x\n")  # the job's: without it the base fails otherwise
        self.assertEqual(self.cli("check", "q"), 0, self.err)
        self.assertEqual(json.loads(self.out)["pre_existing"], ["FAILED draft.py::test_x"])

    def test_a_killed_base_run_is_cleaned_up_and_other_worktrees_stay(self):
        where = os.path.join(self.home, "tmp", "base-q")
        self.git(self.repo, "worktree", "add", "-q", "--detach", where, "HEAD")  # a run killed midway
        target = self.path("installed")
        self.write(os.path.join(target, "keep.txt"), "x\n")
        link = os.path.join(where, "node_modules")
        if os.name == "nt":
            subprocess.check_call(["cmd", "/c", "mklink", "/J", link, target], stdout=subprocess.DEVNULL)
        else:
            os.symlink(target, link)
        gone = self.path("unmounted")  # the user's worktree on a drive that is not there now
        self.git(self.repo, "worktree", "add", "-q", "--detach", gone, "HEAD")
        os.rename(gone, gone + "-away")
        self.plan("true")
        self.assertEqual(self.cli("check", "q"), 0, self.err)
        self.assertFalse(os.path.exists(where))
        self.assertTrue(os.path.isfile(os.path.join(target, "keep.txt")))
        listed = self.worktrees()
        self.assertNotIn("base-q", listed)
        self.assertIn("unmounted", listed)  # never pruned by OKe

    def test_a_base_run_leaves_no_worktree(self):
        self.plan("exit 1")
        self.assertEqual(self.cli("check", "q"), 0, self.err)  # the base fails the same way
        self.assertTrue(os.path.exists(os.path.join(self.home, "jobs", "q", "baseline.json")))
        self.assertNotIn("base-q", self.worktrees())
        self.assertEqual(os.listdir(os.path.join(self.home, "tmp")), [])

    def test_a_base_that_cannot_run_is_accepted_only_by_the_user(self):
        self.plan("bash check.sh")  # bash: a missing script is exit 127 everywhere (dash says 2)
        self.write(os.path.join(self.repo, "check.sh"), "echo FAILED x\nexit 1\n")  # not on the base
        self.assertEqual(self.cli("check", "q"), 1, self.err)
        out = json.loads(self.out)
        self.assertTrue(out["base_unrunnable"])
        self.assertIn("job accept-check q " + out["commit"], out["next"])
        self.assertEqual(self.cli("job", "accept-check", "q", "tree:0000"), 1)
        self.assertEqual(self.cli("job", "accept-check", "q", out["commit"]), 0, self.err)
        row = self.rows()[-1]
        self.assertEqual((row["result"], row["by"], row["commit"]), ("pass", "user", out["commit"]))
        self.assertEqual(row["detail"].split()[:2], [oke.criteria_rev("q"), "accepted_by_user"])
        self.assertEqual(self.cli("job", "accept-check", "q", out["commit"]), 1)  # the last row is a pass now

    def test_a_result_is_run_once_and_recorded(self):
        st = oke.load_state()
        st["jobs"]["q"]["output"] = "result"
        oke.save_state(st)
        self.plan("echo the result > report.md; exit 3")
        self.write(os.path.join(self.repo, "sim.cfg"), "steps=2\n")  # the temporary edit
        self.assertEqual(self.cli("check", "q"), 0, self.err)
        out = json.loads(self.out)
        self.assertEqual((out["result"], out["exit"], out["check_left"]), ("ran", 3, ["report.md"]))
        self.assertFalse(os.path.exists(os.path.join(self.home, "jobs", "q", "baseline.json")))  # no second run
        self.assertEqual(self.rows()[-1]["result"], "ran")
        self.write(os.path.join(self.home, "jobs", "q", "q-rv", "report.md"), "seq: 1\n---\nverdict: pass\n")
        self.assertEqual(self.cli("job", "deliver", "q"), 0, self.err)
        self.assertIn("report.md", json.loads(self.out)["results"])
        self.write(os.path.join(self.repo, "sim.cfg"), "steps=3\n")  # the edit changed after the run
        self.assertEqual(self.cli("job", "deliver", "q"), 1)
        self.assertIn("no recorded run", self.err)

    def test_a_hanging_check_is_stopped_and_leaves_no_worktree(self):
        with open(os.path.join(self.home, "config.json"), "w") as f:
            json.dump({"check_timeout": 0.03}, f)  # 1.8 seconds
        self.plan("sleep 60; true")
        start = time.time()
        self.assertEqual(self.cli("check", "q"), 1, self.err)
        out = json.loads(self.out)
        self.assertTrue(out["timed_out"])
        self.assertIn("check_timeout", out["new"][0])
        self.assertFalse(os.path.exists(os.path.join(self.home, "jobs", "q", "baseline.json")))  # no base run
        # it hangs only on the base: the base run is stopped too, and its worktree removed
        self.plan("test -f x.txt || sleep 60; exit 1")
        self.write(os.path.join(self.repo, "x.txt"), "x\n")
        self.assertEqual(self.cli("check", "q"), 1, self.err)
        out = json.loads(self.out)
        self.assertTrue(out["base_unrunnable"])
        self.assertIn("time limit", self.err)
        self.assertLess(time.time() - start, 45)
        self.assertFalse(os.path.exists(os.path.join(self.home, "tmp", "base-q")))
        self.assertNotIn("base-q", self.worktrees())

    def test_the_base_run_links_only_dependency_folders_and_copies_small_ignored_files(self):
        self.write(os.path.join(self.repo, ".gitignore"), "node_modules/\ndist/\n.pytest_cache/\n.env\nbig.bin\n")
        self.git(self.repo, "add", ".gitignore")
        self.git(self.repo, "commit", "-q", "-m", "ignore")
        self.write(os.path.join(self.repo, "node_modules", "lib.txt"), "installed\n")
        self.write(os.path.join(self.repo, "dist", "out.js"), "built\n")
        self.write(os.path.join(self.repo, ".pytest_cache", "v"), "cache\n")
        self.write(os.path.join(self.repo, ".env"), "KEY=1\n")
        with open(os.path.join(self.repo, "big.bin"), "wb") as f:
            f.write(b"x" * (oke.BASE_COPY_FILE + 1))
        self.restart()
        # on the base only: the dependency folder and the small file are there, the outputs and the big file not
        self.plan("echo made > node_modules/made.txt; if test -f node_modules/lib.txt && test -f .env && ! test -e dist "
                  "&& ! test -e .pytest_cache && ! test -e big.bin; then echo FAILED base_ok; else echo FAILED here; fi; "
                  "exit 1")
        self.assertEqual(self.cli("check", "q"), 1, self.err)
        self.assertEqual(json.loads(self.out)["new"], ["FAILED here"])
        base = oke.read_json(os.path.join(self.home, "jobs", "q", "baseline.json"), None)
        self.assertEqual(base["signatures"], ["FAILED base_ok"])
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "dist", "out.js")))
        self.assertTrue(os.path.isfile(os.path.join(self.repo, ".env")))
        # a known limit: what the run writes into a linked folder lands in the user's (planner.md: never install)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "node_modules", "made.txt")))

    def test_a_tracked_file_the_check_changes_is_the_jobs_change(self):
        self.write(os.path.join(self.repo, "gen.txt"), "generated\n")
        self.git(self.repo, "add", "gen.txt")
        self.git(self.repo, "commit", "-q", "-m", "generated file")
        self.restart()
        self.plan("date +%s%N > gen.txt; date +%s%N > run.log; true")
        self.write(os.path.join(self.repo, "a.txt"), "a\n")
        self.assertEqual(self.cli("check", "q"), 0, self.err)
        self.assertEqual(json.loads(self.out)["check_left"], ["run.log"])
        self.assertNotIn("gen.txt", oke.load_state()["jobs"]["q"].get("byproducts") or {})
        self.assertEqual(self.cli("job", "tree", "q"), 0, self.err)
        out = json.loads(self.out)
        self.assertEqual((out["files"], out["check_left"]), (2, ["run.log"]))  # a.txt and gen.txt
        # the user said a byproduct goes with the job: it does
        self.assertEqual(self.cli("job", "files", "q", "include", "run.log"), 0, self.err)
        self.assertIn("run.log", oke.job_files("q", oke.load_state()["jobs"]["q"], self.repo)["paths"])

    def test_commits_in_the_checkout_stop_the_check_until_the_user_accepts_them(self):
        self.plan("true")
        self.write(os.path.join(self.repo, "other.txt"), "x\n")
        self.git(self.repo, "add", "other.txt")
        self.git(self.repo, "commit", "-q", "-m", "someone's commit")
        self.assertEqual(self.cli("check", "q"), 2)
        out = json.loads(self.out)
        self.assertEqual(out["result"], "head_moved")
        self.assertEqual(len(out["commits"]), 1)
        self.assertIn("someone's commit", out["commits"][0])
        self.assertIn("asks: head_moved", out["next"])
        self.assertEqual(self.rows(), [])  # nothing ran
        self.assertEqual(self.cli("job", "tree", "q"), 0, self.err)
        self.assertEqual(len(json.loads(self.out)["head_moved"]), 1)
        self.assertEqual(self.cli("job", "accept-head", "q"), 0, self.err)
        self.assertEqual(self.cli("check", "q"), 0, self.err)
        self.assertEqual(self.cli("job", "tree", "q"), 0, self.err)
        self.assertNotIn("head_moved", json.loads(self.out))

    def test_job_auto_never_accepts_for_the_user(self):
        self.plan("bash check.sh")  # bash: a missing script is exit 127 everywhere (dash says 2)
        self.write(os.path.join(self.repo, "check.sh"), "echo FAILED x\nexit 1\n")  # not on the base
        self.assertEqual(self.cli("check", "q"), 1, self.err)
        target = json.loads(self.out)["commit"]
        self.assertIn("asks: check_accept", json.loads(self.out)["next"])
        self.assertEqual(self.cli("job", "auto", "q"), 0, self.err)
        self.assertEqual(self.cli("job", "accept-check", "q", target), 1)
        self.assertIn("never answers this", self.err)
        self.assertEqual(self.cli("job", "accept-head", "q"), 1)
        self.assertIn("asks: head_moved", self.err)
        self.assertEqual(self.cli("job", "accept-check", "q", target, "--by-user"), 0, self.err)
        self.assertEqual(self.cli("job", "accept-head", "q", "--by-user"), 0, self.err)

    def test_a_failure_the_base_has_too_that_names_a_changed_file_may_be_accepted(self):
        self.plan("echo FAILED calc.py::test_add; exit 1")
        self.write(os.path.join(self.repo, "calc.py"), "x = 1\n")  # the job's
        self.assertEqual(self.cli("check", "q"), 1, self.err)
        out = json.loads(self.out)
        self.assertEqual((out["new"], out["base_has_it"]), (["FAILED calc.py::test_add"], ["FAILED calc.py::test_add"]))
        self.assertIn("asks: check_accept", out["next"])
        self.assertIn("base_has_it=1", self.rows()[-1]["detail"])
        self.assertEqual(self.cli("job", "accept-check", "q", out["commit"]), 0, self.err)
        self.assertEqual(self.rows()[-1]["result"], "pass")

    def test_a_new_failure_the_base_lacks_is_never_accepted(self):
        self.plan("test -f calc.py && echo FAILED calc.py::test_sub; echo FAILED calc.py::test_add; exit 1")
        self.write(os.path.join(self.repo, "calc.py"), "x = 1\n")
        self.assertEqual(self.cli("check", "q"), 1, self.err)
        out = json.loads(self.out)
        self.assertEqual(out["base_has_it"], ["FAILED calc.py::test_add"])
        self.assertNotIn("next", out)
        self.assertEqual(self.cli("job", "accept-check", "q", out["commit"]), 1)

    def test_only_the_recorded_orchestrator_runs_its_job_actions(self):
        for argv in (["job", "auto", "q"], ["job", "accept-head", "q"], ["job", "phase", "q", "reviewing"]):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = oke.main(argv)  # no runtime place and no recorded session: refused
            self.assertEqual(code, 1, argv)
            self.assertIn("only the orchestrator", err.getvalue())
        os.environ["CLAUDE_CODE_SESSION_ID"] = "someone-else"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(oke.main(["job", "auto", "q"]), 1)
        self.assertFalse(oke.load_state()["jobs"]["q"].get("auto"))

    def test_an_empty_commit_group_is_skipped(self):
        self.write(os.path.join(self.repo, "b.txt"), "b\n")
        self.git(self.repo, "add", "b.txt")
        self.git(self.repo, "commit", "-q", "-m", "b")
        self.write(os.path.join(self.repo, "a.txt"), "a\n")
        self.write(os.path.join(self.home, "jobs", "q", "plan.md"),
                   "job: q\n---\n## Commits\n- first: a.txt\n- second: b.txt\n")
        reviewed = oke.tree_of(self.repo, ["a.txt", "b.txt"])
        made, why = oke.commit_job("q", self.repo, ["a.txt", "b.txt"], reviewed)
        self.assertIsNone(why)
        self.assertEqual(len(made), 1)
        self.assertEqual(self.git_out(self.repo, "log", "-1", "--format=%s").strip(), "first")

    def test_the_base_run_never_writes_through_a_link(self):
        outside = self.path("outside")
        os.makedirs(outside)
        where = self.path("base")
        os.makedirs(where)
        link = os.path.join(where, "sub")
        if os.name == "nt":
            subprocess.check_call(["cmd", "/c", "mklink", "/J", link, outside], stdout=subprocess.DEVNULL)
        else:
            os.symlink(outside, link)
        copies = os.path.join(self.home, "jobs", "q", oke.DIRTY_COPIES)
        self.write(os.path.join(copies, "sub", "x.txt"), "the user's\n")
        self.write(os.path.join(copies, "top.txt"), "the user's\n")
        oke.write_json_atomic(oke.snapshot_path("q"), {"head": "x", "files": {"sub/x.txt": "m", "top.txt": "m"},
                                                       "copied": ["sub/x.txt", "top.txt"]})
        oke.overlay_user_files("q", oke.load_state()["jobs"]["q"], where)
        self.assertEqual(os.listdir(outside), [])
        self.assertTrue(os.path.isfile(os.path.join(where, "top.txt")))
        oke.drop_link(link)

    def test_a_big_file_is_marked_by_size_and_time(self):
        big, oke.BIG_FILE = oke.BIG_FILE, 10
        try:
            self.write(os.path.join(self.repo, "big.dat"), "x" * 20)
            self.assertTrue(oke.file_mark(self.repo, "big.dat").startswith("stat:20:"))
            self.assertEqual(len(oke.file_mark(self.repo, "notes-none") or ""), 1)  # gone: "-"
        finally:
            oke.BIG_FILE = big

    def test_a_git_repository_made_in_the_checkout_is_never_the_jobs(self):
        inner = os.path.join(self.repo, "vendor")
        os.makedirs(inner)
        self.git(inner, "init", "-q")
        self.write(os.path.join(inner, "x.txt"), "x\n")
        self.plan("true")
        self.assertEqual(self.cli("job", "tree", "q"), 1)
        self.assertIn("a git repository of its own", self.err)
        self.assertEqual(self.cli("check", "q"), 2)
        self.assertIn("vendor", self.err)


class TestRunTimed(unittest.TestCase):
    def test_a_process_left_holding_the_output_does_not_stall(self):
        # the child starts a grandchild that keeps its stdout open, then exits at once
        child = ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                 "print('child done')")
        start = time.time()
        r = oke.run_timed([sys.executable, "-c", child], 10, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertLess(time.time() - start, 15)
        self.assertEqual(r.returncode, 0)
        self.assertIn(b"child done", r.stdout)

    def test_a_command_over_its_time_is_stopped(self):
        start = time.time()
        r = oke.run_timed([sys.executable, "-c", "import time; print('a', flush=True); time.sleep(30)"], 2,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.assertLess(time.time() - start, 15)
        self.assertEqual(r.returncode, oke.TIMED_OUT)
        self.assertIn(b"OKe stopped it", r.stdout)

    def test_input_and_output(self):
        r = oke.run_timed([sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
                          input=b"abc", stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"ABC", b""))


class TestSnapshotBeforeLock(SpawnBase):
    """The user's files are read before the state lock when a job starts work in place."""

    def test_starting_work_takes_the_snapshot_and_leaves_no_temporary_copies(self):
        self.job("s", "awaiting_approval")
        self.write(os.path.join(self.repo, "notes.txt"), "the user's\n")
        self.assertEqual(self.cli("job", "phase", "s", "working"), 0, self.err)
        snap = oke.read_json(oke.snapshot_path("s"), None)
        self.assertIn("notes.txt", snap["files"])
        self.assertEqual(snap["copied"], ["notes.txt"])
        folder = os.path.join(self.home, "jobs", "s")
        self.assertEqual(sorted(n for n in os.listdir(folder) if n.startswith("dirty-before")),
                         ["dirty-before", "dirty-before.json"])


if __name__ == "__main__":
    unittest.main()
