"""Slice 3: several jobs per PM, inbox, per-PM wait, user queue, merge line."""
import io
import json
import os
import time
import unittest

from test_oke import AG, AGENT, E, J, OK, SpawnBase
import oke


class S3Base(SpawnBase):
    def setUp(self):
        SpawnBase.setUp(self)
        os.environ["HERDR_ENV"] = "1"
        self.key = oke.repo_key(self.repo)

    def put(self, path, text):
        d = os.path.dirname(path)
        if not os.path.isdir(d):
            os.makedirs(d)
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return path

    def pm_job(self, job, seq, report=None):
        """The orchestrator's assignment for `job` and, optionally, the PM's report."""
        folder = os.path.join(self.home, "jobs", job, "pm")
        self.put(os.path.join(folder, "assignment.md"),
                 "agent: pm-toy\nrole: pm\njob: %s\nseq: %d\n---\n" % (job, seq))
        if report:
            self.put(os.path.join(folder, "report.md"), report)
        return folder


class TestJobState(S3Base):
    def test_job_new_prints_the_pm_job_dir_and_records_phase_at(self):
        before = time.time()
        self.assertEqual(self.cli("job", "new", "fix-a", "--repo", self.repo), 0)
        info = json.loads(self.out)
        self.assertTrue(info["pm_job_dir"].endswith("/jobs/fix-a/pm"))
        self.assertGreaterEqual(oke.load_state()["jobs"]["fix-a"]["phase_at"], before)

    def test_phase_merging_is_accepted_and_moves_phase_at(self):
        self.job("fix-a", "awaiting_merge")
        oke.save_state(dict(oke.load_state(), jobs={"fix-a": dict(oke.load_state()["jobs"]["fix-a"], phase_at=1.0)}))
        self.assertEqual(self.cli("job", "phase", "fix-a", "merging"), 0)
        job = oke.load_state()["jobs"]["fix-a"]
        self.assertEqual(job["phase"], "merging")
        self.assertGreater(job["phase_at"], 1.0)

    def test_spawn_of_a_worker_is_allowed_while_merging(self):
        # a merge conflict sends the worker back to rebase; a dead one must come back
        self.job("fix-a", "merging")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        self.fake({"agent list": [J({"agents": []})], "agent start": [E("agent_not_ready")]})
        self.cli("spawn", a, "--pane", "w1:p2")
        self.assertNotIn("plan not approved", self.err)

    def test_defer_records_the_current_item_key(self):
        self.job("fix-a", "awaiting_merge")
        self.assertEqual(self.cli("job", "defer", "fix-a"), 0)
        job = oke.load_state()["jobs"]["fix-a"]
        self.assertEqual(job["deferred"], "merge:%r" % job["phase_at"])

    def test_defer_with_nothing_pending_fails(self):
        self.job("fix-a", "working")
        self.assertEqual(self.cli("job", "defer", "fix-a"), 1)


if __name__ == "__main__":
    unittest.main()
