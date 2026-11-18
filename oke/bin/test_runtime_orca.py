"""OrcaRuntime against a fake `orca` binary (the fake herdr script, which answers any
argv from a scenario)."""
import io
import json
import os
import unittest

from test_oke import SpawnBase  # noqa: F401
import oke
import runtime_orca


def OK(result):
    return {"stdout": json.dumps({"ok": True, "result": result}), "exit": 0}


def ERR(code):
    return {"stdout": json.dumps({"ok": False, "error": {"code": code, "message": code + " here"}}), "exit": 1}


STARTED = {"dispatchId": "ctx_1", "terminal": "structworker_1", "state": "ready"}
WORKING = {"liveness": "live", "activity": "working", "terminalState": "retained"}


class OrcaBase(SpawnBase):
    def fake(self, scenario):
        SpawnBase.fake(self, scenario)
        os.environ["OKE_ORCA"] = os.environ["OKE_HERDR"]

    def setUp(self):
        SpawnBase.setUp(self)
        self.rt = runtime_orca.OrcaRuntime()

    def register(self, name="fix-a-w1", dispatch="ctx_1", handle="structworker_1"):
        reg = runtime_orca.load_registry()
        reg[name] = {"dispatch": dispatch, "handle": handle, "cwd": self.repo}
        runtime_orca.save_registry(reg)


