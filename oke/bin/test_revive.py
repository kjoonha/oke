"""Slice 2: revive, tick, api-retry, locks. Shares the fake-herdr harness of test_oke."""
import io
import json
import os
import time
import unittest

from test_oke import AG, AGENT, E, J, OK, SpawnBase, help_ok
import oke
import runtime_herdr


def PINFO(*names):
    return J({"process_info": {"foreground_processes": [{"name": n, "argv": [n]} for n in names]}})


def PANE_OK(pane="w1:p5"):
    return J({"pane": {"pane_id": pane}})


def started(argv):
    return [c for c in argv if c[:2] == ["agent", "start"]]


class ReviveBase(SpawnBase):
    def setUp(self):
        SpawnBase.setUp(self)
        os.environ["HERDR_ENV"] = "1"
        self.key = oke.repo_key(self.repo)
        self.job("fix-a", "working")
        s = oke.load_state()
        s["repos"][self.key] = {"pm": {"agent": "pm-toy", "pane": "w1:p3", "session": "s-pm", "seq": 1}}
        s["orchestrator"] = {"pane": "w7:p1", "session": "s-o", "cwd": self.repo,
                             "argv": ["--plugin-dir", "X", "--resume", "old", "--model", "opus"]}
        oke.save_state(s)
        os.makedirs(oke.pm_dir(self.key))
        self.write_pm_assignment()
        self.child_dir = os.path.join(self.home, "jobs", "fix-a", "fix-a-w1")
        os.makedirs(self.child_dir)
        self.write_file(os.path.join(self.child_dir, "assignment.md"),
                        "agent: fix-a-w1\nrole: worker\ncwd: %s\nseq: 2\njob: fix-a\n---\n" % self.repo)
        oke.write_json_atomic(oke.children_path(self.key), {"fix-a-w1": {
            "agent": "fix-a-w1", "pane": "w1:p5", "session": "s-w", "seq": 2, "terminal": "term-5",
            "role": "worker", "job": "fix-a"}})

    def write_pm_assignment(self):
        self.write_file(os.path.join(oke.pm_dir(self.key), "assignment.md"),
                        "agent: pm-toy\nrole: pm\ncwd: %s\nseq: 1\n---\n" % self.repo)

    def write_file(self, path, text):
        with io.open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def child(self):
        return oke.read_children(self.key)["fix-a-w1"]

    def events(self):
        p = os.path.join(self.home, "events.log")
        return io.open(p, encoding="utf-8").read() if os.path.exists(p) else ""

    def revive_scenario(self, **over):
        s = {"agent list": [J({"agents": []})],
             "pane get": [J({"pane": {"pane_id": "w1:p5", "terminal_id": "term-5"}})],
             "pane process-info": [PINFO("bash")],
             "pane split": [J({"pane": {"pane_id": "w1:p8"}})],
             "pane close": [J({"type": "ok"})],
             "agent start": [AGENT()],
             "agent prompt": [J({"type": "agent_prompted"})],
             "agent get": [AGENT("working", name="fix-a-w1", pane="w1:p5", session="s-w2")]}
        s.update(over)
        self.fake(s)


