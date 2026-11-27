# -*- coding: utf-8 -*-
"""oke job deliver: hands over only work the reviewer has seen, on the latest base branch."""
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest

import oke
from test_oke import SpawnBase


class DeliverBase(SpawnBase):
    def setUp(self):
        SpawnBase.setUp(self)
        self.job("fix-a", "reviewing")
        self.base = oke.load_state()["jobs"]["fix-a"]["base"]
        # the job's branch with one commit
        self.git(self.repo, "checkout", "-q", "-b", "oke/fix-a")
        self.write("toy/a.txt", "a\n")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "a")
        self.git(self.repo, "checkout", "-q", "-")
        self.jobs = os.path.join(self.home, "jobs", "fix-a")
        self.set_plan("test -f a.txt")

    def set_plan(self, command, criteria="1. a.txt exists", job="fix-a", workspace=""):
        with io.open(os.path.join(self.home, "jobs", job, "plan.md"), "w", encoding="utf-8") as f:
            f.write(u"job: %s\n---\n%s## Acceptance criteria\n%s\n## Check command\n`%s` passes\n"
                    % (job, workspace, criteria, command))

    def checked(self, target, result="pass", job="fix-a"):
        """The row `oke check` writes for a run on `target` under the current plan."""
        oke.ledger_add(job, "oke", target, "check", result, oke.criteria_rev(job) + " new=0 resolved=0 log=-")

    def report(self, folder, seq, body="", job="fix-a"):
        path = os.path.join(self.home, "jobs", job, folder, "report.md")
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("type: done\nseq: %d\n---\n%s\n" % (seq, body))
        return path

    def review(self, seq, verdict="pass", commit=None, report_seq=None, checked=True, job="fix-a"):
        """The reviewer's assignment (naming the commit to review) and its report, and
        a passing `oke check` on that commit unless `checked` is False."""
        commit = commit or self.tip()
        path = os.path.join(self.home, "jobs", job, job + "-rv", "assignment.md")
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("agent: %s-rv\nrole: reviewer\njob: %s\nseq: %d\ncommit: %s\ncriteria_rev: %s\n---\n"
                    % (job, job, seq, commit, oke.criteria_rev(job)))
        if checked:
            code = subprocess.call(["git", "-C", self.repo, "rev-parse", "-q", "--verify", commit + "^{commit}"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if code == 0:
                self.checked(self.git_out(self.repo, "rev-parse", commit + "^{commit}").strip(), job=job)
        return self.report(job + "-rv", seq if report_seq is None else report_seq, "verdict: " + verdict, job=job)

    def reviewed(self, seq=1, job="fix-a"):
        """Worker reported, then the review passed."""
        self.order(self.report(job + "-w1", seq, job=job), self.review(seq, job=job))

    def tip(self, branch="oke/fix-a"):
        return self.git_out(self.repo, "rev-parse", branch).strip()

    def head(self):
        return self.git_out(self.repo, "rev-parse", "HEAD").strip()

    def order(self, *paths):
        # make the file times strictly increasing in the given order
        now = time.time()
        for i, p in enumerate(paths):
            os.utime(p, (now - 100 + i, now - 100 + i))

    def git_out(self, repo, *args):
        return subprocess.check_output(["git", "-C", repo] + list(args)).decode()

    def worktree(self):
        """Check oke/fix-a out in its own worktree, as `oke worktree fix-a` does."""
        p = self.path("wt-fix-a")
        self.git(self.repo, "worktree", "add", "-q", p, "oke/fix-a")
        st = oke.load_state()
        st["jobs"]["fix-a"]["worktree"] = {"id": p, "path": p}
        oke.save_state(st)
        return p

    def commit_main(self, name, text):
        self.write("toy/" + name, text)
        self.git(self.repo, "add", name)
        self.git(self.repo, "commit", "-q", "-m", "main " + name)

    def commit_in(self, cwd, name, text, msg="w"):
        with io.open(os.path.join(cwd, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        self.git(cwd, "add", name)
        self.git(cwd, "commit", "-q", "-m", msg)

    def deliver(self, job="fix-a"):
        return self.cli("job", "deliver", job)

    def ledger(self, job="fix-a"):
        self.assertEqual(self.cli("ledger", "show", job), 0)
        return json.loads(self.out)


class TestDeliverGate(DeliverBase):
    def test_a_branch_on_the_latest_base_is_delivered_without_a_rebase(self):
        main_head, tip = self.head(), self.tip()
        self.reviewed()
        self.assertEqual(self.deliver(), 0, self.err)
        out = json.loads(self.out)
        self.assertEqual((out["result"], out["branch"], out["base"], out["commit"], out["merge"]),
                         ("delivered", "oke/fix-a", self.base, tip, "git merge oke/fix-a"))
        self.assertEqual(self.head(), main_head)  # the main branch is never touched
        self.assertEqual(self.tip(), tip)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")))
        rows = [(r["check"], r["result"], r["commit"]) for r in self.ledger()["rows"] if r["check"] != "check"]
        self.assertEqual(rows, [("review", "pass", tip), ("deliver", "ok", tip)])
        self.assertEqual(self.ledger()["deliver"], "delivered")

    def test_a_second_deliver_of_the_same_tip_adds_no_row(self):
        self.reviewed()
        self.assertEqual(self.deliver(), 0, self.err)
        self.assertEqual(self.deliver(), 0, self.err)
        self.assertEqual(len([r for r in self.ledger()["rows"] if r["check"] == "deliver"]), 1)

    def test_a_dirty_main_checkout_or_another_branch_there_is_left_alone(self):
        self.write("toy/wip.txt", "user's work\n")
        self.git(self.repo, "checkout", "-q", "-b", "feature")
        self.reviewed()
        self.assertEqual(self.deliver(), 0, self.err)
        self.assertEqual(self.git_out(self.repo, "status", "--porcelain").strip(), "?? wip.txt")
        self.assertEqual(self.git_out(self.repo, "symbolic-ref", "--short", "HEAD").strip(), "feature")

    def test_work_reported_after_the_review_is_refused(self):
        # seen live: the worker rebased (seq 2) and the PM went on without a new review
        self.order(self.review(1), self.report("fix-a-w1", 2))
        self.assertEqual(self.deliver(), 1)
        self.assertIn("review", self.err)
        self.assertEqual(self.ledger()["deliver"], "none")

    def test_a_review_asking_for_changes_is_refused(self):
        self.order(self.report("fix-a-w1", 1), self.review(1, "changes"))
        self.assertEqual(self.deliver(), 1)

    def test_no_review_is_refused(self):
        self.report("fix-a-w1", 1)
        self.assertEqual(self.deliver(), 1)

    def test_a_job_not_at_work_is_refused(self):
        self.reviewed()
        self.assertEqual(self.cli("job", "phase", "fix-a", "planning"), 0, self.err)
        self.assertEqual(self.deliver(), 1)
        self.assertIn("phase planning", self.err)

    def test_a_held_job_is_refused(self):
        self.reviewed()
        st = oke.load_state()
        st["jobs"]["fix-a"]["held"] = True
        oke.save_state(st)
        self.assertEqual(self.deliver(), 1)
        self.assertIn("on hold", self.err)

    def test_a_job_whose_output_is_not_code_is_refused(self):
        st = oke.load_state()
        st["jobs"]["fix-a"]["output"] = "result"
        oke.save_state(st)
        self.reviewed()
        self.assertEqual(self.deliver(), 1)
        self.assertIn("not code", self.err)

    def test_a_job_without_a_base_branch_is_refused(self):
        st = oke.load_state()
        st["jobs"]["fix-a"]["base"] = None  # job new ran on a detached HEAD
        oke.save_state(st)
        self.reviewed()
        self.assertEqual(self.deliver(), 1)
        self.assertIn("detached", self.err)
        self.assertIn("job base fix-a", self.err)
        # the user names the branch: then it delivers
        self.assertEqual(self.cli("job", "base", "fix-a", "nope"), 1)
        self.assertIn("no local branch", self.err)
        self.assertEqual(self.cli("job", "base", "fix-a", "oke/fix-a"), 1)  # never an oke/ branch
        self.assertEqual(self.cli("job", "base", "fix-a", self.base), 0, self.err)
        self.assertEqual(self.deliver(), 0, self.err)
        self.assertEqual(json.loads(self.out)["base"], self.base)
        # once delivered, the base stays
        self.git(self.repo, "branch", "-q", "other")
        self.assertEqual(self.cli("job", "base", "fix-a", "other"), 1)
        self.assertIn("delivered", self.err)

    def test_a_branch_the_base_already_holds_is_recorded_without_a_rewrite(self):
        wt = self.worktree()
        self.reviewed()
        tip = self.tip()
        self.git(self.repo, "merge", "-q", "--no-ff", "-m", "merged by hand", "oke/fix-a")
        self.commit_main("b.txt", "b\n")  # the base moved on past it
        self.assertEqual(self.deliver(), 0, self.err)
        out = json.loads(self.out)
        self.assertEqual((out["commit"], out.get("already_in")), (tip, True))
        self.assertEqual(self.tip(), tip)  # not rebased
        self.assertEqual(self.git_out(wt, "rev-parse", "HEAD").strip(), tip)
        row = self.ledger()["rows"][-1]
        self.assertEqual((row["check"], row["commit"]), ("deliver", tip))
        self.assertIn("already-in", row["detail"])

    def test_job_new_records_the_base_branch(self):
        current = self.git_out(self.repo, "symbolic-ref", "--short", "HEAD").strip()
        self.assertEqual(self.base, current)


class TestQualityGate(DeliverBase):
    def test_refused_without_check_row(self):
        self.order(self.report("fix-a-w1", 1), self.review(1, checked=False))
        self.assertEqual(self.deliver(), 1)
        self.assertIn("oke check", self.err)

    def test_refused_when_plan_changed_after_review(self):
        self.reviewed()
        self.set_plan("test -f a.txt", "1. a.txt exists\n2. and more")
        self.assertEqual(self.deliver(), 1)
        self.assertIn("criteria_rev", self.err)

    def test_refused_when_last_check_failed(self):
        self.reviewed()
        self.checked(self.tip(), "fail")
        self.assertEqual(self.deliver(), 1)
        self.assertIn("oke check", self.err)

    def test_refused_when_check_ran_on_another_commit(self):
        self.order(self.report("fix-a-w1", 1), self.review(1, checked=False))
        self.checked(self.head())
        self.assertEqual(self.deliver(), 1)


class TestReviewedCommit(DeliverBase):
    def test_a_commit_added_after_the_review_is_refused(self):
        reviewed = self.tip()
        self.git(self.repo, "checkout", "-q", "oke/fix-a")
        self.write("toy/b.txt", "unreviewed\n")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "b")
        self.git(self.repo, "checkout", "-q", "-")
        self.order(self.report("fix-a-w1", 1), self.review(1, commit=reviewed))
        self.assertEqual(self.deliver(), 1)
        self.assertIn("review covered", self.err)

    def test_a_short_reviewed_sha_is_accepted(self):
        self.order(self.report("fix-a-w1", 1), self.review(1, commit=self.tip()[:10]))
        self.assertEqual(self.deliver(), 0, self.err)

    def test_a_review_assignment_without_commit_is_refused(self):
        path = os.path.join(self.jobs, "fix-a-rv", "assignment.md")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("agent: fix-a-rv\nrole: reviewer\njob: fix-a\nseq: 1\n---\n")
        self.order(self.report("fix-a-w1", 1), self.report("fix-a-rv", 1, "verdict: pass"))
        self.assertEqual(self.deliver(), 1)
        self.assertIn("commit:", self.err)

    def test_a_pass_for_an_older_review_assignment_is_refused(self):
        self.order(self.report("fix-a-w1", 1), self.review(2, report_seq=1))
        self.assertEqual(self.deliver(), 1)
        self.assertIn("current assignment", self.err)

    def test_a_missing_branch_is_refused(self):
        self.reviewed()
        self.git(self.repo, "branch", "-q", "-m", "oke/fix-a", "oke/fix-a-2")
        self.assertEqual(self.deliver(), 1)
        self.assertIn("does not exist", self.err)

    def test_a_branch_name_as_commit_is_refused(self):
        self.order(self.report("fix-a-w1", 1), self.review(1, commit="oke/fix-a"))
        self.assertEqual(self.deliver(), 1)
        self.assertIn("commit sha", self.err)

    def test_the_newest_reviewer_folder_decides(self):
        old = self.review(1)
        path = os.path.join(self.jobs, "fix-a-rv2", "assignment.md")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("agent: fix-a-rv2\nrole: reviewer\njob: fix-a\nseq: 1\ncommit: %s\n---\n" % self.tip())
        new = self.report("fix-a-rv2", 1, "verdict: changes")
        self.order(self.report("fix-a-w1", 1), old, new)
        self.assertEqual(self.deliver(), 1)


class TestRebase(DeliverBase):
    def test_a_clean_rebase_with_the_same_change_asks_for_a_check_then_delivers(self):
        wt = self.worktree()
        old = self.tip()
        self.reviewed()
        self.commit_main("b.txt", "b\n")  # the base branch moved on, elsewhere
        base_head = self.head()
        self.assertEqual(self.deliver(), 4, self.err)
        out = json.loads(self.out)
        new = self.tip()
        self.assertEqual((out["result"], out["from"], out["commit"]), ("rebased", old, new))
        self.assertIn("check fix-a", out["next"])
        self.assertEqual(self.git_out(wt, "rev-parse", "HEAD").strip(), new)
        self.assertEqual(self.git_out(self.repo, "merge-base", base_head, new).strip(), base_head)
        self.assertEqual(self.head(), base_head)  # the base branch itself is untouched
        carried = [r for r in self.ledger()["rows"] if r["check"] == "review"][-1]
        self.assertEqual((carried["commit"], carried["result"]), (new, "pass"))
        self.assertIn("carried from " + old, carried["detail"])
        # the check has not run on the new tip yet
        self.assertEqual(self.deliver(), 1)
        self.assertIn("oke check", self.err)
        self.checked(new)
        self.assertEqual(self.deliver(), 0, self.err)
        self.assertEqual(json.loads(self.out)["commit"], new)
        self.assertEqual(self.ledger()["rows"][-1]["check"], "deliver")

    def test_a_conflict_is_undone_and_reported(self):
        wt = self.worktree()
        tip = self.tip()
        self.reviewed()
        self.commit_main("a.txt", "other\n")
        self.assertEqual(self.deliver(), 2)
        self.assertEqual(json.loads(self.out)["result"], "conflict")
        self.assertEqual(json.loads(self.out)["files"], ["a.txt"])
        self.assertEqual(self.tip(), tip)  # the branch is as it was
        self.assertEqual(self.git_out(wt, "status", "--porcelain").strip(), "")
        self.assertEqual(self.git_out(wt, "symbolic-ref", "--short", "HEAD").strip(), "oke/fix-a")
        self.assertEqual(self.ledger()["deliver"], "none")

    def test_another_change_after_the_rebase_needs_a_new_review(self):
        wt = self.worktree()
        lines = "".join("line %d\n" % i for i in range(1, 11))
        self.commit_main("f.txt", lines)
        self.git(wt, "rebase", "-q", self.base)  # the worker's branch has f.txt too
        self.commit_in(wt, "f.txt", lines.replace("line 5\n", "line five\n"))
        self.reviewed()
        self.commit_main("f.txt", lines.replace("line 3\n", "line three\n"))  # near, but no conflict
        self.assertEqual(self.deliver(), 1)
        self.assertIn("patch id", self.err)
        self.assertIn("commit: " + self.tip(), self.err)
        self.assertEqual(self.ledger()["deliver"], "none")
        self.assertEqual(self.git_out(self.repo, "merge-base", "--is-ancestor", self.base, "oke/fix-a"), "")

    def test_uncommitted_changes_in_the_worktree_are_refused(self):
        wt = self.worktree()
        self.reviewed()
        self.commit_main("b.txt", "b\n")
        with open(os.path.join(wt, "a.txt"), "w") as f:
            f.write("edit\n")
        tip = self.tip()
        self.assertEqual(self.deliver(), 1)
        self.assertIn("uncommitted", self.err)
        self.assertEqual(self.tip(), tip)

    def test_a_rebase_in_progress_is_named_as_such(self):
        wt = self.worktree()
        self.reviewed()
        self.commit_main("b.txt", "b\n")
        self.commit_in(wt, "a.txt", "mine\n")
        self.commit_main("a.txt", "theirs\n")
        subprocess.call(["git", "-C", wt, "rebase", "-q", self.base], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)  # stops at the conflict, HEAD detached
        try:
            reviewed = self.tip()
            self.review(2, commit=reviewed)
            self.assertEqual(self.deliver(), 1)
            self.assertIn("in progress", self.err)
        finally:
            subprocess.call(["git", "-C", wt, "rebase", "--abort"])

    def test_a_branch_behind_without_a_worktree_is_refused(self):
        self.reviewed()
        self.commit_main("b.txt", "b\n")
        self.assertEqual(self.deliver(), 1)
        self.assertIn("no worktree", self.err)


class TestDone(DeliverBase):
    def test_done_needs_a_delivery(self):
        self.logged("fix-a")
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 1)
        self.assertIn("not delivered", self.err)
        self.reviewed()
        self.assertEqual(self.deliver(), 0, self.err)
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 0, self.err)

    def test_done_refused_when_the_branch_moved_after_its_delivery(self):
        self.reviewed()
        self.assertEqual(self.deliver(), 0, self.err)
        self.git(self.repo, "checkout", "-q", "oke/fix-a")
        self.write("toy/late.txt", "late\n")
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "-q", "-m", "late")
        self.git(self.repo, "checkout", "-q", "-")
        self.logged("fix-a")
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 1)
        self.assertIn("moved", self.err)

    def test_done_is_fine_when_the_user_merged_and_deleted_the_branch(self):
        self.reviewed()
        self.assertEqual(self.deliver(), 0, self.err)
        self.git(self.repo, "merge", "-q", "--ff-only", "oke/fix-a")
        self.git(self.repo, "branch", "-q", "-d", "oke/fix-a")
        self.logged("fix-a")
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 0, self.err)

    def test_an_old_state_with_merge_phases_reads_as_reviewing(self):
        st = oke.load_state()
        st["jobs"]["fix-a"]["phase"] = "awaiting_merge"
        oke.write_json_atomic(oke.state_path(), st)
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["phase"], "reviewing")