class TestOrcaRuntime(OrcaBase):
    def test_status_mapping(self):
        s = runtime_orca.status_of
        self.assertEqual(s(WORKING), "working")
        self.assertEqual(s(dict(WORKING, activity="idle")), "idle")
        self.assertEqual(s(dict(WORKING, activity="waiting")), "blocked")  # a permission prompt
        self.assertEqual(s(dict(WORKING, activity=None)), "working")  # unknown is never dead
        settled = {"liveness": "unverifiable", "stage": "settled", "state": "succeeded"}
        self.assertEqual(s(settled), "idle")  # seen live: a PM done with its task
        self.assertIsNone(s(dict(WORKING, liveness="exited")))
        self.assertIsNone(s(dict(WORKING, terminalState="released")))
        self.assertIsNone(s(None))  # Orca no longer knows the dispatch

    def test_start_runs_a_kept_open_worker_with_the_model_and_records_it(self):
        self.fake({"orchestration worker-start": [OK(STARTED)]})
        seen = []
        info = self.rt.start("fix-a-w1", self.repo, ["--model", "opus", "--plugin-dir", "/p"], "go",
                             lambda: False, lambda: seen.append(1))
        argv = self.calls()[0]
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertNotIn("--plugin-dir", argv)
        self.assertIn("--keep-open", argv)
        self.assertEqual(argv[argv.index("--worktree") + 1], "path:" + self.repo.replace("\\", "/"))
        spec = argv[argv.index("--spec") + 1]
        self.assertTrue(spec.endswith("go") and "worker_done" in spec)
        self.assertEqual((info["pane_id"], info["agent_session"]), ("structworker_1", {"value": "ctx_1"}))
        self.assertEqual(seen, [1])
        self.assertEqual(runtime_orca.load_registry()["fix-a-w1"]["dispatch"], "ctx_1")

    def test_a_revive_gets_the_dead_sessions_transcript(self):
        projects = os.path.join(self.path("claude"), "projects", "x")
        os.makedirs(projects)
        with io.open(os.path.join(projects, "s.jsonl"), "w", encoding="utf-8") as f:
            f.write(u'{"text": "Your dispatch is ctx_old"}\n')
        os.environ["CLAUDE_CONFIG_DIR"] = self.path("claude")
        self.fake({"orchestration worker-start": [OK(STARTED)]})
        self.rt.start("fix-a-w1", self.repo, ["--resume", "ctx_old"], "go", lambda: False)
        spec = self.calls()[0][self.calls()[0].index("--spec") + 1]
        self.assertIn("s.jsonl", spec)
        self.assertNotIn("--resume", self.calls()[0])

    def test_agents_drop_gone_workers(self):
        self.register()
        self.register("pm-toy", "ctx_2", "structworker_2")
        self.fake({"orchestration worker-show --dispatch ctx_1": [OK(WORKING)],
                   "orchestration worker-show --dispatch ctx_2": [OK(dict(WORKING, liveness="lost"))]})
        self.assertEqual([(a["name"], a["agent_status"]) for a in self.rt.agents()], [("fix-a-w1", "working")])
        with self.assertRaises(oke.HerdrError) as c:
            self.rt.agent("pm-toy")
        self.assertEqual(c.exception.code, "agent_not_found")

    def test_an_unknown_dispatch_is_gone(self):
        self.register()
        self.fake({"orchestration worker-show": [ERR("dispatch_not_found")]})
        self.assertEqual(self.rt.agents(), [])

    def test_the_registry_survives_parallel_writers(self):
        import threading
        names = ["w%d" % i for i in range(8)]
        threads = [threading.Thread(target=runtime_orca.update_registry,
                                    args=(lambda r, n=n: r.__setitem__(n, {"dispatch": n}),)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(runtime_orca.load_registry()), names)

    def test_orca_down_is_unavailable_not_dead(self):
        self.register()
        self.fake({"orchestration worker-show": [ERR("runtime_unavailable")]})
        with self.assertRaises(oke.HerdrError) as c:
            self.rt.agents()
        self.assertEqual(c.exception.code, "herdr_unavailable")

    def test_prompt_is_orca_mail_to_the_dispatch(self):
        self.register()
        self.fake({"orchestration send": [OK({"id": "msg_1"})]})
        self.assertTrue(self.rt.prompt("fix-a-w1", "OKe inbox: go"))
        argv = self.calls()[0]
        self.assertEqual(argv[argv.index("--to") + 1], "dispatch:ctx_1")
        self.assertEqual(argv[argv.index("--body") + 1], "OKe inbox: go")

    def test_close_stops_and_forgets(self):
        self.register()
        self.fake({"orchestration worker-stop": [ERR("already_settled")]})
        self.rt.close("structworker_1")
        self.assertEqual(runtime_orca.load_registry(), {})
        self.rt.close(self.repo)  # a folder: nothing started there

    def test_worktree_is_an_orca_worktree_on_the_branch(self):
        self.fake({"worktree create": [OK({"worktree": {"id": "r::/w", "path": "/w"}})],
                   "worktree rm": [OK({})]})
        wt = self.rt.worktree_create(self.repo, "oke/fix-a", "fix-a")
        self.assertEqual(wt, {"id": "r::/w", "path": "/w", "place": "/w"})
        argv = self.calls()[0]
        self.assertEqual(argv[argv.index("--branch") + 1], "oke/fix-a")
        self.rt.worktree_remove("r::/w")
        self.assertEqual(self.calls()[1][:4], ["worktree", "rm", "--worktree", "id:r::/w"])

    def test_a_plain_folder_is_added_to_orca_as_a_folder(self):
        plain = self.path("plain")
        os.makedirs(plain)
        self.fake({"repo add": [OK({}), OK({})]})
        self.rt.place_for_repo(self.repo, "r")
        self.rt.place_for_repo(plain, "p")
        kinds = [argv[argv.index("--kind") + 1] for argv in self.calls()]
        self.assertEqual(kinds, ["git", "folder"])

    def test_screen_is_the_transcript_end(self):
        self.register()
        msgs = [{"role": "assistant", "blocks": [{"type": "text", "text": "Allow edit?"}]}]
        self.fake({"orchestration worker-read": [OK({"transcript": {"messages": msgs}})]})
        self.assertEqual(self.rt.screen("fix-a-w1"), "assistant: Allow edit?\n")


class TestOrcaChoice(OrcaBase):
    def test_inside_orca_the_default_is_orca_and_children_are_not_lean(self):
        del os.environ["OKE_RUNTIME"]
        os.environ["ORCA_TERMINAL_HANDLE"] = "term_1"
        self.assertEqual(oke.runtime().name, "orca")
        self.assertEqual(self.rt.whoami(), "term_1")
        _, args = oke.agent_command("claude", "worker", oke.load_config(), self.home, "/p", None)
        self.assertNotIn("--settings", args)
        os.environ["HERDR_ENV"] = "1"
        self.assertEqual(oke.runtime().name, "herdr")

    def test_tick_knows_the_orchestrator_orca_does_not_list(self):
        os.environ["OKE_RUNTIME"] = "orca"
        state = oke.load_state()
        state["orchestrator"] = {"pane": "term_1", "agent": "orchestrator"}
        oke.save_state(state)
        self.fake({})  # no workers started yet: Orca lists nobody
        self.assertEqual(self.cli("tick", "--pane", "term_1", "--board"), 0, self.err)
        self.assertEqual(json.loads(self.out)["role"], "orchestrator")
        self.assertEqual(self.cli("tick", "--pane", "term_2"), 0, self.err)
        self.assertIsNone(json.loads(self.out)["role"])


if __name__ == "__main__":
    unittest.main()
