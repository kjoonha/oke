# -*- coding: utf-8 -*-
"""oke job merge: refuses to merge work the reviewer has not seen."""
import json
import os
import subprocess
import time
import unittest

import oke
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

    def test_a_branch_already_in_main_is_recorded_at_head_and_can_finish(self):
        # the change was already there, or a merge whose ledger row was lost
        self.git(self.repo, "merge", "-q", "--ff-only", "oke/fix-a")
        head = self.git_out(self.repo, "rev-parse", "HEAD").strip()
        self.order(self.report("fix-a-w1", 1), self.review(1))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 0, self.err)
        self.assertEqual(json.loads(self.out), {"result": "merged", "job": "fix-a", "commit": head,
                                                "already_in": True})
        self.assertEqual(self.git_out(self.repo, "rev-parse", "HEAD").strip(), head)  # no merge commit
        self.assertEqual(self.cli("job", "verify", "fix-a", "true"), 0, self.err)
        self.logged("fix-a")
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 0, self.err)

    def test_a_failed_check_of_a_branch_already_in_main_goes_back_to_the_worker(self):
        self.git(self.repo, "merge", "-q", "--ff-only", "oke/fix-a")
        head = self.git_out(self.repo, "rev-parse", "HEAD").strip()
        self.order(self.report("fix-a-w1", 1), self.review(1))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 0, self.err)
        # nothing to undo (OKe merged nothing), but the job is rolled back like any failed merge
        self.assertEqual(self.cli("job", "verify", "fix-a", "exit 1"), 3)
        self.assertEqual(json.loads(self.out)["already_in"], True)
        self.assertEqual(self.git_out(self.repo, "rev-parse", "HEAD").strip(), head)
        self.assertEqual(oke.merge_state("fix-a")[0], "rolled_back")
        # the worker fixes it on the branch, a new review passes: it merges for real
        self.git(self.repo, "checkout", "-q", "oke/fix-a")
        self.write("toy/fix.txt", "fix\n")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "fix")
        self.git(self.repo, "checkout", "-q", "-")
        self.order(self.report("fix-a-w1", 2), self.review(2))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 0, self.err)
        self.assertNotIn("already_in", json.loads(self.out))
        self.assertEqual(oke.merge_state("fix-a")[0], "unverified")

    def test_no_other_merge_while_a_failed_already_in_merge_sits_in_main(self):
        os.makedirs(os.path.join(self.home, "jobs", "fix-b"))
        oke.ledger_add("fix-b", "oke", "abc1234", "merge", "ok", "pre=abc1234 already-in")
        oke.ledger_add("fix-b", "oke", "abc1234", "verify", "fail", "exit 1: make test")
        oke.ledger_add("fix-b", "oke", "abc1234", "rollback", "ok", "already-in: nothing undone")
        state = oke.load_state()
        state["jobs"]["fix-b"] = dict(state["jobs"]["fix-a"], phase="working")
        oke.save_state(state)
        self.order(self.report("fix-a-w1", 1), self.review(1))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("main still holds fix-b's code", self.err)
        self.assertFalse(self.merged())
        # a hold would keep the block with nobody on it; cancel and archive lift it
        self.assertEqual(self.cli("job", "hold", "fix-b"), 1)
        self.assertIn("blocks the merges", self.err)
        self.assertEqual(self.cli("job", "archive", "fix-b"), 0, self.err)
        self.assertEqual(self.cli("job", "merge", "fix-a"), 0, self.err)

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

    def test_python_caches_left_by_a_verify_do_not_block(self):
        self.write("toy/__pycache__/calc.cpython-311.pyc", "x")
        self.write("toy/.pytest_cache/v", "x")
        self.order(self.report("fix-a-w1", 1), self.review(1))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 0, self.err)
        self.assertTrue(self.merged())

    def test_another_tools_state_folder_does_not_block(self):
        self.write("toy/.omc/state.json", "{}")
        self.order(self.report("fix-a-w1", 1), self.review(1))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 0, self.err)
        self.assertTrue(self.merged())

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