class TestCancelledKept(DeliverBase):
    def test_a_cancelled_jobs_worktree_is_not_for_merging(self):
        os.environ["OKE_RUNTIME"] = "bg"
        self.worktree()
        self.assertEqual(self.cli("job", "cancel", "fix-a"), 0, self.err)
        self.assertEqual(self.cli("job", "archive", "fix-a"), 0, self.err)
        self.assertIn("kept_worktree", self.out)
        self.assertNotIn("for the user to merge", self.out)
        self.assertEqual([k["done"] for k in oke.kept_worktrees()], [False])


class TestClean(DeliverBase):
    def setUp(self):
        DeliverBase.setUp(self)
        os.environ["OKE_RUNTIME"] = "bg"
        self.wt = self.worktree()
        self.reviewed()
        self.assertEqual(self.deliver(), 0, self.err)
        self.logged("fix-a")
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 0, self.err)

    def has_branch(self):
        return self.git_out(self.repo, "branch", "--list", "oke/fix-a").strip() != ""

    def test_archive_keeps_the_worktree_and_branch(self):
        self.assertEqual(self.cli("job", "archive", "fix-a"), 0, self.err)
        self.assertIn("kept_worktree", self.out)
        self.assertTrue(os.path.isdir(self.wt))
        self.assertTrue(self.has_branch())

    def test_an_unmerged_branch_goes_only_with_force(self):
        self.assertEqual(self.cli("job", "archive", "fix-a"), 0, self.err)
        self.assertEqual(self.cli("job", "clean", "fix-a"), 1)
        self.assertIn("not merged", self.err)
        self.assertTrue(os.path.isdir(self.wt))
        self.assertEqual(self.cli("job", "clean", "fix-a", "--force"), 0, self.err)
        self.assertEqual(json.loads(self.out)["result"], "removed")
        self.assertFalse(os.path.isdir(self.wt))
        self.assertFalse(self.has_branch())
        self.assertEqual(self.cli("job", "clean", "fix-a"), 0, self.err)
        self.assertEqual(json.loads(self.out)["result"], "already clean")

    def test_a_merged_branch_goes_without_force(self):
        self.git(self.repo, "merge", "-q", "--ff-only", "oke/fix-a")
        self.assertEqual(self.cli("job", "clean", "fix-a"), 0, self.err)  # done, not archived yet
        self.assertFalse(os.path.isdir(self.wt))
        self.assertFalse(self.has_branch())

    def test_what_the_user_removed_already_is_clean(self):
        self.git(self.repo, "worktree", "remove", "--force", self.wt)
        self.git(self.repo, "branch", "-q", "-D", "oke/fix-a")
        self.assertEqual(self.cli("job", "archive", "fix-a"), 0, self.err)
        self.assertEqual(self.cli("job", "clean", "fix-a"), 0, self.err)
        self.assertEqual(json.loads(self.out)["result"], "already clean")

    def test_uncommitted_files_in_the_worktree_go_only_with_force(self):
        self.git(self.repo, "merge", "-q", "--ff-only", "oke/fix-a")
        with open(os.path.join(self.wt, "notes.txt"), "w") as f:
            f.write("the user's\n")
        self.assertEqual(self.cli("job", "clean", "fix-a"), 1)
        self.assertIn("uncommitted", self.err)
        self.assertEqual(self.cli("job", "clean", "fix-a", "--force"), 0, self.err)

    def test_an_open_job_is_not_cleaned(self):
        self.job("fix-b", "working")
        self.assertEqual(self.cli("job", "clean", "fix-b"), 1)
        self.assertIn("finished", self.err)


