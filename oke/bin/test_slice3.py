"""Slice 3: several jobs per PM, inbox, per-PM wait, user queue."""
import io
import json
import os
import time
import unittest

from test_oke import put_pm, AG, AGENT, E, J, OK, SpawnBase
from test_revive import PANE_OK, ReviveBase, started
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

    def pm_job(self, job, seq, report=None, sent=True):
        """The orchestrator's assignment for `job`, sent unless `sent` is False, and
        optionally the PM's report."""
        folder = os.path.join(self.home, "jobs", job, "pm")
        self.put(os.path.join(folder, "assignment.md"),
                 "agent: pm-toy\nrole: pm\njob: %s\nseq: %d\n---\n" % (job, seq))
        if sent:
            s = oke.load_state()
            if job in s["jobs"]:
                s["jobs"][job]["sent_seq"] = seq
                oke.save_state(s)
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

    def test_phase_reviewing_is_accepted_and_moves_phase_at(self):
        self.job("fix-a", "working")
        oke.save_state(dict(oke.load_state(), jobs={"fix-a": dict(oke.load_state()["jobs"]["fix-a"], phase_at=1.0)}))
        self.assertEqual(self.cli("job", "phase", "fix-a", "reviewing"), 0)
        job = oke.load_state()["jobs"]["fix-a"]
        self.assertEqual(job["phase"], "reviewing")
        self.assertGreater(job["phase_at"], 1.0)

    def test_spawn_of_a_worker_is_allowed_while_reviewing(self):
        # a rebase conflict at the delivery sends the worker back; a dead one must come back
        self.job("fix-a", "reviewing")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        self.fake({"agent list": [J({"agents": []})], "agent start": [E("agent_not_ready")]})
        self.cli("spawn", a, "--pane", "w1:p2")
        self.assertNotIn("plan not approved", self.err)

    def test_defer_records_the_current_item_key(self):
        self.job("fix-a", "awaiting_approval")
        self.assertEqual(self.cli("job", "defer", "fix-a"), 0)
        job = oke.load_state()["jobs"]["fix-a"]
        self.assertEqual(job["deferred"], "plan:%r" % job["phase_at"])

    def test_defer_with_nothing_pending_fails(self):
        self.job("fix-a", "working")
        self.pm_job("fix-a", 1, REPORT("waiting", 1))  # handed over, nothing asked
        self.assertEqual(self.cli("job", "defer", "fix-a"), 1)


class InboxBase(S3Base):
    def setUp(self):
        S3Base.setUp(self)
        self.job("fix-a", "working")
        s = oke.load_state()
        put_pm(s, self.key, {"agent": "pm-toy", "pane": "w1:p3", "session": "s-pm"})
        s["orchestrator"] = {"pane": "w7:p1"}
        oke.save_state(s)
        oke.write_json_atomic(oke.children_path(self.pm), {"fix-a-w1": {
            "agent": "fix-a-w1", "pane": "w1:p5", "session": "s-w", "role": "worker", "job": "fix-a"}})

    def texts(self, agent):
        return [t for _, t in oke.inbox_messages(agent)]


class TestSend(InboxBase):
    def test_messages_keep_their_order(self):
        self.fake({})
        for word in ("one", "two", "three"):
            self.assertEqual(self.cli("send", "pm-toy", word), 0)
        self.assertEqual(self.texts("pm-toy"), ["user: one", "user: two", "user: three"])

    def test_unknown_agent_is_refused(self):
        self.fake({})
        self.assertEqual(self.cli("send", "nobody", "hi"), 1)

    def test_the_orchestrator_has_no_inbox(self):
        self.fake({})
        self.assertEqual(self.cli("send", "orchestrator", "hi"), 1)

    def test_a_leading_slash_is_refused(self):
        # Git Bash rewrites such an argument into a Windows path before python sees it
        self.fake({})
        self.assertEqual(self.cli("send", "pm-toy", "/tmp/x"), 1)
        self.assertEqual(self.texts("pm-toy"), [])

    def test_newlines_become_spaces(self):
        self.fake({})
        self.cli("send", "pm-toy", "a\nb")
        self.assertEqual(self.texts("pm-toy"), ["user: a b"])

    def test_the_sender_is_the_agent_in_this_pane(self):
        os.environ["HERDR_PANE_ID"] = "w1:p5"
        self.fake({"agent list": [J({"agents": [AG("fix-a-w1", pane="w1:p5", session="s-w")]})]})
        self.cli("send", "pm-toy", "URGENT fix-a: the plan misses a file")
        self.assertEqual(self.texts("pm-toy"), ["fix-a-w1: URGENT fix-a: the plan misses a file"])