class TestRevive(ReviveBase):
    def test_closes_the_old_pane_and_starts_next_to_the_parent_with_resume(self):
        # live: a force-killed claude leaves terminal modes set and the next start command
        # typed into that shell arrives garbled, so the old pane is never reused
        self.revive_scenario()
        self.assertEqual(oke.revive("fix-a-w1"), "revived")
        self.assertEqual(self.argv_of("pane", "close"), [["pane", "close", "w1:p5"]])
        self.assertIn("w1:p3", self.argv_of("pane", "split")[0])
        start = started(self.calls())
        self.assertEqual(len(start), 1)
        self.assertIn("w1:p8", start[0])
        self.assertEqual(start[0][start[0].index("--resume") + 1], "s-w")
        prompt = self.argv_of("agent", "prompt")[0]
        self.assertIn("restarted", prompt[3])
        self.assertIn("fix-a-w1/assignment.md", prompt[3])
        rec = self.child()
        self.assertEqual((rec["revives"], rec["session"]), (1, "s-w2"))
        self.assertIn("revived fix-a-w1", self.events())

    def test_a_reused_pane_id_is_not_closed(self):
        # after a herdr restart the recorded pane id can be a user's new shell
        children = oke.read_children(self.key)
        children["fix-a-w1"]["terminal"] = "term-old"
        oke.write_json_atomic(oke.children_path(self.key), children)
        self.revive_scenario(**{"pane get": [J({"pane": {"pane_id": "w1:p5", "terminal_id": "term-user"}})]})
        self.assertEqual(oke.revive("fix-a-w1"), "revived")
        self.assertEqual(self.argv_of("pane", "close"), [])

    def test_a_pane_with_no_recorded_terminal_is_not_closed(self):
        children = oke.read_children(self.key)
        children["fix-a-w1"].pop("terminal", None)
        oke.write_json_atomic(oke.children_path(self.key), children)
        self.revive_scenario()
        self.assertEqual(oke.revive("fix-a-w1"), "revived")
        self.assertEqual(self.argv_of("pane", "close"), [])

    def test_a_hold_while_a_worker_revives_closes_it(self):
        # the hold's close_children saw only the old dead pane
        start = runtime_herdr.start_agent

        def hold_then_start(*a, **k):
            s = oke.load_state()
            s["jobs"]["fix-a"]["held"] = "later"
            oke.save_state(s)
            return start(*a, **k)
        runtime_herdr.start_agent = hold_then_start
        self.addCleanup(setattr, runtime_herdr, "start_agent", start)
        self.revive_scenario()
        self.assertEqual(oke.revive("fix-a-w1"), "not_needed")
        self.assertEqual(len(self.argv_of("pane", "close")), 2)  # the old pane, then the new one
        self.assertIsNone(self.child()["pane"])

    def test_a_child_the_hold_closed_revives_after_unhold(self):
        # the PM revives it on "back from hold"
        self.git(self.repo, "branch", "oke/fix-a")
        self.revive_scenario()
        self.assertEqual(self.cli("job", "hold", "fix-a"), 0, self.err)
        self.assertIsNone(self.child()["pane"])
        self.assertEqual(oke.revive("fix-a-w1"), "not_needed")
        self.assertEqual(self.cli("job", "unhold", "fix-a"), 0, self.err)
        self.assertEqual(oke.revive("fix-a-w1"), "revived")
        self.assertIsNotNone(self.child()["pane"])
        # the hold may have closed a herdr worktree's workspace with it: the new pane opens
        # next to the PM, in the assignment's cwd (the worktree path), not in that workspace
        split = self.argv_of("pane", "split")[-1]
        self.assertEqual(split[split.index("--pane") + 1], "w1:p3")
        cwd = oke.read_header(os.path.join(oke.oke_home(), "jobs", "fix-a", "fix-a-w1", "assignment.md"))["cwd"]
        self.assertEqual(split[split.index("--cwd") + 1], oke.native_path(cwd))

    def test_revive_records_the_new_terminal(self):
        self.revive_scenario()
        oke.revive("fix-a-w1")
        self.assertEqual(self.child()["terminal"], "term-5")

    def test_revives_are_counted_before_the_start(self):
        # a watcher killed mid-start must not leave the count where it was
        seen = []
        original = oke.relaunch

        def spy(*a):
            seen.append(oke.read_children(self.key)["fix-a-w1"]["revives"])
            return original(*a)
        oke.relaunch = spy
        try:
            self.revive_scenario()
            oke.revive("fix-a-w1")
        finally:
            oke.relaunch = original
        self.assertEqual(seen, [1])

    def test_a_record_gone_under_the_lock_is_unknown(self):
        self.revive_scenario()
        original = oke.find_target
        calls = []

        def vanishing(state, name):
            calls.append(name)
            return original(state, name) if len(calls) == 1 else None
        oke.find_target = vanishing
        try:
            self.assertEqual(oke.revive("fix-a-w1"), "unknown")
        finally:
            oke.find_target = original

    def test_a_pane_that_refuses_to_close_is_left(self):
        self.revive_scenario(**{"pane close": [E("confirmation_required")]})
        self.assertEqual(oke.revive("fix-a-w1"), "revived")
        self.assertIn("w1:p8", started(self.calls())[0])

    def test_the_orchestrator_pane_is_never_closed(self):
        self.revive_scenario(**{
                                "agent get": [AGENT("working", name="oke-orch", pane="w1:p8", session="s-o2")]})
        self.assertEqual(oke.revive("orchestrator"), "revived")
        self.assertEqual(self.argv_of("pane", "close"), [])

    def test_final_report_means_not_needed(self):
        # a worker closed after merge has its done report
        self.write_file(os.path.join(self.child_dir, "report.md"), "type: done\nseq: 2\n---\n")
        self.revive_scenario()
        self.assertEqual(oke.revive("fix-a-w1"), "not_needed")
        self.assertEqual(started(self.calls()), [])

    def test_old_seq_final_report_still_revives(self):
        self.write_file(os.path.join(self.child_dir, "report.md"), "type: done\nseq: 1\n---\n")
        self.revive_scenario()
        self.assertEqual(oke.revive("fix-a-w1"), "revived")

    def test_finished_job_means_not_needed(self):
        self.advance("fix-a", "done")
        self.revive_scenario()
        self.assertEqual(oke.revive("fix-a-w1"), "not_needed")
        self.assertEqual(started(self.calls()), [])

    def test_busy_when_another_watcher_holds_the_lock(self):
        self.assertTrue(oke.try_lock("revive-fix-a-w1"))
        self.revive_scenario()
        self.assertEqual(oke.revive("fix-a-w1"), "busy")
        self.assertEqual(self.calls(), [])

    def test_stale_lock_is_taken_over(self):
        self.assertTrue(oke.try_lock("revive-fix-a-w1"))
        old = time.time() - oke.LOCK_STALE - 5
        os.utime(oke.lock_path("revive-fix-a-w1"), (old, old))
        self.revive_scenario()
        self.assertEqual(oke.revive("fix-a-w1"), "revived")

    def test_lock_released_after_revive(self):
        self.revive_scenario()
        oke.revive("fix-a-w1")
        self.assertTrue(oke.try_lock("revive-fix-a-w1"))

    def test_listed_agent_is_alive(self):
        self.revive_scenario(**{"agent list": [J({"agents": [AG("fix-a-w1", pane="w1:p5")]})]})
        self.assertEqual(oke.revive("fix-a-w1"), "alive")
        self.assertEqual(started(self.calls()), [])

    def test_another_claude_in_the_recorded_pane_is_not_this_agent(self):
        # a pane id alone is no identity: oke wait would otherwise loop on "alive" forever
        self.revive_scenario(**{"agent list": [J({"agents": [AG(None, pane="w1:p5", session=None)]})]})
        self.assertEqual(oke.revive("fix-a-w1"), "revived")

    def test_an_asked_revive_has_no_limit(self):
        children = oke.read_children(self.key)
        children["fix-a-w1"].update(revives=3, report_mtime=None)
        oke.write_json_atomic(oke.children_path(self.key), children)
        self.revive_scenario()
        self.assertEqual(oke.revive("fix-a-w1"), "revived")

    def test_an_automatic_resume_is_never_repeated_without_a_report(self):
        # autonomy full: a revive that failed or died at once goes to the user
        self.revive_scenario()
        self.assertEqual(oke.revive("fix-a-w1", auto=True), "revived")
        self.assertTrue(self.child()["auto_revived"])
        self.assertEqual(oke.revive("fix-a-w1", auto=True), "ask")
        self.assertEqual(oke.revive("fix-a-w1"), "revived")  # the user said resume
        self.assertFalse(self.child()["auto_revived"])
        self.assertEqual(oke.revive("fix-a-w1", auto=True), "revived")

    def test_a_revived_agent_gets_the_mail_queued_while_it_was_down(self):
        # seen in an e2e: the mail stayed in the inbox and the PM worked from the older assignment
        oke.queue_mail("fix-a-w1", "pm", "use the new plan")
        self.revive_scenario()
        self.assertEqual(oke.revive("fix-a-w1"), "revived")
        self.assertIn("OKe inbox: pm: use the new plan", json.dumps(self.calls()))
        self.assertEqual(oke.inbox_messages("fix-a-w1"), [])

    def test_mail_a_deliver_holds_is_left_to_it(self):
        oke.queue_mail("fix-a-w1", "pm", "use the new plan")
        self.assertTrue(oke.try_lock("deliver-fix-a-w1"))
        try:
            self.revive_scenario()
            self.assertEqual(oke.revive("fix-a-w1"), "revived")
        finally:
            oke.unlock("deliver-fix-a-w1")
        self.assertNotIn("use the new plan", json.dumps(self.calls()))
        self.assertEqual(len(oke.inbox_messages("fix-a-w1")), 1)

    def test_only_mail_that_fits_goes_with_the_start(self):
        # a Windows command line holds about 32k characters
        for i in range(3):
            oke.queue_mail("fix-a-w1", "pm", ("m%d " % i) + "x" * 7000)
        self.revive_scenario()
        self.assertEqual(oke.revive("fix-a-w1"), "revived")
        self.assertIn("m1 x", json.dumps(self.calls()))
        self.assertNotIn("m2 x", json.dumps(self.calls()))
        self.assertEqual([t[:7] for _, t in oke.inbox_messages("fix-a-w1")], ["pm: m2 "])  # left to deliver

    def test_mail_stays_queued_when_the_revive_fails(self):
        oke.queue_mail("fix-a-w1", "pm", "use the new plan")
        original = oke.relaunch

        def broken(*a):
            raise runtime_herdr.HerdrError("agent_not_ready", "no")
        self.revive_scenario()
        self.assertEqual(len(oke.inbox_messages("fix-a-w1")), 1)
        oke.relaunch = broken
        try:
            self.assertEqual(oke.revive("fix-a-w1"), "failed")
        finally:
            oke.relaunch = original
        self.assertEqual(len(oke.inbox_messages("fix-a-w1")), 1)

    def test_an_automatic_resume_lost_to_an_outage_does_not_count(self):
        original = oke.relaunch

        def down(*a):
            raise runtime_herdr.HerdrError("herdr_unavailable", "down")
        oke.relaunch = down
        try:
            self.revive_scenario()
            with self.assertRaises(runtime_herdr.HerdrError):
                oke.revive("fix-a-w1", auto=True)
        finally:
            oke.relaunch = original
        self.assertFalse(self.child().get("auto_revived"))
        self.assertEqual(oke.revive("fix-a-w1", auto=True), "revived")  # not refused as a second one

    def test_a_report_after_an_automatic_resume_allows_the_next(self):
        self.revive_scenario()
        self.assertEqual(oke.revive("fix-a-w1", auto=True), "revived")
        self.write_file(os.path.join(self.child_dir, "report.md"), "type: waiting\nseq: 2\n---\n")
        os.utime(os.path.join(self.child_dir, "report.md"), (1e9, 1e9))
        self.assertEqual(oke.revive("fix-a-w1", auto=True), "revived")

    def test_new_report_resets_the_death_count(self):
        self.write_file(os.path.join(self.child_dir, "report.md"), "type: waiting\nseq: 2\n---\n")
        children = oke.read_children(self.key)
        children["fix-a-w1"].update(revives=3, report_mtime=1.0)
        oke.write_json_atomic(oke.children_path(self.key), children)
        self.revive_scenario()
        self.assertEqual(oke.revive("fix-a-w1"), "revived")
        self.assertEqual(self.child()["revives"], 1)

    def test_splits_from_the_pm_pane_when_the_old_pane_is_gone(self):
        self.revive_scenario(**{"pane get": [E("pane_not_found")], "pane process-info": [E("pane_not_found")]})
        self.assertEqual(oke.revive("fix-a-w1"), "revived")
        split = self.argv_of("pane", "split")[0]
        self.assertIn("w1:p3", split)
        self.assertIn("w1:p8", started(self.calls())[0])

    def test_new_workspace_when_no_parent_pane_can_split(self):
        self.revive_scenario(**{
            "pane get": [E("pane_not_found")], "pane process-info": [E("pane_not_found")],
            "pane split": [E("pane_not_found")],
            "workspace create": [J({"root_pane": {"pane_id": "wN:p1"}, "workspace": {"workspace_id": "wN"}})]})
        self.assertEqual(oke.revive("fix-a-w1"), "revived")
        self.assertIn("wN:p1", started(self.calls())[0])

    def test_strip_resume(self):
        self.assertEqual(oke.strip_resume(["-r", "--model", "x", "--resume", "id", "-c", "--resume=z", "a"]),
                         ["--model", "x", "a"])

    def test_failed_start_counts_and_logs(self):
        self.revive_scenario(**{"agent start": [E("agent_start_failed")]})
        self.assertEqual(oke.revive("fix-a-w1"), "failed")
        self.assertEqual(self.child()["revives"], 1)
        self.assertIn("revive_failed fix-a-w1", self.events())
        self.assertTrue(oke.try_lock("revive-fix-a-w1"))

    def test_failed_start_closes_its_new_pane(self):
        # live: a timed-out start left the name registered on a dead shell, which a
        # watcher would read as alive
        self.revive_scenario(**{"agent start": [E("timeout")]})
        self.assertEqual(oke.revive("fix-a-w1"), "failed")
        self.assertIn(["pane", "close", "w1:p8"], self.calls())

    def test_reset_lets_the_user_resume_after_give_up(self):
        children = oke.read_children(self.key)
        children["fix-a-w1"].update(revives=3, report_mtime=None, gave_up=True)
        oke.write_json_atomic(oke.children_path(self.key), children)
        self.revive_scenario()
        self.assertEqual(self.cli("revive", "fix-a-w1", "--reset"), 0)
        self.assertEqual(self.out.strip(), "revived")
        self.assertEqual(self.child()["revives"], 1)

    def test_unknown_agent(self):
        self.revive_scenario()
        self.assertEqual(oke.revive("nobody"), "unknown")
        self.assertEqual(self.calls(), [])

    def test_revive_removes_a_stale_api_retry_file(self):
        self.write_file(os.path.join(self.child_dir, "api-retry.json"), "{}")
        self.revive_scenario()
        oke.revive("fix-a-w1")
        self.assertFalse(os.path.exists(os.path.join(self.child_dir, "api-retry.json")))

    def test_pm_revives_with_its_launch_args(self):
        self.revive_scenario(**{"pane get": [PANE_OK("w1:p3")],
                                "agent get": [AGENT("working", name="pm-toy", pane="w1:p3", session="s-pm")]})
        self.assertEqual(oke.revive("pm-toy"), "revived")
        start = started(self.calls())[0]
        self.assertEqual(start[2], "pm-toy")
        self.assertEqual(start[start.index("--resume") + 1], "s-pm")
        self.assertEqual(start[start.index("--model") + 1], "opus")
        self.assertIn("pm/assignment.md", self.argv_of("agent", "prompt")[0][3])
        self.assertEqual(oke.load_state()["repos"][self.key]["pm"]["revives"], 1)

    def test_pm_not_needed_without_open_job(self):
        self.assertEqual(self.cli("job", "phase", "fix-a", "failed"), 0)
        self.revive_scenario()
        self.assertEqual(oke.revive("pm-toy"), "not_needed")

    def test_orchestrator_revives_from_its_recorded_argv(self):
        self.revive_scenario(**{"pane get": [E("pane_not_found")], "pane process-info": [E("pane_not_found")],
                                "agent get": [AGENT("working", name="oke-orch", pane="w1:p8", session="s-o2")]})
        self.assertEqual(oke.revive("orchestrator"), "revived")
        start = started(self.calls())[0]
        self.assertEqual(start[2], "oke-orch")
        self.assertEqual(start[start.index("--") + 1:],
                         ["--plugin-dir", "X", "--model", "opus", "--resume", "s-o"])
        self.assertIn("w1:p3", self.argv_of("pane", "split")[0])
        self.assertEqual(self.argv_of("agent", "prompt")[0][3], "/oke:start")
        orch = oke.load_state()["orchestrator"]
        self.assertEqual((orch["pane"], orch["revives"]), ("w1:p8", 1))

    def test_orchestrator_without_recorded_argv_is_not_revived(self):
        s = oke.load_state()
        del s["orchestrator"]["argv"]
        oke.save_state(s)
        self.revive_scenario()
        self.assertEqual(oke.revive("orchestrator"), "not_needed")


