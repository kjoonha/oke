# -*- coding: utf-8 -*-
"""The light path: a size hint and anchors from the user's words, an interview only without anchors,
per-job auto-approval, the step's elapsed time on the board, the queue order after an answer,
questions in the user's language, and the review round cap."""
import io
import json
import os
import time
import unittest

from test_slice3 import REPORT, S3Base, NL
import oke


def QUESTION(seq, body):
    return "type: question%sseq: %d%s---%s## Questions%s%s%s" % (NL, seq, NL, NL, NL, body, NL)


class TestTriage(unittest.TestCase):
    def test_size_hint_follows_the_words(self):
        self.assertEqual(oke.size_hint("quick: bump the version")[0], "small")
        self.assertEqual(oke.size_hint("just: fix it")[0], "small")
        self.assertEqual(oke.size_hint("refactor the auth module")[0], "large")
        self.assertEqual(oke.size_hint("migrate the database to postgres")[0], "large")
        self.assertEqual(oke.size_hint("look at the entire codebase for dead code")[0], "large")
        self.assertEqual(oke.size_hint(" ".join(["word"] * 201))[0], "large")
        self.assertEqual(oke.size_hint("fix the typo " + " ".join(["word"] * 60))[0], "small")
        self.assertEqual(oke.size_hint("add a csv export")[0], "small")  # 50 words or fewer
        self.assertEqual(oke.size_hint(" ".join(["word"] * 80))[0], "normal")

    def test_size_hint_reads_korean(self):
        self.assertEqual(oke.size_hint(u"README 오타 고쳐줘")[0], "small")
        self.assertEqual(oke.size_hint(u"함수 이름 바꿔줘")[0], "small")
        self.assertEqual(oke.size_hint(u"인증 모듈 리팩터링해줘")[0], "large")
        self.assertEqual(oke.size_hint(u"전체 구조를 다시 짜줘")[0], "large")
        self.assertEqual(oke.size_hint(u"구조체 필드 하나 추가해줘")[0], "small")  # a struct is not the structure

    def test_anchors_are_concrete_things(self):
        kinds = lambda words: [a["kind"] for a in oke.anchors_in(words)]
        self.assertEqual(kinds("make the report better"), [])
        self.assertEqual(kinds(u"보고서 화면을 더 좋게 해줘"), [])
        self.assertEqual(kinds("and/or w/o"), [])
        self.assertIn("path", kinds("fix src/app/report.ts"))
        self.assertIn("file", kinds(u"README.md 고쳐줘"))
        self.assertIn("symbol", kinds("rename parseQuestionText"))
        self.assertIn("symbol", kinds("fix user_id in the form"))
        self.assertIn("symbol", kinds("fix load()"))
        self.assertIn("path", kinds("look at ./build.sh"))
        self.assertIn("path", kinds("under docs/guide/intro"))
        self.assertIn("path", kinds(u"C:\\work\\a 폴더"))

    def test_plain_words_with_a_slash_or_a_dot_are_no_anchor(self):
        kinds = lambda words: [a["kind"] for a in oke.anchors_in(words)]
        for words in ("improve the UI/UX of the iOS app", "split it into client/server",
                      "input/output, read/write, on/off, yes/no and I/O", "rename parseQuestions",
                      "the iPhone layout", "move it to Node.js or Vue.js", "port it to ASP.NET",
                      "fix the login page"):
            self.assertEqual(kinds(words), [], words)
        self.assertIn("symbol", kinds("call `oke status`"))
        self.assertIn("error", kinds("it fails with KeyError on start"))
        self.assertIn("command", kinds("npm test fails"))
        self.assertIn("steps", kinds("do this:\n1. open it\n2. close it"))

    def test_an_interview_only_without_anchors(self):
        self.assertFalse(oke.triage("fix the crash in report.py")["interview"])
        self.assertTrue(oke.triage("make it nicer")["interview"])  # small, but names nothing concrete
        self.assertTrue(oke.triage("make the whole report page work the way our customers expect, "
                                   + " ".join(["more"] * 50))["interview"])

    def test_auto_is_asked_in_the_words(self):
        self.assertTrue(oke.triage(u"README.md 오타 묻지 말고 고쳐줘")["auto_asked"])
        self.assertTrue(oke.triage(u"README.md 확인 없이 바로 고쳐줘")["auto_asked"])
        self.assertTrue(oke.triage(u"승인 없이 진행해줘")["auto_asked"])
        self.assertTrue(oke.triage("fix README.md, no need to ask")["auto_asked"])
        self.assertTrue(oke.triage("fix README.md, don't ask me")["auto_asked"])
        self.assertTrue(oke.triage("fix README.md without asking me")["auto_asked"])
        self.assertFalse(oke.triage("fix README.md")["auto_asked"])

    def test_words_ending_a_sentence_still_count_as_prose(self):
        self.assertEqual(oke.hangul_share(u"고쳤다. 끝났다: 확인."), 1.0)
        self.assertEqual(oke.hangul_share(u"Done. Fixed: yes."), 0.0)
        self.assertEqual(oke.hangul_share(u"src/a.py 고쳤다"), 1.0)  # the path is no prose

    def test_korean_counts_by_words(self):
        self.assertGreaterEqual(oke.hangul_share(u"main 브랜치에 commit합니다"), oke.KOREAN_QUESTION)
        self.assertGreaterEqual(oke.hangul_share(u"commit, push, rebase 다 해도 되나요"), oke.KOREAN_QUESTION)
        self.assertLess(oke.hangul_share(u"Commit it on main and push the branch."), oke.KOREAN_QUESTION)

    def test_auto_words_that_ask_for_a_feature_are_not_auto(self):
        for words in (u"저장하면 자동으로 백업되게 해줘", u"알아서 잘 정리해줘", u"자동 완성 기능 추가",
                      u"안 물어보는 팝업 문제 고쳐줘", u"묻지 마세요 버튼 문구 바꿔줘",
                      "add an auto-save option", "the dialog should not ask twice",
                      u"확인 없이 삭제되는 버그 고쳐줘", "the app deletes files without asking",
                      "improve the UI/UX of the iOS app"):
            self.assertFalse(oke.triage(words)["auto_asked"], words)