class TestLedgerAndVerify(MergeBase):
    def merge(self):
        self.order(self.report("fix-a-w1", 1), self.review(1))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 0, self.err)
        return self.git_out(self.repo, "rev-parse", "HEAD").strip()

    def ledger(self):
        self.assertEqual(self.cli("ledger", "show", "fix-a"), 0)
        return json.loads(self.out)

    def test_a_merge_is_recorded_and_waits_for_verification(self):
        merged = self.merge()
        led = self.ledger()
        self.assertEqual(led["merge"], "unverified")
        self.assertEqual([(r["check"], r["result"]) for r in led["rows"]], [("review", "pass"), ("merge", "ok")])
        self.assertEqual(led["rows"][0]["commit"], self.tip())
        self.assertEqual(led["rows"][1]["commit"], merged)

    def test_a_passing_check_verifies_the_merge(self):
        merged = self.merge()
        self.assertEqual(self.cli("job", "verify", "fix-a", "test -f a.txt && echo checked"), 0, self.err)
        self.assertEqual(json.loads(self.out)["result"], "verified")
        self.assertEqual(self.ledger()["merge"], "verified")
        self.assertEqual(self.git_out(self.repo, "rev-parse", "HEAD").strip(), merged)
        self.assertEqual(self.cli("job", "verify", "fix-a", "exit 1"), 0)  # verified stays verified
        self.assertTrue(json.loads(self.out)["already"])

    def test_a_failing_check_rolls_the_merge_back(self):
        pre = self.git_out(self.repo, "rev-parse", "HEAD").strip()
        self.merge()
        # untracked byproducts of the check stay; the merge is undone
        self.assertEqual(self.cli("job", "verify", "fix-a", "echo log > out.log; echo boom; exit 4"), 3)
        out = json.loads(self.out)
        self.assertEqual((out["result"], out["exit"], out["rolled_back_to"]), ("verify_failed", 4, pre))
        self.assertIn("boom", out["tail"])
        self.assertEqual(self.git_out(self.repo, "rev-parse", "HEAD").strip(), pre)
        self.assertEqual(self.git_out(self.repo, "status", "--porcelain").strip(), "?? out.log")
        os.remove(os.path.join(self.repo, "out.log"))
        self.assertEqual(self.ledger()["merge"], "rolled_back")
        # fixed and reviewed again: the job may merge once more
        self.order(self.report("fix-a-w1", 2), self.review(2))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 0, self.err)

    def test_done_needs_a_verified_merge(self):
        self.merge()
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 1)
        self.assertIn("unverified", self.err)
        self.assertEqual(self.cli("job", "verify", "fix-a", "true"), 0, self.err)
        self.logged("fix-a")
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 0, self.err)

    def test_a_second_merge_of_the_same_job_is_refused(self):
        self.merge()
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("already merged", self.err)

    def test_another_merge_waits_for_the_verification(self):
        self.merge()
        self.job("fix-b", "merging")
        self.assertEqual(self.cli("job", "merge", "fix-b"), 1)
        self.assertIn("not verified", self.err)

    def test_verify_without_a_merge_or_after_the_main_moved(self):
        self.assertEqual(self.cli("job", "verify", "fix-a", "true"), 1)
        self.merge()
        self.write("toy/z.txt", "user\n")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "user commit")
        self.assertEqual(self.cli("job", "verify", "fix-a", "true"), 1)
        self.assertIn("moved past", self.err)

    def test_a_worker_records_its_check(self):
        self.assertEqual(self.cli("ledger", "add", "fix-a", "--by", "fix-a-w1", "--commit", "abc1234",
                                  "--check", "worker-verify", "--result", "pass", "--detail", "pytest -q\tall"), 0)
        row = self.ledger()["rows"][0]
        self.assertEqual((row["by"], row["check"], row["result"], row["detail"]),
                         ("fix-a-w1", "worker-verify", "pass", "pytest -q all"))
        self.assertEqual(self.cli("ledger", "add", "nope", "--by", "x", "--commit", "x",
                                  "--check", "worker-verify", "--result", "pass"), 1)

    def test_nobody_forges_an_oke_row(self):
        self.merge()
        for check in ("verify", "merge", "rollback", "review"):
            self.assertEqual(self.cli("ledger", "add", "fix-a", "--by", "w", "--commit", "x",
                                      "--check", check, "--result", "pass"), 1)
        self.assertEqual(self.ledger()["merge"], "unverified")

    def test_others_work_on_main_is_never_reset(self):
        self.merge()
        # during the check somebody commits on main (a job without a worktree, the user)
        cmd = "echo z > z.txt && git add z.txt && git -c user.name=t -c user.email=t@t commit -qm other; exit 1"
        self.assertEqual(self.cli("job", "verify", "fix-a", cmd), 1)
        self.assertIn("NOT undone", self.err)
        self.assertIn("other", self.git_out(self.repo, "log", "-1", "--format=%s"))
        self.assertEqual(self.ledger()["merge"], "unverified")

    def test_local_edits_in_merged_files_are_kept(self):
        self.merge()
        self.assertEqual(self.cli("job", "verify", "fix-a", "echo users edit > a.txt; exit 1"), 1)
        self.assertIn("NOT undone", self.err)
        with open(os.path.join(self.repo, "a.txt")) as f:
            self.assertEqual(f.read().strip(), "users edit")

    def test_a_command_that_cannot_run_is_no_verdict(self):
        self.merge()
        self.assertEqual(self.cli("job", "verify", "fix-a", "no-such-command-oke-xyz"), 1)
        self.assertIn("could not run", self.err)
        led = self.ledger()
        self.assertEqual(led["merge"], "unverified")
        self.assertNotIn("verify", [r["check"] for r in led["rows"]])

    def test_the_user_may_accept_a_merge(self):
        self.merge()
        self.assertEqual(self.cli("job", "verify", "fix-a"), 1)  # neither a command nor --accept
        self.assertEqual(self.cli("job", "verify", "fix-a", "--accept", "main moved, checked by hand"), 0)
        self.assertEqual(self.ledger()["merge"], "verified")
        self.assertEqual(self.ledger()["rows"][-1]["by"], "user")

    def test_a_torn_last_line_does_not_swallow_the_next_row(self):
        self.merge()
        with open(oke.ledger_path("fix-a"), "a") as f:
            f.write("2026-01-01T00:00:00\tw\tabc")  # a writer died here
        self.assertEqual(self.cli("job", "verify", "fix-a", "true"), 0, self.err)
        self.assertEqual(self.ledger()["merge"], "verified")

    def test_a_failed_job_with_an_unverified_merge_still_holds_the_next(self):
        self.merge()
        self.cli("job", "phase", "fix-a", "failed")
        self.job("fix-b", "merging")
        self.assertEqual(self.cli("job", "merge", "fix-b"), 1)
        self.assertIn("not verified", self.err)