class TestTakeMail(InboxBase):
    """The agent's own mod takes its inbox after a turn (a background subagent may run)."""

    def flag(self, agent):
        return os.path.exists(os.path.join(oke.inbox_dir(agent), oke.MAIL_FLAG))

    def take(self, agent, pane, *extra):
        self.assertEqual(self.cli("take-mail", "--agent", agent, "--pane", pane, *extra), 0)
        return json.loads(self.out.strip().splitlines()[-1])

    def test_a_send_raises_the_flag_and_a_take_empties_the_inbox(self):
        self.fake({})
        self.cli("send", "pm-toy", "one")
        self.cli("send", "pm-toy", "two")
        self.assertTrue(self.flag("pm-toy"))
        out = self.take("pm-toy", "w1:p3")
        self.assertEqual(out["text"], "OKe inbox: user: one | user: two")
        self.assertEqual(len(out["names"]), 2)
        self.assertEqual(self.texts("pm-toy"), [])
        self.assertFalse(self.flag("pm-toy"))
        self.assertIsNone(self.take("pm-toy", "w1:p3")["text"])

    def test_only_the_agents_own_pane_or_session_takes(self):
        self.fake({})
        self.cli("send", "pm-toy", "one")
        self.assertIsNone(self.take("pm-toy", "w1:p5")["text"])  # the worker's pane
        self.assertIsNone(self.take("nobody", "w1:p3")["text"])
        self.assertEqual(self.texts("pm-toy"), ["user: one"])
        self.assertEqual(self.take("pm-toy", "session:s-pm")["text"], "OKe inbox: user: one")

    def test_mail_the_mod_could_not_submit_goes_back(self):
        self.fake({})
        self.cli("send", "fix-a-w1", "one")
        names = self.take("fix-a-w1", "w1:p5")["names"]
        self.take("fix-a-w1", "w1:p5", "--give-back", *names)
        self.assertEqual(self.texts("fix-a-w1"), ["user: one"])
        self.assertTrue(self.flag("fix-a-w1"))
        outside = os.path.join(oke.inbox_dir("fix-a-w1"), "x.txt")
        io.open(os.path.join(oke.oke_home(), "x.txt"), "w").close()
        self.take("fix-a-w1", "w1:p5", "--give-back", "../../x.txt", "..\\..\\x.txt")  # names only
        self.assertFalse(os.path.exists(outside))
        self.assertTrue(os.path.exists(os.path.join(oke.oke_home(), "x.txt")))

    def test_a_delivery_running_elsewhere_keeps_the_mail(self):
        self.fake({})
        self.cli("send", "pm-toy", "one")
        self.assertTrue(oke.try_lock("deliver-pm-toy"))
        try:
            self.assertIsNone(self.take("pm-toy", "w1:p3")["text"])
        finally:
            oke.unlock("deliver-pm-toy")
        self.assertEqual(self.texts("pm-toy"), ["user: one"])

    def test_a_wait_delivery_lowers_the_flag_too(self):
        self.fake({})
        self.cli("send", "fix-a-w1", "one")
        self.fake({"agent prompt": [J({"type": "agent_prompted"})]})
        self.assertTrue(oke.deliver("fix-a-w1"))
        self.assertFalse(self.flag("fix-a-w1"))

    def test_mail_that_lands_while_the_flag_goes_down_raises_it_again(self):
        self.fake({})
        os.makedirs(oke.inbox_dir("pm-toy"), exist_ok=True)
        oke.set_mail_flag("pm-toy", True)
        real = oke.set_mail_flag

        def send_in_between(agent, on):
            real(agent, on)
            if not on:
                oke.set_mail_flag = real
                oke.queue_mail("pm-toy", "user", "late")  # writes its message, then the flag
        oke.set_mail_flag = send_in_between
        try:
            oke.lower_mail_flag("pm-toy")
        finally:
            oke.set_mail_flag = real
        self.assertTrue(self.flag("pm-toy"))
        self.assertEqual(self.texts("pm-toy"), ["user: late"])

    def test_a_stale_flag_goes_away_on_the_next_take(self):
        self.fake({})
        os.makedirs(oke.inbox_dir("pm-toy"), exist_ok=True)
        oke.set_mail_flag("pm-toy", True)
        self.assertIsNone(self.take("pm-toy", "w1:p3")["text"])
        self.assertFalse(self.flag("pm-toy"))


