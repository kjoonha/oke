"""PMs ask each other about cross-repo impact (oke peer), and after: holds a merge."""
import io
import json
import os
import unittest

from test_oke import AG, AGENT, J
from test_slice3 import S3Base
import oke

PANES = {"pm-a": ("w1:p3", "s-a"), "pm-b": ("w2:p3", "s-b"), "pm-c": ("w3:p3", "s-c"),
         "pm-held": ("w4:p3", "s-h"), "pm-done": ("w5:p3", "s-d")}


class PeerBase(S3Base):
    def setUp(self):
        S3Base.setUp(self)
        self.repo_b = self.git_repo("api")
        self.key_b = oke.repo_key(self.repo_b)
        self.job("fix-a", "planning")
        self.other_job("fix-b", self.repo_b, "working")
        s = oke.load_state()
        s["repos"][self.key] = {"pm": {"agent": "pm-a", "pane": "w1:p3", "session": "s-a"}}
        s["repos"][self.key_b] = {"pm": {"agent": "pm-b", "pane": "w2:p3", "session": "s-b"}}
        s["orchestrator"] = {"pane": "w7:p1"}
        oke.save_state(s)
        self.alive = ["pm-a", "pm-b"]

    def other_job(self, name, repo, phase):
        self.assertEqual(self.cli("job", "new", name, "--repo", repo), 0, self.err)
        self.advance(name, phase)

    def add_repo(self, name, pm, job, phase):
        repo = self.git_repo(name)
        self.other_job(job, repo, phase)
        s = oke.load_state()
        s["repos"][oke.repo_key(repo)] = {"pm": {"agent": pm, "pane": PANES[pm][0], "session": PANES[pm][1]}}
        oke.save_state(s)
        return oke.repo_key(repo)

    def as_agent(self, agent, extra=None):
        os.environ["HERDR_PANE_ID"] = PANES[agent][0] if agent in PANES else agent
        agents = [AG(a, pane=PANES[a][0], session=PANES[a][1]) for a in self.alive]
        scenario = {"agent list": [J({"agents": agents})]}
        scenario.update(extra or {})
        self.fake(scenario)

    def texts(self, agent):
        return [t for _, t in oke.inbox_messages(agent)]

    def ask(self, text="auth returns a dict now", by="pm-a", job="fix-a"):
        self.as_agent(by)
        return self.cli("peer", "ask", job, text)

    def answer(self, text="no impact", by="pm-b", job="fix-a"):
        self.as_agent(by)
        return self.cli("peer", "answer", job, text)

    def log(self, job="fix-a"):
        with io.open(os.path.join(self.home, "jobs", job, "peers.md"), encoding="utf-8") as f:
            return f.read()


class TestPeerAsk(PeerBase):
    def test_ask_queues_to_every_other_pm_with_an_open_job(self):
        self.assertEqual(self.ask(), 0, self.err)
        self.assertEqual(json.loads(self.out), {"round": 1, "asked": ["pm-b"]})
        msgs = self.texts("pm-b")
        self.assertEqual(len(msgs), 1)
        self.assertTrue(msgs[0].startswith("pm-a: peer ask (job fix-a, round 1): auth returns a dict now"), msgs[0])
        self.assertIn("oke peer answer fix-a", msgs[0])
        self.assertEqual(list(oke.peer_waiting(oke.load_state(), "fix-a")), ["pm-b"])
        self.assertEqual(self.texts("pm-a"), [])

    def test_ask_includes_a_dead_pm_and_skips_held_and_jobless_repos(self):
        self.add_repo("web", "pm-c", "fix-c", "working")  # pm-c is not alive: still asked
        self.add_repo("held", "pm-held", "fix-h", "working")
        self.add_repo("done", "pm-done", "fix-d", "working")
        s = oke.load_state()
        s["jobs"]["fix-h"]["held"] = True
        s["jobs"]["fix-d"]["phase"] = "done"
        oke.save_state(s)
        self.assertEqual(self.ask(), 0, self.err)
        self.assertEqual(json.loads(self.out)["asked"], ["pm-b", "pm-c"])
        self.assertEqual(self.texts("pm-held"), [])
        self.assertEqual(self.texts("pm-done"), [])

    def test_ask_with_no_other_repo_prints_empty_and_writes_no_waiting(self):
        s = oke.load_state()
        s["jobs"]["fix-b"]["phase"] = "done"
        oke.save_state(s)
        self.assertEqual(self.ask(), 0, self.err)
        self.assertEqual(json.loads(self.out), {"round": 1, "asked": []})
        self.assertEqual(oke.peer_waiting(oke.load_state(), "fix-a"), {})

    def test_ask_from_a_non_pm_is_refused(self):
        self.assertEqual(self.ask(by="w9:p9"), 1)
        self.assertEqual(self.texts("pm-b"), [])

    def test_ask_for_another_repos_job_is_refused(self):
        self.assertEqual(self.ask(by="pm-b"), 1)
        self.assertEqual(self.texts("pm-b"), [])

    def test_a_leading_slash_is_refused(self):
        self.assertEqual(self.ask(text="/tmp/x"), 1)


