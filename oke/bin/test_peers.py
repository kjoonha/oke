"""PMs ask each other about cross-repo impact (oke peer)."""
import io
import json
import os
import unittest

from test_oke import put_pm, AG, AGENT, J
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
        put_pm(s, self.key, {"agent": "pm-a", "pane": "w1:p3", "session": "s-a"})
        put_pm(s, self.key_b, {"agent": "pm-b", "pane": "w2:p3", "session": "s-b"})
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
        put_pm(s, oke.repo_key(repo), {"agent": pm, "pane": PANES[pm][0], "session": PANES[pm][1]})
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
            pm: {"pm": key or pm, "nudged": nudged}}})

    def test_asking_pm_waiting_on_peers_gets_no_idle_no_report(self):
        self.pm_job("fix-a", 1)  # handed over, no report yet
        self.owe()
        self.fake({"agent get": [AGENT("idle")]})
        self.assertIsNone(oke.check_pm_once("pm-a", "pm-a"))

    def test_idle_asked_pm_is_reminded_once_then_peer_no_answer(self):
        self.owe()
        self.fake({"agent get": [AGENT("idle")], "agent prompt": [J({"type": "agent_prompted"})]})
        result = oke.wait_loop("pm-b", None, None, poll=0, max_polls=20, jobs_key="pm-b")
        self.assertEqual(result, "peer_no_answer fix-a pm-b")
        self.assertEqual(oke.read_peers("fix-a")["waiting"]["pm-b"]["nudged"], 2)  # the user was asked
        prompts = [c for c in self.calls() if c[:2] == ["agent", "prompt"]]
        self.assertEqual(len(prompts), 1)
        self.assertIn("oke: Answer the peer ask of job fix-a (round 1): oke peer answer fix-a", prompts[0][3])

    def test_pm_with_mail_is_delivered_before_any_peer_check(self):
        self.owe()
        oke.queue_mail("pm-b", "pm-a", "peer ask (job fix-a, round 1): x")
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(oke.check_pm_once("pm-b", "pm-b"), "?deliver")

    def test_two_asks_nudge_names_the_first_owed_job(self):
        self.job("fix-c", "planning")
        self.owe("fix-a")
        self.owe("fix-c")
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(oke.check_pm_once("pm-b", "pm-b"), "?peer_idle fix-a")
        self.answer(job="fix-a")
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(oke.check_pm_once("pm-b", "pm-b"), "?peer_idle fix-c")
        self.assertEqual(list(oke.peer_waiting(oke.load_state(), "fix-c")), ["pm-b"])


class TestReviewFixes(PeerBase):
    def owe(self, job="fix-a", nudged=0):
        oke.write_json_atomic(oke.peers_path(job), {"round": 1, "asker": "pm-a", "waiting": {
            "pm-b": {"pm": "pm-b", "nudged": nudged}}})

    def test_peer_no_answer_comes_once_and_the_next_owed_job_gets_its_turn(self):
        self.job("fix-c", "planning")
        self.owe("fix-a", nudged=1)
        self.owe("fix-c")
        self.fake({"agent get": [AGENT("idle")], "agent prompt": [J({"type": "agent_prompted"})]})
        self.assertEqual(oke.wait_loop("pm-b", None, None, poll=0, max_polls=20, jobs_key="pm-b"),
                         "peer_no_answer fix-a pm-b")
        self.assertEqual(oke.wait_loop("pm-b", None, None, poll=0, max_polls=20, jobs_key="pm-b"),
                         "peer_no_answer fix-c pm-b")
        self.assertIsNone(oke.wait_loop("pm-b", None, None, poll=0, max_polls=8, jobs_key="pm-b"))
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
        self.assertEqual(json.loads(self.out), {"round": 1, "waiting": [], "ruling": [], "dropped": ["pm-b"], "no_answer": []})

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
        self.assertEqual(json.loads(self.out), {"round": 1, "waiting": [], "ruling": [], "dropped": [], "no_answer": ["pm-b"]})
        self.assertEqual(self.answer(), 0, self.err)

    def test_the_askers_final_report_ends_the_talk(self):
        from test_slice3 import REPORT
        self.ask()
        s = oke.load_state()
        s["jobs"]["fix-b"]["held"] = True  # pm-b drops out, pm-a reports without it
        oke.save_state(s)
        self.pm_job("fix-a", 1, REPORT("done", 1))
        self.fake({"agent get": [AGENT("working")]})
        self.assertEqual(oke.check_pm_once("pm-a", "pm-a"), "report done fix-a")
        s = oke.load_state()
        s["jobs"]["fix-b"]["held"] = False  # back: must not owe the finished talk
        oke.save_state(s)
        self.assertEqual(oke.owed_answers(oke.load_state(), "pm-b"), [])