class TriageBase(S3Base):
    def intake(self, job, words):
        self.put(os.path.join(self.home, "jobs", job, "intake.md"),
                 u"job: %s\n---\n## Desired result\nx\n## User's words\n%s\n" % (job, words))


class TestJobTriage(TriageBase):
    def test_triage_records_the_hint_and_lets_planning_start_without_an_interview(self):
        self.assertEqual(self.cli("job", "new", "fix-a", "--repo", self.repo), 0, self.err)
        self.assertNotEqual(self.cli("job", "triage", "fix-a"), 0)  # no user's words yet
        self.intake("fix-a", "fix the crash in src/report.py " + " ".join(["please"] * 60))
        self.assertEqual(self.cli("job", "triage", "fix-a"), 0, self.err)
        out = json.loads(self.out)
        self.assertEqual((out["size_hint"], out["interview"]), ("normal", False))
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["size_hint"], "normal")
        self.assertIsNone(oke.interview_problem("fix-a"))
        self.assertEqual(self.cli("job", "phase", "fix-a", "planning"), 0, self.err)
        self.assertEqual(self.cli("status"), 0, self.err)
        self.assertEqual(json.loads(self.out)["jobs"]["fix-a"]["size_hint"], "normal")

    def test_without_anchors_the_interview_runs(self):
        self.assertEqual(self.cli("job", "new", "fix-b", "--repo", self.repo), 0, self.err)
        self.intake("fix-b", "make the page feel the way our customers expect " + " ".join(["more"] * 60))
        self.assertEqual(self.cli("job", "triage", "fix-b"), 0, self.err)
        self.assertTrue(json.loads(self.out)["interview"])
        self.assertIsNotNone(oke.interview_problem("fix-b"))
        self.assertNotEqual(self.cli("job", "phase", "fix-b", "planning"), 0)

    def test_the_orchestrator_may_still_interview_an_anchored_request(self):
        self.assertEqual(self.cli("job", "new", "fix-c", "--repo", self.repo), 0, self.err)
        self.intake("fix-c", "fix report.py")
        self.assertEqual(self.cli("job", "triage", "fix-c"), 0, self.err)
        self.assertEqual(self.cli("interview", "fix-c", "--goal", 0.3, "--constraints", 0.3, "--criteria", 0.3),
                         0, self.err)
        self.assertFalse(json.loads(self.out)["done"])
        self.assertIsNotNone(oke.interview_problem("fix-c"))


