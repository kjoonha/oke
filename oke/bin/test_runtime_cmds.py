"""The runtime commands the skills call instead of herdr (runtime layer, step 2)."""
import json
import os
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

    def test_place_splits_next_to_a_pane_in_the_repo(self):
        self.fake({"pane list": [J({"panes": [{"pane_id": "w1:p1", "cwd": "/elsewhere"},
                                              {"pane_id": "w2:p3", "cwd": self.repo}]})],
                   "pane split": [J({"pane": {"pane_id": "w2:p4"}})]})
        self.assertEqual(self.cli("place", self.repo), 0)
        self.assertEqual(json.loads(self.out), {"place": "w2:p4"})
        self.assertIn("--pane w2:p3", " ".join(self.argv()[-1]))

    def test_place_opens_a_workspace_when_no_pane_is_in_the_repo(self):
        self.fake({"pane list": [J({"panes": []})],
                   "workspace create": [J({"root_pane": {"pane_id": "w5:p1"}})]})
        self.assertEqual(self.cli("place", self.repo), 0)
        self.assertEqual(json.loads(self.out), {"place": "w5:p1"})

    def test_place_refuses_a_repo_id(self):
        self.fake({})
        self.assertEqual(self.cli("place", "calc-1b3776"), 1)
        self.assertIn("path", self.err)
        self.assertEqual(self.argv(), [])

    def test_worktree_create_and_remove(self):
        ws = {"workspace_id": "w7", "worktree": {"checkout_path": "/wt/fix-a"}}
        self.fake({"worktree create": [J({"type": "worktree_created", "workspace": ws})],
                   "pane list": [J({"panes": [{"pane_id": "w7:p1"}]})],
                   "worktree remove": [J({})]})
        self.assertEqual(self.cli("worktree", "fix-a", "--repo", self.repo), 0)
        self.assertEqual(json.loads(self.out), {"id": "w7", "path": "/wt/fix-a", "place": "w7:p1"})
        self.assertIn("--branch oke/fix-a", " ".join(self.argv()[0]))
        self.assertEqual(self.cli("worktree", "--remove", "w7"), 0)
        self.assertEqual(self.argv()[-1][-2:], ["--workspace", "w7"])

    def test_worktree_remove_after_its_workspace_closed(self):
        # a hold closed the worker's pane, the worktree's last: herdr forgot the workspace
        self.job("fix-a", "working")
        wt = self.path("wt-fix-a")
        self.git(self.repo, "worktree", "add", "-q", wt, "-b", "oke/fix-a")
        ws = {"workspace_id": "w7", "worktree": {"checkout_path": wt}}
        self.fake({"worktree create": [J({"type": "worktree_created", "workspace": ws})],
                   "pane list": [J({"panes": [{"pane_id": "w7:p1"}]})],
                   "worktree remove": [E("workspace_not_found", "workspace w7 not found")]})
        self.assertEqual(self.cli("worktree", "fix-a", "--repo", self.repo), 0, self.err)
        self.assertEqual(self.cli("worktree", "--remove", "w7"), 0, self.err)
        self.assertFalse(os.path.exists(wt))
        self.assertEqual(oke.git_out(self.repo, "rev-parse", "-q", "--verify", "oke/fix-a")[0], 0)  # the branch stays, as herdr leaves it
        self.assertEqual(self.cli("worktree", "--remove", "w8"), 1)  # unknown id: herdr's error

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
        self.assertEqual(self.cli("worktree", "fix-b", "--repo", self.repo), 0, self.err)
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