class TestDeliver(InboxBase):
    def test_delivery_prompts_once_and_moves_the_messages(self):
        self.fake({})
        self.cli("send", "fix-a-w1", "one")
        self.cli("send", "fix-a-w1", "two")
        self.fake({"agent prompt": [J({"type": "agent_prompted"})]})
        self.assertTrue(oke.deliver("fix-a-w1"))
        self.assertEqual(self.calls(), [["agent", "prompt", "fix-a-w1", "OKe inbox: user: one | user: two",
                                         "--wait", "--until", "working", "--until", "blocked",
                                         "--timeout", "20000"]])
        self.assertEqual(self.texts("fix-a-w1"), [])
        self.assertEqual(len(os.listdir(os.path.join(oke.inbox_dir("fix-a-w1"), "read"))), 2)

    def test_a_delivery_takes_only_what_fits_in_one_command_line(self):
        for i in range(3):
            oke.queue_mail("fix-a-w1", "user", ("m%d " % i) + "x" * 7000)
        oke.queue_mail("fix-a-w1", "user", "y" * 20000)
        self.fake({"agent prompt": [J({"type": "agent_prompted"})]})
        oke.deliver("fix-a-w1")
        self.assertEqual(len(self.texts("fix-a-w1")), 2)  # m2 and the long one wait
        oke.deliver("fix-a-w1")
        self.assertEqual(len(self.texts("fix-a-w1")), 1)
        oke.deliver("fix-a-w1")  # one too long alone still goes, by itself
        self.assertEqual(self.texts("fix-a-w1"), [])

    def test_a_failed_prompt_keeps_the_messages(self):
        self.fake({})
        self.cli("send", "fix-a-w1", "one")
        self.fake({"agent prompt": [E("agent_not_found")]})
        self.assertFalse(oke.deliver("fix-a-w1"))
        self.assertEqual(self.texts("fix-a-w1"), ["user: one"])

    def test_nothing_to_deliver(self):
        self.fake({})
        self.assertFalse(oke.deliver("fix-a-w1"))
        self.assertEqual(self.calls(), [])


