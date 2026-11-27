"""The wiki: caps, read before write, duplicates, usage, archiving, and the gates."""
import io
import json
import glob
import os
import shutil

from test_oke import put_pm, J, SpawnBase, help_ok, spawn_ok  # noqa: F401
import oke
import runtime_herdr
import wiki


class TestWiki(SpawnBase):
    def setUp(self):
        SpawnBase.setUp(self)
        self.repo = self.git_repo("r")
        self.pm = "pm-r"
        self.assertEqual(self.cli("job", "new", "fix-a", "--repo", self.repo), 0, self.err)
        self.dir = oke.wiki_dir(self.pm)

    def page(self, name, text, base="new", *extra):
        src = self.write("src.md", text)
        return self.cli("wiki", "write", name, "--repo", self.repo, "--from", src, "--base", base,
                        "--note", "n", *extra)

    def base(self, name):
        self.assertEqual(self.cli("wiki", "read", name, "--repo", self.repo), 0, self.err)
        return json.loads(self.out.splitlines()[0])["base"]

    def test_a_write_starts_from_the_current_text(self):
        self.assertEqual(self.page("build.md", "- run make test before a commit\n"), 0, self.err)
        self.assertEqual(self.page("build.md", "- other\n"), 1)  # "new", but it exists
        self.assertIn("did not read it", self.err)
        self.assertEqual(self.page("build.md", "- other\n", self.base("build.md")), 0, self.err)
        self.assertIn("curate: build.md: n", io.open(os.path.join(self.dir, "log.md"), encoding="utf-8").read())

    def test_over_the_cap_is_refused(self):
        self.assertEqual(self.page("big.md", "x" * (wiki.PAGE_CHARS + 1)), 1)
        self.assertIn("over its cap", self.err)
        self.assertEqual(self.page("index.md", "x" * (wiki.INDEX_CHARS + 1)), 1)

    def test_a_reworded_line_of_another_page_is_refused(self):
        self.assertEqual(self.page("build.md", "- run `make test` before every commit\n"), 0, self.err)
        self.assertEqual(self.page("tests.md", "- run make test before each commit\n"), 1)
        self.assertIn("build.md", self.err)
        self.assertEqual(self.page("tests.md", "- run make test before each commit\n", "new", "--dup-ok"), 0)

    def test_lessons_wait_for_the_delivery(self):
        src = self.write("src.md", "- a fact about the build system\n")
        args = ("wiki", "write", "build.md", "--job", "fix-a", "--from", src, "--base", "new", "--note", "n")
        self.assertEqual(self.cli(*args), 1)
        self.assertIn("not checked", self.err)
        oke.ledger_add("fix-a", "oke", "abc", "deliver", "ok", "branch=oke/fix-a base=master@abc")
        self.assertEqual(self.cli(*args), 0, self.err)

    def test_the_shared_wiki_takes_no_repository_path(self):
        src = self.write("src.md", "- the tests of %s are slow\n" % self.repo.replace("\\", "/"))
        self.assertEqual(self.cli("wiki", "write", "x.md", "--shared", "--from", src, "--base", "new",
                                  "--note", "n"), 1)
        self.assertIn("beyond one PM", self.err)

    def test_reports_count_use_and_old_pages_are_archived_not_deleted(self):
        self.assertEqual(self.page("build.md", "- run make test before a commit\n"), 0, self.err)
        self.assertEqual(self.page("old.md", "- something nobody needs any more at all\n"), 0, self.err)
        self.assertEqual(self.page("index.md", "- [build](build.md)\n- [old](old.md)\n"), 0, self.err)
        self.write("home/jobs/fix-a/fix-a-w1/report.md",
                   "type: done\n---\n## Summary\nok\n## wiki\n- build.md: wrong: it is make check now\n- x.md: ok\n")
        self.assertEqual(self.archive("fix-a"), 0, self.err)
        usage = wiki.load_usage(self.dir)
        self.assertEqual((usage["build.md"]["uses"], usage["build.md"]["wrong"]), (1, 1))
        later = wiki.time.time() + 31 * wiki.DAY
        usage["build.md"]["used"] = later
        wiki.save_usage(self.dir, usage)
        self.assertEqual(wiki.prune(self.dir, False, later), ["old.md"])
        self.assertEqual(wiki.prune(self.dir, True, later), ["old.md"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "old.md")))
        self.assertEqual(len(os.listdir(os.path.join(self.dir, ".archive"))), 1)
        self.assertNotIn("old.md", io.open(os.path.join(self.dir, "index.md"), encoding="utf-8").read())

    def test_a_pinned_page_stays(self):
        self.assertEqual(self.page("keep.md", "- a rule that holds for years and years\n"), 0, self.err)
        self.assertEqual(self.cli("wiki", "pin", "keep.md", "--repo", self.repo), 0, self.err)
        self.assertEqual(wiki.prune(self.dir, True, wiki.time.time() + 99 * wiki.DAY), [])

    def test_the_archive_says_when_a_curation_is_due(self):
        for i in range(oke.CURATE_EVERY):
            wiki.log(self.dir, "job%d" % i, "none")
        self.assertEqual(self.archive("fix-a"), 0, self.err)
        self.assertIn("kind: curate", self.out)
        wiki.log(self.dir, "curate", "merged two pages")
        self.assertEqual(self.cli("job", "new", "fix-b", "--repo", self.repo), 0, self.err)
        self.assertEqual(self.archive("fix-b"), 0, self.err)
        self.assertNotIn("curate", self.out)

    def test_merging_a_page_may_repeat_its_lines(self):
        self.assertEqual(self.page("a.md", "- run make test before every single commit\n"), 0, self.err)
        self.assertEqual(self.page("b.md", "- run make test before every single commit\n"), 1)
        self.assertEqual(self.page("b.md", "- run make test before every single commit\n", "new",
                                   "--merging", "a.md"), 0, self.err)

    def test_a_page_without_history_is_not_due_at_once(self):
        path = self.write("home/wiki/pm-r/old.md", "- written long before usage was kept\n")
        os.utime(path, (1, 1))
        self.assertEqual(wiki.prune(self.dir, False), [])
        self.assertEqual(wiki.prune(self.dir, False, wiki.time.time() + 31 * wiki.DAY), ["old.md"])

    def test_done_needs_a_log_line_of_this_job_not_of_an_earlier_one(self):
        folder = self.path("plain")
        os.makedirs(folder)
        self.assertEqual(self.cli("job", "new", "calc", "--repo", folder), 0, self.err)
        self.assertEqual(self.cli("job", "new", "calc2", "--repo", folder), 1)  # one direct job at a time
        self.advance("calc", "working")
        self.logged("calc")
        self.assertEqual(self.cli("job", "phase", "calc", "done"), 0, self.err)
        self.assertEqual(self.archive("calc"), 0, self.err)
        self.assertEqual(self.cli("job", "new", "calc", "--repo", folder), 1)  # an archived name stays taken
        for old in glob.glob(os.path.join(oke.oke_home(), "archive", "calc-*")):
            shutil.rmtree(old)  # the user removed the archive: the name is free, its wiki log line is not
        self.assertEqual(self.cli("job", "new", "calc", "--repo", folder), 0, self.err)
        state = oke.load_state()
        state["jobs"]["calc"]["created"] += 2  # the old line is now older than the job
        oke.save_state(state)
        self.assertEqual(self.cli("interview", "calc", "--goal", 1, "--constraints", 1, "--criteria", 1), 0)
        for step in ("planning", "awaiting_approval", "working"):
            self.assertEqual(self.cli("job", "phase", "calc", step), 0, self.err)
        self.assertEqual(self.cli("job", "phase", "calc", "done"), 1)
        self.assertIn("has not recorded", self.err)

    def test_a_direct_jobs_lessons_wait_for_a_passing_review(self):
        folder = self.path("plain")
        os.makedirs(folder)
        self.assertEqual(self.cli("job", "new", "calc", "--repo", folder), 0, self.err)
        src = self.write("src.md", "- a fact about the calculator code\n")
        args = ("wiki", "write", "calc.md", "--job", "calc", "--from", src, "--base", "new", "--note", "n")
        self.assertEqual(self.cli(*args), 1)
        self.write("home/jobs/calc/calc-rv/report.md", "type: done\nseq: 1\n---\nverdict: changes\n")
        self.assertEqual(self.cli(*args), 1)
        self.write("home/jobs/calc/calc-rv/report.md", "type: done\nseq: 1\n---\nverdict: pass\n")
        self.assertEqual(self.cli(*args), 0, self.err)

    def test_a_folder_git_refuses_is_not_a_direct_job(self):
        folder = self.path("broken")
        os.makedirs(os.path.join(folder, ".git"))
        os.makedirs(os.path.join(folder, "src"))
        for path in (folder, os.path.join(folder, "src")):
            with self.assertRaises(ValueError):
                oke.repo_key(path)

    def test_status_says_direct_and_an_answer_may_look_while_a_direct_job_runs(self):
        folder = self.path("plain")
        os.makedirs(folder)
        self.assertEqual(self.cli("job", "new", "calc", "--repo", folder), 0, self.err)
        self.assertEqual(self.cli("job", "new", "look", "--pm", self.pm), 0, self.err)
        self.assertEqual(self.cli("interview", "look", "--goal", 1, "--constraints", 1, "--criteria", 1), 0)
        self.assertEqual(self.cli("job", "phase", "look", "planning"), 0, self.err)
        self.assertEqual(self.cli("job", "path", "look", folder), 0, self.err)  # it only reads while it plans
        self.assertEqual(self.cli("job", "answer", "look"), 0, self.err)
        self.assertEqual(self.cli("job", "path", "look", folder), 0, self.err)  # an answer only reads
        os.environ["HERDR_ENV"] = "1"
        os.environ["HERDR_PANE_ID"] = "w7:p1"
        fake = help_ok()
        fake["agent list"] = [J({"agents": []})]
        self.fake(fake)
        self.assertEqual(self.cli("status"), 0, self.err)
        jobs = json.loads(self.out)["jobs"]
        self.assertEqual((jobs["calc"]["direct"], jobs["look"]["output"]), (True, "result"))

    def test_status_names_the_shared_wiki_curation_the_orchestrators(self):
        self.assertEqual(self.cli("job", "new", "tidy", "--pm", self.pm, "--shared-wiki"), 0, self.err)
        self.assertEqual(self.cli("job", "new", "fix-a", "--pm", self.pm), 0, self.err)
        os.environ["HERDR_ENV"] = "1"
        os.environ["HERDR_PANE_ID"] = "w7:p1"
        fake = help_ok()
        fake["agent list"] = [J({"agents": []})]
        self.fake(fake)
        self.assertEqual(self.cli("status"), 0, self.err)
        jobs = json.loads(self.out)["jobs"]
        self.assertEqual(jobs["tidy"]["class"], "orchestrator")  # never resumed, restarted or asked about
        self.assertNotEqual(jobs["fix-a"]["class"], "orchestrator")

    def test_the_shared_wiki_catches_git_bash_paths_and_ignores_a_root(self):
        if not oke.os.name == "nt":
            self.skipTest("drive letters")
        bash = "/" + self.repo[0].lower() + self.repo[2:].replace("\\", "/")
        src = self.write("src.md", "- see %s/Makefile\n" % bash)
        self.assertEqual(self.cli("wiki", "write", "x.md", "--shared", "--from", src, "--base", "new",
                                  "--note", "n"), 1)
        state = oke.load_state()
        state["jobs"]["drive"] = {"pm": self.pm, "path": "c:/", "git_key": "c:/.git", "phase": "planning"}
        oke.save_state(state)
        src = self.write("src.md", "- deploys use c:/tools only on a Monday\n")
        self.assertEqual(self.cli("wiki", "write", "y.md", "--shared", "--from", src, "--base", "new",
                                  "--note", "n"), 0, self.err)

    def test_a_curation_job_resets_the_count(self):
        for i in range(oke.CURATE_EVERY):
            wiki.log(self.dir, "job%d" % i, "none")
        wiki.log(self.dir, "tidy", "curated: merged two pages")
        self.assertEqual(self.archive("fix-a"), 0, self.err)
        self.assertNotIn("curate", self.out)

    def test_cancel_closes_the_job_and_drops_only_its_mail(self):
        key = oke.repo_key(self.repo)
        oke.write_json_atomic(oke.children_path(self.pm), {"fix-a-w1": {"job": "fix-a", "role": "worker"}})
        pm = self.pm
        state = oke.load_state()
        put_pm(state, key, {"agent": pm, "pane": "w1:p9"})
        oke.save_state(state)
        self.fake({})
        self.assertEqual(self.cli("send", pm, "Job fix-a: assignment updated (seq 2)"), 0, self.err)
        self.assertEqual(self.cli("send", pm, "Job fix-b: go on"), 0, self.err)
        self.write(os.path.join(oke.inbox_dir("fix-a-w1"), "1.txt"), "Assignment updated")
        self.assertEqual(self.cli("job", "cancel", "fix-a"), 0, self.err)
        self.assertEqual(json.loads(self.out.strip().splitlines()[-1])["dropped_messages"], 2)
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["phase"], "failed")
        texts = [t.split(": ", 1)[1] for _, t in oke.inbox_messages(pm)]
        self.assertEqual(texts[0], "Job fix-b: go on")  # the PM still hears of the cancel itself
        self.assertTrue(texts[1].startswith("Job fix-a was cancelled"))
        self.assertFalse(oke.should_be_alive(oke.load_state(), "pm", self.pm, "pm-x", {}))

    def test_a_job_without_a_branch_cannot_go_on_hold_once_it_worked(self):
        self.advance("fix-a", "awaiting_approval")
        self.fake({})
        # never worked: holdable, even with the user's own edits in main
        self.write(os.path.join(self.repo, "a.txt"), "a")
        self.assertEqual(self.cli("job", "hold", "fix-a"), 0, self.err)
        held_at = oke.load_state()["jobs"]["fix-a"]["held_at"]
        # held again (to retry the closes): no second notice, the same held_at
        self.assertEqual(self.cli("job", "hold", "fix-a"), 0, self.err)
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["held_at"], held_at)
        self.assertEqual(self.cli("job", "unhold", "fix-a"), 0, self.err)
        os.remove(os.path.join(self.repo, "a.txt"))
        # worked without a branch (a PM subagent the hold cannot close may still write to
        # main): refused whatever main holds now
        self.advance("fix-a", "working")
        self.assertEqual(self.cli("job", "hold", "fix-a"), 1)
        self.assertIn("no branch oke/fix-a", self.err)
        # the plan uses a worktree (the PM died before making it): main's edits are the user's
        plan = os.path.join(oke.oke_home(), "jobs", "fix-a", "plan.md")
        self.write(plan, "job: fix-a\n---\n## Approach\nx\n## Workspace\nnew worktree (branch oke/fix-a)\n## Steps\nx\n")
        self.assertEqual(self.cli("job", "hold", "fix-a"), 0, self.err)
        self.assertEqual(self.cli("job", "unhold", "fix-a"), 0, self.err)
        self.write(plan, "job: fix-a\n---\n## Workspace\nmain checkout\n## Steps\noke/fix-a later\n")
        self.assertEqual(self.cli("job", "hold", "fix-a"), 1)
        self.git(self.repo, "branch", "oke/fix-a")
        self.assertEqual(self.cli("job", "hold", "fix-a"), 0, self.err)

    def test_a_re_plan_after_work_does_not_count_as_early(self):
        self.advance("fix-a", "working")
        self.write(os.path.join(self.repo, "a.txt"), "a\n")
        self.git(self.repo, "add", "a.txt")  # the work already edited main
        self.assertEqual(self.cli("job", "phase", "fix-a", "planning"), 0, self.err)  # the user wants changes
        self.fake({})
        self.assertEqual(self.cli("job", "hold", "fix-a"), 1)
        self.assertIn("no branch oke/fix-a", self.err)

    def test_unhold_asks_a_deferred_question_again(self):
        self.advance("fix-a", "awaiting_approval")
        self.assertEqual(self.cli("job", "defer", "fix-a"), 0, self.err)
        self.fake({})
        self.assertEqual(self.cli("job", "hold", "fix-a"), 0, self.err)
        self.assertEqual(self.cli("job", "unhold", "fix-a"), 0, self.err)
        self.assertFalse(oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"])["deferred"])

    def test_nothing_of_a_held_job_starts(self):
        self.advance("fix-a", "working")
        self.git(self.repo, "branch", "oke/fix-a")
        self.assertEqual(self.cli("job", "hold", "fix-a"), 0, self.err)
        self.fake(spawn_ok())
        self.assertEqual(self.cli("spawn", self.assignment("fix-a-w1", "worker", "fix-a"),
                                  "--split-from", "w1:p1"), 1)
        self.assertIn("on hold", self.err)
        # a fresh PM hears of the hold it never got as a message
        self.assertEqual(self.cli("spawn", self.assignment("pm-r", "pm"), "--pane", "w1:p9"), 0, self.err)
        self.assertIn("on hold: fix-a", self.argv_of("agent", "prompt")[0][3])
        state = oke.load_state()
        state["jobs"]["fix-a"].pop("held")
        state["jobs"]["fix-a"]["phase"] = "done"
        oke.save_state(state)
        self.assertEqual(self.cli("job", "hold", "fix-a"), 1)

    def test_a_hold_while_a_worker_starts_closes_it(self):
        # the hold closed the job's agents before this one had a record
        self.advance("fix-a", "working")
        self.git(self.repo, "branch", "oke/fix-a")
        start = runtime_herdr.start_agent

        def hold_then_start(*a, **k):
            self.assertEqual(self.cli("job", "hold", "fix-a"), 0, self.err)
            return start(*a, **k)
        runtime_herdr.start_agent = hold_then_start
        self.addCleanup(setattr, runtime_herdr, "start_agent", start)
        scenario = spawn_ok()
        scenario.update({"pane get": [J({"pane": {"pane_id": "w1:p9", "terminal_id": "t9"}})],
                         "pane close": [J({"type": "ok"})]})
        self.fake(scenario)
        self.assertEqual(self.cli("spawn", self.assignment("fix-a-w1", "worker", "fix-a"),
                                  "--split-from", "w1:p1"), 1)
        self.assertIn("went on hold", self.err)
        self.assertEqual(self.argv_of("pane", "close")[0][2], "w1:p9")
        with io.open(oke.children_path(self.pm), encoding="utf-8") as f:
            self.assertIsNone(json.load(f)["fix-a-w1"]["pane"])

    def test_a_question_the_pm_wrote_while_held_does_not_come_back_with_the_job(self):
        # the hold closed the worker; the PM's wait said gone before the notice reached it
        self.fake({})
        self.assertEqual(self.cli("job", "hold", "fix-a"), 0, self.err)
        folder = oke.pm_job_dir("fix-a")
        os.makedirs(folder, exist_ok=True)
        with io.open(os.path.join(folder, "assignment.md"), "w", encoding="utf-8") as f:
            f.write(u"agent: pm-r\nrole: pm\nseq: 1\n---\n")
        state = oke.load_state()
        state["jobs"]["fix-a"]["sent_seq"] = 1
        state["jobs"]["fix-a"]["held_at"] -= 5
        oke.save_state(state)
        with io.open(os.path.join(folder, "report.md"), "w", encoding="utf-8") as f:
            f.write(u"type: question\nseq: 1\n---\nfix-a-w1 died: revive or give up?\n")
        self.assertEqual(self.cli("job", "unhold", "fix-a"), 0, self.err)
        self.assertIsNone(oke.pending_item("fix-a", oke.load_state()["jobs"]["fix-a"]))
        self.assertEqual(oke.pm_job_report("fix-a")[1], {})  # a PM wait has nothing to return
        self.assertTrue(os.path.exists(os.path.join(folder, "report-while-held.md")))
        # a done report the PM finished before it read the hold notice stays
        self.assertEqual(self.cli("job", "hold", "fix-a"), 0, self.err)
        state = oke.load_state()
        state["jobs"]["fix-a"]["held_at"] -= 5
        oke.save_state(state)
        with io.open(os.path.join(folder, "report.md"), "w", encoding="utf-8") as f:
            f.write(u"type: done\nseq: 1\n---\nplan written\n")
        self.assertEqual(self.cli("job", "unhold", "fix-a"), 0, self.err)
        self.assertEqual(oke.pm_job_report("fix-a")[1].get("type"), "done")

    def test_a_held_job_is_left_alone_until_it_comes_back(self):
        key = oke.repo_key(self.repo)
        self.assertEqual(self.cli("job", "new", "fix-b", "--repo", self.repo), 0, self.err)
        oke.write_json_atomic(oke.children_path(self.pm), {"fix-a-w1": {"job": "fix-a", "role": "worker"}})
        pm = self.pm
        state = oke.load_state()
        put_pm(state, key, {"agent": pm, "pane": "w1:p9"})
        oke.save_state(state)
        self.fake({})
        self.assertEqual(self.cli("send", pm, "Job fix-a: assignment updated (seq 2)"), 0, self.err)
        # the notice is queued before the slow agent close: a PM wait that sees no open job
        # meanwhile still has mail, so it does not end with no_jobs and lose the notice
        cancel = oke.cancel_children
        seen = []
        oke.cancel_children = lambda *a: seen.append([t for _, t in oke.inbox_messages(pm)]) or cancel(*a)
        self.addCleanup(setattr, oke, "cancel_children", cancel)
        self.assertEqual(self.cli("job", "hold", "fix-a", "--reason", "later"), 0, self.err)
        self.assertTrue(any("Job fix-a is on hold" in t for t in seen[0]))
        state = oke.load_state()
        self.assertEqual(state["jobs"]["fix-a"]["held"], "later")
        self.assertEqual(oke.open_jobs(state, self.pm), ["fix-b"])
        self.assertEqual(oke.held_jobs(state, self.pm), ["fix-a"])
        # its own agents are not revived; the PM is, for fix-b, and hears to leave fix-a alone
        self.assertFalse(oke.should_be_alive(state, "worker", self.pm, "fix-a-w1", {"job": "fix-a"}))
        self.assertTrue(oke.should_be_alive(state, "pm", self.pm, pm, {}))
        self.assertIn("on hold: fix-a", oke.pm_jobs_note(state, self.pm))
        texts = [t.split(": ", 1)[1] for _, t in oke.inbox_messages(pm)]
        self.assertEqual(len(texts), 1)  # the old assignment notice went; the hold notice stays
        self.assertTrue(texts[0].startswith("Job fix-a is on hold"))
        board_job = oke.board_tree(state, [])[0]["jobs"][0]
        self.assertEqual(board_job["held"], True)
        self.assertEqual(board_job["agents"][0]["status"], "off")  # closed by the hold, not dead
        rec = oke.read_children(self.pm)["fix-a-w1"]
        state["jobs"]["fix-a"].pop("held")  # back: off until the PM revives it
        self.assertEqual(oke.board_tree(state, [])[0]["jobs"][0]["agents"][0]["status"], "off")
        oke.update_record("worker", self.pm, "fix-a-w1", {"pane": "w1:p5"})  # a pane it died in: dead
        self.assertEqual(oke.board_tree(state, [])[0]["jobs"][0]["agents"][0]["status"], "dead")
        oke.update_record("worker", self.pm, "fix-a-w1", {"pane": rec.get("pane")})
        state["jobs"]["fix-a"]["held"] = "later"
        # nothing asks for it, and a PM that missed the notice cannot move it on
        self.assertIsNone(oke.pending_item("fix-a", state["jobs"]["fix-a"]))
        self.assertEqual(self.cli("job", "phase", "fix-a", "planning"), 1)
        self.assertIn("on hold", self.err)
        self.assertEqual(self.cli("job", "deliver", "fix-a"), 1)
        self.assertIn("on hold", self.err)
        # with fix-b gone too, nothing keeps the PM alive
        state["jobs"]["fix-b"]["phase"] = "done"
        oke.save_state(state)
        self.assertFalse(oke.should_be_alive(oke.load_state(), "pm", self.pm, pm, {}))
        self.assertEqual(oke.board_tree(oke.load_state(), [])[0]["pm"]["status"], "off")  # not stuck
        # back: open again, and the unread hold notice is dropped
        self.assertEqual(self.cli("job", "unhold", "fix-a"), 0, self.err)
        self.assertEqual(json.loads(self.out)["pm"], pm)
        state = oke.load_state()
        self.assertNotIn("held", state["jobs"]["fix-a"])
        self.assertEqual(oke.open_jobs(state, self.pm), ["fix-a"])
        self.assertEqual(oke.inbox_messages(pm), [])
        self.assertEqual(self.cli("job", "unhold", "fix-a"), 1)
        state["jobs"]["fix-a"]["phase"] = "done"
        oke.save_state(state)
        self.assertEqual(self.cli("job", "hold", "fix-a"), 1)  # a finished job has nothing to hold

    def test_cancel_closes_only_panes_still_the_childs_and_never_a_done_job(self):
        key = oke.repo_key(self.repo)
        oke.write_json_atomic(oke.children_path(self.pm), {
            "fix-a-w1": {"job": "fix-a", "role": "worker", "pane": "w1:p1", "terminal": "t1"},
            "fix-a-rv": {"job": "fix-a", "role": "reviewer", "pane": "w1:p2", "terminal": "t2"}})
        closed = []

        class Stub(object):
            def place_id(self, pane):
                return {"w1:p1": "t1", "w1:p2": "someone-else"}[pane]  # p2 was reused

            def close(self, pane):
                closed.append(pane)
        real = oke.runtime
        oke.runtime = lambda *a: Stub()
        try:
            self.assertEqual(self.cli("job", "cancel", "fix-a"), 0, self.err)
        finally:
            oke.runtime = real
        self.assertEqual(closed, ["w1:p1"])
        self.assertIsNone(oke.read_children(self.pm)["fix-a-rv"]["pane"])
        state = oke.load_state()
        state["jobs"]["fix-a"]["phase"] = "done"
        oke.save_state(state)
        self.assertEqual(self.cli("job", "cancel", "fix-a"), 1)
        state["jobs"]["fix-a"]["phase"] = "reviewing"  # delivered but not done: the branch stays, the job ends
        oke.save_state(state)
        oke.ledger_add("fix-a", "oke", "abc", "deliver", "ok", "branch=oke/fix-a base=master@abc")
        self.assertEqual(self.cli("job", "cancel", "fix-a"), 0, self.err)

    def test_archive_closes_the_panes_a_pm_left_open(self):
        # live: a PM skipped closing a finished job's worker; archive closes it
        key = oke.repo_key(self.repo)
        oke.write_json_atomic(oke.children_path(self.pm), {
            "fix-a-w1": {"job": "fix-a", "role": "worker", "pane": "w1:p1", "terminal": "t1"},
            "fix-b-w1": {"job": "fix-b", "role": "worker", "pane": "w1:p3", "terminal": "t3"}})
        closed = []
        folder = os.path.join(self.home, "jobs", "fix-a")

        class Stub(object):
            def place_id(self, pane):
                return {"w1:p1": "t1", "w1:p3": "t3"}[pane]

            def close(self, pane):
                closed.append((pane, os.path.isdir(folder)))
        state = oke.load_state()
        state["jobs"]["fix-a"]["phase"] = "done"
        oke.save_state(state)
        real = oke.runtime
        oke.runtime = lambda *a: Stub()
        try:
            self.assertEqual(self.archive("fix-a"), 0, self.err)
        finally:
            oke.runtime = real
        # only this job's child, before its folder moves (or its next write brings it back)
        self.assertEqual(closed, [("w1:p1", True)])
        # its record goes too: a later job of the same name must not inherit it
        self.assertEqual(list(oke.read_children(self.pm)), ["fix-b-w1"])
        self.assertNotIn("fix-a", oke.load_state()["jobs"])

    def test_archive_reports_what_an_outage_left_and_still_succeeds(self):
        key = oke.repo_key(self.repo)
        oke.write_json_atomic(oke.children_path(self.pm), {
            "fix-a-w1": {"job": "fix-a", "role": "worker", "pane": "w1:p1", "terminal": "t1"}})

        class Stub(object):
            def place_id(self, pane):
                return None  # herdr answers None in an outage too

            def place_exists(self, pane):
                raise oke.HerdrError("herdr_unavailable", "down")
        real = oke.runtime
        oke.runtime = lambda *a: Stub()
        try:
            self.assertEqual(self.archive("fix-a"), 0, self.err)
        finally:
            oke.runtime = real
        self.assertIn("not_closed: fix-a-w1", self.out)
        self.assertEqual(oke.read_children(self.pm), {})
        self.assertNotIn("fix-a", oke.load_state()["jobs"])

    def test_archive_reports_a_pane_that_refuses_to_close(self):
        key = oke.repo_key(self.repo)
        oke.write_json_atomic(oke.children_path(self.pm), {
            "fix-a-w1": {"job": "fix-a", "role": "worker", "pane": "w1:p1", "terminal": "t1"}})

        class Stub(object):
            def place_id(self, pane):
                return "t1"

            def close(self, pane):
                raise oke.HerdrError("confirmation_required", "pane refused to close")
        real = oke.runtime
        oke.runtime = lambda *a: Stub()
        try:
            self.assertEqual(self.archive("fix-a"), 0, self.err)
        finally:
            oke.runtime = real
        self.assertIn("not_closed: fix-a-w1", self.out)
        self.assertEqual(oke.read_children(self.pm), {})

    def test_a_pane_gone_meanwhile_clears_its_record(self):
        key = oke.repo_key(self.repo)
        oke.write_json_atomic(oke.children_path(self.pm), {
            "fix-a-w1": {"job": "fix-a", "role": "worker", "pane": "w1:p1", "terminal": "t1"}})

        class Stub(object):
            def place_id(self, pane):
                return "t1"

            def close(self, pane):
                raise oke.HerdrError("pane_not_found", "gone")
        real = oke.runtime
        oke.runtime = lambda *a: Stub()
        try:
            self.assertEqual(self.cli("job", "cancel", "fix-a"), 0, self.err)
        finally:
            oke.runtime = real
        out = json.loads(self.out.strip().splitlines()[-1])
        self.assertEqual((out["not_closed"], out["refused"]), ([], []))
        self.assertIsNone(oke.read_children(self.pm)["fix-a-w1"]["pane"])

    def test_archive_survives_a_broken_cleanup(self):
        class Stub(object):
            def place_id(self, pane):
                raise ValueError("bad json from the runtime")
        key = oke.repo_key(self.repo)
        oke.write_json_atomic(oke.children_path(self.pm), {
            "fix-a-w1": {"job": "fix-a", "role": "worker", "pane": "w1:p1", "terminal": "t1"}})
        real = oke.runtime
        oke.runtime = lambda *a: Stub()
        try:
            self.assertEqual(self.archive("fix-a"), 0, self.err)
        finally:
            oke.runtime = real
        self.assertNotIn("fix-a", oke.load_state()["jobs"])

    def test_report_entries(self):
        text = "## Summary\n- a.md: ok\n## wiki\n- `b.md`: ok\n* c.md: wrong - stale flag\n- d.md: unused\n## Learned\n- e.md: ok\n"
        self.assertEqual(wiki.report_entries(text),
                         [("b.md", "ok", ""), ("c.md", "wrong", "stale flag"), ("d.md", "unused", "")])


if __name__ == "__main__":
    import unittest
    unittest.main()
