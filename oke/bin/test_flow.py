"""One job flow: a PM's waits name what they wait on, questions ask the user in a fixed form,
a request may end as an answer, a plan may have no Check command, reviews grade each criterion,
and the board speaks the user's words."""
import io
import json
import os
import re
import time
import unittest
from contextlib import redirect_stdout

from test_oke import AGENT
from test_slice3 import InboxBase, REPORT, S3Base, NL
from test_deliver import DeliverBase
import oke


def WAITING(seq, on=None):
    return "type: waiting%sseq: %d%s%s---%s" % (NL, seq, NL, ("on: %s%s" % (on, NL)) if on is not None else "", NL)


def QUESTION(seq, body):
    return "type: question%sseq: %d%s---%s## Questions%s%s%s" % (NL, seq, NL, NL, NL, body, NL)


GOOD_Q = NL.join(["job: add a subtract button", "decide: which sign the button shows", "options:",
                  "- minus: the button shows -", "- word: the button shows sub"])


class FlowWait(InboxBase):
    def setUp(self):
        InboxBase.setUp(self)
        self.grace = oke.STUCK_GRACE
        oke.STUCK_GRACE = 0
        self.look = oke.look_at

    def tearDown(self):
        oke.STUCK_GRACE = self.grace
        oke.look_at = self.look
        InboxBase.tearDown(self)

    def wait(self, polls=5):
        return oke.wait_loop("pm-toy", None, None, poll=0, max_polls=polls, jobs_key="pm-toy")

    def child(self, seq, report_type=None, report_seq=None):
        folder = os.path.join(self.home, "jobs", "fix-a", "fix-a-w1")
        self.put(os.path.join(folder, "assignment.md"),
                 "agent: fix-a-w1\nrole: worker\njob: fix-a\nseq: %d\n---\n" % seq)
        if report_type:
            self.put(os.path.join(folder, "report.md"), "type: %s\nseq: %d\n---\n" % (
                report_type, seq if report_seq is None else report_seq))