class TestWaitDelivers(InboxBase):
    def report_path(self):
        return self.put(os.path.join(self.home, "jobs", "fix-a", "fix-a-w1", "report.md"),
                        "type: waiting\nseq: 2\n---\n")

    def test_an_idle_target_gets_its_mail_and_the_wait_goes_on(self):
        r = self.report_path()
        self.fake({})
        self.cli("send", "fix-a-w1", "one")
        self.fake({"agent get": [AGENT("idle")], "agent prompt": [J({"type": "agent_prompted"})]})
        self.assertIsNone(oke.wait_loop("fix-a-w1", r, 2, poll=0, max_polls=5))
        self.assertEqual(len(self.argv_of("agent", "prompt")), 1)
        self.assertEqual(self.texts("fix-a-w1"), [])

    def test_mail_holds_an_idle_verdict(self):
        # a message still on its way means the target has work: not "idle, no report"
        r = self.put(os.path.join(self.home, "jobs", "fix-a", "fix-a-w1", "report.md"), "type: done\nseq: 1\n---\n")
        self.fake({})
        self.cli("send", "fix-a-w1", "one")
        self.fake({"agent get": [AGENT("idle")], "agent prompt": [E("agent_not_idle")]})
        self.assertIsNone(oke.wait_loop("fix-a-w1", r, 2, poll=0, max_polls=8))

    def test_mail_that_never_goes_in_ends_the_wait(self):
        r = self.report_path()
        self.fake({})
        self.cli("send", "fix-a-w1", "one")
        self.fake({"agent get": [AGENT("idle")], "agent prompt": [E("agent_not_idle")]})
        self.assertEqual(oke.wait_loop("fix-a-w1", r, 2, poll=0, max_polls=20), "deliver_failed")
        self.assertEqual(self.texts("fix-a-w1"), ["user: one"])

    def test_a_prompt_that_timed_out_after_typing_counts_as_delivered(self):
        self.fake({})
        self.cli("send", "fix-a-w1", "one")
        self.fake({"agent prompt": [E("timeout")], "agent get": [AGENT("working")]})
        self.assertTrue(oke.deliver("fix-a-w1"))
        self.assertEqual(self.texts("fix-a-w1"), [])

    def test_a_refused_prompt_to_a_busy_target_keeps_the_mail(self):
        self.fake({})
        self.cli("send", "fix-a-w1", "one")
        self.fake({"agent prompt": [E("agent_not_idle")], "agent get": [AGENT("working")]})
        self.assertFalse(oke.deliver("fix-a-w1"))
        self.assertEqual(self.texts("fix-a-w1"), ["user: one"])

    def test_a_file_error_is_a_passing_outage(self):
        r = self.report_path()
        self.fake({"agent get": [AGENT("working")]})
        original, calls = oke.check_once, []

        def flaky(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("file in use")
            return original(*a, **kw)
        oke.check_once = flaky
        try:
            self.assertIsNone(oke.wait_loop("fix-a-w1", r, 2, poll=0, max_polls=3))
        finally:
            oke.check_once = original

    def test_a_result_found_after_a_newer_wait_started_is_dropped(self):
        r = self.report_path()
        self.fake({})
        original = oke.check_once

        def newer_wait_meanwhile(*a, **kw):
            oke.claim_wait("fix-a-w1")
            return "report done"
        oke.check_once = newer_wait_meanwhile
        try:
            self.assertEqual(oke.wait_loop("fix-a-w1", r, 2, poll=0, max_polls=3), "superseded")
        finally:
            oke.check_once = original

    def test_a_working_target_gets_no_mail(self):
        r = self.report_path()
        self.fake({})
        self.cli("send", "fix-a-w1", "one")
        self.fake({"agent get": [AGENT("working")]})
        self.assertIsNone(oke.wait_loop("fix-a-w1", r, 2, poll=0, max_polls=5))
        self.assertEqual(self.argv_of("agent", "prompt"), [])

    def test_a_newer_wait_supersedes_an_older_one(self):
        r = self.report_path()
        self.fake({"agent get": [AGENT("working")]})
        original = oke.check_once

        def newer_wait_starts(*a, **kw):
            oke.claim_wait("fix-a-w1")
            return original(*a, **kw)
        oke.check_once = newer_wait_starts
        try:
            self.assertEqual(oke.wait_loop("fix-a-w1", r, 2, poll=0, max_polls=5), "superseded")
        finally:
            oke.check_once = original


NL = chr(10)


QUESTION_BLOCK = NL.join(["## Questions", "job: add a subtract button", "decide: which sign the button shows",
                          "options:", "- minus: the button shows -", "- word: the button shows sub", ""])


def REPORT(kind, seq):
    """A PM report in the required form: a `waiting` one names what it waits on, a `question` one
    asks one question."""
    on = "on: planner subagent a1%s" % NL if kind == "waiting" else ""
    body = QUESTION_BLOCK if kind == "question" else ""
    return "type: %s%sseq: %d%s%s---%s%s" % (kind, NL, seq, NL, on, NL, body)


class TestPmWait(InboxBase):
    def wait(self, polls=5):
        return oke.wait_loop("pm-toy", None, None, poll=0, max_polls=polls, jobs_key="pm-toy")

    def test_a_final_report_names_its_job_and_is_returned_once(self):
        self.pm_job("fix-a", 2, REPORT("done", 2))
        self.fake({"agent get": [AGENT("working")]})
        self.assertEqual(self.wait(), "report done fix-a")
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["seen"], "done:2")
        self.assertIsNone(self.wait())

    def test_a_job_added_later_is_seen(self):
        self.fake({"agent get": [AGENT("working")]})
        self.assertIsNone(oke.check_pm_once("pm-toy", "pm-toy"))
        self.job("fix-b", "planning")
        self.pm_job("fix-b", 1, REPORT("question", 1))
        self.assertEqual(oke.check_pm_once("pm-toy", "pm-toy"), "report question fix-b")

    def test_the_oldest_report_comes_first(self):
        self.job("fix-b", "planning")
        old = self.pm_job("fix-b", 1, REPORT("done", 1))
        self.pm_job("fix-a", 1, REPORT("done", 1))
        os.utime(os.path.join(old, "report.md"), (1, 1))
        self.fake({"agent get": [AGENT("working")]})
        self.assertEqual(self.wait(), "report done fix-b")

    def test_an_idle_pm_owing_a_report_names_the_job(self):
        self.pm_job("fix-a", 3, REPORT("waiting", 2))
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(self.wait(), "idle_no_report fix-a")

    def test_a_job_without_a_pm_assignment_is_not_owed(self):
        self.job("fix-b", "intake")
        self.pm_job("fix-a", 2, REPORT("waiting", 2))
        self.fake({"agent get": [AGENT("idle")]})
        self.assertIsNone(self.wait())

    def test_a_seen_final_report_is_not_owed(self):
        self.pm_job("fix-a", 2, REPORT("done", 2))
        s = oke.load_state()
        s["jobs"]["fix-a"].update(seen="done:2", phase="awaiting_approval")
        oke.save_state(s)
        self.fake({"agent get": [AGENT("idle")]})
        self.assertIsNone(self.wait())

    def test_a_bad_report_names_the_job(self):
        self.pm_job("fix-a", 2, "garbage")
        self.fake({"agent get": [AGENT("idle")]})
        self.assertTrue(self.wait().startswith("bad_report fix-a "))

    def test_the_pm_retry_marker_holds_the_wait(self):
        self.pm_job("fix-a", 3, REPORT("waiting", 2))
        oke.write_json_atomic(os.path.join(oke.pm_dir(self.pm), "api-retry.json"), {"attempt": 1, "delay": 30})
        self.fake({"agent get": [AGENT("idle")]})
        self.assertIsNone(self.wait())

    def test_a_pm_at_a_dialog_is_blocked(self):
        self.pm_job("fix-a", 2, REPORT("waiting", 2))
        self.fake({"agent get": [AGENT("blocked")], "agent read": [{"stdout": "Allow? 1. Yes", "exit": 0}]})
        self.assertEqual(self.wait(), "blocked" + NL + "Allow? 1. Yes")

    def test_a_report_written_while_the_pm_read_blocked_comes_first(self):
        # a PM reads blocked, then writes its report meanwhile
        folder = self.pm_job("fix-a", 2, REPORT("waiting", 2))
        original = oke.look_at

        def blocked_then_report(*a, **kw):
            self.put(os.path.join(folder, "report.md"), REPORT("done", 2))
            return "blocked" + NL + "Allow? 1. Yes"
        oke.look_at = blocked_then_report
        try:
            self.assertEqual(oke.check_pm_once("pm-toy", "pm-toy"), "report done fix-a")
        finally:
            oke.look_at = original

    def test_a_job_whose_message_is_not_sent_yet_is_not_owed(self):
        # the orchestrator writes the assignment, then sets the phase, then maybe revives
        # the PM, and only then sends: that gap is no "idle, no report"
        self.pm_job("fix-a", 3, REPORT("waiting", 2), sent=False)
        self.fake({"agent get": [AGENT("idle")]})
        self.assertIsNone(self.wait())

    def test_send_with_a_job_marks_its_seq_as_sent(self):
        self.pm_job("fix-a", 3, sent=False)
        self.fake({})
        self.assertEqual(self.cli("send", "pm-toy", "--job", "fix-a", "Job fix-a: assignment updated"), 0)
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["sent_seq"], 3)

    def test_a_blocked_report_after_a_question_at_the_same_seq_comes_through(self):
        folder = self.pm_job("fix-a", 2, REPORT("question", 2))
        self.fake({"agent get": [AGENT("working")]})
        self.assertEqual(self.wait(), "report question fix-a")
        self.put(os.path.join(folder, "report.md"), REPORT("blocked", 2))
        self.assertEqual(self.wait(), "report blocked fix-a")

    def test_a_report_found_by_a_superseded_jobs_wait_is_still_returned(self):
        # it already marked the report seen: the newer wait would never return it
        self.pm_job("fix-a", 2, REPORT("done", 2))
        self.fake({"agent get": [AGENT("working")]})
        original = oke.check_pm_once

        def newer_wait_meanwhile(*a, **kw):
            result = original(*a, **kw)
            oke.claim_wait("pm-toy")
            return result
        oke.check_pm_once = newer_wait_meanwhile
        try:
            self.assertEqual(self.wait(), "report done fix-a")
        finally:
            oke.check_pm_once = original

    def test_a_job_still_silent_after_a_nudge_is_reported_again_then_left(self):
        self.pm_job("fix-a", 3, REPORT("waiting", 2))
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(self.wait(), "idle_no_report fix-a")
        self.assertEqual(self.wait(), "idle_no_report again fix-a")
        self.assertIsNone(self.wait(8))  # the user was told; no endless wake-ups

    def test_two_waits_at_once_return_a_report_only_once(self):
        self.pm_job("fix-a", 2, REPORT("done", 2))
        self.fake({"agent get": [AGENT("working")]})
        original = oke.load_state

        def other_wait_marks_it_meanwhile():
            # the first unlocked read sees the report fresh; the other wait then marks it
            s = original()
            if not calls:
                calls.append(1)
                marked = original()
                marked["jobs"]["fix-a"]["seen"] = "done:2"
                oke.save_state(marked)
            return s
        calls = []
        oke.load_state = other_wait_marks_it_meanwhile
        try:
            self.assertIsNone(oke.check_pm_once("pm-toy", "pm-toy"))
        finally:
            oke.load_state = original

    def test_a_nudge_for_the_same_seq_keeps_the_count(self):
        # the orchestrator's nudge uses send --job too: it must not reset the count
        self.pm_job("fix-a", 3, REPORT("waiting", 2))
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(self.wait(), "idle_no_report fix-a")
        self.fake({})
        self.cli("send", "pm-toy", "--job", "fix-a", "Job fix-a: re-read your assignment")
        self.fake({"agent get": [AGENT("idle")], "agent prompt": [J({"type": "agent_prompted"})]})
        oke.deliver("pm-toy")
        self.assertEqual(self.wait(), "idle_no_report again fix-a")

    def test_a_new_send_for_the_job_clears_the_warnings(self):
        self.pm_job("fix-a", 3, REPORT("waiting", 2))
        self.fake({"agent get": [AGENT("idle")]})
        self.wait()
        self.wait()
        self.pm_job("fix-a", 4, REPORT("waiting", 2), sent=False)
        self.fake({})
        self.cli("send", "pm-toy", "--job", "fix-a", "Job fix-a: assignment updated (seq 4)")
        self.fake({"agent get": [AGENT("idle")], "agent prompt": [J({"type": "agent_prompted"})]})
        oke.deliver("pm-toy")
        self.assertEqual(self.wait(), "idle_no_report fix-a")

    def test_a_pm_without_open_jobs_ends_the_wait(self):
        self.advance("fix-a", "done")
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(self.wait(), "no_jobs")

    def test_a_cancel_for_the_last_job_of_a_dead_pm_is_dropped(self):
        # nobody revives a PM without open jobs: the cancel must not wait for the next PM
        self.pm_job("fix-a", 3, REPORT("waiting", 2))
        self.fake({})
        self.cli("send", "pm-toy", "--job", "fix-a", "Job fix-a: assignment updated (seq 3)")
        self.assertEqual(self.cli("job", "phase", "fix-a", "failed"), 0)
        self.fake({"agent get": [E("agent_not_found")]})
        self.assertEqual(self.wait(), "no_jobs")
        self.assertEqual(self.texts("pm-toy"), [])

    def test_mail_for_a_dead_pm_with_a_legacy_job_is_kept(self):
        # fix-a is open but has no job assignment yet: the PM must come back, not be dropped
        self.fake({})
        self.cli("send", "pm-toy", "hello")
        self.fake({"agent get": [E("agent_not_found")]})
        self.assertEqual(oke.check_pm_once("pm-toy", "pm-toy"), "gone")
        self.assertEqual(self.texts("pm-toy"), ["user: hello"])

    def test_a_cancel_for_the_last_job_is_delivered_before_no_jobs(self):
        self.pm_job("fix-a", 3, REPORT("waiting", 2))
        self.fake({})
        self.cli("send", "pm-toy", "--job", "fix-a", "Job fix-a: assignment updated (seq 3)")
        self.assertEqual(self.cli("job", "phase", "fix-a", "failed"), 0)
        self.fake({"agent get": [AGENT("idle")], "agent prompt": [J({"type": "agent_prompted"})]})
        self.assertEqual(self.wait(), "no_jobs")
        self.assertEqual(self.texts("pm-toy"), [])

    def test_send_to_an_unknown_job_queues_nothing(self):
        self.fake({})
        self.assertEqual(self.cli("send", "pm-toy", "--job", "nope", "hello"), 1)
        self.assertEqual(self.texts("pm-toy"), [])

    def test_cli_needs_a_report_or_jobs(self):
        self.fake({})
        self.assertEqual(self.cli("wait", "pm-toy"), 1)
        self.assertEqual(self.cli("wait", "fix-a-w1", "--jobs"), 1)