class TestPeerAnswer(PeerBase):
    def test_answer_reaches_the_asker_and_clears_the_entry(self):
        self.ask()
        self.assertEqual(self.answer(), 0, self.err)
        self.assertEqual(json.loads(self.out), {"waiting": []})
        self.assertEqual(self.texts("pm-a"), ["pm-b: Job fix-a: peer answer from pm-b (round 1): no impact"])
        self.assertEqual(oke.peer_waiting(oke.load_state(), "fix-a"), {})
        log = self.log()
        self.assertIn("- ask (pm-a): auth returns a dict now", log)
        self.assertIn("- answer (pm-b): no impact", log)

    def test_answer_from_a_pm_not_asked_is_refused(self):
        self.ask()
        self.assertEqual(self.answer(by="pm-a"), 1)
        self.assertIn("not asked", self.err)

    def test_new_round_rebuilds_the_list(self):
        self.ask()
        self.answer()
        self.assertEqual(self.ask(text="also the token field"), 0, self.err)
        self.assertEqual(json.loads(self.out), {"round": 2, "asked": ["pm-b"]})
        self.assertEqual(list(oke.peer_waiting(oke.load_state(), "fix-a")), ["pm-b"])
        self.assertIn("## round 2", self.log())

    def test_held_target_drops_out(self):
        self.ask()
        s = oke.load_state()
        s["jobs"]["fix-b"]["held"] = True
        oke.save_state(s)
        self.assertEqual(oke.peer_waiting(oke.load_state(), "fix-a"), {})


class TestPeerWaits(PeerBase):
    def owe(self, job="fix-a", pm="pm-b", key=None, nudged=0):
        oke.write_json_atomic(oke.peers_path(job), {"round": 1, "asker": "pm-a", "waiting": {
            pm: {"repo": key or self.key_b, "nudged": nudged}}})

    def test_asking_pm_waiting_on_peers_gets_no_idle_no_report(self):
        self.pm_job("fix-a", 1)  # handed over, no report yet
        self.owe()
        self.fake({"agent get": [AGENT("idle")]})
        self.assertIsNone(oke.check_pm_once("pm-a", self.key))

    def test_idle_asked_pm_is_reminded_once_then_peer_no_answer(self):
        self.owe()
        self.fake({"agent get": [AGENT("idle")], "agent prompt": [J({"type": "agent_prompted"})]})
        result = oke.wait_loop("pm-b", None, None, poll=0, max_polls=20, jobs_key=self.key_b)
        self.assertEqual(result, "peer_no_answer fix-a pm-b")
        self.assertEqual(oke.read_peers("fix-a")["waiting"]["pm-b"]["nudged"], 2)  # the user was asked
        prompts = [c for c in self.calls() if c[:2] == ["agent", "prompt"]]
        self.assertEqual(len(prompts), 1)
        self.assertIn("oke: Answer the peer ask of job fix-a (round 1): oke peer answer fix-a", prompts[0][3])

    def test_pm_with_mail_is_delivered_before_any_peer_check(self):
        self.owe()
        oke.queue_mail("pm-b", "pm-a", "peer ask (job fix-a, round 1): x")
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(oke.check_pm_once("pm-b", self.key_b), "?deliver")

    def test_two_asks_nudge_names_the_first_owed_job(self):
        self.job("fix-c", "planning")
        self.owe("fix-a")
        self.owe("fix-c")
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(oke.check_pm_once("pm-b", self.key_b), "?peer_idle fix-a")
        self.answer(job="fix-a")
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(oke.check_pm_once("pm-b", self.key_b), "?peer_idle fix-c")
        self.assertEqual(list(oke.peer_waiting(oke.load_state(), "fix-c")), ["pm-b"])


