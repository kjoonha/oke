"""Slice 3: several jobs per PM, inbox, per-PM wait, user queue, merge line."""
import io
import json
import os
import time
import unittest

from test_oke import AG, AGENT, E, J, OK, SpawnBase
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


class InboxBase(S3Base):
    def setUp(self):
        S3Base.setUp(self)
        self.job("fix-a", "working")
        s = oke.load_state()
        s["repos"][self.key] = {"pm": {"agent": "pm-toy", "pane": "w1:p3", "session": "s-pm"}}
        s["orchestrator"] = {"pane": "w7:p1"}
        oke.save_state(s)
        oke.write_json_atomic(oke.children_path(self.key), {"fix-a-w1": {
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


def REPORT(kind, seq):
    return "type: %s%sseq: %d%s---%s" % (kind, NL, seq, NL, NL)


class TestPmWait(InboxBase):
    def wait(self, polls=5):
        return oke.wait_loop("pm-toy", None, None, poll=0, max_polls=polls, jobs_key=self.key)

    def test_a_final_report_names_its_job_and_is_returned_once(self):
        self.pm_job("fix-a", 2, REPORT("done", 2))
        self.fake({"agent get": [AGENT("working")]})
        self.assertEqual(self.wait(), "report done fix-a")
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["seen"], "done:2")
        self.assertIsNone(self.wait())

    def test_a_job_added_later_is_seen(self):
        self.fake({"agent get": [AGENT("working")]})
        self.assertIsNone(oke.check_pm_once("pm-toy", self.key))
        self.job("fix-b", "planning")
        self.pm_job("fix-b", 1, REPORT("question", 1))
        self.assertEqual(oke.check_pm_once("pm-toy", self.key), "report question fix-b")

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
        oke.write_json_atomic(os.path.join(oke.pm_dir(self.key), "api-retry.json"), {"attempt": 1, "delay": 30})
        self.fake({"agent get": [AGENT("idle")]})
        self.assertIsNone(self.wait())

    def test_a_pm_at_a_dialog_is_blocked(self):
        self.pm_job("fix-a", 2, REPORT("waiting", 2))
        self.fake({"agent get": [AGENT("blocked")], "agent read": [{"stdout": "Allow? 1. Yes", "exit": 0}]})
        self.assertEqual(self.wait(), "blocked" + NL + "Allow? 1. Yes")

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
        self.assertEqual(self.wait(), "idle_no_report fix-a again")
        self.assertIsNone(self.wait(8))  # the user was told; no endless wake-ups

    def test_a_new_send_for_the_job_clears_the_warnings(self):
        self.pm_job("fix-a", 3, REPORT("waiting", 2))
        self.fake({"agent get": [AGENT("idle")]})
        self.wait()
        self.wait()
        self.fake({})
        self.cli("send", "pm-toy", "--job", "fix-a", "Job fix-a: re-read your assignment")
        self.fake({"agent get": [AGENT("idle")], "agent prompt": [J({"type": "agent_prompted"})]})
        oke.deliver("pm-toy")
        self.assertEqual(self.wait(), "idle_no_report fix-a")

    def test_a_pm_without_open_jobs_ends_the_wait(self):
        self.assertEqual(self.cli("job", "phase", "fix-a", "done"), 0)
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(self.wait(), "no_jobs")

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
                                "agent get": [AGENT("working", name="pm-toy", pane="w1:p3", session="s-pm")]})

    def test_a_new_job_report_resets_the_pm_revive_count(self):
        folder = self.pm_job("fix-a", 1, REPORT("waiting", 1))
        s = oke.load_state()
        s["repos"][self.key]["pm"].update(revives=3, report_mtime=1.0)
        oke.save_state(s)
        self.pm_scenario()
        self.assertEqual(oke.revive("pm-toy"), "revived")
        pm = oke.load_state()["repos"][self.key]["pm"]
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
                   "pane split --help": [OK], "worktree create --help": [OK]})
        self.assertEqual(self.cli("status"), 0)
        out = json.loads(self.out)
        self.assertEqual(out["jobs"]["fix-a"]["pm_seq"], 4)
        self.assertTrue(out["jobs"]["fix-a"]["pm_job_dir"].endswith("/jobs/fix-a/pm"))
        self.assertNotIn("seq", out["repos"][self.key])


class TestPendingUnsent(InboxBase):
    def test_an_assignment_written_but_not_sent_is_pending(self):
        # the orchestrator was compacted between writing seq 3 and oke send --job
        self.pm_job("fix-a", 3, REPORT("waiting", 2), sent=False)
        self.fake({})
        self.cli("pending")
        items = json.loads(self.out)["items"]
        self.assertEqual([(i["job"], i["kind"]) for i in items], [("fix-a", "unsent")])

    def test_a_silent_job_after_two_warnings_is_pending(self):
        self.pm_job("fix-a", 3, REPORT("waiting", 2))
        self.fake({"agent get": [AGENT("idle")]})
        oke.wait_loop("pm-toy", None, None, poll=0, max_polls=5, jobs_key=self.key)
        oke.wait_loop("pm-toy", None, None, poll=0, max_polls=5, jobs_key=self.key)
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
        self.job("m-old", "awaiting_merge")
        self.at("m-old", 1.0)
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
                         [("d", "report_done"), ("q", "question"), ("p-new", "plan"), ("m-old", "merge")])

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