class TestPmFolderUsers(ReviveBase):
    pm_job = S3Base.pm_job
    put = S3Base.put

    def pm_scenario(self):
        self.revive_scenario(**{"pane get": [PANE_OK("w1:p3")],
                                "pane list": [J({"panes": []})],  # a revived PM gets its own workspace
                                "workspace create": [J({"root_pane": {"pane_id": "w9:p1"}})],
                                "agent get": [AGENT("working", name="pm-toy", pane="w9:p1", session="s-pm")]})

    def test_a_new_job_report_resets_the_pm_revive_count(self):
        folder = self.pm_job("fix-a", 1, REPORT("waiting", 1))
        s = oke.load_state()
        s["pms"][self.pm]["pm"].update(revives=3, report_mtime=1.0)
        oke.save_state(s)
        self.pm_scenario()
        self.assertEqual(oke.revive("pm-toy"), "revived")
        pm = oke.load_state()["pms"][self.pm]["pm"]
        self.assertEqual(pm["revives"], 1)
        self.assertEqual(pm["report_mtime"], os.path.getmtime(os.path.join(folder, "report.md")))

    def test_the_pm_revive_prompt_names_each_open_job_assignment(self):
        self.pm_job("fix-a", 1)
        self.pm_scenario()
        oke.revive("pm-toy")
        prompt = self.argv_of("agent", "prompt")[0][3]
        self.assertIn("jobs/fix-a/pm/assignment.md", prompt)

    def test_the_pm_compact_message_names_each_open_job_assignment(self):
        self.pm_job("fix-a", 1)
        os.environ["HERDR_PANE_ID"] = "w1:p3"
        self.assertIn("jobs/fix-a/pm/assignment.md", oke.compact_message())

    def give_up(self):
        self.fake({"agent list": [J({"agents": [AG("pm-toy", pane="w1:p3", session="s-pm")]})]})
        self.assertEqual(self.cli("api-retry", "--pane", "w1:p3", "--attempt", "1",
                                  "--error", "billing_error"), 0)

    def test_a_pm_give_up_fails_only_the_jobs_owing_a_report(self):
        self.job("fix-b", "awaiting_approval")
        owing = self.pm_job("fix-a", 3, REPORT("waiting", 2))
        answered = self.pm_job("fix-b", 1, REPORT("done", 1))
        self.give_up()
        self.assertEqual(oke.read_header(os.path.join(owing, "report.md")), {"type": "failed", "seq": 3})
        self.assertEqual(oke.read_header(os.path.join(answered, "report.md"))["type"], "done")

    def test_status_gives_each_job_its_pm_seq_and_folder(self):
        self.pm_job("fix-a", 4)
        self.fake({"agent list": [J({"agents": []})], "agent start --help": [OK], "agent prompt --help": [OK],
                   "agent wait --help": [OK], "agent read --help": [OK], "agent send-keys --help": [OK],
                   "pane split --help": [OK], "tab create --help": [OK]})
        self.assertEqual(self.cli("status"), 0)
        out = json.loads(self.out)
        self.assertEqual(out["jobs"]["fix-a"]["pm_seq"], 4)
        self.assertTrue(out["jobs"]["fix-a"]["pm_job_dir"].endswith("/jobs/fix-a/pm"))
        self.assertNotIn("seq", out["pms"][self.pm])