class TestWaitRevives(ReviveBase):
    def setUp(self):
        ReviveBase.setUp(self)
        self.revived = []
        self._revive = oke.revive

    def tearDown(self):
        oke.revive = self._revive
        ReviveBase.tearDown(self)

    def stub(self, *results):
        results = list(results)

        def fake_revive(name):
            self.revived.append(name)
            return results.pop(0) if len(results) > 1 else results[0]
        oke.revive = fake_revive

    def test_a_gone_child_ends_the_wait_without_a_revive(self):
        # the parent decides; OKe revives nobody by itself
        report = os.path.join(self.child_dir, "report.md")
        self.stub("revived")
        self.fake({"agent get": [E("agent_not_found")]})
        self.assertEqual(oke.wait_loop("fix-a-w1", report, 2, poll=0, max_polls=4), "gone")
        self.assertEqual(self.revived, [])

    def test_a_lost_name_with_a_live_session_is_not_gone(self):
        self.fake({"agent get": [E("agent_not_found")],
                   "agent list": [J({"agents": [AG("other-name", status="working", pane="w1:p5", session="s-w")]})]})
        self.assertIsNone(oke.wait_loop("fix-a-w1", os.path.join(self.child_dir, "report.md"), 2,
                                        poll=0, max_polls=5))

    def test_a_cancelled_job_ends_its_childs_wait(self):
        state = oke.load_state()
        state["jobs"]["fix-a"]["cancelled"] = True
        oke.save_state(state)
        self.fake({})
        self.assertEqual(oke.wait_loop("fix-a-w1", os.path.join(self.child_dir, "report.md"), 2,
                                       poll=0, max_polls=2), "cancelled")

    def test_a_refused_delivery_ends_in_deliver_failed(self):
        oke.queue_mail("fix-a-w1", "user", "hi")
        self.fake({"agent get": [AGENT("idle")], "agent prompt": [E("agent_not_found")]})
        self.assertEqual(oke.wait_loop("fix-a-w1", os.path.join(self.child_dir, "report.md"), 2,
                                       poll=0, max_polls=30), "deliver_failed")

    def test_an_archived_job_ends_its_childs_wait_with_a_slash_path(self):
        state = oke.load_state()
        del state["jobs"]["fix-a"]
        oke.save_state(state)
        self.fake({})
        report = os.path.join(self.child_dir, "report.md").replace("\\", "/")  # as `job new` prints it
        self.assertEqual(oke.wait_loop("fix-a-w1", report, 2, poll=0, max_polls=2), "cancelled")

    def test_one_failed_look_is_not_a_death(self):
        self.fake({"agent get": [E("agent_not_found"), AGENT("working")]})
        self.assertIsNone(oke.wait_loop("fix-a-w1", os.path.join(self.child_dir, "report.md"), 2,
                                        poll=0, max_polls=4))

    def test_pending_api_retry_is_not_idle_no_report(self):
        self.write_file(os.path.join(self.child_dir, "api-retry.json"), '{"attempt": 1}')
        self.fake({"agent get": [AGENT("idle")]})
        self.assertIsNone(oke.wait_loop("fix-a-w1", os.path.join(self.child_dir, "report.md"), 2,
                                        poll=0, max_polls=6))