class TestPmPick(TriageBase):
    def test_only_fit_names_the_one_pm_that_worked_there(self):
        st = oke.load_state()
        st["pms"]["api"] = {"pm": {}, "places": [oke.real_dir(self.repo)]}
        st["pms"]["docs"] = {"pm": {}, "places": [self.path("other")]}
        oke.save_state(st)
        self.assertEqual(self.cli("pm", "list", "--path", self.repo), 0, self.err)
        fits = dict((p["name"], p["only_fit"]) for p in json.loads(self.out))
        self.assertEqual(fits, {"api": True, "docs": False})
        st = oke.load_state()
        st["pms"]["docs"]["places"].append(oke.real_dir(self.repo))
        oke.save_state(st)
        self.assertEqual(self.cli("pm", "list", "--path", self.repo), 0, self.err)
        self.assertFalse(any(p["only_fit"] for p in json.loads(self.out)))


class TestAuto(TriageBase):
    def test_job_auto_marks_approvals_and_recommended_answers(self):
        self.job("fix-a", "awaiting_approval")
        self.pm_job("fix-a", 1, REPORT("done", 1))
        self.assertNotIn("auto", oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"]))
        self.assertEqual(self.cli("job", "auto", "fix-a"), 0, self.err)
        self.assertEqual(json.loads(self.out), {"job": "fix-a", "auto": True})
        item = oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"])
        self.assertEqual((item["kind"], item["auto"]), ("plan", "approve"))
        self.assertEqual(self.cli("status"), 0, self.err)
        self.assertTrue(json.loads(self.out)["jobs"]["fix-a"]["auto"])
        self.assertEqual(self.cli("job", "auto", "fix-a", "--off"), 0, self.err)
        self.assertNotIn("auto", oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"]))

    def test_a_question_is_taken_only_with_a_recommended_option(self):
        self.job("fix-a", "planning")
        self.assertEqual(self.cli("job", "auto", "fix-a"), 0, self.err)
        marked = NL.join(["job: add a button", "decide: which sign it shows", "options:",
                          "- minus (Recommended): the button shows -", "- word: the button shows sub"])
        self.pm_job("fix-a", 2, QUESTION(2, marked))
        item = oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"])
        self.assertEqual((item["kind"], item["auto"], item["picks"]), ("question", "recommended",
                                                                       ["minus (Recommended)"]))
        plain = marked.replace(" (Recommended)", "")
        self.pm_job("fix-a", 3, QUESTION(3, plain))
        self.assertNotIn("auto", oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"]))

    def test_a_finished_job_takes_no_auto(self):
        self.job("fix-a", "done")
        self.assertNotEqual(self.cli("job", "auto", "fix-a"), 0)

    def test_questions_only_the_user_answers_are_never_taken(self):
        self.job("fix-a", "working")
        self.assertEqual(self.cli("job", "auto", "fix-a"), 0, self.err)
        for asks in ("user_files", "review_cap", "check_accept", "head_moved"):
            body = NL.join(["job: add a button", "asks: " + asks, "decide: what happens to notes.txt",
                            "options:", "- include (Recommended): commit it with the job",
                            "- leave: keep it out of the commit"])
            self.pm_job("fix-a", 2, QUESTION(2, body))
            item = oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"])
            self.assertEqual(item["kind"], "question")
            self.assertNotIn("auto", item)
            self.assertIsNone(oke.question_problem(os.path.join(self.home, "jobs", "fix-a", "pm", "report.md")))
        bad = NL.join(["job: x", "asks: something", "decide: y", "options:", "- a: b", "- c: d"])
        self.pm_job("fix-a", 3, QUESTION(3, bad))
        self.assertIn("`asks:`", oke.question_problem(os.path.join(self.home, "jobs", "fix-a", "pm", "report.md")))

    def test_an_oke_rule_block_is_marked(self):
        self.job("fix-a", "planning")
        self.pm_job("fix-a", 1, "type: blocked%sseq: 1%srule: a job may not write outside its path%s---%s" % (
            NL, NL, NL, NL))
        item = oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"])
        self.assertEqual((item["kind"], item["oke_rule"]), ("blocked", "a job may not write outside its path"))


class TestQueueOrder(TriageBase):
    def test_an_approval_acted_on_comes_before_older_reports(self):
        self.job("old", "planning")
        self.pm_job("old", 1, REPORT("done", 1))
        report = os.path.join(self.home, "jobs", "old", "pm", "report.md")
        os.utime(report, (time.time() - 600, time.time() - 600))
        self.job("new", "awaiting_approval")
        self.pm_job("new", 1, REPORT("done", 1))
        self.assertEqual(self.cli("job", "phase", "new", "working"), 0, self.err)  # the user approved
        self.assertEqual(self.cli("pending"), 0, self.err)
        items = json.loads(self.out)["items"]
        self.assertEqual([(i["job"], i["kind"]) for i in items][:2], [("new", "unassigned"), ("old", "report_done")])


class TestBoardElapsed(TriageBase):
    def test_now_says_how_long_the_step_runs(self):
        self.job("fix-a", "planning")
        self.intake("fix-a", "fix report.py")
        self.pm_job("fix-a", 1, "type: waiting%sseq: 1%son: planner subagent a1%s---%s" % (NL, NL, NL, NL))
        report = os.path.join(self.home, "jobs", "fix-a", "pm", "report.md")
        os.utime(report, (time.time() - 12 * 60 - 5, time.time() - 12 * 60 - 5))
        self.assertEqual(self.cli("job", "auto", "fix-a"), 0, self.err)
        state = oke.load_state()
        tree = oke.board_tree(state, [])
        job = [j for repo in tree for j in repo["jobs"] if j["job"] == "fix-a"][0]
        lines = [t for _, t in oke.board_lines(tree)]
        self.assertIn(u"now: planning · 12 min, approves without asking", lines)
        self.assertTrue(job["auto"])
        self.assertEqual(oke.step_elapsed(125 * 60), "2 h 5 min")


class TestQuestionChecks(TriageBase):
    def q(self, body):
        return self.put(os.path.join(self.path("q"), "report.md"), QUESTION(1, body))

    def test_long_lines_go_back(self):
        long_decide = NL.join(["job: x", "decide: " + "y" * 201, "options:", "- a: b", "- c: d"])
        self.assertIn("at most 200", oke.question_problem(self.q(long_decide)))
        long_option = NL.join(["job: x", "decide: y", "options:", "- a: " + "b" * 200, "- c: d"])
        self.assertIn("longer than 200", oke.question_problem(self.q(long_option)))

    def test_a_korean_request_gets_korean_questions(self):
        words = u"보고서 화면에 CSV 내보내기 버튼을 추가해줘"
        english = NL.join([u"job: CSV 내보내기", "decide: where the button goes", "options:",
                           "- top: next to the title", "- bottom: under the table"])
        self.assertIn("not in the user's language", oke.question_problem(self.q(english), words))
        korean = NL.join([u"job: CSV 내보내기", u"decide: 버튼을 어디에 둘까요? `ReportPage.tsx`", "options:",
                          u"- 위 (Recommended): 제목 옆에 둡니다", u"- 아래: 표 밑에 둡니다 (src/report/table.tsx)"])
        self.assertIsNone(oke.question_problem(self.q(korean), words))
        self.assertIsNone(oke.question_problem(self.q(english), "add a csv export"))
        mixed = NL.join([u"job: CSV 내보내기", u"decide: main 브랜치에 commit합니다", "options:",
                         u"- 예: main 에 commit", u"- 아니오: branch 를 따로 만듭니다"])
        self.assertIsNone(oke.question_problem(self.q(mixed), words))

    def test_the_pm_question_reads_the_jobs_words(self):
        self.job("fix-a", "planning")
        self.intake("fix-a", u"보고서 화면에 CSV 내보내기 버튼을 추가해줘")
        self.pm_job("fix-a", 2, QUESTION(2, NL.join([u"job: CSV", "decide: where it goes", "options:",
                                                     "- top: by the title", "- bottom: under the table"])))
        self.assertIn("not in the user's language", oke.pm_question_problem("fix-a"))


class TestRoundCap(TriageBase):
    def test_reviews_stop_after_three_rounds_by_default(self):
        self.assertEqual(oke.load_config()["max_review_rounds"], 3)
        self.job("fix-a", "working")
        info = oke.load_state()["jobs"]["fix-a"]
        self.assertIsNone(oke.over_rounds("fix-a", info, oke.load_config(), 3))
        self.assertIn("past its limit of 3", oke.over_rounds("fix-a", info, oke.load_config(), 4))


if __name__ == "__main__":
    unittest.main()
