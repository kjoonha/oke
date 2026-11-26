"""Role PMs: `oke pm`, `job new --pm`, `job path`, `job split`, path-check, the migration."""
import io
import json
import re
import os
import subprocess
import unittest

import oke
from test_oke import AG, J, SpawnBase


class PmBase(SpawnBase):
    def setUp(self):
        SpawnBase.setUp(self)
        os.environ["HERDR_ENV"] = "1"
        self.fake({"agent list": [J({"agents": []})], "pane close": [J({"type": "ok"})]})

    def pmc(self, *args):
        code = self.cli("pm", *args)
        return code, (json.loads(self.out.strip().splitlines()[-1]) if code == 0 and self.out.strip() else None)

    def folder(self, name):
        p = self.path(name)
        os.makedirs(p)
        return p

    def new(self, job, pm, *extra):
        return self.cli("job", "new", job, "--pm", pm, *extra)

    def places(self, name):
        return oke.load_state()["pms"][name]["places"]

    def norm(self, p):
        return oke.normalize_path(p)


class TestPmCommands(PmBase):
    def test_add_checks_the_name_and_writes_the_standing_assignment(self):
        for bad in ("Backend", "1abc", "orchestrator", "fix-w1", "fix-rv", "fix-rv2", "a" * 25):
            self.assertEqual(self.pmc("add", bad)[0], 1, bad)
        code, out = self.pmc("add", "backend")
        self.assertEqual(code, 0, self.err)
        self.assertEqual(oke.load_state()["pms"]["backend"], {"pm": {}, "places": []})
        self.assertEqual(self.pmc("add", "backend")[0], 1)  # exists
        head = oke.read_header(os.path.join(oke.pm_dir("backend"), "assignment.md"))
        self.assertEqual((head["agent"], head["role"], head["cwd"]),
                         ("backend", "pm", oke.pm_dir("backend").replace("\\", "/")))
        self.assertEqual(head["wiki"], oke.wiki_dir("backend").replace("\\", "/"))

    def test_list_shows_places_and_recommends_the_pm_whose_places_hold_the_path(self):
        self.pmc("add", "a")
        self.pmc("add", "b")
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.assertEqual(self.cli("pm", "list"), 0)
        out = json.loads(self.out)
        self.assertEqual([(p["name"], p["places"], p["open_jobs"]) for p in out],
                         [("a", [self.norm(self.repo)], ["fix-a"]), ("b", [], [])])
        self.assertNotIn("recommended", out[0])
        os.makedirs(os.path.join(self.repo, "src"))
        self.assertEqual(self.cli("pm", "list", "--path", os.path.join(self.repo, "src")), 0)
        self.assertEqual([p["recommended"] for p in json.loads(self.out)], [True, False])
        self.assertEqual(self.cli("pm", "list", "--path", self.path("elsewhere")), 0)
        self.assertEqual([p["recommended"] for p in json.loads(self.out)], [False, False])

    def test_list_resolves_the_users_path_as_places_are(self):
        self.pmc("add", "a")
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        link = self.path("via-link")
        TestPathCheck.link(self, link, self.repo)
        self.assertEqual(self.cli("pm", "list", "--path", os.path.join(link, "src")), 0, self.err)
        self.assertEqual([p["recommended"] for p in json.loads(self.out)], [True])

    def test_add_and_rename_set_an_old_repository_wiki_aside(self):
        wikis = oke.load_config()["wiki_path"]
        for name, files in (("toy", {".oke-repo": "/r/toy\n", "x.md": "old"}), ("calc", {"x.md": "old"}),
                            ("kept", {"log.md": "2026 j: ok\n"})):
            os.makedirs(os.path.join(wikis, name))
            for f, text in files.items():
                with io.open(os.path.join(wikis, name, f), "w", encoding="utf-8") as h:
                    h.write(u"" + text)
        code, out = self.pmc("add", "toy")
        self.assertEqual(code, 0, self.err)
        self.assertRegex(out["old_wiki"], r"/toy\.old-\d{8}-\d{6}$")
        self.assertTrue(os.path.exists(os.path.join(out["old_wiki"], ".oke-repo")))  # moved, unread
        self.assertEqual(os.listdir(oke.wiki_dir("toy")), [])  # the new PM starts with an empty wiki
        code, out = self.pmc("rename", "toy", "calc")  # no log.md a PM wrote: an old one too
        self.assertEqual(code, 0, self.err)
        self.assertRegex(out["old_wiki"], r"/calc\.old-\d{8}-\d{6}$")
        code, out = self.pmc("add", "kept")  # a PM's own wiki (a log.md) stays
        self.assertEqual(code, 0, self.err)
        self.assertNotIn("old_wiki", out)
        self.assertTrue(os.path.exists(os.path.join(wikis, "kept", "log.md")))

    def test_rename_refuses_with_open_jobs_and_moves_the_folders(self):
        self.pmc("add", "a")
        self.assertEqual(self.new("fix-a", "a"), 0, self.err)
        self.assertEqual(self.pmc("rename", "a", "b")[0], 1)
        self.assertIn("fix-a", self.err)
        self.assertEqual(self.cli("job", "cancel", "fix-a"), 0, self.err)
        self.assertEqual(self.cli("job", "archive", "fix-a"), 0, self.err)
        with io.open(os.path.join(oke.wiki_dir("a"), "build.md"), "w", encoding="utf-8") as f:
            f.write(u"- make\n")
        self.assertEqual(self.pmc("rename", "a", "b")[0], 0, self.err)
        self.assertEqual(sorted(oke.load_state()["pms"]), ["b"])
        self.assertTrue(os.path.exists(os.path.join(oke.wiki_dir("b"), "build.md")))
        self.assertFalse(os.path.exists(oke.pm_dir("a")))
        self.assertEqual(oke.read_header(os.path.join(oke.pm_dir("b"), "assignment.md"))["agent"], "b")
        self.assertEqual(self.pmc("rename", "nope", "c")[0], 1)

    def test_rename_moves_all_folders_or_none_and_can_run_again(self):
        self.pmc("add", "a")
        oke.queue_mail("a", "oke", "hello")
        os.makedirs(oke.inbox_dir("b"))
        with io.open(os.path.join(oke.inbox_dir("b"), "x.txt"), "w") as f:
            f.write(u"in the way")
        self.assertEqual(self.pmc("rename", "a", "b")[0], 1)
        self.assertIn("nothing changed", self.err)
        self.assertEqual(sorted(oke.load_state()["pms"]), ["a"])
        self.assertTrue(os.path.isdir(oke.pm_dir("a")))  # moved back
        self.assertFalse(os.path.exists(oke.pm_dir("b")))
        os.remove(os.path.join(oke.inbox_dir("b"), "x.txt"))
        self.assertEqual(self.pmc("rename", "a", "b")[0], 0, self.err)
        self.assertEqual(sorted(oke.load_state()["pms"]), ["b"])
        self.assertTrue(oke.inbox_messages("b"))

    def test_remove_changes_nothing_when_a_folder_will_not_move(self):
        self.pmc("add", "a")
        oke.queue_mail("a", "oke", "hello")
        real = oke.os.rename

        def stuck(old, new):
            if "inbox" in old.replace("\\", "/"):
                raise OSError("in use")
            return real(old, new)
        oke.os.rename = stuck
        try:
            self.assertEqual(self.pmc("remove", "a")[0], 1)
        finally:
            oke.os.rename = real
        self.assertIn("was not removed", self.err)
        self.assertIn("a", oke.load_state()["pms"])
        self.assertTrue(os.path.isdir(oke.pm_dir("a")))
        self.assertEqual(self.pmc("remove", "a")[0], 0, self.err)  # run again
        self.assertNotIn("a", oke.load_state()["pms"])

    def test_remove_refuses_with_open_jobs_and_cancels_them_with_cancel(self):
        self.pmc("add", "a")
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.assertEqual(self.pmc("remove", "a")[0], 1)
        self.assertIn("--cancel", self.err)
        self.assertIn("a", oke.load_state()["pms"])
        code, out = self.pmc("remove", "a", "--cancel")
        self.assertEqual(code, 0, self.err)
        self.assertEqual(out["cancelled"], ["fix-a"])
        self.assertEqual(out["archive"], ["fix-a"])
        st = oke.load_state()
        self.assertNotIn("a", st["pms"])
        self.assertEqual(st["jobs"]["fix-a"]["phase"], "failed")
        self.assertFalse(os.path.exists(oke.pm_dir("a")))
        self.assertEqual(self.pmc("add", "a")[0], 0)  # the name is free again

    def test_a_failed_move_names_the_folder_that_failed(self):
        a, b = self.folder("mf/a"), self.folder("mf/b")
        self.write("mf/bnew/x", "x")
        with self.assertRaises(OSError) as c:
            oke.move_folders([(a, self.path("mf/anew")), (b, self.path("mf/bnew"))])
        self.assertIn("could not move %s:" % b.replace("\\", "/"), str(c.exception))
        self.assertTrue(os.path.isdir(a))  # the first move was undone

    def test_remove_refuses_when_the_pm_made_a_job_meanwhile(self):
        self.pmc("add", "a")
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        real = oke.cancel_for_pm

        def cancel(name, jobs):
            self.assertEqual(self.new("fix-b", "a"), 0, self.err)  # a split while it cancels
            return real(name, jobs)
        oke.cancel_for_pm = cancel
        try:
            self.assertEqual(self.pmc("remove", "a", "--cancel")[0], 1)
        finally:
            oke.cancel_for_pm = real
        self.assertIn("got new jobs meanwhile: fix-b", self.err)
        self.assertIn("a", oke.load_state()["pms"])
        self.assertEqual(self.pmc("remove", "a", "--cancel")[0], 0, self.err)  # asked again: both go

    def test_remove_names_a_pm_session_it_could_not_close(self):
        self.pmc("add", "a")
        st = oke.load_state()
        st["pms"]["a"]["pm"] = {"agent": "a", "pane": "w1:p5"}
        oke.save_state(st)
        real = oke.list_agents
        oke.list_agents = lambda *a, **k: (_ for _ in ()).throw(oke.HerdrError("registry_locked", "busy"))
        try:
            code, out = self.pmc("remove", "a")
        finally:
            oke.list_agents = real
        self.assertEqual(code, 0, self.err)
        self.assertEqual(out["not_closed"], ["w1:p5"])

    def test_remove_names_the_panes_it_could_not_close(self):
        self.pmc("add", "a")
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        real = oke.close_children
        oke.close_children = lambda job, key: ([], ["fix-a-worker"], [])
        try:
            code, out = self.pmc("remove", "a", "--cancel")
        finally:
            oke.close_children = real
        self.assertEqual(code, 0, self.err)
        self.assertEqual(out["not_closed"], ["fix-a-worker"])

    def test_a_remove_that_fails_after_its_cancels_lists_them_to_archive_when_run_again(self):
        self.pmc("add", "a")
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        real = oke.os.rename
        def stuck(old, new):
            if os.path.basename(old) == "a":
                raise OSError("in use")
            return real(old, new)
        oke.os.rename = stuck
        try:
            self.assertEqual(self.pmc("remove", "a", "--cancel")[0], 1)
        finally:
            oke.os.rename = real
        self.assertIn("archive", self.err)
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["phase"], "failed")
        code, out = self.pmc("remove", "a")  # no open job left: no --cancel needed
        self.assertEqual(code, 0, self.err)
        self.assertEqual(out["cancelled"], [])
        self.assertEqual(out["archive"], ["fix-a"])  # the earlier run's cancel is not left behind

    def test_remove_takes_the_pms_mail_away(self):
        self.pmc("add", "a")
        oke.queue_mail("a", "oke", "old news")
        self.assertTrue(oke.inbox_messages("a"))
        self.assertEqual(self.pmc("remove", "a")[0], 0, self.err)
        self.assertFalse(os.path.exists(oke.inbox_dir("a")))
        self.pmc("add", "a")
        self.assertEqual(oke.inbox_messages("a"), [])  # a new PM of that name gets none of it
        kept = [d for d in os.listdir(os.path.dirname(oke.pm_dir("a"))) if d.startswith("a.removed-")]
        self.assertTrue(os.path.isdir(os.path.join(os.path.dirname(oke.pm_dir("a")), kept[0], "inbox")))

    def test_remove_sets_the_wiki_aside_and_archive_makes_no_wiki_again(self):
        self.pmc("add", "a")
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        wikis = oke.load_config()["wiki_path"]
        self.assertEqual(self.cli("wiki", "log", "--job", "fix-a", "--note", "none"), 0, self.err)
        self.assertEqual(self.pmc("remove", "a", "--cancel")[0], 0, self.err)
        self.assertFalse(os.path.exists(os.path.join(wikis, "a")))
        aside = [d for d in os.listdir(wikis) if d.startswith("a.removed-")]
        self.assertEqual(len(aside), 1)
        self.assertTrue(os.path.exists(os.path.join(wikis, aside[0], "log.md")))
        self.write("home/jobs/fix-a/fix-a-w1/report.md", "type: done\nseq: 1\n---\n## wiki\n- used: x.md\n")
        self.assertEqual(self.cli("job", "archive", "fix-a"), 0, self.err)
        self.assertNotIn("curate", self.out)
        self.assertFalse(os.path.exists(os.path.join(wikis, "a")))  # no wiki made for a PM that is gone

    def test_remove_cancel_cancels_nothing_when_one_job_cannot_be_cancelled(self):
        self.pmc("add", "a")
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.assertEqual(self.new("fix-b", "a"), 0, self.err)
        real = oke.merge_state
        oke.merge_state = lambda job: ("verified",) if job == "fix-b" else real(job)
        self.addCleanup(setattr, oke, "merge_state", real)
        self.assertEqual(self.pmc("remove", "a", "--cancel")[0], 1)
        self.assertIn("fix-b", self.err)
        st = oke.load_state()
        self.assertIn("a", st["pms"])
        self.assertEqual((st["jobs"]["fix-a"]["phase"], st["jobs"]["fix-b"]["phase"]), ("intake", "intake"))