class TestReviewFixes(PeerBase):
    def owe(self, job="fix-a", nudged=0):
        oke.write_json_atomic(oke.peers_path(job), {"round": 1, "asker": "pm-a", "waiting": {
            "pm-b": {"repo": self.key_b, "nudged": nudged}}})

    def test_peer_no_answer_comes_once_and_the_next_owed_job_gets_its_turn(self):
        self.job("fix-c", "planning")
        self.owe("fix-a", nudged=1)
        self.owe("fix-c")
        self.fake({"agent get": [AGENT("idle")], "agent prompt": [J({"type": "agent_prompted"})]})
        self.assertEqual(oke.wait_loop("pm-b", None, None, poll=0, max_polls=20, jobs_key=self.key_b),
                         "peer_no_answer fix-a pm-b")
        self.assertEqual(oke.wait_loop("pm-b", None, None, poll=0, max_polls=20, jobs_key=self.key_b),
                         "peer_no_answer fix-c pm-b")
        self.assertIsNone(oke.wait_loop("pm-b", None, None, poll=0, max_polls=8, jobs_key=self.key_b))
        self.assertEqual(self.answer(), 0, self.err)  # a late answer still counts

    def test_a_vanished_entry_keeps_the_wait_going(self):
        self.assertIsNone(oke.peer_nudge("pm-b", "fix-a"))

    def test_peer_status_shows_who_dropped_out(self):
        self.ask()
        s = oke.load_state()
        s["jobs"]["fix-b"]["held"] = True
        oke.save_state(s)
        self.as_agent("pm-a")
        self.assertEqual(self.cli("peer", "status", "fix-a"), 0, self.err)
        self.assertEqual(json.loads(self.out), {"round": 1, "waiting": [], "dropped": ["pm-b"], "no_answer": []})

    def test_ask_and_answer_refuse_a_closed_job(self):
        self.ask()
        s = oke.load_state()
        s["jobs"]["fix-a"].update(phase="failed", cancelled=True)
        oke.save_state(s)
        self.assertEqual(self.answer(), 1)
        self.assertEqual(self.texts("pm-a"), [])
        self.assertEqual(self.ask(), 1)


