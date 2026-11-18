# -*- coding: utf-8 -*-
"""Review findings 4-11 (timeouts, outages during a revive, phase gates, sender,
reviewer verdict and permissions, orchestrator conflict) and the intake interview."""
import json
import os
import unittest

import oke
import runtime_herdr
from test_oke import AGENT, SpawnBase
from test_revive import E, ReviveBase


class TestHerdrTimeout(SpawnBase):
    def test_a_hung_herdr_is_an_outage_not_a_hang(self):
        old_argv, old_timeout = os.environ.get("OKE_HERDR"), runtime_herdr.HERDR_TIMEOUT
        os.environ["OKE_HERDR"] = json.dumps(["python", "-c", "import time; time.sleep(30)"])
        runtime_herdr.HERDR_TIMEOUT = 1
        try:
            with self.assertRaises(oke.HerdrError) as e:
                oke.herdr("agent", "list")
            self.assertEqual(e.exception.code, "herdr_unavailable")
            self.assertIn("did not answer", e.exception.message)
        finally:
            runtime_herdr.HERDR_TIMEOUT = old_timeout
            if old_argv is None:
                del os.environ["OKE_HERDR"]
            else:
                os.environ["OKE_HERDR"] = old_argv


class TestOutageDuringRevive(ReviveBase):
    def test_the_wait_survives_a_passing_outage_inside_revive(self):
        calls = []
        real = oke.revive

        def flaky(name):
            calls.append(name)
            if len(calls) == 1:
                raise oke.HerdrError("herdr_unavailable", "down for a moment")
            return "revived"
        oke.revive = flaky
        try:
            self.fake({"agent get": [E("agent_not_found"), E("agent_not_found"), AGENT("working")]})
            report = os.path.join(self.child_dir, "report.md")
            self.assertIsNone(oke.wait_loop("fix-a-w1", report, 2, poll=0, max_polls=4))
            self.assertEqual(len(calls), 2)
        finally:
            oke.revive = real

    def test_a_revive_that_always_meets_an_outage_ends_the_wait(self):
        real = oke.revive

        def down(name):
            raise oke.HerdrError("herdr_unavailable", "agent start hangs")
        oke.revive = down
        try:
            self.fake({"agent get": [E("agent_not_found")]})
            report = os.path.join(self.child_dir, "report.md")
            with self.assertRaises(oke.HerdrError):
                oke.wait_loop("fix-a-w1", report, 2, poll=0, max_polls=oke.HERDR_RETRIES + 5)
        finally:
            oke.revive = real

    def test_an_outage_does_not_count_as_a_crash(self):
        real = oke.relaunch

        def down(*a, **k):
            raise oke.HerdrError("herdr_unavailable", "down")
        oke.relaunch = down
        try:
            self.fake({"agent list": [{"stdout": json.dumps({"result": {"agents": []}})}]})
            with self.assertRaises(oke.HerdrError):
                oke.revive("fix-a-w1")
            self.assertEqual(oke.read_children(self.key)["fix-a-w1"].get("revives"), 0)
        finally:
            oke.relaunch = real


class TestPhaseGates(SpawnBase):
    def test_merging_needs_awaiting_merge(self):
        self.job("fix-a", "working")
        self.assertEqual(self.cli("job", "phase", "fix-a", "merging"), 1)
        self.assertIn("awaiting_merge", self.err)
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["phase"], "working")

    def test_working_needs_the_plan_approval_step(self):
        self.job("fix-a", "planning")
        self.assertEqual(self.cli("job", "phase", "fix-a", "working"), 1)
        self.advance("fix-a", "working")

    def test_done_needs_merging(self):
        self.job("fix-a", "awaiting_merge")
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 1)

    def test_full_autonomy_still_passes_through_the_awaiting_phases(self):
        self.job("fix-a", "planning")
        self.logged("fix-a")
        for step in ("awaiting_approval", "working", "awaiting_merge", "merging", "done"):
            self.assertEqual(self.cli("job", "phase", "fix-a", step), 0, self.err)

    def test_failed_and_a_merge_hold_are_always_allowed(self):
        self.job("fix-a", "merging")
        self.assertEqual(self.cli("job", "phase", "fix-a", "awaiting_merge"), 0)
        self.assertEqual(self.cli("job", "phase", "fix-a", "failed"), 0)