class TestJobPath(PmBase):
    def setUp(self):
        PmBase.setUp(self)
        self.pmc("add", "a")

    def test_job_new_needs_a_known_pm_and_takes_a_path_only_when_given(self):
        self.assertEqual(self.new("fix-a", "nope"), 1)
        self.assertEqual(self.new("fix-a", "a"), 0, self.err)
        out = json.loads(self.out)
        self.assertEqual((out["pm_agent"], out["path"], out["direct"]), ("a", None, None))
        self.assertEqual(out["wiki"], oke.wiki_dir("a").replace("\\", "/"))
        self.assertEqual(self.places("a"), [])
        os.makedirs(os.path.join(self.repo, "src"))
        self.assertEqual(self.new("fix-b", "a", "--path", os.path.join(self.repo, "src")), 0, self.err)
        out = json.loads(self.out)
        self.assertEqual((out["path"], out["direct"]), (self.norm(self.repo), False))

    def test_job_path_sets_the_path_and_the_places_newest_first(self):
        notes = self.folder("notes")
        self.assertEqual(self.new("fix-a", "a"), 0, self.err)
        self.assertEqual(self.cli("job", "path", "fix-a", self.repo), 0, self.err)
        out = json.loads(self.out)
        self.assertEqual((out["path"], out["direct"]), (self.norm(self.repo), False))
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["git_key"], oke.repo_key(self.repo))
        self.assertTrue(oke.load_state()["jobs"]["fix-a"]["base"])
        self.assertEqual(self.cli("job", "path", "fix-a", notes), 0, self.err)  # changed while planning
        self.assertTrue(json.loads(self.out)["direct"])
        self.assertEqual(self.places("a"), [self.norm(notes), self.norm(self.repo)])
        self.assertEqual(self.cli("job", "path", "fix-a", self.repo), 0, self.err)
        self.assertEqual(self.places("a"), [self.norm(self.repo), self.norm(notes)])  # no repeats
        self.assertEqual(self.cli("job", "path", "fix-a", self.path("missing")), 1)

    def test_job_path_is_refused_once_the_plan_is_approved(self):
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.advance("fix-a", "awaiting_approval")
        self.assertEqual(self.cli("job", "path", "fix-a", self.repo), 0, self.err)
        self.advance("fix-a", "working")
        self.assertEqual(self.cli("job", "path", "fix-a", self.folder("elsewhere")), 1)
        self.assertIn("too late to split. Report a question", self.err)  # job split refuses a working job too

    def test_one_open_direct_job_per_folder(self):
        notes = self.folder("notes")
        self.assertEqual(self.new("calc", "a", "--path", notes), 0, self.err)
        self.assertEqual(self.new("calc2", "a"), 0, self.err)
        self.assertEqual(self.cli("job", "path", "calc2", notes), 1)
        self.assertIn("calc", self.err)
        self.assertEqual(self.cli("job", "path", "calc2", self.folder("other")), 0, self.err)

    def test_a_research_job_sets_its_path_while_it_works(self):
        self.assertEqual(self.cli("job", "new", "look", "--pm", "a", "--research"), 0, self.err)
        self.assertEqual(self.cli("interview", "look", "--goal", 1, "--constraints", 1, "--criteria", 1), 0)
        self.assertEqual(self.cli("job", "phase", "look", "working"), 0, self.err)
        self.assertEqual(self.cli("job", "path", "look", self.repo), 0, self.err)
        self.assertEqual(oke.load_state()["jobs"]["look"]["path"], self.norm(self.repo))
        self.assertEqual(self.cli("job", "path", "look", self.folder("other")), 1)  # set once it works
        self.assertIn("a research job keeps its path; report a question", self.err)
        self.assertNotIn("split", self.err)
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.advance("fix-a", "working")
        self.assertEqual(self.cli("job", "path", "fix-a", self.folder("again")), 1)  # only research

    def launch(self, role, folder):
        """The added folders an agent of `role` starting in `folder` gets."""
        args = oke.agent_command(role, oke.load_config(), oke.oke_home(), oke.plugin_root(), folder)
        if "--settings" in args:
            with io.open(args[args.index("--settings") + 1], encoding="utf-8") as f:
                return json.load(f)["permissions"]["additionalDirectories"]
        return [args[i + 1] for i, a in enumerate(args) if a == "--add-dir"]

    def test_a_pm_reads_the_whole_file_system_and_a_worker_only_its_job(self):
        roots = oke.fs_roots()
        self.assertEqual(roots, ["/"] if os.name != "nt" else
                         [d + ":/" for d in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(d + ":/")])
        self.assertIn(os.path.splitdrive(self.home)[0].upper() + "/" if os.name == "nt" else "/", roots)
        pm = self.launch("pm", oke.pm_dir("a"))
        self.assertTrue(set(roots) <= set(pm), pm)
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        worker = os.path.join(self.home, "jobs", "fix-a", "fix-a-w1")
        os.makedirs(worker)
        with io.open(os.path.join(worker, "assignment.md"), "w", encoding="utf-8") as f:
            f.write(u"agent: fix-a-w1\nrole: worker\njob: fix-a\ncwd: %s\nseq: 1\n---\n" % self.repo)
        self.assertFalse(set(roots) & set(self.launch("worker", worker)))

    def test_no_command_mentions_restart(self):
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.assertNotIn("restart", json.loads(self.out))
        self.assertEqual(self.new("fix-b", "a"), 0, self.err)
        self.assertEqual(self.cli("job", "path", "fix-b", self.folder("notes")), 0, self.err)
        self.assertNotIn("restart", json.loads(self.out))
        self.assertFalse(hasattr(oke, "restart_later") or hasattr(oke, "cmd_restart_pm"))

    def test_a_revived_pm_gets_its_own_workspace(self):
        st = oke.load_state()
        st["orchestrator"]["pane"] = "w1:p1"
        oke.save_state(st)
        self.fake({"pane list": [J({"panes": [{"pane_id": "w1:p1", "cwd": self.home}]})],
                   "workspace create": [J({"root_pane": {"pane_id": "w5:p1"}})]})
        self.assertEqual(oke.new_pane(oke.load_state(), "pm", "a", oke.pm_dir("a")), "w5:p1")
        argv = " ".join(self.argv_of("workspace", "create")[-1])
        self.assertIn("--label a", argv)
        self.assertEqual(self.argv_of("pane", "split"), [])

    def test_a_job_that_worked_keeps_its_path(self):
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.advance("fix-a", "working")
        st = oke.load_state()
        st["jobs"]["fix-a"].update(phase="planning", worktree={"id": "w7", "path": self.path("wt")})  # a re-plan
        oke.save_state(st)
        before = dict(oke.load_state()["jobs"]["fix-a"])
        self.assertEqual(self.cli("job", "path", "fix-a", self.git_repo("api")), 1)
        self.assertIn("split a new job", self.err)
        self.assertEqual(oke.load_state()["jobs"]["fix-a"], before)  # path, git key, worktree untouched

    def test_each_mark_of_a_started_job_alone_keeps_its_path(self):
        other = self.folder("other")
        marks = {"worktree": {"id": "w7", "path": self.path("wt")}, "worked": True, "approved": True}
        for i, mark in enumerate(sorted(marks) + [None]):
            name = "job-%d" % i
            self.assertEqual(self.new(name, "a", "--path", self.repo), 0, self.err)
            self.advance(name, "planning")
            st = oke.load_state()
            st["jobs"][name].pop("worked", None)
            st["jobs"][name].pop("approved", None)
            if mark:
                st["jobs"][name][mark] = marks[mark]
            oke.save_state(st)
            want = 1 if mark else 0  # with none of them the plan may still move the job
            self.assertEqual(self.cli("job", "path", name, other), want, (mark, self.err))
            if mark:
                self.assertIn("split a new job", self.err)
            else:
                self.assertEqual(oke.load_state()["jobs"][name]["path"], self.norm(other))
            self.assertEqual(self.cli("job", "cancel", name), 0, self.err)

    def test_an_approved_job_keeps_its_path_after_a_re_plan(self):
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.advance("fix-a", "awaiting_approval")
        st = oke.load_state()
        st["jobs"]["fix-a"].update(phase="awaiting_merge", size="small")
        oke.save_state(st)
        self.assertEqual(self.cli("job", "phase", "fix-a", "merging"), 0, self.err)
        self.assertTrue(oke.load_state()["jobs"]["fix-a"]["approved"])
        st = oke.load_state()
        st["jobs"]["fix-a"]["phase"] = "planning"
        oke.save_state(st)
        self.assertEqual(self.cli("job", "path", "fix-a", self.git_repo("api")), 1)

    def test_the_same_folder_is_a_no_op_at_any_time(self):
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.advance("fix-a", "working")
        before = dict(oke.load_state()["jobs"]["fix-a"])
        same = self.repo.replace("/", "\\") + "\\" if os.name == "nt" else self.repo + "/"
        if os.name == "nt":
            same = same.upper()
        self.assertEqual(self.cli("job", "path", "fix-a", same), 0, self.err)
        self.assertTrue(json.loads(self.out)["unchanged"])
        self.assertEqual(oke.load_state()["jobs"]["fix-a"], before)

    def test_a_drive_root_is_a_folder_job_on_that_root(self):
        if os.name != "nt":
            self.skipTest("drive roots are Windows only")
        drive = os.path.splitdrive(self.home)[0].upper()
        self.assertEqual(self.new("calc", "a", "--path", drive + "\\"), 0, self.err)
        info = oke.load_state()["jobs"]["calc"]
        self.assertEqual((info["path"], info["direct"]), (drive.lower() + "/", True))
        self.assertEqual(self.cli("job", "path", "calc", drive + "/"), 0, self.err)
        self.assertTrue(json.loads(self.out)["unchanged"])
        self.assertEqual(oke.write_areas(oke.load_state(), "pm", "a"), [{"path": drive.lower() + "/", "direct": True}])
        st = oke.load_state()
        worker = os.path.join(self.home, "jobs", "calc", "calc-w1")
        os.makedirs(worker)
        with io.open(os.path.join(worker, "assignment.md"), "w", encoding="utf-8") as f:
            f.write(u"agent: calc-w1\nrole: worker\njob: calc\ncwd: %s\nseq: 1\n---\n" % info["path"])
        me = ("worker", "calc-w1", "a", {"job": "calc", "agent": "calc-w1"})
        self.assertEqual(oke.agent_cwd(me), drive.lower() + "/")
        self.assertIsNone(oke.write_problem(st, me, drive + "/oke-test-nowhere/x.txt"))
        self.assertIsNone(oke.write_problem(st, me, drive + "\\x.txt"))

    def test_a_taken_branch_at_job_path_tells_the_pm_to_ask(self):
        self.git(self.repo, "branch", "oke/fix-b")
        self.assertEqual(self.new("fix-b", "a", "--path", self.repo), 1)
        self.assertIn("pick another job name", self.err)  # the orchestrator names the job: it picks another
        self.assertEqual(self.new("fix-b", "a"), 0, self.err)
        self.assertEqual(self.cli("job", "path", "fix-b", self.repo), 1)
        self.assertIn("report a question: branch oke/fix-b already exists in %s (an earlier job); the user decides: "
                      "delete that branch, or cancel this job and start it under another name" % self.norm(self.repo),
                      self.err)
        self.assertIsNone(oke.load_state()["jobs"]["fix-b"].get("path"))

    def test_a_research_job_ignores_a_taken_branch(self):
        self.git(self.repo, "branch", "oke/look")  # research never makes or merges a branch
        self.assertEqual(self.cli("job", "new", "look", "--pm", "a", "--research", "--path", self.repo), 0, self.err)

    def test_one_open_direct_job_per_folder_tree(self):
        top = self.folder("top")
        inner = self.folder("top/inner")
        self.pmc("add", "b")
        self.assertEqual(self.new("calc", "a", "--path", inner), 0, self.err)
        self.assertEqual(self.new("calc2", "b", "--path", top), 1)  # holds calc's folder
        self.assertIn("calc", self.err)
        self.assertEqual(self.new("calc3", "b", "--path", self.folder("top/inner/deep")), 1)  # inside it
        self.assertIn("calc", self.err)
        self.assertEqual(self.new("calc4", "b", "--path", self.folder("top/side")), 0, self.err)  # a sibling

    def test_a_job_without_a_path_cannot_execute_or_merge(self):
        self.assertEqual(self.new("fix-a", "a"), 0, self.err)
        self.advance("fix-a", "awaiting_approval")
        self.assertEqual(self.cli("job", "phase", "fix-a", "working"), 1)
        self.assertIn("no path", self.err)
        for args in (("worktree", "fix-a"), ("check", "fix-a"), ("baseline", "fix-a"), ("job", "merge", "fix-a"),
                     ("job", "tree", "fix-a")):
            self.assertEqual(self.cli(*args), 1, args)
            self.assertIn("no path", self.err, args)