class TestPullRequest(DeliverBase):
    def setUp(self):
        DeliverBase.setUp(self)
        self.reviewed()
        self.assertEqual(self.deliver(), 0, self.err)
        self.gh_log = self.path("gh.log")
        script = self.write("gh.py", "import sys, json, os\n"
                                     "open(%r, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                                     "if sys.argv[2] == 'list':\n"
                                     "    print(os.environ.get('OKE_TEST_PR', ''))\n"
                                     "else:\n"
                                     "    print('https://github.com/o/r/pull/7')\n" % self.gh_log)
        os.environ["OKE_GH"] = json.dumps([sys.executable, script])
        os.environ.pop("OKE_TEST_PR", None)
        self.addCleanup(os.environ.pop, "OKE_TEST_PR", None)

    def origin(self, url="https://github.com/o/r.git"):
        """origin as GitHub names it; pushes go to a local bare repository."""
        bare = self.path("origin.git")
        subprocess.check_call(["git", "init", "-q", "--bare", bare])
        self.git(self.repo, "remote", "add", "origin", url or bare)
        self.git(self.repo, "config", "remote.origin.pushurl", bare)
        return bare

    def calls(self):
        if not os.path.exists(self.gh_log):
            return []
        with open(self.gh_log) as f:
            return [json.loads(l) for l in f if l.strip()]

    def pushed(self, bare):
        return subprocess.call(["git", "-C", bare, "rev-parse", "-q", "--verify", "refs/heads/oke/fix-a"],
                               stdout=subprocess.DEVNULL) == 0

    def test_push_and_open_a_pull_request(self):
        bare = self.origin()
        self.assertEqual(self.cli("job", "pr", "fix-a"), 0, self.err)
        self.assertEqual(json.loads(self.out), {"job": "fix-a", "branch": "oke/fix-a", "base": self.base,
                                                "url": "https://github.com/o/r/pull/7"})
        self.assertEqual(self.git_out(bare, "rev-parse", "oke/fix-a").strip(), self.tip())
        self.assertEqual([c[:2] for c in self.calls()], [["pr", "list"], ["pr", "create"]])
        self.assertEqual(self.calls()[-1], ["pr", "create", "--base", self.base, "--head", "oke/fix-a", "--fill"])

    def test_an_existing_pull_request_is_updated_not_opened_again(self):
        bare = self.origin()
        self.assertEqual(self.cli("job", "pr", "fix-a"), 0, self.err)
        # the branch is rewritten (a later rebase): the push replaces it, and the open PR follows
        self.git(self.repo, "branch", "-q", "-f", "oke/fix-a", "HEAD")
        os.environ["OKE_TEST_PR"] = "https://github.com/o/r/pull/7"
        self.assertEqual(self.cli("job", "pr", "fix-a"), 0, self.err)
        out = json.loads(self.out)
        self.assertEqual((out["url"], out["existing"]), ("https://github.com/o/r/pull/7", True))
        self.assertEqual(self.git_out(bare, "rev-parse", "oke/fix-a").strip(), self.head())
        self.assertEqual([c[1] for c in self.calls()], ["list", "create", "list"])

    def test_an_origin_not_on_github_pushes_nothing(self):
        bare = self.origin(url=None)
        self.assertEqual(self.cli("job", "pr", "fix-a"), 1)
        self.assertIn("not on GitHub", self.err)
        self.assertFalse(self.pushed(bare))
        self.assertEqual(self.calls(), [])

    def test_github_urls_are_known(self):
        for url in ("https://github.com/o/r.git", "git@github.com:o/r.git", "ssh://git@github.com/o/r"):
            self.assertTrue(oke.GITHUB.search(url), url)
        for url in ("https://gitlab.com/o/r.git", "C:/repos/github.com-copy", "https://notgithub.com/o/r"):
            self.assertFalse(oke.GITHUB.search(url), url)

    def test_no_origin_is_a_clear_error(self):
        self.assertEqual(self.cli("job", "pr", "fix-a"), 1)
        self.assertIn("no remote `origin`", self.err)
        self.assertEqual(self.calls(), [])

    def test_no_gh_is_a_clear_error_and_pushes_nothing(self):
        bare = self.origin()
        real = oke.gh_argv
        oke.gh_argv = lambda: None
        try:
            self.assertEqual(self.cli("job", "pr", "fix-a"), 1)
        finally:
            oke.gh_argv = real
        self.assertIn("`gh` is not installed", self.err)
        self.assertFalse(self.pushed(bare))

    def test_a_job_not_delivered_has_no_pull_request(self):
        self.origin()
        self.job("fix-b", "working")
        self.assertEqual(self.cli("job", "pr", "fix-b"), 1)
        self.assertIn("not delivered", self.err)