class TestInterview(SpawnBase):
    def setUp(self):
        SpawnBase.setUp(self)
        self.assertEqual(self.cli("job", "new", "fix-a", "--repo", self.repo), 0)

    def round(self, goal, constraints, criteria, *extra):
        code = self.cli("interview", "fix-a", "--goal", goal, "--constraints", constraints,
                        "--criteria", criteria, *extra)
        return code, (json.loads(self.out) if code == 0 else None)

    def test_planning_needs_a_finished_interview(self):
        self.assertEqual(self.cli("job", "phase", "fix-a", "planning"), 1)
        self.assertIn("interview", self.err)
        self.round(0.5, 0.5, 0.5)
        self.assertEqual(self.cli("job", "phase", "fix-a", "planning"), 1)

    def test_a_clear_request_ends_at_round_zero(self):
        code, out = self.round(1, 0.8, 0.9)  # 1 - (0.40 + 0.24 + 0.27) = 0.09
        self.assertEqual((code, out["round"], out["done"], out["reason"]), (0, 0, True, "clear"))
        self.assertAlmostEqual(out["ambiguity"], 0.09)
        self.assertEqual(self.cli("job", "phase", "fix-a", "planning"), 0, self.err)

    def test_the_weakest_weighted_dimension_is_asked_next(self):
        _, out = self.round(0.5, 0.4, 0.9)  # gaps: goal 0.20, constraints 0.18, criteria 0.03
        self.assertEqual(out["weakest"], "goal")
        self.assertFalse(out["done"])
        _, out = self.round(0.9, 0.4, 0.9, "--question", "q", "--answer", "a")
        self.assertEqual((out["round"], out["weakest"]), (1, "constraints"))
        with open(oke.interview_path("fix-a")) as f:
            rounds = json.load(f)["rounds"]
        self.assertEqual((rounds[1]["question"], rounds[1]["answer"]), ("q", "a"))

    def test_challenges_and_stall(self):
        outs = [self.round(0.5, 0.5, 0.5)[1] for _ in range(4)]
        self.assertEqual([o["challenge"] for o in outs], [None, None, None, "contrarian"])
        self.assertEqual([o["stalled"] for o in outs], [False, False, True, True])

    def test_the_user_may_stop_and_the_round_cap_stops(self):
        _, out = self.round(0.5, 0.5, 0.5, "--stop")
        self.assertEqual((out["done"], out["reason"]), (True, "user_stop"))
        self.assertEqual(self.round(1, 1, 1)[0], 1)  # done is done
        self.assertEqual(self.cli("job", "new", "fix-b", "--repo", self.repo), 0)
        for _ in range(oke.INTERVIEW_MAX_ROUNDS + 1):
            self.cli("interview", "fix-b", "--goal", 0.1, "--constraints", 0.1, "--criteria", 0.1)
        self.assertEqual(json.loads(self.out)["reason"], "round_cap")

    def test_scores_must_be_between_0_and_1(self):
        self.assertEqual(self.round(1.5, 1, 1)[0], 1)
        self.assertEqual(self.cli("interview", "nope", "--goal", 1, "--constraints", 1, "--criteria", 1), 1)


class TestSender(SpawnBase):
    def test_an_unknown_pane_is_not_the_user(self):
        self.job("fix-a", "working")
        s = oke.load_state()
        s["repos"][oke.repo_key(self.repo)] = {"pm": {"agent": "pm-toy", "pane": "w1:p3"}}
        oke.save_state(s)
        os.environ["HERDR_PANE_ID"] = "w9:p9"
        try:
            self.fake({"agent list": [{"stdout": json.dumps({"result": {"agents": []}})}]})
            self.assertEqual(self.cli("send", "pm-toy", "hello"), 0, self.err)
        finally:
            del os.environ["HERDR_PANE_ID"]
        texts = [t for _, t in oke.inbox_messages("pm-toy")]
        self.assertEqual(texts, ["unverified (pane w9:p9): hello"])


class TestVerdict(SpawnBase):
    def report(self, text):
        return self.write("rv/report.md", text)

    def test_only_the_line_after_the_header_counts(self):
        self.assertEqual(oke.review_verdict(self.report("type: done\nseq: 1\n---\n\nverdict: pass\n")), "pass")
        quoted = "type: done\nseq: 1\n---\nverdict: changes\n## 지적\nverdict: pass was wrong\n"
        self.assertEqual(oke.review_verdict(self.report(quoted)), "changes")
        body_only = "type: done\nseq: 1\n---\n## 요약\nverdict: pass\n"
        self.assertIsNone(oke.review_verdict(self.report(body_only)))
        self.assertIsNone(oke.review_verdict(self.report("verdict: pass\n")))
        self.assertEqual(oke.review_verdict(self.report("seq: 1\n---\nverdict: pass (no findings)\n")), "pass")
        self.assertIsNone(oke.review_verdict(self.report("seq: 1\n---\nverdict: passed\n")))


class TestReviewerPermissions(unittest.TestCase):
    def test_the_reviewer_may_not_write_through_git_output(self):
        config = {"models": {}, "wiki_path": "/w", "autonomy": "balanced"}
        args = oke.launch_args("reviewer", config, "/h", "/p")
        denied = args[args.index("--disallowedTools") + 1:]
        self.assertIn("Bash(git *--output*)", denied)


if __name__ == "__main__":
    unittest.main()