class TestPendingUnsent(InboxBase):
    def test_an_assignment_written_but_not_sent_is_pending(self):
        # the orchestrator was compacted between writing seq 3 and oke send --job
        self.pm_job("fix-a", 3, REPORT("waiting", 2), sent=False)
        self.fake({})
        self.cli("pending")
        items = json.loads(self.out)["items"]
        self.assertEqual([(i["job"], i["kind"]) for i in items], [("fix-a", "unsent")])

    def test_a_report_of_unknown_type_after_two_warnings_is_pending(self):
        self.pm_job("fix-a", 3, REPORT("progress", 3))
        self.fake({"agent get": [AGENT("idle")]})
        oke.wait_loop("pm-toy", None, None, poll=0, max_polls=5, jobs_key="pm-toy")
        oke.wait_loop("pm-toy", None, None, poll=0, max_polls=5, jobs_key="pm-toy")
        self.cli("pending")
        items = json.loads(self.out)["items"]
        self.assertEqual([(i["job"], i["kind"]) for i in items], [("fix-a", "silent")])

    def test_a_silent_job_after_two_warnings_is_pending(self):
        self.pm_job("fix-a", 3, REPORT("waiting", 2))
        self.fake({"agent get": [AGENT("idle")]})
        oke.wait_loop("pm-toy", None, None, poll=0, max_polls=5, jobs_key="pm-toy")
        oke.wait_loop("pm-toy", None, None, poll=0, max_polls=5, jobs_key="pm-toy")
        self.cli("pending")
        items = json.loads(self.out)["items"]
        self.assertEqual([(i["job"], i["kind"]) for i in items], [("fix-a", "silent")])