class TestTick(ReviveBase):
    def setUp(self):
        ReviveBase.setUp(self)
        self.revived = []
        self._revive = oke.revive
        oke.revive = lambda name: self.revived.append(name) or "revived"

    def tearDown(self):
        oke.revive = self._revive
        ReviveBase.tearDown(self)

    def tick(self, pane, agents, *flags):
        self.fake({"agent list": [J({"agents": agents})]})
        self.assertEqual(self.cli("tick", "--pane", pane, *flags), 0)
        return json.loads(self.out)

    def test_unknown_pane_does_nothing(self):
        out = self.tick("w9:p9", [])
        self.assertIsNone(out["role"])
        self.assertEqual(self.revived, [])
        self.assertEqual(self.calls(), [])

    def test_the_watchers_revive_nobody(self):
        out = self.tick("w7:p1", [AG(None, pane="w7:p1", session="s-o")])
        self.assertEqual(out["role"], "orchestrator")
        out = self.tick("w1:p3", [AG("pm-toy", pane="w1:p3", session="s-pm")])
        self.assertEqual(out["role"], "pm")
        self.assertEqual(self.revived, [])

    def test_pm_skips_a_child_with_its_final_report(self):
        self.write_file(os.path.join(self.child_dir, "report.md"), "type: done\nseq: 2\n---\n")
        self.tick("w1:p3", [AG("pm-toy", pane="w1:p3", session="s-pm"),
                            AG(None, pane="w7:p1", session="s-o")])
        self.assertEqual(self.revived, [])

    def test_a_pane_with_another_session_is_not_the_orchestrator(self):
        # pane ids come back after a herdr restart: pane alone is not identity
        out = self.tick("w7:p1", [AG(None, pane="w7:p1", session="someone-else")])
        self.assertIsNone(out["role"])
        self.assertEqual(self.revived, [])

    def test_a_pane_whose_session_herdr_does_not_know_is_not_the_orchestrator(self):
        out = self.tick("w7:p1", [AG(None, pane="w7:p1", session=None)])
        self.assertIsNone(out["role"])

    def test_all_alive_writes_nothing(self):
        before = (io.open(oke.state_path(), encoding="utf-8").read(),
                  io.open(oke.children_path(self.key), encoding="utf-8").read())
        self.tick("w1:p3", [AG("pm-toy", pane="w1:p3", session="s-pm"),
                            AG(None, pane="w7:p1", session="s-o"),
                            AG("fix-a-w1", pane="w1:p5", session="s-w")])
        after = (io.open(oke.state_path(), encoding="utf-8").read(),
                 io.open(oke.children_path(self.key), encoding="utf-8").read())
        self.assertEqual(before, after)
        self.assertEqual(self.revived, [])

    def test_records_a_child_session_that_spawn_could_not_see(self):
        children = oke.read_children(self.key)
        children["fix-a-w1"]["session"] = None
        oke.write_json_atomic(oke.children_path(self.key), children)
        self.tick("w1:p3", [AG("pm-toy", pane="w1:p3", session="s-pm"),
                            AG(None, pane="w7:p1", session="s-o"),
                            AG("fix-a-w1", pane="w1:p5", session="s-new")])
        self.assertEqual(self.child()["session"], "s-new")

    def test_new_events_from_offset(self):
        oke.log_event("revived pm-toy pane w1:p3")
        # no offset yet: a mod that just started does not replay old events
        first = self.tick("w7:p1", [AG(None, pane="w7:p1", session="s-o"), AG("pm-toy", pane="w1:p3")])
        self.assertEqual(first["events"], [])
        oke.log_event("gave_up fix-a-w1")
        again = self.tick("w7:p1", [AG(None, pane="w7:p1", session="s-o"), AG("pm-toy", pane="w1:p3")],
                          "--events-from", str(first["events_offset"]))
        self.assertEqual(len(again["events"]), 1)
        self.assertIn("gave_up fix-a-w1", again["events"][0])

    def test_board_tree(self):
        self.write_file(os.path.join(oke.pm_dir(self.key), "board.md"), "## 지금\nplanning\n")
        self.write_file(os.path.join(self.child_dir, "board.md"), "## 지금\ncoding\n")
        out = self.tick("w7:p1", [AG(None, pane="w7:p1", session="s-o"),
                                  AG("pm-toy", pane="w1:p3", status="working"),
                                  AG("fix-a-w1", pane="w1:p5", status="idle")], "--board")
        repo = out["board"][0]
        self.assertEqual(repo["pm"]["agent"], "pm-toy")
        self.assertEqual(repo["pm"]["status"], "working")
        self.assertIn("planning", repo["pm"]["board"])
        job = repo["jobs"][0]
        self.assertEqual((job["job"], job["phase"]), ("fix-a", "working"))
        self.assertEqual(job["agents"][0]["agent"], "fix-a-w1")
        self.assertIn("coding", job["agents"][0]["board"])
        self.assertEqual(job["merge"], "none")
        self.assertIn("pending", job)
        self.assertEqual(job["agents"][0]["todos"], [])

    def test_board_shows_todos_and_tick_names_the_folder(self):
        todos = [{"content": "write test", "status": "completed"}, {"content": "fix", "status": "in_progress"},
                 "junk", {"content": "docs"}]
        self.write_file(os.path.join(self.child_dir, "todos.json"), json.dumps(todos))
        out = self.tick("w7:p1", [AG(None, pane="w7:p1", session="s-o"), AG("pm-toy", pane="w1:p3"),
                                  AG("fix-a-w1", pane="w1:p5")], "--board")
        self.assertEqual(out["board"][0]["jobs"][0]["agents"][0]["todos"],
                         [{"content": "write test", "status": "completed"},
                          {"content": "fix", "status": "in_progress"},
                          {"content": "docs", "status": "pending"}])
        worker = self.tick("w1:p5", [AG("fix-a-w1", pane="w1:p5"), AG("pm-toy", pane="w1:p3")])
        self.assertEqual(os.path.normcase(worker["folder"]), os.path.normcase(self.child_dir))

    def test_board_shows_cost_and_tick_names_job_and_seq(self):
        self.write_file(os.path.join(self.child_dir, "usage.json"), json.dumps({"usd": 0.42}))
        self.write_file(os.path.join(self.child_dir, "assignment.md"), "seq: 3\n---\nbody\n")
        out = self.tick("w7:p1", [AG(None, pane="w7:p1", session="s-o"), AG("pm-toy", pane="w1:p3"),
                                  AG("fix-a-w1", pane="w1:p5")], "--board")
        self.assertEqual(out["board"][0]["jobs"][0]["agents"][0]["cost"], 0.42)
        self.assertIsNone(out["board"][0]["pm"]["cost"])  # no turn yet
        worker = self.tick("w1:p5", [AG("fix-a-w1", pane="w1:p5"), AG("pm-toy", pane="w1:p3")])
        self.assertEqual((worker["job"], worker["seq"]), ("fix-a", 3))


