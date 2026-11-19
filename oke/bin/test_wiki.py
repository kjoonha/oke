"""The wiki: caps, read before write, duplicates, usage, archiving, and the gates."""
import io
import json
import os

from test_oke import J, SpawnBase, help_ok  # noqa: F401
import oke
import wiki


class TestWiki(SpawnBase):
    def setUp(self):
        SpawnBase.setUp(self)
        self.repo = self.git_repo("r")
        self.assertEqual(self.cli("job", "new", "fix-a", "--repo", self.repo), 0, self.err)
        self.dir = oke.wiki_dir(oke.repo_key(self.repo))

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

    def test_lessons_wait_for_a_verified_merge(self):
        src = self.write("src.md", "- a fact about the build system\n")
        args = ("wiki", "write", "build.md", "--job", "fix-a", "--from", src, "--base", "new", "--note", "n")
        self.assertEqual(self.cli(*args), 1)
        self.assertIn("not verified", self.err)
        oke.ledger_add("fix-a", "oke", "abc", "merge", "ok")
        oke.ledger_add("fix-a", "oke", "abc", "verify", "pass")
        self.assertEqual(self.cli(*args), 0, self.err)

    def test_the_shared_wiki_takes_no_repository_path(self):
        src = self.write("src.md", "- the tests of %s are slow\n" % self.repo.replace("\\", "/"))
        self.assertEqual(self.cli("wiki", "write", "x.md", "--shared", "--from", src, "--base", "new",
                                  "--note", "n"), 1)
        self.assertIn("beyond one repository", self.err)

    def test_reports_count_use_and_old_pages_are_archived_not_deleted(self):
        self.assertEqual(self.page("build.md", "- run make test before a commit\n"), 0, self.err)
        self.assertEqual(self.page("old.md", "- something nobody needs any more at all\n"), 0, self.err)
        self.assertEqual(self.page("index.md", "- [build](build.md)\n- [old](old.md)\n"), 0, self.err)
        self.write("home/jobs/fix-a/fix-a-w1/report.md",
                   "type: done\n---\n## 요약\nok\n## wiki\n- build.md: wrong: it is make check now\n- x.md: ok\n")
        self.assertEqual(self.cli("job", "archive", "fix-a"), 0, self.err)
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
        self.assertEqual(self.cli("job", "archive", "fix-a"), 0, self.err)
        self.assertIn("kind: curate", self.out)
        wiki.log(self.dir, "curate", "merged two pages")
        self.assertEqual(self.cli("job", "new", "fix-b", "--repo", self.repo), 0, self.err)
        self.assertEqual(self.cli("job", "archive", "fix-b"), 0, self.err)
        self.assertNotIn("curate", self.out)

    def test_merging_a_page_may_repeat_its_lines(self):
        self.assertEqual(self.page("a.md", "- run make test before every single commit\n"), 0, self.err)
        self.assertEqual(self.page("b.md", "- run make test before every single commit\n"), 1)
        self.assertEqual(self.page("b.md", "- run make test before every single commit\n", "new",
                                   "--merging", "a.md"), 0, self.err)

    def test_a_page_without_history_is_not_due_at_once(self):
        path = self.write("home/wiki/r/old.md", "- written long before usage was kept\n")
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
        self.assertEqual(self.cli("job", "archive", "calc"), 0, self.err)
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

    def test_status_says_direct_and_research_may_look_while_a_direct_job_runs(self):
        folder = self.path("plain")
        os.makedirs(folder)
        self.assertEqual(self.cli("job", "new", "calc", "--repo", folder), 0, self.err)
        self.assertEqual(self.cli("job", "new", "look", "--repo", folder, "--research"), 0, self.err)
        os.environ["HERDR_ENV"] = "1"
        os.environ["HERDR_PANE_ID"] = "w7:p1"
        fake = help_ok()
        fake["agent list"] = [J({"agents": []})]
        self.fake(fake)
        self.assertEqual(self.cli("status"), 0, self.err)
        jobs = json.loads(self.out)["jobs"]
        self.assertEqual((jobs["calc"]["direct"], jobs["look"]["size"]), (True, "research"))

    def test_the_shared_wiki_catches_git_bash_paths_and_ignores_a_root(self):
        if not oke.os.name == "nt":
            self.skipTest("drive letters")
        bash = "/" + self.repo[0].lower() + self.repo[2:].replace("\\", "/")
        src = self.write("src.md", "- see %s/Makefile\n" % bash)
        self.assertEqual(self.cli("wiki", "write", "x.md", "--shared", "--from", src, "--base", "new",
                                  "--note", "n"), 1)
        state = oke.load_state()
        state["repos"]["c:/.git"] = {}
        oke.save_state(state)
        src = self.write("src.md", "- deploys use c:/tools only on a Monday\n")
        self.assertEqual(self.cli("wiki", "write", "y.md", "--shared", "--from", src, "--base", "new",
                                  "--note", "n"), 0, self.err)

    def test_a_curation_job_resets_the_count(self):
        for i in range(oke.CURATE_EVERY):
            wiki.log(self.dir, "job%d" % i, "none")
        wiki.log(self.dir, "tidy", "curated: merged two pages")
        self.assertEqual(self.cli("job", "archive", "fix-a"), 0, self.err)
        self.assertNotIn("curate", self.out)

    def test_cancel_closes_the_job_and_drops_only_its_mail(self):
        key = oke.repo_key(self.repo)
        oke.write_json_atomic(oke.children_path(key), {"fix-a-w1": {"job": "fix-a", "role": "worker"}})
        pm = "pm-" + oke.repo_name(key)
        state = oke.load_state()
        state["repos"][key] = {"pm": {"agent": pm, "pane": "w1:p9"}}
        oke.save_state(state)
        self.fake({})
        hold = os.path.join(oke.oke_home(), "hold", pm)
        self.write(hold, "user held the dead PM")
        self.assertEqual(self.cli("send", pm, "Job fix-a: assignment updated (seq 2)"), 0, self.err)
        self.assertEqual(self.cli("send", pm, "Job fix-b: go on"), 0, self.err)
        self.write(os.path.join(oke.inbox_dir("fix-a-w1"), "1.txt"), "Assignment updated")
        self.assertEqual(self.cli("job", "cancel", "fix-a"), 0, self.err)
        self.assertEqual(json.loads(self.out.strip().splitlines()[-1])["dropped_messages"], 2)
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["phase"], "failed")
        texts = [t.split(": ", 1)[1] for _, t in oke.inbox_messages(pm)]
        self.assertEqual(texts[0], "Job fix-b: go on")  # the PM still hears of the cancel itself
        self.assertTrue(texts[1].startswith("Job fix-a was cancelled"))
        self.assertFalse(oke.should_be_alive(oke.load_state(), "pm", key, "pm-x", {}))
        self.assertFalse(os.path.exists(hold))  # its last open job is gone: the hold ends

    def test_cancel_closes_only_panes_still_the_childs_and_never_a_done_job(self):
        key = oke.repo_key(self.repo)
        oke.write_json_atomic(oke.children_path(key), {
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
        self.assertIsNone(oke.read_children(key)["fix-a-rv"]["pane"])
        state = oke.load_state()
        state["jobs"]["fix-a"]["phase"] = "done"
        oke.save_state(state)
        self.assertEqual(self.cli("job", "cancel", "fix-a"), 1)
        state["jobs"]["fix-a"]["phase"] = "merging"  # a merge that never reached main can be given up
        oke.save_state(state)
        self.assertEqual(self.cli("job", "cancel", "fix-a"), 0, self.err)
        state["jobs"]["fix-a"]["phase"] = "merging"
        oke.save_state(state)
        oke.ledger_add("fix-a", "oke", "abc", "merge", "ok")
        self.assertEqual(self.cli("job", "cancel", "fix-a"), 1)  # merged, not verified yet
        self.assertIn("in main", self.err)

    def test_archive_closes_the_panes_a_pm_left_open(self):
        # live: a PM skipped closing a finished job's worker; archive closes it
        key = oke.repo_key(self.repo)
        oke.write_json_atomic(oke.children_path(key), {
            "fix-a-w1": {"job": "fix-a", "role": "worker", "pane": "w1:p1", "terminal": "t1"},
            "fix-b-w1": {"job": "fix-b", "role": "worker", "pane": "w1:p3", "terminal": "t3"}})
        closed = []

        class Stub(object):
            def place_id(self, pane):
                return {"w1:p1": "t1", "w1:p3": "t3"}[pane]

            def close(self, pane):
                closed.append(pane)
        state = oke.load_state()
        state["jobs"]["fix-a"]["phase"] = "done"
        oke.save_state(state)
        real = oke.runtime
        oke.runtime = lambda *a: Stub()
        try:
            self.assertEqual(self.cli("job", "archive", "fix-a"), 0, self.err)
        finally:
            oke.runtime = real
        self.assertEqual(closed, ["w1:p1"])  # only this job's child
        self.assertIsNone(oke.read_children(key)["fix-a-w1"]["pane"])
        self.assertNotIn("fix-a", oke.load_state()["jobs"])

    def test_archive_keeps_what_an_outage_left_and_still_succeeds(self):
        key = oke.repo_key(self.repo)
        oke.write_json_atomic(oke.children_path(key), {
            "fix-a-w1": {"job": "fix-a", "role": "worker", "pane": "w1:p1", "terminal": "t1"}})

        class Stub(object):
            def place_id(self, pane):
                return None  # herdr answers None in an outage too

            def place_exists(self, pane):
                raise oke.HerdrError("herdr_unavailable", "down")
        real = oke.runtime
        oke.runtime = lambda *a: Stub()
        try:
            self.assertEqual(self.cli("job", "archive", "fix-a"), 0, self.err)
        finally:
            oke.runtime = real
        self.assertIn("not_closed: fix-a-w1", self.out)
        self.assertEqual(oke.read_children(key)["fix-a-w1"]["pane"], "w1:p1")
        self.assertNotIn("fix-a", oke.load_state()["jobs"])

    def test_archive_reports_a_pane_that_refuses_to_close(self):
        key = oke.repo_key(self.repo)
        oke.write_json_atomic(oke.children_path(key), {
            "fix-a-w1": {"job": "fix-a", "role": "worker", "pane": "w1:p1", "terminal": "t1"}})

        class Stub(object):
            def place_id(self, pane):
                return "t1"

            def close(self, pane):
                raise oke.HerdrError("confirmation_required", "worktree group root")
        real = oke.runtime
        oke.runtime = lambda *a: Stub()
        try:
            self.assertEqual(self.cli("job", "archive", "fix-a"), 0, self.err)
        finally:
            oke.runtime = real
        self.assertIn("not_closed: fix-a-w1", self.out)
        self.assertEqual(oke.read_children(key)["fix-a-w1"]["pane"], "w1:p1")

    def test_a_pane_gone_meanwhile_clears_its_record(self):
        key = oke.repo_key(self.repo)
        oke.write_json_atomic(oke.children_path(key), {
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
        self.assertIsNone(oke.read_children(key)["fix-a-w1"]["pane"])

    def test_archive_survives_a_broken_cleanup(self):
        class Stub(object):
            def place_id(self, pane):
                raise ValueError("bad json from the runtime")
        key = oke.repo_key(self.repo)
        oke.write_json_atomic(oke.children_path(key), {
            "fix-a-w1": {"job": "fix-a", "role": "worker", "pane": "w1:p1", "terminal": "t1"}})
        real = oke.runtime
        oke.runtime = lambda *a: Stub()
        try:
            self.assertEqual(self.cli("job", "archive", "fix-a"), 0, self.err)
        finally:
            oke.runtime = real
        self.assertNotIn("fix-a", oke.load_state()["jobs"])

    def test_report_entries(self):
        text = "## 요약\n- a.md: ok\n## wiki\n- `b.md`: ok\n* c.md: wrong - stale flag\n- d.md: unused\n## 배운 것\n- e.md: ok\n"
        self.assertEqual(wiki.report_entries(text),
                         [("b.md", "ok", ""), ("c.md", "wrong", "stale flag"), ("d.md", "unused", "")])


if __name__ == "__main__":
    import unittest
    unittest.main()