class TestContinue(DeliverBase):
    """Job fix-b continues the unmerged work of done job fix-a: its own worktree and branch
    oke/fix-b, made at the tip of oke/fix-a."""
    def setUp(self):
        DeliverBase.setUp(self)
        os.environ["OKE_RUNTIME"] = "bg"
        self.wt = self.worktree()
        self.reviewed()
        self.assertEqual(self.deliver(), 0, self.err)
        self.a_tip = self.tip()
        self.logged("fix-a")
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 0, self.err)
        self.job("fix-b", "working")
        self.set_plan("test -f b.txt", "1. b.txt exists", job="fix-b",
                      workspace="## Workspace\nnew worktree (branch oke/fix-b, from oke/fix-a)\n")

    def start(self):
        self.assertEqual(self.cli("worktree", "fix-b"), 0, self.err)
        out = json.loads(self.out)
        return out["path"]

    def reviewed_b(self, seq=1):
        self.order(self.report("fix-b-w1", seq, job="fix-b"), self.review(seq, commit=self.tip("oke/fix-b"),
                                                                           job="fix-b"))

    def test_the_plan_line_names_its_own_branch_and_where_it_starts(self):
        self.assertTrue(oke.plan_worktree("fix-b"))
        self.assertTrue(oke.plan_own_worktree("fix-b"))
        self.assertEqual(oke.plan_continues("fix-b"), "fix-a")
        self.job("fix", "working")
        self.set_plan("true", job="fix", workspace="## Workspace\nmain checkout, like oke/fix-a did\n")
        self.assertFalse(oke.plan_own_worktree("fix"))  # oke/fix is not in oke/fix-a

    def test_its_own_worktree_and_branch_start_at_the_other_branch(self):
        st = oke.load_state()
        st["jobs"]["fix-a"]["base"] = "the-a-base"  # fix-b goes onto fix-a's base, not main's branch now
        oke.save_state(st)
        self.git(self.repo, "branch", "-q", "the-a-base")
        self.assertEqual(self.cli("worktree", "fix-b"), 0, self.err)
        out = json.loads(self.out)
        self.assertNotEqual(os.path.normcase(out["path"]), os.path.normcase(self.wt))
        self.assertEqual((out["continues"], out["review_base"], out["base"]), ("fix-a", self.a_tip, "the-a-base"))
        self.assertEqual(self.tip("oke/fix-b"), self.a_tip)
        info = oke.load_state()["jobs"]["fix-b"]
        self.assertEqual((info["base"], info["start_commit"]), ("the-a-base", self.a_tip))
        self.assertEqual(oke.base_point(info, out["path"]), self.a_tip)  # its review covers its own commits
        self.assertEqual(self.cli("status"), 0, self.err)
        self.assertEqual(json.loads(self.out)["jobs"]["fix-b"]["review_base"], self.a_tip)

    def test_it_delivers_its_own_branch_and_leaves_the_other_alone(self):
        wt = self.start()
        self.commit_in(wt, "b.txt", "b\n")
        self.reviewed_b()
        self.assertEqual(self.deliver("fix-b"), 0, self.err)
        out = json.loads(self.out)
        self.assertEqual((out["branch"], out["commit"], out["contains"]),
                         ("oke/fix-b", self.tip("oke/fix-b"), "oke/fix-a"))
        self.assertEqual(self.tip(), self.a_tip)  # oke/fix-a untouched
        self.logged("fix-b")
        self.assertEqual(self.cli("job", "phase", "fix-b", "done"), 0, self.err)
        self.assertEqual(self.cli("job", "clean", "fix-b", "--force"), 0, self.err)
        self.assertFalse(os.path.isdir(wt))
        self.assertTrue(os.path.isdir(self.wt))
        self.assertEqual(self.tip(), self.a_tip)

    def test_a_rebase_keeps_the_review_range_its_own(self):
        wt = self.start()
        self.commit_in(wt, "b.txt", "b\n")
        self.reviewed_b()
        # the user merged fix-a, and the base moved on: only fix-b's own commit is replayed
        self.git(self.repo, "merge", "-q", "--ff-only", "oke/fix-a")
        self.commit_main("c.txt", "c\n")
        base_head = self.head()
        self.assertEqual(self.deliver("fix-b"), 4, self.err)
        new = self.tip("oke/fix-b")
        info = oke.load_state()["jobs"]["fix-b"]
        self.assertEqual(info["start_commit"], base_head)
        self.assertEqual(self.git_out(self.repo, "rev-list", "--count", "%s..%s" % (base_head, new)).strip(), "1")
        self.checked(new, job="fix-b")
        self.assertEqual(self.deliver("fix-b"), 0, self.err)

    def test_a_rebase_with_the_other_unmerged_moves_its_start_with_it(self):
        wt = self.start()
        self.commit_in(wt, "b.txt", "b\n")
        self.reviewed_b()
        self.commit_main("c.txt", "c\n")
        self.assertEqual(self.deliver("fix-b"), 4, self.err)
        new = self.tip("oke/fix-b")
        start = oke.load_state()["jobs"]["fix-b"]["start_commit"]
        self.assertEqual(start, self.git_out(self.repo, "rev-parse", new + "~1").strip())
        self.assertNotEqual(start, self.a_tip)  # fix-a's commit was rebased too, under it
        self.assertEqual(self.git_out(self.repo, "diff", "--name-only", start, new).split(), ["b.txt"])

    def test_a_merge_commit_in_its_own_work_falls_back_to_the_wider_range(self):
        wt = self.start()
        self.git(wt, "checkout", "-q", "-b", "side")
        self.commit_in(wt, "s.txt", "s\n")
        self.git(wt, "checkout", "-q", "oke/fix-b")
        self.commit_in(wt, "b.txt", "b\n")
        self.git(wt, "merge", "-q", "--no-ff", "-m", "merge side", "side")
        self.reviewed_b()
        self.commit_main("c.txt", "c\n")
        base_head = self.head()
        self.assertEqual(self.deliver("fix-b"), 1)  # fix-a's rebased commit is in the range now
        self.assertIn("patch id", self.err)
        self.assertEqual(oke.load_state()["jobs"]["fix-b"]["start_commit"], base_head)

    def test_a_commit_the_rebase_drops_falls_back_to_the_wider_range(self):
        wt = self.start()
        self.commit_in(wt, "b.txt", "b\n", msg="b")
        dropped = self.git_out(wt, "rev-parse", "HEAD").strip()
        self.commit_in(wt, "d.txt", "d\n", msg="d")
        self.reviewed_b()
        self.git(self.repo, "cherry-pick", dropped)  # the base has that change already
        base_head = self.head()
        self.assertEqual(self.deliver("fix-b"), 1)
        self.assertIn("patch id", self.err)
        self.assertEqual(oke.load_state()["jobs"]["fix-b"]["start_commit"], base_head)
        new = self.tip("oke/fix-b")
        self.assertEqual(self.git_out(self.repo, "rev-list", "--count", "%s..%s" % (base_head, new)).strip(), "2")

    def test_a_rebase_by_hand_moves_the_review_base(self):
        wt = self.start()
        self.commit_in(wt, "b.txt", "b\n")
        self.commit_main("c.txt", "c\n")
        base_head = self.head()
        self.git(wt, "rebase", "-q", self.base)  # the worker, after a conflict ladder step
        st = oke.load_state()
        st["jobs"]["fix-b"]["base"] = self.base
        oke.save_state(st)
        self.assertEqual(self.cli("status"), 0, self.err)
        self.assertEqual(json.loads(self.out)["jobs"]["fix-b"]["review_base"], base_head)
        self.reviewed_b()
        self.assertEqual(self.deliver("fix-b"), 0, self.err)
        self.assertEqual(oke.load_state()["jobs"]["fix-b"]["start_commit"], base_head)

    def test_a_second_start_needs_not_the_other_branch(self):
        self.start()
        self.git(self.repo, "update-ref", "-d", "refs/heads/oke/fix-a")  # merged and deleted meanwhile
        info = oke.load_state()["jobs"]["fix-b"]
        self.assertEqual(oke.continue_start("fix-b", info, "fix-a")["commit"], self.a_tip)

    def test_the_plan_line_takes_a_whole_job_name_only(self):
        long = "a" * 25
        self.set_plan("true", job="fix-b", workspace="## Workspace\nnew worktree (branch oke/fix-b, from oke/%s)\n" % long)
        self.assertIsNone(oke.plan_continues("fix-b"))
        self.set_plan("true", job="fix-b", workspace="## Workspace\nnew worktree (branch oke/fix-b, from `oke/fix-a`)\n")
        self.assertEqual(oke.plan_continues("fix-b"), "fix-a")

    def test_the_other_worktree_may_be_gone_its_branch_not(self):
        self.git(self.repo, "worktree", "remove", "--force", self.wt)
        self.start()
        self.assertEqual(self.tip("oke/fix-b"), self.a_tip)

    def test_a_gone_branch_is_refused(self):
        self.git(self.repo, "worktree", "remove", "--force", self.wt)
        self.git(self.repo, "branch", "-q", "-D", "oke/fix-a")
        self.assertEqual(self.cli("worktree", "fix-b"), 1)
        self.assertIn("gone", self.err)

    def test_an_open_job_is_not_continued(self):
        st = oke.load_state()
        st["jobs"]["fix-a"]["phase"] = "working"
        oke.save_state(st)
        self.assertEqual(self.cli("worktree", "fix-b"), 1)
        self.assertIn("new requirements", self.err)

    def test_an_archived_job_is_continued_and_listed_while_its_branch_is_there(self):
        self.assertEqual(self.cli("job", "archive", "fix-a"), 0, self.err)
        self.assertIn("for the user to merge", self.out)
        kept = oke.kept_worktrees()
        self.assertEqual([(k["job"], k["branch"], k["done"]) for k in kept], [("fix-a", "oke/fix-a", True)])
        self.start()
        self.assertEqual(self.tip("oke/fix-b"), self.a_tip)
        self.git(self.repo, "update-ref", "-d", "refs/heads/oke/fix-a")  # merged and deleted by the user
        self.assertEqual(oke.kept_worktrees()[0]["branch"], None)