class TestPending(S3Base):
    def pending(self):
        self.fake({})
        self.assertEqual(self.cli("pending"), 0)
        return json.loads(self.out)["items"]

    def at(self, job, t):
        s = oke.load_state()
        s["jobs"][job]["phase_at"] = t
        oke.save_state(s)

    def test_order_is_rank_then_time(self):
        self.job("p-old", "awaiting_approval")
        self.at("p-old", 1.0)
        self.job("p-new", "awaiting_approval")
        self.at("p-new", 9.0)
        self.job("q", "working")
        self.pm_job("q", 2, REPORT("question", 2))
        self.job("d", "planning")
        self.pm_job("d", 1, REPORT("done", 1))
        self.job("w", "working")
        self.pm_job("w", 1, REPORT("waiting", 1))
        items = self.pending()
        self.assertEqual([(i["job"], i["kind"]) for i in items],
                         [("d", "report_done"), ("q", "question"), ("p-old", "plan"), ("p-new", "plan")])

    def test_a_report_of_an_old_seq_is_not_pending(self):
        self.job("q", "working")
        self.pm_job("q", 3, REPORT("question", 2))
        self.assertEqual(self.pending(), [])

    def test_a_deferred_item_is_flagged_until_its_job_moves_on(self):
        self.job("q", "working")
        self.pm_job("q", 2, REPORT("question", 2))
        self.assertEqual(self.cli("job", "defer", "q"), 0)
        self.assertTrue(self.pending()[0]["deferred"])
        self.pm_job("q", 3, REPORT("question", 3))
        self.assertFalse(self.pending()[0]["deferred"])

    def test_done_and_failed_jobs_are_left_out(self):
        self.job("x", "done")
        self.pm_job("x", 1, REPORT("question", 1))
        self.assertEqual(self.pending(), [])


if __name__ == "__main__":
    unittest.main()