class TestBoundary(ReviveBase):
    def boundary(self, pane):
        self.fake({"agent list": [J({"agents": [AG("fix-a-w1", pane="w1:p5", session="s-w"),
                                                AG("pm-toy", pane="w1:p3", session="s-pm"),
                                                AG(None, pane="w7:p1", session="s-o")]})]})
        self.assertEqual(self.cli("boundary", "--pane", pane), 0)
        return json.loads(self.out)

    def test_a_report_for_the_current_seq_is_a_boundary(self):
        self.write_file(os.path.join(self.child_dir, "assignment.md"), "seq: 2\n---\nwork\n")
        self.write_file(os.path.join(self.child_dir, "report.md"), "seq: 1\ntype: done\n---\nold\n")
        self.assertEqual(self.boundary("w1:p5"), {"role": "worker", "answered": []})
        self.write_file(os.path.join(self.child_dir, "report.md"), "seq: 2\ntype: done\n---\nnew\n")
        self.assertEqual(self.boundary("w1:p5")["answered"], ["fix-a:2"])

    def test_a_pm_answers_per_job_and_the_orchestrator_has_none(self):
        folder = oke.pm_job_dir("fix-a")
        os.makedirs(folder)
        self.write_file(os.path.join(folder, "assignment.md"), "seq: 1\n---\nplan it\n")
        self.write_file(os.path.join(folder, "report.md"), "seq: 1\ntype: plan\n---\nplan\n")
        self.assertEqual(self.boundary("w1:p3"), {"role": "pm", "answered": ["fix-a:1"]})
        self.assertEqual(self.boundary("w7:p1")["answered"], [])