class TestReviewFixes2(PeerBase):
    def test_a_pm_the_user_ruled_on_leaves_waiting_but_may_still_answer(self):
        self.ask()
        data = oke.read_peers("fix-a")
        data["waiting"]["pm-b"]["nudged"] = 2
        oke.write_json_atomic(oke.peers_path("fix-a"), data)
        self.assertEqual(oke.peer_waiting(oke.load_state(), "fix-a"), {})
        self.as_agent("pm-a")
        self.cli("peer", "status", "fix-a")
        self.assertEqual(json.loads(self.out), {"round": 1, "waiting": [], "dropped": [], "no_answer": ["pm-b"]})
        self.assertEqual(self.answer(), 0, self.err)

    def test_the_askers_final_report_ends_the_talk(self):
        from test_slice3 import REPORT
        self.ask()
        s = oke.load_state()
        s["jobs"]["fix-b"]["held"] = True  # pm-b drops out, pm-a reports without it
        oke.save_state(s)
        self.pm_job("fix-a", 1, REPORT("done", 1))
        self.fake({"agent get": [AGENT("working")]})
        self.assertEqual(oke.check_pm_once("pm-a", self.key), "report done fix-a")
        s = oke.load_state()
        s["jobs"]["fix-b"]["held"] = False  # back: must not owe the finished talk
        oke.save_state(s)
        self.assertEqual(oke.owed_answers(oke.load_state(), "pm-b"), [])

    def test_verify_pass_does_not_tell_a_job_awaiting_merge_approval(self):
        self.advance("fix-a", "awaiting_merge")
        self.put(os.path.join(self.home, "jobs", "fix-a", "plan.md"), "job: fix-a\nafter: fix-b\n---\n")
        self.advance("fix-b", "merging")
        oke.ledger_add("fix-b", "oke", "abc1234", "merge", "pass")
        self.fake({"agent list": [J({"agents": []})]})
        self.assertEqual(self.cli("job", "verify", "fix-b", "--accept", "user checked"), 0, self.err)
        self.assertEqual(self.texts("pm-a"), [])


class TestReviewFixes3(PeerBase):
    def test_a_question_mid_talk_keeps_the_talk_open(self):
        from test_slice3 import REPORT
        self.add_repo("web", "pm-c", "fix-c", "working")
        self.alive.append("pm-c")
        self.ask()
        self.answer("impact: we disagree")
        self.pm_job("fix-a", 1, REPORT("question", 1))
        self.fake({"agent get": [AGENT("working")]})
        self.assertEqual(oke.check_pm_once("pm-a", self.key), "report question fix-a")
        self.assertEqual(list(oke.peer_waiting(oke.load_state(), "fix-a")), ["pm-c"])

    def test_after_the_plan_report_a_late_answer_is_logged_and_mailed(self):
        from test_slice3 import REPORT
        self.ask()
        self.pm_job("fix-a", 1, REPORT("done", 1))
        self.fake({"agent get": [AGENT("working")]})
        self.assertEqual(oke.check_pm_once("pm-a", self.key), "report done fix-a")
        self.assertEqual(oke.peer_waiting(oke.load_state(), "fix-a"), {})
        self.assertEqual(self.answer("impact: late"), 0, self.err)
        self.assertIn("- answer (pm-b): impact: late", self.log())
        self.assertIn("pm-b: Job fix-a: peer answer from pm-b (round 1): impact: late", self.texts("pm-a"))

    def test_asker_gets_no_idle_no_report_while_the_user_decides(self):
        self.pm_job("fix-a", 1)
        oke.write_json_atomic(oke.peers_path("fix-a"), {"round": 1, "asker": "pm-a", "waiting": {
            "pm-b": {"repo": self.key_b, "nudged": 1}}})
        self.assertFalse(oke.peer_nudge("pm-b", "fix-a"))  # the user is asked now
        self.fake({"agent get": [AGENT("idle")]})
        self.assertIsNone(oke.check_pm_once("pm-a", self.key))
        self.pm_job("fix-a", 2)  # the orchestrator's kind: answer: the PM owes a report again
        self.assertEqual(oke.check_pm_once("pm-a", self.key), "?idle_no_report fix-a")


