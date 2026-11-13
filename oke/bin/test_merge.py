# -*- coding: utf-8 -*-
"""oke job merge: refuses to merge work the reviewer has not seen."""
import os
import time
import unittest

from test_oke import SpawnBase


class MergeBase(SpawnBase):
    def setUp(self):
        SpawnBase.setUp(self)
        self.job("fix-a", "merging")
        # the job's branch with one commit
        self.git(self.repo, "checkout", "-q", "-b", "oke/fix-a")
        self.write("toy/a.txt", "a\n")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "a")
        self.git(self.repo, "checkout", "-q", "-")
        self.jobs = os.path.join(self.home, "jobs", "fix-a")

    def report(self, folder, seq, body=""):
        path = os.path.join(self.jobs, folder, "report.md")
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("type: done\nseq: %d\n---\n%s\n" % (seq, body))
        return path

    def order(self, *paths):
        # make the file times strictly increasing in the given order
        now = time.time()
        for i, p in enumerate(paths):
            os.utime(p, (now - 100 + i, now - 100 + i))

    def merged(self):
        return self.git_out(self.repo, "log", "--oneline", "-1").find("Merge") >= 0

    def git_out(self, repo, *args):
        import subprocess
        return subprocess.check_output(["git", "-C", repo] + list(args)).decode()


class TestMergeGate(MergeBase):
    def test_a_reviewed_branch_is_merged(self):
        self.order(self.report("fix-a-w1", 1), self.report("fix-a-rv", 1, "verdict: pass"))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 0, self.err)
        self.assertTrue(self.merged())
        self.assertTrue(os.path.exists(os.path.join(self.repo, "a.txt")))

    def test_work_reported_after_the_review_is_refused(self):
        # seen live: the worker rebased (seq 2) and the PM merged without a new review
        self.order(self.report("fix-a-rv", 1, "verdict: pass"), self.report("fix-a-w1", 2))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("review", self.err)
        self.assertFalse(self.merged())

    def test_a_review_asking_for_changes_is_refused(self):
        self.order(self.report("fix-a-w1", 1), self.report("fix-a-rv", 1, "verdict: changes"))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertFalse(self.merged())

    def test_no_review_is_refused(self):
        self.report("fix-a-w1", 1)
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertFalse(self.merged())

    def test_a_job_the_user_did_not_approve_is_refused(self):
        self.order(self.report("fix-a-w1", 1), self.report("fix-a-rv", 1, "verdict: pass"))
        self.cli("job", "phase", "fix-a", "awaiting_merge")
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertFalse(self.merged())

    def test_a_conflict_is_aborted_and_reported(self):
        self.write("toy/a.txt", "other\n")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "other")
        self.order(self.report("fix-a-w1", 1), self.report("fix-a-rv", 1, "verdict: pass"))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 2)
        self.assertIn("a.txt", self.out)
        self.assertEqual(self.git_out(self.repo, "status", "--short").strip(), "")


if __name__ == "__main__":
    unittest.main()