class TestReviewFixes3(PeerBase):
    def test_a_question_mid_talk_keeps_the_talk_open(self):
        from test_slice3 import REPORT
        self.add_repo("web", "pm-c", "fix-c", "working")
        self.alive.append("pm-c")
        self.ask()
        self.answer("impact: we disagree")
        self.pm_job("fix-a", 1, REPORT("question", 1))
        self.fake({"agent get": [AGENT("working")]})
        self.assertEqual(oke.check_pm_once("pm-a", "pm-a"), "report question fix-a")
        self.assertEqual(list(oke.peer_waiting(oke.load_state(), "fix-a")), ["pm-c"])

    def test_after_the_plan_report_a_late_answer_is_logged_and_mailed(self):
        from test_slice3 import REPORT
        self.ask()
        self.pm_job("fix-a", 1, REPORT("done", 1))
        self.fake({"agent get": [AGENT("working")]})
        self.assertEqual(oke.check_pm_once("pm-a", "pm-a"), "report done fix-a")
        self.assertEqual(oke.peer_waiting(oke.load_state(), "fix-a"), {})
        self.assertEqual(self.answer("impact: late"), 0, self.err)
        self.assertIn("- answer (pm-b): impact: late", self.log())
        self.assertIn("pm-b: Job fix-a: peer answer from pm-b (round 1): impact: late", self.texts("pm-a"))

    def test_asker_gets_no_idle_no_report_while_the_user_decides(self):
        self.pm_job("fix-a", 1)
        oke.write_json_atomic(oke.peers_path("fix-a"), {"round": 1, "asker": "pm-a", "waiting": {
            "pm-b": {"pm": "pm-b", "nudged": 1}}})
        self.assertFalse(oke.peer_nudge("pm-b", "fix-a"))  # the user is asked now
        self.fake({"agent get": [AGENT("idle")]})
        self.assertIsNone(oke.check_pm_once("pm-a", "pm-a"))
        self.pm_job("fix-a", 2)  # the orchestrator's kind: answer: the PM owes a report again
        self.assertEqual(oke.check_pm_once("pm-a", "pm-a"), "?idle_no_report fix-a")


class TestReviewFixes4(PeerBase):
    def test_a_missing_answer_the_user_must_rule_on_stays_in_pending(self):
        self.pm_job("fix-a", 1)
        oke.write_json_atomic(oke.peers_path("fix-a"), {"round": 1, "asker": "pm-a", "waiting": {
            "pm-b": {"pm": "pm-b", "nudged": 1}}})
        oke.peer_nudge("pm-b", "fix-a")
        item = oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"])
        self.assertEqual(item["kind"], "peer")
        self.pm_job("fix-a", 2)  # the orchestrator gave the PM the user's choice
        self.assertIsNone(oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"]))


class TestReviewFixes5(PeerBase):
    def setUp(self):
        PeerBase.setUp(self)
        self.add_repo("web", "pm-c", "fix-c", "working")
        self.pm_job("fix-a", 1)
        oke.write_json_atomic(oke.peers_path("fix-a"), {"round": 1, "asker": "pm-a", "waiting": {
            "pm-b": {"pm": "pm-b", "nudged": 1},
            "pm-c": {"pm": "pm-c", "nudged": 1}}})

    def item(self):
        return oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"])

    def test_a_second_ruling_is_a_new_item_after_a_deferral(self):
        oke.peer_nudge("pm-b", "fix-a")
        first = self.item()
        self.assertEqual(first["peers"], ["pm-b"])
        self.as_agent("pm-a")
        self.assertEqual(self.cli("job", "defer", "fix-a"), 0, self.err)
        oke.peer_nudge("pm-c", "fix-a")
        second = self.item()
        self.assertEqual(second["peers"], ["pm-b", "pm-c"])
        self.assertFalse(second["deferred"])

    def test_a_question_item_also_names_the_pms_to_rule_on(self):
        from test_slice3 import REPORT
        oke.peer_nudge("pm-c", "fix-a")
        self.pm_job("fix-a", 1, REPORT("question", 1))
        item = self.item()
        self.assertEqual((item["kind"], item["peers"]), ("question", ["pm-c"]))

    def test_status_keeps_a_pm_being_ruled_on_out_of_no_answer(self):
        oke.peer_nudge("pm-c", "fix-a")
        self.as_agent("pm-a")
        self.cli("peer", "status", "fix-a")
        out = json.loads(self.out)
        self.assertEqual(out["ruling"], ["pm-c"])
        self.assertEqual(out["no_answer"], [])
        self.assertEqual(out["waiting"], ["pm-b"])


class TestBoard(PeerBase):
    def jobs(self):
        tree = oke.board_tree(oke.load_state(), [])
        return dict((j["job"], j) for repo in tree for j in repo["jobs"])

    def test_board_lists_peer_waits(self):
        oke.write_json_atomic(oke.peers_path("fix-a"), {"round": 1, "asker": "pm-a", "waiting": {
            "pm-b": {"pm": "pm-b", "nudged": 0}}})
        job = self.jobs()["fix-a"]
        self.assertEqual(job["peer_wait"], ["pm-b"])
        self.assertNotIn("after_wait", job)
        self.assertEqual(job["deliver"], "none")
        lines = "\n".join(t for _, t in oke.board_lines(oke.board_tree(oke.load_state(), [])))
        self.assertIn("waiting for PM pm-b", lines)


class TestPhaseByAgent(PeerBase):
    def test_only_the_orchestrator_sets_a_phase(self):
        self.as_agent("pm-a")
        self.assertEqual(self.cli("job", "phase", "fix-a", "awaiting_approval"), 1)
        self.assertIn("only the orchestrator sets a job's phase", self.err)
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["phase"], "planning")
        self.as_agent("w7:p1")  # the orchestrator's pane
        self.cli("job", "phase", "fix-a", "awaiting_approval")
        self.assertNotIn("only the orchestrator", self.err)


if __name__ == "__main__":
    unittest.main()