class TestWaitingOn(FlowWait):
    def test_a_wait_with_no_on_is_a_bad_report_that_says_why(self):
        self.pm_job("fix-a", 2, WAITING(2))
        self.fake({"agent get": [AGENT("idle")]})
        out = self.wait()
        self.assertTrue(out.startswith("bad_report fix-a it is waiting with no `on:`"), out)
        self.assertIn("A decision from the user is a `question` report", out)

    def test_a_wait_on_something_else_is_a_bad_report(self):
        self.pm_job("fix-a", 2, WAITING(2, "the user to pick a colour"))
        self.fake({"agent get": [AGENT("idle")]})
        out = self.wait()
        self.assertTrue(out.startswith("bad_report fix-a it is waiting on 'the user to pick a colour'"), out)
        self.assertIn("`job <other job>`", out)

    def test_every_named_form_is_a_fine_wait(self):
        self.job("fix-b", "planning")
        self.child(1)  # the worker has not reported yet
        self.fake({"agent get": [AGENT("idle")]})
        for on in ("planner subagent a1", "`work subagent` a2", "review subagent", "research subagent x",
                   "explore subagent y", "baseline", "check", "fix-a-w1", "Job fix-b"):
            self.pm_job("fix-a", 2, WAITING(2, on))
            self.assertIsNone(self.wait(), on)

    def test_a_wait_on_a_worker_that_already_reported_is_stuck(self):
        self.child(2, "done")
        self.pm_job("fix-a", 2, WAITING(2, "fix-a-w1"))
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(self.wait(), "stuck_wait fix-a fix-a-w1, which already reported done for seq 2")
        self.assertEqual(self.wait(), "stuck_wait again fix-a fix-a-w1, which already reported done for seq 2")
        self.assertIsNone(self.wait())  # told twice: `pending` keeps it in view
        self.assertEqual(oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"])["kind"], "silent")

    def test_a_report_for_an_older_seq_is_no_reason_to_be_stuck(self):
        self.child(3, "done", report_seq=2)  # a new instruction went out: the worker owes a report
        self.pm_job("fix-a", 2, WAITING(2, "fix-a-w1"))
        self.fake({"agent get": [AGENT("idle")]})
        self.assertIsNone(self.wait())

    def test_a_wait_on_a_gone_worker_is_stuck(self):
        self.child(1)
        look = self.look
        oke.look_at = lambda agent, folder: "gone" if agent == "fix-a-w1" else look(agent, folder)
        self.pm_job("fix-a", 2, WAITING(2, "fix-a-w1 (expect 10m)"))
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(self.wait(), "stuck_wait fix-a fix-a-w1, which is gone")

    def test_a_wait_on_an_agent_the_job_does_not_have_is_stuck(self):
        self.pm_job("fix-a", 2, WAITING(2, "fix-a-w9"))
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(self.wait(), "stuck_wait fix-a fix-a-w9, which is not an agent of this job")

    def test_a_wait_on_peer_answers_nobody_owes_is_stuck(self):
        self.pm_job("fix-a", 2, WAITING(2, "peer answers"))
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(self.wait(), "stuck_wait fix-a peer answers, but no PM owes this job one now")

    def test_a_wait_on_a_closed_job_is_stuck(self):
        self.pm_job("fix-a", 2, WAITING(2, "job fix-z"))
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(self.wait(), "stuck_wait fix-a job fix-z, which is no longer open")

    def test_a_stuck_wait_counts_only_after_its_grace(self):
        oke.STUCK_GRACE = 60  # the PM's own wait may be about to wake it
        self.child(2, "done")
        self.pm_job("fix-a", 2, WAITING(2, "fix-a-w1"))
        self.fake({"agent get": [AGENT("idle")]})
        self.assertIsNone(self.wait())
        self.assertIsNone(self.wait())  # a restarted wait still counts from the first look
        seen = oke.read_json(oke.stuck_path(), {})
        seen["fix-a"]["since"] -= 61
        oke.write_json_atomic(oke.stuck_path(), seen)
        self.assertTrue(self.wait().startswith("stuck_wait fix-a"))

    def test_the_same_stuck_wait_rewritten_keeps_its_grace(self):
        oke.STUCK_GRACE = 60
        self.child(2, "done")
        self.pm_job("fix-a", 2, WAITING(2, "fix-a-w1"))
        self.fake({"agent get": [AGENT("idle")]})
        self.assertIsNone(self.wait())
        seen = oke.read_json(oke.stuck_path(), {})
        seen["fix-a"]["since"] -= 61
        oke.write_json_atomic(oke.stuck_path(), seen)
        report = os.path.join(self.home, "jobs", "fix-a", "pm", "report.md")
        os.utime(report, (time.time() + 5, time.time() + 5))  # the PM wrote the same wait again
        self.assertTrue(self.wait().startswith("stuck_wait fix-a"))
        self.pm_job("fix-a", 2, WAITING(2, "fix-a-w2"))  # a wait on something else: a grace of its own
        self.assertIsNone(self.wait())
        self.child(3)  # a new instruction: the wait is fine again, and its mark is dropped
        self.pm_job("fix-a", 2, WAITING(2, "fix-a-w1"))
        self.assertIsNone(self.wait())
        self.assertNotIn("fix-a", oke.read_json(oke.stuck_path(), {}))

    def rewrite(self, text):
        report = self.put(os.path.join(self.home, "jobs", "fix-a", "pm", "report.md"), text)
        os.utime(report, (time.time() + 5, time.time() + 5))

    def test_a_pm_rewriting_a_stuck_wait_still_goes_silent(self):
        self.child(2, "done")
        self.pm_job("fix-a", 2, WAITING(2, "fix-a-w1"))
        self.fake({"agent get": [AGENT("idle")]})
        self.assertTrue(self.wait().startswith("stuck_wait fix-a"))
        self.rewrite(WAITING(2, "fix-a-w1"))  # the nudged PM writes the same wait again
        self.assertTrue(self.wait().startswith("stuck_wait again fix-a"))
        self.rewrite(WAITING(2, "fix-a-w1"))
        self.assertIsNone(self.wait())  # told twice: the user sees it
        self.assertEqual(oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"])["kind"], "silent")

    def test_a_missing_report_fixed_into_a_stuck_wait_stays_counted(self):
        self.pm_job("fix-a", 2, WAITING(1, "fix-a-w1"))
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(self.wait(), "idle_no_report fix-a")
        self.assertEqual(self.wait(), "idle_no_report again fix-a")
        self.child(2, "done")
        self.rewrite(WAITING(2, "fix-a-w1"))  # valid in form, but on a worker that already reported
        self.assertEqual(oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"])["kind"], "silent")


class TestQuestionForm(FlowWait):
    def q(self, body):
        return self.put(os.path.join(self.path("q"), "report.md"), QUESTION(1, body))

    def test_the_form_is_job_decide_and_options_with_what_happens(self):
        self.assertIsNone(oke.question_problem(self.q(GOOD_Q)))
        two = GOOD_Q + NL + NL + NL.join(["job: and a times button", "decide: where it goes", "options:",
                                         "- left: next to plus", "- right: next to minus"])
        self.assertIsNone(oke.question_problem(self.q(two)))
        self.assertEqual(len(oke.parse_questions(oke.text_section(oke.report_body(self.q(two)), "Questions"))), 2)

    def test_each_gap_is_named(self):
        self.assertIn("holds no question", oke.question_problem(self.q("which sign do you want?")))
        no_decide = NL.join(["job: add a subtract button", "options:", "- minus: -", "- word: sub"])
        self.assertIn("question 1 lacks a `decide:` line", oke.question_problem(self.q(no_decide)))
        one = NL.join(["job: add a subtract button", "decide: which sign", "options:", "- minus: shows -"])
        self.assertIn("lacks 2 or more options", oke.question_problem(self.q(one)))
        no_effect = NL.join(["job: x", "decide: which sign", "options:", "- minus", "- word"])
        self.assertIn("2 or more options", oke.question_problem(self.q(no_effect)))  # what happens is part of it
        five = NL.join(["job: x", "decide: y", "options:"] + ["- o%d: e%d" % (i, i) for i in range(5)])
        self.assertIn("more than 4 options", oke.question_problem(self.q(five)))
        no_job = NL.join(["job:", "decide: y", "options:", "- a: b", "- c: d"])
        self.assertIn("the user's words after `job:`", oke.question_problem(self.q(no_job)))

    def test_the_no_change_question_has_its_own_form(self):
        self.assertIsNone(oke.question_problem(self.q("no-change: already done; checked x. End with no change?")))

    def test_a_question_out_of_form_goes_back_to_the_pm_not_to_the_user(self):
        self.pm_job("fix-a", 2, QUESTION(2, "blue or red?"))
        self.fake({"agent get": [AGENT("idle")]})
        out = self.wait()
        self.assertTrue(out.startswith("bad_report fix-a its Questions section holds no question"), out)
        self.assertIsNone(oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"]))
        self.pm_job("fix-a", 2, QUESTION(2, GOOD_Q))
        self.assertEqual(self.wait(), "report question fix-a")
        self.assertEqual(oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"])["kind"], "question")


class TestAnswerPending(S3Base):
    def test_a_done_report_of_an_answered_job_is_an_answer_item(self):
        self.job("look", "planning")
        self.assertEqual(self.cli("job", "answer", "look"), 0, self.err)
        self.pm_job("look", 1, REPORT("done", 1))
        item = oke.pending_item("look", oke.load_state()["jobs"]["look"])
        self.assertEqual(item["kind"], "answer")
        self.assertIn("nothing changes", item["text"])
        self.assertIn("`job phase look done`", item["text"])
        self.assertEqual(oke.PENDING_RANKS["answer"], oke.PENDING_RANKS["report_done"])

    def test_a_plan_report_of_a_job_still_planning_is_no_answer(self):
        self.job("fix", "planning")
        self.pm_job("fix", 1, REPORT("done", 1))
        self.assertEqual(oke.pending_item("fix", oke.load_state()["jobs"]["fix"])["kind"], "report_done")


NO_CHECK_PLAN = (u"job: fix-a\n---\n## Acceptance criteria\n1. the README says how to install\n"
                 u"2. the README names the requirements\n## Check command\nnone (a document: nothing runs it)\n")


class TestNoCheck(DeliverBase):
    def no_check_plan(self):
        with io.open(os.path.join(self.jobs, "plan.md"), "w", encoding="utf-8") as f:
            f.write(NO_CHECK_PLAN)

    def graded(self, seq, lines, verdict="pass"):
        """A review that grades the criteria (no check row: there is no command)."""
        self.review(seq, checked=False)
        return self.report("fix-a-rv", seq, "verdict: %s\n## Acceptance criteria\n%s" % (verdict, lines))

    def test_the_check_spec_reads_a_command_or_none(self):
        self.assertEqual(oke.check_spec("fix-a"), ("command", "test -f a.txt"))
        rev = oke.criteria_rev("fix-a")
        self.no_check_plan()
        self.assertEqual(oke.check_spec("fix-a"), ("none", "a document: nothing runs it"))
        self.assertNotEqual(oke.criteria_rev("fix-a"), rev)  # a change of the check is a new plan

    def test_baseline_and_check_run_nothing(self):
        self.no_check_plan()
        for cmd in ("baseline", "check"):
            self.assertEqual(self.cli(cmd, "fix-a"), 0, self.err)
            out = json.loads(self.out)
            self.assertEqual((out["result"], out["why"], out["criteria_rev"]),
                             ("none", "a document: nothing runs it", oke.criteria_rev("fix-a")))
        self.assertEqual([r for r in self.ledger()["rows"] if r["check"] == "check"], [])

    def test_deliver_takes_the_graded_review_as_the_check(self):
        self.no_check_plan()
        self.order(self.report("fix-a-w1", 1), self.graded(1, "- 1: VERIFIED - README.md:3\n- 2: PARTIAL - no Python"))
        self.assertEqual(self.deliver(), 1)  # PARTIAL reads as changes
        self.order(self.report("fix-a-w1", 2), self.graded(2, "- 1: VERIFIED - README.md:3"))
        self.assertEqual(self.deliver(), 1)  # a criterion left ungraded
        self.assertIn("criteria 2 are not graded VERIFIED", self.err)
        self.assertIn("`- <n>: VERIFIED | PARTIAL | MISSING - <evidence>`", self.err)  # the form to use
        self.order(self.report("fix-a-w1", 3),
                   self.graded(3, "- 1: VERIFIED - README.md:3\n- 2: VERIFIED - README.md:9"))
        self.assertEqual(self.deliver(), 0, self.err)
        self.assertEqual(json.loads(self.out)["result"], "delivered")

    def test_a_plan_with_a_command_still_needs_its_check(self):
        self.order(self.report("fix-a-w1", 1), self.graded(1, "- 1: VERIFIED - a.txt"))
        self.assertEqual(self.deliver(), 1)
        self.assertIn("oke check", self.err)

    def test_the_brief_says_how_the_job_is_checked(self):
        self.assertEqual(self.cli("job", "brief", "fix-a"), 0, self.err)
        self.assertIn("checked by: `test -f a.txt`", self.out)
        self.no_check_plan()
        self.assertEqual(self.cli("job", "brief", "fix-a"), 0, self.err)
        self.assertIn("checked by review only (no command): a document: nothing runs it. The reviewer grades "
                      "each acceptance criterion; no test runs", self.out)


class TestGrades(DeliverBase):
    def verdict(self, body):
        return oke.review_verdict(self.report("fix-a-rv", 1, body))

    def test_a_partial_or_missing_criterion_is_never_a_pass(self):
        self.assertEqual(self.verdict("verdict: pass\n## Acceptance criteria\n- 1: VERIFIED - a.txt:1"), "pass")
        self.assertEqual(self.verdict("verdict: pass\n## Acceptance criteria\n- 1: VERIFIED - x\n- 2: PARTIAL - y"),
                         "changes")
        self.assertEqual(self.verdict("verdict: pass\n## Acceptance criteria\n- 1: MISSING - nothing"), "changes")
        self.assertEqual(self.verdict("verdict: changes\n## Acceptance criteria\n- 1: VERIFIED - x"), "changes")

    def test_the_older_pass_and_fail_grades_still_read(self):
        self.assertEqual(self.verdict("verdict: pass\n## Acceptance criteria\n- 1: pass - a.txt exists"), "pass")
        self.assertEqual(self.verdict("verdict: pass\n## Acceptance criteria\n- 1: fail - no a.txt"), "changes")
        self.assertEqual(self.verdict("verdict: pass"), "pass")  # no criteria section at all
        self.assertEqual(oke.review_grades(self.report("fix-a-rv", 1, "verdict: pass\n## Acceptance criteria\n"
                                                       "- **1**: **VERIFIED** - x\n- 2: fail - y")),
                         {1: "verified", 2: "fail"})


class TestBoardWords(S3Base):
    def intake(self, job, words):
        self.put(os.path.join(self.home, "jobs", job, "intake.md"),
                 u"job: %s\n---\n## Desired result\nx\n## User's words\n%s\n" % (job, words))

    def test_job_step_names_where_a_job_is_and_what_is_left(self):
        self.job("fix-a", "planning")
        info = oke.load_state()["jobs"]["fix-a"]
        step, left = oke.job_step("fix-a", info, {}, "none")
        self.assertEqual((step, left), ("investigating", ["an answer, or a plan for your approval"]))
        self.put(os.path.join(self.home, "jobs", "fix-a", "plan.md"), "job: fix-a\n---\n")
        self.assertEqual(oke.job_step("fix-a", info, {}, "none")[0], "planning")
        info = dict(info, phase="working")
        self.assertEqual(oke.job_step("fix-a", info, {"type": "waiting", "on": "fix-a-rv"}, "none")[0], "reviewing")
        self.assertEqual(oke.job_step("fix-a", info, {"type": "waiting", "on": "fix-a-w1"}, "none")[0], "working")
        self.assertEqual(oke.job_step("fix-a", info, {}, "delivered")[0], "delivering")
        self.assertEqual(oke.job_step("fix-a", dict(info, answer=True), {}, "none"), ("answering", []))

    def test_the_board_leads_with_what_waits_and_ends_with_the_last_done(self):
        self.job("fix-a", "awaiting_approval")
        self.intake("fix-a", u"add a subtract button to the calculator " + u"and more " * 20)
        self.job("old", "planning")
        self.intake("old", u"why is it slow?")
        self.assertEqual(self.cli("job", "answer", "old"), 0, self.err)
        self.pm_job("old", 1, "type: done\nseq: 1\n---\n## Summary\nthe cache is off\nmore\n")
        st = oke.load_state()
        st["jobs"]["old"]["phase"] = "done"
        oke.save_state(st)
        state = oke.load_state()
        recent = oke.recent_jobs(state)
        pm = state["jobs"]["old"]["pm"]
        self.assertEqual(recent, [{"job": "old", "pm": pm, "words": "why is it slow?", "phase": "done",
                                   "answer": True, "result": "the cache is off", "branch": None}])
        mine = [{"name": "mine", "key": pm, "pm": None, "jobs": []}]  # under its PM's section
        self.assertEqual([(d, t) for d, t in oke.board_lines(mine, recent)][1:],
                         [(0, "PM mine"), (1, "Done"),
                          (2, u"✓ You said: why is it slow?  (old, answered): the cache is off")])
        tree = [{"name": "toy", "key": "toy", "pm": None, "jobs": [
            {"job": "fix-a", "phase": "awaiting_approval", "agents": [], "pending": "plan",
             "words": oke.users_words(os.path.join(self.home, "jobs", "fix-a")), "step": "awaiting approval",
             "left": ["your approval", "work"], "path": "c:/x"}]}]
        lines = [t for _, t in oke.board_lines(tree, recent)]
        self.assertEqual(lines[0], u"To do for you: 1 approval · 0 questions")
        self.assertEqual(lines[1], "PM toy")
        self.assertTrue(lines[2].startswith(oke.SAID + "add a subtract button"), lines[2])
        self.assertTrue(lines[2].endswith(u"\u2026  (fix-a)"), lines[2])
        self.assertLessEqual(len(lines[2][len(oke.SAID):].split(u"\u2026")[0]), oke.WORDS_SHORT)
        self.assertEqual(lines[3], "now: awaiting approval, waiting for your approval of the plan")
        self.assertEqual(lines[4], "left: your approval, work")
        self.assertEqual(lines[5:], ["Done", u"\u2713 You said: why is it slow?  (old, answered): the cache is off",
                                     oke.FULL_HINT])  # something was cut: one hint names --full
        full = [t for _, t in oke.board_lines(tree, recent, full=True)]
        self.assertIn("at c:/x", full)
        self.assertTrue(any(t.startswith(oke.SAID + "add a subtract button") and u"\u2026" not in t for t in full))
        self.assertNotIn(oke.FULL_HINT, full)
        nothing_cut = [t for _, t in oke.board_lines([], recent)]
        self.assertNotIn(oke.FULL_HINT, nothing_cut)


class TestReviewRound1(S3Base):
    def plan(self, job, check):
        self.put(os.path.join(self.home, "jobs", job, "plan.md"),
                 u"job: %s\n---\n## Acceptance criteria\n1. x\n## Check command\n%s\n" % (job, check))

    def test_none_in_backticks_is_never_run_as_a_command(self):
        self.job("fix-a", "working")
        for check in ("`none (docs only)`", "`none` (docs only)", "none (docs only)", "`none (docs only)`."):
            self.plan("fix-a", check)
            self.assertEqual(oke.check_spec("fix-a"), ("none", "docs only"), check)
        self.plan("fix-a", "`none (docs only)` since nothing runs")  # its first line is more than that
        self.assertEqual(oke.check_spec("fix-a"), ("none", "docs only"))  # still never a command
        self.plan("fix-a", "`none (docs only)`")
        self.assertEqual(self.cli("baseline", "fix-a"), 0, self.err)
        self.assertEqual(json.loads(self.out)["result"], "none")

    def test_no_worktree_before_the_approval_or_for_an_answer(self):
        self.job("fix-a", "planning")
        self.assertEqual(self.cli("worktree", "fix-a"), 1)
        self.assertIn("never approved", self.err)
        self.job("look", "planning")
        self.assertEqual(self.cli("job", "answer", "look"), 0, self.err)
        self.assertEqual(self.cli("worktree", "look"), 1)
        self.assertIn("is an answer", self.err)

    def test_the_orchestrator_curates_the_shared_wiki_in_an_answered_job(self):
        self.job("tidy", "planning")
        self.assertEqual(self.cli("job", "answer", "tidy"), 0, self.err)
        self.assertEqual(self.cli("wiki", "log", "--shared", "--job", "tidy", "--note", "curated: none"), 0)
        self.assertEqual(self.cli("job", "phase", "tidy", "done"), 0, self.err)  # a shared-wiki line counts
        self.job("tidy2", "planning")
        self.assertEqual(self.cli("job", "phase", "tidy2", "done"), 1)  # no answer, no log: refused
        self.job("tidy3", "planning")
        src = self.put(self.path("src.md"), u"- a rule that holds for every PM\n")
        write = ("wiki", "write", "rules.md", "--shared", "--job", "tidy3", "--from", src, "--base", "new",
                 "--note", "n")
        self.assertEqual(self.cli(*write), 1)  # not before `job answer`
        self.assertEqual(self.cli("job", "answer", "tidy3"), 0, self.err)
        self.assertEqual(self.cli(*write), 0, self.err)
        self.assertEqual(self.cli("job", "phase", "tidy3", "done"), 0, self.err)

    def test_an_answer_goes_back_to_a_plan_while_it_plans(self):
        self.job("look", "planning")
        self.assertEqual(self.cli("job", "answer", "look"), 0, self.err)
        self.assertEqual(self.cli("job", "size", "look", "small"), 1)
        self.assertIn("--undo", self.err)
        self.assertEqual(self.cli("job", "answer", "look", "--undo"), 0, self.err)
        self.assertEqual(json.loads(self.out), {"job": "look", "answer": False})
        self.assertFalse(oke.is_answer(oke.load_state()["jobs"]["look"]))
        self.assertEqual(self.cli("job", "answer", "look", "--undo"), 0, self.err)  # again: no change
        self.assertTrue(json.loads(self.out)["unchanged"])
        self.assertEqual(self.cli("job", "size", "look", "small"), 0, self.err)
        self.assertEqual(self.cli("job", "phase", "look", "awaiting_approval"), 0, self.err)
        st = oke.load_state()
        st["jobs"]["look"]["answer"] = True  # past planning an answer stays one
        oke.save_state(st)
        self.assertEqual(self.cli("job", "answer", "look", "--undo"), 1)

    def old_research(self, job, phase):
        self.job(job, phase)
        st = oke.load_state()
        st["jobs"][job]["size"] = "research"
        oke.save_state(st)

    def test_an_older_research_job_in_intake_that_reports_done_is_an_answer(self):
        self.old_research("look", "intake")
        self.pm_job("look", 1, REPORT("done", 1))
        self.assertEqual(oke.pending_item("look", oke.load_state()["jobs"]["look"])["kind"], "answer")

    def test_an_older_research_job_has_no_plan_to_approve_until_it_plans_a_change(self):
        self.old_research("look", "planning")
        for phase in ("awaiting_approval", "working"):
            self.assertEqual(self.cli("job", "phase", "look", phase), 1, phase)
            self.assertIn("is answered", self.err)
        # its `kind: research` was read as `kind: plan`, and the planner found a change: the size it records
        # makes it a job with a plan
        self.assertEqual(self.cli("job", "size", "look", "normal"), 0, self.err)
        self.assertFalse(oke.is_answer(oke.load_state()["jobs"]["look"]))
        self.assertEqual(self.cli("job", "phase", "look", "awaiting_approval"), 0, self.err)

    def test_an_older_research_job_already_working_goes_on(self):
        self.old_research("look", "planning")
        st = oke.load_state()
        st["jobs"]["look"]["phase"] = "working"  # it started under an older OKe
        oke.save_state(st)
        self.assertEqual(self.cli("job", "phase", "look", "working"), 0, self.err)  # a dialog sets it again

    def test_the_last_done_of_one_pm_are_found_behind_many_of_others(self):
        for i in range(30):
            d = os.path.join(self.home, "archive", "other%02d-20261101-1000%02d" % (i, i))
            oke.write_json_atomic(os.path.join(d, "job.json"), {"pm": "pm-other", "phase": "done"})
        mine = os.path.join(self.home, "archive", "mine-20261001-100000")
        oke.write_json_atomic(os.path.join(mine, "job.json"), {"pm": "pm-toy", "phase": "done"})
        self.assertEqual([r["job"] for r in oke.recent_jobs(oke.load_state(), "pm-toy")], ["mine"])
        both = oke.recent_jobs(oke.load_state())  # the last ones of each PM
        self.assertEqual(len(both), oke.RECENT_DONE + 1)
        self.assertEqual(both[-1]["job"], "mine")

    def test_an_archived_job_is_read_once(self):
        d = os.path.join(self.home, "archive", "old-20261101-100000")
        oke.write_json_atomic(os.path.join(d, "job.json"), {"pm": "pm-toy", "phase": "done"})
        self.assertEqual([r["job"] for r in oke.recent_jobs(oke.load_state())], ["old"])
        reads = []
        real = oke.read_json
        oke.read_json = lambda path, default=None: reads.append(path) or real(path, default)
        try:
            self.assertEqual([r["job"] for r in oke.recent_jobs(oke.load_state())], ["old"])
        finally:
            oke.read_json = real
        self.assertFalse([r for r in reads if r.endswith("job.json")], reads)  # the index has it
        import shutil
        shutil.rmtree(d)  # gone from the archive: gone from the list
        self.assertEqual(oke.recent_jobs(oke.load_state()), [])


class TestDoneList(S3Base):
    def archived(self, name, info):
        d = os.path.join(self.home, "archive", name)
        oke.write_json_atomic(os.path.join(d, "job.json"), info)
        return os.path.join(d, "job.json")

    def test_a_rewritten_archive_is_read_again_and_an_empty_one_skipped(self):
        path = self.archived("old-20261101-100000", {"pm": "pm-toy", "phase": "done"})
        self.archived("empty-20261101-100001", {})
        self.archived("nophase-20261101-100002", {"pm": "pm-toy"})
        self.assertEqual([(r["job"], r["phase"]) for r in oke.recent_jobs(oke.load_state())], [("old", "done")])
        oke.write_json_atomic(path, {"pm": "pm-toy", "phase": "failed"})  # `job clean` rewrites it
        os.utime(path, (time.time() + 5, time.time() + 5))
        self.assertEqual([(r["job"], r["phase"]) for r in oke.recent_jobs(oke.load_state())], [("old", "failed")])

    def test_a_branch_shows_only_while_it_exists(self):
        key = oke.repo_key(self.repo)
        info = {"pm": "pm-toy", "phase": "done", "output": "code", "git_key": key,
                "worktree": {"id": None, "path": self.path("wt")}}
        self.archived("fix-x-20261101-100000", info)
        self.assertIsNone(oke.recent_jobs(oke.load_state())[0]["branch"])  # merged and deleted
        oke.git_out(self.repo, "branch", "oke/fix-y")
        self.archived("fix-y-20261101-100001", dict(info))
        self.assertEqual(oke.recent_jobs(oke.load_state())[0]["branch"], "oke/fix-y")

    def test_the_board_takes_a_broken_entry(self):
        lines = [t for _, t in oke.board_lines([], [{"pm": None}, {"job": "x", "pm": None, "words": ""}])]
        self.assertIn("Done", lines)


class TestOlderInstructions(S3Base):
    def test_research_on_job_new_is_ignored_and_working_says_what_to_do(self):
        self.job("fix-a", "working")
        pm = oke.load_state()["jobs"]["fix-a"]["pm"]
        self.assertEqual(self.cli("job", "new", "look", "--pm", pm, "--research"), 0, self.err)
        self.assertIn("job answer", json.loads(self.out)["note"])
        self.assertFalse(oke.is_answer(oke.load_state()["jobs"]["look"]))
        self.assertEqual(self.cli("interview", "look", "--goal", 1, "--constraints", 1, "--criteria", 1), 0)
        self.assertEqual(self.cli("job", "phase", "look", "working"), 1)  # the older flow's next step
        self.assertIn("`$OKE job answer look`", self.err)
        self.assertIn("awaiting_approval", self.err)
        with redirect_stdout(io.StringIO()) as shown:
            self.assertEqual(oke.main(["job", "new", "--help"]), 0)
        self.assertNotIn("--research", shown.getvalue())  # hidden from help


class TestLenientForms(S3Base):
    def q(self, body):
        return self.put(os.path.join(self.path("q"), "report.md"), QUESTION(1, body))

    def test_questions_may_be_marked_up(self):
        for body in (NL.join(["- job: add a button", "- decide: which sign", "- options:", "  - minus: shows -",
                              "  - word: shows sub"]),
                     NL.join(["**job:** add a button", "**decide:** which sign", "**options:**",
                              "- **minus**: shows -", "- **word**: shows sub"]),
                     NL.join(["1. job: add a button", "   decide: which sign", "   options:", "   1. minus: shows -",
                              "   2. word: shows sub"]),
                     NL.join(["`job:` add a button", "`decide:` which sign", "`options:`", "* minus: shows -",
                              "* word: shows sub"])):
            self.assertIsNone(oke.question_problem(self.q(body)), body)
            q = oke.parse_questions(oke.text_section(oke.report_body(self.q(body)), "Questions"))
            self.assertEqual((q[0]["job"], q[0]["decide"], q[0]["options"][0]),
                             ("add a button", "which sign", ("minus", "shows -")), body)

    def test_the_no_change_question_may_be_marked_up(self):
        for body in ("`no-change:` already done", "- no-change: already done", "**no-change:** done"):
            self.assertIsNone(oke.question_problem(self.q(body)), body)

    def test_the_hint_shows_the_form_and_the_exception(self):
        why = oke.question_problem(self.q("blue?"))
        self.assertIn("`job: <the user's own words for the job, short>` / `decide:", why)
        self.assertIn("`- <option>: <what happens if the user picks it>`", why)
        self.assertIn("`no-change:`", why)

    def grades(self, lines):
        path = self.put(self.path("rv/report.md"), u"type: done\nseq: 1\n---\nverdict: pass\n## Acceptance criteria\n"
                        + u"\n".join(lines) + u"\n")
        return oke.review_grades(path), oke.review_verdict(path)

    def test_grades_may_be_a_table_or_marked_up(self):
        grades, verdict = self.grades([u"| # | grade | evidence |", u"|---|---|---|", u"| 1 | PARTIAL | half |",
                                       u"AC2: `VERIFIED` — a.py:3", u"#3: MISSING - none", u"- **4**: **VERIFIED**",
                                       u"| 5 | Login works | `VERIFIED` | a.py:9 |"])
        self.assertEqual(grades, {1: "partial", 2: "verified", 3: "missing", 4: "verified", 5: "verified"})
        self.assertEqual(verdict, "changes")

    def test_a_restated_criterion_is_never_read_as_its_grade(self):
        for line in (u"1. Verified users can log in — MISSING", u"1. Missing files are reported: VERIFIED",
                     u"- 1: Verified users can log in — MISSING", u"- 1: Missing files are reported: VERIFIED"):
            self.assertEqual(self.grades([line])[0], {}, ascii(line))  # ungraded: a `none` job stays refused

    def test_a_criterion_graded_twice_keeps_its_worst_grade(self):
        self.assertEqual(self.grades([u"- 1: MISSING - none", u"- 1: VERIFIED - a.py:3"])[0], {1: "missing"})
        self.assertEqual(self.grades([u"- 1: VERIFIED - a", u"| 1 | PARTIAL | b |"])[0], {1: "partial"})


class TestWarnings(FlowWait):
    def rewrite(self, text):
        report = self.put(os.path.join(self.home, "jobs", "fix-a", "pm", "report.md"), text)
        os.utime(report, (time.time() + 5, time.time() + 5))

    def test_a_valid_wait_after_two_warnings_is_not_silent_and_a_stuck_one_still_shows(self):
        self.pm_job("fix-a", 2, WAITING(1, "fix-a-w1"))  # no report for seq 2
        self.fake({"agent get": [AGENT("idle")]})
        self.assertEqual(self.wait(), "idle_no_report fix-a")
        self.assertEqual(self.wait(), "idle_no_report again fix-a")
        self.assertEqual(oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"])["kind"], "silent")
        self.child(2)  # the worker works
        self.rewrite(WAITING(2, "fix-a-w1"))  # the PM wrote a right report at last
        self.assertIsNone(oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"]))
        self.assertIsNone(self.wait())
        self.child(2, "done")  # the worker reported, and the PM still waits on it
        self.assertTrue(self.wait().startswith("stuck_wait fix-a"))  # counted from one again
        self.assertTrue(self.wait().startswith("stuck_wait again fix-a"))
        self.assertEqual(oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"])["kind"], "silent")

    def test_a_bad_report_rewritten_bad_stays_counted(self):
        self.pm_job("fix-a", 2, WAITING(2))
        self.fake({"agent get": [AGENT("idle")]})
        self.assertTrue(self.wait().startswith("bad_report fix-a"))
        self.rewrite(WAITING(2, "the user"))
        self.assertTrue(self.wait().startswith("bad_report again fix-a"))

    def test_the_stuck_file_keeps_open_jobs_only(self):
        oke.STUCK_GRACE = 60
        oke.write_json_atomic(oke.stuck_path(), {"gone-job": {"mark": [1, "x", 0], "since": 1}})
        self.child(2, "done")
        self.pm_job("fix-a", 2, WAITING(2, "fix-a-w1"))
        self.fake({"agent get": [AGENT("idle")]})
        self.assertIsNone(self.wait())
        self.assertEqual(sorted(oke.read_json(oke.stuck_path(), {})), ["fix-a"])


class TestReviewRound2(S3Base):
    def plan(self, job, check, criteria=u"1. x\n"):
        self.put(os.path.join(self.home, "jobs", job, "plan.md"),
                 u"job: %s\n---\n## Acceptance criteria\n%s## Check command\n%s\n" % (job, criteria, check))

    def test_a_first_line_starting_with_none_is_never_a_command(self):
        self.job("fix-a", "working")
        for check, why in ((u"none (see `README`)", u"see `README`"),
                           (u"none (docs only) - the reviewer reads it", u"docs only"),
                           (u"`none` (a doc (one page))", u"a doc (one page)"),
                           (u"`none (docs only)`", u"docs only"),
                           (u"none (docs only)\n`make test`", u"docs only")):
            self.plan("fix-a", check)
            self.assertEqual(oke.check_spec("fix-a"), ("none", why), check)
        self.plan("fix-a", u"`nonexistent --x`")
        self.assertEqual(oke.check_spec("fix-a"), ("command", u"nonexistent --x"))

    def test_question_options_count_only_under_options(self):
        body = NL.join(["job: add a button", "- context: the old one is gone", "- note: users asked",
                        "decide: which sign", "options:", "- minus: shows -"])
        why = oke.question_problem(self.put(self.path("q/report.md"), QUESTION(1, body)))
        self.assertIn("question 1 lacks 2 or more options", why)
        q = oke.parse_questions(NL.join([body, "- word: shows sub", "job: second", "- aside: x"]))
        self.assertEqual([len(x["options"]) for x in q], [2, 0])

    def test_an_answer_undone_needs_a_new_wiki_line(self):
        self.job("look", "planning")
        self.assertEqual(self.cli("job", "answer", "look"), 0, self.err)
        self.assertEqual(self.cli("wiki", "log", "--job", "look", "--note", "none"), 0, self.err)
        self.assertEqual(self.cli("job", "answer", "look", "--undo"), 0, self.err)
        info = oke.load_state()["jobs"]["look"]
        self.assertFalse(oke.wiki.logged(oke.wiki_dir(info["pm"]), "look", oke.wiki_since(info)))
        log = os.path.join(oke.wiki_dir(info["pm"]), "log.md")
        with io.open(log, encoding="utf-8") as f:
            old = re.sub(r"(?m)^\S+ look: ", "2000-01-01T00:00:00Z look: ", f.read())
        with io.open(log, "w", encoding="utf-8") as f:
            f.write(old)  # the answer's line, long before
        st = oke.load_state()
        st["jobs"]["look"]["answer_undone"] -= 5  # the change's lesson comes later
        oke.save_state(st)
        info = oke.load_state()["jobs"]["look"]
        self.assertFalse(oke.wiki.logged(oke.wiki_dir(info["pm"]), "look", oke.wiki_since(info)))
        self.assertEqual(self.cli("wiki", "log", "--job", "look", "--note", "the change"), 0, self.err)
        self.assertTrue(oke.wiki.logged(oke.wiki_dir(info["pm"]), "look", oke.wiki_since(info)))

    def test_an_older_research_job_at_work_plans_again_with_no_write_area(self):
        self.job("look", "planning")
        st = oke.load_state()
        st["jobs"]["look"].update(size="research", phase="working", approved=True, worked=True,
                                  path=st["jobs"]["look"].get("path") or self.repo)  # an older OKe set these
        oke.save_state(st)
        self.assertEqual(self.cli("job", "size", "look", "normal"), 0, self.err)
        self.assertEqual(json.loads(self.out), {"job": "look", "size": "normal", "phase": "planning"})
        info = oke.load_state()["jobs"]["look"]
        self.assertFalse(oke.is_answer(info))
        self.assertFalse(info.get("approved") or info.get("worked"))
        self.assertEqual(oke.write_areas(oke.load_state(), "pm", info["pm"]), [])  # nothing before the approval

    def test_an_experiment_never_goes_on_with_none(self):
        self.job("sim", "planning")
        st = oke.load_state()
        st["jobs"]["sim"]["output"] = "result"
        oke.save_state(st)
        self.plan("sim", u"none (nothing to run)")
        self.assertEqual(self.cli("job", "phase", "sim", "awaiting_approval"), 1)
        self.assertIn("cannot be `none`", self.err)
        for phase in ("working", "reviewing", "done"):
            st = oke.load_state()
            st["jobs"]["sim"]["phase"] = {"working": "awaiting_approval", "reviewing": "working",
                                         "done": "reviewing"}[phase]
            oke.save_state(st)
            self.assertEqual(self.cli("job", "phase", "sim", phase), 1, phase)
            self.assertIn("cannot be `none`", self.err)
        self.job("fix-c", "planning")
        self.plan("fix-c", u"none (docs only)")
        st = oke.load_state()
        st["jobs"]["fix-c"]["phase"] = "awaiting_approval"
        oke.save_state(st)
        self.assertEqual(self.cli("job", "output", "fix-c", "result"), 1)  # it would be an experiment with none
        self.assertIn("cannot be `none`", self.err)

    def test_the_shared_wiki_curation_is_no_pms_job(self):
        self.job("fix-a", "working")
        pm = oke.load_state()["jobs"]["fix-a"]["pm"]
        self.assertEqual(self.cli("job", "new", "tidy", "--pm", pm, "--shared-wiki"), 0, self.err)
        st = oke.load_state()
        self.assertTrue(st["jobs"]["tidy"]["orchestrator_job"])
        self.assertEqual(oke.open_jobs(st, pm), ["fix-a"])
        self.assertEqual(oke.pm_jobs(st, pm), ["fix-a"])
        self.assertNotIn("tidy", [j["job"] for repo in oke.board_tree(st, []) for j in repo["jobs"]])
        self.assertIn("tidy", oke.open_jobs(st))  # still the orchestrator's open job
        self.assertEqual(self.cli("job", "new", "tidy2", "--pm", pm, "--shared-wiki", "--path", self.repo), 1)


if __name__ == "__main__":
    unittest.main()