class TestDirectCommit(MergeBase):
    """A job without a worktree: its reviewed changes are uncommitted in the main checkout."""
    def setUp(self):
        MergeBase.setUp(self)
        self.git(self.repo, "branch", "-q", "-D", "oke/fix-a")
        self.write("toy/d.txt", "d\n")
        self.git(self.repo, "add", "d.txt")  # the PM stages the job's new files
        path = os.path.join(self.jobs, "fix-a-rv", "assignment.md")
        os.makedirs(os.path.dirname(path))
        self.assertEqual(self.cli("job", "tree", "fix-a"), 0, self.err)
        self.tree = json.loads(self.out)["tree"]
        with open(path, "w") as f:  # it reviewed `git diff HEAD`: the tree, not a commit
            f.write("agent: fix-a-rv\nrole: reviewer\njob: fix-a\nseq: 1\ncommit: no\ntree: %s\n---\n" % self.tree)
        self.order(self.report("fix-a-w1", 1), self.report("fix-a-rv", 1, "verdict: pass"))
        self.pre = self.git_out(self.repo, "rev-parse", "HEAD").strip()

    def ledger(self):
        self.assertEqual(self.cli("ledger", "show", "fix-a"), 0)
        return json.loads(self.out)

    def test_merge_commits_the_changes_and_waits_for_verification(self):
        self.assertEqual(self.cli("job", "merge", "fix-a"), 0, self.err)
        self.assertTrue(json.loads(self.out)["direct"])
        self.assertEqual(self.git_out(self.repo, "status", "--porcelain").strip(), "")
        self.assertEqual(self.ledger()["merge"], "unverified")
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 1)
        self.assertEqual(self.cli("job", "verify", "fix-a", "test -f d.txt"), 0, self.err)
        self.logged("fix-a")
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 0, self.err)

    def test_unstaged_byproducts_are_not_committed(self):
        self.write("toy/__pycache__/d.cpython-311.pyc", "x")  # left by the checks after the review
        self.assertEqual(self.cli("job", "merge", "fix-a"), 0, self.err)
        self.assertEqual(self.git_out(self.repo, "status", "--porcelain").strip(), "?? __pycache__/")
        self.assertNotIn("__pycache__", self.git_out(self.repo, "show", "--name-only", "HEAD"))
        self.assertEqual(json.loads(self.out)["left_untracked"], ["__pycache__/d.cpython-311.pyc"])

    def test_an_unstaged_new_file_is_left_out_of_the_review_tree(self):
        self.write("toy/notes.txt", "the user's own\n")  # not staged: not the job's
        self.assertEqual(self.cli("job", "tree", "fix-a"), 0, self.err)
        self.assertEqual(json.loads(self.out)["tree"], self.tree)

    def test_a_tree_git_cannot_read_is_an_error_not_a_traceback(self):
        self.assertIsNone(oke.pending_tree(os.path.join(self.home, "no-such-repo")))

    def test_a_failed_check_gives_the_changes_back_uncommitted(self):
        self.assertEqual(self.cli("job", "merge", "fix-a"), 0, self.err)
        self.assertEqual(self.cli("job", "verify", "fix-a", "exit 1"), 3)
        self.assertTrue(json.loads(self.out)["uncommitted"])
        self.assertEqual(self.git_out(self.repo, "rev-parse", "HEAD").strip(), self.pre)
        self.assertEqual(self.git_out(self.repo, "status", "--porcelain").strip(), "?? d.txt")
        self.assertEqual(self.ledger()["merge"], "rolled_back")

    def test_a_change_after_the_review_is_refused(self):
        self.write("toy/d.txt", "changed after the review\n")
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("not the ones the review covered", self.err)
        self.assertEqual(self.git_out(self.repo, "rev-parse", "HEAD").strip(), self.pre)
        # only what the PM staged, as it was: the refused merge staged nothing of its own
        self.assertEqual(self.git_out(self.repo, "diff", "--cached", "--name-only").strip(), "d.txt")
        self.assertEqual(self.git_out(self.repo, "show", ":d.txt"), "d\n")

    def test_the_commit_holds_exactly_the_reviewed_tree(self):
        self.assertEqual(self.cli("job", "merge", "fix-a"), 0, self.err)
        self.assertEqual(self.git_out(self.repo, "rev-parse", "HEAD^{tree}").strip(), self.tree)

    def test_a_review_of_main_as_it_is_is_recorded_already_in_and_can_finish(self):
        # the change was already there: the review passed on a clean checkout
        self.git(self.repo, "reset", "-q")
        os.remove(os.path.join(self.repo, "d.txt"))
        self.write("toy/__pycache__/x.pyc", "x")  # untracked: not the job's
        tree = self.git_out(self.repo, "rev-parse", "HEAD^{tree}").strip()
        path = os.path.join(self.jobs, "fix-a-rv", "assignment.md")
        with open(path, "w") as f:
            f.write("agent: fix-a-rv\nrole: reviewer\njob: fix-a\nseq: 1\ncommit: no\ntree: %s\n---\n" % tree)
        self.order(self.report("fix-a-w1", 1), self.report("fix-a-rv", 1, "verdict: pass"))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 0, self.err)
        self.assertEqual(json.loads(self.out)["already_in"], True)
        self.assertEqual(self.git_out(self.repo, "rev-parse", "HEAD").strip(), self.pre)  # no commit
        self.assertEqual(self.cli("job", "verify", "fix-a", "true"), 0, self.err)
        self.logged("fix-a")
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 0, self.err)

    def test_an_unstaged_new_file_is_not_taken_for_already_in(self):
        self.git(self.repo, "reset", "-q")  # d.txt stays, untracked: the review tree is HEAD's
        tree = self.git_out(self.repo, "rev-parse", "HEAD^{tree}").strip()
        path = os.path.join(self.jobs, "fix-a-rv", "assignment.md")
        with open(path, "w") as f:
            f.write("agent: fix-a-rv\nrole: reviewer\njob: fix-a\nseq: 1\ncommit: no\ntree: %s\n---\n" % tree)
        self.order(self.report("fix-a-w1", 1), self.report("fix-a-rv", 1, "verdict: pass"))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("untracked files d.txt", self.err)
        self.assertEqual(self.ledger()["merge"], "none")

    def test_nothing_to_commit_is_refused(self):
        self.git(self.repo, "reset", "-q")
        os.remove(os.path.join(self.repo, "d.txt"))
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("no changes", self.err)

    def test_a_reviewed_commit_without_its_branch_is_still_refused(self):
        self.review(1, commit=self.pre)
        self.assertEqual(self.cli("job", "merge", "fix-a"), 1)
        self.assertIn("does not exist", self.err)


if __name__ == "__main__":
    unittest.main()
