"""The runtime commands the skills call instead of herdr (runtime layer, step 2)."""
import io
import json
import os
import shutil
import stat
import unittest

import oke
import runtime_herdr
from test_oke import Base, J, E, OK, spawn_ok, SpawnBase  # noqa: F401


class TestRuntimeCommands(SpawnBase):
    def argv(self):
        return [c["argv"] if isinstance(c, dict) else c for c in self.calls()]

    def test_agents_lists_names(self):
        self.fake({"agent list": [J({"agents": [{"name": "pm-a"}, {"name": "j-w1"}, {}]})]})
        self.assertEqual(self.cli("agents"), 0)
        self.assertEqual(json.loads(self.out), ["j-w1", "pm-a"])

    def test_read_keys_attach_close(self):
        self.fake({"agent read": [{"stdout": "screen\n", "exit": 0}], "agent send-keys": [J({})],
                   "agent focus": [J({})], "pane close": [J({})]})
        self.assertEqual(self.cli("read", "pm-a"), 0)
        self.assertEqual(self.out, "screen\n")
        self.assertEqual(self.cli("keys", "pm-a", "down", "enter"), 0)
        self.assertEqual(self.cli("attach", "pm-a"), 0)
        self.assertEqual(self.cli("close", "w1:p4"), 0)
        joined = [" ".join(a) for a in self.argv()]
        self.assertIn("agent send-keys pm-a down enter", joined)
        self.assertIn("agent focus pm-a", joined)
        self.assertIn("pane close w1:p4", joined)

    def test_close_takes_several_panes_and_errors_name_the_runtime(self):
        self.fake({"pane close": [J({})]})
        self.assertEqual(self.cli("close", "w1:p4", "w1:p5"), 0, self.err)
        joined = [" ".join(a) for a in self.argv()]
        self.assertIn("pane close w1:p4", joined)
        self.assertIn("pane close w1:p5", joined)
        self.assertEqual(json.loads(self.out)["closed"], ["w1:p4", "w1:p5"])
        self.fake({"pane close": [E("pane_not_found", "pane w1:p3 not found"), J({})]})
        self.assertEqual(self.cli("close", "w1:p3", "w1:p6"), 1)
        out = json.loads(self.out)
        self.assertEqual(out["closed"], ["w1:p6"])  # the first failing did not stop the second
        self.assertIn("pane_not_found", out["failed"]["w1:p3"])
        self.fake({"pane close": [J({}), E("herdr_unavailable", "down")]})
        self.assertEqual(self.cli("close", "w1:p4", "w1:p5"), 2)
        self.assertEqual(json.loads(self.out)["closed"], ["w1:p4"])  # what closed before the outage
        self.fake({"pane close": [E("pane_not_found", "pane w1:p9 not found")]})
        self.assertEqual(self.cli("close", "w1:p9"), 1)
        self.assertIn("herdr error: pane_not_found", self.err)

    def test_place_splits_next_to_a_pane_in_the_pm_folder(self):
        self.assertEqual(self.cli("pm", "add", "pm-toy"), 0, self.err)
        self.fake({"pane list": [J({"panes": [{"pane_id": "w1:p1", "cwd": self.repo},
                                              {"pane_id": "w2:p3", "cwd": oke.pm_dir("pm-toy")}]})],
                   "pane split": [J({"pane": {"pane_id": "w2:p4"}})]})
        self.assertEqual(self.cli("place", "pm-toy"), 0, self.err)
        self.assertEqual(json.loads(self.out), {"place": "w2:p4"})
        self.assertIn("--pane w2:p3", " ".join(self.argv()[-1]))

    def test_place_opens_a_workspace_labelled_with_the_pm(self):
        self.assertEqual(self.cli("pm", "add", "backend"), 0, self.err)
        self.fake({"pane list": [J({"panes": []})],
                   "workspace create": [J({"root_pane": {"pane_id": "w5:p1"}})]})
        self.assertEqual(self.cli("place", "backend"), 0, self.err)
        self.assertEqual(json.loads(self.out), {"place": "w5:p1"})
        create = [a for a in self.argv() if a[:2] == ["workspace", "create"]][0]
        self.assertEqual(create[create.index("--label") + 1], "backend")
        self.assertEqual(oke.normalize_path(create[create.index("--cwd") + 1]), oke.normalize_path(oke.pm_dir("backend")))

    def test_place_refuses_an_unknown_pm(self):
        self.fake({})
        self.assertEqual(self.cli("place", self.repo), 1)
        self.assertIn("pm add", self.err)
        self.assertEqual(self.argv(), [])

    TAB = J({"type": "tab_created", "tab": {"tab_id": "w1:t4", "workspace_id": "w1"},
             "root_pane": {"pane_id": "w1:p9", "tab_id": "w1:t4", "workspace_id": "w1"}})
    PM_PANE = J({"pane": {"pane_id": "w1:p3", "workspace_id": "w1", "terminal_id": "term-3"}})

    def pm_pane(self, pane):
        state = oke.load_state()
        state["pms"][self.pm]["pm"]["pane"] = pane
        oke.save_state(state)

    def wt_path(self, job):
        return runtime_herdr.worktree_folder(self.repo, "oke/" + job)

    def panes(self, *tabs):
        """`pane list` with the PM's own tab w1:t1, and a pane in each (tab, cwd) given."""
        out = [{"pane_id": "w1:p3", "tab_id": "w1:t1", "workspace_id": "w1", "cwd": oke.pm_dir(self.pm)}]
        for n, (tab, cwd) in enumerate(tabs):
            out.append({"pane_id": "%s:p%d" % (tab.split(":")[0], 20 + n), "tab_id": tab,
                        "workspace_id": tab.split(":")[0], "cwd": cwd})
        return J({"panes": out})

    def closes(self):
        return [a for a in self.argv() if a[1:2] == ["close"]]

    def test_worktree_opens_a_tab_in_the_pm_workspace_and_remove_closes_it(self):
        self.fake({"pane get": [self.PM_PANE], "tab create": [self.TAB]})
        self.assertEqual(self.cli("worktree", "fix-a"), 1)  # no such job
        self.job("fix-a", "working")
        self.pm_pane("w1:p3")
        self.assertEqual(self.cli("worktree", "fix-a"), 0, self.err)
        wt = self.wt_path("fix-a")
        self.assertEqual(json.loads(self.out), {"id": "w1:t4", "path": wt, "place": "w1:p9"})
        self.assertEqual(oke.git_out(wt, "rev-parse", "--abbrev-ref", "HEAD")[1].strip(), "oke/fix-a")
        self.assertIn(["pane", "get", "w1:p3"], self.argv())
        create = [a for a in self.argv() if a[:2] == ["tab", "create"]][0]
        self.assertEqual(create[create.index("--workspace") + 1], "w1")
        self.assertEqual(create[create.index("--cwd") + 1], wt)
        self.assertEqual(create[create.index("--label") + 1], "fix-a")
        self.assertIn("--no-focus", create)
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["worktree"], {"id": "w1:t4", "path": wt, "tabs": ["w1:t4"]})
        # the tab OKe opened is closed (its id recorded, its panes in the checkout); a tab the user
        # opened there (w1:t6) stays
        self.fake({"pane list": [self.panes(("w1:t4", wt), ("w1:t6", wt))], "tab close": [J({"type": "ok"})]})
        removed_after_close = []
        remove = oke.runtime_bg.git_worktree_remove
        oke.runtime_bg.git_worktree_remove = lambda *a, **k: removed_after_close.append(
            ["tab", "close", "w1:t4"] in self.argv()) or remove(*a, **k)
        self.addCleanup(setattr, oke.runtime_bg, "git_worktree_remove", remove)
        self.assertEqual(self.cli("worktree", "--remove", "w1:t4", "--job", "fix-a"), 0, self.err)
        self.assertEqual(self.closes(), [["tab", "close", "w1:t4"]])  # never the user's w1:t6
        self.assertEqual(removed_after_close, [True])  # the tab first: its shell sits in the folder
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(oke.git_out(self.repo, "rev-parse", "-q", "--verify", "oke/fix-a")[0], 0)  # the branch stays

    def test_the_tab_opens_next_to_the_calling_pm_else_a_pm_pane_still_there(self):
        self.job("fix-a", "working")
        self.pm_pane("w1:p3")
        state = oke.load_state()
        state["pms"][self.pm]["pm"].update(agent=self.pm, terminal="term-3")
        oke.save_state(state)
        os.environ.update(HERDR_ENV="1", HERDR_PANE_ID="w4:p1")
        # the caller is not the PM: the PM's recorded pane, asked first whether it is there
        self.fake({"pane get": [self.PM_PANE], "tab create": [self.TAB], "agent list": [J({"agents": []})]})
        self.assertEqual(self.cli("worktree", "fix-a"), 0, self.err)
        self.assertEqual(self.argv_of("pane", "get"), [["pane", "get", "w1:p3"]] * 2)
        # that pane is gone: the caller's
        self.fake({"pane get": [E("pane_not_found", "pane w1:p3 not found"),
                                J({"pane": {"pane_id": "w4:p1", "workspace_id": "w4"}})],
                   "tab create": [self.TAB], "agent list": [J({"agents": []})]})
        self.assertEqual(self.cli("worktree", "fix-a"), 0, self.err)
        self.assertEqual(self.argv_of("pane", "get"), [["pane", "get", "w1:p3"], ["pane", "get", "w4:p1"]])
        create = self.argv_of("tab", "create")[0]
        self.assertEqual(create[create.index("--workspace") + 1], "w4")
        # herdr gave the PM's pane id to another pane after a restart (another terminal): the caller's
        self.fake({"pane get": [J({"pane": {"pane_id": "w1:p3", "workspace_id": "w9", "terminal_id": "term-99"}}),
                                J({"pane": {"pane_id": "w4:p1", "workspace_id": "w4"}})],
                   "tab create": [self.TAB], "agent list": [J({"agents": []})]})
        self.assertEqual(self.cli("worktree", "fix-a"), 0, self.err)
        create = self.argv_of("tab", "create")[0]
        self.assertEqual(create[create.index("--workspace") + 1], "w4")
        # the caller is the job's PM: its own pane, not the record
        os.environ["HERDR_PANE_ID"] = "w1:p3"
        self.fake({"pane get": [self.PM_PANE], "tab create": [self.TAB],
                   "agent list": [J({"agents": [{"name": self.pm, "pane_id": "w1:p3", "terminal_id": "term-3"}]})]})
        self.assertEqual(self.cli("worktree", "fix-a"), 0, self.err)
        self.assertEqual(self.argv_of("pane", "get"), [["pane", "get", "w1:p3"]])

    def test_worktree_opens_a_workspace_when_there_is_no_pm_pane(self):
        self.job("fix-a", "working")
        wt = self.wt_path("fix-a")
        self.fake({"workspace create": [J({"type": "workspace_created", "workspace": {"workspace_id": "w5"},
                                           "root_pane": {"pane_id": "w5:p1", "workspace_id": "w5"}})],
                   "pane list": [self.panes(("w5:t1", wt))], "workspace close": [J({})]})
        self.assertEqual(self.cli("worktree", "fix-a"), 0, self.err)
        self.assertEqual(json.loads(self.out), {"id": "w5", "path": wt, "place": "w5:p1"})
        self.assertEqual([a[:2] for a in self.argv()], [["workspace", "create"]])
        self.assertEqual(self.cli("worktree", "--remove", "w5", "--job", "fix-a"), 0, self.err)
        self.assertEqual(self.closes(), [["workspace", "close", "w5"]])  # its own: OKe stored that id
        # the PM's pane is gone: a workspace too
        self.pm_pane("w1:p3")
        self.fake({"workspace create": [J({"workspace": {"workspace_id": "w6"}, "root_pane": {"pane_id": "w6:p1"}})],
                   "pane get": [E("pane_not_found", "pane w1:p3 not found")],
                   "pane list": [self.panes(("w6:t1", wt))], "workspace close": [J({})]})
        self.assertEqual(self.cli("worktree", "fix-a"), 0, self.err)
        self.assertEqual(json.loads(self.out)["id"], "w6")
        self.assertEqual(self.cli("worktree", "--remove", "w6", "--job", "fix-a"), 0, self.err)
        self.assertIn(["workspace", "close", "w6"], self.argv())  # no ":t": a workspace
        self.assertFalse(os.path.exists(wt))

    def test_worktree_reuses_the_checkout_already_there(self):
        self.job("fix-a", "working")
        self.pm_pane("w1:p3")
        second = J({"type": "tab_created", "tab": {"tab_id": "w1:t5", "workspace_id": "w1"},
                    "root_pane": {"pane_id": "w1:p10", "tab_id": "w1:t5", "workspace_id": "w1"}})
        self.fake({"pane get": [self.PM_PANE], "tab create": [self.TAB, second]})
        self.assertEqual(self.cli("worktree", "fix-a"), 0, self.err)
        self.write(os.path.relpath(os.path.join(self.wt_path("fix-a"), "work.txt"), self.tmp), "kept")
        self.assertEqual(self.cli("worktree", "fix-a"), 0, self.err)  # a second tab, the same folder
        self.assertEqual(json.loads(self.out)["path"], self.wt_path("fix-a"))
        self.assertTrue(os.path.exists(os.path.join(self.wt_path("fix-a"), "work.txt")))
        self.assertEqual(len(self.argv_of("tab", "create")), 2)
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["worktree"]["tabs"], ["w1:t4", "w1:t5"])
        # --remove closes both tabs OKe opened there: on Windows a shell in either would keep the
        # folder. The user's own tab there (w1:t2) stays
        os.remove(os.path.join(self.wt_path("fix-a"), "work.txt"))
        wt = self.wt_path("fix-a")
        self.fake({"pane list": [self.panes(("w1:t2", wt), ("w1:t4", wt), ("w1:t5", wt))],
                   "tab close": [J({"type": "ok"})]})
        self.assertEqual(self.cli("worktree", "--remove", "w1:t5", "--job", "fix-a"), 0, self.err)
        self.assertEqual(self.closes(), [["tab", "close", "w1:t4"], ["tab", "close", "w1:t5"]])
        self.assertFalse(os.path.exists(self.wt_path("fix-a")))

    def test_a_folder_in_the_way_fails_and_leaves_no_branch(self):
        self.job("fix-b", "working")
        self.write(os.path.relpath(os.path.join(self.wt_path("fix-b"), "x.txt"), self.tmp), "x")  # not a worktree
        self.fake({"workspace create": [J({})]})
        self.assertEqual(self.cli("worktree", "fix-b"), 1)
        self.assertIn("worktree_failed", self.err)
        self.assertIn("oke-fix-b", self.err)
        self.assertNotEqual(oke.git_out(self.repo, "rev-parse", "-q", "--verify", "refs/heads/oke/fix-b")[0], 0)
        self.assertEqual(self.argv(), [])
        # git itself fails after making the branch (here a file where its folder goes): the
        # branch goes again, so a retry starts it where it should
        shutil.rmtree(os.path.dirname(self.wt_path("fix-b")))
        self.write(os.path.relpath(os.path.dirname(self.wt_path("fix-b")), self.tmp), "a file")
        self.assertEqual(self.cli("worktree", "fix-b"), 1)
        self.assertIn("worktree_failed", self.err)
        self.assertNotEqual(oke.git_out(self.repo, "rev-parse", "-q", "--verify", "refs/heads/oke/fix-b")[0], 0)

    def test_a_worktree_an_older_oke_made_elsewhere_is_reused(self):
        self.job("fix-a", "working")
        old = os.path.join(runtime_herdr.WORKTREES_ROOT, "toy", "oke-fix-a")  # before the folders had a hash
        self.git(self.repo, "worktree", "add", "-q", old, "-b", "oke/fix-a")
        self.fake({"workspace create": [J({"workspace": {"workspace_id": "w5"}, "root_pane": {"pane_id": "w5:p1"}})]})
        self.assertEqual(self.cli("worktree", "fix-a"), 0, self.err)
        self.assertEqual(oke.normalize_path(json.loads(self.out)["path"]), oke.normalize_path(old))
        self.assertFalse(os.path.exists(self.wt_path("fix-a")))
        self.git(self.repo, "checkout", "-q", "-b", "oke/fix-b")  # the main checkout never counts
        self.assertIsNone(runtime_herdr.checkout_of(self.repo, "oke/fix-b"))

    def test_a_job_in_a_subfolder_gets_the_folder_of_its_repository(self):
        sub = os.path.join(self.repo, "pkg")
        os.makedirs(sub)
        self.assertEqual(oke.normalize_path(runtime_herdr.toplevel(sub)), oke.normalize_path(self.repo))
        self.assertEqual(runtime_herdr.toplevel(self.path("nogit")), self.path("nogit"))

    def test_worktree_folders_are_named_per_repository(self):
        root = runtime_herdr.worktree_folder(os.path.abspath(os.sep), "oke/x")  # a drive root has no name
        self.assertEqual(os.path.dirname(os.path.dirname(root)), runtime_herdr.WORKTREES_ROOT)
        self.assertTrue(os.path.basename(os.path.dirname(root)).startswith("root-"))
        self.assertEqual(os.path.basename(root), "oke-x")
        self.assertEqual(runtime_herdr.worktree_folder(self.repo + os.sep, "oke/x"), self.wt_path("x"))
        self.assertTrue(os.path.basename(os.path.dirname(self.wt_path("x"))).startswith("toy-"))
        # another repository named toy never shares the folder
        twin = self.git_repo(os.path.join("elsewhere", "toy"))
        self.assertNotEqual(runtime_herdr.worktree_folder(twin, "oke/x"), self.wt_path("x"))

    def test_worktree_places_never_names_a_tab_with_a_pane_elsewhere(self):
        self.job("fix-a", "working")
        wt = self.wt_path("fix-a")
        pm = {"pane_id": "w1:p1", "tab_id": "w1:t1", "workspace_id": "w1", "cwd": oke.pm_dir(self.pm)}
        reviewer = {"pane_id": "w1:p2", "tab_id": "w1:t1", "workspace_id": "w1", "cwd": wt}  # split next to the PM
        worker = {"pane_id": "w1:p5", "tab_id": "w1:t2", "workspace_id": "w1", "cwd": "\\\\?\\" + wt + "\\"}
        shell = {"pane_id": "w1:p6", "tab_id": "w1:t2", "workspace_id": "w1", "cwd": os.path.join(wt, "src")}
        other = {"pane_id": "w1:p7", "tab_id": "w1:t3", "workspace_id": "w1", "cwd": wt + "-old"}
        rt = runtime_herdr.HerdrRuntime()
        self.fake({"pane list": [J({"panes": [pm, reviewer, worker, shell, other]})]})
        every = ["w1:t1", "w1:t2", "w1:t3"]  # as if OKe had opened them all (herdr reuses ids)
        self.assertEqual(rt.worktree_places(self.repo, wt, own=every), ["w1:t2"])
        self.assertEqual(rt.worktree_places(self.repo, wt), [])  # a tab OKe did not open: the user's
        self.fake({"pane list": [J({"panes": [pm, reviewer, other]})]})
        self.assertEqual(rt.worktree_places(self.repo, wt, own=every), [])  # the PM's tab is never closed
        alone = dict(worker, tab_id="w5:t1", workspace_id="w5")
        self.fake({"pane list": [J({"panes": [pm, alone]})]})
        self.assertEqual(rt.worktree_places(self.repo, wt, own=["w5"]), ["w5"])  # the workspace OKe made
        self.assertEqual(rt.worktree_places(self.repo, wt, own=["w5:t1"]), ["w5:t1"])  # a tab OKe made there
        self.assertEqual(rt.worktree_places(self.repo, wt), [])  # one the user opened: nothing

    def git_half_delete(self, wt, keep_git_file):
        """What git's `worktree remove` leaves when Windows locks a file: its record of the
        worktree and the files it could delete are gone, the locked file stays."""
        with io.open(os.path.join(wt, ".git"), encoding="utf-8") as f:
            admin = f.read().split("gitdir:", 1)[1].strip()
        shutil.rmtree(admin)
        for name in os.listdir(wt):
            if name not in ("locked.txt",) + ((".git",) if keep_git_file else ()):
                p = os.path.join(wt, name)
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        raise oke.HerdrError("worktree_failed", "error: failed to delete '%s': Permission denied" % wt)

    def test_remove_waits_for_the_closed_tab_and_finishes_a_half_deleted_folder(self):
        self.job("fix-a", "working")
        self.pm_pane("w1:p3")
        self.fake({"pane get": [self.PM_PANE], "tab create": [self.TAB]})
        self.assertEqual(self.cli("worktree", "fix-a"), 0, self.err)
        wt = self.wt_path("fix-a")
        self.write(os.path.relpath(os.path.join(wt, "locked.txt"), self.tmp), "x")
        runtime_herdr.CLOSE_WAIT = 30
        # a reviewer split next to the PM sits in the worktree: never closed, never waited for
        split = {"pane_id": "w1:p7", "tab_id": "w1:t1", "workspace_id": "w1", "cwd": wt}

        def listing(*tabs):
            out = json.loads(self.panes(*tabs)["stdout"])
            out["result"]["panes"].append(split)
            return {"stdout": json.dumps(out), "exit": 0}
        # herdr still lists the closed tab's pane right after the close, then no more
        self.fake({"pane list": [listing(("w1:t4", wt)), listing(("w1:t4", wt)), listing()],
                   "tab close": [J({"type": "ok"})]})
        remove = oke.runtime_bg.git_worktree_remove
        tries = []

        def locked_once(*a, **k):
            tries.append(len(self.argv_of("pane", "list")))
            if len(tries) == 1:
                self.git_half_delete(wt, keep_git_file=True)
            return remove(*a, **k)  # git: "is not a working tree" from now on
        oke.runtime_bg.git_worktree_remove = locked_once
        self.addCleanup(setattr, oke.runtime_bg, "git_worktree_remove", remove)
        self.assertEqual(self.cli("worktree", "--remove", "w1:t4", "--job", "fix-a"), 0, self.err)
        self.assertEqual(tries, [3, 3])  # after the closed tab's pane left (the lookup, then two waits)
        self.assertFalse(os.path.exists(wt))  # the rest deleted directly
        self.assertNotIn("oke-fix-a", oke.git_out(self.repo, "worktree", "list")[1])
        # the checkout's .git file went too: git is not asked again
        wt2 = self.wt_path("fix-c")
        runtime_herdr.git_worktree_add(self.repo, wt2, "oke/fix-c")
        self.write(os.path.relpath(os.path.join(wt2, "locked.txt"), self.tmp), "x")
        tries[:] = []
        oke.runtime_bg.git_worktree_remove = lambda *a, **k: tries.append(1) or self.git_half_delete(wt2, False)
        runtime_herdr.remove_checkout(wt2, self.repo, False)
        self.assertEqual(tries, [1])
        self.assertFalse(os.path.exists(wt2))
        # a folder OKe did not make, outside its worktree folders: never deleted by hand
        # a folder with no .git where OKe makes worktrees: deleted only on --force (read-only files too)
        odd = self.wt_path("fix-d")
        self.write(os.path.relpath(os.path.join(odd, "ro.txt"), self.tmp), "x")
        os.chmod(os.path.join(odd, "ro.txt"), stat.S_IREAD)
        with self.assertRaises(oke.HerdrError):
            runtime_herdr.remove_checkout(odd, self.repo, False)
        self.assertTrue(os.path.isdir(odd))
        runtime_herdr.remove_checkout(odd, self.repo, True)
        self.assertFalse(os.path.exists(odd))
        plain = os.path.dirname(self.write("plain/a.txt", "a"))
        with self.assertRaises(oke.HerdrError):
            runtime_herdr.remove_checkout(plain, self.repo, False)
        self.assertTrue(os.path.exists(os.path.join(plain, "a.txt")))
        # another refusal (changes in it) is not retried
        oke.runtime_bg.git_worktree_remove = lambda *a, **k: tries.append(0) or (_ for _ in ()).throw(
            oke.HerdrError("worktree_failed", "contains modified or untracked files, use --force"))
        tries[:] = []
        with self.assertRaises(oke.HerdrError):
            runtime_herdr.remove_checkout(self.repo, self.repo, False)
        self.assertEqual(tries, [0])

    def test_an_id_no_job_names_is_closed_only_in_that_jobs_worktree(self):
        # after a herdr restart fix-b ran `worktree fix-b` twice: its first tab got w1:t4, fix-a's
        # old id (so fix-a's record lost it), and fix-b names its second tab, w1:t5
        self.job("fix-a", "working")
        self.job("fix-b", "working")
        wt_b = self.wt_path("fix-b")
        state = oke.load_state()
        state["jobs"]["fix-a"].update(phase="done", worktree={"id": None, "path": self.path("wt-a")})
        state["jobs"]["fix-b"]["worktree"] = {"id": "w1:t5", "path": wt_b, "tabs": ["w1:t4", "w1:t5"]}
        oke.save_state(state)
        self.fake({"pane list": [self.panes(("w1:t4", wt_b), ("w1:t5", wt_b))], "tab close": [J({})]})
        os.makedirs(self.path("wt-a"))  # fix-a's checkout is still there
        self.assertEqual(self.cli("worktree", "--remove", "w1:t4", "--job", "fix-a"), 1)
        self.assertIn("nothing closed", self.err)
        self.assertEqual(self.closes(), [])  # fix-b's working tab stays
        # fix-a's checkout is gone too: nothing of fix-a's is left, so that is success
        os.rmdir(self.path("wt-a"))
        self.assertEqual(self.cli("worktree", "--remove", "w1:t4", "--job", "fix-a"), 0, self.err)
        self.assertEqual(json.loads(self.out), {"removed": "w1:t4", "already": True})
        self.assertEqual(self.closes(), [])
        self.assertEqual(self.cli("worktree", "--remove", "w1:t4"), 1)  # fix-b still works
        self.assertEqual(self.closes(), [])
        state = oke.load_state()
        state["jobs"]["fix-b"]["phase"] = "done"
        oke.save_state(state)
        self.assertEqual(self.cli("worktree", "--remove", "w1:t4"), 0, self.err)  # a finished job's tab
        self.assertEqual(self.closes(), [["tab", "close", "w1:t4"]])

    def test_worktree_remove_after_its_tab_closed(self):
        # a hold closed the worker's pane, the tab's last: herdr closed the tab with it
        self.job("fix-a", "reviewing")
        self.pm_pane("w1:p3")
        self.fake({"pane get": [self.PM_PANE], "tab create": [self.TAB]})
        self.assertEqual(self.cli("worktree", "fix-a"), 0, self.err)
        wt = self.wt_path("fix-a")
        # a worker revived after the unhold: its shell sits in the checkout, outside the tab
        oke.write_json_atomic(oke.children_path(self.pm), {
            "fix-a-w1": {"job": "fix-a", "role": "worker", "pane": "w1:p8", "terminal": "term-8"}})
        self.fake({"pane list": [self.panes()],
                   "pane get": [J({"pane": {"pane_id": "w1:p8", "terminal_id": "term-8"}})],
                   "pane close": [J({"type": "ok"})]})
        remove = oke.runtime_bg.git_worktree_remove
        closed_first = []
        oke.runtime_bg.git_worktree_remove = lambda *a, **k: closed_first.append(
            ["pane", "close", "w1:p8"] in self.argv()) or remove(*a, **k)
        self.addCleanup(setattr, oke.runtime_bg, "git_worktree_remove", remove)
        self.assertEqual(self.cli("worktree", "--remove", "w1:t4", "--job", "fix-a"), 0, self.err)
        self.assertEqual(closed_first, [True])  # Windows: git cannot delete a folder a shell is in
        self.assertEqual(self.argv_of("tab", "close"), [])  # nothing open there: nothing closed
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(oke.git_out(self.repo, "rev-parse", "-q", "--verify", "oke/fix-a")[0], 0)  # the branch stays
        self.fake({"pane list": [self.panes()], "workspace close": [J({})]})
        self.assertEqual(self.cli("worktree", "--remove", "w8"), 1)  # no job's: nothing closed
        self.assertIn("nothing closed", self.err)
        self.assertEqual(self.closes(), [])

    def test_worktree_remove_of_a_folder_already_deleted(self):
        # the user deleted it, or a Windows delete stopped half way: git's list is pruned
        self.job("fix-a", "reviewing")
        wt = self.path("wt-fix-a")
        self.git(self.repo, "worktree", "add", "-q", wt, "-b", "oke/fix-a")
        state = oke.load_state()
        state["jobs"]["fix-a"]["worktree"] = {"id": "w1:t7", "path": wt}
        oke.save_state(state)
        shutil.rmtree(wt)
        self.fake({"pane list": [self.panes()]})
        self.assertEqual(self.cli("worktree", "--remove", "w1:t7", "--job", "fix-a"), 0, self.err)
        self.assertNotIn("wt-fix-a", oke.git_out(self.repo, "worktree", "list")[1])

    def test_a_workspace_id_another_job_works_in_is_never_removed(self):
        # herdr reused w1:t7 for fix-b after a restart; fix-a's PM still names it
        self.job("fix-b", "working")
        state = oke.load_state()
        state["jobs"]["fix-b"]["worktree"] = {"id": "w1:t7", "path": self.path("wt-fix-b")}
        oke.save_state(state)
        self.fake({"tab close": [J({})]})
        self.assertEqual(self.cli("worktree", "--remove", "w1:t7"), 1)
        self.assertIn("fix-b", self.err)
        self.assertEqual(self.argv(), [])  # herdr was never asked

    def test_with_job_a_workspace_id_of_another_open_job_is_never_removed(self):
        self.job("fix-b", "reviewing")  # approved while the PM still finishes fix-a
        state = oke.load_state()
        state["jobs"]["fix-b"]["worktree"] = {"id": "w1:t3", "path": self.path("wt-fix-b")}
        oke.save_state(state)
        self.fake({"tab close": [J({})], "pane list": [self.panes(("w1:t3", self.path("wt-fix-b")))]})
        self.assertEqual(self.cli("worktree", "--remove", "w1:t3", "--job", "fix-a"), 1)
        self.assertIn("fix-b", self.err)
        self.assertEqual(self.argv(), [])  # herdr was never asked
        self.assertEqual(self.cli("worktree", "--remove", "w1:t3", "--job", "fix-b"), 0, self.err)
        self.assertEqual(self.closes(), [["tab", "close", "w1:t3"]])

    def test_a_reused_id_never_closes_what_is_not_the_jobs_now(self):
        # herdr reuses ids after a restart
        self.job("fix-a", "working")
        self.job("fix-b", "working")
        self.pm_pane("w1:p3")
        state = oke.load_state()
        state["jobs"]["fix-a"]["worktree"] = {"id": "w1:t4", "path": self.path("wt-old")}
        oke.save_state(state)
        self.fake({"pane get": [self.PM_PANE], "tab create": [self.TAB]})
        self.assertEqual(self.cli("worktree", "fix-b"), 0, self.err)
        self.assertIsNone(oke.load_state()["jobs"]["fix-a"]["worktree"]["id"])  # the stale id is dropped, not the folder
        wt = self.wt_path("fix-b")
        # two jobs naming one id (an older state): only a tab open in a job's worktree is closed,
        # and no folder is removed
        state = oke.load_state()
        state["jobs"]["fix-a"]["worktree"] = {"id": "w1:t4", "path": self.path("wt-old")}
        for job in ("fix-a", "fix-b"):
            state["jobs"][job]["phase"] = "done"  # an open job's id is refused before any of this
        oke.save_state(state)
        self.fake({"pane list": [self.panes(("w1:t4", oke.pm_dir(self.pm)))], "tab close": [J({})]})
        self.assertEqual(self.cli("worktree", "--remove", "w1:t4"), 1)  # w1:t4 is somewhere else now
        self.assertEqual(self.closes(), [])
        self.fake({"pane list": [self.panes(("w1:t4", wt))], "tab close": [J({})]})
        self.assertEqual(self.cli("worktree", "--remove", "w1:t4"), 1)  # closed, but its checkout stays
        self.assertEqual(json.loads(self.out), {"closed": "w1:t4", "checkout_left": wt})
        self.assertEqual(self.closes(), [["tab", "close", "w1:t4"]])
        self.assertTrue(os.path.exists(wt))
        # fix-b's fallback workspace was w2; after a restart w2 is the PM's workspace
        state = oke.load_state()
        state["jobs"]["fix-a"]["worktree"] = {"id": None, "path": self.path("wt-old")}
        state["jobs"]["fix-b"]["worktree"]["id"] = "w2"
        oke.save_state(state)
        pm = {"pane_id": "w2:p1", "tab_id": "w2:t1", "workspace_id": "w2", "cwd": oke.pm_dir(self.pm)}
        self.fake({"pane list": [J({"panes": [pm]})], "workspace close": [J({})], "tab close": [J({})]})
        self.assertEqual(self.cli("worktree", "--remove", "w2", "--job", "fix-b"), 0, self.err)
        self.assertEqual(self.closes(), [])  # the PM's workspace stays
        self.assertFalse(os.path.exists(wt))

    def test_a_tab_herdr_lost_is_never_oke_s_again(self):
        self.job("fix-a", "working")
        self.pm_pane("w1:p3")
        self.fake({"pane get": [self.PM_PANE], "tab create": [self.TAB]})
        self.assertEqual(self.cli("worktree", "fix-a"), 0, self.err)
        wt = self.wt_path("fix-a")
        rt = runtime_herdr.HerdrRuntime()
        self.fake({"pane list": [self.panes(("w1:t4", wt))]})
        oke.forget_gone_tabs(rt)  # still there: kept
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["worktree"]["tabs"], ["w1:t4"])
        self.fake({"pane list": [self.panes()]})  # herdr restarted: no w1:t4
        oke.forget_gone_tabs(rt)
        self.assertEqual(oke.load_state()["jobs"]["fix-a"]["worktree"]["tabs"], [])
        # the user opens a tab in the checkout and herdr gives it w1:t4: OKe leaves it open
        self.fake({"pane list": [self.panes(("w1:t4", wt))], "tab close": [J({})]})
        self.assertEqual(self.cli("worktree", "--remove", "w1:t4", "--job", "fix-a"), 0, self.err)
        self.assertEqual(self.closes(), [])

    def test_herdr_refusal_is_exit_1(self):
        self.fake({"agent focus": [E("agent_not_found")]})
        self.assertEqual(self.cli("attach", "nobody"), 1)


class TestSpawnNextToCaller(SpawnBase):
    def test_spawn_without_place_splits_from_own_pane(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        self.fake(spawn_ok())
        os.environ["HERDR_PANE_ID"] = "w1:p2"
        self.assertEqual(self.cli("spawn", a), 0)
        self.assertIn(["pane", "split", "--pane", "w1:p2"], [c[:4] for c in self.argv()])

    def test_spawn_without_place_outside_herdr_fails(self):
        self.job("fix-a", "working")
        a = self.assignment("fix-a-w1", "worker", "fix-a")
        self.fake(spawn_ok())
        self.assertEqual(self.cli("spawn", a), 1)
        self.assertIn("give --pane or --split-from", self.err)

    argv = TestRuntimeCommands.argv


if __name__ == "__main__":
    unittest.main()
