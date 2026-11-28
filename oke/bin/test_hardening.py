# -*- coding: utf-8 -*-
"""Review findings 4-11 (timeouts, outages during a revive, phase gates, sender,
reviewer verdict and permissions, orchestrator conflict) and the intake interview."""
import io
import json
import os
import shutil
import tempfile
import unittest

import oke
import runtime_herdr
from test_oke import put_pm, AGENT, SpawnBase
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
    def test_an_outage_does_not_count_as_a_crash(self):
        real = oke.relaunch

        def down(*a, **k):
            raise oke.HerdrError("herdr_unavailable", "down")
        oke.relaunch = down
        try:
            self.fake({"agent list": [{"stdout": json.dumps({"result": {"agents": []}})}]})
            with self.assertRaises(oke.HerdrError):
                oke.revive("fix-a-w1")
            self.assertEqual(oke.read_children(self.pm)["fix-a-w1"].get("revives"), 0)
        finally:
            oke.relaunch = real


class TestPhaseGates(SpawnBase):
    def test_the_merge_phases_are_gone(self):
        self.job("fix-a", "working")
        self.assertEqual(self.cli("job", "phase", "fix-a", "merging"), 1)
        self.assertIn("bad phase", self.err)
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["phase"], "working")

    def test_working_needs_the_plan_approval_step(self):
        self.job("fix-a", "planning")
        self.assertEqual(self.cli("job", "phase", "fix-a", "working"), 1)
        self.advance("fix-a", "working")

    def test_done_needs_the_work_started(self):
        self.job("fix-a", "awaiting_approval")
        self.logged("fix-a")
        self.verified("fix-a")
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 1)

    def test_done_needs_a_delivery(self):
        # a PM that skipped `oke job deliver` must not tell the user the work is ready
        self.job("fix-a", "reviewing")
        self.logged("fix-a")
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 1)
        self.assertIn("not delivered", self.err)
        self.verified("fix-a")
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 0, self.err)

    def test_full_autonomy_still_passes_through_the_awaiting_phases(self):
        self.job("fix-a", "planning")
        self.logged("fix-a")
        self.verified("fix-a")
        self.planned("fix-a")
        for step in ("awaiting_approval", "working", "reviewing", "done"):
            self.assertEqual(self.cli("job", "phase", "fix-a", step), 0, self.err)

    def test_failed_is_always_allowed(self):
        self.job("fix-a", "reviewing")
        self.assertEqual(self.cli("job", "phase", "fix-a", "failed"), 0)


class TestInterview(SpawnBase):
    def setUp(self):
        SpawnBase.setUp(self)
        self.assertEqual(self.cli("job", "new", "fix-a", "--repo", self.repo), 0)

    def round(self, goal, constraints, criteria, *extra):
        code = self.cli("interview", "fix-a", "--goal", goal, "--constraints", constraints,
                        "--criteria", criteria, *extra)
        return code, (json.loads(self.out) if code == 0 else None)

    def test_a_job_name_cannot_reach_outside_the_jobs_folder(self):
        os.makedirs(os.path.join(self.home, "wiki"), exist_ok=True)  # jobs/../wiki exists
        self.assertEqual(self.cli("ledger", "add", "../wiki", "--by", "x", "--commit", "abc1234",
                                  "--check", "x-verify", "--result", "pass"), 1)
        self.assertEqual(self.cli("interview", "../wiki", "--goal", 1, "--constraints", 1, "--criteria", 1), 1)
        self.assertNotIn("ledger.tsv", os.listdir(os.path.join(self.home, "wiki")))
        self.assertNotIn("interview.json", os.listdir(os.path.join(self.home, "wiki")))

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
        put_pm(s, oke.repo_key(self.repo), {"agent": "pm-toy", "pane": "w1:p3"})
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
        quoted = "type: done\nseq: 1\n---\nverdict: changes\n## Findings\nverdict: pass was wrong\n"
        self.assertEqual(oke.review_verdict(self.report(quoted)), "changes")
        body_only = "type: done\nseq: 1\n---\n## Summary\nverdict: pass\n"
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


