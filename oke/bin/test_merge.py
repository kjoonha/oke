# -*- coding: utf-8 -*-
"""oke job merge: refuses to merge work the reviewer has not seen."""
import os
import subprocess
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

    def review(self, seq, verdict="pass", commit=None, report_seq=None):
        """The reviewer's assignment (naming the commit to review) and its report."""
        commit = commit or self.tip()
        path = os.path.join(self.jobs, "fix-a-rv", "assignment.md")
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("agent: fix-a-rv\nrole: reviewer\njob: fix-a\nseq: %d\ncommit: %s\n---\n" % (seq, commit))
        return self.report("fix-a-rv", seq if report_seq is None else report_seq, "verdict: " + verdict)

    def tip(self):
        return self.git_out(self.repo, "rev-parse", "oke/fix-a").strip()

    def order(self, *paths):
        # make the file times strictly increasing in the given order
        now = time.time()
        for i, p in enumerate(paths):
            os.utime(p, (now - 100 + i, now - 100 + i))

    def merged(self):
        return self.git_out(self.repo, "log", "--oneline", "-1").find("Merge") >= 0

    def git_out(self, repo, *args):
        return subprocess.check_output(["git", "-C", repo] + list(args)).decode()


class TestMergeGate(MergeBase):
    def test_a_reviewed_branch_is_merged(self):
        self.order(self.report("fix-a-w1", 1), self.review(1))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 0, self.err)
        self.assertTrue(self.merged())
        self.assertIn("oke/fix-a", self.git_out(self.repo, "log", "-1", "--format=%s"))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "a.txt")))

    def test_work_reported_after_the_review_is_refused(self):
        # seen live: the worker rebased (seq 2) and the PM merged without a new review
        self.order(self.review(1), self.report("fix-a-w1", 2))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("review", self.err)
        self.assertFalse(self.merged())

    def test_a_review_asking_for_changes_is_refused(self):
        self.order(self.report("fix-a-w1", 1), self.review(1, "changes"))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertFalse(self.merged())

    def test_no_review_is_refused(self):
        self.report("fix-a-w1", 1)
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertFalse(self.merged())

    def test_a_job_the_user_did_not_approve_is_refused(self):
        self.order(self.report("fix-a-w1", 1), self.review(1))
        self.cli("job", "phase", "fix-a", "awaiting_merge")
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertFalse(self.merged())

    def test_a_conflict_is_aborted_and_reported(self):
        self.write("toy/a.txt", "other\n")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "other")
        self.order(self.report("fix-a-w1", 1), self.review(1))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 2)
        self.assertIn("a.txt", self.out)
        self.assertEqual(self.git_out(self.repo, "status", "--short").strip(), "")


class TestReviewedCommit(MergeBase):
    def test_a_commit_added_after_the_review_is_refused(self):
        reviewed = self.tip()
        self.git(self.repo, "checkout", "-q", "oke/fix-a")
        self.write("toy/b.txt", "unreviewed\n")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "b")
        self.git(self.repo, "checkout", "-q", "-")
        self.order(self.report("fix-a-w1", 1), self.review(1, commit=reviewed))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("review covered", self.err)
        self.assertFalse(self.merged())

    def test_a_short_reviewed_sha_is_accepted(self):
        self.order(self.report("fix-a-w1", 1), self.review(1, commit=self.tip()[:10]))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 0, self.err)

    def test_a_review_assignment_without_commit_is_refused(self):
        path = os.path.join(self.jobs, "fix-a-rv", "assignment.md")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("agent: fix-a-rv\nrole: reviewer\njob: fix-a\nseq: 1\n---\n")
        self.order(self.report("fix-a-w1", 1), self.report("fix-a-rv", 1, "verdict: pass"))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("commit:", self.err)
        self.assertFalse(self.merged())

    def test_a_pass_for_an_older_review_assignment_is_refused(self):
        # the reviewer got seq 2 (the new commit) but its report still answers seq 1
        self.order(self.report("fix-a-w1", 1), self.review(2, report_seq=1))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("current assignment", self.err)
        self.assertFalse(self.merged())

    def test_a_missing_branch_is_refused(self):
        # the work sits on some other branch (the old `-2` retry): never merge an empty one
        self.order(self.report("fix-a-w1", 1), self.review(1))
        self.git(self.repo, "branch", "-q", "-m", "oke/fix-a", "oke/fix-a-2")
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("does not exist", self.err)


class TestReviewGuards(MergeBase):
    def test_a_branch_name_as_commit_is_refused(self):
        # `commit: oke/fix-a` would always equal the tip: no gate at all
        self.order(self.report("fix-a-w1", 1), self.review(1, commit="oke/fix-a"))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("commit sha", self.err)
        self.assertFalse(self.merged())

    def test_the_newest_reviewer_folder_decides(self):
        old = self.review(1)  # fix-a-rv passed the tip
        path = os.path.join(self.jobs, "fix-a-rv2", "assignment.md")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("agent: fix-a-rv2\nrole: reviewer\njob: fix-a\nseq: 1\ncommit: %s\n---\n" % self.tip())
        new = self.report("fix-a-rv2", 1, "verdict: changes")
        self.order(self.report("fix-a-w1", 1), old, new)
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertFalse(self.merged())


class TestMainCheckout(MergeBase):
    def test_a_detached_head_is_refused(self):
        import oke
        state = oke.load_state()
        state["jobs"]["fix-a"]["base"] = None  # job new ran on a detached HEAD
        oke.save_state(state)
        self.git(self.repo, "checkout", "-q", "--detach")
        self.order(self.report("fix-a-w1", 1), self.review(1))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("detached", self.err)
        self.assertFalse(self.merged())

    def test_job_new_records_the_base_branch(self):
        import oke
        current = self.git_out(self.repo, "symbolic-ref", "--short", "HEAD").strip()
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["base"], current)

    def test_an_untracked_file_counts_as_dirty(self):
        self.write("toy/new.txt", "untracked\n")
        self.order(self.report("fix-a-w1", 1), self.review(1))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("not clean", self.err)

    def test_a_merge_in_progress_is_left_alone(self):
        # the user's own merge, half resolved: OKe must neither merge nor abort it
        self.git(self.repo, "checkout", "-q", "-b", "side")
        self.write("toy/c.txt", "side\n")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "side c")
        self.git(self.repo, "checkout", "-q", "-")
        self.write("toy/c.txt", "main\n")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "main c")
        self.assertNotEqual(subprocess.call(["git", "-C", self.repo, "merge", "-q", "side"],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), 0)
        self.order(self.report("fix-a-w1", 1), self.review(1))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("already in progress", self.err)
        self.assertEqual(subprocess.call(["git", "-C", self.repo, "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                                         stdout=subprocess.DEVNULL), 0)

    def test_a_dirty_main_checkout_is_refused(self):
        self.write("toy/wip.txt", "user's work\n")
        self.order(self.report("fix-a-w1", 1), self.review(1))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("not clean", self.err)
        self.assertFalse(self.merged())

    def test_another_branch_checked_out_is_refused(self):
        self.git(self.repo, "checkout", "-q", "-b", "feature")
        self.order(self.report("fix-a-w1", 1), self.review(1))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("base branch", self.err)
        self.assertFalse(self.merged())

    def test_a_job_from_before_base_was_recorded_merges(self):
        import oke
        state = oke.load_state()
        del state["jobs"]["fix-a"]["base"]
        oke.save_state(state)
        self.git(self.repo, "checkout", "-q", "-b", "feature")
        self.order(self.report("fix-a-w1", 1), self.review(1))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 0, self.err)


if __name__ == "__main__":
    unittest.main()