class TestApiRetry(ReviveBase):
    def retry(self, pane, *args):
        self.fake({"agent list": [J({"agents": [AG("fix-a-w1", pane="w1:p5", session="s-w"),
                                                AG("pm-toy", pane="w1:p3", session="s-pm"),
                                                AG(None, pane="w7:p1", session="s-o")]})]})
        self.assertEqual(self.cli("api-retry", "--pane", pane, *args), 0)
        return json.loads(self.out)

    def retry_file(self):
        return os.path.join(self.child_dir, "api-retry.json")

    def test_backoff_doubles_from_30s_to_a_5_minute_cap(self):
        delays = [self.retry("w1:p5", "--attempt", str(n), "--error", "overloaded")["delay"]
                  for n in (1, 2, 3, 4, 5, 6)]
        self.assertEqual(delays, [30, 60, 120, 240, 300, 300])
        self.assertTrue(os.path.exists(self.retry_file()))

    def test_missing_error_kind_is_retried(self):
        self.assertEqual(self.retry("w1:p5", "--attempt", "1", "--error", "")["action"], "retry")
        self.assertEqual(self.retry("w1:p5", "--attempt", "2", "--error", "unknown")["action"], "retry")
        self.assertEqual(self.retry("w1:p5", "--attempt", "3", "--error", "")["action"], "give_up")

    def test_permanent_error_gives_up_with_a_failed_report(self):
        out = self.retry("w1:p5", "--attempt", "1", "--error", "authentication_failed")
        self.assertEqual(out["action"], "give_up")
        header = oke.read_header(os.path.join(self.child_dir, "report.md"))
        self.assertEqual((header["type"], header["seq"]), ("failed", 2))
        self.assertFalse(os.path.exists(self.retry_file()))

    def test_give_up_keeps_a_final_report_of_the_current_seq(self):
        report = os.path.join(self.child_dir, "report.md")
        seq = oke.read_header(os.path.join(self.child_dir, "assignment.md"))["seq"]
        self.write_file(report, "type: done\nseq: %s\n---\nfinished\n" % seq)
        self.assertEqual(self.retry("w1:p5", "--attempt", "1", "--error", "authentication_failed")["action"], "give_up")
        self.assertEqual(oke.read_header(report)["type"], "done")

    def test_limit_comes_from_config(self):
        self.write_file(os.path.join(self.home, "config.json"), '{"api_retry_limit": 2}')
        self.assertEqual(self.retry("w1:p5", "--attempt", "2", "--error", "rate_limit")["action"], "retry")
        self.assertEqual(self.retry("w1:p5", "--attempt", "3", "--error", "rate_limit")["action"], "give_up")

    def test_default_limit_is_20(self):
        self.assertEqual(self.retry("w1:p5", "--attempt", "20", "--error", "rate_limit")["action"], "retry")
        self.assertEqual(self.retry("w1:p5", "--attempt", "21", "--error", "rate_limit")["action"], "give_up")

    def test_clear_removes_the_file(self):
        self.retry("w1:p5", "--attempt", "1", "--error", "overloaded")
        self.assertEqual(self.retry("w1:p5", "--clear")["action"], "cleared")
        self.assertFalse(os.path.exists(self.retry_file()))

    def test_pm_folder(self):
        self.retry("w1:p3", "--attempt", "1", "--error", "overloaded")
        self.assertTrue(os.path.exists(os.path.join(oke.pm_dir(self.key), "api-retry.json")))

    def test_orchestrator_retries_without_a_file(self):
        out = self.retry("w7:p1", "--attempt", "1", "--error", "overloaded")
        self.assertEqual((out["action"], out["role"]), ("retry", "orchestrator"))
        out = self.retry("w7:p1", "--attempt", "1", "--error", "billing_error")
        self.assertEqual(out["action"], "give_up")

    def test_not_an_oke_session(self):
        self.assertEqual(self.retry("w9:p9", "--attempt", "1", "--error", "overloaded")["action"], "none")