class TestJobSplit(PmBase):
    def setUp(self):
        PmBase.setUp(self)
        self.pmc("add", "a")
        self.assertEqual(self.new("fix-a", "a"), 0, self.err)
        self.write("home/jobs/fix-a/intake.md", "job: fix-a\n---\n## Desired result\nboth\n")
        self.assertEqual(self.cli("interview", "fix-a", "--goal", 1, "--constraints", 1, "--criteria", 1), 0)
        self.assertEqual(self.cli("job", "phase", "fix-a", "planning"), 0, self.err)

    def test_split_makes_a_job_of_the_same_pm_with_the_intake_and_tells_the_orchestrator(self):
        api = self.git_repo("api")
        self.assertEqual(self.cli("job", "split", "fix-a", "fix-a-api", "--path", api, "--after", "fix-a"), 0,
                         self.err)
        st = oke.load_state()
        new = st["jobs"]["fix-a-api"]
        self.assertEqual((new["pm"], new["path"], new["phase"], new["split_from"]),
                         ("a", self.norm(api), "intake", "fix-a"))
        with io.open(os.path.join(self.home, "jobs", "fix-a-api", "intake.md"), encoding="utf-8") as f:
            intake = f.read()
        self.assertIn("both", intake)
        self.assertIn("after: fix-a", intake)
        self.assertTrue(os.path.exists(oke.interview_path("fix-a-api")))
        out = json.loads(self.out)
        item = oke.pending_item("fix-a-api", new)
        self.assertEqual(item["kind"], "split")
        for got in (out, item):  # what the new job's PM assignment header needs
            self.assertEqual((got["pm_agent"], got["wiki"], got["shared_wiki"], got["pm_job_dir"]),
                             ("a", oke.wiki_dir("a").replace("\\", "/"), oke.shared_wiki().replace("\\", "/"),
                              oke.pm_job_dir("fix-a-api").replace("\\", "/")))
        self.assertEqual(item["text"], "PM a split job fix-a: new job fix-a-api (%s)" % self.norm(api))
        self.assertEqual(self.cli("job", "phase", "fix-a-api", "planning"), 0, self.err)  # interview copied
        self.assertEqual(oke.after_jobs("fix-a-api"), ["fix-a"])  # no plan yet: the split's --after counts
        self.write("home/jobs/fix-a-api/plan.md", "job: fix-a-api\nafter: other\n---\n")
        self.assertEqual(oke.after_jobs("fix-a-api"), ["other", "fix-a"])

    def test_a_direct_split_job_starts_only_after_its_after_job(self):
        notes = self.folder("notes")
        self.assertEqual(self.cli("job", "split", "fix-a", "fix-a-notes", "--path", notes, "--after", "fix-a"), 0,
                         self.err)
        with io.open(os.path.join(self.home, "jobs", "fix-a-notes", "intake.md"), encoding="utf-8") as f:
            self.assertIn("It starts its work only after job fix-a", f.read())
        st = oke.load_state()
        st["jobs"]["fix-a-notes"]["phase"] = "awaiting_approval"
        oke.save_state(st)
        self.assertEqual(self.cli("job", "phase", "fix-a-notes", "working"), 1)
        self.assertIn("it starts its work only after job fix-a", self.err)
        st = oke.load_state()
        st["jobs"]["fix-a"].update(phase="done", direct=True)
        oke.save_state(st)
        self.assertEqual(self.cli("job", "phase", "fix-a-notes", "working"), 0, self.err)

    def test_a_second_split_of_the_same_path_names_the_first(self):
        api = self.git_repo("api")
        self.assertEqual(self.cli("job", "split", "fix-a", "fix-a-api", "--path", api), 0, self.err)
        self.assertEqual(self.cli("job", "split", "fix-a", "fix-a-api2", "--path", api), 1)  # a revived PM
        self.assertIn("fix-a-api", self.err)
        self.assertEqual(json.loads(self.out.strip().splitlines()[-1])["existing"], "fix-a-api")
        self.assertNotIn("fix-a-api2", oke.load_state()["jobs"])
        self.assertEqual(self.cli("job", "split", "fix-a", "fix-a-b", "--path", self.folder("b")), 0, self.err)

    def test_a_split_after_a_parent_that_never_merges_waits_for_done_only(self):
        st = oke.load_state()
        st["jobs"]["fix-a"]["direct"] = True
        oke.save_state(st)
        self.assertIn("fix-a", oke.after_block(oke.load_state(), "fix-a"))
        st = oke.load_state()
        st["jobs"]["fix-a"]["phase"] = "done"
        oke.save_state(st)
        self.assertIsNone(oke.after_block(oke.load_state(), "fix-a"))
        self.assertEqual(self.cli("job", "archive", "fix-a"), 0, self.err)
        self.assertIsNone(oke.after_block(oke.load_state(), "fix-a"))  # archived: its kept job.json says so

    def test_split_refuses_a_taken_name_and_a_job_past_planning(self):
        self.assertEqual(self.cli("job", "split", "fix-a", "fix-a", "--path", self.repo), 1)
        self.assertEqual(self.cli("job", "split", "fix-a", "x-b", "--path", self.path("missing")), 1)
        self.assertNotIn("x-b", oke.load_state()["jobs"])
        self.assertFalse(os.path.exists(os.path.join(self.home, "jobs", "x-b")))
        self.assertEqual(self.cli("job", "path", "fix-a", self.repo), 0, self.err)
        self.advance("fix-a", "working")
        self.assertEqual(self.cli("job", "split", "fix-a", "x-c", "--path", self.repo), 1)


class TestPeersBetweenPms(PmBase):
    def test_a_peer_ask_goes_to_every_other_pm_with_an_open_job(self):
        api = self.git_repo("api")
        for name, rec in (("a", ("w1:p3", "s-a")), ("b", ("w2:p3", "s-b")), ("c", ("w3:p3", "s-c"))):
            self.pmc("add", name)
            st = oke.load_state()
            st["pms"][name]["pm"] = {"agent": name, "pane": rec[0], "session": rec[1]}
            oke.save_state(st)
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.assertEqual(self.new("fix-b", "b", "--path", api), 0, self.err)
        self.advance("fix-a", "planning")
        os.environ["HERDR_PANE_ID"] = "w1:p3"
        self.fake({"agent list": [J({"agents": [AG("a", pane="w1:p3", session="s-a")]})]})
        self.assertEqual(self.cli("peer", "ask", "fix-a", "x changes"), 0, self.err)
        self.assertEqual(json.loads(self.out)["asked"], ["b"])  # c has no open job


