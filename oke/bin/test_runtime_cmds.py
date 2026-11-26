"""The runtime commands the skills call instead of herdr (runtime layer, step 2)."""
import json
import os
import shutil
import unittest

import oke
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

    def test_worktree_create_and_remove(self):
        ws = {"workspace_id": "w7", "worktree": {"checkout_path": "/wt/fix-a"}}
        self.fake({"worktree create": [J({"type": "worktree_created", "workspace": ws})],
                   "pane list": [J({"panes": [{"pane_id": "w7:p1"}]})],
                   "worktree remove": [J({})]})
        self.assertEqual(self.cli("worktree", "fix-a"), 1)  # no such job
        self.job("fix-a", "working")
        self.assertEqual(self.cli("worktree", "fix-a"), 0, self.err)
        self.assertEqual(json.loads(self.out), {"id": "w7", "path": "/wt/fix-a", "place": "w7:p1"})
        self.assertIn("--branch oke/fix-a", " ".join(self.argv()[0]))
        self.assertEqual(self.cli("worktree", "--remove", "w7", "--job", "fix-a"), 0, self.err)
        self.assertEqual(self.argv()[-1][-2:], ["--workspace", "w7"])

    def test_worktree_remove_after_its_workspace_closed(self):
        # a hold closed the worker's pane, the worktree's last: herdr forgot the workspace
        self.job("fix-a", "merging")
        wt = self.path("wt-fix-a")
        self.git(self.repo, "worktree", "add", "-q", wt, "-b", "oke/fix-a")
        ws = {"workspace_id": "w7", "worktree": {"checkout_path": wt}}
        self.fake({"worktree create": [J({"type": "worktree_created", "workspace": ws})],
                   "pane list": [J({"panes": [{"pane_id": "w7:p1"}]})],
                   "worktree remove": [E("workspace_not_found", "workspace w7 not found")]})
        self.assertEqual(self.cli("worktree", "fix-a"), 0, self.err)
        # a worker revived after the unhold: its shell sits in the checkout, outside the workspace
        oke.write_json_atomic(oke.children_path(self.pm), {
            "fix-a-w1": {"job": "fix-a", "role": "worker", "pane": "w1:p8", "terminal": "term-8"}})
        self.fake({"worktree remove": [E("workspace_not_found", "workspace w7 not found")],
                   "pane get": [J({"pane": {"pane_id": "w1:p8", "terminal_id": "term-8"}})],
                   "pane close": [J({"type": "ok"})]})
        remove = oke.runtime_bg.git_worktree_remove
        closed_first = []
        oke.runtime_bg.git_worktree_remove = lambda *a: closed_first.append(
            ["pane", "close", "w1:p8"] in self.argv()) or remove(*a)
        self.addCleanup(setattr, oke.runtime_bg, "git_worktree_remove", remove)
        self.assertEqual(self.cli("worktree", "--remove", "w7", "--job", "fix-a"), 0, self.err)
        self.assertEqual(closed_first, [True])  # Windows: git cannot delete a folder a shell is in
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(oke.git_out(self.repo, "rev-parse", "-q", "--verify", "oke/fix-a")[0], 0)  # the branch stays, as herdr leaves it
        self.assertEqual(self.cli("worktree", "--remove", "w8"), 1)  # unknown id: herdr's error

    def test_worktree_remove_of_a_folder_already_deleted(self):
        # the user deleted it, or a Windows delete stopped half way: git's list is pruned
        self.job("fix-a", "merging")
        wt = self.path("wt-fix-a")
        self.git(self.repo, "worktree", "add", "-q", wt, "-b", "oke/fix-a")
        state = oke.load_state()
        state["jobs"]["fix-a"]["worktree"] = {"id": "w7", "path": wt}
        oke.save_state(state)
        shutil.rmtree(wt)
        self.fake({"worktree remove": [E("workspace_not_found", "workspace w7 not found")]})
        self.assertEqual(self.cli("worktree", "--remove", "w7", "--job", "fix-a"), 0, self.err)
        self.assertNotIn("wt-fix-a", oke.git_out(self.repo, "worktree", "list")[1])

    def test_a_workspace_id_another_job_works_in_is_never_removed(self):
        # herdr reused w7 for fix-b after a restart; fix-a's PM still names it
        self.job("fix-b", "working")
        state = oke.load_state()
        state["jobs"]["fix-b"]["worktree"] = {"id": "w7", "path": self.path("wt-fix-b")}
        oke.save_state(state)
        self.fake({"worktree remove": [J({})]})
        self.assertEqual(self.cli("worktree", "--remove", "w7"), 1)
        self.assertIn("fix-b", self.err)
        self.assertEqual(self.argv(), [])  # herdr was never asked

    def test_with_job_a_workspace_id_of_another_merging_job_is_never_removed(self):
        self.job("fix-b", "merging")  # approved while the PM still finishes fix-a
        state = oke.load_state()
        state["jobs"]["fix-b"]["worktree"] = {"id": "w3", "path": self.path("wt-fix-b")}
        oke.save_state(state)
        self.fake({"worktree remove": [J({})]})
        self.assertEqual(self.cli("worktree", "--remove", "w3", "--job", "fix-a"), 1)
        self.assertIn("fix-b", self.err)
        self.assertEqual(self.argv(), [])  # herdr was never asked
        self.assertEqual(self.cli("worktree", "--remove", "w3", "--job", "fix-b"), 0, self.err)
        self.assertEqual(self.argv()[-1][-2:], ["--workspace", "w3"])

    def test_a_reused_workspace_id_never_removes_another_jobs_worktree(self):
        # herdr reuses workspace ids after a restart
        self.job("fix-a", "working")
        self.job("fix-b", "working")
        wt = self.path("wt-fix-b")
        self.git(self.repo, "worktree", "add", "-q", wt, "-b", "oke/fix-b")
        state = oke.load_state()
        state["jobs"]["fix-a"]["worktree"] = {"id": "w7", "path": self.path("wt-old")}
        oke.save_state(state)
        ws = {"workspace_id": "w7", "worktree": {"checkout_path": wt}}
        self.fake({"worktree create": [J({"type": "worktree_created", "workspace": ws})],
                   "pane list": [J({"panes": [{"pane_id": "w7:p1"}]})],
                   "worktree remove": [E("workspace_not_found", "workspace w7 not found")]})
        self.assertEqual(self.cli("worktree", "fix-b"), 0, self.err)
        self.assertNotIn("worktree", oke.load_state()["jobs"]["fix-a"])  # the stale one is dropped
        # two jobs naming one id (an older state): refused, nothing removed
        state = oke.load_state()
        state["jobs"]["fix-a"]["worktree"] = {"id": "w7", "path": self.path("wt-old")}
        oke.save_state(state)
        self.assertEqual(self.cli("worktree", "--remove", "w7"), 1)
        self.assertTrue(os.path.exists(wt))

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