class TestClaimRecordsLaunch(ReviveBase):
    def test_claim_records_argv_session_and_cwd(self):
        s = oke.load_state()
        s["orchestrator"] = {"pane": "w7:p1", "revives": 2}
        oke.save_state(s)
        os.environ["HERDR_PANE_ID"] = "w7:p1"
        sc = help_ok()
        sc["agent list"] = [J({"agents": [AG(None, pane="w7:p1", session="s-o9")]})]
        sc["pane process-info"] = [J({"process_info": {"foreground_processes": [
            {"name": "claude.exe", "cwd": "C:\\r\\", "argv": ["claude.exe", "--plugin-dir", "P",
                                                              "--continue", "--model", "opus"]}]}})]
        self.fake(sc)
        self.assertEqual(self.cli("status", "--claim"), 0)
        orch = oke.load_state()["orchestrator"]
        self.assertEqual(orch["argv"], ["--plugin-dir", "P", "--model", "opus"])
        self.assertEqual(orch["session"], "s-o9")
        self.assertEqual(orch["cwd"], "C:\\r\\")
        self.assertEqual(orch["revives"], 0)


class TestClaimAfterRevive(ReviveBase):
    def test_claim_by_a_revived_orchestrator_keeps_the_count(self):
        s = oke.load_state()
        s["orchestrator"].update(pane="w7:p9", revives=2, agent="oke-orch")
        oke.save_state(s)
        os.environ["HERDR_PANE_ID"] = "w7:p9"
        sc = help_ok()
        sc["agent list"] = [J({"agents": [AG("oke-orch", pane="w7:p9", session="s-o")]})]
        sc["pane process-info"] = [PINFO("claude.exe")]
        self.fake(sc)
        self.assertEqual(self.cli("status", "--claim"), 0)
        self.assertEqual(oke.load_state()["orchestrator"]["revives"], 2)

    def test_a_revived_orchestrator_pane_is_closed_on_the_next_revive(self):
        s = oke.load_state()
        s["orchestrator"].update(agent="oke-orch", terminal="term-5", pane="w1:p5")
        oke.save_state(s)
        self.revive_scenario(**{"agent get": [AGENT("working", name="oke-orch", pane="w1:p8", session="s-o2")]})
        self.assertEqual(oke.revive("orchestrator"), "revived")
        self.assertEqual(self.argv_of("pane", "close"), [["pane", "close", "w1:p5"]])