class TestAfter(PeerBase):
    def setUp(self):
        PeerBase.setUp(self)
        self.advance("fix-a", "merging")
        self.put(os.path.join(self.home, "jobs", "fix-a", "plan.md"), "job: fix-a\nafter: fix-b\n---\n## 접근\n")

    def merge(self):
        self.cli("job", "merge", "fix-a")
        return self.err

    def verified(self, job="fix-b"):
        oke.ledger_add(job, "pm", "abc1234", "merge", "pass")
        oke.ledger_add(job, "pm", "abc1234", "verify", "pass")

    def test_merge_waits_for_the_after_job(self):
        self.assertIn("waiting for fix-b to merge", self.merge())

    def test_merge_passes_once_the_after_job_is_verified(self):
        self.verified()
        self.assertNotIn("fix-b", self.merge())

    def test_merge_refuses_a_cancelled_after_job(self):
        s = oke.load_state()
        s["jobs"]["fix-b"].update(phase="failed", cancelled=True)
        oke.save_state(s)
        self.assertIn("after fix-b was cancelled: ask the user", self.merge())

    def test_archived_after_job_counts_by_its_archived_ledger(self):
        self.verified()
        src = os.path.join(self.home, "jobs", "fix-b")
        dst = os.path.join(self.home, "archive", "fix-b-20261120-101010")
        os.makedirs(os.path.dirname(dst))
        os.rename(src, dst)
        s = oke.load_state()
        del s["jobs"]["fix-b"]
        oke.save_state(s)
        self.assertNotIn("fix-b", self.merge())
        os.remove(os.path.join(dst, "ledger.tsv"))
        self.assertIn("after fix-b was archived without a verified merge: ask the user", self.merge())

    def test_cancel_notifies_the_pm_of_a_job_waiting_on_it(self):
        self.fake({"agent list": [J({"agents": []})]})
        self.assertEqual(self.cli("job", "cancel", "fix-b"), 0, self.err)
        self.assertIn("oke: Job fix-a: after-job fix-b was cancelled. Report question to the user.",
                      self.texts("pm-a"))

    def test_after_itself_is_ignored_and_an_unknown_name_says_so(self):
        self.put(os.path.join(self.home, "jobs", "fix-a", "plan.md"), "job: fix-a\nafter: fix-a, nope\n---\n")
        err = self.merge()
        self.assertIn("after nope is no known job: fix the plan", err)
        self.assertNotIn("waiting for fix-a", err)

    def test_verify_pass_does_not_tell_a_job_still_working(self):
        s = oke.load_state()
        s["jobs"]["fix-a"]["phase"] = "working"
        oke.save_state(s)
        self.advance("fix-b", "merging")
        oke.ledger_add("fix-b", "oke", "abc1234", "merge", "pass")
        self.fake({"agent list": [J({"agents": []})]})
        self.assertEqual(self.cli("job", "verify", "fix-b", "--accept", "user checked"), 0, self.err)
        self.assertEqual(self.texts("pm-a"), [])

    def test_verify_pass_tells_the_pm_of_a_job_waiting_on_it(self):
        self.advance("fix-b", "merging")
        oke.ledger_add("fix-b", "oke", "abc1234", "merge", "pass")
        self.fake({"agent list": [J({"agents": []})]})
        self.assertEqual(self.cli("job", "verify", "fix-b", "--accept", "user checked"), 0, self.err)
        self.assertIn("oke: Job fix-a: after-job fix-b is merged and verified. Run the merge again.",
                      self.texts("pm-a"))


class TestBoard(PeerBase):
    def jobs(self):
        tree = oke.board_tree(oke.load_state(), [])
        return dict((j["job"], j) for repo in tree for j in repo["jobs"])

    def test_board_lists_peer_and_after_waits(self):
        oke.write_json_atomic(oke.peers_path("fix-a"), {"round": 1, "asker": "pm-a", "waiting": {
            "pm-b": {"repo": self.key_b, "nudged": 0}}})
        self.put(os.path.join(self.home, "jobs", "fix-a", "plan.md"), "job: fix-a\nafter: fix-b\n---\n")
        job = self.jobs()["fix-a"]
        self.assertEqual(job["peer_wait"], [oke.repo_name(self.key_b)])
        self.assertEqual(job["after_wait"], [])  # planning: not at the merge yet
        self.advance("fix-a", "merging")
        self.assertEqual(self.jobs()["fix-a"]["after_wait"], ["fix-b"])
        lines = "\n".join(t for _, t in oke.board_lines(oke.board_tree(oke.load_state(), [])))
        self.assertIn("waiting for PM %s" % oke.repo_name(self.key_b), lines)
        self.assertIn("waiting for fix-b to merge", lines)


if __name__ == "__main__":
    unittest.main()