class TestClaudeDevFixes(unittest.TestCase):
    config = {"models": {}, "wiki_path": "/w", "autonomy": "balanced", "lean_launch": False}

    def denied(self, role, folder=None):
        args = oke.launch_args(role, self.config, "/h", "/p", folder=folder)
        return args[args.index("--disallowedTools") + 1:]

    def test_every_child_gets_the_git_deny_rules(self):
        for role in ("pm", "worker", "reviewer"):
            self.assertIn("Bash(git push:*)", self.denied(role))
            self.assertIn("Bash(git reset --hard:*)", self.denied(role))
            self.assertIn("Bash(git branch --delete --force:*)", self.denied(role))
        for mode in ("conservative", "balanced"):
            args = oke.launch_args("worker", dict(self.config, autonomy=mode), "/h", "/p")
            self.assertIn("Bash(git push:*)", args[args.index("--disallowedTools") + 1:])

    def test_workers_and_reviewers_may_not_throw_away_uncommitted_work(self):
        wanted = ["Bash(git reset --hard:*)", "Bash(git clean -f:*)", "Bash(git checkout -- .)",
                  "Bash(git checkout .)", "Bash(git restore .)", "Bash(git branch -D:*)", "Bash(git push:*)",
                  "Bash(git stash:*)"]
        for role in ("worker", "reviewer"):
            for rule in wanted:
                self.assertIn(rule, self.denied(role), (role, rule))
        # the PM cleans its own worktrees: whole-tree checkout and stash stay its own call
        self.assertNotIn("Bash(git stash:*)", self.denied("pm"))
        self.assertNotIn("Bash(git checkout -- .)", self.denied("pm"))

    def test_a_worker_may_not_edit_the_files_the_delivery_gate_trusts(self):
        denied = self.denied("worker", "/h/jobs/j/j-w1")
        for tail in ("/*.md", "/j-rv*/**", "/pm/**", "/*.tsv"):
            self.assertIn(oke.edit_rules("/h/jobs/j", tail=tail)[0], denied)
        self.assertFalse(any("j-w1" in r for r in denied))
        self.assertFalse(any("/jobs/" in r for r in self.denied("reviewer", "/h/jobs/j/j-rv")))
        # a job named with -rv must not deny its own worker folder
        denied = self.denied("worker", "/h/jobs/fix-rv/fix-rv-w1")
        self.assertIn(oke.edit_rules("/h/jobs/fix-rv", tail="/fix-rv-rv*/**")[0], denied)
        self.assertFalse(any("/*-rv*" in r for r in denied))

    def test_effort_per_role_is_the_effort_flag(self):
        # the flag takes max too (effortLevel in settings does not), and works without lean
        c = dict(self.config, effort={"reviewer": "max"})
        r = oke.launch_args("reviewer", c, "/h", "/p")
        self.assertEqual(r[r.index("--effort") + 1], "max")
        self.assertNotIn("--effort", oke.launch_args("worker", c, "/h", "/p"))
        self.assertNotIn("--effort", oke.launch_args("reviewer", dict(c, effort={"reviewer": "huge"}), "/h", "/p"))


if __name__ == "__main__":
    unittest.main()


class TestStrayAgentNotes(unittest.TestCase):
    def test_an_agent_report_in_the_checkout_blocks_the_job_change(self):
        # seen live: a worker wrote its report.md in the repository and delivery committed it
        cwd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cwd)
        for rel, text in (("report.md", u"type: done\nseq: 1\non: \n---\n## Summary\n"),
                          ("docs/report.md", u"# Weekly report\ntype: notes\n"), ("board.md", u"type: x\n")):
            os.makedirs(os.path.dirname(os.path.join(cwd, rel)) or cwd, exist_ok=True)
            with io.open(os.path.join(cwd, rel), "w", encoding="utf-8") as f:
                f.write(text)
        files = {"new": ["report.md", "docs/report.md", "board.md", "a.py"], "nested": []}
        self.assertEqual(oke.stray_notes(files, cwd), ["report.md"])
        self.assertIn("agent's report or board", oke.nested_block(files, cwd))
        self.assertIsNone(oke.nested_block({"new": ["docs/report.md"], "nested": []}, cwd))


class TestUserAnswers(SpawnBase):
    def test_a_user_only_decision_needs_a_popup_answer_once_a_mod_counts_them(self):
        # seen live: an orchestrator approved a plan and put the user's files into a job's commit unasked
        self.job("fix-a", "awaiting_approval")
        self.job("fix-b", "awaiting_approval")
        with oke.locked():
            st = oke.load_state()
            st["orchestrator"]["ask_gate"] = True  # the mod's tick turns it on
            oke.save_state(st)
        self.assertEqual(self.cli("job", "phase", "fix-a", "working"), 1)
        self.assertIn("AskUserQuestion", self.err)
        self.assertEqual(self.cli("job", "files", "fix-a", "include", "a.txt"), 1)
        was = oke.whoami
        self.addCleanup(setattr, oke, "whoami", was)
        oke.whoami = lambda state, pane, session=None: (("orchestrator", "o", None, {}), [])
        self.assertEqual(self.cli("answered", "--pane", "w1:p1", "--session", "s1"), 0, self.err)
        self.assertEqual(self.cli("job", "phase", "fix-a", "working"), 0, self.err)
        # one answer: the next job's approval needs its own
        self.assertEqual(self.cli("job", "phase", "fix-b", "working"), 1)
        self.assertEqual(self.cli("answered", "--pane", "w1:p1"), 0, self.err)
        self.assertEqual(self.cli("job", "auto", "fix-b"), 0, self.err)
        self.assertIn(self.cli("job", "phase", "fix-b", "working"), (0, 3), self.err)  # auto: no popup (it may wait for fix-a)
        oke.whoami = lambda state, pane, session=None: (("pm", "p", "k", {}), [])
        self.assertEqual(self.cli("answered", "--pane", "w2:p1"), 0)
        self.assertEqual(oke.load_state()["orchestrator"]["answers"], 2)  # a PM's popup is not the user's