class TestLedger(DeliverBase):
    def delivered(self):
        self.reviewed()
        self.assertEqual(self.deliver(), 0, self.err)

    def note(self):
        self.assertEqual(self.cli("ledger", "add", "fix-a", "--by", "w", "--commit", "abc", "--check", "note",
                                  "--result", "pass"), 0, self.err)

    def test_a_worker_records_its_check(self):
        self.assertEqual(self.cli("ledger", "add", "fix-a", "--by", "fix-a-w1", "--commit", "abc1234",
                                  "--check", "worker-verify", "--result", "pass", "--detail", "pytest -q\tall"), 0)
        row = self.ledger()["rows"][0]
        self.assertEqual((row["by"], row["check"], row["result"], row["detail"]),
                         ("fix-a-w1", "worker-verify", "pass", "pytest -q all"))
        self.assertEqual(self.cli("ledger", "add", "nope", "--by", "x", "--commit", "x",
                                  "--check", "worker-verify", "--result", "pass"), 1)

    def test_nobody_forges_an_oke_row(self):
        for check in ("deliver", "review", "check"):
            self.assertEqual(self.cli("ledger", "add", "fix-a", "--by", "w", "--commit", "x",
                                      "--check", check, "--result", "ok"), 1)
        self.assertEqual(self.ledger()["deliver"], "none")

    def test_a_head_busy_for_a_moment_is_read_again(self):
        self.delivered()
        real, calls = io.open, []

        def busy(path, *a, **k):
            if str(path).endswith("ledger.head") and not calls:
                calls.append(path)
                raise PermissionError(13, "in use")
            return real(path, *a, **k)
        oke.io.open = busy
        try:
            self.assertEqual(oke.deliver_state("fix-a")[0], "delivered")
        finally:
            oke.io.open = real
        self.assertEqual(len(calls), 1)

    def test_ledgers_signed_before_heads_get_theirs_once(self):
        self.delivered()
        key_path = os.path.join(self.home, oke.LEDGER_KEY)
        os.remove(key_path + oke.KEY_HEADS)  # a key from before heads
        os.remove(os.path.join(self.jobs, "ledger.head"))
        self.assertEqual(oke.deliver_state("fix-a")[0], "delivered")
        self.assertTrue(os.path.exists(os.path.join(self.jobs, "ledger.head")))
        self.assertTrue(os.path.exists(key_path + oke.KEY_HEADS))
        with open(key_path) as f:
            self.assertRegex(f.read(), r"^[0-9a-f]{64}$")  # hex only, as older OKe reads it
        os.remove(os.path.join(self.jobs, "ledger.head"))  # from now on a lost head is a cut ledger
        with self.assertRaises(oke.StateError):
            oke.deliver_state("fix-a")

    def test_a_torn_last_line_does_not_swallow_the_next_row(self):
        self.delivered()
        with open(oke.ledger_path("fix-a"), "a") as f:
            f.write("2026-01-01T00:00:00\tw\tabc")  # a writer died here
        self.note()
        self.assertEqual(self.ledger()["rows"][-1]["check"], "note")

    def test_rows_written_by_hand_are_not_rows(self):
        self.reviewed()
        path = oke.ledger_path("fix-a")
        self.assertEqual(self.deliver(), 0, self.err)
        with open(path) as f:
            lines = f.read().splitlines()
        row = [l for l in lines if "\tdeliver\t" in l][-1]
        commit = row.split("\t")[2]
        with open(path, "w", newline="\n") as f:  # the real delivery row gone: only forged ones
            f.write("\n".join(l for l in lines if l != row) + "\n")
        os.remove(os.path.join(self.jobs, "ledger.head"))
        with self.assertRaises(oke.StateError):  # a cut ledger shows
            oke.deliver_state("fix-a")

    def test_hand_rows_are_ignored(self):
        self.delivered()
        path = oke.ledger_path("fix-a")
        with open(path) as f:
            lines = f.read().splitlines()
        row = [l for l in lines if "\tdeliver\t" in l][-1]
        with open(path, "a", newline="\n") as f:
            f.write("2026-01-01T00:00:00\tw\tabc\tdeliver\tok\tby hand\n")  # no signature
            f.write("2026-01-01T00:00:00\tw\tabc\tdeliver\tok\tby hand\t%s\n" % ("0" * 32))
            f.write(row + "\n")  # a real row copied: its signature belongs to another place
        led = self.ledger()
        self.assertEqual((led["deliver"], led["ignored_lines"]), ("delivered", 3))
        self.note()  # the next real row still chains on
        self.assertEqual(self.ledger()["rows"][-1]["check"], "note")

    def test_rows_cut_from_the_end_show(self):
        self.delivered()
        self.note()
        path = oke.ledger_path("fix-a")
        with open(path) as f:
            lines = f.read().splitlines(True)
        with open(path, "w", newline="\n") as f:
            f.writelines(lines[:-1])
        with self.assertRaises(oke.StateError) as e:
            oke.deliver_state("fix-a")
        self.assertIn("lost rows", str(e.exception))
        with open(path, "w", newline="\n") as f:
            f.writelines(lines)
        os.remove(os.path.join(os.path.dirname(path), "ledger.head"))
        with self.assertRaises(oke.StateError):
            oke.deliver_state("fix-a")

    def test_a_row_written_before_its_head_is_fine(self):
        self.delivered()
        head = os.path.join(os.path.dirname(oke.ledger_path("fix-a")), "ledger.head")
        with open(head) as f:
            old = f.read()
        self.note()
        with open(head, "w", newline="\n") as f:
            f.write(old)  # the writer died after its row, before the head
        self.assertEqual(oke.deliver_state("fix-a")[0], "delivered")
        self.note()
        self.assertEqual(oke.deliver_state("fix-a")[0], "delivered")

    def test_a_row_from_another_job_is_not_a_row(self):
        self.delivered()
        self.job("fix-b", "working")
        with open(oke.ledger_path("fix-a")) as f:
            text = f.read()
        with open(oke.ledger_path("fix-b"), "w", newline="\n") as f:
            f.write(text)
        self.assertEqual(oke.deliver_state("fix-b")[0], "none")

    def test_ledgers_from_before_signing_are_signed_once(self):
        if os.path.exists(os.path.join(self.home, oke.LEDGER_KEY)):
            os.remove(os.path.join(self.home, oke.LEDGER_KEY))
        with open(oke.ledger_path("fix-a"), "w", newline="\n") as f:
            f.write("2026-01-01T00:00:00\toke\tabc\treview\tpass\tr\n")
        self.assertEqual(oke.deliver_state("fix-a")[0], "none")
        with open(oke.ledger_path("fix-a"), "a", newline="\n") as f:
            f.write("2026-01-01T00:00:01\tw\tabc\tdeliver\tok\tlater, by hand\n")
        self.assertEqual(oke.deliver_state("fix-a")[0], "none")  # signed only once

    def test_a_lost_key_is_never_replaced_by_itself(self):
        self.delivered()
        os.remove(os.path.join(self.home, oke.LEDGER_KEY))
        with open(oke.ledger_path("fix-a"), "a", newline="\n") as f:
            f.write("2026-01-01T00:00:01\tw\tabc\tnote\tpass\tby hand\n")
        with self.assertRaises(oke.StateError):
            oke.deliver_state("fix-a")
        self.assertFalse(os.path.exists(os.path.join(self.home, oke.LEDGER_KEY)))
        tree = oke.board_tree(oke.load_state(), [])  # the board still shows everything else
        self.assertEqual([j["deliver"] for r in tree for j in r["jobs"] if j["job"] == "fix-a"], ["unreadable"])
        with self.assertRaises(oke.StateError) as e:
            oke.deliver_state("fix-a")
        self.assertIn(".new", str(e.exception))
        recover = re.search(r'python -c "(.*)"$', str(e.exception)).group(1)
        subprocess.check_call([sys.executable, "-c", recover])  # the user's way out, as printed
        self.assertEqual(oke.deliver_state("fix-a")[0], "delivered")

    def test_a_first_signing_that_stopped_resumes(self):
        if os.path.exists(os.path.join(self.home, oke.LEDGER_KEY)):
            os.remove(os.path.join(self.home, oke.LEDGER_KEY))
        with open(oke.ledger_path("fix-a"), "w", newline="\n") as f:
            f.write("2026-01-01T00:00:00\toke\tabc\tdeliver\tok\tbranch=oke/fix-a base=main@def\n")
        real = oke.replace_file
        calls = []

        def stop_before_key(src, dst):
            calls.append(dst)
            if dst.endswith(oke.LEDGER_KEY):
                raise OSError("stopped")
            real(src, dst)
        oke.replace_file = stop_before_key
        try:
            with self.assertRaises(OSError):
                oke.deliver_state("fix-a")
        finally:
            oke.replace_file = real
        self.assertEqual(oke.deliver_state("fix-a")[0], "delivered")  # resumed with the same key

    def test_a_non_ascii_line_does_not_break_reading(self):
        self.delivered()
        with open(oke.ledger_path("fix-a"), "a", encoding="utf-8", newline="\n") as f:
            f.write(u"2026-01-01T00:00:01\tw\tabc\tnote\tpass\t검증\t검증\n")
        self.assertEqual(oke.deliver_state("fix-a")[0], "delivered")

    def test_an_archived_name_is_never_used_again(self):
        os.makedirs(os.path.join(self.home, "archive", "fix-z-20260101-000000"))
        self.assertEqual(self.cli("job", "new", "fix-z", "--repo", self.repo), 1)
        self.assertIn("archived before", self.err)

    def test_agents_may_not_touch_the_key(self):
        args = oke.launch_args("worker", oke.load_config(), self.home, "plugin", folder=self.jobs + "/fix-a")
        denied = args[args.index("--disallowedTools") + 1:]
        self.assertIn("Bash(*ledger.key*)", denied)
        self.assertTrue(any(r.startswith("Read(") and r.endswith("/ledger.key)") for r in denied), denied)

    def test_no_baseline_needs_a_merge_guard(self):
        self.delivered()
        with open(os.path.join(self.jobs, "baseline.json"), "w") as f:
            json.dump({"version": 1, "exit": 0, "signatures": []}, f)  # one an older OKe recorded
        self.cli("check", "fix-a")
        self.assertIn("run `$OKE baseline fix-a` again", self.err)