class TestPathCheck(PmBase):
    def setUp(self):
        PmBase.setUp(self)
        self.top = self.folder("top")
        self.nested = self.git_repo("top/api")
        self.pmc("add", "docs")
        self.assertEqual(self.new("notes", "docs", "--path", self.top), 0, self.err)
        self.assertEqual(self.new("code", "docs", "--path", self.repo), 0, self.err)
        st = oke.load_state()
        st["pms"]["docs"]["pm"] = {"agent": "docs", "pane": "w1:p3", "session": "s-d"}
        oke.save_state(st)
        self.fake({"agent list": [J({"agents": [AG("docs", pane="w1:p3", session="s-d")]})]})

    def check(self, path, pane="w1:p3"):
        self.assertEqual(self.cli("path-check", "--pane", pane, "--path", path), 0, self.err)
        return json.loads(self.out)

    def test_the_write_areas(self):
        self.assertTrue(self.check(os.path.join(self.top, "a", "b.md"))["allow"])  # a direct job's folder
        self.assertTrue(self.check(os.path.join(self.repo, "src", "x.py"))["allow"])  # a git job's checkout
        self.assertTrue(self.check(os.path.join(self.home, "jobs", "notes", "plan.md"))["allow"])
        mem = oke.memory_dir(oke.pm_dir("docs"))
        self.assertTrue(self.check(os.path.join(mem, "a.md"))["allow"])
        out = self.check(os.path.join(oke.wiki_dir("docs"), "a.md"))
        self.assertFalse(out["allow"])
        self.assertIn("$OKE wiki", out["reason"])
        out = self.check(self.path("elsewhere/x.md"))
        self.assertFalse(out["allow"])
        self.assertIn("outside what this agent may write", out["reason"])
        self.assertIn(self.norm(self.top), out["reason"])  # it names the agent's write areas
        self.assertIn("$OKE job path", out["reason"])  # and what a PM does next

    def link(self, link, target):
        link, target = os.path.normpath(link), os.path.normpath(target)  # mklink wants backslashes
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            if os.name != "nt" or subprocess.call(["cmd", "/c", "mklink", "/J", link, target],
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
                self.skipTest("no symlinks here")

    def test_oke_home_holds_only_the_agents_own_folders(self):
        ok = [oke.pm_dir("docs") + "/board.md", self.home + "/jobs/notes/plan.md",
              self.home + "/jobs/code/code-w1/assignment.md", self.home + "/tmp/page.md"]
        self.pmc("add", "other")
        bad = [oke.state_path(), oke.state_path() + ".tmp", oke.lock_path("records"), self.home + "/config.json",
               oke.pm_dir("other") + "/board.md", self.home + "/automations/a.json", self.home + "/ledger.key",
               self.home + "/inbox/docs/1.txt", oke.pm_dir("docs") + "/children.json",
               self.home + "/jobs/notes/ledger.tsv", self.home + "/jobs/notes/intake.md",
               self.home + "/jobs/notes/check-1.log", self.home + "/jobs/code/code-w1/lean-settings.json"]
        for p in ok:
            self.assertTrue(self.check(p)["allow"], p)
        for p in bad:
            out = self.check(p)
            self.assertFalse(out["allow"], p)
        # another PM's job folder is not this PM's
        self.assertEqual(self.new("theirs", "other"), 0, self.err)
        self.assertFalse(self.check(self.home + "/jobs/theirs/plan.md")["allow"])

    def test_a_worker_writes_its_folder_and_its_job_area_only(self):
        st = oke.load_state()
        me = ("worker", "code-w1", "docs", {"job": "code", "agent": "code-w1"})
        worker = os.path.join(self.home, "jobs", "code", "code-w1")
        self.assertIsNone(oke.write_problem(st, me, os.path.join(worker, "report.md")))
        self.assertIsNone(oke.write_problem(st, me, os.path.join(self.repo, "a.py")))
        for p in (os.path.join(self.home, "jobs", "code", "plan.md"), os.path.join(self.top, "a.md")):
            why = oke.write_problem(st, me, p)
            self.assertIn("report a question to your PM", why)

    def test_a_research_job_has_no_write_area(self):
        self.assertEqual(self.cli("job", "new", "look", "--pm", "docs", "--research", "--path",
                                  self.folder("look")), 0, self.err)
        self.assertEqual(oke.write_areas(oke.load_state(), "worker", "docs", "look"), [])
        self.assertFalse(self.check(os.path.join(self.path("look"), "a.md"))["allow"])

    def test_a_short_name_does_not_pass_the_wiki(self):
        if os.name != "nt":
            self.skipTest("8.3 names are Windows only")
        import ctypes
        wiki = oke.wiki_dir("docs")
        long_name = os.path.join(os.path.dirname(wiki), "a-long-folder-name")
        os.makedirs(long_name)
        buf = ctypes.create_unicode_buffer(32768)
        ctypes.windll.kernel32.GetShortPathNameW(os.path.dirname(wiki), buf, 32768)
        short = buf.value
        if not short or "~" not in short:
            self.skipTest("8.3 names are off on this volume")
        out = self.check(os.path.join(short, "docs", "a.md"))
        self.assertFalse(out["allow"])
        self.assertIn("$OKE wiki", out["reason"])

    def test_a_link_into_a_nested_repository_is_refused(self):
        self.link(os.path.join(self.top, "via"), self.nested)  # top/via -> top/api, its own git
        out = self.check(os.path.join(self.top, "via", "x.py"))
        self.assertFalse(out["allow"])
        self.assertIn("git repository", out["reason"])
        self.assertIn("change it as its own job at", out["reason"])

    def test_a_committed_link_out_of_a_git_checkout_is_refused(self):
        outside = self.folder("outside")
        self.link(os.path.join(self.repo, "out"), outside)
        out = self.check(os.path.join(self.repo, "out", "x.py"))
        self.assertFalse(out["allow"])
        self.assertIn("change it as its own job at", out["reason"])

    def test_a_folder_job_around_a_git_job_does_not_hide_the_git_area(self):
        # a direct job on a folder that holds the PM's own git job (a direct job on ~)
        st = oke.load_state()
        outer = self.norm(os.path.dirname(self.repo))
        st["jobs"]["notes"].update(path=outer, git_key=outer + "/.git")
        oke.save_state(st)
        self.assertTrue(self.check(os.path.join(self.repo, "src", "x.py"))["allow"])
        out = self.check(os.path.join(self.nested, "x.py"))  # nested git, no job of its own
        self.assertFalse(out["allow"])
        self.assertIn("git repository", out["reason"])

    def test_a_held_job_has_no_write_area_until_it_is_back(self):
        st = oke.load_state()
        st["jobs"]["notes"]["held"] = True
        oke.save_state(st)
        self.assertFalse(self.check(os.path.join(self.top, "a.md"))["allow"])
        self.assertFalse(self.check(os.path.join(self.home, "jobs", "notes", "plan.md"))["allow"])
        me = ("worker", "notes-w1", "docs", {"job": "notes", "agent": "notes-w1"})
        own = os.path.join(self.home, "jobs", "notes", "notes-w1", "report.md")
        self.assertIsNone(oke.write_problem(oke.load_state(), me, own))  # its own folder stays
        st = oke.load_state()
        st["jobs"]["notes"].pop("held")  # unhold: the PM revives its children, who write again
        oke.save_state(st)
        self.assertTrue(self.check(os.path.join(self.top, "a.md"))["allow"])
        self.assertTrue(self.check(os.path.join(self.home, "jobs", "notes", "plan.md"))["allow"])
        self.assertIsNone(oke.write_problem(oke.load_state(), me, os.path.join(self.top, "b.md")))

    def test_a_worktree_replaces_the_main_checkout(self):
        st = oke.load_state()
        st["jobs"]["code"]["worktree"] = {"id": "w9", "path": self.path("wt")}
        oke.save_state(st)
        self.assertTrue(self.check(self.path("wt/a.py"))["allow"])
        self.assertFalse(self.check(os.path.join(self.repo, "a.py"))["allow"])

    def test_a_file_in_a_nested_git_repository_is_refused(self):
        out = self.check(os.path.join(self.nested, "src", "x.py"))
        self.assertFalse(out["allow"])
        self.assertIn("git repository", out["reason"])

    def test_a_link_out_of_the_folder_is_refused(self):
        outside = self.folder("elsewhere")
        link = os.path.join(self.top, "link")
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            # Windows without the symlink right: a junction is a link out of the folder too
            if os.name != "nt" or subprocess.call(["cmd", "/c", "mklink", "/J", link, outside],
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
                self.skipTest("no symlinks here")
        out = self.check(os.path.join(link, "x.md"))
        self.assertFalse(out["allow"])
        self.assertIn("outside the job's folder", out["reason"])

    def test_a_done_job_has_no_write_area_and_an_unknown_pane_is_not_judged(self):
        st = oke.load_state()
        st["jobs"]["notes"]["phase"] = "done"
        oke.save_state(st)
        self.assertFalse(self.check(os.path.join(self.top, "a.md"))["allow"])
        out = self.check(self.path("elsewhere/x.md"), pane="w9:p9")  # the mod asks only for an agent
        self.assertFalse(out["allow"])
        self.assertIn("OKe could not check this write", out["reason"])

    def test_tick_gives_the_mod_the_write_areas_and_the_memory_folder(self):
        os.environ["HERDR_PANE_ID"] = "w1:p3"
        self.assertEqual(self.cli("tick", "--pane", "w1:p3"), 0, self.err)
        out = json.loads(self.out.strip().splitlines()[-1])
        self.assertEqual(sorted((a["path"], a["direct"]) for a in out["write_areas"]),
                         sorted([(self.norm(self.top), True), (self.norm(self.repo), False)]))
        self.assertTrue(all(m.endswith("/memory") for m in out["memories"]))
        self.assertNotIn("memory", out)


class TestMemory(PmBase):
    def test_a_worktree_worker_may_write_its_own_and_the_repositorys_memory(self):
        wt = self.path("wt")
        self.git(self.repo, "worktree", "add", "-q", wt, "-b", "wt")
        os.makedirs(os.path.join(wt, "sub"))
        claude = self.path("claude")
        os.environ["CLAUDE_CONFIG_DIR"] = claude
        self.addCleanup(os.environ.pop, "CLAUDE_CONFIG_DIR", None)
        slug = lambda p: re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(p))
        main = os.path.join(claude, "projects", slug(self.repo), "memory")
        for cwd in (wt, os.path.join(wt, "sub"), os.path.join(self.repo, "sub2")):
            if not os.path.isdir(cwd):
                os.makedirs(cwd)
            dirs = oke.memory_dirs(cwd)
            self.assertEqual(dirs[0], os.path.join(claude, "projects", slug(cwd), "memory"))
            self.assertEqual(dirs[-1], main, cwd)
        self.assertEqual(oke.memory_dirs(self.repo), [main])
        plain = self.folder("plain")
        self.assertEqual(len(oke.memory_dirs(plain)), 1)
        self.pmc("add", "a")
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        worker = os.path.join(self.home, "jobs", "fix-a", "fix-a-w1")
        os.makedirs(worker)
        self.write("home/jobs/fix-a/fix-a-w1/assignment.md",
                   "agent: fix-a-w1\nrole: worker\njob: fix-a\ncwd: %s\nseq: 1\n---\n" % wt.replace("\\", "/"))
        me = ("worker", "fix-a-w1", "a", {"job": "fix-a", "agent": "fix-a-w1"})
        st = oke.load_state()
        for d in oke.memory_dirs(wt):
            self.assertIsNone(oke.write_problem(st, me, os.path.join(d, "m.md")))
        other = os.path.join(claude, "projects", slug(plain), "memory", "m.md")
        self.assertIsNotNone(oke.write_problem(st, me, other))
        real = oke.whoami
        oke.whoami = lambda state, pane: (me, [])
        self.addCleanup(setattr, oke, "whoami", real)
        self.assertEqual(self.cli("tick", "--pane", "w1:p9"), 0, self.err)
        out = json.loads(self.out.strip().splitlines()[-1])
        own = os.path.join(claude, "projects", slug(wt), "memory").replace("\\", "/")
        self.assertEqual(out["memories"], [own, main.replace("\\", "/")])  # its cwd's and the repository's


class TestRealPaths(PmBase):
    def test_home_and_pm_folders_compare_resolved(self):
        target = self.folder("realhome")
        link = self.path("linkhome")
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            if os.name != "nt" or subprocess.call(["cmd", "/c", "mklink", "/J", link, target],
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL):
                self.skipTest("no symlinks here")
        self.assertEqual(oke.real_dir(link), oke.real_dir(target))
        self.pmc("add", "a")
        found = oke.find_agent({"agent": "a"}, [{"name": "a", "cwd": oke.pm_dir("a")}], oke.pm_dir("a"))
        self.assertTrue(found)
        os.makedirs(os.path.join(target, "pmfolder"))
        found = oke.find_agent({"agent": "a"}, [{"name": "a", "cwd": os.path.join(link, "pmfolder")}],
                               os.path.join(target, "pmfolder"))
        self.assertTrue(found)


class TestBoardAndStatus(PmBase):
    def test_jobs_show_where_they_work_or_path_unset(self):
        self.pmc("add", "a")
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.assertEqual(self.new("fix-b", "a"), 0, self.err)
        tree = oke.board_tree(oke.load_state(), [])
        self.assertEqual([(t["name"], sorted(t)) for t in tree], [("a", ["jobs", "key", "name", "pm"])])
        self.assertEqual([j["path"] for j in tree[0]["jobs"]], [oke.native_path(oke.load_state()["jobs"]["fix-a"]["path"]), "path?"])
        st = oke.load_state()
        st["jobs"]["fix-a"]["worktree"] = {"id": "w", "path": "/c/wt/fix-a"}
        oke.save_state(st)
        self.assertEqual(oke.board_tree(oke.load_state(), [])[0]["jobs"][0]["path"], oke.native_path("/c/wt/fix-a"))
        self.assertEqual(self.cli("status"), 0, self.err)
        st = json.loads(self.out)
        self.assertEqual(st["pms"]["a"]["places"], [self.norm(self.repo)])
        self.assertEqual((st["jobs"]["fix-a"]["path"], st["jobs"]["fix-b"]["path"]), (self.norm(self.repo), None))
        self.assertIsNone(st["jobs"]["fix-a"]["split_from"])
        self.assertEqual(self.cli("job", "split", "fix-a", "fix-a-x", "--path", self.folder("x")), 0, self.err)
        self.assertEqual(self.cli("status"), 0, self.err)
        self.assertEqual(json.loads(self.out)["jobs"]["fix-a-x"]["split_from"], "fix-a")  # the PM looks it up


class TestAfterNotices(PmBase):
    def setUp(self):
        PmBase.setUp(self)
        self.pmc("add", "a")
        st = oke.load_state()
        st["pms"]["a"]["pm"] = {"agent": "a"}
        oke.save_state(st)

    def plan(self, job, after):
        self.write("home/jobs/%s/plan.md" % job, "job: %s\nafter: %s\n---\n" % (job, after))

    def texts(self):
        """The PM's mail about after-jobs (not a cancelled job's own notice)."""
        return [t for _, t in oke.inbox_messages("a") if "after-job" in t]

    def kinds(self):
        self.assertEqual(self.cli("pending"), 0, self.err)
        return dict((i["job"], i) for i in json.loads(self.out)["items"])

    def test_a_folder_job_waits_at_its_start_and_becomes_after_ready(self):
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.assertEqual(self.new("notes", "a", "--path", self.folder("notes")), 0, self.err)
        self.plan("notes", "fix-a")
        self.advance("notes", "awaiting_approval")
        self.assertEqual(self.kinds()["notes"]["kind"], "plan")
        self.assertEqual(self.cli("job", "phase", "notes", "working"), 1)  # the user approved; the gate holds it
        self.assertIn("waiting for fix-a to merge", self.err)
        self.assertIn("Write no assignment", self.err)
        st = oke.load_state()["jobs"]["notes"]
        self.assertEqual((st["phase"], st["approved"], st["start_after"]), ("awaiting_approval", True, True))
        self.assertNotIn("notes", self.kinds())  # nothing to ask while it waits
        board = dict((j["job"], j) for pm in oke.board_tree(oke.load_state(), []) for j in pm["jobs"])
        self.assertEqual(board["notes"]["after_wait"], ["fix-a"])
        lines = "\n".join(t for _, t in oke.board_lines(oke.board_tree(oke.load_state(), [])))
        self.assertIn("waiting for after-job fix-a", lines)
        self.advance("fix-a", "merging")
        oke.ledger_add("fix-a", "oke", "abc1234", "merge", "pass")
        self.assertEqual(self.cli("job", "verify", "fix-a", "--accept", "user checked"), 0, self.err)
        item = self.kinds()["notes"]
        self.assertEqual((item["kind"], item["rank"]), ("after_ready", 0))
        self.assertEqual(item["text"], "job notes may start: after-job fix-a is finished")
        self.assertEqual(self.texts(), [])  # no mail: the orchestrator starts it
        self.assertEqual(self.cli("job", "phase", "notes", "working"), 0, self.err)
        self.assertNotEqual((self.kinds().get("notes") or {}).get("kind"), "after_ready")
        self.assertNotIn("start_after", oke.load_state()["jobs"]["notes"])

    def gate(self, job, after, *extra):
        """A folder job approved and held at its start by `after`."""
        self.assertEqual(self.new(job, "a", *extra), 0, self.err)
        self.plan(job, after)
        self.advance(job, "awaiting_approval")
        self.assertEqual(self.cli("job", "phase", job, "working"), 1)
        return self.err

    def test_a_cancelled_after_job_asks_the_user_and_the_after_can_be_dropped(self):
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.gate("notes", "fix-a", "--path", self.folder("notes"))
        self.assertNotIn("notes", self.kinds())
        self.assertEqual(self.cli("job", "cancel", "fix-a"), 0, self.err)
        item = self.kinds()["notes"]
        self.assertEqual((item["kind"], item["after"], item["phase"]), ("after_broken", "fix-a", "awaiting_approval"))
        self.assertEqual(item["text"], "job notes cannot start: after-job fix-a was cancelled; ask the user: "
                                       "drop it (`$OKE job after notes --drop fix-a`) or cancel notes")
        self.assertEqual(self.texts(), [])  # its PM is not told to ask too: one question, from the orchestrator
        self.assertEqual(self.cli("job", "after", "notes", "--drop", "nope"), 1)
        self.assertEqual(self.cli("job", "after", "notes", "--drop", "fix-a"), 0, self.err)  # the user's call
        self.assertEqual(json.loads(self.out)["after"], [])
        self.assertNotIn("after", oke.read_header(os.path.join(self.home, "jobs", "notes", "plan.md")))
        item = self.kinds()["notes"]
        self.assertEqual((item["kind"], item["text"]), ("after_ready", "job notes may start: its after-jobs were dropped"))
        self.assertEqual(self.cli("job", "phase", "notes", "working"), 0, self.err)

    def test_two_jobs_waiting_for_each_other_ask_the_user(self):
        self.assertEqual(self.new("n2", "a", "--path", self.folder("n2")), 0, self.err)
        self.plan("n2", "notes")
        self.gate("notes", "n2", "--path", self.folder("notes"))
        item = self.kinds()["notes"]
        self.assertEqual(item["kind"], "after_broken", item)
        self.assertIn("after-job n2 itself waits for notes (a cycle)", item["text"])
        self.assertEqual(self.cli("job", "after", "notes", "--drop", "n2"), 0, self.err)
        self.assertEqual(self.kinds()["notes"]["kind"], "after_ready")

    def test_a_mistyped_after_job_asks_the_user(self):
        self.assertIn("after-job fix-z is no known job", self.gate("notes", "fix-z, other", "--path", self.folder("n")))
        item = self.kinds()["notes"]
        self.assertEqual(item["kind"], "after_broken")
        self.assertIn("after-job fix-z is no known job; ask the user: drop it (`$OKE job after notes --drop fix-z`)", item["text"])
        self.assertEqual(self.cli("job", "after", "notes", "--drop", "fix-z"), 0, self.err)
        self.assertEqual(oke.read_header(os.path.join(self.home, "jobs", "notes", "plan.md"))["after"], "other")

    def test_a_report_still_shows_while_the_job_waits_at_its_start(self):
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.gate("notes", "fix-a", "--path", self.folder("notes"))
        self.write("home/jobs/notes/pm/assignment.md", "agent: a\nrole: pm\njob: notes\nseq: 1\n---\n")
        st = oke.load_state()
        st["jobs"]["notes"]["sent_seq"] = 1
        oke.save_state(st)
        self.assertNotIn("notes", self.kinds())
        self.write("home/jobs/notes/pm/report.md", "type: question\nseq: 1\n---\nwhich file?\n")
        self.assertEqual(self.kinds()["notes"]["kind"], "question")

    def test_an_approval_cut_before_its_assignment_is_unassigned_not_a_done_report(self):
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.write("home/jobs/fix-a/pm/assignment.md", "agent: a\nrole: pm\njob: fix-a\nseq: 1\n---\n## Instruction\n\nkind: answer\n")  # planning's last
        self.write("home/jobs/fix-a/pm/report.md", "type: done\nseq: 1\n---\nthe plan\n")
        st = oke.load_state()
        st["jobs"]["fix-a"]["sent_seq"] = 1
        oke.save_state(st)
        self.advance("fix-a", "awaiting_approval")
        self.assertEqual(self.cli("job", "phase", "fix-a", "working"), 0, self.err)  # approved: phase first
        item = self.kinds()["fix-a"]  # compacted here: the plan report must not read as work done
        self.assertEqual(item["kind"], "unassigned")
        self.assertIn("was never written", item["text"])
        self.write("home/jobs/fix-a/pm/assignment.md", "agent: a\nrole: pm\njob: fix-a\nseq: 2\n---\n## Instruction\n\nkind: execute\n")
        self.assertEqual(self.kinds()["fix-a"]["kind"], "unsent")  # written, not yet sent: the next step
        st = oke.load_state()
        st["jobs"]["fix-a"]["sent_seq"] = 2
        oke.save_state(st)
        self.assertNotIn("fix-a", self.kinds())  # sent: working, nothing to ask
        self.write("home/jobs/fix-a/pm/assignment.md", "agent: a\nrole: pm\njob: fix-a\nseq: 3\n---\n## Instruction\n\nkind: answer\n")
        st = oke.load_state()
        st["jobs"]["fix-a"]["sent_seq"] = 3
        oke.save_state(st)
        self.assertNotIn("fix-a", self.kinds())  # an answer while working is no unassigned
        self.write("home/jobs/fix-a/pm/report.md", "type: done\nseq: 3\n---\nwork reviewed\n")
        self.assertEqual(self.kinds()["fix-a"]["kind"], "report_done")  # the work's own report

    def test_an_approval_after_an_unanswered_peer_answer_is_still_unassigned(self):
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.write("home/jobs/fix-a/pm/report.md", "type: done\nseq: 1\n---\nthe plan\n")
        # "proceed as if no impact" went out while the plan waited; its report is not in yet
        self.write("home/jobs/fix-a/pm/assignment.md", "agent: a\nrole: pm\njob: fix-a\nseq: 2\n---\n## Instruction\n\nkind: answer\n")
        st = oke.load_state()
        st["jobs"]["fix-a"]["sent_seq"] = 2
        oke.save_state(st)
        self.advance("fix-a", "awaiting_approval")
        self.assertEqual(self.cli("job", "phase", "fix-a", "working"), 0, self.err)
        self.write("home/jobs/fix-a/pm/report.md", "type: done\nseq: 2\n---\nthe plan, as answered\n")
        self.assertEqual(self.kinds()["fix-a"]["kind"], "unassigned")  # never work reviewed

    def test_a_curate_start_cut_before_its_assignment_is_unassigned(self):
        self.assertEqual(self.new("look", "a", "--research"), 0, self.err)
        self.assertEqual(self.cli("interview", "look", "--goal", 1, "--constraints", 1, "--criteria", 1), 0)
        self.assertEqual(self.cli("job", "phase", "look", "working"), 0, self.err)  # curate.md: phase first
        item = self.kinds()["look"]
        self.assertEqual(item["kind"], "unassigned")
        self.assertIn("was never written", item["text"])

    def research_sent(self, seq, kind):
        self.write("home/jobs/look/pm/assignment.md",
                   "agent: a\nrole: pm\njob: look\nseq: %d\n---\n## Instruction\n\nkind: %s\n" % (seq, kind))
        st = oke.load_state()
        st["jobs"]["look"]["sent_seq"] = seq
        oke.save_state(st)

    def test_a_phase_set_after_its_assignment_or_again_marks_nothing(self):
        self.assertEqual(self.new("look", "a", "--research"), 0, self.err)
        self.assertEqual(self.cli("interview", "look", "--goal", 1, "--constraints", 1, "--criteria", 1), 0)
        self.research_sent(1, "research")  # "Give the job to the PM": write (step 2), then the phase (step 3)
        self.assertEqual(self.cli("job", "phase", "look", "working"), 0, self.err)
        self.assertNotIn("look", self.kinds())
        self.write("home/jobs/look/pm/report.md", "type: done\nseq: 1\n---\nfindings\n")
        self.assertEqual(self.kinds()["look"]["kind"], "report_done")
        self.research_sent(2, "dialog")  # a dialog while working: step 3 sets working again
        self.assertEqual(self.cli("job", "phase", "look", "working"), 0, self.err)
        self.write("home/jobs/look/pm/report.md", "type: done\nseq: 2\n---\nfindings\n")
        self.assertEqual(self.kinds()["look"]["kind"], "report_done")  # not unassigned

    def test_research_jobs_are_not_split(self):
        self.assertEqual(self.new("look", "a", "--research"), 0, self.err)
        self.assertEqual(self.cli("job", "split", "look", "look-b", "--path", self.folder("b")), 1)
        self.assertIn("research jobs are not split", self.err)

    def test_a_git_split_job_at_its_merge_asks_once_when_its_after_job_is_cancelled(self):
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        api = self.git_repo("api")
        self.assertEqual(self.cli("job", "split", "fix-a", "fix-a-api", "--path", api, "--after", "fix-a"), 0,
                         self.err)
        self.advance("fix-a-api", "merging")
        self.write("home/jobs/fix-a-api/pm/assignment.md", "agent: a\nrole: pm\njob: fix-a-api\nseq: 1\n---\n")
        self.write("home/jobs/fix-a-api/pm/report.md", "type: waiting\nseq: 1\non: fix-a\n---\n")
        st = oke.load_state()
        st["jobs"]["fix-a-api"]["sent_seq"] = 1  # the PM keeps its merge waiting
        oke.save_state(st)
        self.assertEqual(self.cli("job", "merge", "fix-a-api"), 1)
        self.assertIn("waiting for fix-a to merge", self.err)
        self.assertEqual(self.cli("job", "cancel", "fix-a"), 0, self.err)
        self.assertEqual(self.texts(), ["oke: Job fix-a-api: after-job fix-a was cancelled: the orchestrator asks "
                                        "the user; keep the job waiting"])
        self.assertEqual(self.cli("pending"), 0, self.err)
        items = [i for i in json.loads(self.out)["items"] if i["job"] == "fix-a-api"]
        self.assertEqual([(i["kind"], i.get("phase")) for i in items], [("after_broken", "merging")], items)
        self.assertEqual(items[0]["text"], "job fix-a-api cannot merge: after-job fix-a was cancelled; ask the user: "
                                           "drop it (`$OKE job after fix-a-api --drop fix-a`) or cancel fix-a-api")
        self.assertEqual(self.cli("job", "merge", "fix-a-api"), 1)
        self.assertIn("keep the job waiting", self.err)
        self.assertEqual(self.cli("job", "after", "fix-a-api", "--drop", "fix-a"), 0, self.err)  # the split's --after
        self.assertNotIn("after", oke.load_state()["jobs"]["fix-a-api"])
        self.assertNotEqual((self.kinds().get("fix-a-api") or {}).get("kind"), "after_broken")
        self.cli("job", "merge", "fix-a-api")  # the merge goes on past the after check
        self.assertNotIn("after-job", self.err)

    def test_a_folder_job_whose_plan_names_itself_starts(self):
        self.assertEqual(self.new("notes", "a", "--path", self.folder("notes")), 0, self.err)
        self.plan("notes", "notes")
        self.advance("notes", "awaiting_approval")
        self.assertEqual(oke.after_jobs("notes"), [])
        self.assertEqual(self.cli("job", "phase", "notes", "working"), 0, self.err)
        self.assertNotIn("start_after", oke.load_state()["jobs"]["notes"])

    def test_a_job_back_from_review_is_not_held_by_the_gate(self):
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.assertEqual(self.new("notes", "a", "--path", self.folder("notes")), 0, self.err)
        self.advance("notes", "reviewing")
        self.plan("notes", "fix-a")  # an after: that came later
        self.assertEqual(self.cli("job", "phase", "notes", "working"), 0, self.err)
        self.assertNotIn("start_after", oke.load_state()["jobs"]["notes"])

    def seq(self, job, report="waiting"):
        """The PM got its assignment and reported."""
        self.write("home/jobs/%s/pm/assignment.md" % job, "agent: a\nrole: pm\njob: %s\nseq: 1\n---\n" % job)
        self.write("home/jobs/%s/pm/report.md" % job, "type: %s\nseq: 1\n---\n" % report)
        st = oke.load_state()
        st["jobs"][job]["sent_seq"] = 1
        oke.save_state(st)

    def test_a_working_job_hears_of_a_cancelled_after_job_only_at_its_merge(self):
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.assertEqual(self.new("fix-b", "a", "--path", self.repo), 0, self.err)
        self.plan("fix-b", "fix-a")
        self.advance("fix-b", "working")
        self.seq("fix-b")
        self.assertEqual(self.cli("job", "cancel", "fix-a"), 0, self.err)
        self.assertEqual(self.texts(), [])  # it keeps working: nothing to wait for yet
        self.assertNotIn("fix-b", self.kinds())
        self.assertEqual(self.cli("job", "phase", "fix-b", "awaiting_merge"), 0, self.err)
        item = self.kinds()["fix-b"]
        self.assertEqual((item["kind"], item["phase"]), ("after_broken", "awaiting_merge"))
        self.assertIn("job fix-b cannot merge: after-job fix-a was cancelled", item["text"])
        self.assertEqual(self.cli("job", "after", "fix-b", "--drop", "fix-a"), 0, self.err)
        self.assertEqual(self.kinds()["fix-b"]["kind"], "merge")  # the merge question comes back by itself

    def test_a_started_folder_job_gets_no_mail_when_its_after_job_is_cancelled(self):
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.assertEqual(self.new("notes", "a", "--path", self.folder("notes")), 0, self.err)
        self.advance("notes", "working")
        self.plan("notes", "fix-a")
        self.assertEqual(self.cli("job", "cancel", "fix-a"), 0, self.err)
        self.assertEqual(self.texts(), [])

    def test_git_jobs_at_their_merge_hear_when_a_folder_job_is_done(self):
        self.assertEqual(self.new("notes", "a", "--path", self.folder("notes")), 0, self.err)
        for job, phase in (("fix-a", "merging"), ("fix-b", "awaiting_merge")):
            self.assertEqual(self.new(job, "a", "--path", self.repo), 0, self.err)
            self.plan(job, "notes")
            self.advance(job, phase)
        self.assertEqual(oke.after_block(oke.load_state(), "notes"), "waiting for notes to be done")
        self.advance("notes", "working")
        self.logged("notes")
        self.assertEqual(self.texts(), [])
        self.assertEqual(self.cli("job", "phase", "notes", "done"), 0, self.err)
        self.assertEqual(sorted(self.texts()), ["oke: Job fix-a: after-job notes is finished",
                                                "oke: Job fix-b: after-job notes is finished"])
        self.assertIsNone(oke.after_block(oke.load_state(), "notes"))


class TestCost(PmBase):
    def test_old_pms_usage_is_its_own_place(self):
        line = u"2026-01-01T00:00:00,opus," + ",".join(["1"] * len(oke.USAGE_FIELDS))
        for rel in ("repos/toy-1234/pm", "pms/a", ""):
            folder = os.path.join(self.home, rel)
            if not os.path.isdir(folder):
                os.makedirs(folder)
            with io.open(os.path.join(folder, "usage.csv"), "w", encoding="utf-8") as f:
                f.write(u"time,model\n" + line + u"\n")
        totals = oke.usage_totals(self.home)
        self.assertEqual(sorted(totals), ["old PMs", "orchestrator", "pms/a"])
        self.assertEqual(totals["orchestrator"]["turns"], 1)
        self.assertEqual(totals["old PMs"]["turns"], 1)


class TestClosedJobs(PmBase):
    def test_a_finished_job_of_a_gone_pm_is_closed_and_has_no_board_line(self):
        self.pmc("add", "a")
        self.assertEqual(self.new("fix-a", "a", "--path", self.repo), 0, self.err)
        self.assertEqual(self.pmc("remove", "a", "--cancel")[0], 0, self.err)
        st = oke.load_state()
        st["jobs"]["old"] = {"git_key": oke.repo_key(self.repo), "phase": "done"}  # an old OKe's job
        oke.save_state(st)
        self.assertEqual(self.cli("status"), 0, self.err)
        jobs = json.loads(self.out)["jobs"]
        self.assertEqual((jobs["fix-a"]["class"], jobs["old"]["class"]), ("closed", "closed"))
        self.assertEqual(oke.board_tree(oke.load_state(), []), [])


class TestMigration(SpawnBase):
    def old_state(self, phase, pm=False):
        job = {"repo": oke.repo_key(self.repo), "phase": phase}
        if pm:
            job["pm"] = "a"
        with io.open(oke.state_path(), "w", encoding="utf-8") as f:
            json.dump({"orchestrator": {}, "repos": {oke.repo_key(self.repo): {"pm": {"agent": "pm-toy"}}},
                       "jobs": {"fix-a": job}}, f)

    def test_an_old_state_with_open_jobs_is_refused(self):
        self.old_state("working")
        with self.assertRaises(oke.StateError) as e:
            oke.load_state()
        self.assertIn("fix-a", str(e.exception))

    def test_an_open_old_job_is_refused_even_without_repositories(self):
        with io.open(oke.state_path(), "w", encoding="utf-8") as f:
            json.dump({"orchestrator": {}, "jobs": {"fix-a": {"repo": oke.repo_key(self.repo), "phase": "planning"},
                                                    "new": {"pm": "a", "path": None, "phase": "intake"}}}, f)
        with self.assertRaises(oke.StateError) as e:
            oke.load_state()
        self.assertIn("fix-a", str(e.exception))
        self.assertNotIn("new", str(e.exception))

    def test_an_old_state_without_open_jobs_drops_the_repositories(self):
        self.old_state("done")
        st = oke.load_state()
        self.assertNotIn("repos", st)
        self.assertEqual(st["pms"], {})
        self.assertEqual(st["jobs"]["fix-a"]["git_key"], oke.repo_key(self.repo))
        self.assertNotIn("repo", st["jobs"]["fix-a"])


class TestOutputs(PmBase):
    """What a job hands over: code (a merge), files (a folder job), result (an answer or a run)."""

    def setUp(self):
        PmBase.setUp(self)
        self.pmc("add", "a")

    def finished(self, job):
        st = oke.load_state()
        st["jobs"][job]["phase"] = "done"
        oke.save_state(st)

    def plan(self, job, worktree=True):
        self.write("home/jobs/%s/plan.md" % job, "job: %s\n---\n## Workspace\n%s\n" % (
            job, ("oke/" + job) if worktree else u"none"))

    def report(self, job, child, verdict=None):
        body = ("verdict: %s\n" % verdict) if verdict else "done\n"
        self.write("home/jobs/%s/%s/report.md" % (job, child), "agent: %s\ntype: done\n---\n%s" % (child, body))

    def worktree(self, job):
        wt = self.path("wt-" + job)
        self.git(self.repo, "worktree", "add", "-q", wt, "-b", "oke/" + job)
        st = oke.load_state()
        st["jobs"][job]["worktree"] = {"id": "w-" + job, "path": wt}
        oke.save_state(st)
        return wt

    def test_the_output_is_set_while_planning_and_shown(self):
        self.assertEqual(self.new("sim", "a", "--path", self.repo), 0, self.err)
        self.assertEqual(oke.job_output(oke.load_state()["jobs"]["sim"]), "code")  # the default
        self.advance("sim", "planning")
        self.assertEqual(self.cli("job", "output", "sim", "result"), 0, self.err)
        self.assertEqual(self.cli("status"), 0, self.err)
        self.assertEqual(json.loads(self.out)["jobs"]["sim"]["output"], "result")
        self.assertEqual(self.new("notes", "a", "--path", self.folder("notes")), 0, self.err)
        self.assertEqual(self.cli("job", "output", "notes", "result"), 1)  # a folder job hands over files
        self.assertIn("its output is the files", self.err)
        self.assertEqual(self.new("look", "a", "--research"), 0, self.err)
        self.assertEqual(self.cli("job", "output", "look", "code"), 1)  # research: its answer
        self.plan("sim")
        self.advance("sim", "working")
        self.assertEqual(self.cli("job", "output", "sim", "code"), 1)  # it stays once it works
        self.assertIn("has worked on its plan", self.err)
        self.assertEqual(self.cli("job", "output", "sim", "result"), 0, self.err)  # the same again: fine

    def test_an_experiment_needs_a_worktree_never_merges_and_ends_on_a_passing_review(self):
        self.assertEqual(self.new("sim", "a", "--path", self.repo), 0, self.err)
        self.assertEqual(self.new("after-sim", "a", "--path", self.folder("notes")), 0, self.err)
        self.write("home/jobs/after-sim/plan.md", "job: after-sim\nafter: sim\n---\n")
        self.advance("sim", "planning")
        self.assertEqual(self.cli("job", "output", "sim", "result"), 0, self.err)
        self.plan("sim", worktree=False)
        self.advance("sim", "awaiting_approval")
        self.assertEqual(self.cli("job", "phase", "sim", "working"), 1)  # its edits would land in main
        self.assertIn("must name branch oke/sim", self.err)
        self.plan("sim")
        self.assertEqual(self.cli("job", "phase", "sim", "working"), 0, self.err)
        self.worktree("sim")
        self.assertEqual(self.cli("job", "phase", "sim", "awaiting_merge"), 1)
        self.assertEqual(self.cli("job", "merge", "sim"), 1)
        self.assertIn("nothing to merge", self.err)
        self.assertEqual(self.cli("wiki", "log", "--job", "sim", "--note", "none"), 0, self.err)
        self.report("sim", "sim-w1")
        self.assertEqual(self.cli("job", "phase", "sim", "done"), 1)  # no review yet
        self.assertIn("verdict: pass", self.err)
        self.report("sim", "sim-rv", "changes")
        self.assertEqual(self.cli("job", "phase", "sim", "done"), 1)
        self.report("sim", "sim-rv", "pass")
        later = os.path.getmtime(self.path("home/jobs/sim/sim-rv/report.md")) + 5
        os.utime(self.path("home/jobs/sim/sim-w1/report.md"), (later, later))  # the worker ran again
        self.assertEqual(self.cli("job", "phase", "sim", "done"), 1)
        os.utime(self.path("home/jobs/sim/sim-rv/report.md"), (later + 5, later + 5))
        self.assertEqual(self.cli("job", "phase", "sim", "done"), 0, self.err)
        self.assertIsNone(oke.after_block(oke.load_state(), "sim"))  # finished for its waiters
        self.assertTrue(oke.wiki_verified("sim", oke.load_state()["jobs"]["sim"]))

    def test_an_archived_experiment_keeps_its_worktree_until_cleaned(self):
        self.assertEqual(self.new("sim", "a", "--path", self.repo), 0, self.err)
        self.advance("sim", "planning")
        self.assertEqual(self.cli("job", "output", "sim", "result"), 0, self.err)
        self.plan("sim")
        self.advance("sim", "awaiting_approval")
        self.assertEqual(self.cli("job", "phase", "sim", "working"), 0, self.err)
        wt = self.worktree("sim")
        self.write("wt-sim/out/run.log", "ok\n")  # an output the repository ignores
        self.finished("sim")  # done (its review is not what these tests check)
        self.assertEqual(self.cli("job", "archive", "sim"), 0, self.err)
        self.assertIn("kept_worktree: " + wt, self.out)
        self.assertEqual(self.cli("status"), 0, self.err)
        self.assertEqual(json.loads(self.out)["kept_worktrees"], [{"job": "sim", "path": wt}])
        self.assertEqual(self.cli("job", "clean", "sim"), 0, self.err)
        self.assertFalse(os.path.isdir(wt))
        self.assertNotIn("branch_left", json.loads(self.out))
        self.assertEqual(subprocess.check_output(["git", "-C", self.repo, "branch", "--list", "oke/sim"]), b"")
        self.assertEqual(self.cli("status"), 0, self.err)
        self.assertNotIn("kept_worktrees", json.loads(self.out))
        self.assertEqual(self.cli("job", "clean", "sim"), 1)  # gone already: the archive says so
        self.assertEqual(self.cli("job", "clean", "nothing"), 1)

    def test_a_code_job_that_changed_nothing_ends_with_no_change(self):
        self.assertEqual(self.new("fix", "a", "--path", self.repo), 0, self.err)
        self.assertEqual(self.new("next", "a", "--path", self.folder("notes")), 0, self.err)
        self.write("home/jobs/next/plan.md", "job: next\nafter: fix\n---\n")
        self.plan("fix")
        self.advance("fix", "working")
        wt = self.worktree("fix")
        self.write("wt-fix/new.py", "x = 1\n")
        self.assertEqual(self.cli("job", "output", "fix", "result", "--no-change"), 1)
        self.assertIn("new files: new.py", self.err)
        os.remove(os.path.join(wt, "new.py"))
        self.write("wt-fix/a.txt", "a\n")
        self.git(wt, "add", "a.txt")
        self.git(wt, "commit", "-q", "-m", "a")
        self.assertEqual(self.cli("job", "output", "fix", "result", "--no-change"), 1)
        self.assertIn("commits on its branch", self.err)
        self.git(wt, "revert", "--no-edit", "HEAD")  # undone, with a commit: no change left
        self.assertEqual(self.cli("job", "output", "fix", "code", "--no-change"), 1)  # it ends as result
        self.assertEqual(self.cli("job", "output", "fix", "result", "--no-change"), 0, self.err)
        info = oke.load_state()["jobs"]["fix"]
        self.assertEqual((oke.job_output(info), info["no_change"]), ("result", True))
        self.assertEqual([(r["by"], r["check"], r["result"]) for r in oke.ledger_rows("fix")],
                         [("orchestrator", "no-change", "accept")])
        self.assertEqual(self.cli("wiki", "log", "--job", "fix", "--note", "none"), 0, self.err)
        self.assertEqual(self.cli("job", "phase", "fix", "done"), 0, self.err)  # no review: the user accepted
        self.assertIsNone(oke.after_block(oke.load_state(), "fix"))
        self.assertEqual(self.cli("job", "archive", "fix"), 0, self.err)
        self.assertNotIn("kept_worktree", self.out)  # an experiment only
        self.assertIsNone(oke.after_block(oke.load_state(), "fix"))  # archived: still finished

    def test_a_start_in_the_main_checkout_takes_in_only_what_the_plan_names(self):
        self.assertEqual(self.new("small", "a", "--path", self.repo), 0, self.err)
        self.plan("small", worktree=False)
        self.advance("small", "awaiting_approval")
        self.write("toy/x.txt", "x\n")
        self.git(self.repo, "add", "x.txt")  # the user's uncommitted change
        self.assertEqual(self.cli("job", "phase", "small", "working"), 1)
        self.assertIn("x.txt", self.err)
        self.assertIn(u"Takes over", self.err)
        self.write("home/jobs/small/plan.md", u"job: small\n---\n## Workspace\nmain checkout\nTakes over: x.txt\n")
        self.assertEqual(oke.plan_uncommitted("small"), ["x.txt"])
        self.assertEqual(self.cli("job", "phase", "small", "working"), 0, self.err)
        self.assertEqual(self.cli("job", "output", "small", "result", "--no-change"), 1)  # x.txt is the job's now
        self.assertIn("x.txt", self.err)

    def test_the_plan_names_korean_files_and_renames_as_git_stores_them(self):
        self.assertEqual(self.new("small", "a", "--path", self.repo), 0, self.err)
        self.advance("small", "awaiting_approval")
        self.write(u"toy/문서.md", "x\n")
        self.git(self.repo, "add", u"문서.md")
        self.write("toy/README.md", "r\n")
        self.git(self.repo, "add", "README.md")
        self.git(self.repo, "commit", "-qm", "r", "README.md")  # 문서.md stays staged, uncommitted
        self.git(self.repo, "mv", "README.md", "READ ME.md")
        self.write("home/jobs/small/plan.md", u"job: small\n---\n## Workspace\nmain checkout\nTakes over: 문서.md\n")
        self.assertEqual(self.cli("job", "phase", "small", "working"), 1)
        self.assertIn("READ ME.md", self.err)
        self.assertNotIn(u"문서", self.err)
        self.write("home/jobs/small/plan.md",
                   u"job: small\n---\n## Workspace\nmain checkout\nTakes over: `문서.md`, \"READ ME.md\"\n")
        self.assertEqual(self.cli("job", "phase", "small", "working"), 0, self.err)

    def test_the_brief_shows_the_path_first_and_the_main_sections(self):
        self.assertEqual(self.new("small", "a", "--path", self.repo), 0, self.err)
        self.write("home/jobs/small/plan.md", u"job: small\n---\n## Approach\nsize: small\n## Workspace\nmain checkout\n"
                   u"Takes over: none\n## Steps\n1. 고친다\n## Acceptance criteria\n1. 통과\n## Check command\n`true`\n"
                   u"## Impact elsewhere\nnone (nothing)\n## Questions for the user\nnone\n")
        self.assertEqual(self.cli("job", "brief", "small"), 0, self.err)
        lines = self.out.splitlines()
        self.assertEqual(lines[1], "path: " + oke.native_path(oke.load_state()["jobs"]["small"]["path"]))
        self.assertIn(u"1. 고친다", self.out)
        self.assertIn("## Check command", self.out)
        self.assertNotIn("Impact elsewhere", self.out)
        self.assertTrue(lines[-1].startswith("plan: "))
        # the mod's form: only jobs awaiting approval
        self.assertEqual(self.cli("job", "brief", "--json"), 0, self.err)
        self.assertEqual(json.loads(self.out.splitlines()[-1]), {"briefs": []})
        st = oke.load_state()
        st["jobs"]["small"]["phase"] = "awaiting_approval"
        oke.save_state(st)
        self.assertEqual(self.cli("job", "brief", "--json"), 0, self.err)
        briefs = json.loads(self.out.splitlines()[-1])["briefs"]
        self.assertEqual([b["job"] for b in briefs], ["small"])
        self.assertIn(u"1. 고친다", briefs[0]["brief"])

    def test_the_workspace_section(self):
        self.assertEqual(self.new("small", "a", "--path", self.repo), 0, self.err)
        self.write("home/jobs/small/plan.md", u"job: small\n---\n## Workspace\nmain checkout\nTakes over: a.py, b.py\n")
        self.assertEqual(oke.plan_uncommitted("small"), ["a.py", "b.py"])
        self.write("home/jobs/small/plan.md", u"job: small\n---\n## Workspace\nnew worktree (branch oke/small)\n")
        self.assertTrue(oke.plan_worktree("small"))
        self.write("home/jobs/small/plan.md", u"job: small\n---\n## Workspace\nmain checkout\nTakes over: none\n")
        self.assertEqual(oke.plan_uncommitted("small"), [])
        self.assertFalse(oke.plan_worktree("small"))

    def test_a_file_under_oke_slash_job_is_not_a_worktree(self):
        self.assertEqual(self.new("bin", "a", "--path", self.repo), 0, self.err)
        self.write("home/jobs/bin/plan.md", u"job: bin\n---\n## Workspace\nmain checkout\nTakes over: oke/bin/x.py\n")
        self.assertFalse(oke.plan_worktree("bin"))

    def test_a_start_in_a_worktree_ignores_the_main_checkout(self):
        self.assertEqual(self.new("fix", "a", "--path", self.repo), 0, self.err)
        self.plan("fix")
        self.advance("fix", "awaiting_approval")
        self.write("toy/x.txt", "x\n")
        self.git(self.repo, "add", "x.txt")
        self.advance("fix", "working")
        self.assertIs(oke.load_state()["jobs"]["fix"]["worked_in_main"], False)

    def test_no_change_in_the_main_checkout_and_before_it_worked(self):
        self.assertEqual(self.new("look", "a", "--path", self.repo), 0, self.err)
        self.assertEqual(self.new("small", "a", "--path", self.repo), 0, self.err)
        self.advance("look", "awaiting_approval")
        self.write("toy/x.txt", "x\n")
        self.git(self.repo, "add", "x.txt")  # someone else's change: look never worked
        self.assertEqual(self.cli("job", "output", "look", "result", "--no-change"), 0, self.err)
        self.assertEqual(self.cli("wiki", "log", "--job", "look", "--note", "none"), 0, self.err)
        self.assertEqual(self.cli("job", "phase", "look", "done"), 0, self.err)
        self.git(self.repo, "commit", "-qm", "x")  # the user commits it before small starts in the main checkout
        self.plan("small", worktree=False)
        self.advance("small", "working")  # it works in the main checkout: its changes are there
        self.assertEqual(self.cli("job", "output", "small", "result", "--no-change"), 0, self.err)
        st = oke.load_state()
        st["jobs"]["small"].pop("no_change")
        st["jobs"]["small"]["output"] = "code"
        oke.save_state(st)
        self.write("toy/y.txt", "y\n")
        self.git(self.repo, "add", "y.txt")  # the job's own change
        self.assertEqual(self.cli("job", "output", "small", "result", "--no-change"), 1)
        self.assertIn("changed files: y.txt", self.err)
        self.git(self.repo, "rm", "-q", "--cached", "y.txt")
        os.remove(os.path.join(self.repo, "y.txt"))
        self.assertEqual(self.cli("job", "output", "small", "result", "--no-change"), 0, self.err)

    def test_a_no_change_job_leaves_no_worktree_behind(self):
        self.assertEqual(self.new("fix", "a", "--path", self.repo), 0, self.err)
        self.plan("fix")
        self.advance("fix", "working")
        wt = self.worktree("fix")
        with io.open(os.path.join(wt, "f.txt"), "w", encoding="utf-8") as f:
            f.write(u"f\n")
        self.git(wt, "add", "f.txt")
        self.git(wt, "commit", "-q", "-m", "f")
        self.git(wt, "revert", "--no-edit", "HEAD")  # the PM's undo: commits stay, the change does not
        os.makedirs(os.path.join(wt, "__pycache__"))  # a run's byproduct: git alone would keep the folder
        with io.open(os.path.join(wt, "__pycache__", "a.pyc"), "w", encoding="utf-8") as f:
            f.write(u"x")
        self.assertEqual(self.cli("job", "output", "fix", "result", "--no-change"), 0, self.err)
        self.assertEqual(self.cli("wiki", "log", "--job", "fix", "--note", "none"), 0, self.err)
        self.assertEqual(self.cli("job", "archive", "fix"), 1)  # not done yet: it stays open
        self.assertIn("not done or failed", self.err)
        self.assertEqual(self.cli("job", "phase", "fix", "done"), 0, self.err)
        self.assertEqual(self.cli("job", "archive", "fix"), 0, self.err)
        self.assertFalse(os.path.isdir(wt))
        self.assertNotIn("kept_worktree", self.out)
        self.assertEqual(subprocess.check_output(["git", "-C", self.repo, "branch", "--list", "oke/fix"]), b"")

    def test_a_replan_records_the_output_again(self):
        self.assertEqual(self.new("sim", "a", "--path", self.repo), 0, self.err)
        self.advance("sim", "planning")
        self.assertEqual(self.cli("job", "output", "sim", "result"), 0, self.err)
        self.advance("sim", "awaiting_approval")
        self.assertEqual(self.cli("job", "phase", "sim", "planning"), 0, self.err)  # the user wants a fix
        self.assertEqual(self.cli("job", "output", "sim", "code"), 0, self.err)
        self.assertEqual(oke.job_output(oke.load_state()["jobs"]["sim"]), "code")

    def test_a_replan_after_the_approval_may_change_an_output_nothing_used_yet(self):
        self.assertEqual(self.new("sim", "a", "--path", self.repo), 0, self.err)
        self.plan("sim")
        self.advance("sim", "working")
        wt = self.worktree("sim")
        self.assertEqual(self.cli("job", "phase", "sim", "planning"), 0, self.err)  # the user wants another way
        self.assertEqual(self.cli("job", "output", "sim", "code"), 0, self.err)  # the same: always fine
        self.write("wt-sim/a.txt", "a\n")
        self.assertEqual(self.cli("job", "output", "sim", "result"), 1)  # it made a change already
        self.assertIn("Report a question", self.err)
        os.remove(os.path.join(wt, "a.txt"))
        self.assertEqual(self.cli("job", "output", "sim", "result"), 0, self.err)
        self.assertEqual(oke.job_output(oke.load_state()["jobs"]["sim"]), "result")

    def test_a_job_without_a_path_yet_ends_with_no_change(self):
        self.assertEqual(self.new("idea", "a"), 0, self.err)
        self.advance("idea", "planning")
        self.assertEqual(self.cli("job", "output", "idea", "result"), 1)  # an output needs the path
        self.assertEqual(self.cli("job", "output", "idea", "result", "--no-change"), 0, self.err)
        self.assertEqual(self.cli("wiki", "log", "--job", "idea", "--note", "none"), 0, self.err)
        self.assertEqual(self.cli("job", "phase", "idea", "done"), 0, self.err)

    def test_clean_removes_through_herdr_the_workspace_found_by_its_path(self):
        self.assertEqual(self.new("sim", "a", "--path", self.repo), 0, self.err)
        self.advance("sim", "planning")
        self.assertEqual(self.cli("job", "output", "sim", "result"), 0, self.err)
        self.plan("sim")
        self.advance("sim", "awaiting_approval")
        self.assertEqual(self.cli("job", "phase", "sim", "working"), 0, self.err)
        wt = self.worktree("sim")  # stored id w-sim: herdr may give that id to another workspace later
        self.finished("sim")  # done (its review is not what these tests check)
        self.assertEqual(self.cli("job", "archive", "sim"), 0, self.err)
        self.fake({"worktree list": [J({"worktrees": [
                       {"path": self.repo, "is_linked_worktree": False, "open_workspace_id": "w2"},
                       {"path": "\\\\?\\" + wt, "is_linked_worktree": True, "open_workspace_id": "w9"}]})],
                   "worktree remove": [J({})]})
        self.assertEqual(self.cli("job", "clean", "sim"), 0, self.err)
        removes = [c for c in self.calls() if c[:2] == ["worktree", "remove"]]
        self.assertEqual(removes, [["worktree", "remove", "--workspace", "w9", "--force"]])
        self.assertFalse(os.path.isdir(wt))  # the fake removed nothing: git did

    def test_no_change_writes_the_wiki_line_the_pm_left_out(self):
        self.assertEqual(self.new("idea", "a", "--path", self.repo), 0, self.err)
        st = oke.load_state()
        st["pms"]["a"]["pm"]["agent"] = "a"  # a PM that has run
        oke.save_state(st)
        self.advance("idea", "awaiting_approval")
        self.assertEqual(self.cli("job", "output", "idea", "result", "--no-change"), 0, self.err)
        self.assertEqual(self.cli("job", "phase", "idea", "done"), 0, self.err)  # no round to the PM
        with io.open(os.path.join(oke.wiki_dir("a"), "log.md"), encoding="utf-8") as f:
            self.assertIn("idea: ended with no change", f.read())
        self.assertTrue(any("ended with no change" in t for _, t in oke.inbox_messages("a")))  # the PM is told

    def test_no_change_ignores_the_main_checkout_of_a_job_still_without_its_worktree(self):
        self.assertEqual(self.new("fix", "a", "--path", self.repo), 0, self.err)
        self.plan("fix")
        self.advance("fix", "working")  # approved, but `oke worktree` never ran
        self.write("toy/user.txt", "mine\n")
        self.git(self.repo, "add", "user.txt")
        self.assertEqual(self.cli("job", "output", "fix", "result", "--no-change"), 0, self.err)

    def test_a_replan_into_a_worktree_still_sees_the_edits_left_in_the_main_checkout(self):
        self.assertEqual(self.new("fix", "a", "--path", self.repo), 0, self.err)
        self.plan("fix", worktree=False)
        self.advance("fix", "working")  # worked in the main checkout
        self.write("toy/a.txt", "edited by the job\n")
        self.git(self.repo, "add", "a.txt")
        self.plan("fix")  # the replan moves it to a worktree
        self.advance("fix", "planning")
        self.assertEqual(self.cli("job", "output", "fix", "result", "--no-change"), 1)
        self.assertIn("a.txt", self.err)
        self.worktree("fix")  # its new worktree is clean: the main checkout still counts
        self.assertEqual(self.cli("job", "output", "fix", "result", "--no-change"), 1)
        self.assertIn("main checkout: a.txt", self.err)
        st = oke.load_state()
        st["jobs"]["fix"].pop("worktree")
        st["jobs"]["fix"].pop("worked_in_main")  # a job from before the flag, its plan without a worktree
        oke.save_state(st)
        self.plan("fix", worktree=False)
        self.assertEqual(self.cli("job", "output", "fix", "result", "--no-change"), 1)

    def test_an_experiment_is_never_small(self):
        self.assertEqual(self.new("sim", "a", "--path", self.repo), 0, self.err)
        self.advance("sim", "planning")
        self.assertEqual(self.cli("job", "size", "sim", "small"), 0, self.err)
        self.assertEqual(self.cli("job", "output", "sim", "result"), 0, self.err)
        self.assertEqual(json.loads(self.out)["size"], "normal")
        self.assertEqual(oke.load_state()["jobs"]["sim"]["size"], "normal")
        self.assertEqual(self.cli("job", "size", "sim", "small"), 1)
        self.assertIn("normal or large", self.err)

    def test_a_no_change_job_is_not_asked_again_before_done(self):
        self.assertEqual(self.new("fix", "a", "--path", self.repo), 0, self.err)
        self.advance("fix", "planning")
        self.assertEqual(self.cli("job", "output", "fix", "result", "--no-change"), 0, self.err)
        self.assertEqual(self.cli("pending"), 0, self.err)
        kinds = [i["kind"] for i in json.loads(self.out)["items"] if i["job"] == "fix"]
        self.assertEqual(kinds, ["no_change"])
        self.assertEqual(self.cli("wiki", "log", "--job", "fix", "--note", "none"), 0, self.err)
        self.assertEqual(self.cli("job", "phase", "fix", "done"), 0, self.err)
        self.assertEqual(self.cli("pending"), 0, self.err)
        self.assertFalse([i for i in json.loads(self.out)["items"] if i["job"] == "fix"])

    def test_a_folder_job_ends_with_no_change_on_the_users_word(self):
        notes = self.folder("notes")
        self.assertEqual(self.new("notes", "a", "--path", notes), 0, self.err)
        self.advance("notes", "working")
        self.write("notes/a.txt", "a\n")  # outside git: nothing to compare with
        self.assertEqual(self.cli("job", "output", "notes", "result", "--no-change"), 0, self.err)
        info = oke.load_state()["jobs"]["notes"]
        self.assertEqual((oke.job_output(info), info["no_change"]), ("files", True))
        self.assertEqual(self.cli("wiki", "log", "--job", "notes", "--note", "none"), 0, self.err)
        self.assertEqual(self.cli("job", "phase", "notes", "done"), 0, self.err)

    def test_the_no_change_refusal_names_the_files(self):
        self.write("toy/src/a.py", "a = 1\n")
        self.git(self.repo, "add", "src/a.py")
        self.git(self.repo, "commit", "-q", "-m", "a")
        self.assertEqual(self.new("fix", "a", "--path", self.repo), 0, self.err)
        self.plan("fix", worktree=False)
        self.advance("fix", "working")
        self.write("toy/src/a.py", "a = 2\n")
        self.assertEqual(self.cli("job", "output", "fix", "result", "--no-change"), 1)
        self.assertIn("changed files: src/a.py", self.err)

    def test_clean_finds_the_experiment_among_archives_of_one_name(self):
        self.assertEqual(self.new("sim", "a", "--path", self.repo), 0, self.err)
        self.advance("sim", "planning")
        self.assertEqual(self.cli("job", "output", "sim", "result"), 0, self.err)
        self.plan("sim")
        self.advance("sim", "awaiting_approval")
        self.assertEqual(self.cli("job", "phase", "sim", "working"), 0, self.err)
        wt = self.worktree("sim")
        self.finished("sim")  # done (its review is not what these tests check)
        self.assertEqual(self.cli("job", "archive", "sim"), 0, self.err)
        later = self.path("home/archive/sim-29991231-235959")  # a later archive of that name
        os.makedirs(later)
        oke.write_json_atomic(os.path.join(later, "job.json"), {"size": "research", "phase": "done"})
        self.assertEqual(self.cli("job", "clean", "sim"), 0, self.err)
        self.assertFalse(os.path.isdir(wt))

    def test_only_the_orchestrator_ends_a_job_with_no_change(self):
        self.assertEqual(self.new("fix", "a", "--path", self.repo), 0, self.err)
        self.advance("fix", "planning")
        st = oke.load_state()
        st["pms"]["a"]["pm"] = {"agent": "a", "pane": "p-pm"}
        oke.save_state(st)
        os.environ["HERDR_PANE_ID"] = "p-pm"
        self.fake({"agent list": [J({"agents": [AG("a", pane="p-pm")]})]})
        try:
            self.assertEqual(self.cli("job", "output", "fix", "result", "--no-change"), 1)
            self.assertIn("only the orchestrator", self.err)
            self.assertEqual(self.cli("job", "output", "fix", "result"), 0, self.err)  # planning: the PM's call
        finally:
            del os.environ["HERDR_PANE_ID"]


if __name__ == "__main__":
    unittest.main()