class TestRememberSession(ReviveBase):
    def test_a_changed_session_id_is_recorded_for_the_next_revive(self):
        oke._sessions.clear()
        self.fake({"agent get": [AGENT("working", name="fix-a-w1", pane="w1:p5", session="s-new")]})
        self.assertIsNone(oke.look_at("fix-a-w1", self.child_dir))
        self.assertEqual(self.child()["session"], "s-new")


class TestStaleRetryMarker(ReviveBase):
    def test_an_old_marker_no_longer_holds_the_wait(self):
        marker = os.path.join(self.child_dir, "api-retry.json")
        self.write_file(marker, '{"attempt": 1, "delay": 30}')
        old = time.time() - 30 - oke.RETRY_MARKER_GRACE - 5
        os.utime(marker, (old, old))
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(oke.wait_loop("fix-a-w1", os.path.join(self.child_dir, "report.md"), 2,
                                       poll=0, max_polls=6), "idle_no_report")


class TestSpawnTerminal(ReviveBase):
    def test_spawn_records_the_terminal_of_its_pane(self):
        a = self.assignment("fix-a-w2", "worker", "fix-a")
        sc = {"agent list": [J({"agents": []})], "pane split": [J({"pane": {"pane_id": "w1:p9"}})],
              "agent start": [AGENT()], "agent prompt": [J({"type": "agent_prompted"})],
              "agent get": [AGENT("working", pane="w1:p9")],
              "pane get": [J({"pane": {"pane_id": "w1:p9", "terminal_id": "term-9"}})]}
        self.fake(sc)
        self.assertEqual(self.cli("spawn", a, "--split-from", "w1:p3"), 0)
        self.assertEqual(oke.read_children(self.key)["fix-a-w2"]["terminal"], "term-9")


class TestLock(ReviveBase):
    def test_second_try_fails_until_unlock(self):
        self.assertTrue(oke.try_lock("x"))
        self.assertFalse(oke.try_lock("x"))
        oke.unlock("x")
        self.assertTrue(oke.try_lock("x"))


if __name__ == "__main__":
    unittest.main()