class TestDirectCommit(DeliverBase):
    """A job without a worktree: its reviewed changes are uncommitted in the main checkout."""
    def setUp(self):
        DeliverBase.setUp(self)
        self.git(self.repo, "branch", "-q", "-D", "oke/fix-a")
        self.write("toy/d.txt", "d\n")
        self.git(self.repo, "add", "d.txt")  # the PM stages the job's new files
        self.assertEqual(self.cli("job", "tree", "fix-a"), 0, self.err)
        self.tree = json.loads(self.out)["tree"]
        self.tree_review(self.tree)
        self.pre = self.head()

    def tree_review(self, tree):
        path = os.path.join(self.jobs, "fix-a-rv", "assignment.md")
        if not os.path.isdir(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:  # it reviewed `git diff HEAD`: the tree, not a commit
            f.write("agent: fix-a-rv\nrole: reviewer\njob: fix-a\nseq: 1\ncommit: no\ntree: %s\ncriteria_rev: %s\n---\n"
                    % (tree, oke.criteria_rev("fix-a")))
        self.checked("tree:" + tree)
        self.order(self.report("fix-a-w1", 1), self.report("fix-a-rv", 1, "verdict: pass"))

    def test_deliver_commits_the_changes_and_the_job_can_finish(self):
        self.assertEqual(self.deliver(), 0, self.err)
        out = json.loads(self.out)
        self.assertTrue(out["direct"])
        self.assertEqual(out["commit"], self.head())
        self.assertEqual(self.git_out(self.repo, "status", "--porcelain").strip(), "")
        self.assertEqual(self.ledger()["deliver"], "delivered")
        self.assertEqual(self.ledger()["rows"][-1]["detail"], "main pre=" + self.pre)
        self.logged("fix-a")
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 0, self.err)

    def test_unstaged_byproducts_are_not_committed(self):
        self.write("toy/__pycache__/d.cpython-311.pyc", "x")  # left by the checks after the review
        self.assertEqual(self.deliver(), 0, self.err)
        self.assertEqual(self.git_out(self.repo, "status", "--porcelain").strip(), "?? __pycache__/")
        self.assertNotIn("__pycache__", self.git_out(self.repo, "show", "--name-only", "HEAD"))
        self.assertEqual(json.loads(self.out)["left_untracked"], ["__pycache__/d.cpython-311.pyc"])

    def test_an_unstaged_new_file_is_left_out_of_the_review_tree(self):
        self.write("toy/notes.txt", "the user's own\n")  # not staged: not the job's
        self.assertEqual(self.cli("job", "tree", "fix-a"), 0, self.err)
        self.assertEqual(json.loads(self.out)["tree"], self.tree)

    def test_needs_a_check_on_the_reviewed_tree(self):
        self.checked("tree:" + self.tree, "fail")
        self.assertEqual(self.deliver(), 1)
        self.assertIn("oke check", self.err)
        self.checked("tree:" + self.tree)
        self.assertEqual(self.deliver(), 0, self.err)

    def test_a_tree_git_cannot_read_is_an_error_not_a_traceback(self):
        self.assertIsNone(oke.pending_tree(os.path.join(self.home, "no-such-repo")))

    def test_a_change_after_the_review_is_refused(self):
        self.write("toy/d.txt", "changed after the review\n")
        self.assertEqual(self.deliver(), 1)
        self.assertIn("not the ones the review covered", self.err)
        self.assertEqual(self.head(), self.pre)
        self.assertEqual(self.git_out(self.repo, "diff", "--cached", "--name-only").strip(), "d.txt")
        self.assertEqual(self.git_out(self.repo, "show", ":d.txt"), "d\n")

    def test_the_commit_holds_exactly_the_reviewed_tree(self):
        self.assertEqual(self.deliver(), 0, self.err)
        self.assertEqual(self.git_out(self.repo, "rev-parse", "HEAD^{tree}").strip(), self.tree)

    def test_another_branch_checked_out_is_refused(self):
        self.git(self.repo, "checkout", "-q", "-b", "feature")
        self.assertEqual(self.deliver(), 1)
        self.assertIn("base branch", self.err)

    def test_a_merge_in_progress_is_left_alone(self):
        open(os.path.join(self.repo, ".git", "MERGE_HEAD"), "w").write(self.pre + "\n")
        self.assertEqual(self.deliver(), 1)
        self.assertIn("merge is in progress", self.err)

    def test_a_review_of_main_as_it_is_is_recorded_already_in_and_can_finish(self):
        self.git(self.repo, "reset", "-q")
        os.remove(os.path.join(self.repo, "d.txt"))
        self.write("toy/__pycache__/x.pyc", "x")  # untracked: not the job's
        self.tree_review(self.git_out(self.repo, "rev-parse", "HEAD^{tree}").strip())
        self.assertEqual(self.deliver(), 0, self.err)
        self.assertEqual(json.loads(self.out)["already_in"], True)
        self.assertEqual(self.head(), self.pre)  # no commit
        self.logged("fix-a")
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 0, self.err)

    def test_an_unstaged_new_file_is_not_taken_for_already_in(self):
        self.git(self.repo, "reset", "-q")  # d.txt stays, untracked: the review tree is HEAD's
        self.tree_review(self.git_out(self.repo, "rev-parse", "HEAD^{tree}").strip())
        self.assertEqual(self.deliver(), 1)
        self.assertIn("untracked files d.txt", self.err)
        self.assertEqual(self.ledger()["deliver"], "none")

    def test_nothing_to_commit_is_refused(self):
        self.git(self.repo, "reset", "-q")
        os.remove(os.path.join(self.repo, "d.txt"))
        self.assertEqual(self.deliver(), 1)
        self.assertIn("no changes", self.err)

    def test_a_reviewed_commit_without_its_branch_is_still_refused(self):
        self.review(1, commit=self.pre)
        self.assertEqual(self.deliver(), 1)
        self.assertIn("does not exist", self.err)


if __name__ == "__main__":
    unittest.main()
